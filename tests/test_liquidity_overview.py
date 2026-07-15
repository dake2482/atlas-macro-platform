from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    SourceLicense,
)
from research.official_data import (
    CORE_PUBLICATION_KEYS,
    H41_PUBLICATION_KEYS,
    LIQUIDITY_CONTRACT_VERSION,
    LIQUIDITY_REQUIRED_METRIC_KEYS,
    _coordinate_liquidity_dashboard,
    publish_official_dashboards,
    refresh_h41_data,
    refresh_official_data,
    refresh_prates_data,
)
from research.providers import ProviderResult
from research.services import (
    ensure_source,
    record_provider_result,
    store_series_observations,
)

FIXED_NOW = datetime(2026, 7, 12, 16, 30, tzinfo=UTC)
FED_DATE = datetime(2026, 7, 9, tzinfo=UTC)


def _records(series_id: str, values: list[tuple[str, str]]) -> list[dict]:
    return [
        {"series_id": series_id, "date": period, "value": Decimal(value)}
        for period, value in values
    ]


def _record_direct_components(
    *,
    cycle: str | None = None,
    tga_common_value: str = "749244",
    fetched_at: datetime = datetime(2026, 7, 12, 12, tzinfo=UTC),
) -> dict[str, IngestionRun]:
    cycle = cycle or str(uuid.uuid4())
    h41_records = [
        *_records(
            "WALCL",
            [
                ("2026-06-24", "6700000"),
                ("2026-07-01", "6720000"),
                ("2026-07-08", "6735609"),
            ],
        ),
        *_records(
            "WRBWFRBL",
            [
                ("2026-06-24", "3290000"),
                ("2026-07-01", "3300000"),
                ("2026-07-08", "3310000"),
            ],
        ),
    ]
    onrrp_records = _records(
        "ONRRP",
        [
            ("2026-06-24", "5000"),
            ("2026-07-01", "4000"),
            ("2026-07-08", "3347"),
            ("2026-07-09", "1000"),
            ("2026-07-10", "545"),
            ("2026-07-13", "999999"),
        ],
    )
    tga_records = _records(
        "TGA",
        [
            ("2026-06-24", "700000"),
            ("2026-07-01", "720000"),
            ("2026-07-08", tga_common_value),
            ("2026-07-09", "744637"),
            ("2026-07-13", "999999"),
        ],
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
            persist=store_series_observations,
        ),
        "tga": record_provider_result(
            ProviderResult(
                provider="treasury-fiscal-data",
                dataset="daily-treasury-statement:tga",
                fetched_at=fetched_at,
                records=tga_records,
                metadata={"refresh_cycle_id": cycle},
            ),
            persist=store_series_observations,
        ),
    }


def _input_lineage(
    *,
    series_key: str,
    value: str,
    source_key: str,
    batch_id: uuid.UUID,
    fetched_at: datetime,
) -> dict:
    source = ensure_source(source_key)
    return {
        "series_key": series_key,
        "value": float(Decimal(value)),
        "raw_value": value,
        "source_key": source_key,
        "source_name": source.name,
        "license_scope": source.license_scope,
        "value_date": FED_DATE.isoformat(),
        "as_of": FED_DATE.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "batch_id": str(batch_id),
        "quality_status": "fresh",
        "fallback_source": None,
    }


def _create_fed_funds_component() -> tuple[DashboardSnapshot, dict[str, IngestionRun]]:
    fetched_at = datetime(2026, 7, 10, 13, tzinfo=UTC)
    cycle = str(uuid.uuid4())
    ny_fed = ensure_source("ny-fed-markets")
    federal_reserve = ensure_source("federal-reserve")
    internal = ensure_source("internal")
    runs = {
        "sofr": IngestionRun.objects.create(
            source=ny_fed,
            dataset="reference-rate:sofr",
            started_at=fetched_at,
            completed_at=fetched_at,
            status=IngestionRun.Status.SUCCESS,
            row_count=40,
            metadata={"refresh_cycle_id": cycle},
        ),
        "effr": IngestionRun.objects.create(
            source=ny_fed,
            dataset="reference-rate:effr",
            started_at=fetched_at,
            completed_at=fetched_at,
            status=IngestionRun.Status.SUCCESS,
            row_count=40,
            metadata={"refresh_cycle_id": cycle},
        ),
        "iorb": IngestionRun.objects.create(
            source=federal_reserve,
            dataset="prates:iorb",
            started_at=fetched_at,
            completed_at=fetched_at,
            status=IngestionRun.Status.SUCCESS,
            row_count=40,
        ),
    }
    lineages = {
        "sofr": _input_lineage(
            series_key="sofr",
            value="3.53",
            source_key="ny-fed-markets",
            batch_id=runs["sofr"].batch_id,
            fetched_at=fetched_at,
        ),
        "effr": _input_lineage(
            series_key="effr",
            value="3.62",
            source_key="ny-fed-markets",
            batch_id=runs["effr"].batch_id,
            fetched_at=fetched_at,
        ),
        "iorb": _input_lineage(
            series_key="iorb",
            value="3.65",
            source_key="federal-reserve",
            batch_id=runs["iorb"].batch_id,
            fetched_at=fetched_at,
        ),
    }
    specs = (
        ("sofr", "SOFR", Decimal("3.53"), "%", "ny-fed-markets", ("sofr",), None),
        ("iorb", "IORB", Decimal("3.65"), "%", "federal-reserve", ("iorb",), None),
        (
            "sofr-effr",
            "SOFR−EFFR",
            Decimal("-9"),
            "bp",
            "internal",
            ("sofr", "effr"),
            "100 * (SOFR - EFFR)",
        ),
        (
            "sofr-iorb",
            "SOFR−IORB",
            Decimal("-12"),
            "bp",
            "internal",
            ("sofr", "iorb"),
            "100 * (SOFR - IORB)",
        ),
    )
    metrics = []
    publication_batch = uuid.uuid4()
    fresh_until = FED_DATE + timedelta(days=4)
    for key, label, value, unit, source_key, input_keys, formula in specs:
        inputs = [deepcopy(lineages[input_key]) for input_key in input_keys]
        input_batches = sorted({item["batch_id"] for item in inputs})
        source_keys = sorted(
            {
                *(item["source_key"] for item in inputs),
                *({"internal"} if source_key == "internal" else set()),
            }
        )
        metric = {
            "key": key,
            "label": label,
            "value": float(value),
            "display_value": f"{value}{unit}",
            "change": None,
            "change_unit": "",
            "unit": unit,
            "quality_status": "estimated" if source_key == "internal" else "fresh",
            "source": ensure_source(source_key).name,
            "source_key": source_key,
            "source_keys": source_keys,
            "fallback_source": None,
            "as_of": FED_DATE.isoformat(),
            "value_date": FED_DATE.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "fresh_until": fresh_until.isoformat(),
            "batch_id": ",".join(input_batches),
            "metadata": {
                "formula": formula,
                "common_effective_date": FED_DATE.date().isoformat(),
                "input_series": list(input_keys),
                "input_batch_ids": input_batches,
                "input_value_dates": [FED_DATE.isoformat()],
                "input_lineage": inputs,
                "calculation_owner": "Atlas Macro" if formula else None,
            },
        }
        metrics.append(metric)
        source = ensure_source(source_key)
        MetricSnapshot.objects.create(
            key=f"fed-funds-{key}",
            label=label,
            value=value,
            display_value=metric["display_value"],
            unit=unit,
            value_date=FED_DATE,
            as_of=FED_DATE,
            fetched_at=fetched_at,
            batch_id=publication_batch,
            source=source,
            quality_status=metric["quality_status"],
            license_scope=source.license_scope[:120],
            metadata={
                "component_batch_id": metric["batch_id"],
                "formula": formula,
                "common_effective_date": FED_DATE.date().isoformat(),
                "input_series": list(input_keys),
                "source_keys": source_keys,
                "input_batch_ids": input_batches,
                "input_value_dates": [FED_DATE.isoformat()],
                "input_lineage": inputs,
                "public_snapshot": True,
            },
        )
    fingerprint = hashlib.sha256(b"fed-funds-fixture").hexdigest()
    snapshot = DashboardSnapshot.objects.create(
        key="fed-funds",
        title="联邦基金利率",
        as_of=FED_DATE,
        batch_id=publication_batch,
        quality_status="estimated",
        summary="validated fixture",
        data={
            "demo": False,
            "metrics": metrics,
            "charts": [],
            "sections": [],
            "component_batches": sorted(
                str(run.batch_id) for run in runs.values()
            ),
            "source_keys": ["federal-reserve", "internal", "ny-fed-markets"],
            "publication_batch_id": str(publication_batch),
            "fingerprint": fingerprint,
        },
        source=internal,
        is_published=True,
    )
    return snapshot, runs


def _publish_complete_liquidity(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    fed_snapshot, fed_runs = _create_fed_funds_component()
    direct_runs = _record_direct_components(cycle="initial-cycle")
    dashboards, stale = _coordinate_liquidity_dashboard(direct_runs.values())
    assert stale == set()
    assert len(dashboards) == 1
    return dashboards[0], direct_runs, fed_snapshot, fed_runs


@pytest.mark.django_db
def test_liquidity_requires_coordinator_and_uses_latest_exact_common_date(monkeypatch):
    snapshot, direct_runs, fed_snapshot, fed_runs = _publish_complete_liquidity(
        monkeypatch
    )

    assert "liquidity" not in CORE_PUBLICATION_KEYS
    assert "liquidity" not in H41_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"liquidity"}) == []
    assert snapshot.data["contract_version"] == LIQUIDITY_CONTRACT_VERSION
    assert snapshot.data["common_effective_date"] == "2026-07-08"
    metrics = {item["key"]: item for item in snapshot.data["metrics"]}
    assert set(metrics) == set(LIQUIDITY_REQUIRED_METRIC_KEYS)
    assert metrics["net-liquidity"]["value"] == pytest.approx(5.983018)
    assert metrics["net-liquidity"]["display_value"] == "5.983018 USD tn"
    assert metrics["walcl"]["value"] == pytest.approx(6.735609)
    assert metrics["onrrp"]["value"] == pytest.approx(3.347)
    assert metrics["tga"]["value"] == pytest.approx(749.244)
    assert metrics["net-liquidity"]["value"] != pytest.approx(5.990427)

    metadata = metrics["net-liquidity"]["metadata"]
    assert metadata["formula"] == "WALCL - ONRRP - TGA"
    assert metadata["model_label"].endswith("not official LPI")
    assert {item["series_key"] for item in metadata["input_lineage"]} == {
        "walcl",
        "onrrp",
        "tga",
    }
    assert {item["value_date"] for item in metadata["input_lineage"]} == {
        "2026-07-08T00:00:00+00:00"
    }
    assert {item["raw_value"] for item in metadata["input_lineage"]} == {
        "6735609.00000000",
        "3347.00000000",
        "749244.00000000",
    }
    assert metadata["previous_value_date"] == "2026-07-01T00:00:00+00:00"
    assert len(metadata["previous_input_lineage"]) == 3

    chart = snapshot.data["charts"][0]
    assert chart["key"] == "net-liquidity-history"
    assert chart["data"][-1]["date"] == "2026-07-08"
    assert chart["data"][-1]["Net Liquidity"] == pytest.approx(5.983018)
    assert set(chart["batch_ids"]) == {
        str(direct_runs["h41"].batch_id),
        str(direct_runs["onrrp"].batch_id),
        str(direct_runs["tga"].batch_id),
    }
    assert set(snapshot.data["component_batches"]) == {
        *(str(run.batch_id) for run in direct_runs.values()),
        *(str(run.batch_id) for run in fed_runs.values()),
    }
    references = {
        item["component"]: item
        for item in snapshot.data["component_snapshots"]
    }
    assert references["fed-funds"]["snapshot_id"] == fed_snapshot.pk
    assert references["fed-funds"]["fingerprint"] == fed_snapshot.data["fingerprint"]

    normalized = MetricSnapshot.objects.get(
        key="liquidity-net-liquidity", batch_id=snapshot.batch_id
    )
    assert normalized.value == Decimal("5.983018")
    assert normalized.metadata["input_lineage"] == metadata["input_lineage"]
    assert normalized.metadata["previous_input_lineage"] == metadata[
        "previous_input_lineage"
    ]


@pytest.mark.django_db
def test_liquidity_failure_retains_last_complete_snapshot_and_names_component(
    monkeypatch,
):
    snapshot, _, _, _ = _publish_complete_liquidity(monkeypatch)
    original_batch = snapshot.batch_id
    original_metrics = deepcopy(snapshot.data["metrics"])
    original_fingerprint = snapshot.data["fingerprint"]
    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            error="upstream timeout",
        )
    )

    dashboards, stale = _coordinate_liquidity_dashboard([failed])

    assert dashboards == []
    assert stale == {"liquidity"}
    snapshot.refresh_from_db()
    assert snapshot.batch_id == original_batch
    assert snapshot.data["metrics"] == original_metrics
    assert snapshot.data["fingerprint"] == original_fingerprint
    assert snapshot.quality_status == "stale"
    failure = snapshot.data["refresh_failure"]
    assert any(
        item["component"] == "tga" and item["status"] == "failed"
        for item in failure["components"]
    )


@pytest.mark.django_db
def test_liquidity_same_value_recovery_refreshes_lineage_in_place(monkeypatch):
    snapshot, direct_runs, _, _ = _publish_complete_liquidity(monkeypatch)
    original_batch = snapshot.batch_id
    original_fingerprint = snapshot.data["fingerprint"]
    original_component_batches = set(snapshot.data["component_batches"])
    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            error="temporary failure",
        )
    )
    _coordinate_liquidity_dashboard([failed])

    recovered_runs = _record_direct_components(
        cycle="recovery-cycle",
        fetched_at=datetime(2026, 7, 12, 14, tzinfo=UTC),
    )
    dashboards, stale = _coordinate_liquidity_dashboard(
        recovered_runs.values()
    )

    assert dashboards == []
    assert stale == set()
    assert DashboardSnapshot.objects.filter(
        key="liquidity", data__contract_version=LIQUIDITY_CONTRACT_VERSION
    ).count() == 1
    snapshot.refresh_from_db()
    assert snapshot.batch_id == original_batch
    assert snapshot.data["fingerprint"] == original_fingerprint
    assert snapshot.quality_status == "estimated"
    assert "refresh_failure" not in snapshot.data
    assert set(snapshot.data["component_batches"]) != original_component_batches
    assert str(direct_runs["tga"].batch_id) not in snapshot.data[
        "component_batches"
    ]
    assert str(recovered_runs["tga"].batch_id) in snapshot.data[
        "component_batches"
    ]
    normalized = MetricSnapshot.objects.get(
        key="liquidity-net-liquidity", batch_id=original_batch
    )
    assert str(recovered_runs["tga"].batch_id) in normalized.metadata[
        "input_batch_ids"
    ]


@pytest.mark.django_db
def test_liquidity_changed_common_date_input_creates_new_snapshot(monkeypatch):
    first, _, _, _ = _publish_complete_liquidity(monkeypatch)
    changed_runs = _record_direct_components(
        cycle="changed-cycle", tga_common_value="750244"
    )

    dashboards, stale = _coordinate_liquidity_dashboard(changed_runs.values())

    assert stale == set()
    assert len(dashboards) == 1
    second = dashboards[0]
    assert second.batch_id != first.batch_id
    assert second.data["fingerprint"] != first.data["fingerprint"]
    metric = next(
        item
        for item in second.data["metrics"]
        if item["key"] == "net-liquidity"
    )
    assert metric["value"] == pytest.approx(5.982018)


@pytest.mark.django_db
def test_liquidity_revoked_source_keeps_previous_v1_and_route_shows_failure(
    client, monkeypatch
):
    snapshot, direct_runs, _, _ = _publish_complete_liquidity(monkeypatch)
    licence = SourceLicense.objects.get(
        source__key="treasury-fiscal-data", is_current=True
    )
    licence.status = "restricted"
    licence.public_display_allowed = False
    licence.save(
        update_fields=["status", "public_display_allowed", "updated_at"]
    )

    dashboards, stale = _coordinate_liquidity_dashboard(
        [direct_runs["h41"]]
    )

    assert dashboards == []
    assert stale == {"liquidity"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "stale"
    assert snapshot.data["common_effective_date"] == "2026-07-08"
    response = client.get("/liquidity/")
    content = response.content.decode()
    assert response.status_code == 200
    assert "失败组件" in content
    assert "direct-inputs" in content
    assert "5.983018" not in content
    assert '"Net Liquidity": 5.983018' not in content


@pytest.mark.django_db
def test_liquidity_v1_route_hides_newer_legacy_snapshot(client, monkeypatch):
    current, _, _, _ = _publish_complete_liquidity(monkeypatch)
    internal = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="liquidity",
        title="legacy",
        as_of=FIXED_NOW,
        quality_status="fresh",
        summary="legacy mixed-date page",
        data={
            "demo": False,
            "metrics": [
                {
                    "key": "lpi",
                    "label": "LPI",
                    "value": 5.6,
                    "display_value": "5.6 / 10",
                }
            ],
            "source_keys": ["internal"],
        },
        source=internal,
        is_published=True,
    )

    response = client.get("/liquidity/")
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["snapshot"].pk == current.pk
    assert content.count('class="metric-card"') == 9
    assert "5.983018 USD tn" in content
    assert "净流动性代理" in content
    assert "不是美联储官方 LPI" in content
    assert "5.6 / 10" not in content


@pytest.mark.django_db
def test_liquidity_route_never_falls_back_to_legacy_only_snapshot(client):
    internal = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="liquidity",
        title="legacy",
        as_of=FIXED_NOW,
        quality_status="fresh",
        summary="legacy mixed-date page",
        data={
            "demo": False,
            "metrics": [
                {
                    "key": "lpi",
                    "label": "LPI",
                    "value": 5.6,
                    "display_value": "5.6 / 10",
                }
            ],
            "source_keys": ["internal"],
        },
        source=internal,
        is_published=True,
    )

    response = client.get("/liquidity/")
    content = response.content.decode()

    assert response.status_code == 200
    assert not any(
        context.get("snapshot") is not None
        for context in response.context
        if "snapshot" in context
    )
    assert content.count('class="metric-card"') == 9
    assert "5.6 / 10" not in content
    assert "本页尚无通过来源许可与质量检查的可发布快照" in content


@pytest.mark.django_db
def test_liquidity_accepts_h41_main_and_prates_trigger_identities(monkeypatch):
    snapshot, direct_runs, _, fed_runs = _publish_complete_liquidity(monkeypatch)

    for trigger in (
        [direct_runs["h41"]],
        [direct_runs["onrrp"], direct_runs["tga"]],
        [fed_runs["iorb"]],
    ):
        dashboards, stale = _coordinate_liquidity_dashboard(trigger)
        assert dashboards == []
        assert stale == set()
        snapshot.refresh_from_db()
        assert snapshot.quality_status == "estimated"


@pytest.mark.django_db
def test_liquidity_mismatched_fiscal_cycle_never_publishes(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _create_fed_funds_component()
    runs = _record_direct_components(cycle="base-cycle")
    # A later successful TGA-only attempt cannot be paired with the older RRP cycle.
    newer_tga = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            fetched_at=FIXED_NOW,
            records=_records(
                "TGA",
                [
                    ("2026-07-01", "720000"),
                    ("2026-07-08", "749244"),
                    ("2026-07-09", "744637"),
                ],
            ),
            metadata={"refresh_cycle_id": "different-cycle"},
        ),
        persist=store_series_observations,
    )

    dashboards, stale = _coordinate_liquidity_dashboard([newer_tga])

    assert dashboards == []
    assert stale == {"liquidity"}
    assert not DashboardSnapshot.objects.filter(
        key="liquidity", data__contract_version=LIQUIDITY_CONTRACT_VERSION
    ).exists()
    assert runs["onrrp"].metadata["refresh_cycle_id"] == "base-cycle"


@pytest.mark.django_db
def test_liquidity_rejects_fallback_on_the_published_common_date(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _create_fed_funds_component()
    runs = _record_direct_components(cycle="fallback-cycle")
    common_tga = Observation.objects.get(
        series__key="tga",
        source__key="treasury-fiscal-data",
        value_date=datetime(2026, 7, 8, tzinfo=UTC),
    )
    common_tga.fallback_source = ensure_source("internal")
    common_tga.quality_status = Observation.Quality.FALLBACK
    common_tga.save(
        update_fields=["fallback_source", "quality_status", "updated_at"]
    )

    dashboards, stale = _coordinate_liquidity_dashboard([runs["h41"]])

    assert dashboards == []
    assert stale == {"liquidity"}
    assert not DashboardSnapshot.objects.filter(
        key="liquidity", data__contract_version=LIQUIDITY_CONTRACT_VERSION
    ).exists()


@pytest.mark.django_db
def test_liquidity_rejects_tampered_fed_child_lineage(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    fed_snapshot, _ = _create_fed_funds_component()
    direct_runs = _record_direct_components(cycle="fed-tamper-cycle")
    data = deepcopy(fed_snapshot.data)
    metric = next(
        item for item in data["metrics"] if item["key"] == "sofr-iorb"
    )
    metric["metadata"]["input_lineage"][0]["source_key"] = (
        "treasury-fiscal-data"
    )
    fed_snapshot.data = data
    fed_snapshot.save(update_fields=["data", "updated_at"])

    dashboards, stale = _coordinate_liquidity_dashboard(
        direct_runs.values()
    )

    assert dashboards == []
    assert stale == {"liquidity"}
    assert not DashboardSnapshot.objects.filter(
        key="liquidity", data__contract_version=LIQUIDITY_CONTRACT_VERSION
    ).exists()


@pytest.mark.django_db
def test_all_three_refresh_entrypoints_invoke_liquidity_coordinator(monkeypatch):
    class FailingProvider:
        def __init__(self, source_key):
            self.source_key = source_key

        def __getattr__(self, method_name):
            def failed_call(*_args, **_kwargs):
                return ProviderResult.failure(
                    self.source_key,
                    f"{method_name}:fixture",
                    "fixture failure",
                )

            return failed_call

        def close(self):
            return None

    provider_sources = {
        "NYFedMarketsProvider": "ny-fed-markets",
        "TreasuryRatesProvider": "us-treasury-rates",
            "FiscalDataProvider": "treasury-fiscal-data",
            "BLSProvider": "bls",
            "BEAPIOReleaseProvider": "bea-pio-release",
            "DOLWeeklyClaimsProvider": "dol-eta-ui",
            "FederalReserveRSSProvider": "federal-reserve",
            "FederalReserveH41Provider": "federal-reserve",
        "FederalReservePRATESProvider": "federal-reserve",
    }
    for class_name, source_key in provider_sources.items():
        monkeypatch.setattr(
            f"research.official_data.{class_name}",
            lambda source_key=source_key: FailingProvider(source_key),
        )
    calls: list[list[str]] = []

    def coordinate(runs):
        calls.append([run.dataset for run in runs])
        return [], set()

    monkeypatch.setattr(
        "research.official_data._coordinate_liquidity_dashboard", coordinate
    )
    monkeypatch.setattr(
        "research.official_data._coordinate_fed_funds_dashboard",
        lambda runs: ([], set()),
    )
    monkeypatch.setattr(
        "research.official_data._coordinate_economy_dashboard",
        lambda: ([], set()),
    )

    refresh_official_data(current_year=2026)
    refresh_h41_data()
    refresh_prates_data()

    assert len(calls) == 3
    assert len(calls[0]) == 14
    assert not any("yield_curve" in dataset for dataset in calls[0])
    assert calls[1] == ["h41:fixture"]
    assert calls[2] == ["iorb:fixture"]
