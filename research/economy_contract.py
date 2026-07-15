"""Strict append-only publication contract for the Economy overview.

The Economy page is a derived, four-child publication.  It never re-queries
upstream observations: every revision is rebuilt from one immutable GDP,
employment, inflation and consumer dashboard revision and the selected
normalised metric row belonging to each child.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from django.db import transaction
from django.utils import timezone

from .consumer_contract import (
    CONSUMER_CONTRACT_VERSION,
    replay_consumer_snapshot,
    select_public_consumer_snapshot,
)
from .employment_contract import (
    EMPLOYMENT_CONTRACT_VERSION,
    replay_employment_snapshot,
    select_public_employment_snapshot,
)
from .inflation_contract import (
    INFLATION_CONTRACT_VERSION,
    replay_inflation_snapshot,
    select_public_inflation_snapshot,
)
from .macro_contract import (
    GDP_CONTRACT_VERSION,
    replay_gdp_snapshot,
    select_public_gdp_snapshot,
)
from .models import (
    DashboardSnapshot,
    MetricSnapshot,
    Observation,
    Source,
    SourceLicense,
)
from .services import (
    public_source_notices,
    publicly_displayable_source_keys,
)

ECONOMY_CONTRACT_VERSION = 2
ECONOMY_FORMULA_VERSION = "official-four-child-economy-v2"
ECONOMY_TITLE = "经济数据"
ECONOMY_SUMMARY = (
    "实际 GDP 季调年化增速、失业率、核心 CPI 同比与实际 PCE 环比分别"
    "继承 GDP、就业、通胀和消费官方子页的完整发布身份、根新鲜度、许可"
    "和批次血缘；不同频率不强行对齐，任一子页转换或失败时不混装半批次。"
)
ECONOMY_TRANSITION_TIMEOUT = timedelta(hours=2)
MAX_DATABASE_ID = (2**63) - 1

ECONOMY_COMPONENTS: dict[str, dict[str, str]] = {
    "gdp": {
        "metric_key": "bea-a191rl",
        "metric_label": "实际 GDP 季调年化增速",
        "chart_key": "gdp-growth-history",
        "tab": "growth",
        "state_attribute": "gdp_publication_state",
    },
    "employment": {
        "metric_key": "lns14000000",
        "metric_label": "失业率",
        "chart_key": "labor-slack",
        "tab": "labor",
        "state_attribute": "employment_publication_state",
    },
    "inflation": {
        "metric_key": "core-cpi-yoy",
        "metric_label": "核心 CPI 同比",
        "chart_key": "core-cpi-rates",
        "tab": "inflation",
        "state_attribute": "inflation_publication_state",
    },
    "consumer": {
        "metric_key": "bea-real-pce-mom",
        "metric_label": "实际 PCE 环比",
        "chart_key": "real-consumption-income-momentum",
        "tab": "consumer",
        "state_attribute": "consumer_publication_state",
    },
}
ECONOMY_REQUIRED_METRIC_KEYS = frozenset(
    component["metric_key"] for component in ECONOMY_COMPONENTS.values()
)
ECONOMY_REQUIRED_CHART_KEYS = frozenset(
    component["chart_key"] for component in ECONOMY_COMPONENTS.values()
)
ECONOMY_REQUIRED_SECTION_KEYS = frozenset()
ECONOMY_CHILD_CONTRACT_VERSIONS = {
    "gdp": GDP_CONTRACT_VERSION,
    "employment": EMPLOYMENT_CONTRACT_VERSION,
    "inflation": INFLATION_CONTRACT_VERSION,
    "consumer": CONSUMER_CONTRACT_VERSION,
}
ECONOMY_PAYLOAD_KEYS = frozenset(
    {
        "demo",
        "metrics",
        "charts",
        "chart_data",
        "sections",
        "component_batches",
        "source_keys",
        "required_notices",
        "fresh_until",
        "publication_batch_id",
        "contract_version",
        "formula_version",
        "required_metric_keys",
        "required_chart_keys",
        "required_section_keys",
        "component_snapshots",
        "publication_input_identity",
        "fallback_state",
        "fallback_source",
        "semantic_boundary",
        "fingerprint",
        "payload_integrity_hash",
    }
)
ECONOMY_COMPONENT_REFERENCE_KEYS = frozenset(
    {
        "page_key",
        "snapshot_id",
        "snapshot_batch_id",
        "fingerprint",
        "payload_integrity_hash",
        "contract_version",
        "formula_version",
        "state_at_publication",
        "snapshot_quality_status",
        "selected_metric_snapshot_id",
        "selected_metric_snapshot_key",
        "selected_metric_snapshot_batch_id",
        "selected_metric_key",
        "selected_chart_key",
        "component_roles",
        "source_roles",
        "root_fresh_until",
        "root_source_keys",
        "root_component_batches",
    }
)
ECONOMY_COMPONENT_IDENTITY_KEYS = frozenset(
    {
        "snapshot_id",
        "snapshot_batch_id",
        "fingerprint",
        "payload_integrity_hash",
        "contract_version",
        "formula_version",
        "selected_metric_snapshot_id",
        "selected_metric_snapshot_key",
        "selected_metric_snapshot_batch_id",
        "selected_metric_key",
        "selected_chart_key",
        "root_fresh_until",
        "root_source_keys",
        "root_component_batches",
    }
)
ECONOMY_MARKER_KEYS = frozenset(
    {"reason_code", "checked_at", "reason", "reason_sha256", "components"}
)
ECONOMY_COMPONENT_WITNESS_KEYS = frozenset(
    {
        "page_key",
        "state",
        "snapshot_id",
        "snapshot_batch_id",
        "fingerprint",
        "payload_integrity_hash",
        "quality_status",
        "updated_at",
        "failure_hash",
    }
)


class EconomyPublicationPostconditionError(ValueError):
    """A complete four-child target could not become the public revision."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _strict_database_id(value: Any) -> int | None:
    if type(value) is not int or not 0 < value <= MAX_DATABASE_ID:
        return None
    return value


def _canonical_uuid_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if str(parsed) == value else None


def _canonical_uuid_list(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and all(_canonical_uuid_string(item) is not None for item in value)
        and value == sorted(value)
        and len(value) == len(set(value))
    )


def _canonical_datetime_string(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.isoformat() != value:
        return None
    return parsed


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _quality(items: Iterable[dict[str, Any]]) -> str:
    statuses = {item.get("quality_status") for item in items}
    if Observation.Quality.ERROR in statuses:
        return Observation.Quality.ERROR
    if Observation.Quality.STALE in statuses:
        return Observation.Quality.STALE
    if Observation.Quality.FALLBACK in statuses:
        return Observation.Quality.FALLBACK
    if statuses == {Observation.Quality.FRESH}:
        return Observation.Quality.FRESH
    return Observation.Quality.ESTIMATED


def _select_child(page_key: str) -> DashboardSnapshot | None:
    if page_key == "gdp":
        return select_public_gdp_snapshot()
    if page_key == "employment":
        return select_public_employment_snapshot()
    if page_key == "inflation":
        return select_public_inflation_snapshot()
    if page_key == "consumer":
        return select_public_consumer_snapshot()
    raise ValueError(f"unsupported Economy child: {page_key}")


def _replay_child(
    page_key: str,
    snapshot: DashboardSnapshot,
) -> SimpleNamespace | None:
    if page_key == "gdp":
        return replay_gdp_snapshot(snapshot)
    if page_key == "employment":
        return replay_employment_snapshot(snapshot)
    if page_key == "inflation":
        return replay_inflation_snapshot(snapshot)
    if page_key == "consumer":
        return replay_consumer_snapshot(snapshot)
    raise ValueError(f"unsupported Economy child: {page_key}")


def _child_state(page_key: str, snapshot: DashboardSnapshot) -> str | None:
    return getattr(snapshot, ECONOMY_COMPONENTS[page_key]["state_attribute"], None)


def _select_children() -> dict[str, DashboardSnapshot] | None:
    children: dict[str, DashboardSnapshot] = {}
    for page_key in ECONOMY_COMPONENTS:
        selected = _select_child(page_key)
        if selected is None:
            return None
        children[page_key] = selected
    return children


def _source_roles(data: dict[str, Any]) -> dict[str, str]:
    references: list[dict[str, Any]] = []
    input_run = data.get("input_run")
    if isinstance(input_run, dict):
        references.append(input_run)
    input_runs = data.get("input_runs")
    if isinstance(input_runs, list):
        references.extend(item for item in input_runs if isinstance(item, dict))
    roles: dict[str, str] = {}
    for index, item in enumerate(references):
        source_key = str(item.get("source_key") or "")
        if not source_key:
            continue
        role = str(item.get("role") or item.get("dataset") or f"source-{index}")
        roles[role] = source_key
    return dict(sorted(roles.items()))


def _one_keyed_item(
    value: Any,
    *,
    key: str,
) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    matches = [item for item in value if isinstance(item, dict) and item.get("key") == key]
    return matches[0] if len(matches) == 1 else None


def _metric_row_matches_child_payload(
    row: MetricSnapshot,
    metric: dict[str, Any],
) -> bool:
    try:
        value_date = _parse_datetime(metric.get("value_date") or metric.get("as_of"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        fallback_source = row.fallback_source.key if row.fallback_source_id else None
        return bool(
            value_date is not None
            and as_of is not None
            and fetched_at is not None
            and row.value is not None
            and row.value.quantize(Decimal("0.00000001"))
            == Decimal(str(metric["value"])).quantize(Decimal("0.00000001"))
            and row.display_value == str(metric.get("display_value") or "")
            and row.change
            == (
                Decimal(str(metric["change"])).quantize(Decimal("0.000001"))
                if metric.get("change") is not None
                else None
            )
            and row.unit == str(metric.get("unit") or "")
            and row.value_date == value_date
            and row.as_of == as_of
            and row.fetched_at == fetched_at
            and row.source.key == metric.get("source_key")
            and fallback_source == metric.get("fallback_source")
            and row.quality_status == metric.get("quality_status")
            and row.license_scope == str(metric.get("license_scope") or "")[:120]
        )
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False


def _component_payload(
    page_key: str,
    snapshot: DashboardSnapshot,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], MetricSnapshot]:
    component = ECONOMY_COMPONENTS[page_key]
    replay = _replay_child(page_key, snapshot)
    if replay is None:
        raise ValueError(f"{page_key} child revision is not statically replayable")
    data = deepcopy(replay.expected_data)
    metric = _one_keyed_item(data.get("metrics"), key=component["metric_key"])
    chart = _one_keyed_item(data.get("charts"), key=component["chart_key"])
    if metric is None or chart is None:
        raise ValueError(f"{page_key} child is missing the selected metric or chart")
    root_fresh_until = _parse_datetime(data.get("fresh_until"))
    root_source_keys = sorted({str(key) for key in data.get("source_keys") or [] if key})
    root_component_batches = sorted(
        {str(batch_id) for batch_id in data.get("component_batches") or [] if batch_id}
    )
    if (
        root_fresh_until is None
        or not root_source_keys
        or not root_component_batches
        or not publicly_displayable_source_keys(root_source_keys)
        or not _is_sha256(data.get("fingerprint"))
        or not _is_sha256(data.get("payload_integrity_hash"))
    ):
        raise ValueError(f"{page_key} child has invalid root provenance")
    metric_rows = list(
        MetricSnapshot.objects.filter(
            key=f"{page_key}-{component['metric_key']}",
            batch_id=snapshot.batch_id,
        )
        .select_related("source", "fallback_source")
        .order_by("pk")
    )
    if len(metric_rows) != 1 or not _metric_row_matches_child_payload(metric_rows[0], metric):
        raise ValueError(f"{page_key} selected normalized metric does not replay")
    metric_row = metric_rows[0]

    copied_metric = deepcopy(metric)
    copied_metric["label"] = component["metric_label"]
    copied_metric["license_scope"] = metric_row.license_scope
    metric_metadata = deepcopy(copied_metric.get("metadata") or {})
    metric_metadata.update(
        {
            "component_page_key": page_key,
            "component_snapshot_id": snapshot.pk,
            "component_snapshot_batch_id": str(snapshot.batch_id),
            "component_fingerprint": data["fingerprint"],
            "component_payload_integrity_hash": data["payload_integrity_hash"],
            "component_contract_version": data.get("contract_version"),
            "component_formula_version": data.get("formula_version"),
            "component_roles": deepcopy(data.get("component_roles") or {}),
            "component_source_roles": _source_roles(data),
            "selected_metric_snapshot_id": metric_row.pk,
            "selected_metric_snapshot_key": metric_row.key,
        }
    )
    copied_metric["metadata"] = metric_metadata

    copied_chart = deepcopy(chart)
    copied_chart.update(
        {
            "tab": component["tab"],
            "time_axis": "date",
            "component_page_key": page_key,
            "component_snapshot_id": snapshot.pk,
            "component_snapshot_batch_id": str(snapshot.batch_id),
            "component_fingerprint": data["fingerprint"],
            "component_payload_integrity_hash": data["payload_integrity_hash"],
            "component_contract_version": data.get("contract_version"),
            "component_formula_version": data.get("formula_version"),
            "component_roles": deepcopy(data.get("component_roles") or {}),
            "component_source_roles": _source_roles(data),
        }
    )
    reference = {
        "page_key": page_key,
        "snapshot_id": snapshot.pk,
        "snapshot_batch_id": str(snapshot.batch_id),
        "fingerprint": data["fingerprint"],
        "payload_integrity_hash": data["payload_integrity_hash"],
        "contract_version": data.get("contract_version"),
        "formula_version": data.get("formula_version"),
        "state_at_publication": "current_candidate",
        "snapshot_quality_status": replay.expected_quality,
        "selected_metric_snapshot_id": metric_row.pk,
        "selected_metric_snapshot_key": metric_row.key,
        "selected_metric_snapshot_batch_id": str(metric_row.batch_id),
        "selected_metric_key": component["metric_key"],
        "selected_chart_key": component["chart_key"],
        "component_roles": deepcopy(data.get("component_roles") or {}),
        "source_roles": _source_roles(data),
        "root_fresh_until": root_fresh_until.isoformat(),
        "root_source_keys": root_source_keys,
        "root_component_batches": root_component_batches,
    }
    if set(reference) != ECONOMY_COMPONENT_REFERENCE_KEYS:
        raise ValueError("Economy component reference shape regressed")
    return copied_metric, copied_chart, reference, metric_row


def _identity_from_reference(reference: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        "snapshot_id": reference.get("snapshot_id"),
        "snapshot_batch_id": reference.get("snapshot_batch_id"),
        "fingerprint": reference.get("fingerprint"),
        "payload_integrity_hash": reference.get("payload_integrity_hash"),
        "contract_version": reference.get("contract_version"),
        "formula_version": reference.get("formula_version"),
        "selected_metric_snapshot_id": reference.get("selected_metric_snapshot_id"),
        "selected_metric_snapshot_key": reference.get("selected_metric_snapshot_key"),
        "selected_metric_snapshot_batch_id": reference.get(
            "selected_metric_snapshot_batch_id"
        ),
        "selected_metric_key": reference.get("selected_metric_key"),
        "selected_chart_key": reference.get("selected_chart_key"),
        "root_fresh_until": reference.get("root_fresh_until"),
        "root_source_keys": deepcopy(reference.get("root_source_keys")),
        "root_component_batches": deepcopy(reference.get("root_component_batches")),
    }
    page_key = str(reference.get("page_key") or "")
    if page_key not in ECONOMY_COMPONENTS or not _strict_identity_entry(
        page_key,
        identity,
    ):
        raise ValueError("Economy component identity is malformed")
    return identity


def _build_economy_payload(
    children: Mapping[str, DashboardSnapshot],
    *,
    publication_batch_id: uuid.UUID,
) -> tuple[dict[str, Any], datetime, str, dict[str, MetricSnapshot]]:
    if set(children) != set(ECONOMY_COMPONENTS):
        raise ValueError("Economy requires exactly four child revisions")
    metrics: list[dict[str, Any]] = []
    charts: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    metric_rows: dict[str, MetricSnapshot] = {}
    for page_key in ECONOMY_COMPONENTS:
        metric, chart, reference, metric_row = _component_payload(
            page_key,
            children[page_key],
        )
        metrics.append(metric)
        charts.append(chart)
        references.append(reference)
        metric_rows[page_key] = metric_row

    source_keys = sorted(
        {
            key
            for reference in references
            for key in reference["root_source_keys"]
        }
    )
    component_batches = sorted(
        {
            batch_id
            for reference in references
            for batch_id in reference["root_component_batches"]
        }
    )
    deadlines = [_parse_datetime(reference["root_fresh_until"]) for reference in references]
    if any(deadline is None for deadline in deadlines):
        raise ValueError("Economy child root freshness is malformed")
    fresh_until = min(deadline for deadline in deadlines if deadline is not None)
    publication_input_identity = {
        reference["page_key"]: _identity_from_reference(reference)
        for reference in references
    }
    data: dict[str, Any] = {
        "demo": False,
        "metrics": metrics,
        "charts": charts,
        "chart_data": deepcopy(charts[0].get("data") or []),
        "sections": [],
        "component_batches": component_batches,
        "source_keys": source_keys,
        "required_notices": public_source_notices(source_keys),
        "fresh_until": fresh_until.isoformat(),
        "publication_batch_id": str(publication_batch_id),
        "contract_version": ECONOMY_CONTRACT_VERSION,
        "formula_version": ECONOMY_FORMULA_VERSION,
        "required_metric_keys": sorted(ECONOMY_REQUIRED_METRIC_KEYS),
        "required_chart_keys": sorted(ECONOMY_REQUIRED_CHART_KEYS),
        "required_section_keys": [],
        "component_snapshots": references,
        "publication_input_identity": publication_input_identity,
        "fallback_state": "none",
        "fallback_source": None,
        "semantic_boundary": (
            "本页只组合四个已独立审计的官方子页，不把不同频率强行对齐，"
            "不推断衰退、政策路径或交易信号。"
        ),
    }
    fingerprint_data = deepcopy(data)
    fingerprint_data.pop("publication_batch_id")
    data["fingerprint"] = _sha256(
        {"title": ECONOMY_TITLE, "summary": ECONOMY_SUMMARY, "data": fingerprint_data}
    )
    data["payload_integrity_hash"] = _sha256(
        {"title": ECONOMY_TITLE, "summary": ECONOMY_SUMMARY, "data": data}
    )
    metric_as_of = [_parse_datetime(metric.get("as_of")) for metric in metrics]
    if any(value is None for value in metric_as_of):
        raise ValueError("Economy selected metrics lack a valid as_of")
    as_of = min(value for value in metric_as_of if value is not None)
    return data, as_of, _quality([*metrics, *charts]), metric_rows


def _metric_metadata(metric: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    component_metadata = deepcopy(metric.get("metadata") or {})
    return {
        **component_metadata,
        "dashboard_key": "economy",
        "metric_key": metric["key"],
        "source_keys": list(metric.get("source_keys") or []),
        # Daily Evidence validates the copied metric against its original
        # child-series batch, while component_snapshot_batch_id separately
        # preserves the child dashboard revision identity.
        "component_batch_id": str(metric["batch_id"]),
        "input_batch_ids": deepcopy(data["component_batches"]),
        "contract_version": ECONOMY_CONTRACT_VERSION,
        "formula_version": ECONOMY_FORMULA_VERSION,
        "publication_input_identity": deepcopy(data["publication_input_identity"]),
        "fingerprint": data["fingerprint"],
        "payload_integrity_hash": data["payload_integrity_hash"],
    }


def _metric_rows_match(snapshot: DashboardSnapshot, data: dict[str, Any]) -> bool:
    rows = list(
        MetricSnapshot.objects.filter(batch_id=snapshot.batch_id)
        .select_related("source", "fallback_source")
        .order_by("key")
    )
    metrics = {item["key"]: item for item in data["metrics"]}
    if {row.key for row in rows} != {f"economy-{key}" for key in metrics}:
        return False
    try:
        for row in rows:
            metric = metrics[row.key.removeprefix("economy-")]
            value_date = _parse_datetime(metric.get("value_date") or metric.get("as_of"))
            as_of = _parse_datetime(metric.get("as_of"))
            fetched_at = _parse_datetime(metric.get("fetched_at"))
            fallback_source = row.fallback_source.key if row.fallback_source_id else None
            if (
                value_date is None
                or as_of is None
                or fetched_at is None
                or row.label != metric["label"]
                or row.value is None
                or row.value.quantize(Decimal("0.00000001"))
                != Decimal(str(metric["value"])).quantize(Decimal("0.00000001"))
                or row.display_value != str(metric.get("display_value") or "")
                or row.change
                != (
                    Decimal(str(metric["change"])).quantize(Decimal("0.000001"))
                    if metric.get("change") is not None
                    else None
                )
                or row.unit != str(metric.get("unit") or "")
                or row.value_date != value_date
                or row.as_of != as_of
                or row.fetched_at != fetched_at
                or row.source.key != metric["source_key"]
                or fallback_source != metric.get("fallback_source")
                or row.quality_status != metric["quality_status"]
                or row.license_scope != str(metric["license_scope"])[:120]
                or row.metadata != _metric_metadata(metric, data)
            ):
                return False
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return False
    return True


def _strict_reference(page_key: str, reference: Any) -> bool:
    if (
        not isinstance(reference, dict)
        or set(reference) != ECONOMY_COMPONENT_REFERENCE_KEYS
        or reference.get("page_key") != page_key
        or _strict_database_id(reference.get("snapshot_id")) is None
        or _strict_database_id(reference.get("selected_metric_snapshot_id")) is None
        or _canonical_uuid_string(reference.get("snapshot_batch_id")) is None
        or _canonical_uuid_string(
            reference.get("selected_metric_snapshot_batch_id")
        )
        is None
        or type(reference.get("contract_version")) is not int
        or reference.get("contract_version")
        != ECONOMY_CHILD_CONTRACT_VERSIONS[page_key]
    ):
        return False
    return _canonical_uuid_list(reference.get("root_component_batches"))


def _strict_identity_entry(page_key: str, identity: Any) -> bool:
    if (
        not isinstance(identity, dict)
        or set(identity) != ECONOMY_COMPONENT_IDENTITY_KEYS
        or _strict_database_id(identity.get("snapshot_id")) is None
        or _strict_database_id(identity.get("selected_metric_snapshot_id")) is None
        or _canonical_uuid_string(identity.get("snapshot_batch_id")) is None
        or _canonical_uuid_string(
            identity.get("selected_metric_snapshot_batch_id")
        )
        is None
        or type(identity.get("contract_version")) is not int
        or identity.get("contract_version")
        != ECONOMY_CHILD_CONTRACT_VERSIONS[page_key]
    ):
        return False
    return _canonical_uuid_list(identity.get("root_component_batches"))


def _strict_publication_identity(identity: Any) -> bool:
    return bool(
        isinstance(identity, dict)
        and set(identity) == set(ECONOMY_COMPONENTS)
        and all(
            _strict_identity_entry(page_key, identity.get(page_key))
            for page_key in ECONOMY_COMPONENTS
        )
    )


def _references_by_page(data: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    references = data.get("component_snapshots")
    if not isinstance(references, list) or len(references) != len(ECONOMY_COMPONENTS):
        return None
    if any(not isinstance(reference, dict) for reference in references):
        return None
    by_page = {str(reference.get("page_key") or ""): reference for reference in references}
    if set(by_page) != set(ECONOMY_COMPONENTS):
        return None
    return (
        by_page
        if all(
            _strict_reference(page_key, by_page[page_key])
            for page_key in ECONOMY_COMPONENTS
        )
        else None
    )


def _license_is_effective(
    license_row: SourceLicense,
    *,
    today: Any,
    require_derived: bool = False,
    require_storage: bool = False,
) -> bool:
    return bool(
        license_row.is_current
        and license_row.status
        in {Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED}
        and license_row.public_display_allowed
        and (not require_derived or license_row.derived_display_allowed)
        and (not require_storage or license_row.historical_storage_allowed)
        and (license_row.valid_from is None or license_row.valid_from <= today)
        and (license_row.valid_until is None or license_row.valid_until >= today)
    )


def _internal_derived_source_is_licensed(source: Source) -> bool:
    if source.key != "internal" or source.kind != "derived":
        return False
    decisions = list(
        SourceLicense.objects.filter(source=source, is_current=True).order_by("pk")
    )
    return bool(
        len(decisions) == 1
        and _license_is_effective(
            decisions[0],
            today=timezone.localdate(),
            require_derived=True,
            require_storage=True,
        )
    )


def _economy_snapshot_static_replay(
    snapshot: DashboardSnapshot,
) -> SimpleNamespace | None:
    try:
        data = deepcopy(snapshot.data or {})
        audited = deepcopy(data)
        audited.pop("refresh_failure", None)
        references = _references_by_page(audited)
        if (
            snapshot.key != "economy"
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or not _internal_derived_source_is_licensed(snapshot.source)
            or snapshot.title != ECONOMY_TITLE
            or snapshot.summary != ECONOMY_SUMMARY
            or set(audited) != ECONOMY_PAYLOAD_KEYS
            or type(audited.get("contract_version")) is not int
            or audited.get("contract_version") != ECONOMY_CONTRACT_VERSION
            or audited.get("formula_version") != ECONOMY_FORMULA_VERSION
            or _canonical_uuid_string(audited.get("publication_batch_id")) is None
            or audited.get("publication_batch_id") != str(snapshot.batch_id)
            or audited.get("demo") is not False
            or audited.get("fallback_state") != "none"
            or audited.get("fallback_source") is not None
            or set(audited.get("required_metric_keys") or [])
            != ECONOMY_REQUIRED_METRIC_KEYS
            or set(audited.get("required_chart_keys") or [])
            != ECONOMY_REQUIRED_CHART_KEYS
            or set(audited.get("required_section_keys") or [])
            != ECONOMY_REQUIRED_SECTION_KEYS
            or not isinstance(audited.get("metrics"), list)
            or len(audited["metrics"]) != len(ECONOMY_REQUIRED_METRIC_KEYS)
            or {item.get("key") for item in audited["metrics"] if isinstance(item, dict)}
            != ECONOMY_REQUIRED_METRIC_KEYS
            or not isinstance(audited.get("charts"), list)
            or len(audited["charts"]) != len(ECONOMY_REQUIRED_CHART_KEYS)
            or {item.get("key") for item in audited["charts"] if isinstance(item, dict)}
            != ECONOMY_REQUIRED_CHART_KEYS
            or audited.get("sections") != []
            or references is None
            or not _strict_publication_identity(
                audited.get("publication_input_identity")
            )
        ):
            return None
        children: dict[str, DashboardSnapshot] = {}
        for page_key, reference in references.items():
            child = (
                DashboardSnapshot.objects.filter(pk=reference["snapshot_id"])
                .select_related("source")
                .first()
            )
            if (
                child is None
                or child.key != page_key
                or child.pk != reference["snapshot_id"]
                or str(child.batch_id) != reference["snapshot_batch_id"]
            ):
                return None
            # Historical Economy replay is deliberately pinned to the recorded
            # immutable child revision; it never asks a live child selector.
            if _replay_child(page_key, child) is None:
                return None
            children[page_key] = child
        expected, expected_as_of, expected_quality, _metric_rows = _build_economy_payload(
            children,
            publication_batch_id=snapshot.batch_id,
        )
        if (
            _canonical(audited) != _canonical(expected)
            or snapshot.as_of != expected_as_of
            or not _metric_rows_match(snapshot, expected)
        ):
            return None
        return SimpleNamespace(
            data=data,
            expected_data=expected,
            expected_quality=expected_quality,
            children=children,
        )
    except (
        ArithmeticError,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None


def replay_economy_snapshot(snapshot: DashboardSnapshot) -> SimpleNamespace | None:
    """Replay a historical Economy parent from only its recorded child IDs."""

    return _economy_snapshot_static_replay(snapshot)


def _failure_hash(snapshot: DashboardSnapshot) -> str | None:
    failure = (snapshot.data or {}).get("refresh_failure")
    return _sha256(failure) if isinstance(failure, dict) else None


def _component_witness(
    page_key: str,
    selected: DashboardSnapshot,
) -> dict[str, Any]:
    raw = DashboardSnapshot.objects.select_related("source").get(pk=selected.pk)
    replay = _replay_child(page_key, raw)
    if replay is None:
        raise ValueError(f"{page_key} witness child is no longer replayable")
    state = _child_state(page_key, selected)
    if state not in {
        "current_candidate",
        "natural_expiry",
        "transition_pending",
        "retained_failure",
    }:
        raise ValueError(f"{page_key} witness has no strict publication state")
    witness = {
        "page_key": page_key,
        "state": state,
        "snapshot_id": raw.pk,
        "snapshot_batch_id": str(raw.batch_id),
        "fingerprint": replay.expected_data.get("fingerprint"),
        "payload_integrity_hash": replay.expected_data.get("payload_integrity_hash"),
        "quality_status": raw.quality_status,
        "updated_at": raw.updated_at.isoformat(),
        "failure_hash": _failure_hash(raw),
    }
    if set(witness) != ECONOMY_COMPONENT_WITNESS_KEYS:
        raise ValueError("Economy component witness shape regressed")
    return witness


def _live_component_witnesses(
    children: Mapping[str, DashboardSnapshot],
) -> list[dict[str, Any]]:
    return [_component_witness(page_key, children[page_key]) for page_key in ECONOMY_COMPONENTS]


def _strict_component_witness(witness: Any) -> bool:
    return bool(
        isinstance(witness, dict)
        and set(witness) == ECONOMY_COMPONENT_WITNESS_KEYS
        and witness.get("page_key") in ECONOMY_COMPONENTS
        and witness.get("state")
        in {
            "current_candidate",
            "natural_expiry",
            "transition_pending",
            "retained_failure",
        }
        and _strict_database_id(witness.get("snapshot_id")) is not None
        and _canonical_uuid_string(witness.get("snapshot_batch_id")) is not None
        and _is_sha256(witness.get("fingerprint"))
        and _is_sha256(witness.get("payload_integrity_hash"))
        and _canonical_datetime_string(witness.get("updated_at")) is not None
        and (
            witness.get("failure_hash") is None
            or _is_sha256(witness.get("failure_hash"))
        )
    )


def _validated_marker(
    marker: Any,
    children: Mapping[str, DashboardSnapshot],
) -> str:
    if not isinstance(marker, dict) or set(marker) != ECONOMY_MARKER_KEYS:
        raise ValueError("Economy retained marker shape is invalid")
    reason_code = marker.get("reason_code")
    if not isinstance(reason_code, str) or reason_code not in {
        "child-retained-failure",
        "publication-postcondition",
    }:
        raise ValueError("Economy retained marker reason is invalid")
    reason = marker.get("reason")
    reason_sha256 = marker.get("reason_sha256")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or not _is_sha256(reason_sha256)
        or reason_sha256 != _sha256(reason)
    ):
        raise ValueError("Economy retained marker reason integrity is invalid")
    checked_at = _canonical_datetime_string(marker.get("checked_at"))
    if checked_at is None or checked_at > timezone.now() + timedelta(minutes=5):
        raise ValueError("Economy retained marker check time is invalid")
    witnesses = marker.get("components")
    if (
        not isinstance(witnesses, list)
        or len(witnesses) != len(ECONOMY_COMPONENTS)
        or any(
            not _strict_component_witness(witness)
            for witness in witnesses
        )
        or _canonical(witnesses) != _canonical(_live_component_witnesses(children))
    ):
        raise ValueError("Economy retained marker witness changed")
    states = {_child_state(page_key, children[page_key]) for page_key in children}
    if reason_code == "child-retained-failure" and "retained_failure" not in states:
        raise ValueError("Economy child-failure marker has no retained child")
    if reason_code == "publication-postcondition" and states != {"current_candidate"}:
        raise ValueError("Economy postcondition marker target is not current")
    latest_child_update = max(
        _canonical_datetime_string(witness["updated_at"]) for witness in witnesses
    )
    if checked_at < latest_child_update:
        raise ValueError("Economy retained marker predates its child witness")
    return reason_code


def _published_identity(snapshot: DashboardSnapshot) -> dict[str, Any] | None:
    identity = (snapshot.data or {}).get("publication_input_identity")
    return deepcopy(identity) if _strict_publication_identity(identity) else None


def _target_identity(
    children: Mapping[str, DashboardSnapshot],
) -> dict[str, Any]:
    references = {
        page_key: _component_payload(page_key, children[page_key])[2]
        for page_key in ECONOMY_COMPONENTS
    }
    return {
        page_key: _identity_from_reference(references[page_key])
        for page_key in ECONOMY_COMPONENTS
    }


def _economy_public_state(
    snapshot: DashboardSnapshot,
    replay: SimpleNamespace,
) -> str:
    children = _select_children()
    if children is None:
        raise ValueError("Economy cannot resolve all strict children")
    states = {page_key: _child_state(page_key, child) for page_key, child in children.items()}
    if any(state is None for state in states.values()):
        raise ValueError("Economy child state is missing")
    marker = (snapshot.data or {}).get("refresh_failure")
    if marker is not None:
        _validated_marker(marker, children)
        return "retained_failure"

    target_identity = _target_identity(children)
    same_target = target_identity == replay.expected_data["publication_input_identity"]
    deadline = _parse_datetime(replay.expected_data.get("fresh_until"))
    now = timezone.now()
    if states and set(states.values()) == {"current_candidate"} and same_target:
        if deadline is None or deadline < now:
            raise ValueError("Economy current target has expired root freshness")
        return "current_candidate"
    if (
        same_target
        and set(states.values()) <= {"current_candidate", "natural_expiry"}
        and "natural_expiry" in states.values()
    ):
        if deadline is None or deadline >= now:
            raise ValueError("Economy natural expiry precedes its root deadline")
        return "natural_expiry"
    if "retained_failure" in states.values():
        raise ValueError("Economy retained child lacks a validated parent marker")
    if "transition_pending" in states.values():
        return "transition_pending"
    if set(states.values()) == {"current_candidate"} and not same_target:
        newest_child = max(child.created_at for child in children.values())
        if newest_child < now - ECONOMY_TRANSITION_TIMEOUT:
            raise ValueError("Economy unpublished child transition timed out")
        return "transition_pending"
    raise ValueError("Economy child state map is not publicly representable")


def economy_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    replay = _economy_snapshot_static_replay(snapshot)
    if replay is None:
        return False
    try:
        state = _economy_public_state(snapshot, replay)
    except (TypeError, ValueError):
        return False
    if state == "current_candidate" and snapshot.quality_status != replay.expected_quality:
        return False
    if state == "retained_failure":
        if snapshot.quality_status != Observation.Quality.STALE:
            return False
    elif state in {"natural_expiry", "transition_pending"}:
        if snapshot.quality_status not in {replay.expected_quality, Observation.Quality.STALE}:
            return False
    snapshot.economy_publication_state = state
    return True


def select_public_economy_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    queryset = candidates
    if queryset is None:
        queryset = (
            DashboardSnapshot.objects.filter(
                key="economy",
                is_published=True,
                source__key="internal",
                data__contract_version=ECONOMY_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")[:50]
        )
    # Skip only statically invalid rogue rows.  Once the newest statically
    # replayable revision is found, dynamic marker/state failure is fail-closed
    # and must not silently expose an older parent.
    for candidate in queryset:
        if _economy_snapshot_static_replay(candidate) is None:
            continue
        if not economy_snapshot_is_publicly_displayable(candidate):
            return None
        presented = deepcopy(candidate)
        presented.data = deepcopy(candidate.data or {})
        presented.economy_publication_state = getattr(
            candidate,
            "economy_publication_state",
            None,
        )
        if presented.economy_publication_state != "retained_failure":
            presented.data.pop("refresh_failure", None)
        return presented
    return None


def _lock_effective_licenses(sources: Iterable[Source]) -> None:
    source_rows = list(sources)
    source_ids = {source.pk for source in source_rows}
    today = timezone.localdate()
    decisions = list(
        SourceLicense.objects.select_for_update(of=("self",))
        .filter(
            source_id__in=source_ids,
            is_current=True,
        )
        .order_by("source_id", "pk")
    )
    by_source = {decision.source_id: decision for decision in decisions}
    if set(by_source) != source_ids or len(decisions) != len(source_ids):
        raise ValueError("Economy source licence decision is incomplete")
    for source in source_rows:
        require_internal = source.key == "internal"
        if require_internal and source.kind != "derived":
            raise ValueError("Economy internal source is not derived")
        if not _license_is_effective(
            by_source[source.pk],
            today=today,
            require_derived=require_internal,
            require_storage=require_internal,
        ):
            raise ValueError("Economy source licence is not publishable")


def _lock_economy_inputs(
    requested: Mapping[str, DashboardSnapshot],
) -> dict[str, DashboardSnapshot]:
    if set(requested) != set(ECONOMY_COMPONENTS):
        raise ValueError("Economy lock requires exactly four children")
    prebuilt, _as_of, _quality_status, _metric_rows = _build_economy_payload(
        requested,
        publication_batch_id=uuid.uuid4(),
    )
    source_keys = {"internal", *prebuilt["source_keys"]}
    sources = list(Source.objects.filter(key__in=source_keys).order_by("pk"))
    if {source.key for source in sources} != source_keys:
        raise ValueError("Economy source catalogue is incomplete")
    source_ids = {source.pk for source in sources}
    list(
        Source.objects.select_for_update(of=("self",))
        .filter(pk__in=source_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    _lock_effective_licenses(sources)

    current = _select_children()
    if current is None:
        raise ValueError("Economy children changed while acquiring source locks")
    if any(current[page_key].pk != requested[page_key].pk for page_key in current):
        raise ValueError("Economy child selection changed while locking")
    current_payload, _as_of, _quality_status, metric_rows = _build_economy_payload(
        current,
        publication_batch_id=uuid.uuid4(),
    )
    if current_payload["publication_input_identity"] != prebuilt["publication_input_identity"]:
        raise ValueError("Economy child identity changed while locking")
    if not publicly_displayable_source_keys(current_payload["source_keys"]):
        raise ValueError("Economy source licence changed while locking")

    child_ids = sorted(child.pk for child in current.values())
    locked_children = {
        child.key: child
        for child in DashboardSnapshot.objects.select_for_update(of=("self",))
        .filter(pk__in=child_ids)
        .select_related("source")
        .order_by("pk")
    }
    if set(locked_children) != set(ECONOMY_COMPONENTS):
        raise ValueError("Economy child row disappeared while locking")
    selected_metric_ids = sorted(row.pk for row in metric_rows.values())
    locked_selected_metrics = list(
        MetricSnapshot.objects.select_for_update(of=("self",))
        .filter(pk__in=selected_metric_ids)
        .only("pk")
        .order_by("pk")
    )
    if [row.pk for row in locked_selected_metrics] != selected_metric_ids:
        raise ValueError("Economy selected metric row disappeared while locking")
    list(
        DashboardSnapshot.objects.select_for_update(of=("self",))
        .filter(
            key="economy",
            data__contract_version=ECONOMY_CONTRACT_VERSION,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    list(
        MetricSnapshot.objects.select_for_update(of=("self",))
        .filter(key__in={f"economy-{key}" for key in ECONOMY_REQUIRED_METRIC_KEYS})
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return current


def _store_metric_rows(snapshot: DashboardSnapshot) -> None:
    for metric in snapshot.data["metrics"]:
        value_date = _parse_datetime(metric.get("value_date") or metric.get("as_of"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if value_date is None or as_of is None or fetched_at is None:
            raise ValueError("Economy metric timestamp is invalid")
        fallback_source_key = metric.get("fallback_source")
        MetricSnapshot.objects.create(
            key=f"economy-{metric['key']}",
            label=metric["label"],
            value=Decimal(str(metric["value"])),
            display_value=str(metric.get("display_value") or ""),
            change=(
                Decimal(str(metric["change"]))
                if metric.get("change") is not None
                else None
            ),
            unit=str(metric.get("unit") or ""),
            value_date=value_date,
            as_of=as_of,
            fetched_at=fetched_at,
            batch_id=snapshot.batch_id,
            source=Source.objects.get(key=metric["source_key"]),
            fallback_source=(
                Source.objects.get(key=fallback_source_key)
                if fallback_source_key
                else None
            ),
            quality_status=metric["quality_status"],
            license_scope=str(metric["license_scope"])[:120],
            metadata=_metric_metadata(metric, snapshot.data),
        )


def publish_economy_revision(
    *,
    children: Mapping[str, DashboardSnapshot] | None = None,
    publication_batch_id: uuid.UUID | None = None,
) -> DashboardSnapshot | None:
    """Append one Economy revision from four strict current child revisions."""

    requested = dict(children or _select_children() or {})
    if set(requested) != set(ECONOMY_COMPONENTS) or any(
        _child_state(page_key, requested[page_key]) != "current_candidate"
        for page_key in ECONOMY_COMPONENTS
    ):
        raise ValueError("Economy publisher accepts only four current_candidate children")
    with transaction.atomic():
        locked = _lock_economy_inputs(requested)
        if any(
            _child_state(page_key, locked[page_key]) != "current_candidate"
            for page_key in ECONOMY_COMPONENTS
        ):
            # The locked rows are raw DB objects, so re-resolve state through
            # the strict selectors while the shared source mutex is held.
            locked = _select_children() or {}
        if set(locked) != set(ECONOMY_COMPONENTS) or any(
            _child_state(page_key, locked[page_key]) != "current_candidate"
            for page_key in ECONOMY_COMPONENTS
        ):
            raise ValueError("Economy children are not current at commit boundary")
        target_identity = _target_identity(locked)
        existing = []
        for candidate in (
            DashboardSnapshot.objects.filter(
                key="economy",
                is_published=True,
                data__contract_version=ECONOMY_CONTRACT_VERSION,
            )
            .select_related("source")
            .order_by("-created_at", "-id")
        ):
            if _published_identity(candidate) != target_identity:
                continue
            # A malformed row that copied a valid input identity is a rogue
            # candidate, not an idempotence witness and not a publication DoS.
            if _economy_snapshot_static_replay(candidate) is not None:
                existing.append(candidate)
        if len(existing) > 1:
            raise ValueError("Economy input identity has multiple revisions")
        if existing:
            if _economy_snapshot_static_replay(existing[0]) is None:
                raise ValueError("Economy existing identity is not replayable")
            return None
        batch_id = publication_batch_id or uuid.uuid4()
        data, as_of, quality_status, _metric_rows = _build_economy_payload(
            locked,
            publication_batch_id=batch_id,
        )
        snapshot = DashboardSnapshot.objects.create(
            key="economy",
            title=ECONOMY_TITLE,
            summary=ECONOMY_SUMMARY,
            as_of=as_of,
            batch_id=batch_id,
            quality_status=quality_status,
            data=data,
            source=Source.objects.get(key="internal"),
            is_published=True,
        )
        _store_metric_rows(snapshot)
        if _economy_snapshot_static_replay(snapshot) is None:
            raise EconomyPublicationPostconditionError(
                "Economy static publication postcondition failed"
            )
        selected = select_public_economy_snapshot([snapshot])
        if (
            selected is None
            or getattr(selected, "economy_publication_state", None)
            != "current_candidate"
        ):
            raise EconomyPublicationPostconditionError(
                "Economy revision is not current_candidate"
            )
        return snapshot


def _latest_replayable_economy(
    *,
    exclude_identity: Mapping[str, Any] | None = None,
) -> DashboardSnapshot | None:
    candidates = (
        DashboardSnapshot.objects.filter(
            key="economy",
            is_published=True,
            data__contract_version=ECONOMY_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")
    )
    for candidate in candidates:
        if exclude_identity is not None and _published_identity(candidate) == exclude_identity:
            continue
        if _economy_snapshot_static_replay(candidate) is not None:
            return candidate
    return None


def _mark_retained_failure(
    snapshot: DashboardSnapshot,
    children: Mapping[str, DashboardSnapshot],
    *,
    reason_code: str,
    reason: str,
) -> None:
    witnesses = _live_component_witnesses(children)
    checked_at = max(
        timezone.now(),
        *(child.updated_at for child in children.values()),
    )
    data = deepcopy(snapshot.data or {})
    reason_text = reason[:1200]
    data["refresh_failure"] = {
        "reason_code": reason_code,
        "checked_at": checked_at.isoformat(),
        "reason": reason_text,
        "reason_sha256": _sha256(reason_text),
        "components": witnesses,
    }
    snapshot.data = data
    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])


def _retain_failure_marker(
    *,
    reason_code: str,
    reason: str,
    exclude_identity: Mapping[str, Any] | None = None,
) -> bool:
    requested = _select_children()
    if requested is None:
        return False
    with transaction.atomic():
        locked_children = _lock_economy_inputs(requested)
        # Preserve the strict state-bearing copies rather than raw locked rows.
        current = _select_children()
        if current is None or any(
            current[page_key].pk != locked_children[page_key].pk
            for page_key in ECONOMY_COMPONENTS
        ):
            raise ValueError("Economy marker target changed while locking")
        previous = _latest_replayable_economy(exclude_identity=exclude_identity)
        if previous is None:
            return False
        _mark_retained_failure(
            previous,
            current,
            reason_code=reason_code,
            reason=reason,
        )
    selected = select_public_economy_snapshot()
    if (
        selected is None
        or getattr(selected, "economy_publication_state", None) != "retained_failure"
    ):
        raise ValueError("Economy retained marker did not replay")
    return True


def coordinate_economy_dashboard(
    _runs: Iterable[Any] | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    """Publish a complete Economy parent or retain one validated prior parent."""

    children = _select_children()
    if children is None:
        return [], {"economy"}
    states = {page_key: _child_state(page_key, child) for page_key, child in children.items()}
    if "retained_failure" in states.values():
        retained = _retain_failure_marker(
            reason_code="child-retained-failure",
            reason=(
                "至少一个官方经济子页正在保留其上一版已审计快照；经济总览"
                "同步保留上一版完整四组件组合。"
            ),
        )
        return [], ({"economy"} if retained else {"economy"})
    selected = select_public_economy_snapshot()
    if (
        selected is not None
        and getattr(selected, "economy_publication_state", None)
        == "natural_expiry"
    ):
        return [], {"economy"}
    if set(states.values()) != {"current_candidate"}:
        return [], {"economy"}
    target_identity = _target_identity(children)
    try:
        with transaction.atomic():
            published = publish_economy_revision(children=children)
            current = select_public_economy_snapshot()
            if (
                current is None
                or getattr(current, "economy_publication_state", None)
                != "current_candidate"
                or _published_identity(current) != target_identity
            ):
                raise EconomyPublicationPostconditionError(
                    "Economy successful publication failed commit-boundary selection"
                )
        return ([published] if published is not None else []), set()
    except EconomyPublicationPostconditionError as exc:
        retained = _retain_failure_marker(
            reason_code="publication-postcondition",
            reason=(
                "最新四子页组合未通过 Economy current_candidate 发布后置校验；"
                f"本次 revision 已原子回滚并保留上一版：{exc}"
            ),
            exclude_identity=target_identity,
        )
        if retained:
            return [], {"economy"}
        raise
