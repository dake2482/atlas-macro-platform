"""Metadata-only adapter for Berkshire Hathaway's official letter index.

Only the year and first-party outbound URL are persisted.  The linked HTML/PDF
body is never downloaded by this adapter, summarized, mirrored, or stored.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .models import FundLetter, IngestionRun, RawArtifact, Source
from .providers import HTTPProvider, ProviderResult

BERKSHIRE_LETTER_INDEX = "https://www.berkshirehathaway.com/letters/letters.html"
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_LETTER_PATH = re.compile(r"^/letters/(?:19|20)\d{2}(?:ltr)?\.(?:html|pdf)$", re.I)


class _LetterIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


class BerkshireLettersProvider(HTTPProvider):
    key = "berkshire-hathaway"
    base_url = "https://www.berkshirehathaway.com"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_index_bytes: int = 1024 * 1024,
    ) -> None:
        self.max_index_bytes = max_index_bytes
        super().__init__(
            client=client,
            timeout=timeout,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "AtlasMacro/0.1 shareholder-letter metadata index",
            },
        )

    def letter_index(self) -> ProviderResult:
        dataset = "shareholder-letter-index"
        try:
            response = self.client.get("/letters/letters.html")
            response.raise_for_status()
            content = response.content
            if len(content) > self.max_index_bytes:
                return ProviderResult.failure(self.key, dataset, "letter index exceeded size limit")
            content_type = response.headers.get("content-type", "")
            if content_type and "html" not in content_type.lower():
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    f"unexpected content type: {content_type}",
                )
            text = response.text
        except (httpx.HTTPError, UnicodeError) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        parser = _LetterIndexParser()
        parser.feed(text)
        by_year: dict[int, str] = {}
        rejected_links = 0
        for href, label in parser.links:
            if not _YEAR.fullmatch(label):
                continue
            absolute = urljoin(BERKSHIRE_LETTER_INDEX, href)
            parsed = urlparse(absolute)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"berkshirehathaway.com", "www.berkshirehathaway.com"}
                or not _LETTER_PATH.fullmatch(parsed.path)
            ):
                rejected_links += 1
                continue
            by_year[int(label)] = absolute

        years = sorted(by_year)
        missing_years = (
            sorted(set(range(years[0], years[-1] + 1)) - set(years)) if years else []
        )
        quality_status = "complete" if len(years) >= 40 and not missing_years else "partial"
        records = [
            {
                "year": year,
                "fund_name": "Berkshire Hathaway",
                "fund_name_en": "Berkshire Hathaway Inc.",
                "manager": "",
                "quarter": f"FY {year}",
                "strategy": "股东信 / 多元化控股",
                "stance": "",
                "summary": (
                    "本条目仅保存 Berkshire Hathaway 官方股东信的报告年度与原文链接；"
                    "Atlas Macro 未复制、托管或生成该信正文摘要。"
                ),
                "key_points": [],
                "asset_views": [],
                "original_url": by_year[year],
                "source_label": "Berkshire Hathaway 官方索引",
                "license_status": "link-only",
                "published_at": None,
            }
            for year in years
        ]
        digest = hashlib.sha256(content).hexdigest()
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "source_url": BERKSHIRE_LETTER_INDEX,
                "archive_sha256": digest,
                "archive_size": len(content),
                "content_type": response.headers.get("content-type", "text/html"),
                "last_modified": response.headers.get("last-modified", ""),
                "first_year": years[0] if years else None,
                "last_year": years[-1] if years else None,
                "year_count": len(years),
                "missing_years": missing_years,
                "rejected_links": rejected_links,
                "quality_status": quality_status,
                "storage_policy": "metadata-and-first-party-link-only; document bodies not fetched",
            },
        )


def store_berkshire_letters(
    result: ProviderResult,
    source: Source,
    run: IngestionRun,
) -> int:
    count = 0
    for record in result.records:
        FundLetter.objects.update_or_create(
            original_url=record["original_url"],
            defaults={
                key: record[key]
                for key in (
                    "fund_name",
                    "fund_name_en",
                    "manager",
                    "quarter",
                    "strategy",
                    "stance",
                    "summary",
                    "key_points",
                    "asset_views",
                    "source_label",
                    "license_status",
                    "published_at",
                )
            },
        )
        count += 1

    archive_hash = str(result.metadata.get("archive_sha256") or "")
    source_url = str(result.metadata.get("source_url") or "")
    if archive_hash and source_url:
        RawArtifact.objects.create(
            run=run,
            uri=f"{source_url}#sha256={archive_hash}",
            sha256=archive_hash,
            content_type="text/html",
            size_bytes=int(result.metadata.get("archive_size") or 0),
        )
    return count


def refresh_berkshire_letters() -> dict[str, Any]:
    from .services import record_provider_result

    provider = BerkshireLettersProvider()
    try:
        result = provider.letter_index()
        run = record_provider_result(result, persist=store_berkshire_letters)
    finally:
        provider.close()
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
                "metadata": run.metadata,
            }
        ],
        "row_count": run.row_count,
        "failed": int(run.status == IngestionRun.Status.FAILED),
        "partial": int(run.status == IngestionRun.Status.PARTIAL),
    }
