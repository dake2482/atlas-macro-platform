from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import date, timedelta

import httpx
import pytest
from django.test import override_settings
from django.utils import timezone

from research.models import (
    Company,
    DashboardSnapshot,
    FinancialFact,
    IngestionRun,
    RawArtifact,
    SECCompanyFact,
    Source,
    SourceLicense,
    SupplyChainNode,
)
from research.providers import ProviderResult, SECProvider
from research.sec_company_facts import (
    REVIEWED_COMPANIES,
    complete_five_year_metrics,
    normalize_annual_facts,
    refresh_sec_company_data,
    select_annual_metrics,
    select_public_supply_chain_demand_snapshot,
    validate_public_supply_chain_demand_snapshot,
)


def _payload(slug: str = "microsoft"):
    spec = next(item for item in REVIEWED_COMPANIES if item.slug == slug)
    concepts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": [],
        "NetIncomeLoss": [],
        "NetCashProvidedByUsedInOperatingActivities": [],
        "CostOfRevenue": [],
        "PaymentsToAcquireProductiveAssets" if slug == "amazon" else "PaymentsToAcquirePropertyPlantAndEquipment": [],
    }
    for year in range(2020, 2025):
        start = date(year - 1, 7, 1) if slug == "microsoft" else date(year, 1, 1)
        end = date(year, 6, 30) if slug == "microsoft" else date(year, 12, 31)
        for concept, value in (
            ("RevenueFromContractWithCustomerExcludingAssessedTax", 1000 + year),
            ("NetIncomeLoss", 200 + year),
            ("NetCashProvidedByUsedInOperatingActivities", 300 + year),
            ("CostOfRevenue", 400 + year),
            (next(iter(set(concepts) - {"RevenueFromContractWithCustomerExcludingAssessedTax", "NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities", "CostOfRevenue"})), 100 + year),
        ):
            concepts[concept].append({
                "start": start.isoformat(), "end": end.isoformat(), "val": value,
                "accn": f"{year}-10K", "fy": year, "fp": "FY", "form": "10-K",
                "filed": f"{year + 1}-02-01",
            })
    return {"entityName": spec.name, "cik": spec.normalized_cik, "facts": {"us-gaap": {concept: {"units": {"USD": rows}} for concept, rows in concepts.items()}}}


class CompleteFixtureProvider:
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
        raw = json.dumps(payload, sort_keys=True).encode()
        return ProviderResult(
            provider="sec",
            dataset="companyfacts",
            records=[payload],
            fetched_at=timezone.now(),
            raw_bytes=raw,
            metadata={"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
        )


def test_provider_requires_identity_and_preserves_exact_response_bytes():
    skipped = SECProvider(user_agent="", client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    assert skipped.company_facts("789019").skipped
    payload = {"cik": "0000789019", "entityName": "Microsoft", "tickers": ["MSFT"]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw, headers={"content-type": "application/json"})))
    result = SECProvider(user_agent="Atlas Macro test test@example.invalid", client=client).submissions("789019")
    assert result.raw_bytes == raw
    assert result.metadata["byte_length"] == len(raw)
    assert result.metadata["endpoint"].endswith("/submissions/CIK0000789019.json")
    assert result.metadata["sha256"]


def test_sec_provider_retries_rate_limits_with_injected_clock_and_sleep():
    seen = []
    sleeps = []
    raw = b'{"cik":"0000789019"}'

    def handler(request):
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(429, request=request, headers={"retry-after": "0.2"})
        return httpx.Response(200, request=request, content=raw, headers={"content-type": "application/json"})

    client = httpx.Client(base_url="https://data.sec.gov", transport=httpx.MockTransport(handler))
    result = SECProvider(
        user_agent="Atlas Macro monitored-contact@example.invalid",
        client=client,
        clock=lambda: 0.0,
        sleep=sleeps.append,
    ).submissions("789019")
    assert result.raw_bytes == raw
    assert len(seen) == 3
    assert all(request.url.path == "/submissions/CIK0000789019.json" for request in seen)
    assert all(request.headers["user-agent"] == "Atlas Macro monitored-contact@example.invalid" for request in seen)
    assert sleeps and max(sleeps) <= 8.0


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_sec_provider_retries_bounded_server_errors(status):
    seen = []
    raw = b'{"cik":"0000789019"}'

    def handler(request):
        seen.append(request)
        if len(seen) <= 3:
            return httpx.Response(status, request=request)
        return httpx.Response(200, request=request, content=raw)

    client = httpx.Client(base_url="https://data.sec.gov", transport=httpx.MockTransport(handler))
    result = SECProvider(
        user_agent="Atlas Macro monitored-contact@example.invalid",
        client=client,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
    ).submissions("789019")

    assert result.raw_bytes == raw
    assert len(seen) == 4


def test_sec_provider_retries_transport_errors_but_not_parse_errors():
    transport_seen = []
    raw = b'{"cik":"0000789019"}'

    def transport_handler(request):
        transport_seen.append(request)
        if len(transport_seen) <= 2:
            raise httpx.ConnectError("fixture transport error", request=request)
        return httpx.Response(200, request=request, content=raw)

    transport_client = httpx.Client(
        base_url="https://data.sec.gov", transport=httpx.MockTransport(transport_handler)
    )
    transport_result = SECProvider(
        user_agent="Atlas Macro monitored-contact@example.invalid",
        client=transport_client,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
    ).submissions("789019")
    assert transport_result.raw_bytes == raw
    assert len(transport_seen) == 3

    parse_seen = []

    def parse_handler(request):
        parse_seen.append(request)
        return httpx.Response(200, request=request, content=b"not-json")

    parse_client = httpx.Client(
        base_url="https://data.sec.gov", transport=httpx.MockTransport(parse_handler)
    )
    parse_result = SECProvider(
        user_agent="Atlas Macro monitored-contact@example.invalid",
        client=parse_client,
        clock=lambda: 0.0,
        sleep=lambda _delay: None,
    ).submissions("789019")
    assert parse_result.error
    assert len(parse_seen) == 1
    assert parse_result.raw_bytes is None


def test_normalizer_rejects_quarterly_ytd_non_usd_and_bad_duration_and_selects_five_years():
    payload = _payload()
    payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["EUR"] = [{"start": "2023-01-01", "end": "2023-12-31", "val": 1, "accn": "bad", "fp": "FY", "form": "10-K", "filed": "2024-01-01"}]
    payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"].extend([
        {"start": "2024-01-01", "end": "2024-03-31", "val": 1, "accn": "q", "fp": "Q1", "form": "10-Q", "filed": "2024-05-01"},
        {"start": "2023-01-01", "end": "2023-06-30", "val": 1, "accn": "short", "fp": "FY", "form": "10-K", "filed": "2024-01-01"},
    ])
    rows = normalize_annual_facts(payload, "microsoft")
    assert len(rows) == 25
    assert rows.diagnostics["rejected"]["non_usd"] == 1
    assert rows.diagnostics["rejected"]["non_annual_form"] == 1
    assert rows.diagnostics["rejected"]["invalid_duration"] == 1
    metrics = select_annual_metrics(rows, "microsoft")
    assert complete_five_year_metrics(metrics)
    assert metrics[2024]["gross_profit"]["concept"] == "GrossProfitDerived"
    assert metrics[2024]["capex_definition"] == "sec-cash-ppe"


def test_amazon_uses_broader_productive_assets_definition():
    rows = normalize_annual_facts(_payload("amazon"), "amazon")
    metrics = select_annual_metrics(rows, "amazon")
    assert metrics[2024]["capex_definition"] == "sec-cash-productive-assets"


def test_selection_uses_newest_five_year_window_and_blocks_incomplete_latest_year():
    payload = _payload()
    concepts = payload["facts"]["us-gaap"]
    for year in range(2017, 2027):
        for concept, concept_payload in concepts.items():
            rows = concept_payload["units"]["USD"]
            rows.append({
                "start": f"{year - 1}-07-01", "end": f"{year}-06-30", "val": 1000 + year,
                "accn": f"{year}-extra", "fy": year, "fp": "FY", "form": "10-K", "filed": f"{year + 1}-02-01",
            })
    metrics = select_annual_metrics(normalize_annual_facts(payload, "microsoft"), "microsoft")
    assert list(metrics) == [2022, 2023, 2024, 2025, 2026]
    concepts["NetIncomeLoss"]["units"]["USD"].append({
        "start": "2026-07-01", "end": "2027-06-30", "val": 1,
        "accn": "2027-incomplete", "fy": 2027, "fp": "FY", "form": "10-K", "filed": "2028-02-01",
    })
    blocked = select_annual_metrics(normalize_annual_facts(payload, "microsoft"), "microsoft")
    assert blocked == {}
    assert blocked.diagnostics["latest_observed_year_incomplete"] is True


def test_selection_fails_closed_when_latest_is_complete_but_prior_year_has_a_hole():
    payload = _payload()
    concepts = payload["facts"]["us-gaap"]
    for year in (2025, 2026):
        for concept_payload in concepts.values():
            concept_payload["units"]["USD"].append(
                {
                    "start": f"{year - 1}-07-01",
                    "end": f"{year}-06-30",
                    "val": 1000 + year,
                    "accn": f"{year}-extra",
                    "fy": year,
                    "fp": "FY",
                    "form": "10-K",
                    "filed": f"{year + 1}-02-01",
                }
            )
    concepts["NetIncomeLoss"]["units"]["USD"] = [
        row for row in concepts["NetIncomeLoss"]["units"]["USD"] if row["fy"] != 2025
    ]

    metrics = select_annual_metrics(normalize_annual_facts(payload, "microsoft"), "microsoft")

    assert metrics == {}
    assert metrics.diagnostics["latest_observed_fiscal_year"] == 2026
    assert metrics.diagnostics["latest_window_complete"] is False
    assert not complete_five_year_metrics(metrics)


def test_cost_concept_priority_wins_only_after_newest_filing_semantics():
    payload = _payload()
    payload["facts"]["us-gaap"]["CostOfGoodsAndServicesSold"] = {
        "units": {
            "USD": [
                {
                    **row,
                    "val": row["val"] + 1,
                    "accn": row["accn"],
                }
                for row in payload["facts"]["us-gaap"]["CostOfRevenue"]["units"]["USD"]
            ]
        }
    }

    rows = normalize_annual_facts(payload, "microsoft")
    metrics = select_annual_metrics(rows, "microsoft")
    assert metrics[2024]["gross_profit_derivation"]["input_concepts"][-1] == "CostOfRevenue"

    for row in payload["facts"]["us-gaap"]["CostOfGoodsAndServicesSold"]["units"]["USD"]:
        row["filed"] = "2026-02-01"
    newest_metrics = select_annual_metrics(normalize_annual_facts(payload, "microsoft"), "microsoft")
    assert newest_metrics[2024]["gross_profit_derivation"]["input_concepts"][-1] == "CostOfGoodsAndServicesSold"


@pytest.mark.django_db
def test_semantic_fact_idempotence_keeps_count_but_refreshes_public_lineage(client):
    class FixtureProvider:
        def submissions(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(provider="sec", dataset="submissions", records=[{"cik": cik, "entityName": spec.name, "tickers": [spec.ticker]}], fetched_at=timezone.now(), raw_bytes=f"submission-{cik}".encode())

        def company_facts(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            payload = _payload(spec.slug)
            return ProviderResult(provider="sec", dataset="companyfacts", records=[payload], fetched_at=timezone.now(), raw_bytes=json.dumps(payload, sort_keys=True).encode())

    first = refresh_sec_company_data(provider=FixtureProvider())
    first_fact_count = SECCompanyFact.objects.count()
    first_fetched_at = FinancialFact.objects.filter(publication_batch_id=first["batch_id"]).first().fetched_at
    second = refresh_sec_company_data(provider=FixtureProvider())
    assert first["published"] is True and second["published"] is True
    assert SECCompanyFact.objects.count() == first_fact_count
    assert DashboardSnapshot.objects.filter(key="supply-chain-demand", is_published=True).count() == 2
    latest = FinancialFact.objects.filter(publication_batch_id=second["batch_id"]).first()
    assert latest.metadata["lineage"]["capex"]["ingestion_run_batch_id"]
    assert latest.metadata["lineage"]["capex"]["raw_artifact_id"]
    assert latest.fetched_at > first_fetched_at


@pytest.mark.django_db
def test_private_artifact_is_exact_and_orphan_bytes_are_retained_for_safe_gc(tmp_path):
    from research.sec_company_facts import persist_raw_artifact

    with override_settings(RAW_ARTIFACT_ROOT=tmp_path):
        source = Source.objects.create(key="fixture-artifact", name="Fixture", license_status="open")
        run = IngestionRun.objects.create(source=source, dataset="companyfacts:test", started_at=timezone.now())
        raw = b"exact synthetic SEC bytes"
        result = ProviderResult(provider="sec", dataset="companyfacts", raw_bytes=raw)
        artifact = persist_raw_artifact(run=run, result=result)
        path = tmp_path / artifact.sha256[:2] / f"{artifact.sha256}.bin"
        assert path.read_bytes() == raw
        assert artifact.size_bytes == len(raw)
        assert artifact.uri.startswith("private://sec/")
        assert not str(path).startswith("/srv/")
        assert path.exists()
        orphan = persist_raw_artifact(run=run, result=ProviderResult(provider="sec", dataset="orphan", raw_bytes=b"unique orphan"))
        orphan_path = tmp_path / orphan.sha256[:2] / f"{orphan.sha256}.bin"
        orphan.delete()
        # Transaction failure/row deletion never performs an unlocked
        # check-then-unlink; a later safe GC owns this cleanup.
        assert orphan_path.exists()


@pytest.mark.django_db
def test_raw_artifact_requires_exact_nonempty_bytes_and_valid_provider_metadata(tmp_path):
    from research.sec_company_facts import persist_raw_artifact

    with override_settings(RAW_ARTIFACT_ROOT=tmp_path):
        source = Source.objects.create(key="fixture-artifact-validation", name="Fixture", license_status="open")
        run = IngestionRun.objects.create(
            source=source, dataset="companyfacts:validation", started_at=timezone.now()
        )
        with pytest.raises(ValueError, match="non-empty exact response bytes"):
            persist_raw_artifact(
                run=run, result=ProviderResult(provider="sec", dataset="empty", raw_bytes=b"")
            )
        with pytest.raises(ValueError, match="byte_length"):
            persist_raw_artifact(
                run=run,
                result=ProviderResult(
                    provider="sec",
                    dataset="bad-length",
                    raw_bytes=b"exact",
                    metadata={"byte_length": 99},
                ),
            )
        with pytest.raises(ValueError, match="sha256"):
            persist_raw_artifact(
                run=run,
                result=ProviderResult(
                    provider="sec",
                    dataset="bad-hash",
                    raw_bytes=b"exact",
                    metadata={"sha256": "0" * 64},
                ),
            )


@pytest.mark.django_db
def test_raw_artifact_database_failure_retains_bytes_for_safe_gc(tmp_path, monkeypatch):
    from research.sec_company_facts import persist_raw_artifact

    with override_settings(RAW_ARTIFACT_ROOT=tmp_path):
        source = Source.objects.create(key="fixture-artifact-failure", name="Fixture", license_status="open")
        run = IngestionRun.objects.create(
            source=source, dataset="companyfacts:failure", started_at=timezone.now()
        )
        raw = b"retain-on-transaction-failure"

        def fail_create(*_args, **_kwargs):
            raise RuntimeError("fixture database failure")

        monkeypatch.setattr(RawArtifact.objects, "create", fail_create)
        with pytest.raises(RuntimeError, match="fixture database failure"):
            persist_raw_artifact(
                run=run, result=ProviderResult(provider="sec", dataset="failure", raw_bytes=raw)
            )
        digest = hashlib.sha256(raw).hexdigest()
        assert (tmp_path / digest[:2] / f"{digest}.bin").read_bytes() == raw


@pytest.mark.django_db
def test_demand_chart_contract_controls_and_lineage_table(client):
    class FixtureProvider:
        def submissions(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(provider="sec", dataset="submissions", records=[{"cik": cik, "entityName": spec.name, "tickers": [spec.ticker]}], fetched_at=timezone.now(), raw_bytes=b"submission")

        def company_facts(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            payload = _payload(spec.slug)
            return ProviderResult(provider="sec", dataset="companyfacts", records=[payload], fetched_at=timezone.now(), raw_bytes=json.dumps(payload, sort_keys=True).encode())

    refresh_sec_company_data(provider=FixtureProvider())
    response = client.get("/supply-chain/demand/?period=3y&tab=capex-intensity")
    assert response.status_code == 200
    chart = response.context["charts"][0]
    assert response.context["selected_tab"] == "capex-intensity"
    assert len(chart["data"]["_rows"]) == 3
    assert all(isinstance(value, (int, float)) for item in chart["data"]["series"] for value in item["data"])
    assert any(value != 0 for item in chart["data"]["series"] for value in item["data"])
    assert "batch" in response.content.decode()
    assert "来源" in response.content.decode()
    five_year = client.get("/supply-chain/demand/?period=5y&tab=not-a-tab")
    assert five_year.context["selected_period"] == "5y"
    assert five_year.context["selected_tab"] == "reported-capex"
    assert len(five_year.context["charts"][0]["data"]["_rows"]) == 5


@pytest.mark.django_db
def test_demand_snapshot_validator_rejects_missing_rows_charts_and_mixed_batch():
    refresh_sec_company_data(provider=CompleteFixtureProvider())
    snapshot = DashboardSnapshot.objects.get(key="supply-chain-demand", is_published=True)
    assert validate_public_supply_chain_demand_snapshot(snapshot) == []

    original = copy.deepcopy(snapshot.data)
    invalid_cases = []

    missing_company = copy.deepcopy(original)
    missing_company["rows"] = [
        row for row in missing_company["rows"] if row["company"] != "meta"
    ]
    invalid_cases.append(missing_company)

    missing_row = copy.deepcopy(original)
    missing_row["rows"] = missing_row["rows"][:-1]
    invalid_cases.append(missing_row)

    missing_chart = copy.deepcopy(original)
    missing_chart["charts"] = missing_chart["charts"][:-1]
    invalid_cases.append(missing_chart)

    mixed_batch = copy.deepcopy(original)
    mixed_batch["rows"][0]["publication_batch_id"] = str(uuid.uuid4())
    invalid_cases.append(mixed_batch)

    mixed_chart_batch = copy.deepcopy(original)
    mixed_chart_batch["charts"][0]["data"]["series"][0]["lineage"][0][
        "publication_batch_id"
    ] = str(uuid.uuid4())
    invalid_cases.append(mixed_chart_batch)

    incomplete_chart_lineage = copy.deepcopy(original)
    component = incomplete_chart_lineage["charts"][0]["data"]["series"][0][
        "lineage"
    ][0]
    component.pop("value_date")
    component["source_fact_ids"] = {}
    component["quality_status"] = "error"
    invalid_cases.append(incomplete_chart_lineage)

    for data in invalid_cases:
        candidate = copy.copy(snapshot)
        candidate.data = data
        assert validate_public_supply_chain_demand_snapshot(candidate)


@pytest.mark.django_db
def test_newer_invalid_demand_snapshot_does_not_override_last_complete_stale_snapshot(client):
    refresh_sec_company_data(provider=CompleteFixtureProvider())
    valid = DashboardSnapshot.objects.get(key="supply-chain-demand", is_published=True)
    invalid_batch_id = uuid.uuid4()
    invalid_data = json.loads(
        json.dumps(valid.data).replace(str(valid.batch_id), str(invalid_batch_id))
    )
    invalid_data["charts"][0]["data"]["series"][0]["lineage"][0][
        "publication_batch_id"
    ] = str(uuid.uuid4())
    invalid = DashboardSnapshot.objects.create(
        key="supply-chain-demand",
        title="Invalid newer fixture",
        as_of=timezone.now(),
        # Every top-level and nested component was moved to this new batch.
        # Only the single chart lineage component above is mixed, so this
        # regression proves the component-level gate itself.
        batch_id=invalid_batch_id,
        quality_status="fresh",
        data=invalid_data,
        source=valid.source,
        is_published=True,
    )
    DashboardSnapshot.objects.filter(pk=invalid.pk).update(
        created_at=timezone.now() + timedelta(minutes=1)
    )

    selected = select_public_supply_chain_demand_snapshot()
    response = client.get("/supply-chain/demand/")

    assert selected is not None and selected.pk == valid.pk
    assert response.context["snapshot"].pk == valid.pk


@pytest.mark.django_db
def test_demand_kpis_and_series_render_actual_component_lineage(client):
    refresh_sec_company_data(provider=CompleteFixtureProvider())
    snapshot = DashboardSnapshot.objects.get(key="supply-chain-demand", is_published=True)
    response = client.get("/supply-chain/demand/")
    body = response.content.decode()
    metrics = snapshot.data["metrics"]

    assert all(
        metric["source"] == "U.S. Securities and Exchange Commission EDGAR"
        and metric["as_of"]
        and metric["value_date"]
        and metric["publication_batch_id"] == str(snapshot.batch_id)
        and metric["fetched_at"]
        and metric["license_scope"]
        and metric["fallback_source"] is None
        for metric in metrics
    )
    assert all(
        component["fetched_at"]
        and component["publication_batch_id"] == str(snapshot.batch_id)
        and component["source_key"] == "sec"
        for chart in snapshot.data["charts"]
        for series in chart["data"]["series"]
        for component in series["lineage"]
    )
    assert f"批次 {snapshot.batch_id}" in body
    assert "fallback 无" in body
    assert "组件级发布血缘" in body


@pytest.mark.django_db
def test_first_failure_is_private_and_later_failure_marks_prior_exact_batch_stale(client):
    class FailingProvider:
        def submissions(self, cik):
            raise RuntimeError("fixture endpoint unavailable")

        def company_facts(self, cik):
            raise AssertionError("companyfacts must not run after identity failure")

    failed = refresh_sec_company_data(provider=FailingProvider())
    assert failed["published"] is False
    assert not Company.objects.filter(is_published=True).exists()
    assert not FinancialFact.objects.filter(
        company__slug__in=[spec.slug for spec in REVIEWED_COMPANIES]
    ).exists()
    assert not DashboardSnapshot.objects.filter(
        key="supply-chain-demand", source__key="sec", is_published=True
    ).exists()


@pytest.mark.django_db
def test_later_failure_stales_prior_batch_without_replacement(client):
    class FixtureProvider:
        def submissions(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(provider="sec", dataset="submissions", records=[{"cik": cik, "entityName": spec.name, "tickers": [spec.ticker]}], fetched_at=timezone.now(), raw_bytes=b"submission")

        def company_facts(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            payload = _payload(spec.slug)
            return ProviderResult(provider="sec", dataset="companyfacts", records=[payload], fetched_at=timezone.now(), raw_bytes=json.dumps(payload, sort_keys=True).encode())

    class OneCompanyFailure(FixtureProvider):
        def submissions(self, cik):
            if cik == "0001018724":
                raise RuntimeError("Amazon fixture unavailable")
            return super().submissions(cik)

    first = refresh_sec_company_data(provider=FixtureProvider())
    result = refresh_sec_company_data(provider=OneCompanyFailure())
    assert first["published"] is True and result["published"] is False
    assert DashboardSnapshot.objects.filter(key="supply-chain-demand", is_published=True).count() == 1
    snapshot = DashboardSnapshot.objects.get(key="supply-chain-demand")
    assert snapshot.quality_status == "stale"
    assert all(metric["quality_status"] == "stale" for metric in snapshot.data["metrics"])
    assert all(chart["quality_status"] == "stale" for chart in snapshot.data["charts"])
    assert Company.objects.filter(is_published=True, quality_status="stale").count() == 4


@pytest.mark.django_db
def test_refresh_failure_does_not_stale_generic_or_non_sec_companies(client):
    refresh_sec_company_data(provider=CompleteFixtureProvider())
    node = SupplyChainNode.objects.get(slug="cloud-providers")
    generic_source = Source.objects.create(
        key="fixture-generic-refresh", name="Generic fixture", license_status="open"
    )
    SourceLicense.objects.create(
        source=generic_source,
        status="open",
        scope="Generic fixture public display",
        public_display_allowed=True,
        derived_display_allowed=False,
        historical_storage_allowed=True,
    )
    generic = Company.objects.create(
        slug="generic-refresh-company",
        name="Generic Refresh Company",
        ticker="GRC",
        primary_node=node,
        description="Generic public fixture",
        data_source_note="Generic official source",
        source=generic_source,
        publication_batch_id=uuid.uuid4(),
        fetched_at=timezone.now(),
        license_scope="Generic fixture public display",
        is_published=True,
        quality_status="fresh",
    )

    class FailingProvider(CompleteFixtureProvider):
        def submissions(self, cik):
            if cik == "0001018724":
                raise RuntimeError("fixture endpoint unavailable")
            return super().submissions(cik)

    refresh_sec_company_data(provider=FailingProvider())

    generic.refresh_from_db()
    assert generic.quality_status == "fresh"


@pytest.mark.django_db
def test_current_license_revoke_hides_routes_and_regrant_can_reuse_semantic_facts(client):
    class FixtureProvider:
        def submissions(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(provider="sec", dataset="submissions", records=[{"cik": cik, "entityName": spec.name, "tickers": [spec.ticker]}], fetched_at=timezone.now(), raw_bytes=b"submission")

        def company_facts(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            payload = _payload(spec.slug)
            return ProviderResult(provider="sec", dataset="companyfacts", records=[payload], fetched_at=timezone.now(), raw_bytes=json.dumps(payload, sort_keys=True).encode())

    refresh_sec_company_data(provider=FixtureProvider())
    source = Source.objects.get(key="sec")
    license_row = source.licenses.get(is_current=True)
    license_row.public_display_allowed = False
    license_row.derived_display_allowed = False
    license_row.reviewed_by = "fixture reviewer"
    license_row.reviewed_at = timezone.now()
    license_row.save()
    revoked = refresh_sec_company_data(provider=FixtureProvider())
    assert revoked["published"] is False
    revoked_page = client.get("/ai-industry/company/microsoft/")
    assert revoked_page.status_code == 200
    assert "公司数据待接入" in revoked_page.content.decode()
    assert "公司层面的 SEC 现金资本开支" not in revoked_page.content.decode()
    assert "Source: U.S. Securities and Exchange Commission" not in client.get("/supply-chain/demand/").content.decode()
    license_row.is_current = False
    license_row.save()
    SourceLicense.objects.create(source=source, status="open", scope="regrant", public_display_allowed=True, derived_display_allowed=True, historical_storage_allowed=True, is_current=True)
    regranted = refresh_sec_company_data(provider=FixtureProvider())
    assert regranted["published"] is True
    assert SECCompanyFact.objects.count() == 100
    assert client.get("/ai-industry/company/microsoft/").status_code == 200


@pytest.mark.django_db
def test_derived_only_revoke_hides_demand_and_reviewed_financial_projection(client):
    refresh_sec_company_data(provider=CompleteFixtureProvider())
    source = Source.objects.get(key="sec")
    license_row = source.licenses.get(is_current=True)
    license_row.derived_display_allowed = False
    license_row.reviewed_by = "fixture derived-rights reviewer"
    license_row.reviewed_at = timezone.now()
    license_row.save()

    result = refresh_sec_company_data(provider=CompleteFixtureProvider())
    company_page = client.get("/ai-industry/company/microsoft/")
    demand_page = client.get("/supply-chain/demand/")

    assert result["published"] is False
    assert SourceLicense.objects.get(pk=license_row.pk).derived_display_allowed is False
    assert company_page.status_code == 200
    assert "SEC 衍生展示暂不可用" in company_page.content.decode()
    assert "最近三年财务" not in company_page.content.decode()
    assert "公司层面的 SEC 现金资本开支" not in demand_page.content.decode()


@pytest.mark.django_db
def test_complete_fixture_publishes_one_batch_and_twenty_projections(client):
    class FixtureProvider:
        def submissions(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(provider="sec", dataset="submissions", records=[{"cik": cik, "entityName": spec.name, "tickers": [spec.ticker]}], fetched_at=timezone.now(), raw_bytes=b"submission")

        def company_facts(self, cik):
            spec = next(item for item in REVIEWED_COMPANIES if item.normalized_cik == cik)
            return ProviderResult(provider="sec", dataset="companyfacts", records=[_payload(spec.slug)], fetched_at=timezone.now(), raw_bytes=json.dumps(_payload(spec.slug), sort_keys=True).encode())

    result = refresh_sec_company_data(provider=FixtureProvider())
    assert result["published"] is True
    assert DashboardSnapshot.objects.filter(key="supply-chain-demand", is_published=True).count() == 1
    assert FinancialFact.objects.filter(publication_batch_id=result["batch_id"]).count() == 20
    assert SECCompanyFact.objects.count() == 100
    assert Company.objects.filter(is_published=True, publication_batch_id=result["batch_id"]).count() == 4
    company_page = client.get("/ai-industry/company/microsoft/")
    demand_page = client.get("/supply-chain/demand/")
    assert company_page.status_code == 200
    assert demand_page.status_code == 200
    assert "公司层面的 SEC 现金资本开支" in demand_page.content.decode()
