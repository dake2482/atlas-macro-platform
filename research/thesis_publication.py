"""Fail-closed publication contract for daily evidence and public theses."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db import connection, transaction
from django.db.models import QuerySet
from django.utils import timezone

from .models import (
    DashboardSnapshot,
    EvidenceItem,
    Invalidation,
    MetricSnapshot,
    Observation,
    Source,
    Thesis,
    Trigger,
)
from .services import current_display_source_key_sets

DAILY_EVIDENCE_KEY = "daily-evidence"
DAILY_EVIDENCE_CONTRACT_VERSION = 1
DAILY_EVIDENCE_COMPONENT_KEYS = ("economy", "liquidity", "rates")
DAILY_EVIDENCE_PREFERRED_METRICS = {
    "economy": "bea-a191rl",
    "liquidity": "net-liquidity",
    "rates": "ust-10y",
}
MINIMUM_EVIDENCE_ITEMS = 3
MAX_DATABASE_ID = (2**63) - 1
METRIC_VALUE_QUANTUM = Decimal("0.00000001")
PUBLIC_QUALITY_STATES = {
    Observation.Quality.FRESH,
    Observation.Quality.ESTIMATED,
}
MAX_PAYLOAD_DEPTH = 32
MAX_PAYLOAD_CONTAINERS = 20_000
DAILY_EVIDENCE_ADVISORY_LOCK_ID = 0x41544C4153444159


@dataclass(frozen=True)
class PublicationOutcome:
    published_ids: tuple[int, ...]
    errors: dict[int, tuple[str, ...]]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class DailyEvidencePublicationOutcome:
    snapshot: DashboardSnapshot | None
    created: bool
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.snapshot is not None and not self.errors


class _CandidateRejected(Exception):
    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(dict.fromkeys(str(error) for error in errors if error))
        super().__init__("; ".join(self.errors))


def _lock_daily_evidence_producer() -> None:
    """Serialize producers before row locks, including the first parent insert.

    Once a daily-evidence parent exists, every content flow locks parent,
    components, then metrics.  The transaction-scoped PostgreSQL mutex covers
    the initial no-parent case where no row exists to serialize two producers.
    SQLite test transactions intentionally need no equivalent process mutex.
    """

    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            [DAILY_EVIDENCE_ADVISORY_LOCK_ID],
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if timezone.is_naive(value) else value


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _aware(parsed)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _payload_containers(
    value: Any,
    *,
    root_path: str = "payload",
) -> tuple[list[tuple[dict[str, Any] | list[Any], str]], bool]:
    """Iteratively walk untrusted JSON with explicit resource bounds."""

    if not isinstance(value, (dict, list)):
        return [], False
    containers: list[tuple[dict[str, Any] | list[Any], str]] = []
    stack: list[tuple[dict[str, Any] | list[Any], str, int]] = [
        (value, root_path, 0)
    ]
    seen: set[int] = set()
    malformed = False
    while stack:
        current, path, depth = stack.pop()
        if id(current) in seen:
            malformed = True
            continue
        seen.add(id(current))
        if depth > MAX_PAYLOAD_DEPTH or len(containers) >= MAX_PAYLOAD_CONTAINERS:
            malformed = True
            continue
        containers.append((current, path))
        if isinstance(current, dict):
            children = [
                (nested, f"{path}.{key}", depth + 1)
                for key, nested in current.items()
                if isinstance(nested, (dict, list))
            ]
        else:
            children = [
                (nested, f"{path}[{index}]", depth + 1)
                for index, nested in enumerate(current)
                if isinstance(nested, (dict, list))
            ]
        stack.extend(reversed(children))
    return containers, malformed


def _payload_source_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    containers, _malformed = _payload_containers(value)
    for current, _path in containers:
        if not isinstance(current, dict):
            continue
        for field in ("source_key", "fallback_source", "fallback_source_key"):
            if current.get(field):
                keys.add(str(current[field]))
        for field in ("source_keys", "_source_keys"):
            raw = current.get(field)
            if isinstance(raw, list):
                keys.update(str(item) for item in raw if item)
    return keys


def _payload_has_fallback(value: Any) -> bool:
    containers, _malformed = _payload_containers(value)
    return any(
        isinstance(current, dict)
        and any(current.get(field) for field in ("fallback_source", "fallback_source_key"))
        for current, _path in containers
    )


def _payload_shape_errors(value: Any, *, path: str = "payload") -> list[str]:
    errors: list[str] = []
    containers, malformed = _payload_containers(value, root_path=path)
    if malformed:
        errors.append(f"{path} is cyclic, too deep or too large")
    for current, current_path in containers:
        if not isinstance(current, dict):
            continue
        for field in (
            "source_keys",
            "_source_keys",
            "batch_ids",
            "_batch_ids",
            "component_batches",
            "input_batch_ids",
        ):
            if field in current and not isinstance(current[field], list):
                errors.append(f"{current_path}.{field} is not a list")
        for field in ("source_key", "fallback_source", "fallback_source_key"):
            if (
                field in current
                and current[field] is not None
                and not isinstance(current[field], str)
            ):
                errors.append(f"{current_path}.{field} is not a string")
    return errors


def _payload_deadline_state(value: Any) -> tuple[list[datetime], bool]:
    deadlines: list[datetime] = []
    containers, malformed = _payload_containers(value)
    for current, _path in containers:
        if isinstance(current, dict) and "fresh_until" in current:
            parsed = _parse_datetime(current.get("fresh_until"))
            if parsed is None:
                malformed = True
            else:
                deadlines.append(parsed)
    return deadlines, malformed


def _payload_quality_states(value: Any) -> set[str]:
    containers, _malformed = _payload_containers(value)
    return {
        str(current["quality_status"])
        for current, _path in containers
        if isinstance(current, dict) and current.get("quality_status")
    }


def _payload_timestamp_cutoff_errors(
    value: Any,
    *,
    cutoff: datetime,
    label: str,
) -> list[str]:
    errors: list[str] = []
    containers, malformed = _payload_containers(value, root_path=label)
    if malformed:
        return [f"{label} is cyclic, too deep or too large"]
    for current, path in containers:
        if not isinstance(current, dict):
            continue
        for field in ("value_date", "as_of", "fetched_at"):
            if field not in current:
                continue
            parsed = _parse_datetime(current.get(field))
            if parsed is None:
                errors.append(f"{path}.{field} is malformed")
            elif parsed > cutoff:
                errors.append(f"{path}.{field} is after the publication cutoff")
    return errors


def _payload_requires_derived_display(value: Any) -> bool:
    containers, malformed = _payload_containers(value)
    if malformed:
        return True
    for current, _path in containers:
        if not isinstance(current, dict):
            continue
        if current.get("quality_status") == Observation.Quality.ESTIMATED:
            return True
        metadata = current.get("metadata")
        if isinstance(metadata, dict) and any(
            metadata.get(field)
            for field in ("formula", "calculation_owner", "model_label")
        ):
            return True
    return False


def _source_licence_errors(
    public_keys: Iterable[str],
    derived_keys: Iterable[str],
    *,
    allowed_public_source_keys: set[str] | None,
    allowed_derived_source_keys: set[str] | None,
) -> list[str]:
    required_public = {str(key) for key in public_keys if key}
    required_derived = {str(key) for key in derived_keys if key}
    if not required_public and not required_derived:
        return []
    if allowed_public_source_keys is None or allowed_derived_source_keys is None:
        loaded_public, loaded_derived = current_display_source_key_sets(
            required_public | required_derived
        )
        if allowed_public_source_keys is None:
            allowed_public_source_keys = loaded_public
        if allowed_derived_source_keys is None:
            allowed_derived_source_keys = loaded_derived
    errors: list[str] = []
    missing_public = sorted(required_public - allowed_public_source_keys)
    if missing_public:
        errors.append(
            "unlicensed public source(s): " + ", ".join(missing_public)
        )
    missing_derived = sorted(required_derived - allowed_derived_source_keys)
    if missing_derived:
        errors.append(
            "source(s) lack derived-display permission: "
            + ", ".join(missing_derived)
        )
    return errors


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _valid_fingerprint(value: Any) -> bool:
    fingerprint = str(value or "")
    return len(fingerprint) == 64 and all(
        character in "0123456789abcdef" for character in fingerprint.lower()
    )


def _canonical_hash(value: Any) -> str:
    def canonical_default(item: Any) -> str:
        if isinstance(item, datetime):
            return _aware(item).astimezone(UTC).isoformat()
        if isinstance(item, date):
            return item.isoformat()
        return str(item)

    try:
        serialized = json.dumps(
            value,
            default=canonical_default,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return ""
    return hashlib.sha256(serialized.encode()).hexdigest()


def component_reference_fingerprint(reference: dict[str, Any]) -> str:
    payload = dict(reference)
    payload.pop("component_payload_sha256", None)
    return _canonical_hash(payload)


def component_data_fingerprint(data: dict[str, Any]) -> str:
    return _canonical_hash(data)


def daily_evidence_payload_fingerprint(data: dict[str, Any]) -> str:
    payload = dict(data)
    payload.pop("fingerprint", None)
    return _canonical_hash(payload)


def daily_evidence_component_set_fingerprint(data: dict[str, Any]) -> str:
    """Hash only frozen component/evidence inputs, excluding parent-run identity."""

    return _canonical_hash(
        {
            "component_snapshots": data.get("component_snapshots"),
            "evidence_metric_ids": data.get("evidence_metric_ids"),
            "evidence_items": data.get("evidence_items"),
        }
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _positive_database_id(value: Any) -> int | None:
    """Parse an untrusted JSON primary key without raising or accepting booleans."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or len(stripped) > 19 or not stripped.isascii() or not stripped.isdigit():
            return None
        try:
            parsed = int(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    return parsed if 0 < parsed <= MAX_DATABASE_ID else None


def _metric_values_match(payload_value: Any, stored_value: Decimal | None) -> bool:
    parsed = _decimal(payload_value)
    if (
        stored_value is None
        or not stored_value.is_finite()
        or parsed is None
        or not parsed.is_finite()
    ):
        return False
    try:
        return parsed.quantize(METRIC_VALUE_QUANTUM) == stored_value.quantize(
            METRIC_VALUE_QUANTUM
        )
    except ArithmeticError:
        return False


def _metric_contract_errors(
    metric: MetricSnapshot,
    payload: Any,
    *,
    label: str,
    current_time: datetime,
    cutoff: datetime,
    not_before: datetime,
    require_current: bool,
    expected_key: str | None = None,
    batch_mode: str = "normalized",
) -> list[str]:
    if not isinstance(payload, dict):
        return [f"{label} is not an object"]
    errors: list[str] = []
    required_fields = {
        "key",
        "value",
        "display_value",
        "unit",
        "value_date",
        "as_of",
        "fetched_at",
        "batch_id",
        "quality_status",
        "source_key",
        "license_scope",
        "fresh_until",
    }
    missing = sorted(required_fields - payload.keys())
    if missing:
        errors.append(f"{label} lacks fields: {', '.join(missing)}")
    if payload.get("key") != (expected_key or metric.key):
        errors.append(f"{label} key does not match its declared component key")
    if not _metric_values_match(payload.get("value"), metric.value):
        errors.append(f"{label} value does not match MetricSnapshot")
    if payload.get("display_value") != metric.display_value:
        errors.append(f"{label} display value does not match MetricSnapshot")
    if payload.get("unit") != metric.unit:
        errors.append(f"{label} unit does not match MetricSnapshot")
    for field in ("value_date", "as_of", "fetched_at"):
        parsed = _parse_datetime(payload.get(field))
        expected = _aware(getattr(metric, field))
        if parsed != expected:
            errors.append(f"{label} {field} does not match MetricSnapshot")
        if parsed is not None and parsed > cutoff:
            errors.append(f"{label} {field} is after the publication cutoff")
    if batch_mode == "component":
        expected_batch = _mapping(metric.metadata).get("component_batch_id")
        if not expected_batch:
            errors.append(f"{label} normalized metric lacks component batch lineage")
    else:
        expected_batch = metric.batch_id
    if str(payload.get("batch_id") or "") != str(expected_batch):
        errors.append(f"{label} batch does not match MetricSnapshot")
    if payload.get("quality_status") != metric.quality_status:
        errors.append(f"{label} quality does not match MetricSnapshot")
    if payload.get("source_key") != metric.source.key:
        errors.append(f"{label} source does not match MetricSnapshot")
    if payload.get("license_scope") != metric.license_scope:
        errors.append(f"{label} licence scope does not match MetricSnapshot")
    fresh_until = _parse_datetime(payload.get("fresh_until"))
    if fresh_until is None:
        errors.append(f"{label} fresh_until is missing or malformed")
    else:
        if fresh_until < not_before:
            errors.append(f"{label} was already stale when daily-evidence was created")
        if require_current and fresh_until < current_time:
            errors.append(f"{label} is stale at validation time")
    return errors


def _frozen_evidence_errors(
    reference: Any,
    component_metric: Any,
    *,
    metric_id: int,
    component_key: str,
    component_snapshot_batch: str,
    current_time: datetime,
    cutoff: datetime,
    not_before: datetime,
    require_current: bool,
) -> list[str]:
    label = f"daily-evidence metric {metric_id}"
    if not isinstance(reference, dict) or not isinstance(component_metric, dict):
        return [f"{label} frozen contract is malformed"]
    errors: list[str] = []
    required_fields = {
        "component",
        "component_metric_key",
        "metric_id",
        "key",
        "value",
        "display_value",
        "unit",
        "value_date",
        "as_of",
        "fetched_at",
        "batch_id",
        "quality_status",
        "source_key",
        "license_scope",
        "fresh_until",
    }
    missing = sorted(required_fields - reference.keys())
    if missing:
        errors.append(f"{label} lacks fields: {', '.join(missing)}")
    if reference.get("component") != component_key:
        errors.append(f"{label} component does not match")
    if str(reference.get("metric_id")) != str(metric_id):
        errors.append(f"{label} id does not match")
    if str(reference.get("batch_id") or "") != component_snapshot_batch:
        errors.append(f"{label} normalized batch does not match component publication")
    if reference.get("quality_status") not in PUBLIC_QUALITY_STATES:
        errors.append(f"{label} quality is not publishable")
    value = _decimal(reference.get("value"))
    if value is None or not value.is_finite():
        errors.append(f"{label} value is null or non-finite")
    elif not _metric_values_match(component_metric.get("value"), value):
        errors.append(f"{label} value differs from frozen component metric")
    for field in (
        "display_value",
        "unit",
        "value_date",
        "as_of",
        "fetched_at",
        "quality_status",
        "source_key",
        "license_scope",
        "fresh_until",
    ):
        if str(reference.get(field)) != str(component_metric.get(field)):
            errors.append(f"{label} {field} differs from frozen component metric")
    if reference.get("component_metric_key") != component_metric.get("key"):
        errors.append(f"{label} component metric key does not match")
    expected_normalized_key = f"{component_key}-{str(component_metric.get('key') or '').lower()}"
    if reference.get("key") != expected_normalized_key:
        errors.append(f"{label} normalized key does not match component metric")
    for field in ("value_date", "as_of", "fetched_at"):
        parsed = _parse_datetime(reference.get(field))
        if parsed is None or parsed > cutoff:
            errors.append(f"{label} {field} is missing or after the publication cutoff")
    fresh_until = _parse_datetime(reference.get("fresh_until"))
    if fresh_until is None:
        errors.append(f"{label} fresh_until is missing or malformed")
    else:
        if fresh_until < not_before:
            errors.append(f"{label} was stale when daily-evidence was created")
        if require_current and fresh_until < current_time:
            errors.append(f"{label} is stale at validation time")
    if not reference.get("source_key") or not reference.get("license_scope"):
        errors.append(f"{label} source or licence scope is missing")
    return errors


def _component_reference_map(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    references = data.get("component_snapshots")
    if not isinstance(references, list):
        return {}
    mapped: dict[str, dict[str, Any]] = {}
    for item in references:
        if not isinstance(item, dict):
            continue
        key = str(item.get("page_key") or item.get("component") or "")
        if not key or key in mapped:
            return {}
        mapped[key] = item
    return mapped


def _snapshot_batches(snapshot: DashboardSnapshot) -> set[str]:
    batches = {str(snapshot.batch_id)}
    raw = _mapping(snapshot.data).get("component_batches")
    if isinstance(raw, list):
        batches.update(str(item) for item in raw if item)
    return batches


def _snapshot_sources(snapshot: DashboardSnapshot) -> set[str]:
    return {snapshot.source.key, *_payload_source_keys(_mapping(snapshot.data))}


def validate_daily_evidence_snapshot(
    snapshot: DashboardSnapshot | None,
    *,
    now: datetime | None = None,
    require_live_components: bool = False,
    require_current_components: bool = False,
    require_latest_snapshot: bool = False,
    allowed_public_source_keys: set[str] | None = None,
    allowed_derived_source_keys: set[str] | None = None,
) -> tuple[str, ...]:
    """Validate an immutable daily-evidence parent and its exact child snapshots."""

    current_time = _aware(now or timezone.now())
    errors: list[str] = []
    if snapshot is None:
        return ("daily-evidence snapshot is missing",)
    cutoff = _aware(snapshot.created_at)
    require_live_components = require_live_components or require_current_components
    if cutoff > current_time:
        errors.append("daily-evidence creation cutoff is in the future")
    data = _mapping(snapshot.data)
    if not isinstance(snapshot.data, dict):
        errors.append("daily-evidence payload is not an object")
    errors.extend(_payload_shape_errors(data, path="daily-evidence"))
    if snapshot.key != DAILY_EVIDENCE_KEY:
        errors.append("snapshot key is not daily-evidence")
    if require_latest_snapshot:
        latest_id = (
            DashboardSnapshot.objects.filter(key=DAILY_EVIDENCE_KEY)
            .order_by("-created_at", "-id")
            .values_list("pk", flat=True)
            .first()
        )
        if latest_id != snapshot.pk:
            errors.append("daily-evidence snapshot is not the newest candidate")
    if not snapshot.is_published:
        errors.append("daily-evidence snapshot is not published")
    if snapshot.source.key != "internal":
        errors.append("daily-evidence snapshot source is not internal")
    if data.get("contract_version") != DAILY_EVIDENCE_CONTRACT_VERSION:
        errors.append("daily-evidence contract version is not 1")
    if data.get("demo") is not False:
        errors.append("daily-evidence snapshot is demo or lacks demo=false")
    if snapshot.quality_status not in PUBLIC_QUALITY_STATES:
        errors.append("daily-evidence snapshot quality is not publishable")
    if data.get("refresh_failure"):
        errors.append("daily-evidence snapshot carries a refresh failure")
    if _payload_has_fallback(data):
        errors.append("daily-evidence snapshot contains fallback lineage")
    if snapshot.as_of > cutoff:
        errors.append("daily-evidence snapshot as_of is after the publication cutoff")
    research_date = _parse_date(data.get("research_date"))
    if research_date is None:
        errors.append("daily-evidence research_date is missing or malformed")
    else:
        if research_date != timezone.localdate(cutoff):
            errors.append("daily-evidence research_date does not match its creation date")
        if research_date > timezone.localdate(current_time):
            errors.append("daily-evidence research_date is in the future")
        if (
            require_current_components
            and research_date != timezone.localdate(current_time)
        ):
            errors.append("daily-evidence research_date is not the current local date")
    if data.get("publication_batch_id") != str(snapshot.batch_id):
        errors.append("daily-evidence publication batch does not match snapshot batch")
    if not _valid_fingerprint(data.get("fingerprint")):
        errors.append("daily-evidence fingerprint is missing or malformed")
    elif data.get("fingerprint") != daily_evidence_payload_fingerprint(data):
        errors.append("daily-evidence payload fingerprint does not match content")
    if data.get("component_set_sha256") != daily_evidence_component_set_fingerprint(
        data
    ):
        errors.append("daily-evidence component-set fingerprint does not match content")

    required = data.get("required_components")
    if not isinstance(required, list) or set(map(str, required)) != set(
        DAILY_EVIDENCE_COMPONENT_KEYS
    ):
        errors.append("daily-evidence required component set is incomplete")
    references = _component_reference_map(data)
    if set(references) != set(DAILY_EVIDENCE_COMPONENT_KEYS):
        errors.append("daily-evidence component references are incomplete or duplicated")

    component_batches: set[str] = set()
    component_sources: set[str] = set()
    component_as_of_values: list[datetime] = []
    component_quality_states: set[str] = set()
    component_states: dict[str, dict[str, Any]] = {}
    for page_key in DAILY_EVIDENCE_COMPONENT_KEYS:
        reference = references.get(page_key)
        if reference is None:
            continue
        errors.extend(_payload_shape_errors(reference, path=f"reference.{page_key}"))
        if reference.get("demo") is not False:
            errors.append(f"{page_key} frozen reference is demo or lacks demo=false")
        if reference.get("contract_version") != 1:
            errors.append(f"{page_key} frozen contract version is not 1")
        if not _valid_fingerprint(reference.get("fingerprint")):
            errors.append(f"{page_key} frozen fingerprint is malformed")
        if not _valid_fingerprint(reference.get("component_data_sha256")):
            errors.append(f"{page_key} frozen component data hash is malformed")
        if reference.get("component_payload_sha256") != component_reference_fingerprint(
            reference
        ):
            errors.append(f"{page_key} frozen component payload hash is invalid")
        normalized_snapshot_id = _positive_database_id(reference.get("snapshot_id"))
        if normalized_snapshot_id is None:
            errors.append(f"{page_key} component snapshot id is malformed")
            continue
        publication_batch_id = str(reference.get("publication_batch_id") or "")
        if not publication_batch_id:
            errors.append(f"{page_key} frozen publication batch is missing")
        frozen_as_of = _parse_datetime(reference.get("as_of"))
        if frozen_as_of is None or frozen_as_of > cutoff:
            errors.append(f"{page_key} frozen as_of is missing or after cutoff")
        else:
            component_as_of_values.append(frozen_as_of)
        errors.extend(
            _payload_timestamp_cutoff_errors(
                reference,
                cutoff=cutoff,
                label=f"reference.{page_key}",
            )
        )
        if reference.get("quality_status") not in PUBLIC_QUALITY_STATES:
            errors.append(f"{page_key} frozen quality is not publishable")
        if not isinstance(reference.get("component_batches"), list):
            errors.append(f"{page_key} frozen component_batches is not a list")
            batches: set[str] = set()
        else:
            batches = {str(item) for item in reference["component_batches"] if item}
        if not batches or publication_batch_id not in batches:
            errors.append(f"{page_key} frozen component batch set is incomplete")
        if not isinstance(reference.get("source_keys"), list):
            errors.append(f"{page_key} frozen source_keys is not a list")
            sources: set[str] = set()
        else:
            sources = {str(item) for item in reference["source_keys"] if item}
        frozen_source_key = str(reference.get("source_key") or "")
        if not sources or not frozen_source_key or frozen_source_key not in sources:
            errors.append(f"{page_key} frozen source set is incomplete")
        quality_states = _payload_quality_states(reference)
        component_quality_states.update(quality_states)
        if not quality_states or not quality_states <= PUBLIC_QUALITY_STATES:
            errors.append(f"{page_key} frozen component contains unsafe nested quality")
        deadlines, malformed_deadline = _payload_deadline_state(reference)
        if malformed_deadline or not deadlines:
            errors.append(f"{page_key} frozen freshness is missing or malformed")
        reference_deadline = _parse_datetime(reference.get("fresh_until"))
        if reference_deadline is None:
            errors.append(f"{page_key} reference fresh_until is missing or malformed")
        elif deadlines and reference_deadline != min(deadlines):
            errors.append(f"{page_key} reference freshness does not match frozen metrics")
        if deadlines and min(deadlines) < cutoff:
            errors.append(f"{page_key} component was stale when daily-evidence was created")
        metric_rows = reference.get("metrics")
        component_metrics: dict[str, dict[str, Any]] = {}
        if not isinstance(metric_rows, list) or not metric_rows:
            errors.append(f"{page_key} frozen component has no metric payload")
        else:
            for metric_row in metric_rows:
                if not isinstance(metric_row, dict) or not metric_row.get("key"):
                    errors.append(f"{page_key} component has a malformed metric payload")
                    continue
                metric_key = str(metric_row["key"])
                if metric_key in component_metrics:
                    errors.append(f"{page_key} frozen component duplicates metric {metric_key}")
                    continue
                component_metrics[metric_key] = metric_row
        component_states[page_key] = {
            "batches": batches,
            "metrics": component_metrics,
            "snapshot_batch": publication_batch_id,
        }
        component_batches.update(batches)
        component_sources.update(sources)

        if require_live_components:
            try:
                component = DashboardSnapshot.objects.select_related("source").get(
                    pk=normalized_snapshot_id
                )
            except DashboardSnapshot.DoesNotExist:
                errors.append(f"{page_key} live component snapshot is missing")
                continue
            component_data = _mapping(component.data)
            if _aware(component.created_at) > cutoff:
                errors.append(
                    f"{page_key} live component was created after daily-evidence"
                )
            errors.extend(
                _payload_shape_errors(component_data, path=f"live-component.{page_key}")
            )
            if (
                component.key != page_key
                or not component.is_published
                or component_data.get("contract_version") != 1
                or component_data.get("demo") is not False
                or component.quality_status not in PUBLIC_QUALITY_STATES
                or component_data.get("refresh_failure")
                or _payload_has_fallback(component_data)
            ):
                errors.append(f"{page_key} live component state is invalid")
            live_quality_states = _payload_quality_states(component_data)
            live_deadlines, malformed_live_deadline = _payload_deadline_state(
                component_data
            )
            if (
                not live_quality_states
                or not live_quality_states <= PUBLIC_QUALITY_STATES
                or malformed_live_deadline
                or not live_deadlines
            ):
                errors.append(f"{page_key} live nested quality or freshness is invalid")
            elif require_current_components and min(live_deadlines) < current_time:
                errors.append(f"{page_key} live component payload is stale")
            if (
                str(component.batch_id) != publication_batch_id
                or component_data.get("fingerprint") != reference.get("fingerprint")
                or component_data_fingerprint(component_data)
                != reference.get("component_data_sha256")
                or component_data.get("publication_batch_id") != publication_batch_id
                or component_data.get("fresh_until") != reference.get("fresh_until")
                or component.source.key != frozen_source_key
                or component.as_of != frozen_as_of
                or component.quality_status != reference.get("quality_status")
                or _snapshot_batches(component) != batches
                or _snapshot_sources(component) != sources
            ):
                errors.append(f"{page_key} live component differs from frozen reference")
            live_metric_rows = component_data.get("metrics")
            live_metrics = {
                str(item.get("key")): item
                for item in live_metric_rows
                if isinstance(item, dict) and item.get("key")
            } if isinstance(live_metric_rows, list) else {}
            if {
                key: _canonical_hash(item) for key, item in live_metrics.items()
            } != {
                key: _canonical_hash(item) for key, item in component_metrics.items()
            }:
                errors.append(f"{page_key} live metric payload differs from frozen reference")
            if require_current_components:
                latest = (
                    DashboardSnapshot.objects.filter(key=page_key, is_published=True)
                    .order_by("-created_at", "-id")
                    .first()
                )
                if latest is None or latest.pk != component.pk:
                    errors.append(
                        f"{page_key} reference is not the latest published component"
                    )
            if require_current_components and (
                not deadlines or min(deadlines) < current_time
            ):
                errors.append(f"{page_key} component is stale at generation time")

    declared_batches = data.get("component_batches")
    if not isinstance(declared_batches, list) or set(map(str, declared_batches)) != component_batches:
        errors.append("daily-evidence component batch union is not exact")
    declared_sources = data.get("source_keys")
    if not isinstance(declared_sources, list) or set(map(str, declared_sources)) != component_sources:
        errors.append("daily-evidence source-key union is not exact")
    if component_as_of_values and snapshot.as_of != min(component_as_of_values):
        errors.append("daily-evidence as_of is not the minimum component as_of")
    expected_quality = (
        Observation.Quality.ESTIMATED
        if Observation.Quality.ESTIMATED in component_quality_states
        else Observation.Quality.FRESH
    )
    if component_quality_states and snapshot.quality_status != expected_quality:
        errors.append("daily-evidence quality does not match frozen components")
    all_source_keys = {snapshot.source.key, *component_sources, *_payload_source_keys(data)}

    evidence_metric_ids = data.get("evidence_metric_ids")
    normalized_metric_ids: list[int] = []
    raw_metric_id_count = -1
    if isinstance(evidence_metric_ids, list):
        raw_metric_id_count = len(evidence_metric_ids)
        for item in evidence_metric_ids:
            normalized_id = _positive_database_id(item)
            if normalized_id is None:
                normalized_metric_ids = []
                break
            normalized_metric_ids.append(normalized_id)
    if (
        not normalized_metric_ids
        or len(normalized_metric_ids) != raw_metric_id_count
        or len(set(normalized_metric_ids)) != len(normalized_metric_ids)
        or len(set(normalized_metric_ids)) < MINIMUM_EVIDENCE_ITEMS
    ):
        errors.append("daily-evidence has fewer than three evidence metrics")
    else:
        raw_evidence_items = data.get("evidence_items")
        evidence_references: dict[int, dict[str, Any]] = {}
        if not isinstance(raw_evidence_items, list):
            errors.append("daily-evidence evidence-item contract is missing")
        else:
            for reference in raw_evidence_items:
                if not isinstance(reference, dict):
                    errors.append("daily-evidence contains a malformed evidence item")
                    continue
                normalized_id = _positive_database_id(reference.get("metric_id"))
                if normalized_id is None:
                    errors.append("daily-evidence contains a malformed evidence metric id")
                    continue
                if normalized_id in evidence_references:
                    errors.append("daily-evidence duplicates an evidence metric reference")
                    continue
                evidence_references[normalized_id] = reference
        if set(evidence_references) != set(normalized_metric_ids):
            errors.append("daily-evidence evidence-item ids are not exact")
        referenced_components = {
            str(item.get("component") or "") for item in evidence_references.values()
        }
        if referenced_components != set(DAILY_EVIDENCE_COMPONENT_KEYS):
            errors.append("daily-evidence does not contain evidence from every component")
        metrics = (
            {
                item.pk: item
                for item in MetricSnapshot.objects.select_related(
                    "source", "fallback_source"
                ).filter(pk__in=normalized_metric_ids)
            }
            if require_live_components
            else {}
        )
        if require_live_components and set(metrics) != set(normalized_metric_ids):
            errors.append("daily-evidence references a missing current evidence metric")
        for metric_id in normalized_metric_ids:
            evidence_reference = evidence_references.get(metric_id)
            component_key = (
                str(evidence_reference.get("component") or "")
                if evidence_reference
                else ""
            )
            component_state = component_states.get(component_key)
            component_metric_key = ""
            component_metric = None
            if component_state is None:
                errors.append(f"evidence metric {metric_id} has no declared component")
            else:
                component_metric_key = (
                    str(evidence_reference.get("component_metric_key") or "")
                    if evidence_reference
                    else ""
                )
                component_metric = component_state["metrics"].get(component_metric_key)
                if component_metric is None:
                    errors.append(
                        f"evidence metric {metric_id} is absent from {component_key} payload"
                    )
                else:
                    errors.extend(
                        _frozen_evidence_errors(
                            evidence_reference,
                            component_metric,
                            metric_id=metric_id,
                            component_key=component_key,
                            component_snapshot_batch=component_state["snapshot_batch"],
                            current_time=current_time,
                            cutoff=cutoff,
                            not_before=cutoff,
                            require_current=require_current_components,
                        )
                    )
            if require_live_components:
                metric = metrics.get(metric_id)
                if metric is None:
                    continue
                if _aware(metric.created_at) > cutoff:
                    errors.append(
                        f"evidence metric {metric_id} was created after daily-evidence"
                    )
                if (
                    metric.value is None
                    or not metric.value.is_finite()
                    or metric.quality_status not in PUBLIC_QUALITY_STATES
                    or metric.fallback_source_id
                ):
                    errors.append(f"evidence metric {metric_id} has unsafe value or quality")
                if not metric.license_scope:
                    errors.append(f"evidence metric {metric_id} lacks a licence scope")
                if any(
                    value > cutoff
                    for value in (metric.value_date, metric.as_of, metric.fetched_at)
                ):
                    errors.append(
                        f"evidence metric {metric_id} contains a post-cutoff timestamp"
                    )
                if component_state and str(metric.batch_id) != component_state["snapshot_batch"]:
                    errors.append(
                        f"evidence metric {metric_id} normalized batch does not match component"
                    )
                if evidence_reference is not None:
                    errors.extend(
                        _metric_contract_errors(
                            metric,
                            evidence_reference,
                            label=f"daily-evidence metric {metric_id}",
                            current_time=current_time,
                            cutoff=cutoff,
                            not_before=cutoff,
                            require_current=require_current_components,
                        )
                    )
                if component_state and component_metric is not None:
                    errors.extend(
                        _metric_contract_errors(
                            metric,
                            component_metric,
                            label=f"{component_key} metric {component_metric_key}",
                            current_time=current_time,
                            cutoff=cutoff,
                            not_before=cutoff,
                            require_current=require_current_components,
                            expected_key=component_metric_key,
                            batch_mode="component",
                        )
                    )
                metric_sources = {
                    metric.source.key,
                    *_payload_source_keys(metric.metadata or {}),
                }
                if metric.fallback_source_id:
                    metric_sources.add(metric.fallback_source.key)
                all_source_keys.update(metric_sources)
    derived_source_keys = (
        all_source_keys
        if snapshot.source.kind == "derived"
        or snapshot.quality_status == Observation.Quality.ESTIMATED
        or _payload_requires_derived_display(data)
        else set()
    )
    errors.extend(
        _source_licence_errors(
            all_source_keys,
            derived_source_keys,
            allowed_public_source_keys=allowed_public_source_keys,
            allowed_derived_source_keys=allowed_derived_source_keys,
        )
    )
    return tuple(dict.fromkeys(errors))


def _daily_component_reference(
    component: DashboardSnapshot,
) -> dict[str, Any]:
    data = _mapping(component.data)
    reference = {
        "page_key": component.key,
        "demo": False,
        "contract_version": DAILY_EVIDENCE_CONTRACT_VERSION,
        "snapshot_id": component.pk,
        "publication_batch_id": str(component.batch_id),
        "fingerprint": data.get("fingerprint"),
        "as_of": component.as_of.isoformat(),
        "quality_status": component.quality_status,
        "source_key": component.source.key,
        "component_batches": sorted(_snapshot_batches(component)),
        "source_keys": sorted(_snapshot_sources(component)),
        "fresh_until": data.get("fresh_until"),
        "metrics": deepcopy(data.get("metrics")),
        "component_data_sha256": component_data_fingerprint(data),
    }
    reference["component_payload_sha256"] = component_reference_fingerprint(
        reference
    )
    return reference


def _daily_evidence_item(
    component: DashboardSnapshot,
    metric: MetricSnapshot,
    component_metric: dict[str, Any],
) -> dict[str, Any]:
    return {
        "component": component.key,
        "component_metric_key": component_metric.get("key"),
        "metric_id": metric.pk,
        "key": metric.key,
        "value": str(metric.value),
        "display_value": metric.display_value,
        "unit": metric.unit,
        "value_date": metric.value_date.isoformat(),
        "as_of": metric.as_of.isoformat(),
        "fetched_at": metric.fetched_at.isoformat(),
        "batch_id": str(metric.batch_id),
        "quality_status": metric.quality_status,
        "source_key": metric.source.key,
        "license_scope": metric.license_scope,
        "fresh_until": component_metric.get("fresh_until"),
    }


def publish_daily_evidence_snapshot(
    *,
    now: datetime | None = None,
    batch_id: uuid.UUID | str | None = None,
) -> DailyEvidencePublicationOutcome:
    """Atomically freeze the newest complete official component set."""

    current_time = _aware(now or timezone.now())
    try:
        publication_batch = uuid.UUID(str(batch_id)) if batch_id else uuid.uuid4()
    except (TypeError, ValueError, AttributeError):
        return DailyEvidencePublicationOutcome(
            None,
            False,
            ("daily-evidence publication batch id is malformed",),
        )
    try:
        internal = Source.objects.get(key="internal")
    except Source.DoesNotExist:
        return DailyEvidencePublicationOutcome(
            None,
            False,
            ("internal publication source is missing",),
        )

    try:
        with transaction.atomic():
            _lock_daily_evidence_producer()
            # All daily-evidence consumers use parent -> components -> metrics.
            # Keep the producer in that same row-lock order to avoid inversion.
            latest_parent = (
                DashboardSnapshot.objects.select_for_update(of=("self",))
                .select_related("source")
                .filter(key=DAILY_EVIDENCE_KEY)
                .order_by("-created_at", "-id")
                .first()
            )
            selected_ids: dict[str, int] = {}
            missing: list[str] = []
            for page_key in DAILY_EVIDENCE_COMPONENT_KEYS:
                snapshot_id = (
                    DashboardSnapshot.objects.filter(
                        key=page_key,
                        is_published=True,
                    )
                    .order_by("-created_at", "-id")
                    .values_list("pk", flat=True)
                    .first()
                )
                if snapshot_id is None:
                    missing.append(f"{page_key}: latest published component is missing")
                else:
                    selected_ids[page_key] = snapshot_id
            if missing:
                raise _CandidateRejected(missing)

            locked_components = list(
                DashboardSnapshot.objects.select_for_update(of=("self",))
                .select_related("source")
                .filter(pk__in=selected_ids.values())
                .order_by("pk")
            )
            components = {item.key: item for item in locked_components}
            if set(components) != set(DAILY_EVIDENCE_COMPONENT_KEYS):
                raise _CandidateRejected(
                    ("one or more selected component snapshots disappeared",)
                )
            for page_key, selected_id in selected_ids.items():
                newest_id = (
                    DashboardSnapshot.objects.filter(
                        key=page_key,
                        is_published=True,
                    )
                    .order_by("-created_at", "-id")
                    .values_list("pk", flat=True)
                    .first()
                )
                if newest_id != selected_id:
                    raise _CandidateRejected(
                        (f"{page_key}: a newer component won the publication mutex",)
                    )

            component_metrics: dict[str, dict[str, Any]] = {}
            metric_ids: dict[str, int] = {}
            selection_errors: list[str] = []
            for page_key in DAILY_EVIDENCE_COMPONENT_KEYS:
                component = components[page_key]
                raw_metrics = _mapping(component.data).get("metrics")
                preferred_key = DAILY_EVIDENCE_PREFERRED_METRICS[page_key]
                matches = (
                    [
                        item
                        for item in raw_metrics
                        if isinstance(item, dict) and item.get("key") == preferred_key
                    ]
                    if isinstance(raw_metrics, list)
                    else []
                )
                if len(matches) != 1:
                    selection_errors.append(
                        f"{page_key}: required metric {preferred_key} is missing or duplicated"
                    )
                    continue
                component_metrics[page_key] = matches[0]
                normalized_key = f"{page_key}-{preferred_key.lower()}"
                metric_id = (
                    MetricSnapshot.objects.filter(
                        key=normalized_key,
                        batch_id=component.batch_id,
                    )
                    .values_list("pk", flat=True)
                    .first()
                )
                if metric_id is None:
                    selection_errors.append(
                        f"{page_key}: normalized metric {normalized_key} is missing"
                    )
                else:
                    metric_ids[page_key] = metric_id
            if selection_errors:
                raise _CandidateRejected(selection_errors)

            locked_metrics = list(
                MetricSnapshot.objects.select_for_update(of=("self",))
                .select_related("source", "fallback_source")
                .filter(pk__in=metric_ids.values())
                .order_by("pk")
            )
            metrics = {
                page_key: next(
                    (item for item in locked_metrics if item.pk == metric_ids[page_key]),
                    None,
                )
                for page_key in DAILY_EVIDENCE_COMPONENT_KEYS
            }
            if any(item is None for item in metrics.values()):
                raise _CandidateRejected(
                    ("one or more selected normalized metrics disappeared",)
                )

            references = [
                _daily_component_reference(components[page_key])
                for page_key in DAILY_EVIDENCE_COMPONENT_KEYS
            ]
            evidence_items = [
                _daily_evidence_item(
                    components[page_key],
                    metrics[page_key],
                    component_metrics[page_key],
                )
                for page_key in DAILY_EVIDENCE_COMPONENT_KEYS
            ]
            component_batches = sorted(
                {
                    str(item)
                    for reference in references
                    for item in reference["component_batches"]
                }
            )
            source_keys = sorted(
                {
                    str(item)
                    for reference in references
                    for item in reference["source_keys"]
                }
            )
            parent_data = {
                "demo": False,
                "contract_version": DAILY_EVIDENCE_CONTRACT_VERSION,
                "publication_batch_id": str(publication_batch),
                "research_date": timezone.localdate(current_time).isoformat(),
                "required_components": list(DAILY_EVIDENCE_COMPONENT_KEYS),
                "component_snapshots": references,
                "component_batches": component_batches,
                "source_keys": source_keys,
                "evidence_metric_ids": [
                    metrics[page_key].pk
                    for page_key in DAILY_EVIDENCE_COMPONENT_KEYS
                ],
                "evidence_items": evidence_items,
            }
            parent_data["component_set_sha256"] = (
                daily_evidence_component_set_fingerprint(parent_data)
            )
            parent_data["fingerprint"] = daily_evidence_payload_fingerprint(parent_data)

            allowed_public, allowed_derived = current_display_source_key_sets()
            if (
                latest_parent is not None
                and isinstance(latest_parent.data, dict)
                and latest_parent.data.get("component_set_sha256")
                == parent_data["component_set_sha256"]
                and not validate_daily_evidence_snapshot(
                    latest_parent,
                    now=current_time,
                    require_current_components=True,
                    require_latest_snapshot=True,
                    allowed_public_source_keys=allowed_public,
                    allowed_derived_source_keys=allowed_derived,
                )
            ):
                return DailyEvidencePublicationOutcome(latest_parent, False, ())

            quality_states = {
                *(
                    components[key].quality_status
                    for key in DAILY_EVIDENCE_COMPONENT_KEYS
                ),
                *(metrics[key].quality_status for key in DAILY_EVIDENCE_COMPONENT_KEYS),
            }
            quality = (
                Observation.Quality.ESTIMATED
                if Observation.Quality.ESTIMATED in quality_states
                else Observation.Quality.FRESH
            )
            candidate = DashboardSnapshot.objects.create(
                key=DAILY_EVIDENCE_KEY,
                title=f"{parent_data['research_date']} daily evidence",
                as_of=min(
                    components[key].as_of for key in DAILY_EVIDENCE_COMPONENT_KEYS
                ),
                batch_id=publication_batch,
                quality_status=quality,
                summary="三类官方组件已冻结，等待人工研究与审核。",
                data=parent_data,
                source=internal,
                is_published=True,
            )
            DashboardSnapshot.objects.filter(pk=candidate.pk).update(
                created_at=current_time,
                updated_at=current_time,
            )
            candidate.refresh_from_db()
            candidate_errors = validate_daily_evidence_snapshot(
                candidate,
                now=current_time,
                require_current_components=True,
                require_latest_snapshot=True,
                allowed_public_source_keys=allowed_public,
                allowed_derived_source_keys=allowed_derived,
            )
            if candidate_errors:
                raise _CandidateRejected(candidate_errors)
            return DailyEvidencePublicationOutcome(candidate, True, ())
    except _CandidateRejected as exc:
        return DailyEvidencePublicationOutcome(None, False, exc.errors)


def latest_ready_daily_evidence(
    *, now: datetime | None = None
) -> tuple[DashboardSnapshot | None, tuple[str, ...]]:
    """Return only the newest candidate; never fall back around a failed attempt."""

    snapshot = (
        DashboardSnapshot.objects.filter(key=DAILY_EVIDENCE_KEY)
        .select_related("source")
        .order_by("-created_at", "-id")
        .first()
    )
    errors = validate_daily_evidence_snapshot(
        snapshot,
        now=now,
        require_current_components=True,
        require_latest_snapshot=True,
    )
    return (snapshot if not errors else None), errors


def _evidence_lineage(item):
    if item.observation_id and item.snapshot_id:
        return None, "evidence item references both an observation and metric snapshot"
    if item.observation_id:
        return None, "daily-evidence v1 requires MetricSnapshot lineage, not Observation"
    if item.snapshot_id:
        return item.snapshot, ""
    return None, "evidence item has no normalized observation or metric snapshot"


def _lineage_fingerprint_payload(lineage: Observation | MetricSnapshot) -> dict[str, Any]:
    return {
        "model": lineage._meta.label_lower,
        "id": lineage.pk,
    }


def thesis_publication_fingerprint(thesis: Thesis) -> str:
    """Hash every field and lineage object that a public report can expose."""

    snapshot = thesis.source_snapshot
    component_payloads = (
        [
            reference
            for _page_key, reference in sorted(
                _component_reference_map(_mapping(snapshot.data)).items()
            )
        ]
        if snapshot is not None
        else []
    )
    evidence_payloads: list[dict[str, Any]] = []
    for item in thesis.evidence_items.all():
        lineage, lineage_error = _evidence_lineage(item)
        evidence_payloads.append(
            {
                "id": item.pk,
                "label": item.label,
                "body": item.body,
                "source_id": item.source_id,
                "source_key": item.source.key if item.source_id else None,
                "source_name": item.source.name if item.source_id else None,
                "source_license_scope": item.source.license_scope if item.source_id else None,
                "source_url": item.source_url,
                "value_date": item.value_date,
                "confidence": item.confidence,
                "lineage_error": lineage_error,
                "lineage": _lineage_fingerprint_payload(lineage) if lineage else None,
            }
        )
    triggers = [
        {
            "id": item.pk,
            "name": item.name,
            "condition": item.condition,
            "display_threshold": item.display_threshold,
            "status": item.status,
            "triggered_at": item.triggered_at,
        }
        for item in thesis.trigger_items.all()
    ]
    try:
        invalidation = thesis.invalidation_record
    except ObjectDoesNotExist:
        invalidation = None
    payload = {
        "thesis": {
            "id": thesis.pk,
            "date": thesis.date,
            "regime": thesis.regime,
            "confidence": thesis.confidence,
            "summary": thesis.summary,
            "legacy_evidence": thesis.evidence,
            "legacy_triggers": thesis.triggers,
            "legacy_invalidation": thesis.invalidation,
            "status": thesis.status,
            "hit_rate": thesis.hit_rate,
            "simulated_return": thesis.simulated_return,
            "review_status": thesis.review_status,
            "reviewed_by": thesis.reviewed_by,
            "reviewed_at": thesis.reviewed_at,
            "is_published": thesis.is_published,
            "published_at": thesis.published_at,
        },
        "snapshot": (
            {
                "id": snapshot.pk,
                "key": snapshot.key,
                "as_of": snapshot.as_of,
                "batch_id": snapshot.batch_id,
                "quality_status": snapshot.quality_status,
                "is_published": snapshot.is_published,
                "source": {
                    "id": snapshot.source_id,
                    "key": snapshot.source.key,
                    "name": snapshot.source.name,
                    "license_scope": snapshot.source.license_scope,
                },
                "data": snapshot.data,
            }
            if snapshot is not None
            else None
        ),
        "components": component_payloads,
        "evidence": sorted(evidence_payloads, key=lambda item: item["id"]),
        "triggers": sorted(triggers, key=lambda item: item["id"]),
        "invalidation": (
            {
                "id": invalidation.pk,
                "condition": invalidation.condition,
                "is_triggered": invalidation.is_triggered,
                "observed_at": invalidation.observed_at,
                "evidence": invalidation.evidence,
            }
            if invalidation is not None
            else None
        ),
    }
    return _canonical_hash(payload)


def validate_thesis_readiness(
    thesis: Thesis,
    *,
    now: datetime | None = None,
    snapshot_errors: tuple[str, ...] | None = None,
    require_live_snapshot: bool = False,
    require_current_snapshot: bool = False,
    allowed_public_source_keys: set[str] | None = None,
    allowed_derived_source_keys: set[str] | None = None,
) -> tuple[str, ...]:
    """Validate data, relations and time boundaries before human publication."""

    current_time = _aware(now or timezone.now())
    current_date = timezone.localdate(current_time)
    errors: list[str] = []
    if thesis.date > current_date:
        errors.append("thesis date is in the future")
    if not thesis.regime.strip() or not thesis.summary.strip():
        errors.append("thesis regime or summary is blank")
    if not thesis.source_snapshot_id:
        errors.append("thesis has no daily-evidence snapshot")
        evidence_references: dict[int, dict[str, Any]] = {}
        research_date = None
    else:
        snapshot = thesis.source_snapshot
        errors.extend(
            snapshot_errors
            if snapshot_errors is not None
            else validate_daily_evidence_snapshot(
                snapshot,
                now=current_time,
                require_live_components=require_live_snapshot,
                require_current_components=require_current_snapshot,
                require_latest_snapshot=require_current_snapshot,
                allowed_public_source_keys=allowed_public_source_keys,
                allowed_derived_source_keys=allowed_derived_source_keys,
            )
        )
        research_date = _parse_date(_mapping(snapshot.data).get("research_date"))
        if research_date is not None and thesis.date != research_date:
            errors.append("thesis date does not match daily-evidence research_date")
        raw_evidence_references = _mapping(snapshot.data).get("evidence_items")
        evidence_references = {}
        if isinstance(raw_evidence_references, list):
            for reference in raw_evidence_references:
                if not isinstance(reference, dict):
                    continue
                metric_id = _positive_database_id(reference.get("metric_id"))
                if metric_id is not None:
                    evidence_references[metric_id] = reference

    evidence_items = list(thesis.evidence_items.all())
    if len(evidence_items) < MINIMUM_EVIDENCE_ITEMS:
        errors.append("thesis has fewer than three relationized evidence items")
    lineage_keys: set[tuple[str, int]] = set()
    evidence_public_sources: set[str] = set()
    evidence_derived_sources: set[str] = set()
    for item in evidence_items:
        lineage, lineage_error = _evidence_lineage(item)
        if lineage_error:
            errors.append(f"evidence {item.pk}: {lineage_error}")
            continue
        if not item.label.strip() or not item.body.strip():
            errors.append(f"evidence {item.pk}: label or body is blank")
        lineage_keys.add(("metric", lineage.pk))
        reference = evidence_references.get(lineage.pk)
        if reference is None:
            errors.append(f"evidence {item.pk}: metric is not declared by daily-evidence")
            continue
        if item.source_id is None or not item.source_url or item.value_date is None:
            errors.append(f"evidence {item.pk}: source, URL or value date is missing")
            continue
        if not str(item.source_url).startswith(("https://", "http://")):
            errors.append(f"evidence {item.pk}: source URL is not HTTP(S)")
        if _aware(item.value_date) > current_time:
            errors.append(f"evidence {item.pk}: value date is in the future")
        reference_value_date = _parse_datetime(reference.get("value_date"))
        if reference_value_date != _aware(item.value_date):
            errors.append(f"evidence {item.pk}: value date does not match frozen lineage")
        if reference.get("source_key") != item.source.key:
            errors.append(f"evidence {item.pk}: source does not match frozen lineage")
        evidence_public_sources.add(item.source.key)
        if (
            reference.get("quality_status") == Observation.Quality.ESTIMATED
            or _payload_requires_derived_display(reference)
        ):
            evidence_derived_sources.add(item.source.key)
    if len(lineage_keys) < MINIMUM_EVIDENCE_ITEMS:
        errors.append("thesis evidence does not contain three distinct normalized lineages")
    errors.extend(
        _source_licence_errors(
            evidence_public_sources,
            evidence_derived_sources,
            allowed_public_source_keys=allowed_public_source_keys,
            allowed_derived_source_keys=allowed_derived_source_keys,
        )
    )

    triggers = list(thesis.trigger_items.all())
    if not triggers:
        errors.append("thesis has no relationized trigger")
    elif any(not item.name.strip() or not item.condition.strip() for item in triggers):
        errors.append("thesis has a blank trigger name or condition")
    for item in triggers:
        if item.status == Trigger.Status.TRIGGERED:
            if item.triggered_at is None:
                errors.append(f"trigger {item.pk}: triggered status lacks triggered_at")
            elif _aware(item.triggered_at) > current_time:
                errors.append(f"trigger {item.pk}: triggered_at is in the future")
        elif item.triggered_at is not None:
            errors.append(f"trigger {item.pk}: non-triggered status has triggered_at")
    try:
        invalidation = thesis.invalidation_record
    except ObjectDoesNotExist:
        invalidation = None
    if invalidation is None or not invalidation.condition.strip():
        errors.append("thesis has no relationized invalidation condition")
    elif invalidation.is_triggered:
        if invalidation.observed_at is None:
            errors.append("triggered invalidation lacks observed_at")
        elif _aware(invalidation.observed_at) > current_time:
            errors.append("invalidation observed_at is in the future")
    elif invalidation.observed_at is not None:
        errors.append("non-triggered invalidation has observed_at")
    return tuple(dict.fromkeys(errors))


def validate_public_thesis(
    thesis: Thesis,
    *,
    now: datetime | None = None,
    snapshot_errors: tuple[str, ...] | None = None,
    allowed_public_source_keys: set[str] | None = None,
    allowed_derived_source_keys: set[str] | None = None,
) -> tuple[str, ...]:
    current_time = _aware(now or timezone.now())
    errors = list(
        validate_thesis_readiness(
            thesis,
            now=current_time,
            snapshot_errors=snapshot_errors,
            allowed_public_source_keys=allowed_public_source_keys,
            allowed_derived_source_keys=allowed_derived_source_keys,
        )
    )
    if not thesis.is_published:
        errors.append("thesis is not published")
    if thesis.review_status != Thesis.ReviewStatus.REVIEWED:
        errors.append("thesis has not been reviewed")
    if not thesis.reviewed_by or thesis.reviewed_at is None:
        errors.append("thesis reviewer or review time is missing")
    elif _aware(thesis.reviewed_at) > current_time:
        errors.append("thesis review time is in the future")
    if thesis.published_at is None:
        errors.append("thesis publication time is missing")
    elif _aware(thesis.published_at) > current_time:
        errors.append("thesis publication time is in the future")
    expected_fingerprint = thesis_publication_fingerprint(thesis)
    if not _valid_fingerprint(thesis.publication_fingerprint):
        errors.append("thesis publication fingerprint is missing or malformed")
    elif thesis.publication_fingerprint != expected_fingerprint:
        errors.append("thesis content or lineage changed after review")
    return tuple(dict.fromkeys(errors))


def _publication_queryset(queryset: QuerySet[Thesis]) -> QuerySet[Thesis]:
    return queryset.select_related("source_snapshot", "source_snapshot__source").prefetch_related(
        "evidence_items__source",
        "evidence_items__observation__source",
        "evidence_items__observation__fallback_source",
        "evidence_items__snapshot__source",
        "evidence_items__snapshot__fallback_source",
        "trigger_items",
        "invalidation_record",
    )


def public_theses(
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[Thesis, ...]:
    """Materialize the exact reports validated for one public response.

    Returning the already-hydrated objects prevents a second query from
    reloading a relation graph that was not the graph just validated.
    """

    current_time = _aware(now or timezone.now())
    current_date = timezone.localdate(current_time)
    if limit is not None and limit <= 0:
        return ()
    base = Thesis.objects.filter(
        is_published=True,
        review_status=Thesis.ReviewStatus.REVIEWED,
        reviewed_at__isnull=False,
        published_at__isnull=False,
        published_at__lte=current_time,
        date__lte=current_date,
        source_snapshot__isnull=False,
    ).order_by("-date", "-pk")
    allowed_public, allowed_derived = current_display_source_key_sets()

    def candidate_batches() -> Iterable[list[Thesis]]:
        if limit is None:
            yield list(_publication_queryset(base))
            return
        chunk_size = min(100, max(20, limit * 4))
        offset = 0
        while True:
            chunk = list(
                _publication_queryset(base[offset : offset + chunk_size])
            )
            if not chunk:
                return
            yield chunk
            if len(chunk) < chunk_size:
                return
            offset += chunk_size

    safe: list[Thesis] = []
    snapshot_results: dict[int, tuple[str, ...]] = {}
    for candidates in candidate_batches():
        for thesis in candidates:
            snapshot_id = int(thesis.source_snapshot_id)
            if snapshot_id not in snapshot_results:
                snapshot_results[snapshot_id] = validate_daily_evidence_snapshot(
                    thesis.source_snapshot,
                    now=current_time,
                    allowed_public_source_keys=allowed_public,
                    allowed_derived_source_keys=allowed_derived,
                )
            snapshot_errors = snapshot_results[snapshot_id]
            if not validate_public_thesis(
                thesis,
                now=current_time,
                snapshot_errors=snapshot_errors,
                allowed_public_source_keys=allowed_public,
                allowed_derived_source_keys=allowed_derived,
            ):
                safe.append(thesis)
                if limit is not None and len(safe) >= limit:
                    return tuple(safe)
    return tuple(safe)


def publish_theses(
    theses: QuerySet[Thesis] | Iterable[Thesis],
    *,
    reviewer: str,
    now: datetime | None = None,
) -> PublicationOutcome:
    """Atomically review and publish all selected theses, or publish none."""

    reviewer_name = reviewer.strip()
    if not reviewer_name:
        return PublicationOutcome((), {0: ("reviewer is required",)})
    current_time = _aware(now or timezone.now())
    ids = [item.pk for item in theses] if not isinstance(theses, QuerySet) else list(
        theses.values_list("pk", flat=True)
    )
    with transaction.atomic():
        locked_base = list(
            Thesis.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        )
        locked_ids = [item.pk for item in locked_base]
        snapshot_ids = {
            item.source_snapshot_id for item in locked_base if item.source_snapshot_id
        }
        parent_snapshots = list(
            DashboardSnapshot.objects.select_for_update()
            .filter(pk__in=snapshot_ids)
            .order_by("pk")
        )
        component_snapshot_ids: set[int] = set()
        metric_snapshot_ids: set[int] = set()
        for snapshot in parent_snapshots:
            data = _mapping(snapshot.data)
            for reference in data.get("component_snapshots", []):
                if isinstance(reference, dict):
                    normalized = _positive_database_id(reference.get("snapshot_id"))
                    if normalized is not None:
                        component_snapshot_ids.add(normalized)
            for raw_id in data.get("evidence_metric_ids", []):
                normalized = _positive_database_id(raw_id)
                if normalized is not None:
                    metric_snapshot_ids.add(normalized)
        list(
            DashboardSnapshot.objects.select_for_update()
            .filter(pk__in=component_snapshot_ids)
            .order_by("pk")
        )
        list(
            MetricSnapshot.objects.select_for_update()
            .filter(pk__in=metric_snapshot_ids)
            .order_by("pk")
        )
        list(
            EvidenceItem.objects.select_for_update()
            .filter(thesis_id__in=locked_ids)
            .order_by("pk")
        )
        list(
            Trigger.objects.select_for_update()
            .filter(thesis_id__in=locked_ids)
            .order_by("pk")
        )
        list(
            Invalidation.objects.select_for_update()
            .filter(thesis_id__in=locked_ids)
            .order_by("pk")
        )
        locked = list(
            _publication_queryset(
                Thesis.objects.filter(pk__in=locked_ids).order_by("pk")
            )
        )
        allowed_public, allowed_derived = current_display_source_key_sets()
        errors = {}
        already_valid: dict[int, bool] = {}
        for item in locked:
            current_fingerprint = thesis_publication_fingerprint(item)
            already_valid[item.pk] = (
                item.is_published
                and item.publication_fingerprint == current_fingerprint
                and not validate_public_thesis(
                    item,
                    now=current_time,
                    allowed_public_source_keys=allowed_public,
                    allowed_derived_source_keys=allowed_derived,
                )
            )
            validation = validate_thesis_readiness(
                item,
                now=current_time,
                require_live_snapshot=not already_valid[item.pk],
                allowed_public_source_keys=allowed_public,
                allowed_derived_source_keys=allowed_derived,
            )
            if validation:
                errors[item.pk] = validation
        if len(locked) != len(set(ids)):
            errors[0] = ("one or more selected theses no longer exist",)
        if errors:
            return PublicationOutcome((), errors)
        published_ids: list[int] = []
        for item in locked:
            if not already_valid[item.pk]:
                item.review_status = Thesis.ReviewStatus.REVIEWED
                item.reviewed_by = reviewer_name
                item.reviewed_at = current_time
                item.is_published = True
                item.published_at = current_time
                item.publication_fingerprint = thesis_publication_fingerprint(item)
                item.save(
                    update_fields=[
                        "review_status",
                        "reviewed_by",
                        "reviewed_at",
                        "publication_fingerprint",
                        "is_published",
                        "published_at",
                        "updated_at",
                    ]
                )
            published_ids.append(item.pk)
        return PublicationOutcome(tuple(published_ids), {})


def unpublish_theses(theses: QuerySet[Thesis] | Iterable[Thesis]) -> tuple[int, ...]:
    """Withdraw public visibility while retaining review audit fields."""

    ids = [item.pk for item in theses] if not isinstance(theses, QuerySet) else list(
        theses.values_list("pk", flat=True)
    )
    with transaction.atomic():
        locked = list(
            Thesis.objects.select_for_update().filter(
                pk__in=ids,
                is_published=True,
            )
        )
        for item in locked:
            item.is_published = False
            item.published_at = None
            item.save(update_fields=["is_published", "published_at", "updated_at"])
    return tuple(sorted(item.pk for item in locked))
