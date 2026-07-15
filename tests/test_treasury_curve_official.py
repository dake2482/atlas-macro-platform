from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from research.models import (
    DashboardSnapshot,
    IngestionRun,
    Instrument,
    MetricSnapshot,
    Observation,
    RawArtifact,
)
from research.official_data import (
    TREASURY_CURVE_CONTRACT_VERSION,
    TREASURY_NOMINAL_TENORS,
    TREASURY_REAL_TENORS,
    TREASURY_YIELD_SUMMARY,
    TREASURY_YIELD_TITLE,
    _coordinate_fed_funds_dashboard,
    _coordinate_treasury_curve_dashboards,
    _publish_dashboard,
    _publish_treasury_curve_revisions,
    _store_treasury_curve_observations,
    refresh_treasury_curve_data,
    select_public_treasury_curve_snapshot,
)
from research.providers import ProviderResult, TreasuryRatesProvider
from research.services import begin_ingestion, ensure_source, record_provider_result
from tests.test_fed_funds_official import _fed_funds_runs

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
TREASURY_CANONICAL_FEED_IDS = {
    "daily_treasury_yield_curve": (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml-item?data=daily_treasury_yield_curve"
    ),
    "daily_treasury_real_yield_curve": (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml-item?data=daily_treasury_real_yield_curve"
    ),
}


@pytest.fixture(autouse=True)
def compact_history_contract(monkeypatch, settings, tmp_path):
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
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"


@pytest.fixture(autouse=True)
def isolated_treasury_contract_state(db):
    """Keep strict-contract tests independent of the session demo seed."""

    MetricSnapshot.objects.all().delete()
    DashboardSnapshot.objects.all().delete()
    Observation.objects.all().delete()
    IngestionRun.objects.all().delete()
    Instrument.objects.filter(symbol="TLT").delete()
    ensure_source("internal")
    ensure_source("us-treasury-rates")


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
        records = _curve_records(component, year)
        field_by_series = {
            series_id: field
            for field, series_id in (
                TreasuryRatesProvider.NOMINAL_FIELDS.items()
                if component == "nominal"
                else TreasuryRatesProvider.REAL_FIELDS.items()
            )
        }
        entries: list[tuple[str, dict[str, str]]] = []
        for period in _fixture_dates(year):
            by_date = {
                field_by_series[str(item["series_id"])]: str(item["value"])
                for item in records
                if item["date"] == period.isoformat()
            }
            entries.append((period.isoformat(), by_date))
        payload = _xml_curve_payload(component=component, entries=entries)
        result = (
            _provider(payload).yield_curve(year=year)
            if component == "nominal"
            else _provider(payload).real_yield_curve(year=year)
        )
        assert result.ok, result.error
        result.fetched_at = datetime(2026, 7, 10, 20, tzinfo=UTC)
        result.metadata["refresh_cycle_id"] = cycle
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
    component: str,
    entries: list[tuple[str, dict[str, str]]],
    title_override: str | None = None,
    feed_id_override: str | None = None,
    updated: str = "2026-07-10T19:00:00Z",
) -> str:
    rendered_entries = []
    for value_date, values in entries:
        fields = "".join(
            f'<d:{key} m:type="Edm.Double">{value}</d:{key}>'
            for key, value in values.items()
        )
        rendered_entries.append(
            '<entry><content type="application/xml"><m:properties>'
            f'<d:NEW_DATE m:type="Edm.DateTime">{value_date}T00:00:00'
            f"</d:NEW_DATE>{fields}</m:properties></content></entry>"
        )
    curve = (
        "daily_treasury_yield_curve"
        if component == "nominal"
        else "daily_treasury_real_yield_curve"
    )
    title = title_override or TreasuryRatesProvider.CURVE_TITLES[curve]
    feed_id = feed_id_override or TREASURY_CANONICAL_FEED_IDS[curve]
    return (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
        'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">'
        f"<title>{title}</title>"
        f"<id>{feed_id}</id>"
        f"<updated>{updated}</updated>"
        + "".join(rendered_entries)
        + "</feed>"
    )


def _provider(
    payload: str,
    provider_class: type[TreasuryRatesProvider] = TreasuryRatesProvider,
) -> TreasuryRatesProvider:
    client = httpx.Client(
        base_url=provider_class.base_url,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=payload.encode(),
                headers={"content-type": "text/xml; charset=UTF-8"},
            )
        ),
    )
    return provider_class(client=client)


def _curve_provider_result(component: str, year: int) -> ProviderResult:
    records = _curve_records(component, year)
    field_by_series = {
        series_id: field
        for field, series_id in (
            TreasuryRatesProvider.NOMINAL_FIELDS.items()
            if component == "nominal"
            else TreasuryRatesProvider.REAL_FIELDS.items()
        )
    }
    payload = _xml_curve_payload(
        component=component,
        entries=[
            (
                period.isoformat(),
                {
                    field_by_series[str(item["series_id"])]: str(item["value"])
                    for item in records
                    if item["date"] == period.isoformat()
                },
            )
            for period in _fixture_dates(year)
        ],
    )
    result = (
        _provider(payload).yield_curve(year=year)
        if component == "nominal"
        else _provider(payload).real_yield_curve(year=year)
    )
    assert result.ok, result.error
    result.fetched_at = datetime(2026, 7, 10, 20, tzinfo=UTC)
    return result


@pytest.mark.django_db
def test_treasury_provider_records_response_artifact_and_rejects_conflicting_duplicate():
    fields = {
        field: str(NOMINAL_VALUES[series_id.removeprefix("UST-").lower()])
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
    }
    payload = _xml_curve_payload(
        component="nominal", entries=[("2026-07-10", fields)]
    )
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
    assert stored.uri.startswith("private://us-treasury-rates/")
    assert result.raw_bytes == payload.encode()

    conflict = _xml_curve_payload(
        component="nominal",
        entries=[
            ("2026-07-10", fields),
            ("2026-07-10", {**fields, "BC_10YEAR": "9.99"}),
        ],
    )
    conflict_result = _provider(conflict).yield_curve(year=2026)
    assert not conflict_result.ok
    assert "duplicate Treasury curve entry" in conflict_result.error

    future_result = _provider(payload).yield_curve(year=2099)
    assert not future_result.ok
    assert "outside the supported" in future_result.error


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
def test_rates_parent_rebuilds_after_each_strict_fed_funds_revision(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    fed_runs = _fed_funds_runs(cycle="rates-fed-one")
    fed_dashboards, fed_stale = _coordinate_fed_funds_dashboard(
        fed_runs.values()
    )
    assert {item.key for item in fed_dashboards} == {"fed-funds"}
    assert fed_stale == set()
    treasury_runs = _record_curve_history(cycle="rates-treasury")

    dashboards, stale = _coordinate_treasury_curve_dashboards(
        treasury_runs,
        end_year=2026,
    )

    assert {item.key for item in dashboards} == {
        "yield-curve",
        "real-rates",
        "rates",
    }
    assert stale == set()
    first = select_public_treasury_curve_snapshot("rates")
    assert first is not None
    first_fed_reference = next(
        item
        for item in first.data["component_snapshots"]
        if item.get("component") == "fed-funds"
    )

    next_fed_runs = _fed_funds_runs(cycle="rates-fed-two")
    refreshed, refreshed_stale = _coordinate_fed_funds_dashboard(
        next_fed_runs.values()
    )

    assert refreshed_stale == set()
    assert {item.key for item in refreshed} == {"rates"}
    second = select_public_treasury_curve_snapshot("rates")
    assert second is not None and second.pk != first.pk
    second_fed_reference = next(
        item
        for item in second.data["component_snapshots"]
        if item.get("component") == "fed-funds"
    )
    assert (
        second_fed_reference["snapshot_id"]
        != first_fed_reference["snapshot_id"]
        or second_fed_reference["component_batches"]
        != first_fed_reference["component_batches"]
    )


@pytest.mark.django_db
def test_rates_parent_retains_on_fed_failure_and_recovers_atomically(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    fed_runs = _fed_funds_runs(cycle="rates-retained-fed")
    _coordinate_fed_funds_dashboard(fed_runs.values())
    treasury_runs = _record_curve_history(cycle="rates-retained-treasury")
    _coordinate_treasury_curve_dashboards(treasury_runs, end_year=2026)
    original = select_public_treasury_curve_snapshot("rates")
    assert original is not None

    failed_sofr = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets",
            "reference-rate:sofr",
            "SOFR unavailable",
        )
    )
    dashboards, stale = _coordinate_fed_funds_dashboard([failed_sofr])

    assert dashboards == []
    assert stale == {"fed-funds", "rates"}
    retained = select_public_treasury_curve_snapshot("rates")
    assert retained is not None and retained.pk == original.pk
    assert retained.treasury_publication_state == "retained_failure"
    assert retained.quality_status == Observation.Quality.STALE
    assert retained.data["refresh_failure"]["kind"] == "fed-funds-child"

    treasury_running = begin_ingestion(
        "us-treasury-rates",
        "daily_treasury_yield_curve:2026",
    )
    mixed = select_public_treasury_curve_snapshot("rates")
    assert mixed is not None and mixed.pk == original.pk
    assert mixed.treasury_publication_state == "retained_failure"
    assert mixed.data["refresh_failure"]["kind"] == "fed-funds-child"
    treasury_running.delete()

    fed_running = begin_ingestion(
        "ny-fed-markets",
        "reference-rate:sofr",
    )
    transition = select_public_treasury_curve_snapshot("rates")
    assert transition is not None and transition.pk == original.pk
    assert transition.treasury_publication_state == "transition_pending"
    assert "refresh_failure" not in transition.data
    original.refresh_from_db()
    assert original.data["refresh_failure"]["kind"] == "fed-funds-child"

    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: FIXED_NOW + timedelta(hours=2),
    )
    assert select_public_treasury_curve_snapshot("rates") is None
    fed_running.delete()
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)

    recovery_runs = _fed_funds_runs(cycle="rates-retained-recovery")
    recovered, recovered_stale = _coordinate_fed_funds_dashboard(
        recovery_runs.values()
    )
    assert recovered_stale == set()
    assert {item.key for item in recovered} == {"rates"}
    current = select_public_treasury_curve_snapshot("rates")
    assert current is not None and current.pk != original.pk
    assert current.treasury_publication_state == "current_candidate"


@pytest.mark.django_db
def test_rates_parent_fails_closed_for_child_reference_and_metric_tamper(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    fed_runs = _fed_funds_runs(cycle="rates-tamper-fed")
    _coordinate_fed_funds_dashboard(fed_runs.values())
    treasury_runs = _record_curve_history(cycle="rates-tamper-treasury")
    _coordinate_treasury_curve_dashboards(treasury_runs, end_year=2026)
    parent = select_public_treasury_curve_snapshot("rates")
    assert parent is not None

    original_data = deepcopy(parent.data)
    tampered = deepcopy(parent.data)
    child_reference = next(
        item
        for item in tampered["component_snapshots"]
        if item.get("component_page_key") == "yield-curve"
    )
    child_reference["component_payload_integrity_hash"] = "0" * 64
    parent.data = tampered
    parent.save(update_fields=["data", "updated_at"])
    assert select_public_treasury_curve_snapshot("rates") is None
    parent.data = original_data
    parent.save(update_fields=["data", "updated_at"])

    metric = MetricSnapshot.objects.get(
        key="rates-sofr",
        batch_id=parent.batch_id,
    )
    original_value = metric.value
    metric.value = Decimal("9.99")
    metric.save(update_fields=["value", "updated_at"])
    assert select_public_treasury_curve_snapshot("rates") is None
    metric.value = original_value
    metric.save(update_fields=["value", "updated_at"])
    assert select_public_treasury_curve_snapshot("rates") is not None


@pytest.mark.django_db
def test_rates_postcondition_tamper_rolls_back_the_whole_treasury_publication(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    fed_runs = _fed_funds_runs(cycle="rates-rollback-fed")
    _coordinate_fed_funds_dashboard(fed_runs.values())
    first_runs = _record_curve_history(cycle="rates-rollback-first")
    _coordinate_treasury_curve_dashboards(first_runs, end_year=2026)
    snapshot_count = DashboardSnapshot.objects.count()
    metric_count = MetricSnapshot.objects.count()
    original_publisher = _publish_treasury_curve_revisions

    def corrupt_parent(**kwargs):
        published = original_publisher(**kwargs)
        rates = (
            DashboardSnapshot.objects.filter(key="rates")
            .order_by("-created_at", "-id")
            .first()
        )
        assert rates is not None
        metric = MetricSnapshot.objects.get(
            key="rates-sofr",
            batch_id=rates.batch_id,
        )
        metric.value = Decimal("9.99")
        metric.save(update_fields=["value", "updated_at"])
        return published

    monkeypatch.setattr(
        "research.official_data._publish_treasury_curve_revisions",
        corrupt_parent,
    )
    next_runs = [
        _record_curve_run(component, 2026, cycle="rates-rollback-second")
        for component in ("nominal", "real")
    ]
    dashboards, stale = _coordinate_treasury_curve_dashboards(
        next_runs,
        end_year=2026,
    )

    assert dashboards == []
    assert stale == {"yield-curve", "real-rates", "rates"}
    assert DashboardSnapshot.objects.count() == snapshot_count
    assert MetricSnapshot.objects.count() == metric_count
    for key in ("yield-curve", "real-rates", "rates"):
        retained = select_public_treasury_curve_snapshot(key)
        assert retained is not None, key
        assert retained.treasury_publication_state == "retained_failure"


@pytest.mark.django_db
def test_treasury_curve_failure_retains_and_marks_previous_contract_stale(
    client,
    monkeypatch,
):
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
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk == original.pk
    assert selected.treasury_publication_state == "retained_failure"

    overview = client.get("/assets/")
    bond_group = next(
        item for item in overview.context["asset_groups"] if item["key"] == "bond"
    )
    assert bond_group["rows"]
    assert {item["status"] for item in bond_group["rows"]} == {
        Observation.Quality.STALE
    }
    bonds = client.get("/assets/bonds/")
    assert {
        item["quality_status"] for item in bonds.context["metrics"]
    } == {Observation.Quality.STALE}

    original.quality_status = Observation.Quality.FRESH
    original.save(update_fields=["quality_status", "updated_at"])
    assert select_public_treasury_curve_snapshot("yield-curve") is None
    original.quality_status = Observation.Quality.STALE
    original.save(update_fields=["quality_status", "updated_at"])

    running_nominal = begin_ingestion(
        "us-treasury-rates",
        "daily_treasury_yield_curve:2026",
    )
    running_real = begin_ingestion(
        "us-treasury-rates",
        "daily_treasury_real_yield_curve:2026",
    )
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk == original.pk
    assert selected.treasury_publication_state == "transition_pending"
    assert "refresh_failure" not in selected.data
    original.refresh_from_db()
    assert "refresh_failure" in original.data

    response = client.get("/rates/yield-curve/")
    assert response.status_code == 200
    assert response.context["refresh_failure"] is None
    assert response.context["stale_notice"]["reason_code"] == "transition-pending"
    assert "fixture annual curve failure" not in response.content.decode()

    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: FIXED_NOW + timedelta(hours=2),
    )
    assert select_public_treasury_curve_snapshot("yield-curve") is None
    running_nominal.delete()
    running_real.delete()


@pytest.mark.django_db
def test_treasury_same_values_refresh_creates_an_immutable_revision(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    first_runs = _record_curve_history(cycle="same-values-one")
    _coordinate_treasury_curve_dashboards(first_runs, end_year=2026)
    first = DashboardSnapshot.objects.get(key="yield-curve")
    first_batches = set(first.data["component_batches"])
    first_data = dict(first.data)

    second_runs = [
        _record_curve_run(component, 2026, cycle="same-values-two")
        for component in ("nominal", "real")
    ]
    dashboards, _stale = _coordinate_treasury_curve_dashboards(
        second_runs,
        end_year=2026,
    )

    assert {item.key for item in dashboards} == {"yield-curve", "real-rates"}
    assert DashboardSnapshot.objects.filter(key="yield-curve").count() == 2
    first.refresh_from_db()
    assert first.data["publication_batch_id"] == str(first.batch_id)
    assert first.data == first_data
    assert set(first.data["component_batches"]) == first_batches
    latest = (
        DashboardSnapshot.objects.filter(key="yield-curve")
        .order_by("-created_at", "-id")
        .first()
    )
    assert latest is not None and latest.pk != first.pk
    assert set(latest.data["component_batches"]) == {
        *(
            str(run.batch_id)
            for run in first_runs
            if run.dataset.startswith("daily_treasury_yield_curve:")
            and not run.dataset.endswith(":2026")
        ),
        str(second_runs[0].batch_id),
    }
    first_current_nominal = next(
        run
        for run in first_runs
        if run.dataset == "daily_treasury_yield_curve:2026"
    )
    assert (
        Observation.objects.filter(batch_id=first_current_nominal.batch_id).count()
        == first_current_nominal.row_count
    )
    assert (
        Observation.objects.filter(batch_id=second_runs[0].batch_id).count()
        == second_runs[0].row_count
    )
    assert MetricSnapshot.objects.filter(
        batch_id=first.batch_id, key__startswith="yield-curve-"
    ).count() == 7
    assert MetricSnapshot.objects.filter(
        batch_id=latest.batch_id, key__startswith="yield-curve-"
    ).count() == 7


@pytest.mark.django_db
def test_treasury_exact_run_retry_clears_overlay_without_duplicate_revision(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_curve_history(cycle="retry-same-inputs")
    _coordinate_treasury_curve_dashboards(runs, end_year=2026)
    snapshot = DashboardSnapshot.objects.get(key="yield-curve")
    expected_quality = snapshot.quality_status
    initial_snapshot_count = DashboardSnapshot.objects.count()
    initial_metric_count = MetricSnapshot.objects.count()
    data = dict(snapshot.data)
    data["refresh_failure"] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "reason": "transient publication overlay",
        "components": [],
    }
    snapshot.data = data
    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])

    dashboards, stale = _coordinate_treasury_curve_dashboards(runs, end_year=2026)

    assert dashboards == []
    assert stale == {"rates"}
    snapshot.refresh_from_db()
    assert "refresh_failure" not in snapshot.data
    assert snapshot.quality_status == expected_quality
    assert DashboardSnapshot.objects.count() == initial_snapshot_count
    assert MetricSnapshot.objects.count() == initial_metric_count
    assert select_public_treasury_curve_snapshot("yield-curve") is not None


@pytest.mark.django_db
def test_treasury_marker_and_regression_anchor_ignore_a_newer_rogue_snapshot(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    first_runs = _record_curve_history(cycle="rogue-base")
    _coordinate_treasury_curve_dashboards(first_runs, end_year=2026)
    original = DashboardSnapshot.objects.get(key="yield-curve")
    rogue = DashboardSnapshot.objects.create(
        key="yield-curve",
        title=original.title,
        summary=original.summary,
        as_of=original.as_of,
        quality_status=Observation.Quality.FRESH,
        source=original.source,
        is_published=True,
        data={
            **original.data,
            "publication_batch_id": str(uuid.uuid4()),
            "common_effective_date": "2099-01-01",
        },
    )
    failed_nominal = _record_curve_run(
        "nominal", 2026, cycle="rogue-failure", fail=True
    )
    successful_real = _record_curve_run(
        "real", 2026, cycle="rogue-failure"
    )

    dashboards, stale = _coordinate_treasury_curve_dashboards(
        [failed_nominal, successful_real],
        end_year=2026,
    )

    assert dashboards == []
    assert stale == {"yield-curve", "real-rates", "rates"}
    rogue.refresh_from_db()
    original.refresh_from_db()
    assert "refresh_failure" not in rogue.data
    assert original.data["refresh_failure"]
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk == original.pk

    recovery = [
        _record_curve_run(component, 2026, cycle="rogue-recovery")
        for component in ("nominal", "real")
    ]
    published, _stale = _coordinate_treasury_curve_dashboards(
        recovery,
        end_year=2026,
    )
    assert {item.key for item in published} == {"yield-curve", "real-rates"}
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk not in {original.pk, rogue.pk}


@pytest.mark.django_db
def test_treasury_dedicated_publisher_rejects_superseded_run_map(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_curve_history(cycle="superseded-base")
    selected = {
        (component, year): run
        for run in runs
        for component, prefix in (
            ("nominal", "daily_treasury_yield_curve"),
            ("real", "daily_treasury_real_yield_curve"),
        )
        if run.dataset.startswith(f"{prefix}:")
        for year in [int(run.dataset.rsplit(":", 1)[1])]
    }
    _record_curve_run("nominal", 2026, cycle="superseding-run")

    with pytest.raises(ValueError, match="superseded annual run"):
        _publish_treasury_curve_revisions(
            selected_runs=selected,
            include_rates=False,
            batch_id=uuid.uuid4(),
        )
    assert DashboardSnapshot.objects.count() == 0
    assert MetricSnapshot.objects.count() == 0


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
def test_assets_overview_never_falls_back_to_bond_or_etf_prices(client):
    source = ensure_source("internal")
    instrument = Instrument.objects.create(
        symbol="TLT",
        name="iShares 20+ Year Treasury Bond ETF",
        asset_class="bond",
    )
    Observation.objects.create(
        instrument=instrument,
        value=Decimal("92.43"),
        value_date=FIXED_NOW,
        as_of=FIXED_NOW,
        fetched_at=FIXED_NOW,
        source=source,
        quality_status=Observation.Quality.FRESH,
    )

    response = client.get("/assets/")
    body = response.content.decode()

    assert response.status_code == 200
    assert "TLT" not in body
    assert "$92.43" not in body
    assert "绝不回退为债券或 ETF 价格" in body
    bond_group = next(
        item for item in response.context["asset_groups"] if item["key"] == "bond"
    )
    assert bond_group["rows"] == []


@pytest.mark.django_db
def test_treasury_storage_rejects_annual_tail_regression():
    first = _record_curve_run("nominal", 2026, cycle="regression-one")
    assert first.status == "success"
    regressed_records = [
        item
        for item in _curve_records("nominal", 2026)
        if item["date"] <= "2026-07-03"
    ]
    field_by_series = {
        series_id: field
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
    }
    dates = sorted({str(item["date"]) for item in regressed_records})
    payload = _xml_curve_payload(
        component="nominal",
        entries=[
            (
                period,
                {
                    field_by_series[str(item["series_id"])]: str(item["value"])
                    for item in regressed_records
                    if item["date"] == period
                },
            )
            for period in dates
        ],
    )
    result = _provider(payload).yield_curve(year=2026)
    assert result.ok, result.error
    result.fetched_at = datetime(2026, 7, 11, tzinfo=UTC)
    result.metadata["refresh_cycle_id"] = "regression-two"
    second = record_provider_result(
        result,
        persist=_store_treasury_curve_observations,
    )

    assert second.status == "failed"
    assert "latest date regressed" in second.error


@pytest.mark.django_db
def test_treasury_provider_rejects_feed_and_field_contract_drift():
    fields = {
        field: str(NOMINAL_VALUES[series_id.removeprefix("UST-").lower()])
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
    }
    wrong_title = _xml_curve_payload(
        component="nominal",
        entries=[("2026-07-10", fields)],
        title_override="WrongFeed",
    )
    assert "title" in _provider(wrong_title).yield_curve(year=2026).error

    wrong_type = _xml_curve_payload(
        component="nominal", entries=[("2026-07-10", fields)]
    ).replace(
        '<d:BC_10YEAR m:type="Edm.Double">',
        '<d:BC_10YEAR m:type="Edm.String">',
    )
    assert "field type" in _provider(wrong_type).yield_curve(year=2026).error

    non_finite = _xml_curve_payload(
        component="nominal",
        entries=[("2026-07-10", {**fields, "BC_10YEAR": "NaN"})],
    )
    assert "non-finite" in _provider(non_finite).yield_curve(year=2026).error

    future_updated = _xml_curve_payload(
        component="nominal",
        entries=[("2026-07-10", fields)],
        updated="2099-01-01T00:00:00Z",
    )
    assert "updated time" in _provider(future_updated).yield_curve(year=2026).error

    no_entries = _xml_curve_payload(component="nominal", entries=[])
    assert "no entries" in _provider(no_entries).yield_curve(year=2026).error

    duplicate_field = _xml_curve_payload(
        component="nominal", entries=[("2026-07-10", fields)]
    ).replace(
        "</m:properties>",
        '<d:BC_10YEAR m:type="Edm.Double">4.56</d:BC_10YEAR></m:properties>',
    )
    assert "duplicate field" in _provider(duplicate_field).yield_curve(year=2026).error

    missing_current_four_month = _xml_curve_payload(
        component="nominal",
        entries=[("2026-07-10", {key: value for key, value in fields.items() if key != "BC_4MONTH"})],
    )
    assert "coverage is incomplete" in _provider(
        missing_current_four_month
    ).yield_curve(year=2026).error

    evil_feed_id = _xml_curve_payload(
        component="nominal",
        entries=[("2026-07-10", fields)],
        feed_id_override=(
            "https://attacker.invalid/xml-item?data=daily_treasury_yield_curve"
        ),
    )
    assert "feed id" in _provider(evil_feed_id).yield_curve(year=2026).error

    legacy_shortcut_feed_id = _xml_curve_payload(
        component="nominal",
        entries=[("2026-07-10", fields)],
        feed_id_override=(
            "https://home.treasury.gov/xml-item?data=daily_treasury_yield_curve"
        ),
    )
    assert "feed id" in _provider(legacy_shortcut_feed_id).yield_curve(year=2026).error

    assert "outside the supported" in _provider(wrong_type).yield_curve(year=1989).error


@pytest.mark.parametrize(
    ("component", "method_name"),
    (("nominal", "yield_curve"), ("real", "real_yield_curve")),
)
def test_treasury_curve_feed_identity_is_independent_of_transport_origin(
    component,
    method_name,
):
    class MirrorTreasuryProvider(TreasuryRatesProvider):
        base_url = "https://mirror.invalid"

    field_map = (
        TreasuryRatesProvider.NOMINAL_FIELDS
        if component == "nominal"
        else TreasuryRatesProvider.REAL_FIELDS
    )
    values = NOMINAL_VALUES if component == "nominal" else REAL_VALUES
    prefix = "UST-" if component == "nominal" else "TIPS-"
    fields = {
        field: str(values[series_id.removeprefix(prefix).lower()])
        for field, series_id in field_map.items()
    }
    canonical_payload = _xml_curve_payload(
        component=component,
        entries=[("2026-07-10", fields)],
    )
    canonical_result = getattr(
        _provider(canonical_payload, MirrorTreasuryProvider),
        method_name,
    )(year=2026)
    assert canonical_result.ok, canonical_result.error

    curve = (
        "daily_treasury_yield_curve"
        if component == "nominal"
        else "daily_treasury_real_yield_curve"
    )
    mirror_payload = _xml_curve_payload(
        component=component,
        entries=[("2026-07-10", fields)],
        feed_id_override=(
            "https://mirror.invalid/resource-center/data-chart-center/"
            f"interest-rates/pages/xml-item?data={curve}"
        ),
    )
    mirror_result = getattr(
        _provider(mirror_payload, MirrorTreasuryProvider),
        method_name,
    )(year=2026)
    assert "feed id" in mirror_result.error


@pytest.mark.django_db
def test_treasury_historical_feed_can_omit_a_not_yet_published_tenor():
    fields = {
        field: str(NOMINAL_VALUES[series_id.removeprefix("UST-").lower()])
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
        if series_id != "UST-4M"
    }
    payload = _xml_curve_payload(
        component="nominal", entries=[("2021-12-31", fields)]
    )
    result = _provider(payload).yield_curve(year=2021)
    assert result.ok, result.error
    assert "UST-4M" not in result.metadata["series_coverage"]


@pytest.mark.django_db
def test_treasury_persistence_rolls_back_raw_record_or_transport_tamper():
    fields = {
        field: str(NOMINAL_VALUES[series_id.removeprefix("UST-").lower()])
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
    }
    payload = _xml_curve_payload(
        component="nominal", entries=[("2026-07-10", fields)]
    )
    result = _provider(payload).yield_curve(year=2026)
    assert result.ok
    result.metadata["refresh_cycle_id"] = "tampered-record"
    result.records[0]["value"] = Decimal("9.99")
    run = record_provider_result(
        result,
        persist=_store_treasury_curve_observations,
    )
    assert run.status == IngestionRun.Status.FAILED
    assert "do not replay" in run.error
    assert not Observation.objects.filter(batch_id=run.batch_id).exists()
    assert not RawArtifact.objects.filter(run=run).exists()

    result = _provider(payload).yield_curve(year=2026)
    assert result.ok
    result.metadata["refresh_cycle_id"] = "tampered-transport"
    result.metadata["sha256"] = "0" * 64
    run = record_provider_result(
        result,
        persist=_store_treasury_curve_observations,
    )
    assert run.status == IngestionRun.Status.FAILED
    assert "HTTP response witness" in run.error
    assert not Observation.objects.filter(batch_id=run.batch_id).exists()
    assert not RawArtifact.objects.filter(run=run).exists()


@pytest.mark.django_db
def test_treasury_selector_fails_closed_for_raw_payload_metric_and_licence_tamper(
    monkeypatch,
    settings,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_curve_history()
    _coordinate_treasury_curve_dashboards(runs, end_year=2026)
    snapshot = select_public_treasury_curve_snapshot("yield-curve")
    assert snapshot is not None
    assert snapshot.treasury_publication_state == "current_candidate"

    run = next(
        item
        for item in runs
        if item.dataset == "daily_treasury_yield_curve:2026"
    )
    artifact = RawArtifact.objects.get(run=run)
    artifact_path = (
        Path(settings.RAW_ARTIFACT_ROOT)
        / artifact.sha256[:2]
        / f"{artifact.sha256}.bin"
    )
    original_bytes = artifact_path.read_bytes()
    artifact_path.write_bytes(original_bytes + b"tamper")
    assert select_public_treasury_curve_snapshot("yield-curve") is None
    artifact_path.write_bytes(original_bytes)

    original_data = dict(snapshot.data)
    snapshot.data = {**original_data, "common_effective_date": "2026-07-09"}
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_treasury_curve_snapshot("yield-curve") is None
    snapshot.data = original_data
    snapshot.save(update_fields=["data", "updated_at"])

    metric = MetricSnapshot.objects.get(
        batch_id=snapshot.batch_id,
        key="yield-curve-ust-10y",
    )
    original_value = metric.value
    metric.value = Decimal("9.99")
    metric.save(update_fields=["value", "updated_at"])
    assert select_public_treasury_curve_snapshot("yield-curve") is None
    metric.value = original_value
    metric.save(update_fields=["value", "updated_at"])

    licence = run.source.licenses.get(is_current=True)
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])
    assert select_public_treasury_curve_snapshot("yield-curve") is None


@pytest.mark.django_db
def test_treasury_selector_distinguishes_transition_expiry_and_new_success(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_curve_history()
    _coordinate_treasury_curve_dashboards(runs, end_year=2026)
    snapshot = select_public_treasury_curve_snapshot("yield-curve")
    assert snapshot is not None

    running = begin_ingestion(
        "us-treasury-rates",
        "daily_treasury_yield_curve:2026",
    )
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk == snapshot.pk
    assert selected.treasury_publication_state == "transition_pending"
    running.delete()

    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: FIXED_NOW + timedelta(days=10),
    )
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk == snapshot.pk
    assert selected.treasury_publication_state == "natural_expiry"

    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _record_curve_run("nominal", 2026, cycle="unpublished-success")
    assert select_public_treasury_curve_snapshot("yield-curve") is None


@pytest.mark.django_db
def test_treasury_refresh_precreates_the_full_cycle_and_commits_publication_atomically(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    initial_runs = _record_curve_history(cycle="initial-cycle")
    _coordinate_treasury_curve_dashboards(initial_runs, end_year=2026)
    original = select_public_treasury_curve_snapshot("yield-curve")
    assert original is not None
    observed_states: list[str] = []

    class FakeTreasuryProvider:
        NOMINAL_FIELDS = TreasuryRatesProvider.NOMINAL_FIELDS
        REAL_FIELDS = TreasuryRatesProvider.REAL_FIELDS
        parse_curve_xml_bytes = TreasuryRatesProvider.parse_curve_xml_bytes

        def yield_curve(self, *, year):
            running = IngestionRun.objects.filter(
                source__key="us-treasury-rates",
                dataset__in={
                    f"daily_treasury_yield_curve:{year}",
                    f"daily_treasury_real_yield_curve:{year}",
                },
                status=IngestionRun.Status.RUNNING,
            )
            assert running.count() == 2
            return _curve_provider_result("nominal", year)

        def real_yield_curve(self, *, year):
            selected = select_public_treasury_curve_snapshot("yield-curve")
            assert selected is not None and selected.pk == original.pk
            observed_states.append(selected.treasury_publication_state)
            assert IngestionRun.objects.filter(
                dataset=f"daily_treasury_yield_curve:{year}",
                status=IngestionRun.Status.SUCCESS,
            ).count() == 2
            assert IngestionRun.objects.filter(
                dataset=f"daily_treasury_real_yield_curve:{year}",
                status=IngestionRun.Status.RUNNING,
            ).count() == 1
            return _curve_provider_result("real", year)

        def close(self):
            return None

    monkeypatch.setattr(
        "research.official_data.TreasuryRatesProvider",
        FakeTreasuryProvider,
    )
    summary = refresh_treasury_curve_data(start_year=2026, end_year=2026)

    assert observed_states == ["transition_pending"]
    assert set(summary["dashboard_keys"]) == {"yield-curve", "real-rates"}
    assert summary["stale_dashboard_keys"] == ["rates"]
    current = select_public_treasury_curve_snapshot("yield-curve")
    assert current is not None and current.pk != original.pk
    assert current.treasury_publication_state == "current_candidate"

    retained_before_no_publish = current.pk
    no_publish = refresh_treasury_curve_data(
        start_year=2026,
        end_year=2026,
        publish=False,
    )
    assert no_publish["dashboard_keys"] == []
    assert no_publish["stale_dashboard_keys"] == []
    selected = select_public_treasury_curve_snapshot("yield-curve")
    assert selected is not None and selected.pk == retained_before_no_publish


@pytest.mark.django_db
def test_generic_writer_and_legacy_snapshot_cannot_publish_treasury_v2():
    with pytest.raises(ValueError, match="dedicated Treasury curve v2 publisher"):
        _publish_dashboard(
            key="yield-curve",
            title="rogue",
            summary="rogue",
            metrics=[],
            charts=[],
            sections=[],
            batch_id=uuid.uuid4(),
        )

    source = _record_curve_run(
        "nominal", 2026, cycle="legacy-source"
    ).source
    legacy = DashboardSnapshot.objects.create(
        key="yield-curve",
        title=TREASURY_YIELD_TITLE,
        summary=TREASURY_YIELD_SUMMARY,
        as_of=FIXED_NOW,
        quality_status=Observation.Quality.FRESH,
        source=source,
        is_published=True,
        data={"contract_version": 1, "demo": False},
    )
    assert select_public_treasury_curve_snapshot(
        "yield-curve", [legacy]
    ) is None
