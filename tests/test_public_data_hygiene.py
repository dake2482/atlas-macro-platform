from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from research.models import (
    Company,
    DashboardSnapshot,
    FedDocument,
    FundLetter,
    GitHubProject,
    GitHubProjectSnapshot,
    GlossaryTerm,
    NewsItem,
    ResearchMention,
    Source,
    SourceLicense,
    SupplyChainNode,
    Thesis,
)
from research.page_registry import get_page_config
from tests.thesis_factories import build_complete_thesis


def _licensed_source(key: str) -> Source:
    source = Source.objects.create(
        key=key,
        name=f"{key} source",
        license_status=Source.LicenseStatus.OPEN,
        redistribution_allowed=True,
    )
    SourceLicense.objects.create(
        source=source,
        is_current=True,
        status=Source.LicenseStatus.OPEN,
        scope="Public test display",
        public_display_allowed=True,
        redistribution_allowed=True,
    )
    return source


def test_registry_prototype_values_are_never_public_configuration():
    config = get_page_config("rates")

    assert config["metrics"]
    assert all(metric["value"] is None for metric in config["metrics"])
    assert all(metric["display_value"] == "—" for metric in config["metrics"])
    assert "原型数值" in config["analysis"]


@pytest.mark.django_db
def test_demo_seed_requires_an_explicit_destructive_acknowledgement():
    with pytest.raises(CommandError, match="--allow-demo-data"):
        call_command("seed_platform", verbosity=0)


@pytest.mark.django_db
def test_example_com_content_is_not_public(client):
    marker = "PLACEHOLDER-CONTENT-MUST-NOT-RENDER"
    now = timezone.now()
    NewsItem.objects.create(
        title=marker,
        source_name="Placeholder",
        source_url="https://example.com/news",
        category="test",
        published_at=now,
    )
    ResearchMention.objects.create(
        bank="Placeholder",
        title=marker,
        category="test",
        published_at=now,
        source_url="https://example.com/research",
        review_status="reviewed",
    )
    letter = FundLetter.objects.create(
        fund_name=marker,
        quarter="2031Q1",
        strategy="test",
        stance="neutral",
        summary=marker,
        original_url="https://example.com/letter",
        published_at=date(2031, 1, 1),
    )
    document = FedDocument.objects.create(
        document_type=FedDocument.DocumentType.NEWS,
        slug="placeholder-fed-document",
        title=marker,
        summary="",
        published_at=now,
        original_url="https://example.com/fed",
    )
    GlossaryTerm.objects.create(
        slug="placeholder-glossary-term",
        term=marker,
        category="test",
        definition=marker,
        source_url="https://example.com/methodology",
    )

    for path in ("/", "/news/", "/research/reports/", "/research/fund-letters/", "/fed/", "/glossary/"):
        response = client.get(path)
        assert response.status_code == 200
        assert marker not in response.content.decode(), path
    assert client.get(letter.get_absolute_url()).status_code == 404
    assert client.get(f"/fed/news/{document.slug}/").status_code == 404


@pytest.mark.django_db
def test_demo_theses_do_not_leak_through_adjacent_links_or_sitemap(client):
    center_date = date(1900, 7, 2)
    public = build_complete_thesis(
        "PUBLIC-THESIS",
        report_date=center_date,
    )
    Thesis.objects.create(
        date=center_date - timedelta(days=1),
        regime="LEAKED-DEMO-PREVIOUS",
        summary="演示日报 2040-01-01：不应公开",
        evidence=[],
        triggers=[],
        invalidation="demo",
    )
    Thesis.objects.create(
        date=center_date + timedelta(days=1),
        regime="LEAKED-DEMO-FOLLOWING",
        summary="演示日报 2040-01-03：不应公开",
        evidence=[],
        triggers=[],
        invalidation="demo",
    )

    detail = client.get(public.get_absolute_url()).content.decode()
    sitemap = client.get("/sitemap.xml").content.decode()

    assert "LEAKED-DEMO" not in detail
    assert (center_date - timedelta(days=1)).isoformat() not in sitemap
    assert (center_date + timedelta(days=1)).isoformat() not in sitemap
    assert center_date.isoformat() in sitemap


@pytest.mark.django_db
def test_blank_snapshot_never_falls_back_to_static_market_conclusion(client):
    source = _licensed_source("blank-analysis-snapshot")
    DashboardSnapshot.objects.create(
        key="rates",
        title="Rates fixture",
        as_of=timezone.now(),
        summary="",
        source=source,
        is_published=True,
        data={
            "demo": False,
            "contract_version": 1,
            "metrics": [],
            "chart_data": [],
            "sections": [],
        },
    )

    body = client.get("/rates/").content.decode()

    assert "曲线仍倒挂" not in body
    assert "尚未生成经审核的信号解读" in body


@pytest.mark.django_db
def test_seed_policy_is_not_presented_as_a_current_licence_review(client):
    source = Source.objects.create(
        key="seed-policy-only-source",
        name="SEED-POLICY-ONLY-SOURCE",
        homepage="https://www.bls.gov/",
        license_status=Source.LicenseStatus.OPEN,
    )
    SourceLicense.objects.create(
        source=source,
        is_current=True,
        status=Source.LicenseStatus.OPEN,
        scope="Prototype seed decision",
        reviewed_by="clean-room seed policy",
        terms_url="https://www.bls.gov/bls/linksite.htm",
    )

    body = client.get("/data-sources/").content.decode()

    row = body[body.index("SEED-POLICY-ONLY-SOURCE") :]
    assert "未审核" in row.split("</tr>", 1)[0]


@pytest.mark.django_db
def test_market_map_count_and_relation_list_exclude_demo_companies(client):
    node = SupplyChainNode.objects.create(
        slug="public-node-with-demo-relation",
        name="PUBLIC-NODE",
        layer="test",
        description="Reviewed node",
        source_note="Company IR",
    )
    Company.objects.create(
        slug="clean-room-company-attached-to-public-node",
        name="LEAKED-DEMO-COMPANY",
        ticker="DEMO",
        primary_node=node,
        description="Synthetic company",
        data_source_note="合成演示数据",
    )

    body = client.get("/ai-industry/market-map/").content.decode()

    assert "PUBLIC-NODE" in body
    assert "LEAKED-DEMO-COMPANY" not in body
    assert "0 companies" in body


@pytest.mark.django_db
def test_unanalysed_fed_document_does_not_claim_neutral_score(client):
    document = FedDocument.objects.create(
        document_type=FedDocument.DocumentType.NEWS,
        slug="official-unanalysed-document",
        title="Official unanalysed document",
        official_description="Official RSS description only",
        summary="",
        key_points=[],
        published_at=timezone.now(),
        original_url="https://www.federalreserve.gov/newsevents/pressreleases/test.htm",
    )

    listing = client.get("/fed/news/").content.decode()
    detail = client.get(f"/fed/news/{document.slug}/").content.decode()

    assert "待评分" in listing
    assert "官方 RSS 描述 / 摘要" in detail
    assert "Official RSS description only" in detail
    assert "AI generated" not in detail


@pytest.mark.django_db
def test_first_github_snapshot_is_not_presented_as_seven_day_change(client):
    source = _licensed_source("github-hygiene-fixture")
    project = GitHubProject.objects.create(
        repo="verified/one-snapshot",
        category="Agent",
        stars=123,
        stars_7d=0,
        momentum_score=0,
        homepage="https://github.com/verified/one-snapshot",
        source=source,
        data_as_of=timezone.now(),
    )
    GitHubProjectSnapshot.objects.create(
        project=project,
        snapshot_date=timezone.localdate(),
        stars=123,
        fetched_at=datetime.now(UTC),
        batch_id=uuid.uuid4(),
        source=source,
    )

    body = client.get("/ai-industry/chain/applications/").content.decode()

    assert "verified/one-snapshot" in body
    assert "等待至少两个每日快照" in body
    assert "<td class=\"numeric \">—</td>" in body
