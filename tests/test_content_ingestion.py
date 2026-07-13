from __future__ import annotations

from datetime import UTC, datetime

import pytest

from research.models import FedDocument, NewsItem
from research.official_news import store_official_news
from research.providers import ProviderResult
from research.services import store_fed_documents


@pytest.mark.django_db
def test_official_news_refresh_preserves_existing_enrichment():
    source_url = "https://www.bls.gov/news.release/cpi.example.htm"
    item = NewsItem.objects.create(
        title="Original headline",
        original_title="Original language headline",
        summary="Reviewed CPI summary",
        source_name="BLS",
        source_url=source_url,
        category="economy",
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
        tickers=["SPY", "TLT"],
        themes=["inflation"],
        sentiment="hawkish",
        relevance=9,
        license_status="review",
    )
    result = ProviderResult(
        provider="bls",
        dataset="rss:consumer-prices",
        records=[
            {
                "title": "Corrected official headline",
                "published_at": "2026-07-14T12:30:00+00:00",
                "source_name": "U.S. Bureau of Labor Statistics",
                "source_url": source_url,
                "category": "inflation",
                "summary": "UNTRUSTED FEED BODY",
            }
        ],
    )

    assert store_official_news(result) == 1

    item.refresh_from_db()
    assert item.title == "Corrected official headline"
    assert item.source_name == "U.S. Bureau of Labor Statistics"
    assert item.category == "inflation"
    assert item.published_at == datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    assert item.license_status == "link-only"
    assert item.original_title == "Original language headline"
    assert item.summary == "Reviewed CPI summary"
    assert item.tickers == ["SPY", "TLT"]
    assert item.themes == ["inflation"]
    assert item.sentiment == "hawkish"
    assert item.relevance == 9


@pytest.mark.django_db
def test_fed_document_refresh_preserves_existing_analysis_enrichment():
    document = FedDocument.objects.create(
        document_type=FedDocument.DocumentType.SPEECH,
        slug="fixture-fed-document",
        title="Original title",
        speaker="Governor Example",
        summary="Reviewed policy summary",
        key_points=["Balance sheet", "Inflation persistence"],
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
        hawkish_score=3,
        original_url="https://www.federalreserve.gov/newsevents/speech/original.htm",
        analysis_status=FedDocument.AnalysisStatus.REVIEWED,
        analysis_model="fixture-model",
        analysis_prompt_version="fed-v1",
        analysis_generated_at=datetime(2026, 7, 1, tzinfo=UTC),
        analysis_evidence=[{"id": "official-original"}],
        reviewed_by="Fixture Reviewer",
        reviewed_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    result = ProviderResult(
        provider="federal-reserve",
        dataset="feed:speeches",
        records=[
            {
                "slug": document.slug,
                "document_type": FedDocument.DocumentType.STATEMENT,
                "title": "Corrected official title",
                "official_description": "Corrected official RSS description",
                "published_at": "2026-07-10T16:00:00+00:00",
                "original_url": (
                    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260710a.htm"
                ),
            }
        ],
    )

    assert store_fed_documents(result, None, None) == 1

    document.refresh_from_db()
    assert document.document_type == FedDocument.DocumentType.STATEMENT
    assert document.title == "Corrected official title"
    assert document.published_at == datetime(2026, 7, 10, 16, tzinfo=UTC)
    assert document.original_url.endswith("monetary20260710a.htm")
    assert document.official_description == "Corrected official RSS description"
    assert document.speaker == "Governor Example"
    assert document.summary == "Reviewed policy summary"
    assert document.key_points == ["Balance sheet", "Inflation persistence"]
    assert document.hawkish_score == 3
    assert document.analysis_status == FedDocument.AnalysisStatus.REVIEWED
    assert document.analysis_model == "fixture-model"
    assert document.analysis_prompt_version == "fed-v1"
    assert document.analysis_evidence == [{"id": "official-original"}]
    assert document.reviewed_by == "Fixture Reviewer"
