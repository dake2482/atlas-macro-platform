from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.utils import timezone

from research.models import (
    Company,
    FundLetter,
    NewsItem,
    ResearchMention,
    Source,
    SourceLicense,
    SupplyChainNode,
)


def page_text(response) -> str:
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.django_db
def test_global_search_finds_content_without_leaking_unmatched_rows(client, seeded_platform):
    NewsItem.objects.create(
        title="ZXQ-731 Global Search Needle",
        summary="Unique global search fixture",
        source_name="Fixture Wire",
        source_url="https://www.sec.gov/fixtures/global-search",
        category="fixture-search",
        published_at=timezone.now(),
    )
    NewsItem.objects.create(
        title="ZXQ-000 Search Decoy",
        source_name="Fixture Wire",
        source_url="https://www.sec.gov/fixtures/global-decoy",
        category="fixture-search",
        published_at=timezone.now(),
    )

    body = page_text(client.get("/search/", {"q": "ZXQ-731"}))

    assert "ZXQ-731 Global Search Needle" in body
    assert "ZXQ-000 Search Decoy" not in body


@pytest.mark.django_db
def test_news_filters_are_shareable_and_composable(client):
    NewsItem.objects.create(
        title="NEWS-ALPHA Composed Match",
        summary="Semiconductor credit crossover",
        source_name="Alpha Fixture Wire",
        source_url="https://www.sec.gov/fixtures/news-alpha",
        category="fixture-credit",
        published_at=timezone.now(),
    )
    NewsItem.objects.create(
        title="NEWS-BETA Wrong Source",
        source_name="Beta Fixture Wire",
        source_url="https://www.sec.gov/fixtures/news-beta",
        category="fixture-credit",
        published_at=timezone.now(),
    )
    NewsItem.objects.create(
        title="NEWS-GAMMA Wrong Category",
        source_name="Alpha Fixture Wire",
        source_url="https://www.sec.gov/fixtures/news-gamma",
        category="fixture-rates",
        published_at=timezone.now(),
    )

    body = page_text(
        client.get(
            "/news/",
            {"q": "NEWS", "source": "Alpha Fixture Wire", "category": "fixture-credit"},
        )
    )

    assert "NEWS-ALPHA Composed Match" in body
    assert "NEWS-BETA Wrong Source" not in body
    assert "NEWS-GAMMA Wrong Category" not in body


@pytest.mark.django_db
def test_research_filters_are_shareable_and_composable(client):
    now = timezone.now()
    ResearchMention.objects.create(
        bank="Fixture Alpha Bank",
        title="REPORT-ALPHA Composed Match",
        summary="Unique research fixture",
        category="fixture-ai",
        stance="bullish",
        published_at=now,
        source_url="https://www.federalreserve.gov/fixtures/report-alpha",
        review_status="reviewed",
    )
    ResearchMention.objects.create(
        bank="Fixture Beta Bank",
        title="REPORT-BETA Wrong Bank",
        category="fixture-ai",
        stance="bullish",
        published_at=now,
        source_url="https://www.federalreserve.gov/fixtures/report-beta",
        review_status="reviewed",
    )

    body = page_text(
        client.get(
            "/research/reports/",
            {
                "q": "REPORT",
                "bank": "Fixture Alpha Bank",
                "category": "fixture-ai",
                "stance": "bullish",
            },
        )
    )

    assert "REPORT-ALPHA Composed Match" in body
    assert "REPORT-BETA Wrong Bank" not in body


@pytest.mark.django_db
def test_fund_letter_filters_are_shareable_and_composable(client):
    FundLetter.objects.create(
        fund_name="LETTER-ALPHA Composed Match",
        quarter="2099 Q4",
        strategy="fixture-value",
        stance="constructive",
        summary="Unique fund letter fixture",
        original_url="https://www.berkshirehathaway.com/letters/letter-alpha",
        published_at=date(2099, 12, 31),
    )
    FundLetter.objects.create(
        fund_name="LETTER-BETA Wrong Quarter",
        quarter="2099 Q3",
        strategy="fixture-value",
        stance="constructive",
        summary="Fund letter decoy",
        original_url="https://www.berkshirehathaway.com/letters/letter-beta",
        published_at=date(2099, 9, 30),
    )

    body = page_text(
        client.get(
            "/research/fund-letters/",
            {
                "q": "LETTER",
                "quarter": "2099 Q4",
                "strategy": "fixture-value",
                "stance": "constructive",
            },
        )
    )

    assert "LETTER-ALPHA Composed Match" in body
    assert "LETTER-BETA Wrong Quarter" not in body


@pytest.mark.django_db
def test_market_map_filters_nodes_and_companies(client):
    source = Source.objects.create(key="fixture-map-sec", name="SEC fixture", license_status="open")
    SourceLicense.objects.create(source=source, status="open", scope="Fixture", public_display_allowed=True)
    node_match = SupplyChainNode.objects.create(
        slug="fixture-filter-node",
        name="MAPNODE-ALPHA Composed Match",
        layer="fixture-layer",
        quadrant="fixture-quadrant",
        description="Unique market map fixture",
        source_note="Reviewed company disclosures",
    )
    node_decoy = SupplyChainNode.objects.create(
        slug="fixture-decoy-node",
        name="MAPNODE-BETA Wrong Layer",
        layer="fixture-other-layer",
        quadrant="fixture-quadrant",
        description="Market map decoy",
        source_note="Reviewed company disclosures",
    )
    Company.objects.create(
        slug="fixture-filter-company",
        name="MAPCO-ALPHA Searchable Company",
        ticker="MAPA",
        primary_node=node_match,
        description="Unique company fixture",
        data_source_note="SEC EDGAR",
        source=source,
        sec_cik="0000000004",
        publication_batch_id=uuid.uuid4(),
        fetched_at=timezone.now(),
        license_scope="Fixture",
        is_published=True,
        quality_status="fresh",
    )
    Company.objects.create(
        slug="fixture-decoy-company",
        name="MAPCO-BETA Wrong Layer",
        ticker="MAPB",
        primary_node=node_decoy,
        description="Company decoy",
        data_source_note="SEC EDGAR",
        source=source,
        sec_cik="0000000005",
        publication_batch_id=uuid.uuid4(),
        fetched_at=timezone.now(),
        license_scope="Fixture",
        is_published=True,
        quality_status="fresh",
    )

    body = page_text(
        client.get(
            "/ai-industry/market-map/",
            {
                "q": "MAP",
                "layer": "fixture-layer",
                "quadrant": "fixture-quadrant",
                "sort": "name",
            },
        )
    )

    assert "MAPNODE-ALPHA Composed Match" in body or "MAPCO-ALPHA Searchable Company" in body
    assert "MAPNODE-BETA Wrong Layer" not in body
    assert "MAPCO-BETA Wrong Layer" not in body
