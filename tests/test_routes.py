from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.utils import timezone

from research.context_processors import NAV_GROUPS
from research.models import (
    CodingAgentProfile,
    Company,
    DashboardSnapshot,
    DataRequirement,
    FedDocument,
    FundLetter,
    ModelProfile,
    Source,
    SourceLicense,
    SupplyChainNode,
)
from tests.thesis_factories import build_complete_thesis

STATIC_PUBLIC_PATHS = [
    "/",
    "/trade-map/",
    "/regime-log/",
    "/daily-report/",
    "/search/",
    "/news/",
    "/semiconductor-news/",
    "/research/reports/",
    "/research/reports/all/",
    "/research/fund-letters/",
    "/glossary/",
    "/data-sources/",
    "/supply-chain/",
    "/supply-chain/foundry/",
    "/supply-chain/packaging/",
    "/supply-chain/hbm/",
    "/supply-chain/gpu/",
    "/supply-chain/demand/",
    "/assets/",
    "/assets/equities/",
    "/assets/etfs/",
    "/assets/equities/options/",
    "/assets/equities/positioning/",
    "/assets/bonds/",
    "/assets/commodities/",
    "/assets/fx/",
    "/assets/crypto/",
    "/assets/crypto/derivatives/",
    "/rates/",
    "/rates/fed-funds/",
    "/rates/yield-curve/",
    "/rates/auctions/",
    "/rates/real-rates/",
    "/rates/expectations/",
    "/fed/",
    "/fed/statements/",
    "/fed/speeches/",
    "/fed/news/",
    "/fed/hawkish-dovish/",
    "/liquidity/",
    "/liquidity/transmission-chain/",
    "/liquidity/fed-balance-sheet/",
    "/liquidity/operations/",
    "/liquidity/rrp-tga/",
    "/liquidity/reserves/",
    "/liquidity/global-dollar/",
    "/liquidity/subsurface/",
    "/economy/",
    "/economy/gdp/",
    "/economy/employment/",
    "/economy/inflation/",
    "/economy/consumer/",
    "/volatility/",
    "/volatility/dashboard/",
    "/volatility/vix/",
    "/volatility/move/",
    "/volatility/fx-vol/",
    "/volatility/implied-vs-realized/",
    "/credit/",
    "/credit/spreads/",
    "/credit/cds/",
    "/credit/stress/",
    "/ai-industry/",
    "/ai-industry/market-map/",
    "/ai-industry/graph/",
    "/ai-industry/news/",
    "/ai-industry/chain/",
    "/ai-industry/chain/semiconductor-manufacturing/",
    "/ai-industry/chain/model-evolution/",
    "/ai-industry/vibe-coding/",
    "/ai-industry/chain/applications/",
    "/ai-industry/chain/glossary/",
    "/ai-industry/chain/teardown/",
]


def test_route_contract_covers_every_navigation_item():
    nav_paths = {path for group in NAV_GROUPS for _label, path in group["items"]}
    assert nav_paths <= set(STATIC_PUBLIC_PATHS)


def test_every_top_level_volatility_route_is_navigable():
    nav_paths = {path for group in NAV_GROUPS for _label, path in group["items"]}
    assert {
        "/volatility/",
        "/volatility/dashboard/",
        "/volatility/vix/",
        "/volatility/move/",
        "/volatility/fx-vol/",
        "/volatility/implied-vs-realized/",
    } <= nav_paths


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    ("/volatility/dashboard/", "/volatility/vix/"),
)
def test_volatility_navigation_marks_each_route_current(client, path):
    body = client.get(path).content.decode()

    assert f'href="{path}" aria-current="page"' in body


@pytest.mark.django_db
@pytest.mark.parametrize("path", STATIC_PUBLIC_PATHS)
def test_public_routes_render(client, seeded_platform, path):
    response = client.get(path)
    assert response.status_code == 200, path


@pytest.mark.django_db
def test_assets_overview_keeps_legacy_chart_contract(client, seeded_platform):
    body = client.get("/assets/").content.decode()

    assert 'id="dashboard-primary-chart"' in body
    assert 'data-chart-source="dashboard-primary-chart"' in body
    assert 'aria-labelledby="dashboard-primary-chart-title"' in body


@pytest.mark.django_db
def test_no_public_route_leaks_seeded_demo_content(client, seeded_platform):
    forbidden = (
        "演示日报",
        "Atlas Demo Wire",
        "Clean-room Demonstration",
        "产业链演示公司",
        "Frontier Demo Model",
        "demo-project",
    )
    for path in STATIC_PUBLIC_PATHS:
        body = client.get(path).content.decode()
        assert not any(marker in body for marker in forbidden), path


@pytest.mark.django_db
def test_dynamic_detail_routes_render(client, seeded_platform):
    thesis = build_complete_thesis(
        "verified fixture",
        report_date=date(1900, 6, 1),
    )
    letter = FundLetter.objects.create(
        fund_name="Verified Fixture Fund",
        quarter="2030Q1",
        strategy="macro",
        stance="neutral",
        summary="Reviewed fixture summary",
        key_points=[],
        original_url="https://example.org/verified-letter",
        published_at="2030-01-01",
    )
    node = SupplyChainNode.objects.create(
        slug="verified-fixture-node",
        name="Verified Fixture Node",
        layer="fixture",
        description="Reviewed public evidence fixture",
        source_note="Company IR",
    )
    source = Source.objects.create(key="fixture-route-sec", name="SEC fixture", license_status="open")
    SourceLicense.objects.create(source=source, status="open", scope="Fixture", public_display_allowed=True)
    company = Company.objects.create(
        slug="verified-fixture-company",
        name="Verified Fixture Company",
        ticker="VFCX",
        primary_node=node,
        description="Reviewed public company fixture",
        data_source_note="SEC EDGAR",
        source=source,
        sec_cik="0000000003",
        publication_batch_id=uuid.uuid4(),
        fetched_at=timezone.now(),
        license_scope="Fixture",
        is_published=True,
        quality_status="fresh",
    )
    objects = [
        thesis,
        letter,
        node,
        company,
    ]

    assert all(objects), "seed_platform must provide one example of every detail page"
    for obj in objects:
        response = client.get(obj.get_absolute_url())
        assert response.status_code == 200, obj.get_absolute_url()

    fed_document = FedDocument.objects.create(
        document_type=FedDocument.DocumentType.SPEECH,
        slug="verified-fixture-speech",
        title="Verified Fixture Speech",
        summary="Official metadata fixture",
        published_at=timezone.now(),
        original_url="https://www.federalreserve.gov/newsevents/speech/fixture.htm",
    )
    model = ModelProfile.objects.create(
        slug="verified-fixture-model",
        name="Verified Fixture Model",
        provider="Fixture Provider",
        release_date="2030-01-01",
        capability_score=1,
        description="Official vendor metadata fixture",
        sources=[{"label": "Vendor", "url": "https://openai.com/research/fixture"}],
    )
    agent = CodingAgentProfile.objects.create(
        slug="verified-fixture-agent",
        name="Verified Fixture Agent",
        provider="Fixture Provider",
        product_type="CLI",
        release_date="2030-01-01",
        price_label="N/A",
        capability_score=1,
        description="Official vendor metadata fixture",
        homepage="https://example.org/verified-agent",
    )
    assert fed_document and model and agent

    fed_prefix = {
        FedDocument.DocumentType.STATEMENT: "statements",
        FedDocument.DocumentType.SPEECH: "speeches",
        FedDocument.DocumentType.NEWS: "news",
    }[fed_document.document_type]
    dynamic_paths = [
        f"/fed/{fed_prefix}/{fed_document.slug}/",
        f"/ai-industry/chain/model-evolution/model/{model.slug}/",
        f"/ai-industry/vibe-coding/{agent.slug}/",
    ]
    for path in dynamic_paths:
        assert client.get(path).status_code == 200, path


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/credit/issuance/", "/credit/events/"])
def test_retired_credit_routes_return_gone(client, path):
    response = client.get(path)
    assert response.status_code == 410
    assert "不再提供" in response.content.decode() or "gone" in response.content.decode().lower()


@pytest.mark.django_db
def test_credit_cds_is_a_page_specific_no_number_purchase_boundary(
    client, seeded_platform
):
    rogue_source = Source.objects.create(
        key=f"rogue-cds-{uuid.uuid4()}",
        name="Rogue CDS fixture",
        license_status=Source.LicenseStatus.OPEN,
        redistribution_allowed=True,
    )
    DashboardSnapshot.objects.create(
        key="credit-cds",
        title="Rogue CDS snapshot",
        as_of=timezone.now(),
        source=rogue_source,
        is_published=True,
        summary="38 / 100",
        data={
            "demo": False,
            "metrics": [
                {
                    "label": "银行代理",
                    "display_value": "322bp",
                    "value": 322,
                }
            ],
            "charts": [
                {
                    "key": "rogue-cds-chart",
                    "title": "KBWB 14D",
                    "kind": "line",
                    "data": [{"date": "2026-07-15", "Yahoo 代理": 38}],
                }
            ],
            "sections": [{"title": "主权代理", "body": "HY 保护代理"}],
        },
    )
    DataRequirement.objects.create(
        key="credit-cds-route-fixture",
        page_key="credit-cds",
        metric_name="CDX IG/HY 与单名 CDS",
        status=DataRequirement.Status.PURCHASE_REQUIRED,
        vendor="S&P Global / ICE / LSEG",
        reason="Composite history and public display rights are required.",
    )
    response = client.get("/credit/cds/")
    body = response.content.decode()

    assert response.status_code == 200
    assert "Composite、成交与代理不是同一口径" in body
    assert "采购后最低字段合同" in body
    assert "数据覆盖与采购状态" in body
    assert "CDX IG/HY 与单名 CDS" in body
    assert 'aria-label="数据视图筛选"' not in body
    assert 'id="dashboard-primary-chart"' not in body
    assert "暂无可视化数据" not in body
    assert response.context["metrics"] == []
    assert response.context["charts"] == []
    assert response.context.get("snapshot") is None
    sections = response.context["sections"]
    assert [column["key"] for column in sections[0]["columns"]] == [
        "quote-type",
        "what-it-is",
        "why-not-substitute",
        "required-licence",
    ]
    assert [column["key"] for column in sections[2]["columns"]] == [
        "field",
        "requirement",
        "reason",
    ]
    for section in (sections[0], sections[2]):
        expected_keys = [column["key"] for column in section["columns"]]
        for row in section["rows"]:
            assert [item["key"] for item in row["cells_list"]] == expected_keys
            assert all(
                set(item["cell"]) == {"kind", "value"}
                and item["cell"]["kind"] == "text"
                for item in row["cells_list"]
            )
    assert body.count("<table") == 3
    assert 'class="metric-card' not in body
    assert "data-chart-source=" not in body
    for forbidden in (
        "322bp",
        "KBWB 14D",
        "银行代理",
        "主权代理",
        "HY 保护代理",
        "Yahoo 代理",
        "38 / 100",
    ):
        assert forbidden not in body


@pytest.mark.django_db
def test_unknown_route_returns_404(client):
    assert client.get("/this-route-must-never-exist/").status_code == 404


@pytest.mark.django_db
def test_health_endpoint_is_cache_safe(client):
    response = client.get("/healthz/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
