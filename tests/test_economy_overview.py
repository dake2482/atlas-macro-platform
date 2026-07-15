from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from dateutil.relativedelta import relativedelta

from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    SourceLicense,
)
from research.official_data import (
    CORE_PUBLICATION_KEYS,
    ECONOMY_CONTRACT_VERSION,
    ECONOMY_REQUIRED_METRIC_KEYS,
    _coordinate_economy_dashboard,
    publish_official_dashboards,
    refresh_macro_official_data,
    refresh_official_data,
)
from research.providers import ProviderResult
from research.services import ensure_source

FIXED_NOW = datetime(2026, 7, 13, tzinfo=UTC)
COMPONENTS = {
    "gdp": {
        "metric_key": "bea-a191rl",
        "label": "实际 GDP 增速",
        "value": Decimal("2.10"),
        "value_date": datetime(2026, 4, 1, tzinfo=UTC),
        "source_key": "bea-release",
        "source_keys": ["bea-release"],
        "chart_key": "gdp-growth-history",
        "chart_title": "实际 GDP 增速",
        "quality": "fresh",
    },
    "employment": {
        "metric_key": "lns14000000",
        "label": "失业率",
        "value": Decimal("4.10"),
        "value_date": datetime(2026, 6, 1, tzinfo=UTC),
        "source_key": "bls",
        "source_keys": ["bls"],
        "chart_key": "labor-slack",
        "chart_title": "失业率",
        "quality": "fresh",
    },
    "inflation": {
        "metric_key": "core-cpi-yoy",
        "label": "核心 CPI 同比",
        "value": Decimal("2.90"),
        "value_date": datetime(2026, 5, 1, tzinfo=UTC),
        "source_key": "internal",
        "source_keys": ["bls", "internal"],
        "chart_key": "core-cpi-rates",
        "chart_title": "核心 CPI 同比",
        "quality": "estimated",
    },
    "consumer": {
        "metric_key": "bea-real-pce-mom",
        "label": "实际 PCE 环比",
        "value": Decimal("0.30"),
        "value_date": datetime(2026, 6, 1, tzinfo=UTC),
        "source_key": "bea-pio-release",
        "source_keys": ["bea-pio-release"],
        "chart_key": "real-consumption-income-momentum",
        "chart_title": "实际消费动能",
        "quality": "fresh",
    },
}


@pytest.fixture(autouse=True)
def _select_synthetic_strict_children_fixture(monkeypatch):
    """Keep generic economy unit fixtures explicit at the strict-child boundary."""

    def select_fixture():
        snapshot = (
            DashboardSnapshot.objects.filter(key="gdp", is_published=True)
            .order_by("-created_at", "-id")
            .first()
        )
        if snapshot is not None:
            snapshot.gdp_publication_state = "current_candidate"
        return snapshot

    monkeypatch.setattr(
        "research.official_data.select_public_gdp_snapshot",
        select_fixture,
    )

    def select_employment_fixture():
        snapshot = (
            DashboardSnapshot.objects.filter(key="employment", is_published=True)
            .order_by("-created_at", "-id")
            .first()
        )
        if snapshot is not None:
            snapshot.employment_publication_state = "current_candidate"
        return snapshot

    monkeypatch.setattr(
        "research.official_data.select_public_employment_snapshot",
        select_employment_fixture,
    )

    def select_inflation_fixture():
        snapshot = (
            DashboardSnapshot.objects.filter(key="inflation", is_published=True)
            .order_by("-created_at", "-id")
            .first()
        )
        if snapshot is not None:
            snapshot.inflation_publication_state = "current_candidate"
        return snapshot

    monkeypatch.setattr(
        "research.official_data.select_public_inflation_snapshot",
        select_inflation_fixture,
    )


def _chart_rows(value: Decimal, batch_id: uuid.UUID, source_keys: list[str]):
    latest = date(2026, 6, 1)
    rows = []
    for offset in range(-47, 1):
        value_date = latest + relativedelta(months=offset)
        rows.append(
            {
                "date": value_date.isoformat(),
                "指标": float(value),
                "_source_keys": source_keys,
                "_lineage": {
                    "指标": {
                        "batch_id": str(batch_id),
                        "source_keys": source_keys,
                        "value_date": value_date.isoformat(),
                    }
                },
            }
        )
    return rows


def _create_component(
    page_key: str,
    *,
    value: Decimal | None = None,
    fetched_at: datetime | None = None,
) -> DashboardSnapshot:
    spec = COMPONENTS[page_key]
    value = value if value is not None else spec["value"]
    fetched_at = fetched_at or FIXED_NOW - timedelta(hours=2)
    input_batch = uuid.uuid4()
    publication_batch = uuid.uuid4()
    source = ensure_source(spec["source_key"])
    internal = ensure_source("internal")
    for source_key in spec["source_keys"]:
        ensure_source(source_key)
    fresh_until = FIXED_NOW + timedelta(days=30)
    metadata = {}
    if page_key == "inflation":
        metadata = {
            "formula": "100 * (core_t / core_t-12 - 1)",
            "input_series": ["cuur0000sa0l1e"],
            "input_batch_ids": [str(input_batch)],
            "input_value_dates": [
                "2025-05-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
            ],
            "input_lineage": [
                {
                    "series_key": "cuur0000sa0l1e",
                    "source_key": "bls",
                    "batch_id": str(input_batch),
                    "value_date": "2026-05-01T00:00:00+00:00",
                    "fetched_at": fetched_at.isoformat(),
                }
            ],
        }
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
        "batch_id": str(input_batch),
        "source_key": spec["source_key"],
        "source_keys": spec["source_keys"],
        "source": source.name,
        "fallback_source": None,
        "quality_status": spec["quality"],
        "metadata": metadata,
    }
    metrics = [metric]
    if page_key == "employment":
        metrics.append(
            {
                "key": "ces0000000001",
                "label": "非农就业总量",
                "value": 159876,
                "display_value": "159,876K",
            }
        )
    if page_key == "inflation":
        metrics.extend(
            [
                {"key": "cusr0000sa0", "label": "CPI 指数", "value": 333.333},
                {
                    "key": "cusr0000sa0l1e",
                    "label": "核心 CPI 指数",
                    "value": 444.444,
                },
            ]
        )
    chart = {
        "key": spec["chart_key"],
        "title": spec["chart_title"],
        "kind": "line",
        "data": _chart_rows(value, input_batch, spec["source_keys"]),
        "source_keys": spec["source_keys"],
        "batch_ids": [str(input_batch)],
        "as_of": spec["value_date"].isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "quality_status": spec["quality"],
    }
    fingerprint = hashlib.sha256(
        f"{page_key}:{value}:{spec['value_date'].isoformat()}".encode()
    ).hexdigest()
    payload_integrity_hash = hashlib.sha256(
        f"payload:{page_key}:{value}:{publication_batch}".encode()
    ).hexdigest()
    source_roles = (
        [
            {"role": "bls", "source_key": "bls"},
            {"role": "bea-pio", "source_key": "bea-pio-release"},
        ]
        if page_key == "inflation"
        else [{"role": page_key, "source_key": spec["source_key"]}]
    )
    snapshot = DashboardSnapshot.objects.create(
        key=page_key,
        title=page_key,
        as_of=spec["value_date"],
        batch_id=publication_batch,
        quality_status=spec["quality"],
        summary=f"{page_key} fixture",
        data={
            "demo": False,
            "metrics": metrics,
            "charts": [chart],
            "chart_data": chart["data"],
            "sections": [],
            "component_batches": [str(input_batch)],
            "source_keys": spec["source_keys"],
            "fresh_until": fresh_until.isoformat(),
            "publication_batch_id": str(publication_batch),
            "fingerprint": fingerprint,
            "payload_integrity_hash": payload_integrity_hash,
            "contract_version": 2 if page_key in {"gdp", "employment", "inflation"} else 1,
            "formula_version": f"{page_key}-fixture-v2",
            "component_roles": {
                page_key: {
                    "source_key": spec["source_key"],
                    "batch_id": str(input_batch),
                }
            },
            "input_runs": source_roles,
        },
        source=internal,
        is_published=True,
    )
    MetricSnapshot.objects.create(
        key=f"{page_key}-{spec['metric_key']}",
        label=spec["label"],
        value=value,
        display_value=f"{value}%",
        unit="%",
        value_date=spec["value_date"],
        as_of=spec["value_date"],
        fetched_at=fetched_at,
        batch_id=publication_batch,
        source=source,
        quality_status=spec["quality"],
        license_scope=source.license_scope,
        metadata={
            "component_batch_id": str(input_batch),
            **metadata,
        },
    )
    return snapshot


def _create_all_components() -> dict[str, DashboardSnapshot]:
    return {page_key: _create_component(page_key) for page_key in COMPONENTS}


@pytest.mark.django_db
def test_economy_requires_coordinator_prepared_data():
    _create_all_components()

    assert "economy" not in CORE_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"economy"}) == []
    assert not DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).exists()


@pytest.mark.django_db
def test_economy_selects_exact_four_rates_and_preserves_component_lineage(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    components = _create_all_components()

    dashboards, stale = _coordinate_economy_dashboard()

    assert stale == set()
    assert len(dashboards) == 1
    snapshot = dashboards[0]
    assert snapshot.data["contract_version"] == ECONOMY_CONTRACT_VERSION
    assert [item["key"] for item in snapshot.data["metrics"]] == [
        "bea-a191rl",
        "lns14000000",
        "core-cpi-yoy",
        "bea-real-pce-mom",
    ]
    published_metric_keys = {
        item["key"] for item in snapshot.data["metrics"]
    }
    published_metric_labels = {
        item["label"] for item in snapshot.data["metrics"]
    }
    assert not published_metric_keys & {
        "ces0000000001",
        "cusr0000sa0",
        "cusr0000sa0l1e",
    }
    assert not published_metric_labels & {
        "非农就业总量",
        "CPI 指数",
        "核心 CPI 指数",
    }
    assert {item["unit"] for item in snapshot.data["metrics"]} == {"%"}
    assert set(snapshot.data["source_keys"]) == {
        "bea-release",
        "bea-pio-release",
        "bls",
        "internal",
    }
    references = {
        item["page_key"]: item for item in snapshot.data["component_snapshots"]
    }
    assert set(references) == set(COMPONENTS)
    for page_key, child in components.items():
        reference = references[page_key]
        assert reference["snapshot_id"] == child.pk
        assert reference["publication_batch_id"] == str(child.batch_id)
        assert reference["fingerprint"] == child.data["fingerprint"]
        metric = next(
            item
            for item in snapshot.data["metrics"]
            if item["key"] == COMPONENTS[page_key]["metric_key"]
        )
        assert metric["metadata"]["component_snapshot_id"] == child.pk
        stored = MetricSnapshot.objects.get(
            key=f"economy-{metric['key']}", batch_id=snapshot.batch_id
        )
        assert stored.metadata["component_snapshot_id"] == child.pk
        assert stored.metadata["component_page_key"] == page_key
        assert stored.metadata["inherited_license_scope"]


@pytest.mark.django_db
def test_economy_allows_unrelated_child_stale_state_but_rejects_selected_source(
    client,
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    components = _create_all_components()
    employment = components["employment"]
    employment.quality_status = "stale"
    employment.data["refresh_failure"] = {
        "sources": [
            {"source": "bls", "status": "success"},
            {"source": "dol-eta-ui", "status": "failed"},
        ]
    }
    employment.save(update_fields=["data", "quality_status", "updated_at"])

    dashboards, stale = _coordinate_economy_dashboard()
    assert stale == set()
    assert len(dashboards) == 1

    employment.data["refresh_failure"]["sources"][0]["status"] = "failed"
    employment.save(update_fields=["data", "updated_at"])
    dashboards, stale = _coordinate_economy_dashboard()
    assert dashboards == []
    assert stale == {"economy"}
    snapshot = DashboardSnapshot.objects.get(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    )
    assert snapshot.quality_status == "stale"
    failure = snapshot.data["refresh_failure"]["components"]
    assert failure[0]["page_key"] == "employment"
    assert "bls" in failure[0]["reason"]
    content = client.get("/economy/").content.decode()
    assert "失败组件：employment" in content
    assert "bls" in content


@pytest.mark.django_db
def test_economy_missing_component_never_publishes_partial_snapshot(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    for page_key in ("gdp", "employment", "inflation"):
        _create_component(page_key)

    dashboards, stale = _coordinate_economy_dashboard()

    assert dashboards == []
    assert stale == {"economy"}
    assert not DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).exists()


@pytest.mark.django_db
def test_economy_rejects_component_internal_batch_mismatch(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    components = _create_all_components()
    inflation = components["inflation"]
    data = deepcopy(inflation.data)
    selected = next(
        item for item in data["metrics"] if item.get("key") == "core-cpi-yoy"
    )
    mismatched_batch = uuid.uuid4()
    selected["batch_id"] = str(mismatched_batch)
    selected["metadata"]["input_batch_ids"] = [str(mismatched_batch)]
    for lineage in selected["metadata"]["input_lineage"]:
        lineage["batch_id"] = str(mismatched_batch)
    data["component_batches"].append(str(mismatched_batch))
    inflation.data = data
    inflation.save(update_fields=["data", "updated_at"])

    dashboards, stale = _coordinate_economy_dashboard()

    assert dashboards == []
    assert stale == {"economy"}
    assert not DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).exists()


@pytest.mark.django_db
def test_economy_skips_source_wide_batch_check_for_strict_employment_and_inflation(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    components = _create_all_components()
    first, _ = _coordinate_economy_dashboard()
    parent = first[0]
    employment = components["employment"]
    data = deepcopy(employment.data)
    data["refresh_failure"] = {
        "sources": [
            {"source": "bls", "status": "success"},
            {"source": "dol-eta-ui", "status": "failed"},
        ]
    }
    employment.data = data
    employment.quality_status = "stale"
    employment.save(update_fields=["data", "quality_status", "updated_at"])
    bls = ensure_source("bls")
    latest_bls = IngestionRun.objects.create(
        source=bls,
        dataset="series:fixture",
        started_at=FIXED_NOW,
        completed_at=FIXED_NOW,
        status=IngestionRun.Status.SUCCESS,
        row_count=10,
    )

    dashboards, stale = _coordinate_economy_dashboard()

    assert dashboards == []
    assert stale == set()
    parent.refresh_from_db()
    assert parent.quality_status == "estimated"
    assert "refresh_failure" not in parent.data
    assert all(
        str(latest_bls.batch_id) not in item["component_batches"]
        for item in parent.data["component_snapshots"]
        if item["page_key"] in {"employment", "inflation"}
    )


@pytest.mark.django_db
def test_economy_uses_latest_component_and_never_falls_back(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _create_all_components()
    first, _ = _coordinate_economy_dashboard()
    baseline = first[0]
    latest_consumer = _create_component("consumer")
    latest_data = deepcopy(latest_consumer.data)
    latest_data["charts"][0]["quality_status"] = "stale"
    latest_consumer.data = latest_data
    latest_consumer.quality_status = "stale"
    latest_consumer.save(
        update_fields=["data", "quality_status", "updated_at"]
    )

    dashboards, stale = _coordinate_economy_dashboard()

    assert dashboards == []
    assert stale == {"economy"}
    assert DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).count() == 1
    baseline.refresh_from_db()
    assert baseline.quality_status == "stale"
    failure = baseline.data["refresh_failure"]["components"][0]
    assert failure["page_key"] == "consumer"
    assert failure["snapshot_id"] == latest_consumer.pk


@pytest.mark.django_db
def test_economy_rejects_revoked_selected_source_and_retains_last_complete(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _create_all_components()
    first, _ = _coordinate_economy_dashboard()
    snapshot = first[0]
    baseline_metrics = snapshot.data["metrics"]
    licence = SourceLicense.objects.get(source__key="bls", is_current=True)
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])

    dashboards, stale = _coordinate_economy_dashboard()

    assert dashboards == []
    assert stale == {"economy"}
    assert DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).count() == 1
    snapshot.refresh_from_db()
    assert snapshot.data["metrics"] == baseline_metrics
    assert snapshot.quality_status == "stale"
    assert snapshot.data["refresh_failure"]["components"][0]["status"] == "unlicensed"


@pytest.mark.django_db
def test_economy_failure_and_same_value_recovery_refreshes_lineage_in_place(
    monkeypatch,
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    components = _create_all_components()
    first, _ = _coordinate_economy_dashboard()
    parent = first[0]
    original_batch = parent.batch_id
    original_fingerprint = parent.data["fingerprint"]
    original_component_batches = set(parent.data["component_batches"])

    inflation = components["inflation"]
    failed_data = deepcopy(inflation.data)
    selected_metric = next(
        item
        for item in failed_data["metrics"]
        if item.get("key") == "core-cpi-yoy"
    )
    selected_metric["quality_status"] = "stale"
    inflation.data = failed_data
    inflation.quality_status = "stale"
    inflation.save(update_fields=["data", "quality_status", "updated_at"])
    dashboards, stale = _coordinate_economy_dashboard()
    assert dashboards == []
    assert stale == {"economy"}
    parent.refresh_from_db()
    assert parent.quality_status == "stale"

    recovered_batch = uuid.uuid4()
    recovered_fetched_at = FIXED_NOW + timedelta(hours=1)
    recovered_data = deepcopy(inflation.data)
    selected_metric = next(
        item
        for item in recovered_data["metrics"]
        if item.get("key") == "core-cpi-yoy"
    )
    selected_metric["quality_status"] = "estimated"
    selected_metric["batch_id"] = str(recovered_batch)
    selected_metric["fetched_at"] = recovered_fetched_at.isoformat()
    selected_metric["metadata"]["input_batch_ids"] = [str(recovered_batch)]
    for lineage in selected_metric["metadata"]["input_lineage"]:
        lineage["batch_id"] = str(recovered_batch)
        lineage["fetched_at"] = recovered_fetched_at.isoformat()
    chart = recovered_data["charts"][0]
    chart["quality_status"] = "estimated"
    chart["batch_ids"] = [str(recovered_batch)]
    chart["fetched_at"] = recovered_fetched_at.isoformat()
    for row in chart["data"]:
        row["_lineage"]["指标"]["batch_id"] = str(recovered_batch)
    recovered_data["component_batches"] = [str(recovered_batch)]
    inflation.data = recovered_data
    inflation.quality_status = "estimated"
    inflation.save(update_fields=["data", "quality_status", "updated_at"])
    child_metric = MetricSnapshot.objects.get(
        key="inflation-core-cpi-yoy", batch_id=inflation.batch_id
    )
    child_metric.fetched_at = recovered_fetched_at
    child_metadata = deepcopy(child_metric.metadata)
    child_metadata["component_batch_id"] = str(recovered_batch)
    child_metadata["input_batch_ids"] = [str(recovered_batch)]
    for lineage in child_metadata["input_lineage"]:
        lineage["batch_id"] = str(recovered_batch)
        lineage["fetched_at"] = recovered_fetched_at.isoformat()
    child_metric.metadata = child_metadata
    child_metric.save(update_fields=["fetched_at", "metadata", "updated_at"])

    recovered, stale = _coordinate_economy_dashboard()

    assert recovered == []
    assert stale == set()
    assert DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).count() == 1
    parent.refresh_from_db()
    assert parent.batch_id == original_batch
    assert parent.data["fingerprint"] == original_fingerprint
    assert parent.quality_status == "estimated"
    assert "refresh_failure" not in parent.data
    assert set(parent.data["component_batches"]) != original_component_batches
    assert str(recovered_batch) in parent.data["component_batches"]
    stored = MetricSnapshot.objects.get(
        key="economy-core-cpi-yoy", batch_id=parent.batch_id
    )
    assert stored.fetched_at == recovered_fetched_at
    assert stored.metadata["component_batch_id"] == str(recovered_batch)
    assert stored.metadata["input_batch_ids"] == [str(recovered_batch)]


@pytest.mark.django_db
def test_economy_changed_component_value_creates_new_atomic_snapshot(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _create_all_components()
    first, _ = _coordinate_economy_dashboard()
    first_snapshot = first[0]

    _create_component("inflation", value=Decimal("3.00"))
    second, stale = _coordinate_economy_dashboard()

    assert stale == set()
    assert len(second) == 1
    assert DashboardSnapshot.objects.filter(
        key="economy", data__contract_version=ECONOMY_CONTRACT_VERSION
    ).count() == 2
    second_snapshot = second[0]
    assert second_snapshot.batch_id != first_snapshot.batch_id
    assert second_snapshot.data["fingerprint"] != first_snapshot.data["fingerprint"]
    assert len(second_snapshot.data["metrics"]) == 4
    assert next(
        item for item in second_snapshot.data["metrics"] if item["key"] == "core-cpi-yoy"
    )["value"] == 3.0


@pytest.mark.django_db
def test_economy_route_uses_v1_contract_and_safe_get_controls(client, monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    _create_all_components()
    dashboards, _ = _coordinate_economy_dashboard()
    current = dashboards[0]
    internal = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="economy",
        title="legacy",
        as_of=FIXED_NOW,
        batch_id=uuid.uuid4(),
        quality_status="fresh",
        summary="legacy raw BLS page",
        data={
            "demo": False,
            "metrics": [
                {"key": "ces0000000001", "label": "非农就业总量", "value": 159876},
                {"key": "cusr0000sa0", "label": "CPI 指数", "value": 333.333},
            ],
            "source_keys": ["internal"],
        },
        source=internal,
        is_published=True,
    )

    response = client.get(
        "/economy/",
        {"period": "<script>alert(1)</script>", "tab": "unknown"},
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert content.count('class="metric-card"') == 4
    assert "实际 GDP 季调年化增速" in content
    assert "核心 CPI 同比" in content
    assert "非农就业总量" not in content
    assert "CPI 指数" not in content
    assert "<script>alert(1)</script>" not in content
    assert response.context["snapshot"].pk == current.pk
    assert response.context["selected_period"] == "3y"
    assert response.context["selected_tab"] == "overview"
    assert len(response.context["charts"]) == 4
    assert all(chart["time_axis"] == "date" for chart in response.context["charts"])

    labor = client.get("/economy/", {"period": "1y", "tab": "labor"})
    assert labor.context["selected_period"] == "1y"
    assert labor.context["selected_tab"] == "labor"
    assert [chart["key"] for chart in labor.context["charts"]] == ["labor-slack"]
    assert len(labor.context["charts"][0]["data"]) == 13


@pytest.mark.django_db
def test_economy_route_hides_legacy_snapshot_when_v1_is_missing(client):
    internal = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="economy",
        title="legacy",
        as_of=FIXED_NOW,
        quality_status="fresh",
        summary="legacy",
        data={
            "demo": False,
            "metrics": [
                {"key": "cusr0000sa0", "label": "CPI 指数", "value": 333.333}
            ],
            "source_keys": ["internal"],
        },
        source=internal,
        is_published=True,
    )

    response = client.get("/economy/")
    content = response.content.decode()

    assert response.status_code == 200
    assert not any(
        context.get("snapshot") is not None
        for context in response.context
        if "snapshot" in context
    )
    assert "333.333" not in content
    assert "CPI 指数" not in content
    assert "本页尚无通过来源许可与质量检查的可发布快照" in content


@pytest.mark.django_db
def test_both_official_refresh_entrypoints_trigger_economy_coordinator(
    monkeypatch,
):
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
        "DOLWeeklyClaimsProvider": "dol-eta-ui",
        "FederalReserveRSSProvider": "federal-reserve",
        "BEAGDPReleaseProvider": "bea-release",
        "CensusMARTSProvider": "census",
        "CensusMARTSReleaseProvider": "census-release",
        "BEAPIOReleaseProvider": "bea-pio-release",
        "FederalReserveG19Provider": "federal-reserve-g19",
        "NYFedHouseholdDebtProvider": "ny-fed-household-credit",
    }
    for class_name, source_key in provider_sources.items():
        monkeypatch.setattr(
            f"research.official_data.{class_name}",
            lambda source_key=source_key: FailingProvider(source_key),
        )
    calls = []

    def coordinate():
        calls.append("economy")
        return [], set()

    monkeypatch.setattr(
        "research.official_data._coordinate_economy_dashboard", coordinate
    )
    inflation_calls = []

    def coordinate_inflation(runs):
        inflation_calls.append(
            {(run.source.key, run.dataset, run.status) for run in runs}
        )
        return [], {"inflation"}

    monkeypatch.setattr(
        "research.official_data.coordinate_inflation_dashboard",
        coordinate_inflation,
    )

    refresh_official_data(current_year=2026)
    refresh_macro_official_data(current_year=2026)

    assert calls == ["economy", "economy"]
    assert len(inflation_calls) == 2
    assert {source for source, _dataset, _status in inflation_calls[0]} >= {
        "bls",
        "bea-pio-release",
    }
    assert "bea-pio-release" in {
        source for source, _dataset, _status in inflation_calls[1]
    }


def test_economy_required_metric_contract_is_exact():
    assert ECONOMY_REQUIRED_METRIC_KEYS == {
        "bea-a191rl",
        "lns14000000",
        "core-cpi-yoy",
        "bea-real-pce-mom",
    }
