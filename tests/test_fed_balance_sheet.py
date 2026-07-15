from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.db.models import QuerySet

from research.data_catalog import DATA_REQUIREMENTS
from research.models import (
    DashboardSnapshot,
    DataRequirement,
    IngestionRun,
    MetricSnapshot,
    Observation,
    Source,
    SourceLicense,
)
from research.official_data import (
    FED_BALANCE_SHEET_CONTRACT_VERSION,
    FED_BALANCE_SHEET_MINIMUM_COMMON_ROWS,
    FED_BALANCE_SHEET_REQUIRED_CHART_KEYS,
    FED_BALANCE_SHEET_REQUIRED_METRIC_KEYS,
    H41_PUBLICATION_KEYS,
    INDEPENDENT_PUBLICATION_KEYS,
    _coordinate_fed_balance_sheet_dashboard,
    _select_fed_balance_sheet_runs,
    _store_fed_balance_sheet_component_observations,
    fed_balance_sheet_snapshot_is_publicly_displayable,
    publish_official_dashboards,
    select_public_fed_balance_sheet_snapshot,
)
from research.page_registry import get_page_config
from research.providers import ProviderResult
from research.services import (
    begin_ingestion,
    ensure_source,
    record_provider_result,
    store_series_observations,
)

FIXED_NOW = datetime(2026, 7, 14, 10, tzinfo=UTC)
COMMON_END = date(2026, 7, 8)


def _weekly_dates(*, count: int, end: date = COMMON_END) -> list[date]:
    return [end - timedelta(weeks=offset) for offset in reversed(range(count))]


def _records(
    series_id: str,
    periods: list[date],
    *,
    base: Decimal,
    step: Decimal,
    shift: Decimal = Decimal("0"),
) -> list[dict]:
    return [
        {
            "series_id": series_id,
            "date": period.isoformat(),
            "value": base + shift + step * index,
        }
        for index, period in enumerate(periods)
    ]


def _record_components(
    *,
    cycle: str | None = None,
    count: int = 22,
    end: date = COMMON_END,
    shift: Decimal = Decimal("0"),
    fetched_at: datetime = datetime(2026, 7, 10, 12, tzinfo=UTC),
) -> dict[str, IngestionRun]:
    cycle = cycle or str(uuid.uuid4())
    periods = _weekly_dates(count=count, end=end)
    h41_records = [
        *_records(
            "WALCL", periods, base=Decimal("6700000"), step=Decimal("1000"), shift=shift
        ),
        *_records(
            "WSHOTSL", periods, base=Decimal("4200000"), step=Decimal("500"), shift=shift
        ),
        *_records(
            "WSHOMCB", periods, base=Decimal("2100000"), step=Decimal("250"), shift=shift
        ),
        *_records(
            "WRBWFRBL", periods, base=Decimal("3000000"), step=Decimal("750"), shift=shift
        ),
    ]
    daily_tail = [date(2026, 7, 10)] if end <= COMMON_END else []
    onrrp_records = _records(
        "ONRRP",
        [*periods, *daily_tail],
        base=Decimal("5000"),
        step=Decimal("10"),
        shift=shift,
    )
    tga_records = _records(
        "TGA",
        [*periods, *daily_tail],
        base=Decimal("700000"),
        step=Decimal("100"),
        shift=shift,
    )
    return {
        "h41": record_provider_result(
            ProviderResult(
                provider="federal-reserve",
                dataset="h41",
                fetched_at=fetched_at,
                records=h41_records,
            ),
            persist=store_series_observations,
        ),
        "onrrp": record_provider_result(
            ProviderResult(
                provider="ny-fed-markets",
                dataset="repo:reverse-repo-fixed-results",
                fetched_at=fetched_at,
                records=onrrp_records,
                metadata={"refresh_cycle_id": cycle},
            ),
            persist=_store_fed_balance_sheet_component_observations,
        ),
        "tga": record_provider_result(
            ProviderResult(
                provider="treasury-fiscal-data",
                dataset="daily-treasury-statement:tga",
                fetched_at=fetched_at,
                records=tga_records,
                metadata={"refresh_cycle_id": cycle},
            ),
            persist=_store_fed_balance_sheet_component_observations,
        ),
    }


def _publish_complete(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    runs = _record_components(cycle="complete-cycle")
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(runs.values())
    assert stale == set()
    assert len(dashboards) == 1
    return dashboards[0], runs


def test_fed_balance_sheet_contract_constants_and_page_registry():
    config = get_page_config("fed-balance-sheet")
    requirements = {item["key"]: item for item in DATA_REQUIREMENTS}

    assert FED_BALANCE_SHEET_CONTRACT_VERSION == 1
    assert FED_BALANCE_SHEET_MINIMUM_COMMON_ROWS == 20
    assert FED_BALANCE_SHEET_REQUIRED_METRIC_KEYS == {
        "walcl",
        "wshotsl",
        "wshomcb",
        "wrbwfrbl",
        "net-liquidity",
    }
    assert FED_BALANCE_SHEET_REQUIRED_CHART_KEYS == {
        "fed-balance-sheet-history",
        "fed-balance-sheet-net-liquidity-history",
    }
    assert config["snapshot_contract_version"] == 1
    assert len(config["metrics"]) == 5
    assert requirements["fed-h41-balance-sheet-inputs"]["status"] == (
        DataRequirement.Status.LIVE
    )
    assert requirements["fed-balance-sheet-public-contract"]["status"] == (
        DataRequirement.Status.PROXY
    )


@pytest.mark.django_db
def test_generic_publisher_cannot_bypass_fed_balance_sheet_contract():
    assert H41_PUBLICATION_KEYS == frozenset()
    assert "fed-balance-sheet" in INDEPENDENT_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"fed-balance-sheet"}) == []
    assert all(
        item.key != "fed-balance-sheet" for item in publish_official_dashboards()
    )


@pytest.mark.django_db
def test_fed_balance_sheet_v1_publishes_exact_common_contract(
    client, monkeypatch
):
    snapshot, runs = _publish_complete(monkeypatch)
    data = snapshot.data

    assert data["contract_version"] == FED_BALANCE_SHEET_CONTRACT_VERSION
    assert data["common_effective_date"] == COMMON_END.isoformat()
    assert data["common_row_count"] == 22
    assert set(data["component_batches"]) == {
        str(run.batch_id) for run in runs.values()
    }
    metrics = {item["key"]: item for item in data["metrics"]}
    assert set(metrics) == set(FED_BALANCE_SHEET_REQUIRED_METRIC_KEYS)
    assert all(item["fallback_source"] is None for item in metrics.values())
    assert metrics["net-liquidity"]["metadata"]["formula"] == (
        "WALCL - ONRRP - TGA"
    )
    net_inputs = metrics["net-liquidity"]["metadata"]["input_lineage"]
    expected_net = (
        Decimal(next(item["raw_value"] for item in net_inputs if item["series_key"] == "walcl"))
        - Decimal(next(item["raw_value"] for item in net_inputs if item["series_key"] == "onrrp"))
        - Decimal(next(item["raw_value"] for item in net_inputs if item["series_key"] == "tga"))
    ) * Decimal("0.000001")
    assert Decimal(str(metrics["net-liquidity"]["value"])) == expected_net

    charts = {item["key"]: item for item in data["charts"]}
    assert set(charts) == set(FED_BALANCE_SHEET_REQUIRED_CHART_KEYS)
    balance_dates = [
        row["date"] for row in charts["fed-balance-sheet-history"]["data"]
    ]
    net_dates = [
        row["date"]
        for row in charts["fed-balance-sheet-net-liquidity-history"]["data"]
    ]
    assert balance_dates == net_dates
    assert len(balance_dates) == 22
    assert all(date.fromisoformat(period).weekday() == 2 for period in balance_dates)
    recent = next(
        item for item in data["sections"] if item["key"] == "recent-fed-balance-sheet"
    )
    assert [row["date"] for row in recent["rows"]] == balance_dates[-20:]
    assert recent["rows"][-1]["cells_list"] == [
        {"key": "date", "cell": {"value": COMMON_END.isoformat()}},
        {
            "key": "total-assets",
            "cell": {
                "value": f"{recent['rows'][-1]['Total assets']:,.6f}"
            },
        },
        {
            "key": "treasuries",
            "cell": {"value": f"{recent['rows'][-1]['Treasuries']:,.6f}"},
        },
        {
            "key": "mbs",
            "cell": {"value": f"{recent['rows'][-1]['MBS']:,.6f}"},
        },
        {
            "key": "net-liquidity",
            "cell": {
                "value": f"{recent['rows'][-1]['Net liquidity']:,.6f}"
            },
        },
    ]
    normalized = MetricSnapshot.objects.filter(
        key__startswith="fed-balance-sheet-", batch_id=snapshot.batch_id
    )
    assert normalized.count() == 5
    assert all(item.license_scope for item in normalized)
    assert select_public_fed_balance_sheet_snapshot().pk == snapshot.pk

    response = client.get("/liquidity/fed-balance-sheet/")
    body = response.content.decode()
    assert response.status_code == 200
    assert response.context["snapshot"].pk == snapshot.pk
    assert "净流动性代理" in body
    assert "近期精确共同周三" in body
    assert COMMON_END.isoformat() in body


@pytest.mark.django_db
def test_latest_failure_retains_stale_then_same_value_recovery_appends_revision(
    client, monkeypatch
):
    snapshot, original_runs = _publish_complete(monkeypatch)
    original_id = snapshot.pk
    original_batch = snapshot.batch_id
    original_fingerprint = snapshot.data["fingerprint"]
    original_metrics = deepcopy(snapshot.data["metrics"])
    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            error="upstream timeout",
        )
    )

    dashboards, stale = _coordinate_fed_balance_sheet_dashboard([failed])
    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == {"fed-balance-sheet"}
    assert snapshot.quality_status == Observation.Quality.STALE
    assert snapshot.data["metrics"] == original_metrics
    assert fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert client.get("/liquidity/fed-balance-sheet/").context["snapshot"].pk == original_id

    retained_failure = deepcopy(snapshot.data["refresh_failure"])
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        original_runs.values()
    )
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == set()
    assert snapshot.data["refresh_failure"] == retained_failure

    recovered = _record_components(
        cycle="recovered-cycle",
        fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
    )
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        recovered.values()
    )
    snapshot.refresh_from_db()
    assert len(dashboards) == 1 and stale == set()
    recovered_snapshot = dashboards[0]
    assert recovered_snapshot.pk != original_id
    assert recovered_snapshot.batch_id != original_batch
    assert recovered_snapshot.data["fingerprint"] == original_fingerprint
    assert "refresh_failure" not in recovered_snapshot.data
    assert snapshot.data["refresh_failure"] == retained_failure
    assert snapshot.batch_id == original_batch
    assert set(recovered_snapshot.data["component_batches"]) == {
        str(run.batch_id) for run in recovered.values()
    }
    normalized = MetricSnapshot.objects.get(
        key="fed-balance-sheet-net-liquidity",
        batch_id=recovered_snapshot.batch_id,
    )
    assert set(normalized.metadata["input_batch_ids"]) == {
        str(recovered[key].batch_id) for key in ("h41", "onrrp", "tga")
    }

    unchanged = deepcopy(recovered_snapshot.data)
    unchanged_updated_at = recovered_snapshot.updated_at
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        recovered.values()
    )
    recovered_snapshot.refresh_from_db()
    assert dashboards == [] and stale == set()
    assert recovered_snapshot.data == unchanged
    assert recovered_snapshot.updated_at == unchanged_updated_at


@pytest.mark.django_db
def test_selector_keeps_previous_snapshot_stale_during_newer_failed_attempt(
    monkeypatch,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            error="upstream timeout before coordinator",
        )
    )

    assert failed.status == IngestionRun.Status.FAILED
    snapshot.refresh_from_db()
    assert snapshot.quality_status != Observation.Quality.STALE
    assert "refresh_failure" not in snapshot.data
    selected = select_public_fed_balance_sheet_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.quality_status == Observation.Quality.STALE
    assert fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    snapshot.refresh_from_db()
    assert snapshot.quality_status != Observation.Quality.STALE
    assert "refresh_failure" not in snapshot.data


@pytest.mark.django_db
def test_selector_keeps_naturally_expired_complete_snapshot_explicitly_stale(
    client,
    monkeypatch,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: datetime(2026, 7, 25, 10, tzinfo=UTC),
    )

    selected = select_public_fed_balance_sheet_snapshot()

    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.quality_status == Observation.Quality.STALE
    assert fed_balance_sheet_snapshot_is_publicly_displayable(
        DashboardSnapshot.objects.get(pk=snapshot.pk)
    )
    response = client.get("/liquidity/fed-balance-sheet/")
    assert response.context["snapshot"].pk == snapshot.pk
    assert response.context["dashboard"]["quality_status"] == "stale"


@pytest.mark.django_db
@pytest.mark.parametrize("tamper", ["fingerprint", "title", "summary"])
def test_fresh_selector_recomputes_fingerprint_with_title_and_summary(
    monkeypatch,
    tamper,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    if tamper == "fingerprint":
        data = deepcopy(snapshot.data)
        data["fingerprint"] = "0" * 64
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    else:
        setattr(snapshot, tamper, f"{getattr(snapshot, tamper)} tampered")
        snapshot.save(update_fields=[tamper, "updated_at"])

    assert not fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert select_public_fed_balance_sheet_snapshot() is None


@pytest.mark.django_db
@pytest.mark.parametrize("attempt_kind", ["running", "partial"])
def test_selector_keeps_previous_snapshot_stale_during_open_attempt(
    monkeypatch,
    attempt_kind,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    if attempt_kind == "running":
        attempt = begin_ingestion(
            "ny-fed-markets",
            "repo:reverse-repo-fixed-results",
        )
    else:
        attempt = record_provider_result(
            ProviderResult(
                provider="ny-fed-markets",
                dataset="repo:reverse-repo-fixed-results",
                skipped=True,
                metadata={"reason": "upstream window not complete"},
            )
        )

    assert attempt.status == {
        "running": IngestionRun.Status.RUNNING,
        "partial": IngestionRun.Status.PARTIAL,
    }[attempt_kind]
    selected = select_public_fed_balance_sheet_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.quality_status == Observation.Quality.STALE
    snapshot.refresh_from_db()
    assert snapshot.quality_status != Observation.Quality.STALE
    assert "refresh_failure" not in snapshot.data

    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["quality_status", "updated_at"])
    assert not fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert select_public_fed_balance_sheet_snapshot() is None


@pytest.mark.django_db
def test_selector_keeps_previous_snapshot_during_onrrp_only_success_transition(
    monkeypatch,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    periods = _weekly_dates(count=22)
    newer = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
            records=_records(
                "ONRRP",
                periods,
                base=Decimal("5100"),
                step=Decimal("10"),
            ),
            metadata={"refresh_cycle_id": "onrrp-only-transition"},
        ),
        persist=_store_fed_balance_sheet_component_observations,
    )

    assert newer.status == IngestionRun.Status.SUCCESS
    selected = select_public_fed_balance_sheet_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.quality_status == Observation.Quality.STALE
    assert fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    snapshot.refresh_from_db()
    assert snapshot.quality_status != Observation.Quality.STALE
    assert "refresh_failure" not in snapshot.data


@pytest.mark.django_db
def test_transient_snapshot_rejects_historical_payload_tampering(monkeypatch):
    snapshot, _runs = _publish_complete(monkeypatch)
    periods = _weekly_dates(count=22)
    newer = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
            records=_records(
                "ONRRP",
                periods,
                base=Decimal("5200"),
                step=Decimal("10"),
            ),
            metadata={"refresh_cycle_id": "tamper-transition"},
        ),
        persist=_store_fed_balance_sheet_component_observations,
    )
    assert newer.status == IngestionRun.Status.SUCCESS

    tampered = deepcopy(snapshot.data)
    original_fingerprint = tampered["fingerprint"]
    history = next(
        chart
        for chart in tampered["charts"]
        if chart["key"] == "fed-balance-sheet-history"
    )
    row = history["data"][0]
    lineage = row["_lineage"]["Total assets"]
    changed_raw = Decimal(lineage["raw_value"]) + Decimal("1000000")
    row["Total assets"] = float(changed_raw * Decimal("0.000001"))
    lineage["raw_value"] = str(changed_raw)
    lineage["value"] = float(changed_raw)
    tampered["fingerprint"] = original_fingerprint
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])

    assert not fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert select_public_fed_balance_sheet_snapshot() is None


@pytest.mark.django_db
@pytest.mark.parametrize("transition", [False, True])
def test_selector_rejects_corrupt_failure_and_unknown_quality_states(
    monkeypatch,
    transition,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    if transition:
        periods = _weekly_dates(count=22)
        newer = record_provider_result(
            ProviderResult(
                provider="ny-fed-markets",
                dataset="repo:reverse-repo-fixed-results",
                fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
                records=_records(
                    "ONRRP",
                    periods,
                    base=Decimal("5300"),
                    step=Decimal("10"),
                ),
                metadata={"refresh_cycle_id": "strict-state-transition"},
            ),
            persist=_store_fed_balance_sheet_component_observations,
        )
        assert newer.status == IngestionRun.Status.SUCCESS

    original_data = deepcopy(snapshot.data)
    corrupt_failure = deepcopy(original_data)
    corrupt_failure["refresh_failure"] = "corrupt"
    snapshot.data = corrupt_failure
    snapshot.save(update_fields=["data", "updated_at"])
    assert not fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert select_public_fed_balance_sheet_snapshot() is None

    snapshot.data = original_data
    snapshot.quality_status = "garbage"
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])
    assert not fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert select_public_fed_balance_sheet_snapshot() is None


@pytest.mark.django_db
@pytest.mark.parametrize("failure_payload", [{}, {"arbitrary": True}])
def test_durable_retention_requires_complete_refresh_failure_schema(
    monkeypatch,
    failure_payload,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            error="upstream timeout",
        )
    )
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard([failed])
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)

    data = deepcopy(snapshot.data)
    data["refresh_failure"] = failure_payload
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])
    assert not fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    assert select_public_fed_balance_sheet_snapshot() is None


@pytest.mark.django_db
def test_superseded_onrrp_and_tga_persistence_cannot_overwrite_new_batches(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    periods = _weekly_dates(count=22)
    old_cycle = "old-cycle"
    old_results = {
        "onrrp": ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            fetched_at=datetime(2026, 7, 10, 11, tzinfo=UTC),
            records=_records(
                "ONRRP", periods, base=Decimal("4900"), step=Decimal("10")
            ),
            metadata={"refresh_cycle_id": old_cycle},
        ),
        "tga": ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            fetched_at=datetime(2026, 7, 10, 11, tzinfo=UTC),
            records=_records(
                "TGA", periods, base=Decimal("690000"), step=Decimal("100")
            ),
            metadata={"refresh_cycle_id": old_cycle},
        ),
    }
    old_runs = {
        identity: begin_ingestion(
            result.provider,
            result.dataset,
            metadata={
                "provider": result.provider,
                "fetched_at": result.fetched_at.isoformat(),
                **result.metadata,
            },
        )
        for identity, result in old_results.items()
    }

    new_runs = _record_components(cycle="new-cycle")
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        new_runs.values()
    )
    assert len(dashboards) == 1 and stale == set()

    for identity in ("onrrp", "tga"):
        monkeypatch.setattr(
            "research.services.begin_ingestion",
            lambda *_args, run=old_runs[identity], **_kwargs: run,
        )
        failed = record_provider_result(
            old_results[identity],
            persist=_store_fed_balance_sheet_component_observations,
        )
        assert failed.pk == old_runs[identity].pk
        assert failed.status == IngestionRun.Status.FAILED
        assert failed.row_count == 0
        assert "superseded" in failed.error
        assert not Observation.objects.filter(
            batch_id=old_runs[identity].batch_id
        ).exists()

    for identity, series_key in (("onrrp", "onrrp"), ("tga", "tga")):
        assert set(
            Observation.objects.filter(series__key=series_key).values_list(
                "batch_id", flat=True
            )
        ) == {new_runs[identity].batch_id}
    assert select_public_fed_balance_sheet_snapshot().pk == dashboards[0].pk


@pytest.mark.django_db
def test_guarded_onrrp_persistence_allows_only_known_auxiliary_series(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    allowed = [
        "ONRRP",
        "ONRRP-RATE",
        "ONRRP-PARTICIPANTS",
        "ONRRP-BANK",
        "ONRRP-GSE",
        "ONRRP-MMF",
        "ONRRP-PD",
    ]
    run = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
            records=[
                {
                    "series_id": series_key,
                    "date": COMMON_END.isoformat(),
                    "value": Decimal(index + 1),
                }
                for index, series_key in enumerate(allowed)
            ],
            metadata={"refresh_cycle_id": "allowed-onrrp-series"},
        ),
        persist=_store_fed_balance_sheet_component_observations,
    )

    assert run.status == IngestionRun.Status.SUCCESS
    assert run.row_count == len(allowed)
    assert set(
        Observation.objects.filter(batch_id=run.batch_id).values_list(
            "series__key", flat=True
        )
    ) == {series_key.lower() for series_key in allowed}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("invalid_record", "error_fragment"),
    [
        (
            {
                "series_id": "WALCL",
                "date": "2026-07-07",
                "value": Decimal("1"),
            },
            "unknown series",
        ),
        (
            {
                "series_id": "TGA",
                "date": "2026-01-01junk",
                "value": Decimal("1"),
            },
            "malformed row",
        ),
        (
            {
                "series_id": "TGA",
                "date": "2026-07-07",
                "value": Decimal("-1"),
            },
            "negative value",
        ),
    ],
)
def test_guarded_tga_invalid_rows_fail_and_roll_back_atomically(
    monkeypatch,
    invalid_record,
    error_fragment,
):
    snapshot, _runs = _publish_complete(monkeypatch)
    original_data = deepcopy(snapshot.data)
    run = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
            records=[
                {
                    "series_id": "TGA",
                    "date": COMMON_END.isoformat(),
                    "value": Decimal("700000"),
                },
                invalid_record,
            ],
            metadata={"refresh_cycle_id": "invalid-tga-row"},
        ),
        persist=_store_fed_balance_sheet_component_observations,
    )

    assert run.status == IngestionRun.Status.FAILED
    assert run.row_count == 0
    assert error_fragment in run.error
    assert not Observation.objects.filter(batch_id=run.batch_id).exists()
    assert not Observation.objects.filter(
        source__key="treasury-fiscal-data",
        series__key="walcl",
    ).exists()
    assert all(
        item.value_date.date() != date.min
        for item in Observation.objects.filter(
            source__key="treasury-fiscal-data"
        )
    )
    snapshot.refresh_from_db()
    assert snapshot.data == original_data
    assert "refresh_failure" not in snapshot.data
    selected = select_public_fed_balance_sheet_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.quality_status == Observation.Quality.STALE


@pytest.mark.django_db
def test_invalid_new_success_is_retained_stale_but_fresh_old_cannot_bypass_latest(
    client, monkeypatch
):
    snapshot, _ = _publish_complete(monkeypatch)
    original_id = snapshot.pk
    newer = _record_components(cycle="new-cycle", shift=Decimal("100"))
    newer["tga"].metadata = {
        **newer["tga"].metadata,
        "refresh_cycle_id": "mismatched-cycle",
    }
    newer["tga"].save(update_fields=["metadata", "updated_at"])

    selected = select_public_fed_balance_sheet_snapshot()
    assert selected is not None
    assert selected.pk == original_id
    assert selected.quality_status == Observation.Quality.STALE
    snapshot.refresh_from_db()
    assert snapshot.quality_status != Observation.Quality.STALE
    assert "refresh_failure" not in snapshot.data
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(newer.values())
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert snapshot.quality_status == Observation.Quality.STALE
    assert fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)
    response = client.get("/liquidity/fed-balance-sheet/")
    assert response.context["snapshot"].pk == original_id
    assert response.context["dashboard"]["quality_status"] == "stale"
    assert response.context["refresh_failure"]


@pytest.mark.django_db
def test_date_regression_and_mixed_batch_retain_previous_stale(monkeypatch):
    snapshot, _ = _publish_complete(monkeypatch)
    original_metrics = deepcopy(snapshot.data["metrics"])
    regressed = _record_components(
        cycle="regressed", end=date(2026, 7, 1), shift=Decimal("50")
    )
    for observation in Observation.objects.filter(
        source=regressed["h41"].source,
        batch_id=regressed["h41"].batch_id,
    ):
        observation.metadata = {
            **observation.metadata,
            "source_release_time": datetime(
                2026, 7, 10, 12, tzinfo=UTC
            ).isoformat(),
            "release_freshness_days": 8,
        }
        observation.save(update_fields=["metadata", "updated_at"])
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        regressed.values()
    )
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert snapshot.data["metrics"] == original_metrics
    assert "回退" in snapshot.data["refresh_failure"]["reason"]

    mixed = _record_components(cycle="mixed", shift=Decimal("75"))
    polluted = Observation.objects.filter(
        source=mixed["h41"].source,
        batch_id=mixed["h41"].batch_id,
        series__key="wshotsl",
    ).first()
    polluted.batch_id = uuid.uuid4()
    polluted.save(update_fields=["batch_id", "updated_at"])
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(mixed.values())
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert "batch-pollution" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
def test_licence_revocation_hides_retained_snapshot(client, monkeypatch):
    snapshot, runs = _publish_complete(monkeypatch)
    licence = SourceLicense.objects.get(
        source__key="ny-fed-markets", is_current=True
    )
    licence.status = Source.LicenseStatus.RESTRICTED
    licence.public_display_allowed = False
    licence.derived_display_allowed = False
    licence.reviewed_by = "licence-admin"
    licence.reviewed_at = FIXED_NOW
    licence.save(
        update_fields=[
            "status",
            "public_display_allowed",
            "derived_display_allowed",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )

    selected, states, triggered = _select_fed_balance_sheet_runs([runs["h41"]])
    assert triggered, states
    assert selected is not None, states
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard([runs["h41"]])
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert select_public_fed_balance_sheet_snapshot() is None
    response = client.get("/liquidity/fed-balance-sheet/")
    assert response.status_code == 200
    assert response.context.get("snapshot") is None
    assert "unlicensed" in str(response.context["refresh_failure"])


@pytest.mark.django_db
def test_fallback_duplicate_and_insufficient_sample_fail_closed(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    insufficient = _record_components(cycle="short", count=19)
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        insufficient.values()
    )
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert not DashboardSnapshot.objects.filter(key="fed-balance-sheet").exists()

    complete = _record_components(cycle="complete-after-short")
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(complete.values())
    assert len(dashboards) == 1 and stale == set()
    snapshot = dashboards[0]

    fallback_runs = _record_components(cycle="fallback", shift=Decimal("25"))
    fallback = Observation.objects.get(
        series__key="onrrp",
        batch_id=fallback_runs["onrrp"].batch_id,
        value_date__date=COMMON_END,
    )
    fallback.fallback_source = ensure_source("internal")
    fallback.quality_status = Observation.Quality.FALLBACK
    fallback.save(
        update_fields=["fallback_source", "quality_status", "updated_at"]
    )
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        fallback_runs.values()
    )
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert "invalid-lineage" in str(snapshot.data["refresh_failure"])

    duplicate_runs = _record_components(cycle="duplicate", shift=Decimal("50"))
    original = Observation.objects.get(
        series__key="tga",
        batch_id=duplicate_runs["tga"].batch_id,
        value_date__date=COMMON_END,
    )
    Observation.objects.create(
        series=original.series,
        value=original.value,
        value_date=original.value_date,
        as_of=original.as_of,
        fetched_at=original.fetched_at,
        batch_id=original.batch_id,
        source=original.source,
        quality_status=original.quality_status,
    )
    duplicate_runs["tga"].row_count += 1
    duplicate_runs["tga"].save(update_fields=["row_count", "updated_at"])
    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(
        duplicate_runs.values()
    )
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert "duplicate" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
def test_postcondition_rollback_and_lock_path_preserve_previous(monkeypatch):
    snapshot, _ = _publish_complete(monkeypatch)
    original_data = deepcopy(snapshot.data)
    changed = _record_components(cycle="changed", shift=Decimal("100"))
    lock_calls: list[bool] = []
    original_select_for_update = QuerySet.select_for_update

    def tracked_select_for_update(queryset, *args, **kwargs):
        lock_calls.append(True)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)
    monkeypatch.setattr(
        "research.official_data._fed_balance_sheet_runs_still_latest",
        lambda selected: False,
    )

    dashboards, stale = _coordinate_fed_balance_sheet_dashboard(changed.values())
    snapshot.refresh_from_db()
    assert dashboards == [] and stale == {"fed-balance-sheet"}
    assert lock_calls
    assert DashboardSnapshot.objects.filter(key="fed-balance-sheet").count() == 1
    assert snapshot.data["metrics"] == original_data["metrics"]
    assert snapshot.data["charts"] == original_data["charts"]
    assert "publication postcondition" in str(snapshot.data["refresh_failure"])
    assert fed_balance_sheet_snapshot_is_publicly_displayable(snapshot)


@pytest.mark.django_db
def test_selector_rejects_payload_and_normalized_tampering(monkeypatch):
    snapshot, _ = _publish_complete(monkeypatch)
    original = deepcopy(snapshot.data)

    tampered = deepcopy(original)
    tampered["metrics"][1]["batch_id"] = str(uuid.uuid4())
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_fed_balance_sheet_snapshot() is None

    tampered = deepcopy(original)
    recent = next(
        item for item in tampered["sections"] if item["key"] == "recent-fed-balance-sheet"
    )
    recent["rows"][-1]["cells_list"][-1]["cell"]["value"] = "tampered"
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_fed_balance_sheet_snapshot() is None

    snapshot.data = original
    snapshot.save(update_fields=["data", "updated_at"])
    tampered = deepcopy(original)
    tampered["metrics"].append(deepcopy(tampered["metrics"][0]))
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_fed_balance_sheet_snapshot() is None

    tampered = deepcopy(original)
    tampered["charts"].append(deepcopy(tampered["charts"][0]))
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_fed_balance_sheet_snapshot() is None

    tampered = deepcopy(original)
    tampered["metrics"][0]["display_value"] = "999.000000 USD tn"
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_fed_balance_sheet_snapshot() is None

    snapshot.data = original
    snapshot.save(update_fields=["data", "updated_at"])
    normalized = MetricSnapshot.objects.get(
        key="fed-balance-sheet-walcl", batch_id=snapshot.batch_id
    )
    normalized.license_scope = "wrong scope"
    normalized.save(update_fields=["license_scope", "updated_at"])
    assert select_public_fed_balance_sheet_snapshot() is None
