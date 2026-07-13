from __future__ import annotations

import httpx
import pytest

from research.berkshire_letters import (
    BerkshireLettersProvider,
    store_berkshire_letters,
)
from research.models import FundLetter, RawArtifact
from research.services import record_provider_result


def _client(html: str, *, content_type: str = "text/html") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/letters/letters.html"
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": content_type, "last-modified": "Sat, 01 Mar 2025 00:00:00 GMT"},
        )

    return httpx.Client(base_url="https://www.berkshirehathaway.com", transport=httpx.MockTransport(handler))


def _index(first: int = 1977, last: int = 2024) -> str:
    links = []
    for year in range(first, last + 1):
        suffix = ".html" if year <= 2003 else "ltr.pdf"
        links.append(f'<a href="{year}{suffix}">{year}</a>')
    links.append('<a href="https://attacker.invalid/2024ltr.pdf">2024</a>')
    return "<html><body>" + "".join(links) + "</body></html>"


def test_berkshire_provider_keeps_only_contiguous_first_party_year_links():
    result = BerkshireLettersProvider(client=_client(_index())).letter_index()

    assert result.ok
    assert result.row_count == 48
    assert result.metadata["first_year"] == 1977
    assert result.metadata["last_year"] == 2024
    assert result.metadata["missing_years"] == []
    assert result.metadata["quality_status"] == "complete"
    assert result.metadata["rejected_links"] == 1
    assert all(item["original_url"].startswith("https://www.berkshirehathaway.com/letters/") for item in result.records)
    assert all(item["published_at"] is None for item in result.records)
    assert all(item["license_status"] == "link-only" for item in result.records)


def test_berkshire_provider_marks_gapped_index_partial():
    html = _index().replace('<a href="2000.html">2000</a>', "")
    result = BerkshireLettersProvider(client=_client(html)).letter_index()

    assert result.ok
    assert result.metadata["missing_years"] == [2000]
    assert result.metadata["quality_status"] == "partial"


@pytest.mark.django_db
def test_berkshire_metadata_ingestion_is_idempotent_link_only_and_public(client):
    provider = BerkshireLettersProvider(client=_client(_index()))
    first = record_provider_result(provider.letter_index(), persist=store_berkshire_letters)
    second = record_provider_result(provider.letter_index(), persist=store_berkshire_letters)

    assert first.status == "success"
    assert second.status == "success"
    assert FundLetter.objects.count() == 48
    assert RawArtifact.objects.filter(run__in=[first, second]).count() == 2
    latest = FundLetter.objects.get(quarter="FY 2024")
    assert latest.published_at is None
    assert latest.key_points == []
    assert "未复制、托管或生成" in latest.summary
    listing = client.get("/research/fund-letters/")
    detail = client.get(latest.get_absolute_url())
    sitemap = client.get("/sitemap.xml")
    assert listing.status_code == detail.status_code == sitemap.status_code == 200
    assert "FY 2024" in listing.content.decode()
    assert latest.get_absolute_url() in sitemap.content.decode()
