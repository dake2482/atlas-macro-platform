"""Metadata-only adapters for official U.S. government news feeds.

The upstream feeds sometimes embed release bodies in ``description`` or
``content`` elements.  This module deliberately never reads those elements and
does not persist raw feed payloads.  Only the original headline, publication
time, fixed source attribution, canonical feed link, and a short Atlas category
are normalized into :class:`~research.models.NewsItem`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import IngestionRun, NewsItem, Source
from .providers import HTTPProvider, ProviderResult

SOURCE_POLICIES: dict[str, dict[str, str]] = {
    "sec": {
        "source_name": "U.S. Securities and Exchange Commission",
        "category": "regulation",
        "terms_url": "https://www.sec.gov/about/privacy-information",
        "license_scope": (
            "SEC website information may be copied or distributed with attribution; "
            "SEC seals, logos, trademarks and third-party material are excluded. "
            "Atlas stores feed metadata and links only."
        ),
    },
    "us-treasury-news": {
        "source_name": "U.S. Department of the Treasury",
        "category": "treasury-policy",
        "terms_url": "https://home.treasury.gov/footer/privacy-act/privacy-policy",
        "license_scope": (
            "U.S. government release metadata and links only; Treasury seals and any "
            "third-party material are excluded."
        ),
    },
    "bls": {
        "source_name": "U.S. Bureau of Labor Statistics",
        "category": "economy",
        "terms_url": "https://www.bls.gov/bls/linksite.htm",
        "license_scope": (
            "BLS publications are public domain except previously copyrighted photos and "
            "illustrations; source citation is requested and the BLS emblem is excluded."
        ),
    },
}


def _local_name(tag: str) -> str:
    """Return an XML local name for both RSS and namespaced Atom tags."""

    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _element_text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _first_child(element: ElementTree.Element, names: Iterable[str]) -> ElementTree.Element | None:
    wanted = set(names)
    return next((child for child in element if _local_name(child.tag) in wanted), None)


def _published_at(element: ElementTree.Element) -> datetime | None:
    value = _element_text(_first_child(element, ("pubDate", "published", "updated")))
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entry_link(element: ElementTree.Element, allowed_hosts: frozenset[str]) -> str:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        if child.attrib.get("rel", "alternate") not in {"", "alternate"}:
            continue
        value = (child.attrib.get("href") or child.text or "").strip()
        parsed = urlparse(value)
        if parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed_hosts:
            return value
    return ""


def _parse_metadata_feed(
    payload: str,
    *,
    provider: str,
    dataset: str,
    source_name: str,
    category: str,
    allowed_hosts: frozenset[str],
    feed_url: str,
    limit: int | None,
    metadata: Mapping[str, Any],
) -> ProviderResult:
    """Parse RSS 2.0 or Atom without touching description/content bodies."""

    try:
        root = ElementTree.fromstring(payload.lstrip("\ufeff"))
    except ElementTree.ParseError as exc:
        return ProviderResult.failure(provider, dataset, f"ParseError: {exc}")

    records: list[dict[str, Any]] = []
    skipped_items = 0
    entries = (item for item in root.iter() if _local_name(item.tag) in {"item", "entry"})
    normalized_limit = max(0, int(limit)) if limit is not None else None
    for entry in entries:
        if normalized_limit is not None and len(records) >= normalized_limit:
            break
        title = _element_text(_first_child(entry, ("title",)))
        source_url = _entry_link(entry, allowed_hosts)
        published_at = _published_at(entry)
        if not title or not source_url or published_at is None:
            skipped_items += 1
            continue
        records.append(
            {
                "title": title,
                "published_at": published_at.isoformat(),
                "source_name": source_name,
                "source_url": source_url,
                "category": category,
                "license_status": "link-only",
            }
        )

    return ProviderResult(
        provider=provider,
        dataset=dataset,
        records=records,
        metadata={
            **dict(metadata),
            "feed_url": feed_url,
            "metadata_only": True,
            "ignored_feed_elements": ["description", "summary", "content"],
            "skipped_items": skipped_items,
        },
    )


class MetadataFeedProvider(HTTPProvider):
    """Base adapter that enforces fixed source fields and HTTPS link hosts."""

    source_name = ""
    category = ""
    allowed_hosts: frozenset[str] = frozenset()
    terms_url = ""
    license_scope = ""

    def _metadata_feed(
        self,
        *,
        dataset: str,
        path: str,
        feed_url: str,
        category: str | None = None,
        limit: int | None = None,
    ) -> ProviderResult:
        payload, failure = self._get_text(dataset, path)
        if failure:
            return failure
        return _parse_metadata_feed(
            payload or "",
            provider=self.key,
            dataset=dataset,
            source_name=self.source_name,
            category=category or self.category,
            allowed_hosts=self.allowed_hosts,
            feed_url=feed_url,
            limit=limit,
            metadata={
                "attribution": self.source_name,
                "terms_url": self.terms_url,
                "license_scope": self.license_scope,
            },
        )


class SECPressReleaseProvider(MetadataFeedProvider):
    """Official SEC press-release RSS.

    SEC asks automated clients to identify themselves.  A missing
    ``SEC_USER_AGENT`` therefore produces an explicit skipped result without a
    network request.
    """

    key = "sec"
    base_url = "https://www.sec.gov"
    source_name = SOURCE_POLICIES[key]["source_name"]
    category = SOURCE_POLICIES[key]["category"]
    terms_url = SOURCE_POLICIES[key]["terms_url"]
    license_scope = SOURCE_POLICIES[key]["license_scope"]
    allowed_hosts = frozenset({"sec.gov", "www.sec.gov"})
    feed_url = "https://www.sec.gov/news/pressreleases.rss"

    def __init__(self, user_agent: str | None = None, **kwargs: Any) -> None:
        self.user_agent = user_agent or os.getenv("SEC_USER_AGENT", "")
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        headers.setdefault("Accept", "application/rss+xml, application/xml;q=0.9")
        super().__init__(headers=headers, **kwargs)

    def press_releases(self, *, limit: int | None = 100) -> ProviderResult:
        dataset = "rss:press-releases"
        if not self.user_agent:
            return ProviderResult.skip(
                self.key,
                dataset,
                "SEC_USER_AGENT is not configured (use 'product contact@example.com')",
            )
        return self._metadata_feed(
            dataset=dataset,
            path="/news/pressreleases.rss",
            feed_url=self.feed_url,
            limit=limit,
        )

    def fetch(self, dataset: str = "press-releases", **kwargs: Any) -> ProviderResult:
        if dataset not in {"press-releases", "rss:press-releases"}:
            return ProviderResult.failure(self.key, dataset, f"unsupported feed: {dataset}")
        return self.press_releases(**kwargs)


class TreasuryPressReleaseProvider(MetadataFeedProvider):
    """Treasury's official GovDelivery topic feed for all press releases."""

    key = "us-treasury-news"
    base_url = "https://public.govdelivery.com"
    source_name = SOURCE_POLICIES[key]["source_name"]
    category = SOURCE_POLICIES[key]["category"]
    terms_url = SOURCE_POLICIES[key]["terms_url"]
    license_scope = SOURCE_POLICIES[key]["license_scope"]
    allowed_hosts = frozenset({"content.govdelivery.com"})
    topic_code = "USTREAS_49"
    feed_url = f"https://public.govdelivery.com/topics/{topic_code}/feed.rss"

    def __init__(self, **kwargs: Any) -> None:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/rss+xml, application/xml;q=0.9")
        super().__init__(headers=headers, **kwargs)

    def press_releases(self, *, limit: int | None = 100) -> ProviderResult:
        return self._metadata_feed(
            dataset="rss:press-releases",
            path=f"/topics/{self.topic_code}/feed.rss",
            feed_url=self.feed_url,
            limit=limit,
        )

    def fetch(self, dataset: str = "press-releases", **kwargs: Any) -> ProviderResult:
        if dataset not in {"press-releases", "rss:press-releases"}:
            return ProviderResult.failure(self.key, dataset, f"unsupported feed: {dataset}")
        return self.press_releases(**kwargs)


class BLSReleaseProvider(MetadataFeedProvider):
    """Selected official BLS Atom feeds used by the macro dashboards."""

    key = "bls"
    base_url = "https://www.bls.gov"
    source_name = SOURCE_POLICIES[key]["source_name"]
    category = SOURCE_POLICIES[key]["category"]
    terms_url = SOURCE_POLICIES[key]["terms_url"]
    license_scope = SOURCE_POLICIES[key]["license_scope"]
    allowed_hosts = frozenset({"bls.gov", "www.bls.gov"})
    feeds = {
        "employment-situation": ("/feed/empsit.rss", "employment"),
        "job-openings": ("/feed/jolts.rss", "employment"),
        "consumer-prices": ("/feed/cpi.rss", "inflation"),
        "producer-prices": ("/feed/ppi.rss", "inflation"),
    }

    def __init__(self, user_agent: str | None = None, **kwargs: Any) -> None:
        # BLS currently rejects httpx's generic user agent.  Reuse the same
        # descriptive operator identity already required for SEC when a
        # BLS-specific value is not configured.
        self.user_agent = (
            user_agent
            or os.getenv("BLS_USER_AGENT", "")
            or os.getenv("SEC_USER_AGENT", "")
        )
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        headers.setdefault("Accept", "application/atom+xml, application/xml;q=0.9")
        super().__init__(headers=headers, **kwargs)

    def releases(self, feed_name: str, *, limit: int | None = 25) -> ProviderResult:
        config = self.feeds.get(feed_name)
        dataset = f"rss:{feed_name}"
        if config is None:
            return ProviderResult.failure(self.key, dataset, f"unsupported feed: {feed_name}")
        if not self.user_agent:
            return ProviderResult.skip(
                self.key,
                dataset,
                "BLS_USER_AGENT or SEC_USER_AGENT is not configured",
            )
        path, category = config
        return self._metadata_feed(
            dataset=dataset,
            path=path,
            feed_url=f"{self.base_url}{path}",
            category=category,
            limit=limit,
        )

    def fetch(self, dataset: str, **kwargs: Any) -> ProviderResult:
        return self.releases(dataset.removeprefix("rss:"), **kwargs)


def _stored_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, UTC)
    return parsed


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname)


def store_official_news(
    result: ProviderResult,
    _source: Source | None = None,
    _run: IngestionRun | None = None,
) -> int:
    """Idempotently store only normalized official-feed metadata.

    The optional ``Source`` and ``IngestionRun`` parameters make the helper
    directly compatible with ``record_provider_result(..., persist=...)``.
    They are intentionally not copied onto ``NewsItem`` because that model has
    no lineage foreign keys yet.
    """

    count = 0
    seen_urls: set[str] = set()
    with transaction.atomic():
        for record in result.records:
            title = " ".join(str(record.get("title") or "").split())[:320]
            source_url = str(record.get("source_url") or "").strip()[:800]
            published_at = _stored_datetime(record.get("published_at"))
            source_name = " ".join(str(record.get("source_name") or "").split())[:120]
            category = " ".join(str(record.get("category") or "").split())[:80]
            if (
                not title
                or not source_name
                or not category
                or not _is_https_url(source_url)
                or published_at is None
                or source_url in seen_urls
            ):
                continue
            seen_urls.add(source_url)
            create_defaults = {
                "title": title,
                "original_title": "",
                "summary": "",
                "source_name": source_name,
                "category": category,
                "published_at": published_at,
                "tickers": [],
                "themes": [],
                "sentiment": "",
                "relevance": 0,
                "license_status": "link-only",
            }
            existing = NewsItem.objects.filter(source_url=source_url).order_by("pk").first()
            if existing is None:
                NewsItem.objects.create(source_url=source_url, **create_defaults)
            else:
                upstream_fields = {
                    "title": title,
                    "source_name": source_name,
                    "category": category,
                    "published_at": published_at,
                    "license_status": "link-only",
                }
                for field, value in upstream_fields.items():
                    setattr(existing, field, value)
                existing.save(update_fields=[*upstream_fields, "updated_at"])
            count += 1
    return count


__all__ = [
    "BLSReleaseProvider",
    "SECPressReleaseProvider",
    "SOURCE_POLICIES",
    "TreasuryPressReleaseProvider",
    "store_official_news",
]
