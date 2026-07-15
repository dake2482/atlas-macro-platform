from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import timezone
from test_global_dollar import _h10_result, _swap_result
from test_operations import (
    _component_results as _operations_component_results,
)
from test_operations import (
    _raw_result as _operations_raw_result,
)
from test_reserves_rate_spreads import (
    _rate_records,
    _weekly_records,
)
from test_subsurface import (
    _business_dates as _subsurface_business_dates,
)
from test_subsurface import (
    _record_components as _record_subsurface_components,
)

from research.data_catalog import DATA_REQUIREMENTS
from research.management.commands.sync_official_glossary import (
    OFFICIAL_GLOSSARY_TERMS,
)
from research.models import (
    DashboardSnapshot,
    DataRequirement,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    SourceLicense,
)
from research.official_data import (
    CORE_PUBLICATION_KEYS,
    PRATES_PUBLICATION_KEYS,
    TRANSMISSION_CHAIN_COMPONENTS,
    TRANSMISSION_CHAIN_REFRESH_FAILURE_REASONS,
    _coordinate_fed_balance_sheet_dashboard,
    _coordinate_global_dollar_dashboard,
    _coordinate_operations_dashboard,
    _coordinate_reserves_dashboard,
    _coordinate_reserves_rate_spreads_dashboard,
    _coordinate_subsurface_dashboard,
    _coordinate_transmission_chain_dashboard,
    _publish_dashboard,
    _publish_dashboard_core,
    _store_fed_balance_sheet_component_observations,
    _store_global_dollar_swap_observations,
    _store_h8_observations,
    _store_h10_observations,
    _store_h41_observations,
    _store_operations_ny_fed_observations,
    _transmission_artifact_evidence,
    _transmission_chain_content_fingerprint,
    _transmission_chain_payload_integrity_hash,
    _transmission_child_contract_is_valid,
    _transmission_component_envelope,
    _transmission_component_fetched_at,
    _transmission_semantic_manifest,
    _transmission_snapshot_mode,
    _without_exact_global_dollar_acquisition,
    _without_volatile_dashboard_lineage,
    publish_official_dashboards,
    refresh_h8_data,
    refresh_h10_data,
    refresh_h41_data,
    refresh_official_data,
    refresh_prates_data,
    select_public_transmission_chain_snapshot,
    transmission_chain_snapshot_is_publicly_displayable,
)
from research.page_registry import PAGE_CONFIGS
from research.providers import ProviderResult
from research.services import ensure_source, record_provider_result, store_series_observations


def _rehash_parent_data(snapshot, data):
    data["fingerprint"] = _transmission_chain_content_fingerprint(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=data,
    )
    data["payload_integrity_hash"] = (
        _transmission_chain_payload_integrity_hash(
            title=snapshot.title,
            summary=snapshot.summary,
            snapshot_data=data,
        )
    )


def _parent_input_run(snapshot, page_key, input_role):
    envelope = next(
        item
        for item in snapshot.data["component_snapshots"]
        if item["component_page_key"] == page_key
    )
    run_state = next(
        item
        for item in envelope["input_runs"]
        if item["input_role"] == input_role
    )
    return IngestionRun.objects.select_related("source").get(pk=run_state["run_id"])


def _successor_attempt(
    referenced,
    *,
    status,
    row_count=0,
    started_at=None,
):
    started_at = started_at or referenced.started_at + timedelta(seconds=1)
    completed_at = (
        None
        if status == IngestionRun.Status.RUNNING
        else started_at + timedelta(seconds=1)
    )
    return IngestionRun.objects.create(
        source=referenced.source,
        dataset=referenced.dataset,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        row_count=row_count,
        error="fixture terminal failure"
        if status == IngestionRun.Status.FAILED
        else "",
    )


@pytest.fixture
def published_transmission_chain(db, monkeypatch, tmp_path):
    now = datetime(2026, 7, 14, 10, tzinfo=UTC)
    cycle = "transmission-chain-integration"
    monkeypatch.setattr("research.official_data.timezone.now", lambda: now)
    monkeypatch.setattr("research.services.timezone.now", lambda: now)
    monkeypatch.setattr("research.views.timezone.now", lambda: now)
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)

    # SOFR and IORB use the strict subsurface fixtures.  Its temporary SRF and
    # swap attempts are removed before the shared, stricter operations/global
    # inputs are recorded, leaving one authoritative run per dataset.
    subsurface_seed = _record_subsurface_components(monkeypatch, cycle=cycle)
    for role in ("srf", "swaps"):
        temporary = subsurface_seed.pop(role)
        Observation.objects.filter(batch_id=temporary.batch_id).delete()
        temporary.delete()

    operation_results = _operations_component_results(cycle)
    onrrp_result = operation_results["onrrp"]
    existing_onrrp_dates = {
        str(item["date"])
        for item in onrrp_result.records
        if item.get("series_id") == "ONRRP"
    }
    template_date = min(existing_onrrp_dates)
    onrrp_templates = [
        item for item in onrrp_result.records if item.get("date") == template_date
    ]
    weekly_rows = _weekly_records("WRBWFRBL", count=60)
    for row in weekly_rows:
        if row["date"] in existing_onrrp_dates:
            continue
        for template in onrrp_templates:
            copied = deepcopy(template)
            copied["date"] = row["date"]
            for operation in (copied.get("metadata") or {}).get(
                "operations", []
            ):
                operation["operationDate"] = row["date"]
                operation["operationId"] = (
                    f"{operation.get('operationId', 'RRP')}-{row['date']}"
                )
            onrrp_result.records.append(copied)
    operation_results["onrrp"] = _operations_raw_result(
        dataset="repo:reverse-repo-fixed-results",
        records=onrrp_result.records,
        cycle=cycle,
    )
    operation_runs = {
        role: record_provider_result(
            result,
            persist=_store_operations_ny_fed_observations,
        )
        for role, result in operation_results.items()
    }

    h10_result = _h10_result()
    h10_result.fetched_at = now - timedelta(hours=1)
    h10_run = record_provider_result(
        h10_result, persist=_store_h10_observations
    )
    swap_result = _swap_result()
    swap_result.fetched_at = now - timedelta(hours=1)
    swap_result.metadata = {
        **dict(swap_result.metadata or {}),
        "refresh_cycle_id": cycle,
    }
    swap_run = record_provider_result(
        swap_result,
        persist=_store_global_dollar_swap_observations,
    )

    board_ids = {
        "WALCL": "RESPPMA_N.WW",
        "WSHOTSL": "RESPPALGUO_N.WW",
        "WSHOMCB": "RESPPALGASMO_N.WW",
    }
    h41_records = list(weekly_rows)
    for series_index, (series_id, board_id) in enumerate(board_ids.items(), start=1):
        for index, row in enumerate(weekly_rows):
            value = 6_500_000 - series_index * 500_000 + index * 1_000
            h41_records.append(
                {
                    "series_id": series_id,
                    "source_series_id": board_id,
                    "date": row["date"],
                    "value": value,
                    "metadata": {
                        "board_series_id": board_id,
                        "raw_value": str(value),
                        "unit_multiplier": "1000000",
                        "currency": "USD",
                    },
                }
            )
    release_metadata = {
        "reserves_refresh_id": cycle,
        "prepared_at": "2026-07-10T19:00:00+00:00",
    }
    h41_run = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="h41",
            fetched_at=datetime(2026, 7, 10, 20, tzinfo=UTC),
            records=h41_records,
            metadata=release_metadata,
        ),
        persist=_store_h41_observations,
    )
    h8_run = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="h8",
            fetched_at=datetime(2026, 7, 10, 20, tzinfo=UTC),
            records=_weekly_records("H8-B1151NCBA", count=60),
            metadata={
                "reserves_refresh_id": cycle,
                "prepared_at": "2026-07-10T19:30:00+00:00",
            },
        ),
        persist=_store_h8_observations,
    )
    tga_run = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            fetched_at=datetime(2026, 7, 10, 12, tzinfo=UTC),
            records=[
                *[
                    {
                        "series_id": "TGA",
                        "date": row["date"],
                        "value": 700_000 + index * 100,
                    }
                    for index, row in enumerate(weekly_rows)
                ],
                {
                    "series_id": "TGA",
                    "date": "2026-07-13",
                    "value": 706_100,
                },
            ],
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=_store_fed_balance_sheet_component_observations,
    )

    rate_dates = _subsurface_business_dates(65)
    tbill_run = record_provider_result(
        ProviderResult(
            provider="us-treasury-rates",
            dataset="treasury-bill-rates:13w-coupon-equivalent",
            fetched_at=datetime(2026, 7, 13, 20, tzinfo=UTC),
            records=_rate_records("tbill", rate_dates),
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )

    child_results = [
        _coordinate_fed_balance_sheet_dashboard(
            [h41_run, operation_runs["onrrp"], tga_run]
        ),
        _coordinate_operations_dashboard(operation_runs.values()),
        _coordinate_reserves_rate_spreads_dashboard([tbill_run]),
        _coordinate_subsurface_dashboard(
            [
                subsurface_seed["sofr"],
                subsurface_seed["iorb"],
                operation_runs["srf"],
                swap_run,
            ]
        ),
        _coordinate_reserves_dashboard([h8_run]),
        _coordinate_global_dollar_dashboard([h10_run, swap_run]),
    ]
    assert all(stale == set() for _dashboards, stale in child_results), [
        ([item.key for item in dashboards], stale)
        for dashboards, stale in child_results
    ]

    component_snapshots = {
        page_key: DashboardSnapshot.objects.filter(key=page_key)
        .order_by("-created_at", "-id")
        .first()
        for page_key in TRANSMISSION_CHAIN_COMPONENTS
    }
    assert all(component_snapshots.values())
    assert all(
        _transmission_child_contract_is_valid(
            child, mode="current_candidate"
        )
        for child in component_snapshots.values()
    )

    dashboards, stale = _coordinate_transmission_chain_dashboard()
    assert stale == set()
    assert len(dashboards) == 1
    return dashboards[0]


@pytest.mark.django_db
def test_existing_artifact_with_unknown_uri_fails_closed(monkeypatch):
    source = ensure_source("federal-reserve")
    payload = b"official-input"
    digest = hashlib.sha256(payload).hexdigest()
    now = timezone.now()
    run = IngestionRun.objects.create(
        source=source,
        dataset="h10",
        started_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=1),
        status=IngestionRun.Status.SUCCESS,
        row_count=1,
        metadata={"sha256": digest},
    )
    RawArtifact.objects.create(
        run=run,
        uri="https://example.invalid/artifact-without-hash",
        sha256=digest,
        content_type="application/octet-stream",
        size_bytes=len(payload),
    )
    monkeypatch.setattr(
        "research.official_data._transmission_component_datasets",
        lambda _page_key: {"h10": ("federal-reserve", "h10")},
    )

    assert _transmission_artifact_evidence("global-dollar", {"h10": run}) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("artifact_mode", "expected_status"),
    [
        ("private", "PRIVATE_BYTES"),
        ("pointer", "HASH_POINTER_ONLY"),
        ("missing", "NOT_AVAILABLE_UNDER_CHILD_V1"),
    ],
)
def test_artifact_evidence_distinguishes_all_three_child_v1_states(
    monkeypatch,
    tmp_path,
    artifact_mode,
    expected_status,
):
    source = ensure_source("federal-reserve")
    payload = b"official-input"
    digest = hashlib.sha256(payload).hexdigest()
    now = timezone.now()
    run = IngestionRun.objects.create(
        source=source,
        dataset="h10",
        started_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=1),
        status=IngestionRun.Status.SUCCESS,
        row_count=1,
        metadata={"sha256": digest},
    )
    if artifact_mode != "missing":
        uri = (
            f"private://sha256/{digest}"
            if artifact_mode == "private"
            else f"https://example.invalid/archive?sha256={digest}"
        )
        RawArtifact.objects.create(
            run=run,
            uri=uri,
            sha256=digest,
            content_type="application/octet-stream",
            size_bytes=len(payload),
        )
    if artifact_mode == "private":
        artifact_path = tmp_path / digest[:2] / f"{digest}.bin"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(payload)
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(
        "research.official_data._transmission_component_datasets",
        lambda _page_key: {"h10": ("federal-reserve", "h10")},
    )

    evidence = _transmission_artifact_evidence(
        "global-dollar",
        {"h10": run},
    )

    assert evidence is not None
    assert [item["artifact_status"] for item in evidence] == [expected_status]


@pytest.mark.django_db
def test_component_envelope_is_json_native_across_round_trip(monkeypatch):
    source = ensure_source("federal-reserve")
    now = datetime(2026, 7, 14, 10, tzinfo=UTC)
    run = IngestionRun.objects.create(
        source=source,
        dataset="h41",
        started_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=1),
        status=IngestionRun.Status.SUCCESS,
        row_count=1,
    )
    child_batch = uuid.uuid4()
    metric = SimpleNamespace(
        pk=7,
        key="fed-balance-sheet-walcl",
        batch_id=child_batch,
    )
    snapshot = SimpleNamespace(
        key="fed-balance-sheet",
        pk=11,
        batch_id=child_batch,
        as_of=now,
        data={
            "contract_version": 1,
            "fingerprint": "f" * 64,
            "payload_integrity_hash": "e" * 64,
            "source_keys": ["federal-reserve"],
            "fresh_until": (now + timedelta(days=1)).isoformat(),
            "metrics": [{"fetched_at": now.isoformat()}],
        },
    )
    monkeypatch.setattr(
        "research.official_data._transmission_component_datasets",
        lambda _page_key: {"h41": ("federal-reserve", "h41")},
    )
    monkeypatch.setattr(
        "research.official_data._transmission_component_runs_from_snapshot",
        lambda _snapshot: {"h41": run},
    )
    monkeypatch.setattr(
        "research.official_data._transmission_component_metric_rows",
        lambda _snapshot: {"walcl": metric},
    )
    monkeypatch.setattr(
        "research.official_data._transmission_artifact_evidence",
        lambda _page_key, _runs: [
            {
                "component_page_key": "fed-balance-sheet",
                "input_role": "h41",
                "source": "federal-reserve",
                "dataset": "h41",
                "run_id": run.pk,
                "batch_id": str(run.batch_id),
                "artifact_status": "NOT_AVAILABLE_UNDER_CHILD_V1",
                "artifacts": [{"reason": "child v1 has no artifact row"}],
            }
        ],
    )

    envelope = _transmission_component_envelope(snapshot)

    assert envelope is not None
    assert envelope["datasets"] == [
        {"source": "federal-reserve", "dataset": "h41"}
    ]
    assert json.loads(json.dumps(envelope)) == envelope


def test_component_fetched_at_compares_instants_not_iso_text():
    snapshot = SimpleNamespace(
        data={
            "metrics": [
                {"fetched_at": "2026-07-14T20:00:00+09:00"},
                {"fetched_at": "2026-07-14T12:30:00+00:00"},
            ],
            "refresh_failure": {"fetched_at": "2099-01-01T00:00:00+00:00"},
        }
    )

    assert _transmission_component_fetched_at(snapshot) == datetime(
        2026, 7, 14, 12, 30, tzinfo=UTC
    )


def test_child_shadow_fields_and_natural_expiry_marker_fail_closed(monkeypatch):
    component = TRANSMISSION_CHAIN_COMPONENTS["fed-balance-sheet"]
    base = {
        "demo": False,
        "contract_version": component["contract_version"],
        "metrics": [
            {"key": key}
            for key in (
                "walcl",
                "wshotsl",
                "wshomcb",
                "wrbwfrbl",
                "net-liquidity",
            )
        ],
        "charts": [
            {"key": "fed-balance-sheet-history"},
            {"key": "fed-balance-sheet-net-liquidity-history"},
        ],
        "fingerprint": "f" * 64,
        "payload_integrity_hash": "e" * 64,
        "fresh_until": (timezone.now() - timedelta(hours=1)).isoformat(),
    }
    snapshot = SimpleNamespace(
        key="fed-balance-sheet",
        is_published=True,
        source=SimpleNamespace(key="internal"),
        quality_status=Observation.Quality.STALE,
        data={**base, "title": "shadow"},
    )
    monkeypatch.setattr(
        "research.official_data._transmission_component_runs_from_snapshot",
        lambda _snapshot: {"h41": SimpleNamespace(pk=1)},
    )
    monkeypatch.setattr(
        "research.official_data._transmission_child_payload_hash",
        lambda _snapshot: "e" * 64,
    )
    monkeypatch.setattr(
        "research.official_data._transmission_child_fingerprint",
        lambda _snapshot: "f" * 64,
    )
    monkeypatch.setattr(
        "research.official_data._transmission_child_licences_are_current",
        lambda _snapshot: True,
    )
    monkeypatch.setattr(
        "research.official_data._transmission_artifact_evidence",
        lambda _page_key, _runs: [],
    )
    monkeypatch.setattr(
        "research.official_data._transmission_runs_are_latest",
        lambda _page_key, _runs: True,
    )

    assert not _transmission_child_contract_is_valid(
        snapshot, mode="natural_expiry"
    )
    snapshot.data = {**base, "refresh_failure": {"forged": True}}
    assert not _transmission_child_contract_is_valid(
        snapshot, mode="natural_expiry"
    )


@pytest.mark.parametrize(
    ("expected_mode", "deadline_delta", "marker", "quality", "newer_child"),
    [
        ("current_candidate", 1, None, Observation.Quality.ESTIMATED, False),
        ("natural_expiry", -1, None, Observation.Quality.STALE, False),
        ("retained_failure", 1, {"audit": True}, Observation.Quality.STALE, False),
        ("transition_pending", 1, None, Observation.Quality.ESTIMATED, True),
    ],
)
def test_parent_selector_distinguishes_four_states(
    monkeypatch,
    expected_mode,
    deadline_delta,
    marker,
    quality,
    newer_child,
):
    components = {
        page_key: SimpleNamespace(pk=index)
        for index, page_key in enumerate(TRANSMISSION_CHAIN_COMPONENTS, start=1)
    }
    data = {
        "fresh_until": (
            timezone.now() + timedelta(hours=deadline_delta)
        ).isoformat(),
        "metrics": [{"quality_status": Observation.Quality.ESTIMATED}],
        "charts": [{"quality_status": Observation.Quality.ESTIMATED}],
    }
    if marker is not None:
        data["refresh_failure"] = marker
    snapshot = SimpleNamespace(data=data, quality_status=quality)
    monkeypatch.setattr(
        "research.official_data._transmission_parent_static_contract",
        lambda _snapshot: components,
    )

    def latest_child(page_key, *, mode="current_candidate"):
        if newer_child and mode == "current_candidate" and page_key == "operations":
            return SimpleNamespace(pk=999)
        return components[page_key]

    monkeypatch.setattr(
        "research.official_data._transmission_latest_valid_child", latest_child
    )
    monkeypatch.setattr(
        "research.official_data._transmission_child_contract_is_valid",
        lambda _child, *, mode: True,
    )
    monkeypatch.setattr(
        "research.official_data._transmission_refresh_failure_is_valid",
        lambda _snapshot, *, require_current_attempts: True,
    )
    monkeypatch.setattr(
        "research.official_data._transmission_transition_is_valid",
        lambda _snapshot, _components: newer_child,
    )

    assert _transmission_snapshot_mode(snapshot) == expected_mode


def test_six_real_children_publish_one_exact_parent(published_transmission_chain):
    snapshot = published_transmission_chain

    assert select_public_transmission_chain_snapshot([snapshot]).pk == snapshot.pk
    assert len(snapshot.data["metrics"]) == 12
    assert len(snapshot.data["charts"]) == 6
    assert len(snapshot.data["sections"]) == 3
    assert len(snapshot.data["component_snapshots"]) == 6
    assert len(snapshot.data["shared_input_reconciliation"]) == 6
    child_as_of = [
        datetime.fromisoformat(item["as_of"])
        for item in snapshot.data["component_snapshots"]
    ]
    child_fresh_until = [
        datetime.fromisoformat(item["fresh_until"])
        for item in snapshot.data["component_snapshots"]
    ]
    assert snapshot.as_of == min(child_as_of)
    assert datetime.fromisoformat(snapshot.data["as_of"]) == min(child_as_of)
    assert datetime.fromisoformat(snapshot.data["fresh_until"]) == min(
        child_fresh_until
    )
    direct = next(
        item
        for item in snapshot.data["metrics"]
        if item["key"] == "balance-sheet-total-assets"
    )
    derived = next(
        item
        for item in snapshot.data["metrics"]
        if item["key"] == "balance-sheet-net-liquidity"
    )
    assert len(direct["metadata"]["component_input_runs"]) == 1
    assert len(direct["metadata"]["input_datasets"]) == 1
    assert len(derived["metadata"]["component_input_runs"]) == 3
    assert len(derived["metadata"]["input_datasets"]) == 3
    assert DashboardSnapshot.objects.filter(key="transmission-chain").count() == 1

    duplicate, stale = _coordinate_transmission_chain_dashboard()

    assert duplicate == []
    assert stale == set()
    assert DashboardSnapshot.objects.filter(key="transmission-chain").count() == 1


@pytest.mark.django_db
def test_generic_publishers_cannot_write_transmission_chain():
    assert "transmission-chain" not in CORE_PUBLICATION_KEYS
    assert "transmission-chain" not in PRATES_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"transmission-chain"}) == []
    with pytest.raises(ValueError, match="dedicated v1 publisher"):
        _publish_dashboard(
            key="transmission-chain",
            title="forged",
            summary="forged",
            metrics=[],
            batch_id=uuid.uuid4(),
        )
    with pytest.raises(TypeError, match="independent_contract"):
        _publish_dashboard(
            key="transmission-chain",
            title="forged",
            summary="forged",
            metrics=[],
            batch_id=uuid.uuid4(),
            independent_contract=True,
        )
    with pytest.raises(ValueError, match="dedicated v1 publisher"):
        _publish_dashboard_core(
            key="transmission-chain",
            title="forged",
            summary="forged",
            metrics=[],
            batch_id=uuid.uuid4(),
        )


def test_transmission_semantic_manifest_excludes_exact_acquisition_identity(
    published_transmission_chain,
):
    snapshot = published_transmission_chain
    data = deepcopy(snapshot.data)
    envelopes = {
        item["component_page_key"]: item
        for item in deepcopy(data["component_snapshots"])
    }
    reconciliation = deepcopy(data["shared_input_reconciliation"])
    baseline = _transmission_semantic_manifest(
        metrics=deepcopy(data["metrics"]),
        charts=deepcopy(data["charts"]),
        sections=deepcopy(data["sections"]),
        envelopes=envelopes,
        reconciliation=reconciliation,
    )
    assert baseline == data["semantic_manifest"]

    for envelope in envelopes.values():
        envelope["component_snapshot_id"] += 100_000
        envelope["component_publication_batch_id"] = str(uuid.uuid4())
        for run in envelope["input_runs"]:
            run["run_id"] += 100_000
            run["batch_id"] = str(uuid.uuid4())
        for evidence in envelope["artifact_evidence"]:
            evidence["run_id"] += 100_000
            evidence["batch_id"] = str(uuid.uuid4())
            for artifact in evidence["artifacts"]:
                artifact["artifact_id"] = 999_999
                artifact["uri"] = "private://forged-exact-row"
    for row in reconciliation:
        row["run_id"] += 100_000
        row["batch_id"] = str(uuid.uuid4())
        for evidence in row["artifact_identity"]:
            for artifact in evidence["artifacts"]:
                artifact["artifact_id"] = 999_999
                artifact["uri"] = "private://forged-exact-row"

    recovered_semantics = _transmission_semantic_manifest(
        metrics=deepcopy(data["metrics"]),
        charts=deepcopy(data["charts"]),
        sections=deepcopy(data["sections"]),
        envelopes=envelopes,
        reconciliation=reconciliation,
    )
    assert recovered_semantics == baseline

    semantic_keys: set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            semantic_keys.update(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(recovered_semantics)
    assert semantic_keys.isdisjoint(
        {"artifact_id", "artifact_identity", "uri", "run_id", "batch_id"}
    )

    exact_data = deepcopy(data)
    exact_data["component_snapshots"] = [
        envelopes[page_key] for page_key in TRANSMISSION_CHAIN_COMPONENTS
    ]
    exact_data["shared_input_reconciliation"] = reconciliation
    exact_data["semantic_manifest"] = recovered_semantics
    assert _transmission_chain_payload_integrity_hash(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=exact_data,
    ) != _transmission_chain_payload_integrity_hash(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=data,
    )


def test_transmission_data_catalog_exposes_all_five_honest_states():
    requirements = {
        item["key"]: item
        for item in DATA_REQUIREMENTS
        if item["page_key"] == "transmission-chain"
    }

    assert "liquidity-transmission-inputs" not in requirements
    assert {item["status"] for item in requirements.values()} == {
        DataRequirement.Status.LIVE,
        DataRequirement.Status.PROXY,
        DataRequirement.Status.NEEDS_SOURCE,
        DataRequirement.Status.LICENSE_REVIEW,
        DataRequirement.Status.PURCHASE_REQUIRED,
    }
    assert requirements["transmission-official-evidence-v1"]["status"] == (
        DataRequirement.Status.LIVE
    )
    assert requirements["transmission-transparent-proxies-v1"]["status"] == (
        DataRequirement.Status.PROXY
    )
    assert requirements["transmission-cross-currency-basis"]["status"] == (
        DataRequirement.Status.PURCHASE_REQUIRED
    )


def test_registry_and_glossary_remove_legacy_scoring_language():
    registry_text = json.dumps(
        PAGE_CONFIGS["transmission-chain"],
        ensure_ascii=False,
    )
    glossary = next(
        item
        for item in OFFICIAL_GLOSSARY_TERMS
        if item["slug"] == "transmission-chain"
    )
    glossary_text = json.dumps(glossary, ensure_ascii=False)

    for legacy in (
        "5.6 / 10",
        "4 / 10",
        "6 / 10",
        "偏紧",
        "缓冲下降",
        "收缩",
        "共振",
        "分层评分",
        "行动建议",
    ):
        assert legacy not in registry_text
        assert legacy not in glossary_text


@pytest.mark.parametrize(
    ("entrypoint", "required_children"),
    [
        (
            refresh_official_data,
            (
                "_coordinate_fed_balance_sheet_dashboard",
                "_coordinate_operations_dashboard",
                "_coordinate_reserves_rate_spreads_dashboard",
                "_coordinate_subsurface_dashboard",
                "_coordinate_assets_fx_dashboard",
                "coordinate_fx_vol_dashboard",
                "_coordinate_global_dollar_dashboard",
            ),
        ),
        (
            refresh_h41_data,
            (
                "_coordinate_fed_balance_sheet_dashboard",
                "_coordinate_reserves_dashboard",
            ),
        ),
        (refresh_h8_data, ("_coordinate_reserves_dashboard",)),
        (
            refresh_prates_data,
            (
                "_coordinate_reserves_rate_spreads_dashboard",
                "_coordinate_subsurface_dashboard",
            ),
        ),
        (
            refresh_h10_data,
            (
                "_coordinate_assets_fx_dashboard",
                "coordinate_fx_vol_dashboard",
                "_coordinate_global_dollar_dashboard",
            ),
        ),
    ],
)
def test_refresh_entrypoints_order_parent_after_relevant_children(
    entrypoint,
    required_children,
):
    source = inspect.getsource(entrypoint)
    parent_position = source.index("_coordinate_transmission_chain_dashboard")

    assert all(source.index(child) < parent_position for child in required_children)


def test_h10_refresh_orders_assets_fx_before_global_dollar_and_parent():
    source = inspect.getsource(refresh_h10_data)

    assert source.index("_coordinate_assets_fx_dashboard") < source.index(
        "coordinate_fx_vol_dashboard"
    ) < source.index("_coordinate_global_dollar_dashboard") < source.index(
        "_coordinate_transmission_chain_dashboard"
    )


def test_main_refresh_orders_assets_fx_before_fx_vol_and_parent():
    source = inspect.getsource(refresh_official_data)

    assert source.index("_coordinate_assets_fx_dashboard") < source.index(
        "coordinate_fx_vol_dashboard"
    ) < source.index("_coordinate_global_dollar_dashboard") < source.index(
        "_coordinate_transmission_chain_dashboard"
    )


def test_main_official_refresh_reuses_latest_h10_without_fetching_it():
    source = inspect.getsource(refresh_official_data)

    assert "_coordinate_assets_fx_dashboard" in source
    assert "FederalReserveH10Provider" not in source


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tamper",
    [
        "metric-exact-set",
        "chart-copy",
        "table-cell",
        "component-reference",
        "shared-input",
    ],
)
def test_parent_rejects_rehashed_nested_tampering(
    published_transmission_chain,
    tamper,
):
    snapshot = published_transmission_chain
    data = deepcopy(snapshot.data)
    if tamper == "metric-exact-set":
        data["metrics"].pop()
    elif tamper == "chart-copy":
        data["charts"][0]["title"] = "forged copied chart"
    elif tamper == "table-cell":
        data["sections"][0]["rows"][0]["cells_list"][0]["cell"][
            "value"
        ] = "forged layer"
    elif tamper == "component-reference":
        data["component_snapshots"][0]["component_snapshot_id"] += 999_999
    else:
        data["shared_input_reconciliation"][0]["batch_id"] = str(
            uuid.uuid4()
        )
    _rehash_parent_data(snapshot, data)
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])

    assert not transmission_chain_snapshot_is_publicly_displayable(snapshot)
    assert select_public_transmission_chain_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_parent_rejects_outer_metric_and_licence_tampering(
    published_transmission_chain,
):
    snapshot = published_transmission_chain
    normalized = MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).first()
    original_metadata = deepcopy(normalized.metadata)
    normalized.metadata = {**original_metadata, "public_snapshot": False}
    normalized.save(update_fields=["metadata", "updated_at"])

    assert select_public_transmission_chain_snapshot([snapshot]) is None

    normalized.metadata = original_metadata
    normalized.save(update_fields=["metadata", "updated_at"])
    licence = SourceLicense.objects.get(
        source__key="federal-reserve",
        is_current=True,
    )
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])

    assert select_public_transmission_chain_snapshot([snapshot]) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("metric_key", "field"),
    [
        ("reserves-reserve-commercial-bank-assets-ratio", "display_value"),
        (
            "reserves-rate-spreads-reserves-sofr-iorb-spread",
            "license_scope",
        ),
    ],
)
def test_parent_rejects_exact_child_metric_row_tampering(
    published_transmission_chain,
    metric_key,
    field,
):
    snapshot = published_transmission_chain
    row = MetricSnapshot.objects.get(key=metric_key)
    setattr(row, field, "forged normalized field")
    row.save(update_fields=[field, "updated_at"])

    assert select_public_transmission_chain_snapshot([snapshot]) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("page_key", "input_role", "series_key", "delta"),
    [
        ("reserves", "h8", "h8-b1151ncba", Decimal("1")),
        (
            "reserves-rate-spreads",
            "sofr",
            "sofr",
            Decimal("0.01"),
        ),
    ],
)
def test_parent_and_child_reject_exact_observation_tampering_without_payload_change(
    published_transmission_chain,
    page_key,
    input_role,
    series_key,
    delta,
):
    parent = published_transmission_chain
    envelope = next(
        item
        for item in parent.data["component_snapshots"]
        if item["component_page_key"] == page_key
    )
    run = next(
        item
        for item in envelope["input_runs"]
        if item["input_role"] == input_role
    )
    child = DashboardSnapshot.objects.get(
        pk=envelope["component_snapshot_id"]
    )
    immutable_hashes = (
        child.data["fingerprint"],
        child.data["payload_integrity_hash"],
    )
    observation = Observation.objects.filter(
        series__key=series_key,
        batch_id=run["batch_id"],
    ).latest("value_date", "id")
    observation.value += delta
    observation.save(update_fields=["value", "updated_at"])
    child.refresh_from_db()

    assert (
        child.data["fingerprint"],
        child.data["payload_integrity_hash"],
    ) == immutable_hashes
    assert not _transmission_child_contract_is_valid(
        child,
        mode="current_candidate",
    )
    assert select_public_transmission_chain_snapshot([parent]) is None


@pytest.mark.django_db
def test_markerless_running_successor_is_transition_and_coordinator_writes_no_marker(
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    immutable_hashes = (
        snapshot.data["fingerprint"],
        snapshot.data["payload_integrity_hash"],
    )
    referenced = _parent_input_run(snapshot, "global-dollar", "h10")
    running = _successor_attempt(
        referenced,
        status=IngestionRun.Status.RUNNING,
    )
    now = running.started_at + timedelta(seconds=1)
    monkeypatch.setattr("research.official_data.timezone.now", lambda: now)

    selected = select_public_transmission_chain_snapshot([snapshot])

    assert selected is not None
    assert selected.transmission_chain_state == "transition_pending"
    assert selected.transmission_chain_changed_components == {"global-dollar"}

    dashboards, stale = _coordinate_transmission_chain_dashboard()

    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    assert "refresh_failure" not in snapshot.data
    assert (
        snapshot.data["fingerprint"],
        snapshot.data["payload_integrity_hash"],
    ) == immutable_hashes
    assert (
        select_public_transmission_chain_snapshot([snapshot])
        .transmission_chain_state
        == "transition_pending"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL],
)
def test_markerless_terminal_successor_is_rejected(
    published_transmission_chain,
    monkeypatch,
    status,
):
    snapshot = published_transmission_chain
    referenced = _parent_input_run(snapshot, "global-dollar", "h10")
    terminal = _successor_attempt(
        referenced,
        status=status,
        row_count=1 if status == IngestionRun.Status.PARTIAL else 0,
    )
    now = terminal.completed_at + timedelta(seconds=1)
    monkeypatch.setattr("research.official_data.timezone.now", lambda: now)

    assert select_public_transmission_chain_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_old_failure_marker_accepts_its_direct_running_successor_transition(
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    immutable_hashes = (
        snapshot.data["fingerprint"],
        snapshot.data["payload_integrity_hash"],
    )
    referenced = _parent_input_run(snapshot, "global-dollar", "h10")
    failure = _successor_attempt(
        referenced,
        status=IngestionRun.Status.FAILED,
    )
    failure_checked_at = failure.completed_at + timedelta(seconds=1)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: failure_checked_at
    )
    dashboards, stale = _coordinate_transmission_chain_dashboard()
    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    marker = deepcopy(snapshot.data["refresh_failure"])

    running = _successor_attempt(
        failure,
        status=IngestionRun.Status.RUNNING,
        started_at=failure_checked_at + timedelta(seconds=1),
    )
    running_checked_at = running.started_at + timedelta(seconds=1)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: running_checked_at
    )

    selected = select_public_transmission_chain_snapshot([snapshot])

    assert selected is not None
    assert selected.transmission_chain_state == "transition_pending"
    assert selected.transmission_chain_changed_components == {"global-dollar"}
    snapshot.refresh_from_db()
    assert snapshot.data["refresh_failure"] == marker
    assert (
        snapshot.data["fingerprint"],
        snapshot.data["payload_integrity_hash"],
    ) == immutable_hashes


@pytest.mark.django_db
def test_terminal_marker_and_other_running_successor_remain_transition_pending(
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    immutable_hashes = (
        snapshot.data["fingerprint"],
        snapshot.data["payload_integrity_hash"],
    )
    h10_reference = _parent_input_run(snapshot, "global-dollar", "h10")
    failure = _successor_attempt(
        h10_reference,
        status=IngestionRun.Status.FAILED,
    )
    failure_checked_at = failure.completed_at + timedelta(seconds=1)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: failure_checked_at
    )
    dashboards, stale = _coordinate_transmission_chain_dashboard()
    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    marker = deepcopy(snapshot.data["refresh_failure"])

    h8_reference = _parent_input_run(snapshot, "reserves", "h8")
    running = _successor_attempt(
        h8_reference,
        status=IngestionRun.Status.RUNNING,
        started_at=failure_checked_at + timedelta(seconds=1),
    )
    running_checked_at = running.started_at + timedelta(seconds=1)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: running_checked_at
    )

    selected = select_public_transmission_chain_snapshot([snapshot])

    assert selected is not None
    assert selected.transmission_chain_state == "transition_pending"
    assert selected.transmission_chain_changed_components == {
        "global-dollar",
        "reserves",
    }
    dashboards, stale = _coordinate_transmission_chain_dashboard()
    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    assert snapshot.data["refresh_failure"] == marker
    assert (
        snapshot.data["fingerprint"],
        snapshot.data["payload_integrity_hash"],
    ) == immutable_hashes


@pytest.mark.django_db
def test_all_success_component_validation_marks_only_changed_route_layer(
    client,
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    referenced = _parent_input_run(snapshot, "global-dollar", "h10")
    successor = _successor_attempt(
        referenced,
        status=IngestionRun.Status.SUCCESS,
        row_count=1,
    )
    now = successor.completed_at + timedelta(seconds=1)
    monkeypatch.setattr("research.official_data.timezone.now", lambda: now)
    monkeypatch.setattr("research.views.timezone.now", lambda: now)

    dashboards, stale = _coordinate_transmission_chain_dashboard()

    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    assert snapshot.data["refresh_failure"]["reason_code"] == (
        "component-validation"
    )
    assert all(
        item["status"] == IngestionRun.Status.SUCCESS
        for item in snapshot.data["refresh_failure"]["attempts"]
    )

    response = client.get("/liquidity/transmission-chain/")

    assert response.status_code == 200
    assert response.context["snapshot"].transmission_chain_state == (
        "retained_failure"
    )
    rows = next(
        section
        for section in response.context["sections"]
        if section["key"] == "layer-evidence-ledger"
    )["rows"]
    changed = next(row for row in rows if row["layer"] == "global-dollar")
    assert changed["stale"] == "true"
    assert changed["refresh_failure"] == "component-validation"
    assert all(
        row["stale"] == "false" and row["refresh_failure"] == "none"
        for row in rows
        if row["layer"] != "global-dollar"
    )

    forged = deepcopy(snapshot.data)
    forged["refresh_failure"]["reason_code"] = "latest-attempt-incomplete"
    forged["refresh_failure"]["reason"] = (
        TRANSMISSION_CHAIN_REFRESH_FAILURE_REASONS[
            "latest-attempt-incomplete"
        ]
    )
    snapshot.data = forged
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_transmission_chain_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_terminal_attempt_marker_rejects_success_only_reason_code(
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    referenced = _parent_input_run(snapshot, "global-dollar", "h10")
    failure = _successor_attempt(
        referenced,
        status=IngestionRun.Status.FAILED,
    )
    now = failure.completed_at + timedelta(seconds=1)
    monkeypatch.setattr("research.official_data.timezone.now", lambda: now)
    _coordinate_transmission_chain_dashboard()
    snapshot.refresh_from_db()

    forged = deepcopy(snapshot.data)
    forged["refresh_failure"]["reason_code"] = "component-validation"
    forged["refresh_failure"]["reason"] = (
        TRANSMISSION_CHAIN_REFRESH_FAILURE_REASONS["component-validation"]
    )
    snapshot.data = forged
    snapshot.save(update_fields=["data", "updated_at"])

    assert select_public_transmission_chain_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_parent_natural_expiry_is_a_presentation_state_without_failure(
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: datetime(2026, 8, 20, 10, tzinfo=UTC),
    )

    selected = select_public_transmission_chain_snapshot([snapshot])

    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.quality_status == Observation.Quality.STALE
    assert selected.transmission_chain_state == "natural_expiry"
    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.ESTIMATED
    assert "refresh_failure" not in snapshot.data


@pytest.mark.django_db
def test_repeated_failures_then_same_value_recovery_preserve_parent_transition(
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain
    original_fingerprint = snapshot.data["fingerprint"]
    original_payload_hash = snapshot.data["payload_integrity_hash"]
    original_global_envelope = next(
        item
        for item in snapshot.data["component_snapshots"]
        if item["component_page_key"] == "global-dollar"
    )
    original_global_child = DashboardSnapshot.objects.get(
        pk=original_global_envelope["component_snapshot_id"]
    )
    original_global_semantics = _without_volatile_dashboard_lineage(
        _without_exact_global_dollar_acquisition(
            deepcopy(original_global_child.data)
        )
    )
    first_failure = record_provider_result(
        ProviderResult.failure(
            "federal-reserve",
            "h10",
            "first H.10 upstream timeout",
        )
    )
    dashboards, stale = _coordinate_transmission_chain_dashboard()
    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    first_marker = deepcopy(snapshot.data["refresh_failure"])
    assert snapshot.quality_status == Observation.Quality.STALE
    assert first_marker["attempts"]

    second_failure = record_provider_result(
        ProviderResult.failure(
            "federal-reserve",
            "h10",
            "second H.10 schema failure",
        )
    )
    dashboards, stale = _coordinate_transmission_chain_dashboard()
    assert dashboards == []
    assert stale == {"transmission-chain"}
    snapshot.refresh_from_db()
    second_marker = deepcopy(snapshot.data["refresh_failure"])
    assert second_marker != first_marker
    h10_marker = next(
        item
        for item in second_marker["attempts"]
        if item["component_page_key"] == "global-dollar"
        and item["input_role"] == "h10"
    )
    assert h10_marker["ingestion_run_id"] == second_failure.pk
    assert h10_marker["ingestion_run_id"] != first_failure.pk
    selected = select_public_transmission_chain_snapshot([snapshot])
    assert selected is not None
    assert selected.transmission_chain_state == "retained_failure"

    audited_data = deepcopy(snapshot.data)
    forged = deepcopy(audited_data)
    forged["refresh_failure"]["checked_at"] = "2099-01-01T00:00:00+00:00"
    snapshot.data = forged
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_transmission_chain_snapshot([snapshot]) is None
    forged = deepcopy(audited_data)
    forged["refresh_failure"]["attempts"][0]["batch_id"] = str(uuid.uuid4())
    snapshot.data = forged
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_transmission_chain_snapshot([snapshot]) is None
    snapshot.data = audited_data
    snapshot.save(update_fields=["data", "updated_at"])

    recovered_at = datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: recovered_at
    )
    monkeypatch.setattr("research.services.timezone.now", lambda: recovered_at)
    recovered_result = _h10_result()
    recovered_result.fetched_at = recovered_at - timedelta(minutes=1)
    recovered_h10 = record_provider_result(
        recovered_result,
        persist=_store_h10_observations,
    )
    child_dashboards, child_stale = _coordinate_global_dollar_dashboard(
        [recovered_h10]
    )
    assert child_stale == set()
    assert len(child_dashboards) == 1
    recovered_global_semantics = _without_volatile_dashboard_lineage(
        _without_exact_global_dollar_acquisition(
            deepcopy(child_dashboards[0].data)
        )
    )
    assert recovered_global_semantics == original_global_semantics

    transition = select_public_transmission_chain_snapshot([snapshot])
    assert transition is not None
    assert transition.pk == snapshot.pk
    assert transition.transmission_chain_state == "transition_pending"
    assert transition.quality_status == Observation.Quality.STALE

    recovered_parents, parent_stale = _coordinate_transmission_chain_dashboard()
    assert parent_stale == set()
    assert len(recovered_parents) == 1
    recovered_parent = recovered_parents[0]
    assert recovered_parent.pk != snapshot.pk
    assert recovered_parent.batch_id != snapshot.batch_id
    assert {
        item["key"]: item["value"] for item in recovered_parent.data["metrics"]
    } == {item["key"]: item["value"] for item in snapshot.data["metrics"]}
    assert recovered_parent.data["semantic_manifest"] == snapshot.data[
        "semantic_manifest"
    ]
    assert recovered_parent.data["fingerprint"] == original_fingerprint
    assert recovered_parent.data["payload_integrity_hash"] != original_payload_hash
    assert "refresh_failure" not in recovered_parent.data
    assert select_public_transmission_chain_snapshot().pk == recovered_parent.pk


@pytest.mark.django_db
def test_coordinator_ignores_newer_poison_parent_shell_for_regression_baseline(
    published_transmission_chain,
    monkeypatch,
):
    valid = published_transmission_chain
    poison_data = deepcopy(valid.data)
    poison_data["publication_batch_id"] = str(uuid.uuid4())
    poison_data["as_of"] = "2099-01-01T00:00:00+00:00"
    for envelope in poison_data["component_snapshots"]:
        envelope["as_of"] = "2099-01-01T00:00:00+00:00"
    poison = DashboardSnapshot.objects.create(
        key="transmission-chain",
        title="forged future parent shell",
        summary="invalid regression baseline",
        as_of=datetime(2099, 1, 1, tzinfo=UTC),
        quality_status=Observation.Quality.ERROR,
        data=poison_data,
        source=ensure_source("internal"),
        is_published=True,
    )

    recovered_at = datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: recovered_at
    )
    monkeypatch.setattr("research.services.timezone.now", lambda: recovered_at)
    h10_result = _h10_result()
    h10_result.fetched_at = recovered_at - timedelta(minutes=1)
    h10_run = record_provider_result(
        h10_result,
        persist=_store_h10_observations,
    )
    child_dashboards, child_stale = _coordinate_global_dollar_dashboard(
        [h10_run]
    )
    assert child_stale == set()
    assert len(child_dashboards) == 1

    parents, stale = _coordinate_transmission_chain_dashboard()

    assert stale == set()
    assert len(parents) == 1
    replacement = parents[0]
    assert replacement.pk not in {valid.pk, poison.pk}
    assert replacement.title == valid.title
    assert select_public_transmission_chain_snapshot().pk == replacement.pk
    poison.refresh_from_db()
    assert poison.quality_status == Observation.Quality.ERROR
    assert "refresh_failure" not in poison.data


@pytest.mark.django_db
def test_stale_writer_skips_newer_invalid_parent_shell(
    published_transmission_chain,
):
    valid = published_transmission_chain
    invalid = DashboardSnapshot.objects.create(
        key="transmission-chain",
        title="forged shell",
        summary="not a contract",
        as_of=valid.as_of,
        quality_status=Observation.Quality.ERROR,
        data={"contract_version": 1, "demo": False},
        source=ensure_source("internal"),
        is_published=True,
    )
    record_provider_result(
        ProviderResult.failure(
            "federal-reserve",
            "h10",
            "H.10 unavailable",
        )
    )

    dashboards, stale = _coordinate_transmission_chain_dashboard()

    assert dashboards == []
    assert stale == {"transmission-chain"}
    invalid.refresh_from_db()
    valid.refresh_from_db()
    assert "refresh_failure" not in invalid.data
    assert invalid.quality_status == Observation.Quality.ERROR
    assert "refresh_failure" in valid.data
    assert valid.quality_status == Observation.Quality.STALE
    assert select_public_transmission_chain_snapshot().pk == valid.pk


@pytest.mark.django_db
def test_route_renders_all_three_exact_tables_and_live_component_status(
    client,
    published_transmission_chain,
    monkeypatch,
):
    snapshot = published_transmission_chain

    response = client.get("/liquidity/transmission-chain/")

    assert response.status_code == 200
    assert response.context["snapshot"].pk == snapshot.pk
    sections = {item["key"]: item for item in response.context["sections"]}
    assert set(sections) == {
        "layer-evidence-ledger",
        "shared-input-reconciliation",
        "methodology-and-licensing-gaps",
    }
    assert len(sections["layer-evidence-ledger"]["rows"]) == 6
    assert len(sections["shared-input-reconciliation"]["rows"]) == 6
    for section in sections.values():
        column_keys = [item["key"] for item in section["columns"]]
        assert all(
            [item["key"] for item in row["cells_list"]] == column_keys
            for row in section["rows"]
        )
    assert all(
        row["stale"] == "false" and row["refresh_failure"] == "none"
        for row in sections["layer-evidence-ledger"]["rows"]
    )
    content = response.content.decode()
    assert "六层官方证据台账" in content
    assert "共享输入对账" in content
    assert "方法与许可缺口" in content
    assert "5.6 / 10" not in content

    record_provider_result(
        ProviderResult.failure(
            "federal-reserve",
            "h10",
            "route overlay fixture failure",
        )
    )
    _coordinate_transmission_chain_dashboard()
    stale_response = client.get("/liquidity/transmission-chain/")
    stale_sections = {
        item["key"]: item for item in stale_response.context["sections"]
    }
    global_row = next(
        row
        for row in stale_sections["layer-evidence-ledger"]["rows"]
        if row["layer"] == "global-dollar"
    )
    assert global_row["stale"] == "true"
    assert global_row["refresh_failure"] == "h10: failed"
    assert all(
        row["stale"] == "false" and row["refresh_failure"] == "none"
        for row in stale_sections["layer-evidence-ledger"]["rows"]
        if row["layer"] != "global-dollar"
    )
    assert "global-dollar / h10" in stale_response.content.decode()

    snapshot.refresh_from_db()
    persisted_layer = next(
        item
        for item in snapshot.data["sections"]
        if item["key"] == "layer-evidence-ledger"
    )
    persisted_global = next(
        row for row in persisted_layer["rows"] if row["layer"] == "global-dollar"
    )
    assert persisted_global["stale"] == "false"
    assert persisted_global["refresh_failure"] == "none"

    recovered_at = datetime(2026, 7, 14, 10, 1, tzinfo=UTC)
    monkeypatch.setattr(
        "research.official_data.timezone.now", lambda: recovered_at
    )
    monkeypatch.setattr("research.services.timezone.now", lambda: recovered_at)
    monkeypatch.setattr("research.views.timezone.now", lambda: recovered_at)
    recovered_result = _h10_result()
    recovered_result.fetched_at = recovered_at - timedelta(minutes=1)
    recovered_h10 = record_provider_result(
        recovered_result,
        persist=_store_h10_observations,
    )
    child_dashboards, child_stale = _coordinate_global_dollar_dashboard(
        [recovered_h10]
    )
    assert child_stale == set()
    assert len(child_dashboards) == 1

    transition_response = client.get("/liquidity/transmission-chain/")
    transition_rows = next(
        item
        for item in transition_response.context["sections"]
        if item["key"] == "layer-evidence-ledger"
    )["rows"]
    transition_global = next(
        row for row in transition_rows if row["layer"] == "global-dollar"
    )
    assert transition_global["stale"] == "true"
    assert transition_global["refresh_failure"] == "等待父页原子发布"
    assert all(
        row["stale"] == "false" and row["refresh_failure"] == "none"
        for row in transition_rows
        if row["layer"] != "global-dollar"
    )
