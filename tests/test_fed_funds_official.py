from __future__ import annotations

import html
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from research.data_catalog import DATA_REQUIREMENTS
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Source,
    SourceLicense,
)
from research.official_data import (
    FED_FUNDS_DATASETS,
    FED_FUNDS_REQUIRED_METRIC_KEYS,
    _coordinate_fed_funds_dashboard,
    _derived_metric,
    _fed_funds_page_data,
    _fed_funds_page_is_buildable,
    publish_official_dashboards,
    refresh_official_data,
    refresh_prates_data,
)
from research.providers import NYFedMarketsProvider, ProviderResult
from research.services import record_provider_result, store_series_observations

LATEST_MARKET_DATE = date(2026, 7, 9)
FIXED_NOW = datetime(2026, 7, 12, 16, 30, tzinfo=UTC)


def _month(start: date, offset: int) -> date:
    month_index = start.year * 12 + start.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, start.day)


def _json_metadata(values: dict) -> dict:
    return {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in values.items()
    }


def _sofr_metadata(rate: Decimal, *, exact_latest: bool = False) -> dict:
    if exact_latest:
        return {
            "percentPercentile1": Decimal("3.50"),
            "percentPercentile25": Decimal("3.51"),
            "percentPercentile75": Decimal("3.57"),
            "percentPercentile99": Decimal("3.65"),
            "volumeInBillions": Decimal("3126"),
            "revisionIndicator": "",
            "footnoteId": None,
        }
    return {
        "percentPercentile1": rate - Decimal("0.04"),
        "percentPercentile25": rate - Decimal("0.02"),
        "percentPercentile75": rate + Decimal("0.02"),
        "percentPercentile99": rate + Decimal("0.08"),
        "volumeInBillions": Decimal("2000"),
        "revisionIndicator": "",
        "footnoteId": None,
    }


def _effr_metadata(rate: Decimal, *, exact_latest: bool = False) -> dict:
    if exact_latest:
        return {
            "targetRateFrom": Decimal("3.50"),
            "targetRateTo": Decimal("3.75"),
            "percentPercentile1": Decimal("3.60"),
            "percentPercentile25": Decimal("3.62"),
            "percentPercentile75": Decimal("3.63"),
            "percentPercentile99": Decimal("3.64"),
            "volumeInBillions": Decimal("126"),
            "revisionIndicator": "",
            "footnoteId": None,
        }
    return {
        "targetRateFrom": Decimal("3.50"),
        "targetRateTo": Decimal("3.75"),
        "percentPercentile1": rate - Decimal("0.03"),
        "percentPercentile25": rate - Decimal("0.01"),
        "percentPercentile75": rate + Decimal("0.01"),
        "percentPercentile99": rate + Decimal("0.03"),
        "volumeInBillions": Decimal("120"),
        "revisionIndicator": "",
        "footnoteId": None,
    }


def _market_records(
    series_id: str,
    *,
    history_months: int = 0,
    latest_metadata_updates: dict | None = None,
) -> list[dict]:
    latest_metadata_updates = latest_metadata_updates or {}
    if history_months:
        periods = [
            _month(date(2023, 4, 9), index)
            for index in range(history_months)
        ]
        periods = [period for period in periods if period <= LATEST_MARKET_DATE]
        if periods[-1] != LATEST_MARKET_DATE:
            periods.append(LATEST_MARKET_DATE)
    else:
        periods = [date(2026, 7, 8), LATEST_MARKET_DATE]
    if series_id == "EFFR":
        periods.append(date(2026, 7, 3))
    periods = sorted(set(periods))
    records = []
    for index, period in enumerate(periods):
        if period == LATEST_MARKET_DATE:
            rate = Decimal("3.53") if series_id == "SOFR" else Decimal("3.62")
            metadata = (
                _sofr_metadata(rate, exact_latest=True)
                if series_id == "SOFR"
                else _effr_metadata(rate, exact_latest=True)
            )
            metadata.update(latest_metadata_updates)
        else:
            rate = (
                Decimal("3.58")
                if series_id == "SOFR"
                else Decimal("3.62")
            )
            metadata = (
                _sofr_metadata(rate)
                if series_id == "SOFR"
                else _effr_metadata(rate)
            )
        records.append(
            {
                "series_id": series_id,
                "date": period.isoformat(),
                "value": rate,
                "metadata": _json_metadata(metadata),
            }
        )
    return records


def _iorb_records(
    *,
    future_value: Decimal = Decimal("3.40"),
    omit_latest_market: bool = False,
    history_periods: list[date] | None = None,
) -> list[dict]:
    periods = set(history_periods or [])
    periods.update(
        {
            date(2026, 7, 8),
            date(2026, 7, 10),
            date(2026, 7, 11),
            date(2026, 7, 12),
            date(2026, 7, 13),
        }
    )
    if omit_latest_market:
        periods.discard(LATEST_MARKET_DATE)
    else:
        periods.add(LATEST_MARKET_DATE)
    return [
        {
            "series_id": "IORB",
            "date": period.isoformat(),
            "value": (
                future_value
                if period == date(2026, 7, 13)
                else Decimal("3.65")
            ),
            "metadata": {
                "board_series_id": "RESBM_N.D",
                "prates_status": "A",
            },
        }
        for period in sorted(periods)
    ]


def _record_market_run(
    series_id: str,
    *,
    cycle: str,
    records: list[dict] | None = None,
    fetched_at: datetime = datetime(2026, 7, 10, 12, tzinfo=UTC),
):
    return record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset=f"reference-rate:{series_id.lower()}",
            fetched_at=fetched_at,
            records=(
                records if records is not None else _market_records(series_id)
            ),
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )


def _record_iorb_run(
    *,
    records: list[dict] | None = None,
    fetched_at: datetime = datetime(2026, 7, 10, 13, tzinfo=UTC),
):
    return record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="prates:iorb",
            fetched_at=fetched_at,
            records=records if records is not None else _iorb_records(),
            metadata={"quality_status": "complete"},
        ),
        persist=store_series_observations,
    )


def _fed_funds_runs(
    *,
    cycle: str | None = None,
    effr_cycle: str | None = None,
    future_iorb: Decimal = Decimal("3.40"),
    omit_latest_iorb: bool = False,
    latest_effr_metadata_updates: dict | None = None,
    latest_sofr_metadata_updates: dict | None = None,
    history_months: int = 40,
    fetched_at: datetime = datetime(2026, 7, 10, 12, tzinfo=UTC),
):
    cycle = cycle or str(uuid.uuid4())
    sofr_records = _market_records(
        "SOFR",
        history_months=history_months,
        latest_metadata_updates=latest_sofr_metadata_updates,
    )
    effr_records = _market_records(
        "EFFR",
        history_months=history_months,
        latest_metadata_updates=latest_effr_metadata_updates,
    )
    history_periods = [date.fromisoformat(item["date"]) for item in sofr_records]
    runs = {
        "sofr": _record_market_run(
            "SOFR", cycle=cycle, records=sofr_records, fetched_at=fetched_at
        ),
        "effr": _record_market_run(
            "EFFR",
            cycle=effr_cycle or cycle,
            records=effr_records,
            fetched_at=fetched_at,
        ),
        "iorb": _record_iorb_run(
            records=_iorb_records(
                future_value=future_iorb,
                omit_latest_market=omit_latest_iorb,
                history_periods=history_periods,
            ),
            fetched_at=fetched_at,
        ),
    }
    return runs


def _batches(runs) -> dict[str, uuid.UUID]:
    return {key: run.batch_id for key, run in runs.items()}


def _assert_successful_runs(runs) -> None:
    assert set(runs) == {"sofr", "effr", "iorb"}
    assert all(run.status == "success" for run in runs.values())
    assert all(run.row_count > 0 for run in runs.values())


def test_ny_fed_reference_rate_clamps_to_supported_limit_and_keeps_metadata():
    def handler(request):
        assert request.url.path.endswith(
            "/api/rates/secured/sofr/last/800.json"
        )
        return httpx.Response(
            200,
            json={
                "refRates": [
                    {
                        "effectiveDate": "2026-07-09",
                        "percentRate": 3.53,
                        "percentPercentile1": 3.50,
                        "percentPercentile25": 3.51,
                        "percentPercentile75": 3.57,
                        "percentPercentile99": 3.65,
                        "volumeInBillions": 3126,
                        "revisionIndicator": "",
                        "footnoteId": "SOFR-NORMAL",
                    }
                ]
            },
        )

    client = httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    provider = NYFedMarketsProvider(client=client)
    result = provider.sofr(limit=1000)

    assert result.ok
    assert result.metadata["endpoint"].endswith("/last/800.json")
    assert result.metadata["attribution"]
    assert result.metadata["terms_url"].startswith("https://")
    metadata = result.records[0]["metadata"]
    assert metadata == {
        "percentPercentile1": 3.5,
        "percentPercentile25": 3.51,
        "percentPercentile75": 3.57,
        "percentPercentile99": 3.65,
        "volumeInBillions": 3126,
        "revisionIndicator": "",
        "footnoteId": "SOFR-NORMAL",
    }


def test_ny_fed_effr_preserves_target_range_and_distribution_fields():
    client = httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "refRates": [
                        {
                            "effectiveDate": "2026-07-09",
                            "percentRate": 3.62,
                            "targetRateFrom": 3.50,
                            "targetRateTo": 3.75,
                            "percentPercentile1": 3.60,
                            "percentPercentile25": 3.62,
                            "percentPercentile75": 3.63,
                            "percentPercentile99": 3.64,
                            "volumeInBillions": 126,
                            "revisionIndicator": "",
                            "footnoteId": "EFFR-NORMAL",
                        }
                    ]
                },
            )
        ),
    )

    result = NYFedMarketsProvider(client=client).effr(limit=800)

    assert result.ok
    assert result.records[0]["metadata"] == {
        "percentPercentile1": 3.6,
        "percentPercentile25": 3.62,
        "percentPercentile75": 3.63,
        "percentPercentile99": 3.64,
        "targetRateFrom": 3.5,
        "targetRateTo": 3.75,
        "volumeInBillions": 126,
        "revisionIndicator": "",
        "footnoteId": "EFFR-NORMAL",
    }


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"refRates": None}, {"refRates": [None]}],
)
def test_ny_fed_reference_rate_schema_drift_returns_failure(payload):
    client = httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )

    result = NYFedMarketsProvider(client=client).sofr(limit=800)

    assert not result.ok
    assert result.error
    if payload is not None:
        assert "invalid reference-rate" in result.error


def test_ny_fed_reference_rate_rejects_one_invalid_latest_record():
    payload = {
        "refRates": [
            {
                "effectiveDate": "2026-07-08",
                "percentRate": 3.58,
            },
            {
                "effectiveDate": "2026-07-09",
                "percentRate": None,
            },
        ]
    }
    client = httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )

    result = NYFedMarketsProvider(client=client).sofr(limit=800)

    assert not result.ok
    assert "missing effectiveDate or percentRate" in result.error


@pytest.mark.django_db
def test_fed_funds_uses_latest_nonfuture_three_way_common_date(monkeypatch):
    runs = _fed_funds_runs(future_iorb=Decimal("3.40"))
    _assert_successful_runs(runs)
    batches = _batches(runs)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )

    assert publish_official_dashboards(keys={"fed-funds"}) == []
    assert _fed_funds_page_is_buildable(dataset_batches=batches)
    snapshot = publish_official_dashboards(
        keys={"fed-funds"}, dataset_batches=batches
    )[0]
    metrics = {item["key"]: item for item in snapshot.data["metrics"]}

    assert set(metrics) == set(FED_FUNDS_REQUIRED_METRIC_KEYS)
    assert metrics["effr"]["display_value"] == "3.62%"
    assert metrics["sofr"]["display_value"] == "3.53%"
    assert metrics["iorb"]["display_value"] == "3.65%"
    assert metrics["target-lower"]["display_value"] == "3.50%"
    assert metrics["target-upper"]["display_value"] == "3.75%"
    assert metrics["sofr-effr"]["display_value"] == "-9bp"
    assert metrics["sofr-iorb"]["display_value"] == "-12bp"
    assert metrics["effr-iorb"]["display_value"] == "-3bp"
    assert metrics["effr-p1-p99-width"]["display_value"] == "+4bp"
    assert metrics["sofr-p1-p99-width"]["display_value"] == "+15bp"
    assert metrics["effr-corridor-position"]["display_value"] == "48.0%"
    assert metrics["effr-volume"]["display_value"] == "126 USD bn"
    assert metrics["sofr-volume"]["display_value"] == "3,126 USD bn"
    assert {item["value_date"] for item in metrics.values()} == {
        "2026-07-09T00:00:00+00:00"
    }
    for metric in metrics.values():
        assert metric["metadata"]["common_effective_date"] == "2026-07-09"
        assert set(metric["metadata"]["input_value_dates"]) == {
            "2026-07-09T00:00:00+00:00"
        }
        assert metric["metadata"]["input_lineage"]
        assert all(
            item["license_scope"]
            for item in metric["metadata"]["input_lineage"]
        )
    iorb_lineage = metrics["iorb"]["metadata"]["input_lineage"]
    assert iorb_lineage[0]["value_date"].startswith("2026-07-09")
    assert metrics["sofr-iorb"]["metadata"]["input_batch_ids"] == sorted(
        [str(runs["sofr"].batch_id), str(runs["iorb"].batch_id)]
    )
    assert set(snapshot.data["component_batches"]) == {
        str(run.batch_id) for run in runs.values()
    }
    assert set(snapshot.data["source_keys"]) == {
        "federal-reserve",
        "internal",
        "ny-fed-markets",
    }
    assert any(
        "not affiliated with the New York Fed" in notice
        for notice in snapshot.data["required_notices"]
    )
    assert {item["key"] for item in snapshot.data["charts"]} == {
        "policy-corridor",
        "effr-distribution",
        "sofr-distribution",
    }
    for chart in snapshot.data["charts"]:
        assert chart["frequency"] == "daily"
        assert chart["time_axis"] == "date"
        assert max(row["date"] for row in chart["data"]) == "2026-07-09"
        assert not any(row["date"] == "2026-07-03" for row in chart["data"])
    stored = MetricSnapshot.objects.get(
        key="fed-funds-sofr-iorb", batch_id=snapshot.batch_id
    )
    assert stored.value_date.date() == LATEST_MARKET_DATE
    assert stored.metadata["common_effective_date"] == "2026-07-09"
    assert stored.metadata["input_batch_ids"] == sorted(
        [str(runs["sofr"].batch_id), str(runs["iorb"].batch_id)]
    )

    aligned_global = _derived_metric(
        "global-sofr-iorb",
        "SOFR−IORB",
        "SOFR",
        "IORB",
        basis_points=True,
    )
    assert aligned_global["display_value"] == "-12bp"
    assert aligned_global["value_date"].startswith("2026-07-09")


@pytest.mark.django_db
def test_other_policy_pages_align_direct_cards_and_history(monkeypatch):
    runs = _fed_funds_runs(future_iorb=Decimal("3.40"))
    _assert_successful_runs(runs)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )

    dashboards = publish_official_dashboards(
        keys={"transmission-chain", "subsurface"}
    )
    pages = {item.key: item.data for item in dashboards}

    metrics = {
        item["key"]: item for item in pages["transmission-chain"]["metrics"]
    }
    assert metrics["sofr"]["value_date"].startswith("2026-07-09")
    assert metrics["iorb"]["value_date"].startswith("2026-07-09")
    assert metrics["iorb"]["display_value"] == "3.65%"
    transmission_rows = pages["transmission-chain"]["charts"][0]["data"]
    assert max(row["date"] for row in transmission_rows) == "2026-07-09"
    assert all({"SOFR", "EFFR", "IORB"} <= set(row) for row in transmission_rows)
    subsurface = {
        item["key"]: item for item in pages["subsurface"]["metrics"]
    }
    assert subsurface["iorb"]["value_date"].startswith("2026-07-09")
    assert subsurface["sofr-iorb"]["display_value"] == "-12bp"


@pytest.mark.django_db
def test_fed_funds_excludes_utc_next_day_while_new_york_is_prior_day(monkeypatch):
    market_records = {}
    for series_id, rate in (
        ("SOFR", Decimal("3.53")),
        ("EFFR", Decimal("3.62")),
    ):
        records = _market_records(series_id, history_months=40)
        metadata_factory = (
            _sofr_metadata if series_id == "SOFR" else _effr_metadata
        )
        for period in (date(2026, 7, 10), date(2026, 7, 13)):
            records.append(
                {
                    "series_id": series_id,
                    "date": period.isoformat(),
                    "value": rate,
                    "metadata": _json_metadata(metadata_factory(rate)),
                }
            )
        market_records[series_id] = records
    history_periods = [
        date.fromisoformat(item["date"])
        for item in market_records["SOFR"]
    ]
    runs = {
        "sofr": _record_market_run(
            "SOFR", cycle="et-boundary", records=market_records["SOFR"]
        ),
        "effr": _record_market_run(
            "EFFR", cycle="et-boundary", records=market_records["EFFR"]
        ),
        "iorb": _record_iorb_run(
            records=_iorb_records(history_periods=history_periods)
        ),
    }
    _assert_successful_runs(runs)
    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: datetime(2026, 7, 13, 2, tzinfo=UTC),
    )

    metrics, charts, _ = _fed_funds_page_data(
        dataset_batches=_batches(runs)
    )

    assert {item["value_date"] for item in metrics} == {
        "2026-07-10T00:00:00+00:00"
    }
    assert all(
        max(row["date"] for row in chart["data"]) == "2026-07-10"
        for chart in charts
    )


@pytest.mark.django_db
def test_fed_funds_requires_full_three_year_history(monkeypatch):
    runs = _fed_funds_runs(history_months=0)
    _assert_successful_runs(runs)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )

    assert not _fed_funds_page_is_buildable(dataset_batches=_batches(runs))


@pytest.mark.parametrize(
    ("effr_updates", "sofr_updates"),
    [
        ({"targetRateFrom": None}, {}),
        ({"targetRateTo": Decimal("3.50")}, {}),
        ({"volumeInBillions": Decimal("-1")}, {}),
        ({"percentPercentile25": Decimal("3.70")}, {}),
        ({}, {"percentPercentile75": Decimal("3.40")}),
    ],
)
@pytest.mark.django_db
def test_fed_funds_invalid_latest_metadata_blocks_publication(
    effr_updates, sofr_updates, monkeypatch
):
    runs = _fed_funds_runs(
        latest_effr_metadata_updates=effr_updates,
        latest_sofr_metadata_updates=sofr_updates,
    )
    _assert_successful_runs(runs)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )

    assert not _fed_funds_page_is_buildable(dataset_batches=_batches(runs))
    assert (
        publish_official_dashboards(
            keys={"fed-funds"}, dataset_batches=_batches(runs)
        )
        == []
    )


@pytest.mark.django_db
def test_fed_funds_latest_market_date_missing_iorb_does_not_fallback(monkeypatch):
    runs = _fed_funds_runs(omit_latest_iorb=True)
    _assert_successful_runs(runs)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )

    assert not _fed_funds_page_is_buildable(dataset_batches=_batches(runs))
    assert (
        publish_official_dashboards(
            keys={"fed-funds"}, dataset_batches=_batches(runs)
        )
        == []
    )


@pytest.mark.django_db
def test_fed_funds_coordinator_rejects_cycle_mismatch_and_unrelated_runs(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    mismatched = _fed_funds_runs(
        cycle="ny-cycle-one", effr_cycle="ny-cycle-two"
    )
    dashboards, stale = _coordinate_fed_funds_dashboard(mismatched.values())
    assert dashboards == []
    assert stale == {"fed-funds"}
    assert DashboardSnapshot.objects.filter(key="fed-funds").count() == 0

    valid = _fed_funds_runs(cycle="ny-cycle-valid")
    unrelated = record_provider_result(
        ProviderResult.failure(
            "us-treasury-rates", "yield-curve", "fixture failure"
        )
    )
    dashboards, stale = _coordinate_fed_funds_dashboard(
        [*valid.values(), unrelated]
    )
    assert [item.key for item in dashboards] == ["fed-funds"]
    assert stale == set()


@pytest.mark.django_db
def test_fed_funds_single_source_triggers_reuse_latest_counterpart(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    first_runs = _fed_funds_runs(cycle="initial-cycle")

    dashboards, stale = _coordinate_fed_funds_dashboard(
        [first_runs["iorb"]]
    )

    assert [item.key for item in dashboards] == ["fed-funds"]
    assert stale == set()
    snapshot = dashboards[0]
    second_sofr = _record_market_run(
        "SOFR",
        cycle="ny-only-cycle",
        records=_market_records("SOFR", history_months=40),
    )
    second_effr = _record_market_run(
        "EFFR",
        cycle="ny-only-cycle",
        records=_market_records("EFFR", history_months=40),
    )

    dashboards, stale = _coordinate_fed_funds_dashboard(
        [second_sofr, second_effr]
    )

    assert dashboards == []
    assert stale == set()
    assert DashboardSnapshot.objects.filter(key="fed-funds").count() == 1
    snapshot.refresh_from_db()
    assert set(snapshot.data["component_batches"]) == {
        str(second_sofr.batch_id),
        str(second_effr.batch_id),
        str(first_runs["iorb"].batch_id),
    }


@pytest.mark.django_db
def test_refresh_prates_entrypoint_coordinates_fed_funds(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    _record_market_run(
        "SOFR",
        cycle="entrypoint-cycle",
        records=_market_records("SOFR", history_months=40),
    )
    _record_market_run(
        "EFFR",
        cycle="entrypoint-cycle",
        records=_market_records("EFFR", history_months=40),
    )
    history_periods = [
        date.fromisoformat(item["date"])
        for item in _market_records("SOFR", history_months=40)
    ]

    class FakePRATESProvider:
        def iorb(self):
            return ProviderResult(
                provider="federal-reserve",
                dataset="prates:iorb",
                fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
                records=_iorb_records(history_periods=history_periods),
                metadata={"quality_status": "complete"},
            )

        def close(self):
            return None

    monkeypatch.setattr(
        "research.official_data.FederalReservePRATESProvider",
        FakePRATESProvider,
    )
    monkeypatch.setattr(
        "research.official_data._coordinate_liquidity_dashboard",
        lambda runs: ([], set()),
    )

    result = refresh_prates_data()

    assert "fed-funds" in result["dashboard_keys"]
    assert result["stale_dashboard_keys"] == []
    snapshot = DashboardSnapshot.objects.get(key="fed-funds")
    assert snapshot.quality_status == "estimated"
    assert {
        item["metadata"]["common_effective_date"]
        for item in snapshot.data["metrics"]
    } == {"2026-07-09"}


@pytest.mark.django_db
def test_refresh_official_entrypoint_coordinates_fed_funds(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    history_periods = [
        date.fromisoformat(item["date"])
        for item in _market_records("SOFR", history_months=40)
    ]
    _record_iorb_run(records=_iorb_records(history_periods=history_periods))

    class FakeNYFedProvider:
        def sofr(self, *, limit):
            assert limit == 800
            return ProviderResult(
                provider="ny-fed-markets",
                dataset="reference-rate:sofr",
                records=_market_records("SOFR", history_months=40),
            )

        def effr(self, *, limit):
            assert limit == 800
            return ProviderResult(
                provider="ny-fed-markets",
                dataset="reference-rate:effr",
                records=_market_records("EFFR", history_months=40),
            )

        def __getattr__(self, method_name):
            def failed_call(**_kwargs):
                return ProviderResult.failure(
                    "ny-fed-markets",
                    method_name,
                    "unrelated NY Fed fixture failure",
                )

            return failed_call

        def close(self):
            return None

    class FailingProvider:
        def __init__(self, source_key):
            self.source_key = source_key

        def __getattr__(self, method_name):
            def failed_call(**_kwargs):
                return ProviderResult.failure(
                    self.source_key,
                    method_name,
                    "unrelated fixture failure",
                )

            return failed_call

        def close(self):
            return None

    monkeypatch.setattr(
        "research.official_data.NYFedMarketsProvider", FakeNYFedProvider
    )
    monkeypatch.setattr(
        "research.official_data.TreasuryRatesProvider",
        lambda: FailingProvider("us-treasury-rates"),
    )
    monkeypatch.setattr(
        "research.official_data.FiscalDataProvider",
        lambda: FailingProvider("treasury-fiscal-data"),
    )
    monkeypatch.setattr(
        "research.official_data.BLSProvider",
        lambda: FailingProvider("bls"),
    )
    monkeypatch.setattr(
        "research.official_data.DOLWeeklyClaimsProvider",
        lambda: FailingProvider("dol-eta-ui"),
    )
    monkeypatch.setattr(
        "research.official_data.FederalReserveRSSProvider",
        lambda: FailingProvider("federal-reserve"),
    )

    result = refresh_official_data(current_year=2026)

    assert "fed-funds" in result["dashboard_keys"]
    assert "fed-funds" not in result["stale_dashboard_keys"]
    snapshot = DashboardSnapshot.objects.get(key="fed-funds")
    assert snapshot.quality_status == "estimated"
    latest_ny_runs = list(
        IngestionRun.objects.filter(
            dataset__in=("reference-rate:sofr", "reference-rate:effr")
        ).order_by("dataset")
    )
    assert len(latest_ny_runs) == 2
    assert len(
        {str(run.metadata["refresh_cycle_id"]) for run in latest_ny_runs}
    ) == 1


@pytest.mark.django_db
def test_fed_funds_outdated_trigger_is_a_noop(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    outdated_runs = _fed_funds_runs(cycle="outdated-cycle")
    current_runs = _fed_funds_runs(cycle="current-cycle")
    dashboards, stale = _coordinate_fed_funds_dashboard(
        current_runs.values()
    )
    assert [item.key for item in dashboards] == ["fed-funds"]
    assert stale == set()
    snapshot = dashboards[0]
    baseline = dict(snapshot.data)

    dashboards, stale = _coordinate_fed_funds_dashboard(
        outdated_runs.values()
    )

    assert dashboards == []
    assert stale == set()
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "estimated"
    assert snapshot.data == baseline
    assert "refresh_failure" not in snapshot.data


@pytest.mark.django_db
def test_fed_funds_rejects_effective_date_regression(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: datetime(2026, 7, 11, 16, 30, tzinfo=UTC),
    )
    first_runs = _fed_funds_runs(cycle="current-date-cycle")
    dashboards, stale = _coordinate_fed_funds_dashboard(first_runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    baseline_metrics = snapshot.data["metrics"]
    baseline_batches = snapshot.data["component_batches"]
    baseline_fingerprint = snapshot.data["fingerprint"]

    truncated_market = {}
    for series_id in ("SOFR", "EFFR"):
        records_by_date = {
            item["date"]: item
            for item in _market_records(series_id, history_months=40)
            if item["date"] < LATEST_MARKET_DATE.isoformat()
        }
        records_by_date.update(
            {
                item["date"]: item
                for item in _market_records(series_id, history_months=0)
                if item["date"] == "2026-07-08"
            }
        )
        truncated_market[series_id] = list(records_by_date.values())
    history_periods = [
        date.fromisoformat(item["date"])
        for item in truncated_market["SOFR"]
    ]
    regressed_runs = {
        "sofr": _record_market_run(
            "SOFR",
            cycle="regressed-cycle",
            records=truncated_market["SOFR"],
        ),
        "effr": _record_market_run(
            "EFFR",
            cycle="regressed-cycle",
            records=truncated_market["EFFR"],
        ),
        "iorb": _record_iorb_run(
            records=_iorb_records(history_periods=history_periods)
        ),
    }

    dashboards, stale = _coordinate_fed_funds_dashboard(
        regressed_runs.values()
    )

    assert dashboards == []
    assert stale == {"fed-funds"}
    assert DashboardSnapshot.objects.filter(key="fed-funds").count() == 1
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "stale"
    assert snapshot.data["metrics"] == baseline_metrics
    assert snapshot.data["component_batches"] == baseline_batches
    assert snapshot.data["fingerprint"] == baseline_fingerprint
    assert "早于当前已发布快照" in snapshot.data["refresh_failure"]["reason"]


@pytest.mark.parametrize("source_key", ["ny-fed-markets", "federal-reserve"])
@pytest.mark.django_db
def test_fed_funds_requires_both_current_public_licences(
    source_key, monkeypatch
):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    runs = _fed_funds_runs(cycle="licence-cycle")
    dashboards, stale = _coordinate_fed_funds_dashboard(runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    baseline_metrics = snapshot.data["metrics"]
    source = Source.objects.get(key=source_key)
    licence = SourceLicense.objects.get(source=source, is_current=True)
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])

    dashboards, stale = _coordinate_fed_funds_dashboard([runs["iorb"]])

    assert dashboards == []
    assert stale == {"fed-funds"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "stale"
    assert snapshot.data["metrics"] == baseline_metrics
    assert "许可" in snapshot.data["refresh_failure"]["reason"]


@pytest.mark.django_db
def test_fed_funds_failure_and_same_value_recovery_refresh_lineage(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    first_runs = _fed_funds_runs(cycle="first-cycle")
    first_dashboards, _ = _coordinate_fed_funds_dashboard(first_runs.values())
    snapshot = first_dashboards[0]
    first_batches = set(snapshot.data["component_batches"])

    failed_sofr = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets", "reference-rate:sofr", "SOFR unavailable"
        )
    )
    second_effr = _record_market_run(
        "EFFR", cycle="failed-cycle", records=_market_records("EFFR")
    )
    unrelated_same_source = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="soma:summary",
            records=[
                {
                    "series_id": "SOMA-TOTAL",
                    "date": "2026-07-09",
                    "value": Decimal("1"),
                }
            ],
        ),
        persist=store_series_observations,
    )
    dashboards, stale = _coordinate_fed_funds_dashboard(
        [failed_sofr, second_effr, unrelated_same_source]
    )
    assert dashboards == []
    assert stale == {"fed-funds"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "stale"
    states = snapshot.data["refresh_failure"]["sources"]
    assert next(item for item in states if item["component"] == "sofr") == {
        "component": "sofr",
        "source": "ny-fed-markets",
        "dataset": "reference-rate:sofr",
        "status": "failed",
        "row_count": 0,
        "error": "SOFR unavailable",
        "batch_id": str(failed_sofr.batch_id),
        "refresh_cycle_id": "",
    }

    recovered_runs = _fed_funds_runs(
        cycle="recovered-cycle",
        fetched_at=datetime(2026, 7, 12, 14, tzinfo=UTC),
    )
    recovered, stale = _coordinate_fed_funds_dashboard(
        recovered_runs.values()
    )
    assert recovered == []
    assert stale == set()
    assert DashboardSnapshot.objects.filter(key="fed-funds").count() == 1
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "estimated"
    assert "refresh_failure" not in snapshot.data
    recovered_batches = {str(run.batch_id) for run in recovered_runs.values()}
    expected_chart_batches = {
        "policy-corridor": {
            "目标下限": str(recovered_runs["effr"].batch_id),
            "目标上限": str(recovered_runs["effr"].batch_id),
            "IORB": str(recovered_runs["iorb"].batch_id),
            "EFFR": str(recovered_runs["effr"].batch_id),
            "SOFR": str(recovered_runs["sofr"].batch_id),
        },
        "effr-distribution": {
            label: str(recovered_runs["effr"].batch_id)
            for label in (
                "EFFR 1P",
                "EFFR 25P",
                "EFFR",
                "EFFR 75P",
                "EFFR 99P",
            )
        },
        "sofr-distribution": {
            label: str(recovered_runs["sofr"].batch_id)
            for label in (
                "SOFR 1P",
                "SOFR 25P",
                "SOFR",
                "SOFR 75P",
                "SOFR 99P",
            )
        },
    }
    assert set(snapshot.data["component_batches"]) == recovered_batches
    assert set(snapshot.data["component_batches"]) != first_batches
    for chart in snapshot.data["charts"]:
        assert set(chart["batch_ids"]) <= recovered_batches
        assert set(chart["batch_ids"])
        assert chart["lineage_mode"] == "series-batch"
        for label, lineage in chart["series_lineage"].items():
            assert lineage["batch_id"] == expected_chart_batches[
                chart["key"]
            ][label]
            assert "input_lineage" not in lineage
            assert "input_batch_ids" not in lineage
        for row in chart["data"]:
            assert "_lineage" not in row
            assert "_source_keys" not in row
    stored = MetricSnapshot.objects.get(
        key="fed-funds-sofr-iorb", batch_id=snapshot.batch_id
    )
    assert set(stored.metadata["input_batch_ids"]) == {
        str(recovered_runs["sofr"].batch_id),
        str(recovered_runs["iorb"].batch_id),
    }


@pytest.mark.django_db
def test_fed_funds_get_controls_slice_group_and_sanitize(client, monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: FIXED_NOW
    )
    runs = _fed_funds_runs(history_months=40)
    _coordinate_fed_funds_dashboard(runs.values())

    response = client.get(
        "/rates/fed-funds/", {"period": "1y", "tab": "effr"}
    )
    assert response.status_code == 200
    assert response.context["selected_period"] == "1y"
    assert response.context["selected_tab"] == "effr"
    assert [item["key"] for item in response.context["charts"]] == [
        "effr-distribution"
    ]
    assert len(response.context["charts"][0]["data"]) <= 13
    body = html.unescape(response.content.decode())
    assert "Federal Reserve Bank of New York" in body
    assert "Federal Reserve Board" in body
    assert "period=3y&tab=effr" in body or "tab=effr&period=3y" in body
    assert "period=1y&tab=sofr" in body or "tab=sofr&period=1y" in body
    assert "not affiliated with the New York Fed" in body

    three_year = client.get(
        "/rates/fed-funds/", {"period": "3y", "tab": "corridor"}
    )
    assert three_year.context["selected_period"] == "3y"
    assert three_year.context["selected_tab"] == "corridor"
    assert [item["key"] for item in three_year.context["charts"]] == [
        "policy-corridor"
    ]
    three_year_rows = three_year.context["charts"][0]["data"]
    assert len(three_year_rows) >= 36
    assert three_year_rows[0]["date"] >= "2023-07-09"

    default = client.get("/rates/fed-funds/")
    assert default.context["selected_period"] == "1y"
    assert default.context["selected_tab"] == "overview"
    assert len(default.context["charts"]) == 3

    invalid = client.get(
        "/rates/fed-funds/",
        {"period": "<script>alert(1)</script>", "tab": "missing"},
    )
    assert invalid.context["selected_period"] == "1y"
    assert invalid.context["selected_tab"] == "overview"
    assert "<script>alert(1)</script>" not in invalid.content.decode()
    assert "alert%281%29" not in invalid.content.decode()


def test_fed_funds_catalog_is_live_without_paid_source():
    requirement = next(
        item for item in DATA_REQUIREMENTS if item["key"] == "nyfed-policy-rates"
    )
    assert requirement["page_key"] == "fed-funds"
    assert requirement["status"] == "live"
    assert "共同有效日" in requirement["reason"]
    assert "PRATES" in requirement["source_name"]
    assert FED_FUNDS_DATASETS == {
        "sofr": ("ny-fed-markets", "reference-rate:sofr"),
        "effr": ("ny-fed-markets", "reference-rate:effr"),
        "iorb": ("federal-reserve", "prates:iorb"),
    }
