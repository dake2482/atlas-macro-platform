from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from research.models import DashboardSnapshot, IngestionRun, MetricSnapshot, RawArtifact
from research.official_data import (
    TREASURY_CURVE_CONTRACT_VERSION,
    TREASURY_NOMINAL_TENORS,
    TREASURY_REAL_TENORS,
    _coordinate_treasury_curve_dashboards,
    _store_treasury_curve_observations,
)
from research.providers import ProviderResult, TreasuryRatesProvider
from research.services import record_provider_result

FIXED_NOW = datetime(2026, 7, 12, 16, 30, tzinfo=UTC)
CURRENT_DATE = date(2026, 7, 10)
NOMINAL_VALUES = {
    "1m": Decimal("3.70"),
    "2m": Decimal("3.75"),
    "3m": Decimal("3.85"),
    "4m": Decimal("3.90"),
    "6m": Decimal("4.00"),
    "1y": Decimal("4.10"),
    "2y": Decimal("4.21"),
    "3y": Decimal("4.25"),
    "5y": Decimal("4.30"),
    "7y": Decimal("4.40"),
    "10y": Decimal("4.56"),
    "20y": Decimal("4.90"),
    "30y": Decimal("5.06"),
}
REAL_VALUES = {
    "5y": Decimal("2.02"),
    "7y": Decimal("2.16"),
    "10y": Decimal("2.32"),
    "20y": Decimal("2.45"),
    "30y": Decimal("2.50"),
}


@pytest.fixture(autouse=True)
def compact_history_contract(monkeypatch):
    """Keep deterministic tests small while production still requires daily density."""

    monkeypatch.setattr(
        "research.official_data.TREASURY_CURVE_MIN_HISTORY_POINTS", 50
    )
    monkeypatch.setattr(
        "research.official_data.TREASURY_CURVE_MAX_GAP_DAYS", 40
    )
    monkeypatch.setattr(
        "research.official_data.TREASURY_CURVE_START_TOLERANCE_DAYS", 40
    )


def _first_friday(year: int) -> date:
    period = date(year, 1, 1)
    while period.weekday() != 4:
        period += timedelta(days=1)
    return period


def _fixture_dates(year: int) -> list[date]:
    end = CURRENT_DATE if year == CURRENT_DATE.year else date(year, 12, 31)
    period = _first_friday(year)
    dates = []
    while period <= end:
        dates.append(period)
        period += timedelta(days=30)
    if year == CURRENT_DATE.year and CURRENT_DATE not in dates:
        dates.append(CURRENT_DATE)
    return dates


def _curve_records(component: str, year: int) -> list[dict]:
    tenors = TREASURY_NOMINAL_TENORS if component == "nominal" else TREASURY_REAL_TENORS
    values = NOMINAL_VALUES if component == "nominal" else REAL_VALUES
    prefix = "UST" if component == "nominal" else "TIPS"
    records = []
    for period in _fixture_dates(year):
        for tenor in tenors:
            value = values[tenor]
            if year == CURRENT_DATE.year and period < CURRENT_DATE:
                value -= Decimal("0.01")
            records.append(
                {
                    "series_id": f"{prefix}-{tenor.upper()}",
                    "date": period.isoformat(),
                    "value": value,
                    "metadata": {
                        "curve": component,
                        "requested_year": year,
                        "dataset": f"fixture:{component}:{year}",
                    },
                }
            )
    return records


def _record_curve_run(
    component: str,
    year: int,
    *,
    cycle: str,
    fail: bool = False,
) -> IngestionRun:
    dataset_prefix = (
        "daily_treasury_yield_curve"
        if component == "nominal"
        else "daily_treasury_real_yield_curve"
    )
    dataset = f"{dataset_prefix}:{year}"
    if fail:
        result = ProviderResult.failure(
            "us-treasury-rates", dataset, "fixture annual curve failure"
        )
        result.metadata["refresh_cycle_id"] = cycle
    else:
        result = ProviderResult(
            provider="us-treasury-rates",
            dataset=dataset,
            fetched_at=datetime(2026, 7, 10, 20, tzinfo=UTC),
            records=_curve_records(component, year),
            metadata={
                "requested_year": year,
                "curve": component,
                "refresh_cycle_id": cycle,
            },
        )
    return record_provider_result(
        result,
        persist=_store_treasury_curve_observations,
    )


def _record_curve_history(*, cycle: str | None = None) -> list[IngestionRun]:
    resolved_cycle = cycle or str(uuid.uuid4())
    return [
        _record_curve_run(component, year, cycle=resolved_cycle)
        for year in range(2021, 2027)
        for component in ("nominal", "real")
    ]


def _xml_curve_payload(
    *,
    value_date: str,
    entries: list[dict[str, str]],
) -> str:
    rendered_entries = []
    for values in entries:
        fields = "".join(f"<d:{key}>{value}</d:{key}>" for key, value in values.items())
        rendered_entries.append(
            f"<entry><content><m:properties><d:NEW_DATE>{value_date}T00:00:00"
            f"</d:NEW_DATE>{fields}</m:properties></content></entry>"
        )
    return (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
        'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">'
        + "".join(rendered_entries)
        + "</feed>"
    )


def _provider(payload: str) -> TreasuryRatesProvider:
    client = httpx.Client(
        base_url="https://home.treasury.gov",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=payload)
        ),
    )
    return TreasuryRatesProvider(client=client)


@pytest.mark.django_db
def test_treasury_provider_records_response_artifact_and_rejects_conflicting_duplicate():
    fields = {
        field: str(NOMINAL_VALUES[series_id.removeprefix("UST-").lower()])
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
    }
    payload = _xml_curve_payload(value_date="2026-07-10", entries=[fields])
    result = _provider(payload).yield_curve(year=2026)

    assert result.ok
    assert result.metadata["requested_year"] == 2026
    assert result.metadata["latest_value_date"] == "2026-07-10"
    artifact = result.metadata["artifacts"][0]
    assert artifact["url"].endswith(
        "?data=daily_treasury_yield_curve&field_tdr_date_value=2026"
    )
    assert len(artifact["sha256"]) == 64

    run = record_provider_result(
        result,
        persist=_store_treasury_curve_observations,
    )
    stored = RawArtifact.objects.get(run=run)
    assert stored.sha256 == artifact["sha256"]
    assert stored.size_bytes == artifact["size"]

    conflict = _xml_curve_payload(
        value_date="2026-07-10",
        entries=[fields, {**fields, "BC_10YEAR": "9.99"}],
    )
    conflict_result = _provider(conflict).yield_curve(year=2026)
    assert not conflict_result.ok
    assert "conflicting duplicate" in conflict_result.error

    future = _xml_curve_payload(value_date="2099-07-10", entries=[fields])
    future_result = _provider(future).yield_curve(year=2099)
    assert not future_result.ok
    assert "future date" in future_result.error


@pytest.mark.django_db
def test_treasury_curve_coordinator_publishes_exact_multi_year_contract(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_curve_history()

    dashboards, stale_keys = _coordinate_treasury_curve_dashboards(
        runs,
        end_year=2026,
    )

    assert {item.key for item in dashboards} == {"yield-curve", "real-rates"}
    assert stale_keys == {"rates"}
    yield_snapshot = DashboardSnapshot.objects.get(key="yield-curve")
    real_snapshot = DashboardSnapshot.objects.get(key="real-rates")
    assert yield_snapshot.data["contract_version"] == TREASURY_CURVE_CONTRACT_VERSION
    assert yield_snapshot.data["common_effective_date"] == "2026-07-10"
    assert yield_snapshot.data["comparison_dates"] == {
        "当前": "2026-07-10",
        "1周前": "2026-07-01",
        "1月前": "2026-06-01",
        "3月前": "2026-04-02",
    }
    assert len(yield_snapshot.data["component_batches"]) == 6
    assert len(real_snapshot.data["component_batches"]) == 12

    yield_metrics = {item["key"]: item for item in yield_snapshot.data["metrics"]}
    real_metrics = {item["key"]: item for item in real_snapshot.data["metrics"]}
    assert yield_metrics["2s10s"]["display_value"] == "+35bp"
    assert yield_metrics["3m10s"]["display_value"] == "+71bp"
    assert yield_metrics["5s30s"]["display_value"] == "+76bp"
    assert real_metrics["5y-bei"]["display_value"] == "2.28%"
    assert real_metrics["10y-bei"]["display_value"] == "2.24%"
    assert real_metrics["10y-bei"]["metadata"]["formula"] == "UST-10Y - TIPS-10Y"
    assert "approximation" in real_metrics["10y-bei"]["metadata"]["model_label"]

    charts = {item["key"]: item for item in yield_snapshot.data["charts"]}
    assert len(charts["curve-spreads-history"]["data"]) >= 50
    assert charts["curve-spreads-history"]["time_axis"] == "date"
    assert charts["curve-spreads-history"]["lineage_mode"] == "series-batch-segments"
    assert len(charts["curve-spreads-history"]["series_batch_lineage"]["ust-10y"]) == 6

    normalized = MetricSnapshot.objects.get(
        key="real-rates-10y-bei",
        batch_id=real_snapshot.batch_id,
    )
    assert normalized.metadata["formula"] == "UST-10Y - TIPS-10Y"
    assert len(normalized.metadata["input_lineage"]) == 2


@pytest.mark.django_db
def test_treasury_curve_failure_retains_and_marks_previous_contract_stale(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    first_runs = _record_curve_history(cycle="first-cycle")
    _coordinate_treasury_curve_dashboards(first_runs, end_year=2026)
    original = DashboardSnapshot.objects.get(key="yield-curve")
    original_batch = original.batch_id

    failed_nominal = _record_curve_run(
        "nominal", 2026, cycle="failed-cycle", fail=True
    )
    successful_real = _record_curve_run("real", 2026, cycle="failed-cycle")
    dashboards, stale_keys = _coordinate_treasury_curve_dashboards(
        [failed_nominal, successful_real],
        end_year=2026,
    )

    assert dashboards == []
    assert stale_keys == {"yield-curve", "real-rates", "rates"}
    original.refresh_from_db()
    assert original.batch_id == original_batch
    assert original.quality_status == "stale"
    assert "继续保留上一版" in original.data["refresh_failure"]["reason"]
    assert any(
        item["source"] == "daily_treasury_yield_curve:2026"
        and item["status"] == "failed"
        for item in original.data["refresh_failure"]["sources"]
    )


@pytest.mark.django_db
def test_treasury_same_values_refresh_lineage_without_duplicate_snapshot(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    first_runs = _record_curve_history(cycle="same-values-one")
    _coordinate_treasury_curve_dashboards(first_runs, end_year=2026)
    first = DashboardSnapshot.objects.get(key="yield-curve")
    first_batches = set(first.data["component_batches"])

    second_runs = [
        _record_curve_run(component, 2026, cycle="same-values-two")
        for component in ("nominal", "real")
    ]
    dashboards, _stale = _coordinate_treasury_curve_dashboards(
        second_runs,
        end_year=2026,
    )

    assert dashboards == []
    assert DashboardSnapshot.objects.filter(key="yield-curve").count() == 1
    first.refresh_from_db()
    assert first.data["publication_batch_id"] == str(first.batch_id)
    assert set(first.data["component_batches"]) != first_batches
    assert set(first.data["component_batches"]) == {
        *(
            str(run.batch_id)
            for run in first_runs
            if run.dataset.startswith("daily_treasury_yield_curve:")
            and not run.dataset.endswith(":2026")
        ),
        str(second_runs[0].batch_id),
    }


@pytest.mark.django_db
def test_treasury_routes_and_assets_overview_use_yields_not_etf_prices(client, monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_curve_history()
    _coordinate_treasury_curve_dashboards(runs, end_year=2026)

    default_curve = client.get("/rates/yield-curve/")
    assert default_curve.context["selected_tab"] == "curve"
    assert [item["key"] for item in default_curve.context["charts"]] == [
        "nominal-curve-comparison"
    ]

    response = client.get("/assets/bonds/?period=1y&tab=spreads")
    body = response.content.decode()
    assert response.status_code == 200
    assert response.context["snapshot"].key == "yield-curve"
    assert response.context["selected_period"] == "1y"
    assert response.context["selected_tab"] == "spreads"
    assert len(response.context["charts"]) == 1
    assert response.context["charts"][0]["key"] == "curve-spreads-history"
    assert len(response.context["charts"][0]["data"]) < 20
    assert "4.56%" in body
    assert "+35bp" in body
    assert "不是债券或 ETF 价格" in body
    assert "TLT" not in body
    assert "IEF" not in body
    assert "LQD" not in body
    assert "HYG" not in body

    assets = client.get("/assets/").content.decode()
    assert "1 / 6" in assets
    assert "UST 10Y" in assets
    assert "4.56%" in assets
    assert "+1bp" in assets
    assert "$" not in assets


@pytest.mark.django_db
def test_treasury_storage_rejects_annual_tail_regression():
    first = _record_curve_run("nominal", 2026, cycle="regression-one")
    assert first.status == "success"
    regressed_records = [
        item
        for item in _curve_records("nominal", 2026)
        if item["date"] <= "2026-07-03"
    ]
    result = ProviderResult(
        provider="us-treasury-rates",
        dataset="daily_treasury_yield_curve:2026",
        fetched_at=datetime(2026, 7, 11, tzinfo=UTC),
        records=regressed_records,
        metadata={"requested_year": 2026, "refresh_cycle_id": "regression-two"},
    )
    second = record_provider_result(
        result,
        persist=_store_treasury_curve_observations,
    )

    assert second.status == "failed"
    assert "latest date regressed" in second.error
