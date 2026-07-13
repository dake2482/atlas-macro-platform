from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.utils import timezone

from research.admin import CompanyAdmin
from research.models import (
    Company,
    FinancialFact,
    Instrument,
    MarketBar,
    Source,
    SourceLicense,
    SupplyChainNode,
)
from research.providers import ProviderResult
from research.public_ai_contract import PUBLIC_AI_COMPANY_CONTRACT_SLUGS
from research.sec_company_facts import REVIEWED_COMPANIES, refresh_sec_company_data

CONTRACT_FILE = Path(__file__).resolve().parents[1] / "assets/timsun_public_ai_contract.json"
COMPANY_ROUTE_PREFIX = "/ai-industry/company/"


def _published_company_fields():
    source = Source.objects.create(
        key=f"fixture-sec-{uuid.uuid4().hex[:8]}", name="SEC fixture", license_status="open",
        redistribution_allowed=True,
    )
    SourceLicense.objects.create(
        source=source, status="open", scope="Fixture public display", public_display_allowed=True,
        derived_display_allowed=True,
    )
    return {
        "source": source,
        "sec_cik": "0000000001",
        "publication_batch_id": uuid.uuid4(),
        "fetched_at": timezone.now(),
        "license_scope": "Fixture public display",
        "is_published": True,
        "quality_status": "fresh",
    }


def _manifest_company_paths() -> list[str]:
    payload = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
    return [
        item["path"]
        for item in payload["routes"]
        if item.get("family") == "company"
    ]


def _slug_from_path(path: str) -> str:
    assert path.startswith(COMPANY_ROUTE_PREFIX) and path.endswith("/")
    return path.removeprefix(COMPANY_ROUTE_PREFIX).removesuffix("/")


def test_compiled_company_contract_exactly_matches_audit_manifest():
    paths = _manifest_company_paths()

    assert len(paths) == 219
    assert len(paths) == len(set(paths))
    assert {_slug_from_path(path) for path in paths} == PUBLIC_AI_COMPANY_CONTRACT_SLUGS


@pytest.mark.django_db
def test_all_219_missing_company_contract_routes_return_transparent_pending_pages(client):
    paths = _manifest_company_paths()
    original_company_count = Company.objects.count()

    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "公司数据待接入".encode() in response.content, path
        assert _slug_from_path(path).encode() in response.content, path
        assert b'<meta name="robots" content="noindex,nofollow">' in response.content, path
        assert b'class="metric-card"' not in response.content, path

    assert Company.objects.count() == original_company_count


@pytest.mark.django_db
def test_public_database_company_keeps_the_sourced_company_detail(client):
    node = SupplyChainNode.objects.create(
        slug="verified-gpu-node",
        name="Verified GPU node",
        layer="compute",
        description="Reviewed node",
        source_note="Official company filing",
    )
    Company.objects.create(
        slug="nvidia",
        name="VERIFIED-COMPANY-DETAIL",
        ticker="NVDA",
        primary_node=node,
        description="Reviewed company record",
        data_source_note="SEC filing and official investor relations",
        investor_relations_url="https://investor.nvidia.com/",
        **_published_company_fields(),
    )

    response = client.get("/ai-industry/company/nvidia/")
    body = response.content.decode()

    assert response.status_code == 200
    assert "VERIFIED-COMPANY-DETAIL" in body
    assert "公司数据待接入" not in body
    assert '<meta name="robots" content="index,follow">' in body


@pytest.mark.django_db
def test_unreviewed_database_row_does_not_replace_contract_pending_page(client):
    node = SupplyChainNode.objects.create(
        slug="unreviewed-contract-node",
        name="Unreviewed contract node",
        layer="test",
        description="Unreviewed node",
        source_note="Official source",
    )
    Company.objects.create(
        slug="nvidia",
        name="UNREVIEWED-COMPANY-MUST-NOT-LEAK",
        ticker="NVDA",
        primary_node=node,
        description="No source record",
        data_source_note="",
    )

    response = client.get("/ai-industry/company/nvidia/")
    body = response.content.decode()

    assert response.status_code == 200
    assert "公司数据待接入" in body
    assert "UNREVIEWED-COMPANY-MUST-NOT-LEAK" not in body
    assert Company.objects.count() == 1


@pytest.mark.django_db
def test_unknown_company_slug_remains_not_found(client):
    response = client.get("/ai-industry/company/not-in-the-audited-contract/")

    assert response.status_code == 404
    assert Company.objects.count() == 0


@pytest.mark.django_db
def test_published_company_contract_and_reviewed_identity_constraints_are_fail_closed():
    node = SupplyChainNode.objects.create(
        slug="contract-constraint-node",
        name="Contract constraint node",
        layer="compute",
        description="Constraint fixture",
        source_note="Official fixture",
    )
    valid_fields = _published_company_fields()
    valid_fields["sec_cik"] = ""
    valid = Company.objects.create(
        slug="generic-valid-company",
        name="Generic Valid Company",
        ticker="GVC",
        primary_node=node,
        description="Valid generic published company",
        data_source_note="Generic official source",
        **valid_fields,
    )
    assert valid.is_published is True

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Company.objects.create(
                slug="published-contract-missing-fields",
                name="Missing contract fields",
                ticker="MCF",
                primary_node=node,
                description="Must fail universal publication contract",
                data_source_note="Generic official source",
                is_published=True,
                quality_status="fresh",
            )

    for slug, cik in (
        ("microsoft", "0000000009"),
        ("generic-reviewed-cik", "0000789019"),
    ):
        fields = _published_company_fields()
        fields["sec_cik"] = cik
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Company.objects.create(
                    slug=slug,
                    name=f"Invalid reviewed identity {slug}",
                    ticker="BAD",
                    primary_node=node,
                    description="Must fail reviewed identity contract",
                    data_source_note="Generic official source",
                    **fields,
                )


@pytest.mark.django_db
def test_reviewed_identity_with_non_sec_source_is_not_publicly_selectable(client):
    node = SupplyChainNode.objects.create(
        slug="non-sec-reviewed-node",
        name="Non-SEC reviewed node",
        layer="compute",
        description="Selector fixture",
        source_note="Official fixture",
    )
    fields = _published_company_fields()
    fields["sec_cik"] = "0000789019"
    Company.objects.create(
        slug="microsoft",
        name="Wrong Source Microsoft",
        ticker="MSFT",
        primary_node=node,
        description="Reviewed identity with the wrong provider",
        data_source_note="Generic official source",
        **fields,
    )

    response = client.get("/ai-industry/company/microsoft/")

    assert response.status_code == 200
    assert "公司数据待接入" in response.content.decode()
    assert "Wrong Source Microsoft" not in response.content.decode()


def test_company_admin_denies_mutation_for_published_rows_but_keeps_unpublished_editable():
    model_admin = CompanyAdmin(Company, admin.site)
    request = RequestFactory().get("/admin/research/company/")
    published = SimpleNamespace(is_published=True)
    unpublished = SimpleNamespace(is_published=False)

    assert model_admin.has_change_permission(request, published) is False
    assert model_admin.has_delete_permission(request, published) is False
    assert "slug" in model_admin.get_readonly_fields(request, published)
    assert "sec_cik" in model_admin.get_readonly_fields(request, published)
    assert model_admin.get_readonly_fields(request, unpublished) == ()


@pytest.mark.django_db
def test_generic_company_restores_public_financial_and_authorized_market_chart(client):
    node = SupplyChainNode.objects.create(
        slug="generic-company-node",
        name="Generic company node",
        layer="applications",
        description="Generic fixture",
        source_note="Official fixture",
    )
    fields = _published_company_fields()
    fields["source"].name = "Generic public source"
    fields["source"].save(update_fields=["name"])
    company = Company.objects.create(
        slug="generic-market-company",
        name="Generic Market Company",
        name_en="Generic Market Company",
        ticker="GMC",
        price="123.45",
        market_cap_usd_m="9876.00",
        primary_node=node,
        description="Generic public company fixture",
        data_source_note="Generic official source",
        **fields,
    )
    FinancialFact.objects.create(
        company=company,
        fiscal_year=2024,
        revenue_usd_m="100.00",
        revenue_growth="8.00",
        gross_margin="42.00",
        source=fields["source"],
        quality_status="fresh",
        fetched_at=timezone.now(),
        license_scope="Fixture public display",
    )
    instrument = Instrument.objects.create(
        symbol="GMC",
        name="Generic Market Company",
        asset_class="equity",
    )
    MarketBar.objects.create(
        instrument=instrument,
        interval="1d",
        value_date=timezone.now() - timedelta(days=1),
        open="120",
        high="125",
        low="119",
        close="123",
        fetched_at=timezone.now(),
        source=fields["source"],
        quality_status="fresh",
        license_scope="Fixture public display",
    )

    body = client.get("/ai-industry/company/generic-market-company/").content.decode()

    assert "Generic sourced profile" in body
    assert "公开财务事实" in body
    assert "授权价格走势" in body
    assert "AI-only CapEx" not in body
    assert "Amazon productive-assets" not in body
    assert "SEC 数据声明" not in body


@pytest.mark.django_db
def test_generic_unbatched_financial_fact_keeps_company_year_unique():
    node = SupplyChainNode.objects.create(
        slug="generic-financial-constraint-node",
        name="Generic financial constraint node",
        layer="applications",
        description="Generic financial uniqueness fixture",
        source_note="Official fixture",
    )
    fields = _published_company_fields()
    company = Company.objects.create(
        slug="generic-financial-constraint-company",
        name="Generic Financial Constraint Company",
        ticker="GFCC",
        primary_node=node,
        description="Generic financial uniqueness fixture",
        data_source_note="Generic official source",
        **fields,
    )
    FinancialFact.objects.create(
        company=company,
        fiscal_year=2024,
        revenue_usd_m="100.00",
        source=fields["source"],
        quality_status="fresh",
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FinancialFact.objects.create(
                company=company,
                fiscal_year=2024,
                revenue_usd_m="101.00",
                source=fields["source"],
                quality_status="fresh",
            )


@pytest.mark.django_db
def test_successful_sec_contract_has_four_live_and_215_pending_routes_and_clean_discovery(client):
    from tests.test_sec_company_facts import _payload

    class FixtureProvider:
        def submissions(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(
                provider="sec",
                dataset="submissions",
                records=[{"cik": cik, "entityName": spec.name, "tickers": [spec.ticker]}],
                fetched_at=timezone.now(),
                raw_bytes=f"submission-{cik}".encode(),
            )

        def company_facts(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            payload = _payload(spec.slug)
            return ProviderResult(
                provider="sec",
                dataset="companyfacts",
                records=[payload],
                fetched_at=timezone.now(),
                raw_bytes=json.dumps(payload, sort_keys=True).encode(),
            )

    result = refresh_sec_company_data(provider=FixtureProvider())
    live_slugs = set(
        Company.objects.filter(is_published=True).values_list("slug", flat=True)
    )
    live_paths = [
        path for path in _manifest_company_paths() if _slug_from_path(path) in live_slugs
    ]
    pending_paths = [
        path for path in _manifest_company_paths() if _slug_from_path(path) not in live_slugs
    ]
    pending_slug = _slug_from_path(pending_paths[0])
    node = SupplyChainNode.objects.get(slug="cloud-providers")
    Company.objects.create(
        slug=pending_slug,
        name="PRIVATE PARTIAL COMPANY",
        ticker="PPC",
        primary_node=node,
        description="Private submission row must remain pending",
        data_source_note="SEC private partial",
        is_published=False,
    )

    assert result["published"] is True
    assert live_slugs == {spec.slug for spec in REVIEWED_COMPANIES}
    assert len(live_paths) == 4
    assert len(pending_paths) == 215
    for path in live_paths:
        response = client.get(path)
        assert response.status_code == 200
        assert "公司数据待接入" not in response.content.decode()
    for path in pending_paths:
        response = client.get(path)
        assert response.status_code == 200
        assert "公司数据待接入" in response.content.decode()

    private_response = client.get(f"{COMPANY_ROUTE_PREFIX}{pending_slug}/")
    search_body = client.get("/search/?q=PRIVATE+PARTIAL+COMPANY").content.decode()
    sitemap_body = client.get("/sitemap.xml").content.decode()
    llms_body = client.get("/llms.txt").content.decode()
    assert private_response.status_code == 200
    assert "公司数据待接入" in private_response.content.decode()
    assert f"{COMPANY_ROUTE_PREFIX}{pending_slug}/" not in search_body
    assert all(path not in sitemap_body for path in pending_paths)
    assert all(path not in llms_body for path in pending_paths)
    assert all(path in sitemap_body for path in live_paths)
    assert all(path in llms_body for path in live_paths)
