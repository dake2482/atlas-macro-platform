from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from research.models import NewsItem
from research.official_news import (
    BLSReleaseProvider,
    SECPressReleaseProvider,
    TreasuryPressReleaseProvider,
    store_official_news,
)
from research.providers import ProviderResult

FIXTURES = Path(__file__).parent / "fixtures" / "official_news"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _client(handler):
    return httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))


def test_sec_press_release_provider_reads_metadata_and_ignores_description():
    def handler(request):
        assert request.url.path == "/news/pressreleases.rss"
        return httpx.Response(200, text=_fixture("sec_press_releases.xml"))

    provider = SECPressReleaseProvider(
        user_agent="Atlas Macro tests engineering@example.com",
        client=_client(handler),
    )
    result = provider.press_releases()

    assert result.ok
    assert result.records == [
        {
            "title": "SEC Seeks Public Comment on Market Structure",
            "published_at": "2026-07-10T16:00:32+00:00",
            "source_name": "U.S. Securities and Exchange Commission",
            "source_url": (
                "https://www.sec.gov/newsroom/press-releases/2026-1-market-structure"
            ),
            "category": "regulation",
            "license_status": "link-only",
        }
    ]
    assert result.metadata["metadata_only"] is True
    assert "description" in result.metadata["ignored_feed_elements"]
    assert "BODY" not in str(result.records)


def test_sec_provider_skips_network_without_declared_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    def handler(_request):
        raise AssertionError("missing identity must skip before any network request")

    result = SECPressReleaseProvider(client=_client(handler)).press_releases()

    assert result.skipped
    assert "SEC_USER_AGENT" in result.metadata["reason"]


def test_treasury_provider_uses_topic_specific_official_govdelivery_feed():
    def handler(request):
        assert request.url.path == "/topics/USTREAS_49/feed.rss"
        return httpx.Response(200, text=_fixture("treasury_press_releases.xml"))

    result = TreasuryPressReleaseProvider(client=_client(handler)).press_releases()

    assert result.ok
    assert result.records[0]["source_name"] == "U.S. Department of the Treasury"
    assert result.records[0]["category"] == "treasury-policy"
    assert result.records[0]["source_url"].startswith("https://content.govdelivery.com/")
    assert result.metadata["feed_url"].endswith("/topics/USTREAS_49/feed.rss")
    assert "TREASURY RELEASE BODY" not in str(result.records)


def test_bls_provider_parses_namespaced_atom_and_assigns_owned_category():
    def handler(request):
        assert request.url.path == "/feed/cpi.rss"
        return httpx.Response(200, text=_fixture("bls_releases.xml"))

    result = BLSReleaseProvider(
        user_agent="Atlas Macro tests engineering@example.com",
        client=_client(handler),
    ).releases("consumer-prices")

    assert result.ok
    assert result.records[0]["published_at"] == "2026-07-14T12:30:00+00:00"
    assert result.records[0]["category"] == "inflation"
    assert result.records[0]["source_name"] == "U.S. Bureau of Labor Statistics"
    assert "BLS RELEASE BODY" not in str(result.records)


def test_feed_parser_rejects_non_official_entry_links():
    payload = """<?xml version="1.0"?><rss version="2.0"><channel><item>
    <title>Injected item</title><link>https://attacker.example/release</link>
    <pubDate>Fri, 10 Jul 2026 12:00:32 -0400</pubDate>
    </item></channel></rss>"""

    result = TreasuryPressReleaseProvider(
        client=_client(lambda _request: httpx.Response(200, text=payload))
    ).press_releases()

    assert result.ok
    assert result.records == []
    assert result.metadata["skipped_items"] == 1


@pytest.mark.django_db
def test_store_official_news_is_idempotent_and_never_persists_feed_body():
    url = "https://www.bls.gov/news.release/archives/cpi_fixture.htm"
    first = ProviderResult(
        provider="bls",
        dataset="rss:consumer-prices",
        records=[
            {
                "title": "Original CPI headline",
                "published_at": "2026-07-14T12:30:00+00:00",
                "source_name": "U.S. Bureau of Labor Statistics",
                "source_url": url,
                "category": "inflation",
                "summary": "UNTRUSTED BODY",
            }
        ],
    )
    second = ProviderResult(
        provider="bls",
        dataset="rss:consumer-prices",
        records=[
            {
                **first.records[0],
                "title": "Corrected CPI headline",
                "published_at": datetime(2026, 7, 14, 13, 0, tzinfo=UTC),
            },
            first.records[0],
        ],
    )

    assert store_official_news(first) == 1
    assert store_official_news(second) == 1

    assert NewsItem.objects.filter(source_url=url).count() == 1
    item = NewsItem.objects.get(source_url=url)
    assert item.title == "Corrected CPI headline"
    assert item.published_at == datetime(2026, 7, 14, 13, 0, tzinfo=UTC)
    assert item.summary == ""
    assert item.original_title == ""
    assert item.tickers == []
    assert item.themes == []
    assert item.license_status == "link-only"
