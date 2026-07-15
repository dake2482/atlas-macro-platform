from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from research.models import (
    DashboardSnapshot,
    MetricSnapshot,
    Observation,
    SeriesDefinition,
    SourceLicense,
)
from research.official_data import (
    RESERVES_RATE_SPREADS_REQUIRED_CHART_KEYS,
    RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS,
    _coordinate_reserves_dashboard,
    _coordinate_reserves_rate_spreads_dashboard,
    _reserves_rate_spreads_snapshot_contract_is_valid,
    _store_h8_observations,
    _store_h41_observations,
    select_public_reserves_rate_spreads_snapshot,
    select_public_reserves_snapshot,
)
from research.providers import ProviderResult, TreasuryRatesProvider
from research.services import record_provider_result, store_series_observations

FIXED_NOW = datetime(2026, 7, 14, 13, 0, tzinfo=UTC)
COMMON_DATE = date(2026, 7, 10)
LATEST_WEDNESDAY = date(2026, 7, 8)


@pytest.fixture(autouse=True)
def _fixed_official_now(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)


def _bill_xml(
    *,
    year: int,
    quote_date: date | None = None,
    maturity_date: date | None = None,
    coupon_type: str = "Edm.Double",
) -> str:
    period = date(2025, 12, 30) if year == 2025 else COMMON_DATE
    quote_date = quote_date or period
    maturity_date = maturity_date or period + timedelta(days=91)
    updated = "2025-12-31T18:00:00Z" if year == 2025 else "2026-07-13T18:00:00Z"
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
      <title>DailyTreasuryBillRateData</title>
      <updated>{updated}</updated>
      <entry><content type="application/xml"><m:properties>
        <d:INDEX_DATE m:type="Edm.DateTime">{period.isoformat()}T00:00:00</d:INDEX_DATE>
        <d:QUOTE_DATE m:type="Edm.DateTime">{quote_date.isoformat()}T00:00:00</d:QUOTE_DATE>
        <d:MATURITY_DATE_13WK m:type="Edm.DateTime">{maturity_date.isoformat()}T00:00:00</d:MATURITY_DATE_13WK>
        <d:ROUND_B1_YIELD_13WK_2 m:type="{coupon_type}">3.80</d:ROUND_B1_YIELD_13WK_2>
        <d:ROUND_B1_CLOSE_13WK_2 m:type="Edm.Double">3.68</d:ROUND_B1_CLOSE_13WK_2>
        <d:CUSIP_13WK>912797AB1</d:CUSIP_13WK>
      </m:properties></content></entry>
    </feed>"""


def _treasury_client(payloads: dict[int, str], requests: list[int] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            "/resource-center/data-chart-center/interest-rates/pages/xml"
        )
        params = dict(request.url.params)
        assert params["data"] == "daily_treasury_bill_rates"
        year = int(params["field_tdr_date_value"])
        if requests is not None:
            requests.append(year)
        return httpx.Response(
            200,
            text=payloads[year],
            headers={"Content-Type": "application/atom+xml"},
        )

    return httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )


def test_treasury_13_week_provider_fetches_two_years_and_uses_coupon_equivalent():
    requests: list[int] = []
    provider = TreasuryRatesProvider(
        client=_treasury_client(
            {2025: _bill_xml(year=2025), 2026: _bill_xml(year=2026)},
            requests,
        )
    )

    result = provider.treasury_bill_rates_13w_coupon_equivalent(current_year=2026)

    assert result.ok
    assert requests == [2025, 2026]
    assert result.dataset == "treasury-bill-rates:13w-coupon-equivalent"
    assert result.metadata["requested_years"] == [2025, 2026]
    assert result.metadata["latest_value_date"] == COMMON_DATE.isoformat()
    assert len(result.metadata["artifacts"]) == 2
    assert {item["requested_year"] for item in result.metadata["artifacts"]} == {
        2025,
        2026,
    }
    assert [item["series_id"] for item in result.records] == [
        "UST-BILL-13W-COUPON-EQUIVALENT",
        "UST-BILL-13W-COUPON-EQUIVALENT",
    ]
    assert [item["value"] for item in result.records] == [
        Decimal("3.80"),
        Decimal("3.80"),
    ]
    latest = result.records[-1]
    assert latest["metadata"]["quote_convention"] == "13-week Coupon Equivalent"
    assert latest["metadata"]["treasury_field"] == "ROUND_B1_YIELD_13WK_2"
    assert latest["metadata"]["bank_discount_rate"] == "3.68"
    assert latest["metadata"]["bank_discount_field"] == "ROUND_B1_CLOSE_13WK_2"


@pytest.mark.parametrize(
    ("current_xml", "message"),
    [
        (
            _bill_xml(year=2026, quote_date=date(2026, 7, 9)),
            "INDEX_DATE and QUOTE_DATE mismatch",
        ),
        (
            _bill_xml(year=2026, coupon_type="Edm.Decimal"),
            "quotation field convention drift",
        ),
        (
            _bill_xml(year=2026, maturity_date=date(2026, 8, 1)),
            "13-week maturity convention drift",
        ),
    ],
)
def test_treasury_13_week_provider_rejects_quote_contract_drift(
    current_xml, message
):
    provider = TreasuryRatesProvider(
        client=_treasury_client(
            {2025: _bill_xml(year=2025), 2026: current_xml}
        )
    )

    result = provider.treasury_bill_rates_13w_coupon_equivalent(current_year=2026)

    assert not result.ok
    assert message in result.error


def _business_dates(start: date, end: date) -> list[date]:
    dates = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    return dates


def _rate_records(
    identity: str,
    dates: list[date],
    *,
    value: Decimal | None = None,
) -> list[dict]:
    specifications = {
        "sofr": ("SOFR", Decimal("3.55")),
        "iorb": ("IORB", Decimal("3.65")),
        "tbill": ("UST-BILL-13W-COUPON-EQUIVALENT", Decimal("3.80")),
    }
    series_id, default_value = specifications[identity]
    value = default_value if value is None else value
    records = []
    for period in dates:
        metadata = {}
        if identity == "tbill":
            metadata = {
                "quote_convention": "13-week Coupon Equivalent",
                "treasury_field": "ROUND_B1_YIELD_13WK_2",
                "bank_discount_field": "ROUND_B1_CLOSE_13WK_2",
                "bank_discount_rate": "3.68",
                "cusip": "912797AB1",
                "maturity_date": (period + timedelta(days=91)).isoformat(),
                "feed_updated_time": "2026-07-13T18:00:00+00:00",
            }
        records.append(
            {
                "series_id": series_id,
                "date": period.isoformat(),
                "value": value,
                "metadata": metadata,
            }
        )
    return records


def _record_custom_rate_runs(
    *,
    sofr_dates: list[date],
    tbill_dates: list[date],
    iorb_dates: list[date],
    cycle: str,
    fetched_at: datetime,
    values: dict[str, Decimal] | None = None,
):
    values = values or {}
    sofr = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="reference-rate:sofr",
            fetched_at=fetched_at,
            records=_rate_records(
                "sofr", sofr_dates, value=values.get("sofr")
            ),
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )
    tbill = record_provider_result(
        ProviderResult(
            provider="us-treasury-rates",
            dataset="treasury-bill-rates:13w-coupon-equivalent",
            fetched_at=fetched_at,
            records=_rate_records(
                "tbill", tbill_dates, value=values.get("tbill")
            ),
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )
    iorb = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="prates:iorb",
            fetched_at=fetched_at,
            records=_rate_records(
                "iorb", iorb_dates, value=values.get("iorb")
            ),
        ),
        persist=store_series_observations,
    )
    return {"sofr": sofr, "tbill": tbill, "iorb": iorb}


def _record_rate_runs():
    shared_dates = _business_dates(COMMON_DATE - timedelta(days=59), COMMON_DATE)
    return _record_custom_rate_runs(
        sofr_dates=shared_dates,
        tbill_dates=[*shared_dates, date(2026, 7, 13)],
        iorb_dates=shared_dates,
        fetched_at=datetime(2026, 7, 13, 20, 0, tzinfo=UTC),
        cycle="reserves-rate-spreads-fixture",
    )


def _rate_metric_rows(snapshot: DashboardSnapshot):
    return MetricSnapshot.objects.filter(
        key__startswith="reserves-rate-spreads-",
        batch_id=snapshot.batch_id,
    ).order_by("pk")


def _retained_publication_identity(snapshot: DashboardSnapshot) -> dict:
    return {
        "snapshot_pk": snapshot.pk,
        "snapshot_batch": snapshot.batch_id,
        "fingerprint": snapshot.data["fingerprint"],
        "metric_pks": tuple(_rate_metric_rows(snapshot).values_list("pk", flat=True)),
    }


def _assert_only_previous_publication_is_stale(
    snapshot: DashboardSnapshot,
    identity: dict,
    *,
    failure_fragment: str,
) -> None:
    snapshot.refresh_from_db()
    assert DashboardSnapshot.objects.filter(key="reserves-rate-spreads").count() == 1
    assert snapshot.pk == identity["snapshot_pk"]
    assert snapshot.batch_id == identity["snapshot_batch"]
    assert snapshot.data["fingerprint"] == identity["fingerprint"]
    assert snapshot.quality_status == Observation.Quality.STALE
    assert failure_fragment in str(snapshot.data["refresh_failure"])
    metric_pks = tuple(_rate_metric_rows(snapshot).values_list("pk", flat=True))
    assert metric_pks == identity["metric_pks"]
    assert len(metric_pks) == len(RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS)
    assert not MetricSnapshot.objects.filter(
        key__startswith="reserves-rate-spreads-"
    ).exclude(batch_id=snapshot.batch_id).exists()


@pytest.mark.django_db
def test_rate_spreads_coordinator_publishes_then_retains_stale_on_latest_failure():
    runs = _record_rate_runs()

    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard([runs["tbill"]])

    assert stale == set()
    assert [item.key for item in dashboards] == ["reserves-rate-spreads"]
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    data = snapshot.data
    assert data["contract_version"] == 1
    assert data["common_effective_date"] == COMMON_DATE.isoformat()
    assert data["history_start_date"] == (
        COMMON_DATE - timedelta(days=59)
    ).isoformat()
    assert data["history_row_count"] == len(
        _business_dates(COMMON_DATE - timedelta(days=59), COMMON_DATE)
    )
    assert data["no_forward_fill"] is True
    metrics = {item["key"]: item for item in data["metrics"]}
    assert set(metrics) == set(RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS)
    assert metrics["reserves-sofr"]["value"] == 3.55
    assert metrics["reserves-tbill-13w-coupon-equivalent"]["value"] == 3.8
    assert metrics["reserves-iorb"]["value"] == 3.65
    assert metrics["reserves-sofr-tbill-spread"]["value"] == -25.0
    assert metrics["reserves-sofr-iorb-spread"]["value"] == -10.0
    charts = {item["key"]: item for item in data["charts"]}
    assert set(charts) == set(RESERVES_RATE_SPREADS_REQUIRED_CHART_KEYS)
    assert len({tuple(row["date"] for row in chart["data"]) for chart in charts.values()}) == 1
    assert all(chart["data"][-1]["date"] == COMMON_DATE.isoformat() for chart in charts.values())

    failed = record_provider_result(
        ProviderResult.failure(
            "federal-reserve", "prates:iorb", "PRATES-LATEST-ATTEMPT-FAILED"
        )
    )
    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard([failed])

    assert dashboards == []
    assert stale == {"reserves-rate-spreads"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.STALE
    assert "PRATES-LATEST-ATTEMPT-FAILED" in str(snapshot.data["refresh_failure"])
    assert select_public_reserves_rate_spreads_snapshot([snapshot]) == snapshot


@pytest.mark.django_db
def test_rate_spreads_strict_rebuild_rejects_exact_observation_tamper_when_expired(
    monkeypatch,
):
    runs = _record_rate_runs()
    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [runs["tbill"]]
    )
    assert stale == set()
    snapshot = dashboards[0]
    assert _reserves_rate_spreads_snapshot_contract_is_valid(
        snapshot,
        selected_runs=runs,
    )

    expired_now = FIXED_NOW + timedelta(days=10)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: expired_now
    )
    assert not _reserves_rate_spreads_snapshot_contract_is_valid(
        snapshot,
        selected_runs=runs,
    )
    assert _reserves_rate_spreads_snapshot_contract_is_valid(
        snapshot,
        selected_runs=runs,
        allow_expired=True,
    )

    sofr_observation = Observation.objects.filter(
        series__key="sofr",
        batch_id=runs["sofr"].batch_id,
    ).latest("value_date", "id")
    original_value = sofr_observation.value
    sofr_observation.value = original_value + Decimal("0.01")
    sofr_observation.save(update_fields=["value", "updated_at"])
    assert not _reserves_rate_spreads_snapshot_contract_is_valid(
        snapshot,
        selected_runs=runs,
        allow_expired=True,
    )

    sofr_observation.value = original_value
    sofr_observation.fetched_at = expired_now + timedelta(minutes=1)
    sofr_observation.save(
        update_fields=["value", "fetched_at", "updated_at"]
    )
    assert not _reserves_rate_spreads_snapshot_contract_is_valid(
        snapshot,
        selected_runs=runs,
        allow_expired=True,
    )


@pytest.mark.django_db
def test_rate_spreads_public_selector_rechecks_current_source_licence():
    runs = _record_rate_runs()
    _coordinate_reserves_rate_spreads_dashboard([runs["tbill"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    licence = SourceLicense.objects.get(
        source__key="us-treasury-rates",
        is_current=True,
    )
    licence.public_display_allowed = False
    licence.derived_display_allowed = False
    licence.reviewed_by = "licence-review"
    licence.reviewed_at = FIXED_NOW
    licence.save(
        update_fields=[
            "public_display_allowed",
            "derived_display_allowed",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )

    assert select_public_reserves_rate_spreads_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_rate_spreads_rejects_regressed_common_date_and_retains_previous(
    monkeypatch,
):
    first_now = datetime(2026, 7, 12, 13, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: first_now
    )
    initial_dates = _business_dates(
        COMMON_DATE - timedelta(days=59), COMMON_DATE
    )
    initial = _record_custom_rate_runs(
        sofr_dates=initial_dates,
        tbill_dates=initial_dates,
        iorb_dates=initial_dates,
        cycle="initial-common-date",
        fetched_at=datetime(2026, 7, 11, 20, 0, tzinfo=UTC),
    )
    _coordinate_reserves_rate_spreads_dashboard([initial["tbill"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    identity = _retained_publication_identity(snapshot)

    regressed_date = COMMON_DATE - timedelta(days=1)
    regressed_dates = _business_dates(
        regressed_date - timedelta(days=59), regressed_date
    )
    second_now = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: second_now
    )
    regressed = _record_custom_rate_runs(
        sofr_dates=regressed_dates,
        tbill_dates=regressed_dates,
        iorb_dates=regressed_dates,
        cycle="regressed-common-date",
        fetched_at=datetime(2026, 7, 12, 20, 0, tzinfo=UTC),
    )

    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [regressed["tbill"]]
    )

    assert dashboards == []
    assert stale == {"reserves-rate-spreads"}
    _assert_only_previous_publication_is_stale(
        snapshot,
        identity,
        failure_fragment="regressed behind the published v1 component",
    )
    assert snapshot.data["common_effective_date"] == COMMON_DATE.isoformat()


@pytest.mark.django_db
def test_rate_spreads_duplicate_is_idempotent_and_same_value_recovery_appends():
    initial = _record_rate_runs()
    _coordinate_reserves_rate_spreads_dashboard([initial["tbill"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    identity = _retained_publication_identity(snapshot)

    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [initial["tbill"], initial["tbill"]]
    )

    assert dashboards == []
    assert stale == set()
    snapshot.refresh_from_db()
    assert _retained_publication_identity(snapshot) == identity

    failed = record_provider_result(
        ProviderResult.failure(
            "federal-reserve", "prates:iorb", "temporary recovery fixture"
        )
    )
    _coordinate_reserves_rate_spreads_dashboard([failed])
    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.STALE

    recovered = _record_rate_runs()
    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [recovered["tbill"]]
    )

    assert len(dashboards) == 1
    assert stale == set()
    recovered_snapshot = dashboards[0]
    snapshot.refresh_from_db()
    assert DashboardSnapshot.objects.filter(key="reserves-rate-spreads").count() == 2
    assert snapshot.pk == identity["snapshot_pk"]
    assert snapshot.batch_id == identity["snapshot_batch"]
    assert snapshot.quality_status == Observation.Quality.STALE
    assert "refresh_failure" in snapshot.data
    assert recovered_snapshot.pk != identity["snapshot_pk"]
    assert recovered_snapshot.batch_id != identity["snapshot_batch"]
    assert recovered_snapshot.data["fingerprint"] == identity["fingerprint"]
    assert recovered_snapshot.quality_status == Observation.Quality.ESTIMATED
    assert "refresh_failure" not in recovered_snapshot.data
    recovered_batches = {str(run.batch_id) for run in recovered.values()}
    assert set(recovered_snapshot.data["component_batches"]) == recovered_batches
    assert {
        item["batch_id"] for item in recovered_snapshot.data["input_datasets"]
    } == recovered_batches
    metric_rows = list(_rate_metric_rows(recovered_snapshot))
    assert len(metric_rows) == len(RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS)
    assert {item.pk for item in metric_rows} != set(identity["metric_pks"])
    assert {item.batch_id for item in metric_rows} == {recovered_snapshot.batch_id}
    assert set().union(
        *(set(item.metadata["input_batch_ids"]) for item in metric_rows)
    ) == recovered_batches
    assert MetricSnapshot.objects.filter(
        key__startswith="reserves-rate-spreads-"
    ).exclude(batch_id=recovered_snapshot.batch_id).exists()


@pytest.mark.django_db
def test_rate_spreads_rejects_mixed_batch_and_retains_previous():
    initial = _record_rate_runs()
    _coordinate_reserves_rate_spreads_dashboard([initial["tbill"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    identity = _retained_publication_identity(snapshot)

    mixed = _record_rate_runs()
    target_observation = Observation.objects.filter(
        source=mixed["sofr"].source,
        series__key="sofr",
        batch_id=mixed["sofr"].batch_id,
    ).first()
    assert target_observation is not None
    unexpected_series = SeriesDefinition.objects.create(
        key="unexpected-ny-fed-mixed-batch-series",
        name="Unexpected NY Fed mixed-batch series",
        unit="%",
        frequency="daily",
        source=mixed["sofr"].source,
    )
    Observation.objects.create(
        series=unexpected_series,
        instrument=None,
        value=Decimal("3.50"),
        value_date=target_observation.value_date,
        as_of=target_observation.as_of,
        fetched_at=target_observation.fetched_at,
        batch_id=mixed["sofr"].batch_id,
        source=mixed["sofr"].source,
        fallback_source=None,
        quality_status=Observation.Quality.FRESH,
        metadata={"fixture": "unexpected non-target series in exact run batch"},
    )
    assert Observation.objects.filter(
        source=mixed["sofr"].source,
        series__key="sofr",
        batch_id=mixed["sofr"].batch_id,
    ).count() == mixed["sofr"].row_count
    assert Observation.objects.filter(
        source=mixed["sofr"].source,
        batch_id=mixed["sofr"].batch_id,
    ).count() == mixed["sofr"].row_count + 1

    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [mixed["sofr"]]
    )

    assert dashboards == []
    assert stale == {"reserves-rate-spreads"}
    _assert_only_previous_publication_is_stale(
        snapshot,
        identity,
        failure_fragment="mixed-batch",
    )


@pytest.mark.django_db
def test_rate_spreads_rejects_intersection_below_thirty_and_retains_previous():
    initial = _record_rate_runs()
    _coordinate_reserves_rate_spreads_dashboard([initial["tbill"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    identity = _retained_publication_identity(snapshot)

    shared_dates = _business_dates(
        COMMON_DATE - timedelta(days=59), COMMON_DATE
    )
    insufficient = _record_custom_rate_runs(
        sofr_dates=shared_dates,
        tbill_dates=[*shared_dates, date(2026, 7, 13)],
        iorb_dates=shared_dates[-29:],
        cycle="insufficient-common-sample",
        fetched_at=datetime(2026, 7, 13, 21, 0, tzinfo=UTC),
    )

    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [insufficient["tbill"]]
    )

    assert dashboards == []
    assert stale == {"reserves-rate-spreads"}
    _assert_only_previous_publication_is_stale(
        snapshot,
        identity,
        failure_fragment="insufficient-sample",
    )


@pytest.mark.django_db
def test_rate_spreads_publication_postcondition_failure_rolls_back_new_rows(
    monkeypatch,
):
    initial = _record_rate_runs()
    _coordinate_reserves_rate_spreads_dashboard([initial["tbill"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    identity = _retained_publication_identity(snapshot)

    shared_dates = _business_dates(
        COMMON_DATE - timedelta(days=59), COMMON_DATE
    )
    changed = _record_custom_rate_runs(
        sofr_dates=shared_dates,
        tbill_dates=[*shared_dates, date(2026, 7, 13)],
        iorb_dates=shared_dates,
        cycle="changed-values-before-postcondition",
        fetched_at=datetime(2026, 7, 13, 21, 0, tzinfo=UTC),
        values={"sofr": Decimal("3.56")},
    )
    attempted: dict = {}

    def reject_new_publication(candidate, **_kwargs):
        attempted.update(
            {
                "snapshot_pk": candidate.pk,
                "batch_id": candidate.batch_id,
                "fingerprint": candidate.data["fingerprint"],
                "metric_count": _rate_metric_rows(candidate).count(),
            }
        )
        return False

    monkeypatch.setattr(
        "research.official_data._reserves_rate_spreads_snapshot_contract_is_valid",
        reject_new_publication,
    )

    dashboards, stale = _coordinate_reserves_rate_spreads_dashboard(
        [changed["tbill"]]
    )

    assert dashboards == []
    assert stale == {"reserves-rate-spreads"}
    assert attempted["snapshot_pk"] != identity["snapshot_pk"]
    assert attempted["batch_id"] != identity["snapshot_batch"]
    assert attempted["fingerprint"] != identity["fingerprint"]
    assert attempted["metric_count"] == len(
        RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS
    )
    assert not DashboardSnapshot.objects.filter(
        pk=attempted["snapshot_pk"]
    ).exists()
    assert not MetricSnapshot.objects.filter(
        key__startswith="reserves-rate-spreads-",
        batch_id=attempted["batch_id"],
    ).exists()
    _assert_only_previous_publication_is_stale(
        snapshot,
        identity,
        failure_fragment="publication postcondition",
    )


def _weekly_records(series_id: str, *, count: int = 60) -> list[dict]:
    start = LATEST_WEDNESDAY - timedelta(weeks=count - 1)
    rows = []
    for index in range(count):
        period = start + timedelta(weeks=index)
        assets = Decimal("24000000") + Decimal(index * 32000)
        if series_id == "WRBWFRBL":
            value = Decimal("3000000") + Decimal(index * index * 137) + Decimal(
                index * 4100
            )
            board_id = "RESH4R_N.WW"
        else:
            value = assets
            board_id = "B1151NCBA"
        rows.append(
            {
                "series_id": series_id,
                "source_series_id": board_id,
                "date": period.isoformat(),
                "value": value,
                "metadata": {
                    "board_series_id": board_id,
                    "raw_value": str(value),
                    "unit_multiplier": "1000000",
                    "currency": "USD",
                },
            }
        )
    return rows


def _publish_weekly_reserves() -> DashboardSnapshot:
    fetched_at = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    h41 = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="h41",
            fetched_at=fetched_at,
            records=_weekly_records("WRBWFRBL"),
            metadata={
                "reserves_refresh_id": "h41-rate-spread-fixture",
                "prepared_at": "2026-07-10T19:00:00+00:00",
            },
        ),
        persist=_store_h41_observations,
    )
    h8 = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="h8",
            fetched_at=fetched_at,
            records=_weekly_records("H8-B1151NCBA"),
            metadata={
                "reserves_refresh_id": "h8-rate-spread-fixture",
                "prepared_at": "2026-07-10T19:30:00+00:00",
            },
        ),
        persist=_store_h8_observations,
    )
    assert h41.status == "success"
    assert h8.status == "success"
    dashboards, stale = _coordinate_reserves_dashboard([h8])
    assert stale == set()
    assert [item.key for item in dashboards] == ["reserves"]
    return DashboardSnapshot.objects.get(key="reserves")


def _downgrade_to_legacy_weekly_v1(snapshot: DashboardSnapshot) -> DashboardSnapshot:
    data = deepcopy(snapshot.data)
    data["sections"] = [
        item
        for item in data.get("sections", [])
        if item.get("key") != "recent-reserve-balances"
    ]
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])
    return snapshot


@pytest.mark.django_db
def test_weekly_legacy_v1_without_recent_section_remains_publicly_compatible():
    snapshot = _downgrade_to_legacy_weekly_v1(_publish_weekly_reserves())

    assert all(
        item.get("key") != "recent-reserve-balances"
        for item in snapshot.data["sections"]
    )
    assert select_public_reserves_snapshot([snapshot]) == snapshot


@pytest.mark.django_db
def test_reserves_funding_view_keeps_empty_daily_charts_when_component_missing(client):
    _downgrade_to_legacy_weekly_v1(_publish_weekly_reserves())

    response = client.get("/liquidity/reserves/?tab=funding")

    assert response.status_code == 200
    assert response.context["selected_tab"] == "funding"
    charts = response.context["charts"]
    assert {item["key"] for item in charts} == set(
        RESERVES_RATE_SPREADS_REQUIRED_CHART_KEYS
    )
    assert all(item["data"] == [] for item in charts)
    assert all(item["quality_status"] == Observation.Quality.STALE for item in charts)
    assert "日频资金利差组件缺失" in response.content.decode()


@pytest.mark.django_db
def test_reserves_funding_view_propagates_stale_component_status(client):
    _publish_weekly_reserves()
    runs = _record_rate_runs()
    _coordinate_reserves_rate_spreads_dashboard([runs["tbill"]])
    rate_snapshot = DashboardSnapshot.objects.get(key="reserves-rate-spreads")
    rate_data = deepcopy(rate_snapshot.data)
    rate_data["refresh_failure"] = {
        "reason": "latest IORB refresh failed",
        "checked_at": FIXED_NOW.isoformat(),
    }
    rate_snapshot.data = rate_data
    rate_snapshot.quality_status = Observation.Quality.STALE
    rate_snapshot.save(update_fields=["data", "quality_status", "updated_at"])

    response = client.get("/liquidity/reserves/?tab=funding")

    assert response.status_code == 200
    assert response.context["selected_tab"] == "funding"
    charts = response.context["charts"]
    assert {item["key"] for item in charts} == set(
        RESERVES_RATE_SPREADS_REQUIRED_CHART_KEYS
    )
    assert all(item["quality_status"] == Observation.Quality.STALE for item in charts)
    rate_metric_keys = set(RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS)
    rate_metrics = [
        item
        for item in response.context["metrics"]
        if item.get("key") in rate_metric_keys
    ]
    assert len(rate_metrics) == len(rate_metric_keys)
    assert all(
        item["quality_status"] == Observation.Quality.STALE
        for item in rate_metrics
    )
    assert all(
        item["metadata"]["upstream_quality_status"]
        in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
        for item in rate_metrics
    )
