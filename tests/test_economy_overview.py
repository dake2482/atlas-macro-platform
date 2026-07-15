from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from dateutil.relativedelta import relativedelta
from django.db import connection
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext

from research.economy_contract import (
    ECONOMY_COMPONENTS,
    ECONOMY_CONTRACT_VERSION,
    ECONOMY_FORMULA_VERSION,
    ECONOMY_REQUIRED_CHART_KEYS,
    ECONOMY_REQUIRED_METRIC_KEYS,
    _target_identity,
    coordinate_economy_dashboard,
    publish_economy_revision,
    replay_economy_snapshot,
    select_public_economy_snapshot,
)
from research.models import (
    DashboardSnapshot,
    MetricSnapshot,
    Observation,
    Source,
    SourceLicense,
)
from research.official_data import (
    APPEND_ONLY_PUBLICATION_KEYS,
    INDEPENDENT_PUBLICATION_KEYS,
    _publish_dashboard,
    _publish_dashboard_core,
    publish_official_dashboards,
)
from research.page_registry import get_page_config
from research.services import ensure_source
from research.thesis_publication import (
    DAILY_EVIDENCE_COMPONENT_CONTRACT_VERSIONS,
    DAILY_EVIDENCE_CONTRACT_VERSION,
    publish_daily_evidence_snapshot,
    validate_daily_evidence_snapshot,
)
from tests.thesis_factories import build_daily_components

FIXED_NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
COMPONENT_SPECS = {
    "gdp": {
        "metric_key": "bea-a191rl",
        "label": "实际 GDP 增速",
        "chart_key": "gdp-growth-history",
        "value": Decimal("2.10"),
        "value_date": datetime(2026, 4, 1, tzinfo=UTC),
        "source_key": "bea-release",
        "source_keys": ["bea-release"],
    },
    "employment": {
        "metric_key": "lns14000000",
        "label": "失业率",
        "chart_key": "labor-slack",
        "value": Decimal("4.10"),
        "value_date": datetime(2026, 6, 1, tzinfo=UTC),
        "source_key": "bls",
        "source_keys": ["bls", "dol-eta-ui"],
    },
    "inflation": {
        "metric_key": "core-cpi-yoy",
        "label": "核心 CPI 同比",
        "chart_key": "core-cpi-rates",
        "value": Decimal("2.90"),
        "value_date": datetime(2026, 6, 1, tzinfo=UTC),
        "source_key": "bls",
        "source_keys": ["bea-pio-release", "bls"],
    },
    "consumer": {
        "metric_key": "bea-real-pce-mom",
        "label": "实际 PCE 环比",
        "chart_key": "real-consumption-income-momentum",
        "value": Decimal("0.30"),
        "value_date": datetime(2026, 6, 1, tzinfo=UTC),
        "source_key": "bea-pio-release",
        "source_keys": [
            "bea-pio-release",
            "census-release",
            "federal-reserve-g19",
            "ny-fed-household-credit",
        ],
    },
}


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _child_hashes(title: str, summary: str, data: dict) -> tuple[str, str]:
    fingerprint = _hash({"page": title, "data": data})
    with_fingerprint = {**deepcopy(data), "fingerprint": fingerprint}
    payload_hash = _hash(
        {"title": title, "summary": summary, "data": with_fingerprint}
    )
    return fingerprint, payload_hash


def _synthetic_child_replay(
    snapshot: DashboardSnapshot,
    *,
    page_key: str,
) -> SimpleNamespace | None:
    if snapshot.key != page_key or not snapshot.is_published or snapshot.source.key != "internal":
        return None
    data = deepcopy(snapshot.data or {})
    data.pop("refresh_failure", None)
    fingerprint = data.pop("fingerprint", None)
    payload_hash = data.pop("payload_integrity_hash", None)
    expected_fingerprint, expected_payload_hash = _child_hashes(
        snapshot.title,
        snapshot.summary,
        data,
    )
    if fingerprint != expected_fingerprint or payload_hash != expected_payload_hash:
        return None
    expected = {
        **data,
        "fingerprint": fingerprint,
        "payload_integrity_hash": payload_hash,
    }
    metric = expected["metrics"][0]
    expected_quality = metric["quality_status"]
    return SimpleNamespace(
        data=deepcopy(snapshot.data),
        expected_data=expected,
        expected_quality=expected_quality,
    )


@pytest.fixture
def strict_children(monkeypatch):
    states = {page_key: "current_candidate" for page_key in COMPONENT_SPECS}
    monkeypatch.setattr("research.economy_contract.timezone.now", lambda: FIXED_NOW)

    for page_key, state_attribute in (
        ("gdp", "gdp_publication_state"),
        ("employment", "employment_publication_state"),
        ("inflation", "inflation_publication_state"),
        ("consumer", "consumer_publication_state"),
    ):
        def selector(
            page_key=page_key,
            state_attribute=state_attribute,
        ):
            candidates = (
                DashboardSnapshot.objects.filter(key=page_key, is_published=True)
                .select_related("source")
                .order_by("-created_at", "-id")
            )
            for candidate in candidates:
                if _synthetic_child_replay(candidate, page_key=page_key) is None:
                    continue
                selected = deepcopy(candidate)
                selected.data = deepcopy(candidate.data or {})
                setattr(selected, state_attribute, states[page_key])
                if states[page_key] != "retained_failure":
                    selected.data.pop("refresh_failure", None)
                return selected
            return None

        monkeypatch.setattr(
            f"research.economy_contract.select_public_{page_key}_snapshot",
            selector,
        )
        replay_name = (
            "replay_gdp_snapshot"
            if page_key == "gdp"
            else f"replay_{page_key}_snapshot"
        )
        monkeypatch.setattr(
            f"research.economy_contract.{replay_name}",
            lambda snapshot, page_key=page_key: _synthetic_child_replay(
                snapshot,
                page_key=page_key,
            ),
        )
    return states


def _chart_rows(value: Decimal, source_keys: list[str], batch_ids: list[str]):
    latest = date(2026, 6, 1)
    return [
        {
            "date": (latest + relativedelta(months=offset)).isoformat(),
            "指标": float(value),
            "_source_keys": source_keys,
            "_batch_ids": batch_ids,
        }
        for offset in range(-47, 1)
    ]


def _create_child(
    page_key: str,
    *,
    value: Decimal | None = None,
    fresh_until: datetime | None = None,
) -> DashboardSnapshot:
    spec = COMPONENT_SPECS[page_key]
    value = spec["value"] if value is None else value
    fresh_until = fresh_until or FIXED_NOW + timedelta(days=30)
    internal = ensure_source("internal")
    sources = {source_key: ensure_source(source_key) for source_key in spec["source_keys"]}
    selected_source = sources[spec["source_key"]]
    licence = SourceLicense.objects.get(source=selected_source, is_current=True)
    component_batches = [str(uuid.uuid4()), str(uuid.uuid4())]
    publication_batch = uuid.uuid4()
    fetched_at = FIXED_NOW - timedelta(hours=2)
    metric = {
        "key": spec["metric_key"],
        "label": spec["label"],
        "value": float(value),
        "display_value": f"{value}%",
        "change": None,
        "unit": "%",
        "value_date": spec["value_date"].isoformat(),
        "as_of": spec["value_date"].isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": component_batches[0],
        "source_key": spec["source_key"],
        "source_keys": spec["source_keys"],
        "fallback_source": None,
        "quality_status": Observation.Quality.FRESH,
        "license_scope": licence.scope[:120],
        "metadata": {"formula": f"{page_key}-fixture"},
    }
    chart = {
        "key": spec["chart_key"],
        "title": spec["chart_key"],
        "kind": "line",
        "data": _chart_rows(value, spec["source_keys"], component_batches),
        "source_keys": spec["source_keys"],
        "batch_ids": component_batches,
        "as_of": spec["value_date"].isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "quality_status": Observation.Quality.FRESH,
    }
    title = f"{page_key} fixture"
    summary = f"immutable {page_key} fixture"
    unhashed = {
        "demo": False,
        "metrics": [metric],
        "charts": [chart],
        "chart_data": chart["data"],
        "sections": [],
        "component_batches": component_batches,
        "source_keys": spec["source_keys"],
        "fresh_until": fresh_until.isoformat(),
        "publication_batch_id": str(publication_batch),
        "contract_version": 2,
        "formula_version": f"{page_key}-fixture-v2",
        "component_roles": {
            page_key: {
                "source_key": spec["source_key"],
                "batch_id": component_batches[0],
            }
        },
        "input_runs": [
            {"role": f"role-{index}", "source_key": source_key}
            for index, source_key in enumerate(spec["source_keys"])
        ],
    }
    fingerprint, payload_hash = _child_hashes(title, summary, unhashed)
    snapshot = DashboardSnapshot.objects.create(
        key=page_key,
        title=title,
        summary=summary,
        as_of=spec["value_date"],
        batch_id=publication_batch,
        quality_status=Observation.Quality.FRESH,
        data={
            **unhashed,
            "fingerprint": fingerprint,
            "payload_integrity_hash": payload_hash,
        },
        source=internal,
        is_published=True,
    )
    MetricSnapshot.objects.create(
        key=f"{page_key}-{spec['metric_key']}",
        label=spec["label"],
        value=value,
        display_value=f"{value}%",
        change=None,
        unit="%",
        value_date=spec["value_date"],
        as_of=spec["value_date"],
        fetched_at=fetched_at,
        batch_id=publication_batch,
        source=selected_source,
        fallback_source=None,
        quality_status=Observation.Quality.FRESH,
        license_scope=licence.scope[:120],
        metadata={"fixture": True},
    )
    return snapshot


def _create_children(**overrides) -> dict[str, DashboardSnapshot]:
    return {
        page_key: _create_child(page_key, **overrides)
        for page_key in COMPONENT_SPECS
    }


def _write_queries(queries) -> list[str]:
    return [
        query["sql"]
        for query in queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


def _save_rehashed_economy_parent(
    snapshot: DashboardSnapshot,
    data: dict,
) -> None:
    data.pop("fingerprint", None)
    data.pop("payload_integrity_hash", None)
    fingerprint_data = deepcopy(data)
    fingerprint_data.pop("publication_batch_id")
    data["fingerprint"] = _hash(
        {
            "title": snapshot.title,
            "summary": snapshot.summary,
            "data": fingerprint_data,
        }
    )
    data["payload_integrity_hash"] = _hash(
        {"title": snapshot.title, "summary": snapshot.summary, "data": data}
    )
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])


@pytest.mark.django_db
def test_economy_contract_surface_and_generic_writers_are_closed():
    assert ECONOMY_CONTRACT_VERSION == 2
    assert ECONOMY_FORMULA_VERSION == "official-four-child-economy-v2"
    assert ECONOMY_REQUIRED_METRIC_KEYS == {
        "bea-a191rl",
        "lns14000000",
        "core-cpi-yoy",
        "bea-real-pce-mom",
    }
    assert ECONOMY_REQUIRED_CHART_KEYS == {
        "gdp-growth-history",
        "labor-slack",
        "core-cpi-rates",
        "real-consumption-income-momentum",
    }
    assert "economy" in INDEPENDENT_PUBLICATION_KEYS
    assert "economy" in APPEND_ONLY_PUBLICATION_KEYS
    assert get_page_config("economy")["snapshot_contract_version"] == 2
    assert publish_official_dashboards(keys={"economy"}) == []
    kwargs = {
        "key": "economy",
        "title": "rogue",
        "summary": "rogue",
        "metrics": [{"key": "x", "label": "x", "value": 1}],
        "batch_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError, match="dedicated macro v2"):
        _publish_dashboard(**kwargs)
    with pytest.raises(ValueError, match="dedicated macro v2"):
        _publish_dashboard_core(**kwargs)


@pytest.mark.django_db
def test_publish_exact_four_children_and_root_provenance(strict_children):
    children = _create_children()

    snapshot = publish_economy_revision()

    assert snapshot is not None
    assert snapshot.data["contract_version"] == ECONOMY_CONTRACT_VERSION
    assert snapshot.data["formula_version"] == ECONOMY_FORMULA_VERSION
    assert [item["key"] for item in snapshot.data["metrics"]] == [
        "bea-a191rl",
        "lns14000000",
        "core-cpi-yoy",
        "bea-real-pce-mom",
    ]
    assert [item["key"] for item in snapshot.data["charts"]] == [
        "gdp-growth-history",
        "labor-slack",
        "core-cpi-rates",
        "real-consumption-income-momentum",
    ]
    assert snapshot.data["sections"] == []
    expected_sources = sorted(
        {key for spec in COMPONENT_SPECS.values() for key in spec["source_keys"]}
    )
    expected_batches = sorted(
        {
            batch_id
            for child in children.values()
            for batch_id in child.data["component_batches"]
        }
    )
    assert snapshot.data["source_keys"] == expected_sources
    assert snapshot.data["component_batches"] == expected_batches
    assert snapshot.data["fresh_until"] == min(
        child.data["fresh_until"] for child in children.values()
    )
    references = {item["page_key"]: item for item in snapshot.data["component_snapshots"]}
    assert set(references) == set(COMPONENT_SPECS)
    for page_key, child in children.items():
        reference = references[page_key]
        assert reference["snapshot_id"] == child.pk
        assert reference["snapshot_batch_id"] == str(child.batch_id)
        assert reference["state_at_publication"] == "current_candidate"
        assert reference["root_source_keys"] == sorted(child.data["source_keys"])
        assert reference["root_component_batches"] == sorted(
            child.data["component_batches"]
        )
        assert reference["root_fresh_until"] == child.data["fresh_until"]
        assert reference["selected_metric_snapshot_id"] == MetricSnapshot.objects.get(
            key=f"{page_key}-{COMPONENT_SPECS[page_key]['metric_key']}",
            batch_id=child.batch_id,
        ).pk
        normalized = MetricSnapshot.objects.get(
            key=f"economy-{COMPONENT_SPECS[page_key]['metric_key']}",
            batch_id=snapshot.batch_id,
        )
        copied_metric = next(
            item
            for item in snapshot.data["metrics"]
            if item["key"] == COMPONENT_SPECS[page_key]["metric_key"]
        )
        assert normalized.metadata["component_batch_id"] == copied_metric["batch_id"]
        assert normalized.metadata["component_snapshot_batch_id"] == str(
            child.batch_id
        )
    assert MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).count() == 4
    assert replay_economy_snapshot(snapshot) is not None


@pytest.mark.django_db
def test_exact_identity_retry_is_zero_writes(strict_children):
    _create_children()
    snapshot = publish_economy_revision()
    assert snapshot is not None
    before_parent = DashboardSnapshot.objects.get(pk=snapshot.pk)
    before_metrics = list(
        MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).values_list(
            "pk", "updated_at", "metadata"
        )
    )

    with CaptureQueriesContext(connection) as queries:
        result = publish_economy_revision()

    assert result is None
    assert _write_queries(queries.captured_queries) == []
    after_parent = DashboardSnapshot.objects.get(pk=snapshot.pk)
    assert after_parent.updated_at == before_parent.updated_at
    assert after_parent.data == before_parent.data
    assert list(
        MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).values_list(
            "pk", "updated_at", "metadata"
        )
    ) == before_metrics


@pytest.mark.django_db
def test_same_visible_value_new_child_identity_appends_parent(strict_children):
    _create_children()
    first = publish_economy_revision()
    assert first is not None
    first_data = deepcopy(first.data)
    first_metric_rows = list(
        MetricSnapshot.objects.filter(batch_id=first.batch_id).values(
            "pk", "key", "value", "metadata", "updated_at"
        )
    )

    replacement = _create_child("gdp", value=COMPONENT_SPECS["gdp"]["value"])
    second = publish_economy_revision()

    assert second is not None
    assert second.pk != first.pk
    assert second.data["fingerprint"] != first_data["fingerprint"]
    assert second.data["publication_input_identity"]["gdp"]["snapshot_id"] == replacement.pk
    assert [item["value"] for item in second.data["metrics"]] == [
        item["value"] for item in first_data["metrics"]
    ]
    first.refresh_from_db()
    assert first.data == first_data
    assert list(
        MetricSnapshot.objects.filter(batch_id=first.batch_id).values(
            "pk", "key", "value", "metadata", "updated_at"
        )
    ) == first_metric_rows
    assert DashboardSnapshot.objects.filter(
        key="economy",
        source__key="internal",
        data__contract_version=ECONOMY_CONTRACT_VERSION,
    ).count() == 2
    assert MetricSnapshot.objects.filter(key__startswith="economy-").count() == 8


@pytest.mark.django_db
def test_historical_parent_replays_after_child_is_no_longer_current(strict_children):
    children = _create_children()
    parent = publish_economy_revision()
    assert parent is not None
    gdp = children["gdp"]
    data = deepcopy(gdp.data)
    data["refresh_failure"] = {"reason": "later failure"}
    gdp.data = data
    gdp.quality_status = Observation.Quality.STALE
    gdp.save(update_fields=["data", "quality_status", "updated_at"])
    strict_children["gdp"] = "retained_failure"

    replay = replay_economy_snapshot(parent)

    assert replay is not None
    assert replay.expected_data == parent.data


@pytest.mark.django_db
def test_new_child_exposes_transition_then_coordinator_publishes(strict_children):
    _create_children()
    first, stale = coordinate_economy_dashboard()
    assert stale == set()
    assert len(first) == 1
    _create_child("inflation", value=Decimal("3.00"))

    transitional = select_public_economy_snapshot()
    second, stale = coordinate_economy_dashboard()

    assert transitional is not None
    assert transitional.economy_publication_state == "transition_pending"
    assert stale == set()
    assert len(second) == 1
    assert second[0].data["publication_input_identity"]["inflation"][
        "snapshot_id"
    ] != first[0].data["publication_input_identity"]["inflation"]["snapshot_id"]


@pytest.mark.django_db
def test_freshness_boundary_is_current_at_equality_then_natural_after(
    strict_children,
    monkeypatch,
):
    _create_children(fresh_until=FIXED_NOW)
    parent = publish_economy_revision()
    assert parent is not None
    selected = select_public_economy_snapshot()
    assert selected is not None
    assert selected.economy_publication_state == "current_candidate"

    strict_children.update({key: "natural_expiry" for key in strict_children})
    monkeypatch.setattr(
        "research.economy_contract.timezone.now",
        lambda: FIXED_NOW + timedelta(microseconds=1),
    )
    selected = select_public_economy_snapshot()

    assert selected is not None
    assert selected.economy_publication_state == "natural_expiry"


@pytest.mark.django_db
def test_natural_expiry_and_transition_reads_are_zero_write(strict_children, monkeypatch):
    _create_children(fresh_until=FIXED_NOW)
    parent = publish_economy_revision()
    assert parent is not None
    strict_children.update({key: "natural_expiry" for key in strict_children})
    monkeypatch.setattr(
        "research.economy_contract.timezone.now",
        lambda: FIXED_NOW + timedelta(seconds=1),
    )
    with CaptureQueriesContext(connection) as natural_queries:
        natural = select_public_economy_snapshot()
    assert natural is not None
    assert natural.economy_publication_state == "natural_expiry"
    assert _write_queries(natural_queries.captured_queries) == []

    strict_children.update({key: "current_candidate" for key in strict_children})
    strict_children["gdp"] = "transition_pending"
    monkeypatch.setattr("research.economy_contract.timezone.now", lambda: FIXED_NOW)
    with CaptureQueriesContext(connection) as transition_queries:
        transition = select_public_economy_snapshot()
    assert transition is not None
    assert transition.economy_publication_state == "transition_pending"
    assert _write_queries(transition_queries.captured_queries) == []


@pytest.mark.django_db
def test_child_retained_failure_marker_is_exact_and_mutation_fails_closed(
    strict_children,
):
    _create_children()
    first = publish_economy_revision()
    assert first is not None
    _create_child("consumer")
    second = publish_economy_revision()
    assert second is not None
    consumer = DashboardSnapshot.objects.filter(key="consumer").order_by("-id").first()
    assert consumer is not None
    data = deepcopy(consumer.data)
    data["refresh_failure"] = {"reason_code": "fixture-failure", "attempt": 1}
    consumer.data = data
    consumer.quality_status = Observation.Quality.STALE
    consumer.save(update_fields=["data", "quality_status", "updated_at"])
    strict_children["consumer"] = "retained_failure"

    published, stale = coordinate_economy_dashboard()
    retained = select_public_economy_snapshot()

    assert published == []
    assert stale == {"economy"}
    assert retained is not None
    assert retained.pk == second.pk
    assert retained.economy_publication_state == "retained_failure"
    marker = retained.data["refresh_failure"]
    assert marker["reason_code"] == "child-retained-failure"
    assert len(marker["components"]) == 4
    assert {item["page_key"] for item in marker["components"]} == set(COMPONENT_SPECS)
    assert all("failure_hash" in item and "updated_at" in item for item in marker["components"])

    data = deepcopy(consumer.data)
    data["refresh_failure"]["attempt"] = 2
    consumer.data = data
    consumer.save(update_fields=["data", "updated_at"])

    assert replay_economy_snapshot(second) is not None
    assert select_public_economy_snapshot() is None
    assert replay_economy_snapshot(first) is not None


@pytest.mark.django_db
def test_publication_postcondition_rolls_back_and_marks_prior(
    strict_children,
    monkeypatch,
):
    _create_children()
    baseline = publish_economy_revision()
    assert baseline is not None
    _create_child("gdp", value=Decimal("2.20"))
    original_replay = __import__(
        "research.economy_contract",
        fromlist=["_economy_snapshot_static_replay"],
    )._economy_snapshot_static_replay

    def reject_new_parent(snapshot):
        if snapshot.key == "economy" and snapshot.pk != baseline.pk:
            return None
        return original_replay(snapshot)

    monkeypatch.setattr(
        "research.economy_contract._economy_snapshot_static_replay",
        reject_new_parent,
    )

    published, stale = coordinate_economy_dashboard()

    assert published == []
    assert stale == {"economy"}
    assert DashboardSnapshot.objects.filter(
        key="economy",
        source__key="internal",
        data__contract_version=ECONOMY_CONTRACT_VERSION,
    ).count() == 1
    assert MetricSnapshot.objects.filter(key__startswith="economy-").count() == 4
    baseline.refresh_from_db()
    assert baseline.quality_status == Observation.Quality.STALE
    assert baseline.data["refresh_failure"]["reason_code"] == "publication-postcondition"
    selected = select_public_economy_snapshot()
    assert selected is not None
    assert selected.economy_publication_state == "retained_failure"


@pytest.mark.django_db
def test_rogue_copied_target_identity_cannot_dos_publication(strict_children):
    _create_children()
    first = publish_economy_revision()
    assert first is not None
    _create_child("gdp", value=Decimal("2.20"))
    children = {
        page_key: __import__(
            "research.economy_contract",
            fromlist=[f"select_public_{page_key}_snapshot"],
        ).__dict__[f"select_public_{page_key}_snapshot"]()
        for page_key in ECONOMY_COMPONENTS
    }
    target = _target_identity(children)
    internal = ensure_source("internal")
    rogue = DashboardSnapshot.objects.create(
        key="economy",
        title="rogue",
        summary="copied identity",
        as_of=FIXED_NOW,
        batch_id=uuid.uuid4(),
        quality_status=Observation.Quality.FRESH,
        data={
            "demo": False,
            "contract_version": ECONOMY_CONTRACT_VERSION,
            "publication_input_identity": target,
        },
        source=internal,
        is_published=True,
    )

    published = publish_economy_revision(children=children)

    assert published is not None
    assert published.pk != rogue.pk
    assert replay_economy_snapshot(published) is not None


@pytest.mark.django_db
def test_selector_skips_static_invalid_newer_rogue(strict_children):
    _create_children()
    valid = publish_economy_revision()
    assert valid is not None
    internal = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="economy",
        title="rogue",
        summary="rogue",
        as_of=FIXED_NOW,
        quality_status=Observation.Quality.FRESH,
        data={"demo": False, "contract_version": ECONOMY_CONTRACT_VERSION},
        source=internal,
        is_published=True,
    )

    selected = select_public_economy_snapshot()

    assert selected is not None
    assert selected.pk == valid.pk


@pytest.mark.django_db
def test_revoked_child_root_source_hides_parent(strict_children):
    _create_children()
    parent = publish_economy_revision()
    assert parent is not None
    licence = SourceLicense.objects.get(source__key="bls", is_current=True)
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])

    assert replay_economy_snapshot(parent) is None
    assert select_public_economy_snapshot() is None


@pytest.mark.django_db
def test_economy_materializes_selected_metric_locks_in_global_order(
    strict_children,
    monkeypatch,
):
    _create_children()
    lock_calls: list[type] = []
    materialized_selected_metric_lock = False
    original_select_for_update = QuerySet.select_for_update
    original_iter = QuerySet.__iter__
    original_count = QuerySet.count

    def tracked_select_for_update(queryset, *args, **kwargs):
        if queryset.model in {
            Source,
            SourceLicense,
            DashboardSnapshot,
            MetricSnapshot,
        }:
            assert kwargs.get("of") == ("self",)
            lock_calls.append(queryset.model)
        return original_select_for_update(queryset, *args, **kwargs)

    def tracked_iter(queryset):
        nonlocal materialized_selected_metric_lock
        if queryset.model is MetricSnapshot and queryset.query.select_for_update:
            materialized_selected_metric_lock = True
        return original_iter(queryset)

    def guarded_count(queryset):
        if queryset.model is MetricSnapshot and queryset.query.select_for_update:
            raise AssertionError("select_for_update aggregation does not lock metric rows")
        return original_count(queryset)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)
    monkeypatch.setattr(QuerySet, "__iter__", tracked_iter)
    monkeypatch.setattr(QuerySet, "count", guarded_count)

    snapshot = publish_economy_revision()

    assert snapshot is not None
    assert materialized_selected_metric_lock
    assert lock_calls[:4] == [
        Source,
        SourceLicense,
        DashboardSnapshot,
        MetricSnapshot,
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutation",
    [
        "float-snapshot-id",
        "bool-metric-id",
        "numeric-string-id",
        "oversized-id",
        "invalid-uuid",
        "bool-contract-version",
        "string-contract-version",
    ],
)
def test_economy_reference_and_identity_reject_type_smuggling(
    strict_children,
    mutation,
):
    _create_children()
    parent = publish_economy_revision()
    assert parent is not None
    data = deepcopy(parent.data)
    reference = next(
        item for item in data["component_snapshots"] if item["page_key"] == "gdp"
    )
    identity = data["publication_input_identity"]["gdp"]
    if mutation == "float-snapshot-id":
        value = float(reference["snapshot_id"])
        reference["snapshot_id"] = value
        identity["snapshot_id"] = value
    elif mutation == "bool-metric-id":
        reference["selected_metric_snapshot_id"] = True
        identity["selected_metric_snapshot_id"] = True
    elif mutation == "numeric-string-id":
        value = str(reference["snapshot_id"])
        reference["snapshot_id"] = value
        identity["snapshot_id"] = value
    elif mutation == "oversized-id":
        value = 2**63
        reference["selected_metric_snapshot_id"] = value
        identity["selected_metric_snapshot_id"] = value
    elif mutation == "invalid-uuid":
        reference["snapshot_batch_id"] = "not-a-canonical-uuid"
        identity["snapshot_batch_id"] = "not-a-canonical-uuid"
    elif mutation == "bool-contract-version":
        reference["contract_version"] = True
        identity["contract_version"] = True
    else:
        reference["contract_version"] = "2"
        identity["contract_version"] = "2"
    _save_rehashed_economy_parent(parent, data)

    assert replay_economy_snapshot(parent) is None
    assert select_public_economy_snapshot() is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutation",
    ["dict-reason", "changed-reason", "changed-hash", "noncanonical-checked-at"],
)
def test_economy_retained_marker_reason_integrity_is_fail_closed(
    strict_children,
    mutation,
):
    _create_children()
    publish_economy_revision()
    _create_child("consumer")
    retained_parent = publish_economy_revision()
    assert retained_parent is not None
    consumer = DashboardSnapshot.objects.filter(key="consumer").order_by("-id").first()
    data = deepcopy(consumer.data)
    data["refresh_failure"] = {"reason_code": "fixture-failure"}
    consumer.data = data
    consumer.quality_status = Observation.Quality.STALE
    consumer.save(update_fields=["data", "quality_status", "updated_at"])
    strict_children["consumer"] = "retained_failure"
    published, stale = coordinate_economy_dashboard()
    assert published == [] and stale == {"economy"}

    retained_parent.refresh_from_db()
    data = deepcopy(retained_parent.data)
    marker = data["refresh_failure"]
    assert marker["reason_sha256"] == _hash(marker["reason"])
    if mutation == "dict-reason":
        marker["reason"] = {"message": marker["reason"]}
    elif mutation == "changed-reason":
        marker["reason"] += " tampered"
    elif mutation == "changed-hash":
        marker["reason_sha256"] = "0" * 64
    else:
        marker["checked_at"] = marker["checked_at"].replace("+00:00", "Z")
    retained_parent.data = data
    retained_parent.save(update_fields=["data", "updated_at"])

    assert select_public_economy_snapshot() is None


@pytest.mark.django_db
def test_internal_derived_licence_revocation_hides_and_blocks_economy(
    strict_children,
):
    _create_children()
    parent = publish_economy_revision()
    assert parent is not None
    licence = SourceLicense.objects.get(source__key="internal", is_current=True)
    licence.derived_display_allowed = False
    licence.save(update_fields=["derived_display_allowed", "updated_at"])

    assert replay_economy_snapshot(parent) is None
    assert select_public_economy_snapshot() is None
    with pytest.raises(ValueError, match="source licence is not publishable"):
        publish_economy_revision()
    assert DashboardSnapshot.objects.filter(
        key="economy",
        source__key="internal",
        data__contract_version=ECONOMY_CONTRACT_VERSION,
    ).count() == 1


@pytest.mark.django_db
def test_economy_route_uses_strict_selector_and_get_is_zero_write(
    strict_children,
    client,
):
    _create_children()
    parent = publish_economy_revision()
    assert parent is not None
    with CaptureQueriesContext(connection) as queries:
        response = client.get(
            "/economy/",
            {"period": "<script>alert(1)</script>", "tab": "unknown"},
        )
    content = response.content.decode()

    assert response.status_code == 200
    assert response.context["snapshot"].pk == parent.pk
    assert response.context["economy_state"] == "current_candidate"
    assert response.context["selected_period"] == "3y"
    assert response.context["selected_tab"] == "overview"
    assert content.count('class="metric-card"') == 4
    assert "实际 GDP 季调年化增速" in content
    assert "核心 CPI 同比" in content
    assert "<script>alert(1)</script>" not in content
    assert _write_queries(queries.captured_queries) == []

    labor = client.get("/economy/", {"period": "1y", "tab": "labor"})
    assert labor.context["selected_period"] == "1y"
    assert labor.context["selected_tab"] == "labor"
    assert [chart["key"] for chart in labor.context["charts"]] == ["labor-slack"]
    assert len(labor.context["charts"][0]["data"]) == 13


@pytest.mark.django_db
def test_real_economy_v2_is_frozen_into_daily_evidence_v2(
    strict_children,
    monkeypatch,
):
    _create_children()
    economy = publish_economy_revision()
    assert economy is not None
    synthetic_components, _metrics = build_daily_components(
        "economy-to-daily-seam",
        now=FIXED_NOW,
        component_contract_versions=DAILY_EVIDENCE_COMPONENT_CONTRACT_VERSIONS,
    )
    synthetic_economy = next(
        item for item in synthetic_components if item.key == "economy"
    )
    rates = next(item for item in synthetic_components if item.key == "rates")
    selected_rates = deepcopy(rates)
    selected_rates.treasury_publication_state = "current_candidate"
    monkeypatch.setattr(
        "research.thesis_publication.select_public_treasury_curve_snapshot",
        lambda page_key: selected_rates if page_key == "rates" else None,
    )

    outcome = publish_daily_evidence_snapshot(now=FIXED_NOW)

    assert outcome.ok
    assert outcome.snapshot.data["contract_version"] == DAILY_EVIDENCE_CONTRACT_VERSION
    references = {
        item["page_key"]: item
        for item in outcome.snapshot.data["component_snapshots"]
    }
    assert references["economy"]["snapshot_id"] == economy.pk
    assert references["economy"]["snapshot_id"] != synthetic_economy.pk
    assert references["economy"]["contract_version"] == ECONOMY_CONTRACT_VERSION
    assert references["economy"]["formula_version"] == ECONOMY_FORMULA_VERSION
    assert not validate_daily_evidence_snapshot(
        outcome.snapshot,
        now=FIXED_NOW,
        require_current_components=True,
        require_latest_snapshot=True,
    )
