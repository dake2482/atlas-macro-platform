"""Strict append-only publication contract for the official consumer page."""

from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .consumer_credit import (
    G19_SERIES,
    HHDC_BALANCE_SERIES,
    HHDC_DELINQUENCY_SERIES,
    FederalReserveG19Provider,
    NYFedHouseholdDebtProvider,
)
from .macro_official import CensusMARTSProvider
from .macro_releases import BEAPIOReleaseProvider, CensusMARTSReleaseProvider
from .models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    Source,
    SourceLicense,
)
from .providers import ProviderResult
from .raw_evidence import EVIDENCE_BUNDLE_CONTENT_TYPE, parse_evidence_bundle
from .services import SERIES_CATALOG, ensure_source, public_source_notices

CONSUMER_CONTRACT_VERSION = 2
CONSUMER_FORMULA_VERSION = "official-consumer-four-source-v2"
CONSUMER_TITLE = "消费与零售"
CONSUMER_SUMMARY = (
    "Census MARTS 发布工作簿控制当前零售指标与可见尾部，BEA PIO、"
    "Federal Reserve G.19 和 New York Fed HHDC 分别保留独立批次与新鲜度。"
    "Census API 仅在完整历史和重叠值逐项一致时扩展更早历史；消费者信心"
    "继续保持无数字采购边界。"
)
CONSUMER_RUNNING_TIMEOUT = timedelta(hours=2)

CONSUMER_ROLE_IDENTITIES = {
    "retail_release": ("census-release", "marts:retail-food-services"),
    "pio": ("bea-pio-release", "personal-income-outlays-release"),
    "g19": ("federal-reserve-g19", "consumer-credit"),
    "hhdc": ("ny-fed-household-credit", "household-debt-credit"),
}
CONSUMER_OPTIONAL_IDENTITY = ("census", "marts:44X72:SM:yes")
CONSUMER_MANDATORY_ROLES = tuple(CONSUMER_ROLE_IDENTITIES)

CONSUMER_REQUIRED_METRIC_KEYS = frozenset(
    {
        "census-mrts-44x72-sm-sa",
        "census-mrts-44x72-sm-sa-mom",
        "census-mrts-44x72-sm-sa-yoy",
        "bea-real-pce-mom",
        "bea-personal-saving-rate",
        "bea-real-dpi-mom",
        "g19-consumer-credit-outstanding-sa",
        "g19-consumer-credit-growth-saar",
        "g19-revolving-credit-growth-saar",
        "g19-nonrevolving-credit-growth-saar",
        "hhdc-total-debt-balance",
        "hhdc-credit-card-balance",
        "hhdc-all-90d-delinquent",
        "hhdc-credit-card-90d-delinquent",
    }
)
CONSUMER_REQUIRED_CHART_KEYS = frozenset(
    {
        "retail-sales",
        "real-consumption-income-momentum",
        "personal-saving-rate",
        "consumer-credit-composition",
        "household-debt-composition",
        "household-debt-delinquency",
    }
)
CONSUMER_REQUIRED_SECTION_KEYS = frozenset()

CONSUMER_ROLE_SERIES = {
    "retail_release": frozenset(
        {
            "census-mrts-44x72-sm-sa",
            "census-mrts-44x72-sm-sa-mom",
            "census-mrts-44x72-sm-sa-yoy",
        }
    ),
    "pio": frozenset(key.lower() for key in BEAPIOReleaseProvider.SERIES),
    "g19": frozenset(series_id.lower() for series_id, _unit in G19_SERIES.values()),
    "hhdc": frozenset(
        key.lower()
        for key in (*HHDC_BALANCE_SERIES.values(), *HHDC_DELINQUENCY_SERIES.values())
    ),
}
CONSUMER_RELEASE_SERIES = CONSUMER_ROLE_SERIES["retail_release"]
CONSUMER_OPTIONAL_SERIES = frozenset(
    {
        "census-api-mrts-44x72-sm-sa",
        "census-api-mrts-44x72-sm-sa-mom",
        "census-api-mrts-44x72-sm-sa-yoy",
    }
)
CONSUMER_API_TO_RELEASE_SERIES = {
    "census-api-mrts-44x72-sm-sa": "census-mrts-44x72-sm-sa",
    "census-api-mrts-44x72-sm-sa-mom": "census-mrts-44x72-sm-sa-mom",
    "census-api-mrts-44x72-sm-sa-yoy": "census-mrts-44x72-sm-sa-yoy",
}

CONSUMER_PAYLOAD_KEYS = frozenset(
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
        "input_runs",
        "optional_history_attempt",
        "publication_input_identity",
        "component_roles",
        "component_freshness",
        "retail_history_coverage",
        "fallback_state",
        "fallback_source",
        "semantic_boundary",
        "fingerprint",
        "payload_integrity_hash",
    }
)


class ConsumerPublicationPostconditionError(ValueError):
    """A completed mandatory input map could not become current."""


@dataclass(frozen=True)
class ConsumerRunEvidence:
    role: str
    run: IngestionRun
    artifact: RawArtifact
    fetched_at: datetime
    records: tuple[dict[str, Any], ...]
    replay_metadata: dict[str, Any]


@dataclass(frozen=True)
class ConsumerEvidence:
    retail_release: ConsumerRunEvidence
    pio: ConsumerRunEvidence
    g19: ConsumerRunEvidence
    hhdc: ConsumerRunEvidence
    retail_history_api: ConsumerRunEvidence | None
    retail_history_coverage: dict[str, Any]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _artifact_path(digest: str) -> Path:
    root = Path(
        getattr(
            settings,
            "RAW_ARTIFACT_ROOT",
            settings.BASE_DIR / "data" / "artifacts",
        )
    )
    return root / digest[:2] / f"{digest}.bin"


def _current_license(
    source: Source,
    *,
    lock: bool = False,
) -> SourceLicense | None:
    today = timezone.localdate()
    query = SourceLicense.objects.filter(
        source=source,
        is_current=True,
        status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
        public_display_allowed=True,
        derived_display_allowed=True,
        historical_storage_allowed=True,
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=today),
        Q(valid_until__isnull=True) | Q(valid_until__gte=today),
    )
    if lock:
        query = query.select_for_update()
    return query.order_by("-created_at", "-pk").first()


def _load_artifact(
    run: IngestionRun,
    *,
    lock: bool,
) -> tuple[RawArtifact, bytes, datetime, dict[str, Any]]:
    metadata = dict(run.metadata or {})
    fetched_at = _parse_datetime(metadata.get("fetched_at"))
    if (
        fetched_at is None
        or run.completed_at is None
        or fetched_at > run.completed_at + timedelta(minutes=5)
        or fetched_at > timezone.now() + timedelta(minutes=5)
    ):
        raise ValueError("consumer run fetch chronology is invalid")
    query = RawArtifact.objects.filter(run=run).order_by("pk")
    if lock:
        query = query.select_for_update()
    artifacts = list(query)
    if len(artifacts) != 1:
        raise ValueError("consumer run requires exactly one private artifact")
    artifact = artifacts[0]
    digest = str(metadata.get("sha256") or "").lower()
    try:
        declared_size = int(metadata.get("byte_length"))
    except (TypeError, ValueError) as exc:
        raise ValueError("consumer artifact size witness is invalid") from exc
    expected_uri = f"private://{run.source.key}/{digest[:2]}/{digest}.bin"
    if (
        len(digest) != 64
        or artifact.sha256 != digest
        or artifact.size_bytes != declared_size
        or artifact.uri != expected_uri
        or artifact.content_type != EVIDENCE_BUNDLE_CONTENT_TYPE
        or metadata.get("content_type") != EVIDENCE_BUNDLE_CONTENT_TYPE
        or metadata.get("raw_artifact_sha256") != digest
        or metadata.get("raw_artifact_uri") != expected_uri
    ):
        raise ValueError("consumer artifact database witness is invalid")
    try:
        payload = _artifact_path(digest).read_bytes()
    except OSError as exc:
        raise ValueError("consumer private artifact bytes are unavailable") from exc
    if len(payload) != declared_size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("consumer private artifact bytes are missing or tampered")
    return artifact, payload, fetched_at, metadata


_OBSERVATION_VALUE_QUANTUM = Decimal("0.00000001")


def _observation_database_value(value: Any) -> Decimal:
    """Return one finite value exactly representable by Observation.value."""

    try:
        decimal_value = Decimal(str(value))
        database_value = decimal_value.quantize(_OBSERVATION_VALUE_QUANTUM)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("consumer observation value exceeds database precision") from exc
    if not decimal_value.is_finite() or decimal_value != database_value:
        raise ValueError("consumer observation value exceeds database precision")
    return database_value


def _record_contract(record: dict[str, Any]) -> tuple[str, str, Decimal, str]:
    return (
        str(record.get("series_id") or "").lower(),
        str(record.get("date") or ""),
        _observation_database_value(record.get("value")),
        _canonical(record.get("metadata") or {}),
    )


def _prepare_replay_records(
    records: Iterable[dict[str, Any]],
    *,
    run: IngestionRun,
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    prepared = deepcopy(list(records))
    if run.source.key in {"census", "census-release"}:
        # Census API-derived records acquire exact run/batch lineage immediately
        # before persistence. Static replay must reproduce the same transformation.
        from .official_data import _bind_calculated_record_lineage

        result = ProviderResult(
            provider=run.source.key,
            dataset=run.dataset,
            records=prepared,
            fetched_at=fetched_at,
        )
        _bind_calculated_record_lineage(result, run.source, run)
        prepared = result.records
    return prepared


def _validate_observation_batch(
    records: Iterable[dict[str, Any]],
    *,
    run: IngestionRun,
    fetched_at: datetime,
    expected_series: frozenset[str],
    expected_frequency: str,
    lock: bool,
) -> tuple[Observation, ...]:
    expected_records = _prepare_replay_records(
        records,
        run=run,
        fetched_at=fetched_at,
    )
    base_query = Observation.objects.filter(batch_id=run.batch_id)
    if lock:
        list(base_query.select_for_update().order_by("pk").values_list("pk", flat=True))
    observations = tuple(
        base_query.select_related("series", "source", "fallback_source").order_by(
            "series__key", "value_date", "pk"
        )
    )
    actual_series = {item.series.key for item in observations if item.series_id}
    if actual_series != expected_series:
        raise ValueError("consumer observation batch has the wrong exact series set")
    for item in observations:
        catalogue = SERIES_CATALOG.get(item.series.key.upper()) if item.series_id else None
        if (
            catalogue is None
            or item.source_id != run.source_id
            or item.series.source_id != run.source_id
            or item.series.frequency != expected_frequency
            or catalogue[1] != item.series.unit
            or catalogue[2] != expected_frequency
            or item.instrument_id is not None
            or item.as_of != item.value_date
            or item.fetched_at != fetched_at
            or item.quality_status != Observation.Quality.FRESH
            or item.fallback_source_id is not None
        ):
            raise ValueError("consumer normalized observation lineage drifted")
    expected = sorted(_record_contract(record) for record in expected_records)
    actual = sorted(
        (
            item.series.key,
            item.value_date.date().isoformat(),
            _observation_database_value(item.value),
            _canonical(item.metadata or {}),
        )
        for item in observations
    )
    if (
        len(observations) != len(expected)
        or actual != expected
        or run.row_count != len(expected)
    ):
        raise ValueError("consumer normalized rows do not match exact evidence")
    return observations


def _provider_replay(
    role: str,
    payload: bytes,
) -> tuple[list[dict[str, Any]], dict[str, Any], set[str]]:
    identity = (
        CONSUMER_OPTIONAL_IDENTITY
        if role == "retail_history_api"
        else CONSUMER_ROLE_IDENTITIES[role]
    )
    evidence = parse_evidence_bundle(
        payload,
        expected_provider=identity[0],
        expected_dataset=identity[1],
    )
    roles = set(evidence.responses)
    if role == "retail_release":
        records, metadata = CensusMARTSReleaseProvider.replay_evidence_bundle(payload)
        probe_roles = {
            item for item in roles if item.startswith("recent-probe-")
        }
        fallback_roles = roles - probe_roles - {"current-workbook-failure"}
        if not (
            roles == {"current-workbook"}
            or (
                "current-workbook-failure" in roles
                and probe_roles
                and fallback_roles in (set(), {"archive-index", "archive-workbook"})
            )
        ):
            raise ValueError("consumer Census release evidence roles drifted")
    elif role == "retail_history_api":
        records, metadata = CensusMARTSProvider.replay_evidence_bundle(
            payload,
            expected_dataset=CONSUMER_OPTIONAL_IDENTITY[1],
        )
        if roles != {"marts-api-response"}:
            raise ValueError("consumer Census API evidence roles drifted")
    elif role == "pio":
        records, metadata = BEAPIOReleaseProvider.replay_evidence_bundle(payload)
        if roles != {"release-page", "summary-workbook", "section2-workbook"}:
            raise ValueError("consumer PIO evidence roles drifted")
    elif role == "g19":
        records, metadata = FederalReserveG19Provider.replay_evidence_bundle(payload)
        if roles != {"choose-page", "output-csv"}:
            raise ValueError("consumer G.19 evidence roles drifted")
    elif role == "hhdc":
        records, metadata = NYFedHouseholdDebtProvider.replay_evidence_bundle(payload)
        if roles != {"databank-page", "household-debt-workbook"}:
            raise ValueError("consumer HHDC evidence roles drifted")
    else:
        raise ValueError("consumer evidence role is unsupported")
    return records, metadata, roles


def _validate_run(
    role: str,
    run: IngestionRun,
    *,
    lock: bool = False,
) -> ConsumerRunEvidence:
    identity = (
        CONSUMER_OPTIONAL_IDENTITY
        if role == "retail_history_api"
        else CONSUMER_ROLE_IDENTITIES[role]
    )
    if (
        (run.source.key, run.dataset) != identity
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        or run.completed_at is None
        or run.completed_at < run.started_at
        or _current_license(run.source) is None
    ):
        raise ValueError(f"consumer requires a complete licensed {role} run")
    artifact, payload, fetched_at, metadata = _load_artifact(run, lock=lock)
    evidence = parse_evidence_bundle(
        payload,
        expected_provider=run.source.key,
        expected_dataset=run.dataset,
    )
    records, replay_metadata, roles = _provider_replay(role, payload)
    replay_metadata_mismatch = any(
        metadata.get(key) != value
        for key, value in replay_metadata.items()
        if not (
            role == "retail_history_api"
            and key == "retrieved_at"
            and key not in metadata
        )
    )
    if (
        metadata.get("provider") != run.source.key
        or metadata.get("evidence_bundle_schema") != evidence.manifest["schema_version"]
        or metadata.get("evidence_roles") != sorted(roles)
        or int(metadata.get("response_count") or 0) != len(roles)
        or int(metadata.get("unique_blob_count") or 0) != len(evidence.manifest["blobs"])
        or replay_metadata_mismatch
    ):
        raise ValueError(f"consumer {role} metadata does not replay from exact evidence")
    if role in {"retail_release", "retail_history_api", "g19", "hhdc"}:
        retrieved_at = _parse_datetime(replay_metadata.get("retrieved_at"))
        if retrieved_at is None or retrieved_at != fetched_at:
            raise ValueError(f"consumer {role} final retrieval does not match fetched_at")
    expected_series = (
        CONSUMER_OPTIONAL_SERIES
        if role == "retail_history_api"
        else CONSUMER_ROLE_SERIES[role]
    )
    frequency = "quarterly" if role == "hhdc" else "monthly"
    _validate_observation_batch(
        records,
        run=run,
        fetched_at=fetched_at,
        expected_series=expected_series,
        expected_frequency=frequency,
        lock=lock,
    )
    return ConsumerRunEvidence(
        role=role,
        run=run,
        artifact=artifact,
        fetched_at=fetched_at,
        records=tuple(records),
        replay_metadata=dict(replay_metadata),
    )


def _month_sequence(start: date, end: date) -> list[date]:
    cursor = start.replace(day=1)
    expected = []
    while cursor <= end.replace(day=1):
        expected.append(cursor)
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return expected


def _record_map(
    records: Iterable[dict[str, Any]],
    *,
    series_aliases: dict[str, str] | None = None,
) -> dict[tuple[str, date], Decimal]:
    aliases = series_aliases or {}
    return {
        (
            aliases.get(
                str(item["series_id"]).lower(),
                str(item["series_id"]).lower(),
            ),
            date.fromisoformat(str(item["date"])),
        ): Decimal(str(item["value"]))
        for item in records
    }


def _release_only_coverage(
    release: ConsumerRunEvidence,
    optional_attempt: IngestionRun | None,
    *,
    reason_code: str,
) -> dict[str, Any]:
    level_key = "census-mrts-44x72-sm-sa"
    periods = sorted(
        date.fromisoformat(str(item["date"]))
        for item in release.records
        if str(item.get("series_id") or "").lower() == level_key
    )
    return {
        "status": "release_only",
        "complete_history": False,
        "reason_code": "optional-api-unavailable-or-invalid",
        "coverage_start": periods[0].isoformat(),
        "coverage_end": periods[-1].isoformat(),
        "merge_policy": "release-workbook-only-v2",
        "source_roles": {
            "tail": {
                "role": "retail_release",
                "source_key": release.run.source.key,
                "ingestion_run_id": release.run.pk,
                "batch_id": str(release.run.batch_id),
            },
            "history": None,
        },
        "release_run_id": release.run.pk,
        "release_batch_id": str(release.run.batch_id),
        "api_run_id": None,
        "api_batch_id": None,
        "api_artifact_sha256": None,
        "overlap_dates": [],
        "overlap_verified": False,
    }


def _optional_history_coverage(
    release: ConsumerRunEvidence,
    optional_attempt: IngestionRun | None,
    *,
    lock: bool = False,
) -> tuple[ConsumerRunEvidence | None, dict[str, Any]]:
    if optional_attempt is None:
        return None, _release_only_coverage(
            release,
            None,
            reason_code="optional-api-missing",
        )
    if optional_attempt.status != IngestionRun.Status.SUCCESS or optional_attempt.row_count <= 0:
        return None, _release_only_coverage(
            release,
            optional_attempt,
            reason_code=f"optional-api-{optional_attempt.status}",
        )
    try:
        api = _validate_run("retail_history_api", optional_attempt, lock=lock)
        if (
            api.replay_metadata.get("require_complete_history") is not True
            or api.replay_metadata.get("history_start") != "1992-01-01"
        ):
            raise ValueError("Census API did not prove complete history")
        api_map = _record_map(
            api.records,
            series_aliases=CONSUMER_API_TO_RELEASE_SERIES,
        )
        release_map = _record_map(release.records)
        level_key = "census-mrts-44x72-sm-sa"
        level_dates = sorted(
            period for series, period in api_map if series == level_key
        )
        if (
            not level_dates
            or level_dates[0] != date(1992, 1, 1)
            or level_dates != _month_sequence(level_dates[0], level_dates[-1])
        ):
            raise ValueError("Census API level history is not continuous from 1992-01")
        release_level_dates = sorted(
            period for series, period in release_map if series == level_key
        )
        if (
            not release_level_dates
            or release_level_dates
            != _month_sequence(release_level_dates[0], release_level_dates[-1])
        ):
            raise ValueError("Census release level tail is not monthly continuous")
        release_complete_dates = {
            period
            for _series, period in release_map
            if all((series, period) in release_map for series in CONSUMER_RELEASE_SERIES)
        }
        api_complete_dates = {
            period
            for _series, period in api_map
            if all((series, period) in api_map for series in CONSUMER_RELEASE_SERIES)
        }
        overlap_dates = sorted(release_complete_dates & api_complete_dates)
        if not overlap_dates:
            raise ValueError("Census API and release have no complete three-series overlap")
        overlap_by_series = {
            series: sorted(
                {
                    period for candidate, period in api_map if candidate == series
                }
                & {
                    period for candidate, period in release_map if candidate == series
                }
            )
            for series in CONSUMER_RELEASE_SERIES
        }
        if any(not periods for periods in overlap_by_series.values()) or any(
            api_map[(series, period)] != release_map[(series, period)]
            for series, periods in overlap_by_series.items()
            for period in periods
        ):
            raise ValueError("Census API and release overlap differs")
        stitched_level_dates = [
            period for period in level_dates if period < release_level_dates[0]
        ] + release_level_dates
        if stitched_level_dates != _month_sequence(
            date(1992, 1, 1),
            release_level_dates[-1],
        ):
            raise ValueError("Census stitched level history contains a missing month")
        api_history_dates = [
            period for period in level_dates if period < release_level_dates[0]
        ]
        coverage = {
            "status": "complete_history",
            "complete_history": True,
            "reason_code": "api-history-overlap-verified",
            "coverage_start": level_dates[0].isoformat(),
            "coverage_end": release_level_dates[-1].isoformat(),
            "merge_policy": "api-history-plus-release-tail-overlap-verified-v2",
            "release_run_id": release.run.pk,
            "release_batch_id": str(release.run.batch_id),
            "api_run_id": api.run.pk,
            "api_batch_id": str(api.run.batch_id),
            "api_artifact_sha256": api.artifact.sha256,
            "api_history_end": (
                api_history_dates[-1].isoformat() if api_history_dates else None
            ),
            "release_coverage_start": release_level_dates[0].isoformat(),
            "release_coverage_end": release_level_dates[-1].isoformat(),
            "overlap_dates": [item.isoformat() for item in overlap_dates],
            "overlap_dates_by_series": {
                key: [item.isoformat() for item in value]
                for key, value in sorted(overlap_by_series.items())
            },
            "overlap_verified": True,
            "source_roles": {
                "history": {
                    "role": "retail_history_api",
                    "source_key": api.run.source.key,
                    "ingestion_run_id": api.run.pk,
                    "batch_id": str(api.run.batch_id),
                },
                "tail": {
                    "role": "retail_release",
                    "source_key": release.run.source.key,
                    "ingestion_run_id": release.run.pk,
                    "batch_id": str(release.run.batch_id),
                },
            },
        }
        return api, coverage
    except (ArithmeticError, AttributeError, KeyError, OSError, TypeError, ValueError):
        return None, _release_only_coverage(
            release,
            optional_attempt,
            reason_code="optional-api-invalid-or-overlap-mismatch",
        )


def _validate_inputs(
    runs: dict[str, IngestionRun],
    optional_attempt: IngestionRun | None,
    *,
    lock: bool = False,
) -> ConsumerEvidence:
    evidence = {
        role: _validate_run(role, runs[role], lock=lock)
        for role in CONSUMER_MANDATORY_ROLES
    }
    optional, coverage = _optional_history_coverage(
        evidence["retail_release"],
        optional_attempt,
        lock=lock,
    )
    return ConsumerEvidence(
        retail_release=evidence["retail_release"],
        pio=evidence["pio"],
        g19=evidence["g19"],
        hhdc=evidence["hhdc"],
        retail_history_api=optional,
        retail_history_coverage=coverage,
    )


def _attempt_reference(run: IngestionRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "ingestion_run_id": run.pk,
        "source_key": run.source.key,
        "dataset": run.dataset,
        "batch_id": str(run.batch_id),
        "status": run.status,
        "row_count": run.row_count,
        "error": run.error,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def _run_reference(evidence: ConsumerRunEvidence) -> dict[str, Any]:
    reference = _attempt_reference(evidence.run)
    if reference is None:
        raise ValueError("consumer run reference is missing")
    return {
        "role": evidence.role,
        **reference,
        "fetched_at": evidence.fetched_at.isoformat(),
        "artifact_id": evidence.artifact.pk,
        "artifact_uri": evidence.artifact.uri,
        "artifact_sha256": evidence.artifact.sha256,
        "artifact_size": evidence.artifact.size_bytes,
    }


def _input_identity(
    runs: dict[str, IngestionRun],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    optional = {
        "status": coverage["status"],
        "reason_code": coverage["reason_code"],
        "coverage_start": coverage["coverage_start"],
        "coverage_end": coverage["coverage_end"],
        "merge_policy": coverage["merge_policy"],
    }
    if coverage["status"] == "complete_history":
        optional.update(
            {
                "api_run_id": coverage["api_run_id"],
                "api_batch_id": coverage["api_batch_id"],
                "api_artifact_sha256": coverage["api_artifact_sha256"],
                "api_history_end": coverage["api_history_end"],
                "overlap_dates": coverage["overlap_dates"],
            }
        )
    return {
        "mandatory": {role: runs[role].pk for role in CONSUMER_MANDATORY_ROLES},
        "optional_effective": optional,
    }


def _observation_lineage(item: Observation) -> dict[str, Any]:
    return {
        "series_key": item.series.key,
        "source_key": item.source.key,
        "source_name": item.source.name,
        "value": str(item.value),
        "value_date": item.value_date.isoformat(),
        "as_of": item.as_of.isoformat(),
        "fetched_at": item.fetched_at.isoformat(),
        "batch_id": str(item.batch_id),
        "quality_status": item.quality_status,
        "license_scope": item.source.license_scope,
        "fallback_source": None,
    }


def _metric_with_lineage(
    metric: dict[str, Any],
    *,
    source: Source,
    batch_id: uuid.UUID,
) -> dict[str, Any]:
    enriched = deepcopy(metric)
    licence = _current_license(source)
    if licence is None:
        raise ValueError("consumer metric source licence changed during publication")
    observations = list(
        Observation.objects.filter(
            source=source,
            batch_id=batch_id,
            series__key=str(metric["key"]).lower(),
        )
        .select_related("series", "source")
        .order_by("-value_date", "-pk")[:2]
    )
    if not observations:
        raise ValueError("consumer metric lacks its exact source observation")
    metadata = deepcopy(enriched.get("metadata") or {})
    metadata.update(
        {
            "input_series": [observations[0].series.key],
            "input_batch_ids": [str(batch_id)],
            "input_value_dates": [item.value_date.isoformat() for item in observations],
            "input_lineage": [_observation_lineage(item) for item in observations],
        }
    )
    enriched["metadata"] = metadata
    enriched["license_scope"] = licence.scope
    enriched["fallback_source"] = None
    return enriched


def _retail_chart(evidence: ConsumerEvidence) -> dict[str, Any]:
    release = evidence.retail_release
    level_key = "census-mrts-44x72-sm-sa"
    release_rows = list(
        Observation.objects.filter(
            source=release.run.source,
            batch_id=release.run.batch_id,
            series__key=level_key,
        )
        .select_related("series", "source")
        .order_by("value_date", "pk")
    )
    if not release_rows:
        raise ValueError("consumer release-only retail chart has no observations")
    selected_rows = release_rows
    source_keys = [release.run.source.key]
    batch_ids = [str(release.run.batch_id)]
    if evidence.retail_history_api is not None:
        api = evidence.retail_history_api
        release_start = min(item.value_date.date() for item in release_rows)
        api_rows = list(
            Observation.objects.filter(
                source=api.run.source,
                batch_id=api.run.batch_id,
                series__key="census-api-mrts-44x72-sm-sa",
                value_date__date__lt=release_start,
            )
            .select_related("series", "source")
            .order_by("value_date", "pk")
        )
        selected_rows = [*api_rows, *release_rows]
        source_keys = [api.run.source.key, release.run.source.key]
        batch_ids = [str(api.run.batch_id), str(release.run.batch_id)]
    latest = release_rows[-1]
    deadline = _fresh_until(latest)
    return {
        "key": "retail-sales",
        "title": "零售与餐饮服务销售",
        "description": "季调月度水平，单位：百万美元；当前尾部始终来自 Census 发布工作簿。",
        "kind": "line",
        "time_axis": "date",
        "data": [
            {
                "date": item.value_date.date().isoformat(),
                "零售与餐饮服务": float(item.value),
                "_source_keys": [item.source.key],
                "_logical_series_key": level_key,
                "_lineage": {"零售与餐饮服务": _observation_lineage(item)},
            }
            for item in selected_rows
        ],
        "source_keys": source_keys,
        "as_of": latest.as_of.isoformat(),
        "fetched_at": max(item.fetched_at for item in selected_rows).isoformat(),
        "fresh_until": deadline.isoformat(),
        "quality_status": Observation.Quality.FRESH,
        "batch_ids": batch_ids,
        "frequency": "monthly",
        "merge_policy": evidence.retail_history_coverage["merge_policy"],
        "overlap_dates": evidence.retail_history_coverage["overlap_dates"],
        "overlap_verified": evidence.retail_history_coverage["overlap_verified"],
    }


def _fresh_until(observation: Observation) -> datetime:
    metadata = observation.metadata or {}
    release_date = metadata.get("source_release_time") or metadata.get(
        "source_revision_date"
    )
    freshness_days = metadata.get("release_freshness_days")
    if release_date and freshness_days:
        released_at = _parse_datetime(release_date)
        if released_at is not None:
            return released_at + timedelta(days=int(freshness_days))
    period = observation.value_date
    if observation.series.frequency == "quarterly":
        month = ((period.month - 1) // 3 + 1) * 3
        end = period.replace(month=month, day=calendar.monthrange(period.year, month)[1])
        return end + timedelta(days=121) - timedelta(microseconds=1)
    end = period.replace(day=calendar.monthrange(period.year, period.month)[1])
    return end + timedelta(days=46) - timedelta(microseconds=1)


def _build_consumer_payload(
    evidence: ConsumerEvidence,
    *,
    publication_batch_id: uuid.UUID,
    optional_attempt: IngestionRun | None,
) -> tuple[dict[str, Any], datetime, str]:
    from .official_data import _history_chart, _metric

    batches = {
        "retail_release": evidence.retail_release.run.batch_id,
        "pio": evidence.pio.run.batch_id,
        "g19": evidence.g19.run.batch_id,
        "hhdc": evidence.hhdc.run.batch_id,
    }
    sources = {
        role: getattr(evidence, role).run.source for role in CONSUMER_MANDATORY_ROLES
    }
    metric_specs = (
        ("CENSUS-MRTS-44X72-SM-SA", "零售与餐饮服务", "retail_release", 0, " USD mn", Decimal("1")),
        ("CENSUS-MRTS-44X72-SM-SA-MOM", "零售环比", "retail_release", 2, "%", Decimal("1")),
        ("CENSUS-MRTS-44X72-SM-SA-YOY", "零售同比", "retail_release", 2, "%", Decimal("1")),
        ("BEA-REAL-PCE-MOM", "实际 PCE 环比", "pio", 2, "%", Decimal("1")),
        ("BEA-PERSONAL-SAVING-RATE", "个人储蓄率", "pio", 2, "%", Decimal("1")),
        ("BEA-REAL-DPI-MOM", "实际可支配收入环比", "pio", 2, "%", Decimal("1")),
        ("G19-CONSUMER-CREDIT-OUTSTANDING-SA", "G.19 消费者信贷余额", "g19", 3, " USD tn", Decimal("0.000001")),
        ("G19-CONSUMER-CREDIT-GROWTH-SAAR", "G.19 信贷增速", "g19", 2, "%", Decimal("1")),
        ("G19-REVOLVING-CREDIT-GROWTH-SAAR", "循环信贷增速", "g19", 2, "%", Decimal("1")),
        ("G19-NONREVOLVING-CREDIT-GROWTH-SAAR", "非循环信贷增速", "g19", 2, "%", Decimal("1")),
        ("HHDC-TOTAL-DEBT-BALANCE", "家庭债务余额", "hhdc", 3, " USD tn", Decimal("1")),
        ("HHDC-CREDIT-CARD-BALANCE", "信用卡余额", "hhdc", 3, " USD tn", Decimal("1")),
        ("HHDC-ALL-90D-DELINQUENT", "全部债务 90+ 天逾期", "hhdc", 2, "%", Decimal("1")),
        ("HHDC-CREDIT-CARD-90D-DELINQUENT", "信用卡 90+ 天逾期", "hhdc", 2, "%", Decimal("1")),
    )
    metrics = []
    for series_key, label, role, decimals, suffix, scale in metric_specs:
        metric = _metric(
            series_key,
            label,
            decimals=decimals,
            suffix=suffix,
            scale=scale,
            source_key=sources[role].key,
            batch_id=batches[role],
            apply_freshness=False,
        )
        if metric is None:
            raise ValueError(f"consumer metric {series_key} is unavailable")
        metrics.append(
            _metric_with_lineage(
                metric,
                source=sources[role],
                batch_id=batches[role],
            )
        )

    chart_specs = (
        (
            "real-consumption-income-momentum",
            "实际消费与收入动能",
            {"BEA-REAL-PCE-MOM": "实际 PCE 环比", "BEA-REAL-DPI-MOM": "实际 DPI 环比"},
            "pio",
            120,
        ),
        (
            "personal-saving-rate",
            "个人储蓄率",
            {"BEA-PERSONAL-SAVING-RATE": "个人储蓄率"},
            "pio",
            120,
        ),
        (
            "consumer-credit-composition",
            "G.19 消费者信贷结构",
            {
                "G19-REVOLVING-CREDIT-OUTSTANDING-SA": "循环信贷",
                "G19-NONREVOLVING-CREDIT-OUTSTANDING-SA": "非循环信贷",
            },
            "g19",
            120,
        ),
        (
            "household-debt-composition",
            "家庭债务结构",
            {
                "HHDC-MORTGAGE-BALANCE": "抵押贷款",
                "HHDC-HELOC-BALANCE": "HELOC",
                "HHDC-AUTO-LOAN-BALANCE": "汽车贷款",
                "HHDC-CREDIT-CARD-BALANCE": "信用卡",
                "HHDC-STUDENT-LOAN-BALANCE": "学生贷款",
            },
            "hhdc",
            96,
        ),
        (
            "household-debt-delinquency",
            "90+ 天严重逾期率",
            {
                "HHDC-ALL-90D-DELINQUENT": "全部债务",
                "HHDC-CREDIT-CARD-90D-DELINQUENT": "信用卡",
                "HHDC-AUTO-90D-DELINQUENT": "汽车贷款",
                "HHDC-MORTGAGE-90D-DELINQUENT": "抵押贷款",
            },
            "hhdc",
            96,
        ),
    )
    charts = [_retail_chart(evidence)]
    for key, title, series, role, limit in chart_specs:
        chart = _history_chart(
            key=key,
            title=title,
            description="官方序列；单位与口径见来源血缘。",
            series=series,
            limit=limit,
            source_key=sources[role].key,
            batch_id=batches[role],
            apply_freshness=False,
        )
        if chart is None:
            raise ValueError(f"consumer chart {key} is unavailable")
        chart["time_axis"] = "date"
        charts.append(chart)
    if (
        {item["key"] for item in metrics} != CONSUMER_REQUIRED_METRIC_KEYS
        or {item["key"] for item in charts} != CONSUMER_REQUIRED_CHART_KEYS
    ):
        raise ValueError("consumer builder did not produce exact 14/6/0 containers")
    for chart in charts:
        chart_sources = list(chart.get("source_keys") or [])
        chart_licenses = [
            (key, _current_license(Source.objects.get(key=key)))
            for key in chart_sources
        ]
        if any(licence is None for _key, licence in chart_licenses):
            raise ValueError("consumer chart source licence changed during publication")
        chart["license_scopes"] = [
            f"{key}: {licence.scope}"
            for key, licence in chart_licenses
            if licence is not None
        ]
        chart["fallback_sources"] = []

    component_freshness = {}
    role_metric_keys = {
        "retail_release": "census-mrts-44x72-sm-sa",
        "pio": "bea-real-pce-mom",
        "g19": "g19-consumer-credit-outstanding-sa",
        "hhdc": "hhdc-total-debt-balance",
    }
    for role, metric_key in role_metric_keys.items():
        metric = next(item for item in metrics if item["key"] == metric_key)
        source_release_timestamp = None
        freshness_basis = "observation-month-end-plus-45-days"
        if role in {"pio", "g19"}:
            source_release_timestamp = (
                metric.get("metadata", {}).get("source_release_time")
                or metric.get("metadata", {}).get("source_revision_date")
            )
            if not source_release_timestamp:
                raise ValueError(f"consumer {role} lacks an official release timestamp")
            freshness_basis = "source-release-date-plus-45-days"
        elif role == "hhdc":
            freshness_basis = "latest-quarter-end-plus-120-days"
        component_freshness[role] = {
            "source_key": sources[role].key,
            "ingestion_run_id": getattr(evidence, role).run.pk,
            "batch_id": str(batches[role]),
            "as_of": metric["as_of"],
            "fetched_at": metric["fetched_at"],
            "fresh_until": metric["fresh_until"],
            "freshness_basis": freshness_basis,
            "source_release_timestamp": source_release_timestamp,
        }
    page_fresh_until = min(
        item["fresh_until"] for item in component_freshness.values()
    )
    input_runs = [_run_reference(getattr(evidence, role)) for role in CONSUMER_MANDATORY_ROLES]
    component_roles = {
        role: {
            "source_key": sources[role].key,
            "ingestion_run_id": getattr(evidence, role).run.pk,
            "batch_id": str(batches[role]),
            "series_keys": sorted(CONSUMER_ROLE_SERIES[role]),
        }
        for role in CONSUMER_MANDATORY_ROLES
    }
    component_roles["retail_history_api"] = {
        "source_key": CONSUMER_OPTIONAL_IDENTITY[0],
        "ingestion_run_id": (
            evidence.retail_history_api.run.pk if evidence.retail_history_api else None
        ),
        "batch_id": (
            str(evidence.retail_history_api.run.batch_id)
            if evidence.retail_history_api
            else None
        ),
        "status": evidence.retail_history_coverage["status"],
        "complete_history": evidence.retail_history_coverage["complete_history"],
        "series_keys": (
            sorted(CONSUMER_OPTIONAL_SERIES)
            if evidence.retail_history_api
            else []
        ),
    }
    source_keys = sorted(
        {
            key
            for item in [*metrics, *charts]
            for key in item.get("source_keys", [])
        }
    )
    component_batches = sorted(
        {
            str(batch)
            for batch in batches.values()
        }
        | (
            {str(evidence.retail_history_api.run.batch_id)}
            if evidence.retail_history_api
            else set()
        )
    )
    identity_runs = {role: getattr(evidence, role).run for role in CONSUMER_MANDATORY_ROLES}
    data = {
        "demo": False,
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": [],
        "component_batches": component_batches,
        "source_keys": source_keys,
        "required_notices": public_source_notices(source_keys),
        "fresh_until": page_fresh_until,
        "publication_batch_id": str(publication_batch_id),
        "contract_version": CONSUMER_CONTRACT_VERSION,
        "formula_version": CONSUMER_FORMULA_VERSION,
        "required_metric_keys": sorted(CONSUMER_REQUIRED_METRIC_KEYS),
        "required_chart_keys": sorted(CONSUMER_REQUIRED_CHART_KEYS),
        "required_section_keys": [],
        "input_runs": input_runs,
        "optional_history_attempt": (
            _run_reference(evidence.retail_history_api)
            if evidence.retail_history_api
            else None
        ),
        "publication_input_identity": _input_identity(
            identity_runs,
            evidence.retail_history_coverage,
        ),
        "component_roles": component_roles,
        "component_freshness": component_freshness,
        "retail_history_coverage": deepcopy(evidence.retail_history_coverage),
        "fallback_state": "none",
        "fallback_source": None,
        "semantic_boundary": (
            "当前零售值和尾部只取 Census release workbook；Census API 仅在"
            "完整连续且三系列重叠逐项一致时扩展更早历史。消费者信心无授权"
            "数字，G.19/HHDC 总量不推断收入群体压力。"
        ),
    }
    fingerprint_data = deepcopy(data)
    fingerprint_data.pop("publication_batch_id")
    data["fingerprint"] = hashlib.sha256(
        _canonical(
            {"title": CONSUMER_TITLE, "summary": CONSUMER_SUMMARY, "data": fingerprint_data}
        ).encode()
    ).hexdigest()
    data["payload_integrity_hash"] = hashlib.sha256(
        _canonical(
            {"title": CONSUMER_TITLE, "summary": CONSUMER_SUMMARY, "data": data}
        ).encode()
    ).hexdigest()
    as_of_values = [_parse_datetime(item.get("as_of")) for item in [*metrics, *charts]]
    valid_as_of = [item for item in as_of_values if item is not None]
    if not valid_as_of:
        raise ValueError("consumer payload lacks a valid as_of")
    quality = (
        Observation.Quality.FRESH
        if all(item.get("quality_status") == Observation.Quality.FRESH for item in [*metrics, *charts])
        else Observation.Quality.ESTIMATED
    )
    return data, min(valid_as_of), quality


def _metric_metadata(metric: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    metadata = deepcopy(metric.get("metadata") or {})
    return {
        **metadata,
        "dashboard_key": "consumer",
        "metric_key": metric["key"],
        "source_keys": list(metric.get("source_keys") or []),
        "component_batch_id": str(metric["batch_id"]),
        "input_batch_ids": list(metadata.get("input_batch_ids") or []),
        "input_lineage": deepcopy(metadata.get("input_lineage") or []),
        "contract_version": CONSUMER_CONTRACT_VERSION,
        "formula_version": CONSUMER_FORMULA_VERSION,
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
    if {row.key for row in rows} != {
        f"consumer-{key}" for key in CONSUMER_REQUIRED_METRIC_KEYS
    }:
        return False
    try:
        for row in rows:
            metric = metrics[row.key.removeprefix("consumer-")]
            value_date = _parse_datetime(metric.get("value_date"))
            as_of = _parse_datetime(metric.get("as_of"))
            fetched_at = _parse_datetime(metric.get("fetched_at"))
            if (
                value_date is None
                or as_of is None
                or fetched_at is None
                or row.label != metric["label"]
                or row.value != Decimal(str(metric["value"])).quantize(Decimal("0.00000001"))
                or row.display_value != metric["display_value"]
                or row.change
                != (
                    Decimal(str(metric["change"])).quantize(Decimal("0.000001"))
                    if metric.get("change") is not None
                    else None
                )
                or row.unit != metric.get("unit", "")
                or row.value_date != value_date
                or row.as_of != as_of
                or row.fetched_at != fetched_at
                or row.source.key != metric["source_key"]
                or row.fallback_source_id is not None
                or row.quality_status != metric["quality_status"]
                or row.license_scope != str(metric["license_scope"])[:120]
                or row.metadata != _metric_metadata(metric, data)
            ):
                return False
    except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
        return False
    return True


def _exact_keyed_items(value: Any, required: frozenset[str]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(required)
        and all(isinstance(item, dict) for item in value)
        and {item.get("key") for item in value} == required
    )


def _snapshot_static_replay(snapshot: DashboardSnapshot) -> SimpleNamespace | None:
    try:
        data = dict(snapshot.data or {})
        audited = dict(data)
        audited.pop("refresh_failure", None)
        if (
            snapshot.key != "consumer"
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or snapshot.title != CONSUMER_TITLE
            or snapshot.summary != CONSUMER_SUMMARY
            or set(audited) != CONSUMER_PAYLOAD_KEYS
            or audited.get("contract_version") != CONSUMER_CONTRACT_VERSION
            or audited.get("formula_version") != CONSUMER_FORMULA_VERSION
            or audited.get("demo") is not False
            or audited.get("fallback_state") != "none"
            or audited.get("fallback_source") is not None
            or audited.get("publication_batch_id") != str(snapshot.batch_id)
            or set(audited.get("required_metric_keys") or [])
            != CONSUMER_REQUIRED_METRIC_KEYS
            or set(audited.get("required_chart_keys") or [])
            != CONSUMER_REQUIRED_CHART_KEYS
            or set(audited.get("required_section_keys") or [])
            != CONSUMER_REQUIRED_SECTION_KEYS
            or not _exact_keyed_items(
                audited.get("metrics"), CONSUMER_REQUIRED_METRIC_KEYS
            )
            or not _exact_keyed_items(
                audited.get("charts"), CONSUMER_REQUIRED_CHART_KEYS
            )
            or not _exact_keyed_items(
                audited.get("sections"), CONSUMER_REQUIRED_SECTION_KEYS
            )
            or not isinstance(audited.get("component_roles"), dict)
            or set(audited["component_roles"])
            != {*CONSUMER_MANDATORY_ROLES, "retail_history_api"}
            or not isinstance(audited.get("component_freshness"), dict)
            or set(audited["component_freshness"]) != set(CONSUMER_MANDATORY_ROLES)
            or not isinstance(audited.get("retail_history_coverage"), dict)
        ):
            return None
        witnesses = audited.get("input_runs")
        if not isinstance(witnesses, list) or len(witnesses) != 4:
            return None
        by_role = {
            str(item.get("role") or ""): item
            for item in witnesses
            if isinstance(item, dict)
        }
        if set(by_role) != set(CONSUMER_MANDATORY_ROLES):
            return None
        runs = {
            role: IngestionRun.objects.filter(pk=reference.get("ingestion_run_id"))
            .select_related("source")
            .first()
            for role, reference in by_role.items()
        }
        if any(run is None for run in runs.values()):
            return None
        optional_reference = audited.get("optional_history_attempt")
        optional_attempt = None
        if optional_reference is not None:
            if not isinstance(optional_reference, dict):
                return None
            optional_attempt = (
                IngestionRun.objects.filter(
                    pk=optional_reference.get("ingestion_run_id")
                )
                .select_related("source")
                .first()
            )
            if optional_attempt is None:
                return None
        evidence = _validate_inputs(runs, optional_attempt)
        if any(_run_reference(getattr(evidence, role)) != by_role[role] for role in by_role):
            return None
        if optional_reference is not None and (
            evidence.retail_history_api is None
            or _run_reference(evidence.retail_history_api) != optional_reference
        ):
            return None
        expected, expected_as_of, expected_quality = _build_consumer_payload(
            evidence,
            publication_batch_id=snapshot.batch_id,
            optional_attempt=optional_attempt,
        )
        if (
            audited != expected
            or snapshot.as_of != expected_as_of
            or not _metric_rows_match(snapshot, expected)
        ):
            return None
        return SimpleNamespace(
            data=data,
            evidence=evidence,
            expected_data=expected,
            expected_quality=expected_quality,
        )
    except (ArithmeticError, AttributeError, KeyError, OSError, TypeError, ValueError):
        return None


def replay_consumer_snapshot(
    snapshot: DashboardSnapshot,
) -> SimpleNamespace | None:
    """Replay one immutable consumer revision without live-state selection."""

    return _snapshot_static_replay(snapshot)


def _latest_attempts(*, lock: bool = False) -> dict[str, IngestionRun] | None:
    latest = {}
    for role, (source_key, dataset) in CONSUMER_ROLE_IDENTITIES.items():
        query = IngestionRun.objects.filter(source__key=source_key, dataset=dataset)
        if lock:
            query = query.select_for_update(of=("self",))
        run = query.select_related("source").order_by("-started_at", "-id").first()
        if run is None:
            return None
        latest[role] = run
    return latest


def _latest_optional_attempt(*, lock: bool = False) -> IngestionRun | None:
    query = IngestionRun.objects.filter(
        source__key=CONSUMER_OPTIONAL_IDENTITY[0],
        dataset=CONSUMER_OPTIONAL_IDENTITY[1],
    )
    if lock:
        query = query.select_for_update(of=("self",))
    return query.select_related("source").order_by("-started_at", "-id").first()


def _validated_failure_marker(
    marker: Any,
    *,
    replay: ConsumerEvidence,
) -> dict[str, IngestionRun]:
    reason_code = marker.get("reason_code") if isinstance(marker, dict) else None
    if reason_code not in {"latest-attempt-incomplete", "publication-postcondition"}:
        raise ValueError("consumer retained-failure marker is missing")
    attempts = marker.get("attempts")
    checked_at = _parse_datetime(marker.get("checked_at"))
    if not isinstance(attempts, dict) or set(attempts) != set(CONSUMER_MANDATORY_ROLES):
        raise ValueError("consumer retained-failure marker is invalid")
    replay_runs = {role: getattr(replay, role).run for role in CONSUMER_MANDATORY_ROLES}
    marker_runs = {}
    for role, reference in attempts.items():
        run = (
            IngestionRun.objects.filter(pk=reference.get("ingestion_run_id"))
            .select_related("source")
            .first()
            if isinstance(reference, dict)
            else None
        )
        if (
            run is None
            or _attempt_reference(run) != reference
            or (run.source.key, run.dataset) != CONSUMER_ROLE_IDENTITIES[role]
        ):
            raise ValueError("consumer retained-failure run witness is invalid")
        marker_runs[role] = run
    statuses = {run.status for run in marker_runs.values()}
    if (
        checked_at is None
        or checked_at > timezone.now() + timedelta(minutes=5)
        or (
            reason_code == "latest-attempt-incomplete"
            and (
                not (
                    statuses
                    & {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}
                    or any(
                        run.status == IngestionRun.Status.RUNNING
                        and run.completed_at is None
                        and run.started_at
                        <= checked_at - CONSUMER_RUNNING_TIMEOUT
                        for run in marker_runs.values()
                    )
                )
                or not statuses
                <= {
                    IngestionRun.Status.SUCCESS,
                    IngestionRun.Status.FAILED,
                    IngestionRun.Status.PARTIAL,
                    IngestionRun.Status.RUNNING,
                }
            )
        )
        or (reason_code == "publication-postcondition" and statuses != {IngestionRun.Status.SUCCESS})
        or any(
            run.started_at < replay_runs[role].started_at
            for role, run in marker_runs.items()
        )
        or any(
            run.completed_at is not None and checked_at < run.completed_at
            for run in marker_runs.values()
        )
    ):
        raise ValueError("consumer retained-failure marker is invalid")
    return marker_runs


def _public_state(snapshot: DashboardSnapshot, replay: SimpleNamespace) -> str:
    latest = _latest_attempts()
    if latest is None:
        raise ValueError("consumer latest attempts are missing")
    replay_runs = {
        role: getattr(replay.evidence, role).run for role in CONSUMER_MANDATORY_ROLES
    }
    marker = (snapshot.data or {}).get("refresh_failure")
    if all(latest[role].pk == replay_runs[role].pk for role in CONSUMER_MANDATORY_ROLES):
        if marker is not None:
            raise ValueError("consumer current snapshot carries a failure marker")
        deadline = _parse_datetime(replay.expected_data.get("fresh_until"))
        if deadline is None:
            raise ValueError("consumer freshness deadline is invalid")
        return "natural_expiry" if timezone.now() > deadline else "current_candidate"
    if any(
        latest[role].started_at < replay_runs[role].started_at
        for role in CONSUMER_MANDATORY_ROLES
    ):
        raise ValueError("consumer attempt chronology regressed")
    statuses = {run.status for run in latest.values()}
    if statuses & {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}:
        if marker is not None:
            marker_runs = _validated_failure_marker(marker, replay=replay.evidence)
            if (
                marker.get("reason_code") == "latest-attempt-incomplete"
                and all(marker_runs[role].pk == latest[role].pk for role in latest)
            ):
                return "retained_failure"
        newest = max(run.completed_at or run.started_at for run in latest.values())
        if newest < timezone.now() - CONSUMER_RUNNING_TIMEOUT:
            raise ValueError("consumer uncoordinated failure transition timed out")
        return "transition_pending"
    if IngestionRun.Status.RUNNING in statuses:
        if marker is not None:
            marker_runs = _validated_failure_marker(marker, replay=replay.evidence)
            if (
                marker.get("reason_code") == "latest-attempt-incomplete"
                and all(marker_runs[role].pk == latest[role].pk for role in latest)
            ):
                return "retained_failure"
        if any(
            run.status == IngestionRun.Status.RUNNING
            and run.started_at < timezone.now() - CONSUMER_RUNNING_TIMEOUT
            for run in latest.values()
        ):
            raise ValueError("consumer running transition timed out")
        return "transition_pending"
    if statuses == {IngestionRun.Status.SUCCESS}:
        if marker is not None:
            marker_runs = _validated_failure_marker(marker, replay=replay.evidence)
            if (
                marker.get("reason_code") == "publication-postcondition"
                and all(marker_runs[role].pk == latest[role].pk for role in latest)
            ):
                return "retained_failure"
        newest = max(run.completed_at or run.started_at for run in latest.values())
        if newest < timezone.now() - CONSUMER_RUNNING_TIMEOUT:
            raise ValueError("consumer unpublished success transition timed out")
        return "transition_pending"
    raise ValueError("consumer latest attempts have unsupported states")


def consumer_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    replay = _snapshot_static_replay(snapshot)
    if replay is None:
        return False
    try:
        state = _public_state(snapshot, replay)
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
    snapshot.consumer_publication_state = state
    return True


def select_public_consumer_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    queryset = candidates
    if queryset is None:
        queryset = (
            DashboardSnapshot.objects.filter(
                key="consumer",
                is_published=True,
                source__key="internal",
                data__contract_version=CONSUMER_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")
        )
    for candidate in queryset:
        if consumer_snapshot_is_publicly_displayable(candidate):
            presented = deepcopy(candidate)
            presented.data = deepcopy(candidate.data or {})
            presented.consumer_publication_state = getattr(
                candidate,
                "consumer_publication_state",
                None,
            )
            if presented.consumer_publication_state != "retained_failure":
                presented.data.pop("refresh_failure", None)
            return presented
    return None


def _store_metric_rows(snapshot: DashboardSnapshot) -> None:
    for metric in snapshot.data["metrics"]:
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if value_date is None or as_of is None or fetched_at is None:
            raise ValueError("consumer metric timestamp is invalid")
        MetricSnapshot.objects.create(
            key=f"consumer-{metric['key']}",
            label=metric["label"],
            value=Decimal(str(metric["value"])),
            display_value=metric["display_value"],
            change=(
                Decimal(str(metric["change"]))
                if metric.get("change") is not None
                else None
            ),
            unit=metric.get("unit", ""),
            value_date=value_date,
            as_of=as_of,
            fetched_at=fetched_at,
            batch_id=snapshot.batch_id,
            source=Source.objects.get(key=metric["source_key"]),
            fallback_source=None,
            quality_status=metric["quality_status"],
            license_scope=str(metric["license_scope"])[:120],
            metadata=_metric_metadata(metric, snapshot.data),
        )


def _published_identity(snapshot: DashboardSnapshot) -> dict[str, Any] | None:
    identity = (snapshot.data or {}).get("publication_input_identity")
    return identity if isinstance(identity, dict) else None


def _lock_publisher_inputs(
    *,
    internal: Source,
    requested_runs: dict[str, IngestionRun],
    requested_optional: IngestionRun | None,
) -> tuple[
    dict[str, IngestionRun],
    IngestionRun | None,
    dict[str, IngestionRun],
    IngestionRun | None,
]:
    requested_ids = {run.pk for run in requested_runs.values()}
    if requested_optional is not None:
        requested_ids.add(requested_optional.pk)

    source_keys = {
        "internal",
        CONSUMER_OPTIONAL_IDENTITY[0],
        *(source_key for source_key, _dataset in CONSUMER_ROLE_IDENTITIES.values()),
    }
    sources = list(Source.objects.filter(key__in=source_keys).order_by("pk"))
    required_source_keys = {
        "internal",
        *(source_key for source_key, _dataset in CONSUMER_ROLE_IDENTITIES.values()),
    }
    if requested_optional is not None:
        required_source_keys.add(CONSUMER_OPTIONAL_IDENTITY[0])
    if (
        not required_source_keys <= {source.key for source in sources}
        or internal not in sources
    ):
        raise ValueError("consumer publisher source catalogue is incomplete")
    list(
        Source.objects.select_for_update(of=("self",))
        .filter(pk__in=[source.pk for source in sources])
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    today = timezone.localdate()
    list(
        SourceLicense.objects.select_for_update(of=("self",))
        .filter(
            source_id__in=[source.pk for source in sources],
            is_current=True,
            status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
            public_display_allowed=True,
            derived_display_allowed=True,
            historical_storage_allowed=True,
        )
        .filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=today),
            Q(valid_until__isnull=True) | Q(valid_until__gte=today),
        )
        .order_by("source_id", "pk")
        .values_list("pk", flat=True)
    )

    latest = _latest_attempts()
    latest_optional = _latest_optional_attempt()
    latest_ids = {run.pk for run in latest.values()} if latest is not None else set()
    if latest_optional is not None:
        latest_ids.add(latest_optional.pk)
    locked_by_id = {
        run.pk: run
        for run in IngestionRun.objects.select_for_update(of=("self",))
        .filter(pk__in=requested_ids | latest_ids)
        .select_related("source")
        .order_by("pk")
    }
    if not requested_ids <= set(locked_by_id):
        raise ValueError("consumer publisher input run is missing")
    latest_after_lock = _latest_attempts()
    optional_after_lock = _latest_optional_attempt()
    if latest is None or latest_after_lock is None or any(
        latest_after_lock[role].pk != latest[role].pk for role in latest
    ):
        raise ValueError("consumer latest mandatory attempts changed during locking")
    if (latest_optional.pk if latest_optional else None) != (
        optional_after_lock.pk if optional_after_lock else None
    ):
        raise ValueError("consumer latest optional attempt changed during locking")
    return (
        {role: locked_by_id[run.pk] for role, run in requested_runs.items()},
        locked_by_id[requested_optional.pk] if requested_optional is not None else None,
        {role: locked_by_id[run.pk] for role, run in latest.items()},
        (
            locked_by_id[latest_optional.pk]
            if latest_optional is not None
            else None
        ),
    )


def publish_consumer_revision(
    *,
    retail_release_run: IngestionRun,
    pio_run: IngestionRun,
    g19_run: IngestionRun,
    hhdc_run: IngestionRun,
    retail_history_api_run: IngestionRun | None = None,
    publication_batch_id: uuid.UUID | None = None,
) -> DashboardSnapshot | None:
    runs = {
        "retail_release": retail_release_run,
        "pio": pio_run,
        "g19": g19_run,
        "hhdc": hhdc_run,
    }
    internal = ensure_source("internal")
    with transaction.atomic():
        (
            locked_runs,
            locked_optional,
            latest,
            latest_optional,
        ) = _lock_publisher_inputs(
            internal=internal,
            requested_runs=runs,
            requested_optional=retail_history_api_run,
        )
        if any(latest[role].pk != locked_runs[role].pk for role in latest):
            raise ValueError("consumer publisher requires exact latest mandatory attempts")
        if (latest_optional.pk if latest_optional else None) != (
            locked_optional.pk if locked_optional else None
        ):
            raise ValueError("consumer publisher requires the latest optional Census attempt")
        evidence = _validate_inputs(locked_runs, locked_optional, lock=True)
        target_identity = _input_identity(
            locked_runs,
            evidence.retail_history_coverage,
        )
        existing = [
            candidate
            for candidate in DashboardSnapshot.objects.select_for_update(of=("self",))
            .filter(
                key="consumer",
                is_published=True,
                data__contract_version=CONSUMER_CONTRACT_VERSION,
            )
            .select_related("source")
            .order_by("-created_at", "-id")
            if _published_identity(candidate) == target_identity
        ]
        if len(existing) > 1:
            raise ValueError("consumer input map has multiple publication revisions")
        if existing:
            if _snapshot_static_replay(existing[0]) is None:
                raise ValueError("consumer existing revision is not replayable")
            return None
        batch_id = publication_batch_id or uuid.uuid4()
        data, as_of, quality = _build_consumer_payload(
            evidence,
            publication_batch_id=batch_id,
            optional_attempt=locked_optional,
        )
        snapshot = DashboardSnapshot.objects.create(
            key="consumer",
            title=CONSUMER_TITLE,
            summary=CONSUMER_SUMMARY,
            as_of=as_of,
            batch_id=batch_id,
            quality_status=quality,
            data=data,
            source=internal,
            is_published=True,
        )
        _store_metric_rows(snapshot)
        if _snapshot_static_replay(snapshot) is None:
            raise ConsumerPublicationPostconditionError(
                "consumer publication postcondition failed"
            )
        return snapshot


def _mark_retained_failure(
    snapshot: DashboardSnapshot,
    latest: dict[str, IngestionRun],
    *,
    reason_code: str = "latest-attempt-incomplete",
    reason: str | None = None,
) -> None:
    checked_at = max((run.completed_at or timezone.now()) for run in latest.values())
    data = deepcopy(snapshot.data or {})
    data["refresh_failure"] = {
        "reason_code": reason_code,
        "checked_at": checked_at.isoformat(),
        "reason": reason
        or "最新四个必需 Consumer 输入未形成完整可重放批次；保留上一版。",
        "attempts": {role: _attempt_reference(run) for role, run in latest.items()},
    }
    snapshot.data = data
    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])


def _retain_publication_postcondition(
    latest: dict[str, IngestionRun],
    error: Exception,
    *,
    target_identity: dict[str, Any],
    optional_attempt: IngestionRun | None,
) -> str:
    previous = None
    internal = ensure_source("internal")
    with transaction.atomic():
        (
            _locked_requested,
            _locked_optional,
            locked_latest,
            _latest_optional,
        ) = _lock_publisher_inputs(
            internal=internal,
            requested_runs=latest,
            requested_optional=optional_attempt,
        )
        if any(
            locked_latest[role].pk != latest[role].pk for role in latest
        ):
            raise ValueError("consumer attempts changed after publication rollback")
        candidates = (
            DashboardSnapshot.objects.select_for_update(of=("self",))
            .filter(
                key="consumer",
                is_published=True,
                data__contract_version=CONSUMER_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")
        )
        for candidate in candidates:
            if _published_identity(candidate) == target_identity:
                continue
            try:
                replay = _snapshot_static_replay(candidate)
            except Exception:
                continue
            if replay is not None:
                previous = candidate
                break
        if previous is not None:
            previous_identity = _published_identity(previous) or {}
            if previous_identity.get("mandatory") == target_identity.get("mandatory"):
                # A failure while changing only the optional Census history must
                # never masquerade as a mandatory four-source refresh failure.
                # The last replayable revision remains untouched.
                return "optional_only_retained"
            _mark_retained_failure(
                previous,
                locked_latest,
                reason_code="publication-postcondition",
                reason=(
                    "最新完整 Consumer 成功批次未通过 current_candidate 发布后置"
                    f"校验；本次 revision 已回滚并保留上一版：{error}"
                ),
            )
            selected = select_public_consumer_snapshot()
            if (
                selected is None
                or getattr(selected, "consumer_publication_state", None)
                != "retained_failure"
            ):
                raise ValueError("consumer publication-postcondition marker did not replay")
    if previous is None:
        return "unretained"
    return "mandatory_failure_retained"


def coordinate_consumer_dashboard(
    runs: Iterable[IngestionRun] | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    _ = runs
    latest = _latest_attempts()
    if latest is None:
        return [], {"consumer"}
    selected = select_public_consumer_snapshot()
    mandatory_identity = {
        role: latest[role].pk for role in CONSUMER_MANDATORY_ROLES
    }
    if (
        selected is not None
        and getattr(selected, "consumer_publication_state", None)
        == "natural_expiry"
        and (_published_identity(selected) or {}).get("mandatory")
        == mandatory_identity
    ):
        return [], {"consumer"}
    if all(
        run.status == IngestionRun.Status.SUCCESS and run.row_count > 0
        for run in latest.values()
    ):
        optional_attempt = _latest_optional_attempt()
        selected_identity = (
            _published_identity(selected) if selected is not None else None
        ) or {}
        if (
            selected is not None
            and selected_identity.get("mandatory") == mandatory_identity
            and optional_attempt is not None
            and optional_attempt.status != IngestionRun.Status.SUCCESS
        ):
            # Optional acquisition churn cannot downgrade a known-good full
            # history or stale an otherwise current four-source publication.
            return [], set()
        target_identity = None
        try:
            prevalidated = _validate_inputs(latest, optional_attempt)
            target_identity = _input_identity(
                latest,
                prevalidated.retail_history_coverage,
            )
            if (
                selected is not None
                and selected_identity.get("mandatory") == mandatory_identity
                and (selected_identity.get("optional_effective") or {}).get("status")
                == "complete_history"
                and (target_identity.get("optional_effective") or {}).get("status")
                == "release_only"
            ):
                # Complete optional history is monotonic for a fixed mandatory
                # revision. Missing, failed or semantically invalid later API
                # attempts cannot publish a downgrade.
                return [], set()
            with transaction.atomic():
                published = publish_consumer_revision(
                    retail_release_run=latest["retail_release"],
                    pio_run=latest["pio"],
                    g19_run=latest["g19"],
                    hhdc_run=latest["hhdc"],
                    retail_history_api_run=optional_attempt,
                )
                selected = select_public_consumer_snapshot()
                state = getattr(selected, "consumer_publication_state", None)
                if (
                    published is None
                    and state == "natural_expiry"
                    and selected is not None
                    and (_published_identity(selected) or {}).get("mandatory")
                    == mandatory_identity
                ):
                    return [], {"consumer"}
                if (
                    selected is None
                    or state != "current_candidate"
                    or _published_identity(selected) != target_identity
                ):
                    raise ConsumerPublicationPostconditionError(
                        "consumer publication is not the current replayable revision"
                    )
            return ([published] if published is not None else []), set()
        except Exception as exc:
            failed_identity = target_identity or {
                "mandatory": mandatory_identity,
                "optional_effective": {"status": "publication-validation-error"},
            }
            retention = _retain_publication_postcondition(
                latest,
                exc,
                target_identity=failed_identity,
                optional_attempt=optional_attempt,
            )
            if retention == "optional_only_retained":
                return [], set()
            if retention == "mandatory_failure_retained":
                return [], {"consumer"}
            raise
    running = [
        run for run in latest.values() if run.status == IngestionRun.Status.RUNNING
    ]
    timed_out_running = [
        run
        for run in running
        if run.started_at <= timezone.now() - CONSUMER_RUNNING_TIMEOUT
    ]
    has_terminal_failure = any(
        run.status in {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}
        for run in latest.values()
    )
    if running and not timed_out_running and not has_terminal_failure:
        return [], {"consumer"}
    if not timed_out_running and not has_terminal_failure:
        raise ValueError("consumer latest attempts have unsupported terminal states")
    previous = None
    internal = ensure_source("internal")
    optional_attempt = _latest_optional_attempt()
    with transaction.atomic():
        (
            _locked_requested,
            _locked_optional,
            locked_latest,
            _latest_optional,
        ) = _lock_publisher_inputs(
            internal=internal,
            requested_runs=latest,
            requested_optional=optional_attempt,
        )
        if any(
            locked_latest[role].pk != latest[role].pk for role in latest
        ):
            raise ValueError("consumer attempts changed during failure coordination")
        candidates = (
            DashboardSnapshot.objects.select_for_update(of=("self",))
            .filter(
                key="consumer",
                is_published=True,
                data__contract_version=CONSUMER_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")
        )
        previous = next(
            (candidate for candidate in candidates if _snapshot_static_replay(candidate) is not None),
            None,
        )
        if previous is not None:
            _mark_retained_failure(previous, locked_latest)
            selected = select_public_consumer_snapshot()
            if (
                selected is None
                or getattr(selected, "consumer_publication_state", None)
                != "retained_failure"
            ):
                raise ValueError("consumer retained failure marker did not replay")
    return [], {"consumer"}
