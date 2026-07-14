from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from research.models import DashboardSnapshot, Observation, SourceLicense
from research.official_data import (
    RESERVES_RATE_SPREADS_REQUIRED_CHART_KEYS,
    RESERVES_RATE_SPREADS_REQUIRED_METRIC_KEYS,
    _coordinate_reserves_dashboard,
    _coordinate_reserves_rate_spreads_dashboard,
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


def _rate_records(identity: str, dates: list[date]) -> list[dict]:
    specifications = {
        "sofr": ("SOFR", Decimal("3.55")),
        "iorb": ("IORB", Decimal("3.65")),
        "tbill": ("UST-BILL-13W-COUPON-EQUIVALENT", Decimal("3.80")),
    }
    series_id, value = specifications[identity]
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


def _record_rate_runs():
    shared_dates = _business_dates(COMMON_DATE - timedelta(days=59), COMMON_DATE)
    treasury_dates = [*shared_dates, date(2026, 7, 13)]
    fetched_at = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    cycle = "reserves-rate-spreads-fixture"
    sofr = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="reference-rate:sofr",
            fetched_at=fetched_at,
            records=_rate_records("sofr", shared_dates),
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )
    tbill = record_provider_result(
        ProviderResult(
            provider="us-treasury-rates",
            dataset="treasury-bill-rates:13w-coupon-equivalent",
            fetched_at=fetched_at,
            records=_rate_records("tbill", treasury_dates),
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )
    iorb = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="prates:iorb",
            fetched_at=fetched_at,
            records=_rate_records("iorb", shared_dates),
        ),
        persist=store_series_observations,
    )
    return {"sofr": sofr, "tbill": tbill, "iorb": iorb}


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
