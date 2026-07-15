"""Strict append-only publication contract for the official employment page."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .labor_official import (
    CONTINUED_4WK,
    CONTINUED_SA,
    INITIAL_4WK,
    INITIAL_SA,
    IUR_SA,
    DOLWeeklyClaimsProvider,
)
from .models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    Source,
    SourceLicense,
)
from .providers import BLSProvider
from .raw_evidence import EVIDENCE_BUNDLE_CONTENT_TYPE
from .services import SERIES_CATALOG, ensure_source, public_source_notices

EMPLOYMENT_CONTRACT_VERSION = 2
EMPLOYMENT_FORMULA_VERSION = "bls-dol-evidence-v2"
EMPLOYMENT_TITLE = "就业"
EMPLOYMENT_SUMMARY = (
    "BLS 非农、家庭调查与 JOLTS 和 DOL 周度受保失业申领分组发布。"
    "非农新增、3M 均值和时薪同比为可复算派生；所有组件分别保留"
    "数值日、抓取时间、批次、初值/修订状态与来源。"
)
EMPLOYMENT_RUNNING_TIMEOUT = timedelta(hours=2)
EMPLOYMENT_BLS_SOURCE = "bls"
EMPLOYMENT_DOL_SOURCE = "dol-eta-ui"
EMPLOYMENT_DOL_DATASET = "national-weekly-claims"
EMPLOYMENT_BLS_REQUEST_SERIES = (
    "CES0000000001",
    "LNS14000000",
    "LNS11300000",
    "CES0500000003",
    "JTS000000000000000JOL",
    "JTS000000000000000JOR",
    "JTS000000000000000HIL",
    "JTS000000000000000HIR",
    "JTS000000000000000QUL",
    "JTS000000000000000QUR",
    "JTS000000000000000LDL",
    "JTS000000000000000LDR",
    "CUSR0000SA0",
    "CUUR0000SA0",
    "CUSR0000SA0L1E",
    "CUUR0000SA0L1E",
    "CUSR0000SAH1",
    "CUUR0000SAH1",
    "CUSR0000SACL1E",
    "CUUR0000SACL1E",
    "CUSR0000SASLE",
    "CUUR0000SASLE",
    "WPSFD4",
    "WPUFD4",
)
EMPLOYMENT_BLS_REQUIRED_SERIES = frozenset(
    {
        "ces0000000001",
        "ces0500000003",
        "lns14000000",
        "lns11300000",
        "jts000000000000000jol",
        "jts000000000000000jor",
        "jts000000000000000hil",
        "jts000000000000000hir",
        "jts000000000000000qul",
        "jts000000000000000qur",
        "jts000000000000000ldl",
        "jts000000000000000ldr",
    }
)
EMPLOYMENT_DOL_REQUIRED_SERIES = frozenset(
    {
        INITIAL_SA.lower(),
        INITIAL_4WK.lower(),
        CONTINUED_SA.lower(),
        CONTINUED_4WK.lower(),
        IUR_SA.lower(),
    }
)
EMPLOYMENT_REQUIRED_METRIC_KEYS = frozenset(
    {
        "nonfarm-payroll-change",
        "nonfarm-payroll-change-3m",
        "average-hourly-earnings-yoy",
        "lns14000000",
        "lns11300000",
        "jts000000000000000jol",
        "jts000000000000000qur",
        INITIAL_SA.lower(),
        INITIAL_4WK.lower(),
        CONTINUED_SA.lower(),
        IUR_SA.lower(),
    }
)
EMPLOYMENT_REQUIRED_CHART_KEYS = frozenset(
    {
        "payroll-change",
        "average-hourly-earnings-yoy",
        "labor-slack",
        "jolts-rates",
        "initial-claims",
        "continued-claims",
    }
)
EMPLOYMENT_REQUIRED_SECTION_KEYS = frozenset({"jolts-official-levels", "employment-methodology"})
EMPLOYMENT_PAYLOAD_KEYS = frozenset(
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
        "component_roles",
        "refresh_cycle_id",
        "fallback_state",
        "fallback_source",
        "semantic_boundary",
        "fingerprint",
        "payload_integrity_hash",
    }
)


class EmploymentPublicationPostconditionError(ValueError):
    """A completed input pair could not become the current public revision."""


@dataclass(frozen=True)
class EmploymentRunEvidence:
    role: str
    run: IngestionRun
    artifact: RawArtifact
    fetched_at: datetime
    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class EmploymentEvidence:
    bls: EmploymentRunEvidence
    dol: EmploymentRunEvidence
    refresh_cycle_id: str


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
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


def _current_license(source: Source, *, lock: bool = False) -> SourceLicense | None:
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


def _record_quality(record: dict[str, Any]) -> str:
    quality = str(record.get("quality_status") or Observation.Quality.FRESH)
    return quality if quality in Observation.Quality.values else Observation.Quality.FRESH


def _record_contract(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(record.get("series_id") or "").lower(),
        str(record.get("date") or ""),
        f"{Decimal(str(record.get('value'))):.8f}",
        _record_quality(record),
        _canonical(record.get("metadata") or {}),
    )


def _run_reference(evidence: EmploymentRunEvidence) -> dict[str, Any]:
    run = evidence.run
    return {
        "role": evidence.role,
        "ingestion_run_id": run.pk,
        "source_key": run.source.key,
        "dataset": run.dataset,
        "batch_id": str(run.batch_id),
        "status": run.status,
        "row_count": run.row_count,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "fetched_at": evidence.fetched_at.isoformat(),
        "artifact_id": evidence.artifact.pk,
        "artifact_uri": evidence.artifact.uri,
        "artifact_sha256": evidence.artifact.sha256,
        "artifact_size": evidence.artifact.size_bytes,
    }


def _load_artifact(
    run: IngestionRun,
    *,
    expected_content_type: str,
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
        raise ValueError("employment run fetch chronology is invalid")
    query = RawArtifact.objects.filter(run=run).order_by("pk")
    if lock:
        query = query.select_for_update()
    artifacts = list(query)
    if len(artifacts) != 1:
        raise ValueError("employment run requires exactly one private artifact")
    artifact = artifacts[0]
    digest = str(metadata.get("sha256") or "").lower()
    try:
        declared_size = int(metadata.get("byte_length"))
    except (TypeError, ValueError) as exc:
        raise ValueError("employment artifact size witness is invalid") from exc
    expected_uri = f"private://{run.source.key}/{digest[:2]}/{digest}.bin"
    if (
        len(digest) != 64
        or artifact.sha256 != digest
        or artifact.size_bytes != declared_size
        or artifact.uri != expected_uri
        or artifact.content_type != expected_content_type
        or metadata.get("content_type") != expected_content_type
        or metadata.get("raw_artifact_sha256") != digest
        or metadata.get("raw_artifact_uri") != expected_uri
    ):
        raise ValueError("employment artifact database witness is invalid")
    try:
        payload = _artifact_path(digest).read_bytes()
    except OSError as exc:
        raise ValueError("employment private artifact bytes are unavailable") from exc
    if len(payload) != declared_size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("employment private artifact bytes are missing or tampered")
    return artifact, payload, fetched_at, metadata


def _validate_series_contract(
    observations: Iterable[Observation],
    *,
    source: Source,
    frequency: str,
) -> None:
    for observation in observations:
        if observation.series_id is None:
            raise ValueError("employment observation lacks a series definition")
        catalogue = SERIES_CATALOG.get(observation.series.key.upper())
        if (
            catalogue is None
            or observation.series.source_id != source.pk
            or observation.series.frequency != frequency
            or observation.series.unit != catalogue[1]
            or catalogue[2] != frequency
        ):
            raise ValueError("employment series source, unit or frequency drifted")


def _validate_observation_batch(
    evidence_records: Iterable[dict[str, Any]],
    *,
    run: IngestionRun,
    source: Source,
    fetched_at: datetime,
    frequency: str,
    lock: bool,
) -> tuple[Observation, ...]:
    base_query = Observation.objects.filter(batch_id=run.batch_id)
    if lock:
        # Lock only the Observation table. ``series`` and ``fallback_source``
        # are nullable FKs; PostgreSQL rejects FOR UPDATE on the nullable side
        # of the LEFT OUTER JOIN emitted by select_related().
        list(base_query.select_for_update().order_by("pk").values_list("pk", flat=True))
    observations = tuple(
        base_query.select_related("series", "source").order_by("series__key", "value_date", "pk")
    )
    _validate_series_contract(observations, source=source, frequency=frequency)
    expected = sorted(_record_contract(record) for record in evidence_records)
    actual = sorted(
        (
            item.series.key,
            item.value_date.date().isoformat(),
            f"{item.value:.8f}",
            item.quality_status,
            _canonical(item.metadata or {}),
        )
        for item in observations
        if item.instrument_id is None
        and item.source_id == source.pk
        and item.series.source_id == source.pk
        and item.series.frequency == frequency
        and item.as_of == item.value_date
        and item.fetched_at == fetched_at
        and item.fallback_source_id is None
    )
    if (
        len(observations) != len(expected)
        or len(actual) != len(expected)
        or actual != expected
        or run.row_count != len(expected)
    ):
        raise ValueError("employment normalized rows do not match exact evidence")
    return observations


def _validate_bls_run(
    run: IngestionRun,
    *,
    lock: bool = False,
) -> EmploymentRunEvidence:
    if (
        run.source.key != EMPLOYMENT_BLS_SOURCE
        or not _is_employment_bls_dataset(run.dataset)
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        or run.completed_at is None
        or run.completed_at < run.started_at
        or _current_license(run.source, lock=lock) is None
    ):
        raise ValueError("employment requires a complete licensed BLS run")
    metadata = dict(run.metadata or {})
    content_type = str(metadata.get("content_type") or "").lower()
    if "json" not in content_type or metadata.get("provider") != EMPLOYMENT_BLS_SOURCE:
        raise ValueError("employment BLS provider or content type is invalid")
    artifact, payload, fetched_at, metadata = _load_artifact(
        run,
        expected_content_type=str(metadata.get("content_type")),
        lock=lock,
    )
    endpoint = urlparse(str(metadata.get("endpoint") or ""))
    witness = metadata.get("request_witness")
    if (
        endpoint.scheme != "https"
        or endpoint.netloc.lower() != "api.bls.gov"
        or endpoint.path != "/publicAPI/v2/timeseries/data/"
        or endpoint.query
        or not isinstance(witness, dict)
        or set(witness) != {"series_ids", "start_year", "end_year"}
    ):
        raise ValueError("employment BLS endpoint or request witness is invalid")
    series_ids = witness.get("series_ids")
    try:
        start_year = int(witness.get("start_year"))
        end_year = int(witness.get("end_year"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("employment BLS request years are invalid") from exc
    if (
        not isinstance(series_ids, list)
        or any(not isinstance(item, str) or not item for item in series_ids)
        or run.dataset != "series:" + ",".join(series_ids)
        or tuple(series_ids) != EMPLOYMENT_BLS_REQUEST_SERIES
    ):
        raise ValueError("employment BLS request coverage is incomplete")
    records, replay_metadata = BLSProvider.parse_series_json_bytes(
        payload,
        series_ids=series_ids,
        start_year=start_year,
        end_year=end_year,
        fetched_at=fetched_at,
    )
    replay_keys = {
        "requested_series",
        "returned_series",
        "missing_series",
        "messages",
        "quality_status",
        "start_year",
        "end_year",
        "latest_value_dates",
    }
    if (
        any(metadata.get(key) != replay_metadata.get(key) for key in replay_keys)
        or replay_metadata.get("missing_series")
        or set(replay_metadata.get("returned_series") or []) != set(series_ids)
        or not records
    ):
        raise ValueError("employment BLS metadata does not replay from exact JSON")
    _validate_observation_batch(
        records,
        run=run,
        source=run.source,
        fetched_at=fetched_at,
        frequency="monthly",
        lock=lock,
    )
    if EMPLOYMENT_BLS_REQUIRED_SERIES - {
        str(record.get("series_id") or "").lower() for record in records
    }:
        raise ValueError("employment BLS evidence lacks required series")
    return EmploymentRunEvidence(
        role="bls",
        run=run,
        artifact=artifact,
        fetched_at=fetched_at,
        records=tuple(records),
    )


def _validate_dol_run(
    run: IngestionRun,
    *,
    lock: bool = False,
) -> EmploymentRunEvidence:
    if (
        run.source.key != EMPLOYMENT_DOL_SOURCE
        or run.dataset != EMPLOYMENT_DOL_DATASET
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        or run.completed_at is None
        or run.completed_at < run.started_at
        or _current_license(run.source, lock=lock) is None
    ):
        raise ValueError("employment requires a complete licensed DOL run")
    metadata = dict(run.metadata or {})
    if metadata.get("provider") != EMPLOYMENT_DOL_SOURCE:
        raise ValueError("employment DOL provider identity is invalid")
    artifact, payload, fetched_at, metadata = _load_artifact(
        run,
        expected_content_type=EVIDENCE_BUNDLE_CONTENT_TYPE,
        lock=lock,
    )
    records, replay_metadata = DOLWeeklyClaimsProvider.replay_evidence_bundle(payload)
    if any(metadata.get(key) != value for key, value in replay_metadata.items()):
        raise ValueError("employment DOL metadata does not replay from evidence")
    _validate_observation_batch(
        records,
        run=run,
        source=run.source,
        fetched_at=fetched_at,
        frequency="weekly",
        lock=lock,
    )
    if EMPLOYMENT_DOL_REQUIRED_SERIES - {
        str(record.get("series_id") or "").lower() for record in records
    }:
        raise ValueError("employment DOL evidence lacks required series")
    return EmploymentRunEvidence(
        role="dol",
        run=run,
        artifact=artifact,
        fetched_at=fetched_at,
        records=tuple(records),
    )


def _validate_employment_runs(
    bls_run: IngestionRun,
    dol_run: IngestionRun,
    *,
    lock: bool = False,
) -> EmploymentEvidence:
    bls = _validate_bls_run(bls_run, lock=lock)
    dol = _validate_dol_run(dol_run, lock=lock)
    cycles = {
        str((bls.run.metadata or {}).get("refresh_cycle_id") or ""),
        str((dol.run.metadata or {}).get("refresh_cycle_id") or ""),
    }
    if len(cycles) != 1 or not next(iter(cycles)):
        raise ValueError("employment inputs do not share one refresh cycle")
    return EmploymentEvidence(
        bls=bls,
        dol=dol,
        refresh_cycle_id=next(iter(cycles)),
    )


def _licence_scope(source_key: str, licences: dict[str, SourceLicense]) -> str:
    if source_key == "internal":
        return "Original calculation from attributed BLS inputs"
    licence = licences.get(source_key)
    if licence is None:
        raise ValueError("employment payload references an unlicensed source")
    return licence.scope


def _quality(items: Iterable[dict[str, Any]]) -> str:
    statuses = {str(item.get("quality_status") or "") for item in items}
    if Observation.Quality.ERROR in statuses:
        return Observation.Quality.ERROR
    if Observation.Quality.STALE in statuses:
        return Observation.Quality.STALE
    if Observation.Quality.FALLBACK in statuses:
        return Observation.Quality.FALLBACK
    if statuses == {Observation.Quality.FRESH}:
        return Observation.Quality.FRESH
    return Observation.Quality.ESTIMATED


def _enrich_metric(
    metric: dict[str, Any],
    *,
    licences: dict[str, SourceLicense],
) -> dict[str, Any]:
    enriched = deepcopy(metric)
    enriched["license_scope"] = _licence_scope(str(enriched.get("source_key") or ""), licences)
    return enriched


def _component_observations(
    evidence: EmploymentEvidence,
) -> tuple[list[Observation], list[Observation]]:
    bls = list(
        Observation.objects.filter(
            source=evidence.bls.run.source,
            batch_id=evidence.bls.run.batch_id,
            series__key__in=EMPLOYMENT_BLS_REQUIRED_SERIES,
        )
        .select_related("series", "source")
        .order_by("series__key", "value_date", "pk")
    )
    dol = list(
        Observation.objects.filter(
            source=evidence.dol.run.source,
            batch_id=evidence.dol.run.batch_id,
            series__key__in=EMPLOYMENT_DOL_REQUIRED_SERIES,
        )
        .select_related("series", "source")
        .order_by("series__key", "value_date", "pk")
    )
    if not bls or not dol:
        raise ValueError("employment component observations are unavailable")
    return bls, dol


def _build_employment_payload(
    evidence: EmploymentEvidence,
    *,
    publication_batch_id: uuid.UUID,
) -> tuple[dict[str, Any], datetime, str]:
    from .official_data import _employment_page_data

    licences = {
        source.key: licence
        for source in (evidence.bls.run.source, evidence.dol.run.source)
        if (licence := _current_license(source)) is not None
    }
    if set(licences) != {EMPLOYMENT_BLS_SOURCE, EMPLOYMENT_DOL_SOURCE}:
        raise ValueError("employment licences changed during publication")
    metrics, charts, sections = _employment_page_data(
        bls_batch_id=evidence.bls.run.batch_id,
        dol_batch_id=evidence.dol.run.batch_id,
        apply_freshness=False,
    )
    if (
        {item.get("key") for item in metrics} != EMPLOYMENT_REQUIRED_METRIC_KEYS
        or {item.get("key") for item in charts} != EMPLOYMENT_REQUIRED_CHART_KEYS
        or {item.get("key") for item in sections} != EMPLOYMENT_REQUIRED_SECTION_KEYS
    ):
        raise ValueError("employment page builder did not produce the exact contract")
    metrics = [_enrich_metric(item, licences=licences) for item in metrics]
    for chart in charts:
        source_keys = list(chart.get("source_keys") or [])
        chart["license_scopes"] = [
            f"{key}: {_licence_scope(key, licences)}" for key in source_keys if key != "internal"
        ]
        chart["fallback_sources"] = []

    bls_observations, dol_observations = _component_observations(evidence)
    observation_by_batch = {
        str(evidence.bls.run.batch_id): bls_observations,
        str(evidence.dol.run.batch_id): dol_observations,
    }
    for section in sections:
        if section["key"] == "jolts-official-levels":
            section["rows"] = [
                _enrich_metric(item, licences=licences) for item in section.get("rows") or []
            ]
            component_rows = bls_observations
            source_keys = [EMPLOYMENT_BLS_SOURCE]
            batches = [str(evidence.bls.run.batch_id)]
        else:
            component_rows = [*bls_observations, *dol_observations]
            source_keys = [EMPLOYMENT_BLS_SOURCE, EMPLOYMENT_DOL_SOURCE]
            batches = sorted(observation_by_batch)
        latest_by_series: dict[str, Observation] = {}
        for observation in component_rows:
            current = latest_by_series.get(observation.series.key)
            if current is None or observation.value_date > current.value_date:
                latest_by_series[observation.series.key] = observation
        latest = list(latest_by_series.values())
        section.update(
            {
                "source_keys": source_keys,
                "license_scopes": [
                    f"{key}: {_licence_scope(key, licences)}" for key in source_keys
                ],
                "fallback_sources": [],
                "as_of": min(item.as_of for item in latest).isoformat(),
                "fetched_at": max(item.fetched_at for item in latest).isoformat(),
                "fresh_until": min(
                    str(item["fresh_until"])
                    for item in metrics
                    if str(item.get("batch_id") or "") in batches and item.get("fresh_until")
                ),
                "quality_status": _quality(
                    [{"quality_status": item.quality_status} for item in latest]
                ),
                "batch_ids": batches,
            }
        )

    role_series = {
        "ces": ["ces0000000001", "ces0500000003"],
        "cps": ["lns11300000", "lns14000000"],
        "jolts": sorted(
            EMPLOYMENT_BLS_REQUIRED_SERIES
            - {"ces0000000001", "ces0500000003", "lns11300000", "lns14000000"}
        ),
        "claims": sorted(EMPLOYMENT_DOL_REQUIRED_SERIES),
    }
    component_roles = {
        role: {
            "source_key": (EMPLOYMENT_DOL_SOURCE if role == "claims" else EMPLOYMENT_BLS_SOURCE),
            "ingestion_run_id": (evidence.dol.run.pk if role == "claims" else evidence.bls.run.pk),
            "batch_id": str(
                evidence.dol.run.batch_id if role == "claims" else evidence.bls.run.batch_id
            ),
            "series_keys": series_keys,
        }
        for role, series_keys in role_series.items()
    }
    items = [*metrics, *charts, *sections]
    as_of_values = [
        parsed
        for item in [*metrics, *charts]
        if (parsed := _parse_datetime(item.get("as_of"))) is not None
    ]
    if not as_of_values:
        raise ValueError("employment payload has no valid observation timestamp")
    data = {
        "demo": False,
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": sections,
        "component_batches": sorted(
            {str(evidence.bls.run.batch_id), str(evidence.dol.run.batch_id)}
        ),
        "source_keys": [EMPLOYMENT_BLS_SOURCE, EMPLOYMENT_DOL_SOURCE, "internal"],
        "required_notices": public_source_notices([EMPLOYMENT_BLS_SOURCE, EMPLOYMENT_DOL_SOURCE]),
        "fresh_until": min(str(item["fresh_until"]) for item in items if item.get("fresh_until")),
        "publication_batch_id": str(publication_batch_id),
        "contract_version": EMPLOYMENT_CONTRACT_VERSION,
        "formula_version": EMPLOYMENT_FORMULA_VERSION,
        "required_metric_keys": sorted(EMPLOYMENT_REQUIRED_METRIC_KEYS),
        "required_chart_keys": sorted(EMPLOYMENT_REQUIRED_CHART_KEYS),
        "required_section_keys": sorted(EMPLOYMENT_REQUIRED_SECTION_KEYS),
        "input_runs": [_run_reference(evidence.bls), _run_reference(evidence.dol)],
        "component_roles": component_roles,
        "refresh_cycle_id": evidence.refresh_cycle_id,
        "fallback_state": "none",
        "fallback_source": None,
        "semantic_boundary": (
            "就业页面复述 BLS 和 DOL 官方统计并透明计算差分、移动均值与同比；"
            "不是实时就业人数、完整历史发布 vintage、衰退判断或交易信号。"
        ),
    }
    fingerprint_payload = deepcopy(data)
    fingerprint_payload.pop("publication_batch_id")
    data["fingerprint"] = hashlib.sha256(
        _canonical(
            {"title": EMPLOYMENT_TITLE, "summary": EMPLOYMENT_SUMMARY, "data": fingerprint_payload}
        ).encode()
    ).hexdigest()
    data["payload_integrity_hash"] = hashlib.sha256(
        _canonical(
            {"title": EMPLOYMENT_TITLE, "summary": EMPLOYMENT_SUMMARY, "data": data}
        ).encode()
    ).hexdigest()
    return data, min(as_of_values), _quality([*metrics, *charts])


def _batch_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_batch_ids(item))
        return sorted(result)
    return sorted({item.strip() for item in str(value or "").split(",") if item.strip()})


def _metric_metadata(metric: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    input_batch_ids = _batch_ids(metric.get("batch_id"))
    metadata = deepcopy(metric.get("metadata") or {})
    input_lineage = metadata.get("input_lineage")
    if not isinstance(input_lineage, list) or not input_lineage:
        input_lineage = [
            {
                "series_key": metric["key"],
                "source_key": metric["source_key"],
                "value_date": metric["value_date"],
                "as_of": metric["as_of"],
                "fetched_at": metric["fetched_at"],
                "batch_id": input_batch_ids[0],
                "quality_status": metric["quality_status"],
                "fallback_source": metric.get("fallback_source"),
                "license_scope": metric.get("license_scope"),
            }
        ]
    return {
        **metadata,
        "dashboard_key": "employment",
        "metric_key": metric["key"],
        "source_keys": list(metric.get("source_keys") or []),
        "component_batch_id": ",".join(input_batch_ids),
        "input_batch_ids": input_batch_ids,
        "input_lineage": input_lineage,
        "input_metadata": metadata,
        "contract_version": EMPLOYMENT_CONTRACT_VERSION,
        "formula_version": EMPLOYMENT_FORMULA_VERSION,
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
        f"employment-{key}" for key in EMPLOYMENT_REQUIRED_METRIC_KEYS
    }:
        return False
    for row in rows:
        metric = metrics[row.key.removeprefix("employment-")]
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
    return True


def _exact_keyed_items(value: Any, required_keys: frozenset[str]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == len(required_keys)
        and all(isinstance(item, dict) for item in value)
        and {item.get("key") for item in value} == required_keys
    )


def _employment_snapshot_static_replay(
    snapshot: DashboardSnapshot,
) -> SimpleNamespace | None:
    try:
        data = dict(snapshot.data or {})
        audited = dict(data)
        audited.pop("refresh_failure", None)
        metrics = audited.get("metrics")
        charts = audited.get("charts")
        sections = audited.get("sections")
        component_roles = audited.get("component_roles")
        if (
            snapshot.key != "employment"
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or snapshot.title != EMPLOYMENT_TITLE
            or snapshot.summary != EMPLOYMENT_SUMMARY
            or set(audited) != EMPLOYMENT_PAYLOAD_KEYS
            or audited.get("contract_version") != EMPLOYMENT_CONTRACT_VERSION
            or audited.get("formula_version") != EMPLOYMENT_FORMULA_VERSION
            or audited.get("demo") is not False
            or audited.get("fallback_state") != "none"
            or audited.get("fallback_source") is not None
            or audited.get("publication_batch_id") != str(snapshot.batch_id)
            or set(audited.get("required_metric_keys") or []) != EMPLOYMENT_REQUIRED_METRIC_KEYS
            or set(audited.get("required_chart_keys") or []) != EMPLOYMENT_REQUIRED_CHART_KEYS
            or set(audited.get("required_section_keys") or []) != EMPLOYMENT_REQUIRED_SECTION_KEYS
            or not _exact_keyed_items(metrics, EMPLOYMENT_REQUIRED_METRIC_KEYS)
            or not _exact_keyed_items(charts, EMPLOYMENT_REQUIRED_CHART_KEYS)
            or not _exact_keyed_items(sections, EMPLOYMENT_REQUIRED_SECTION_KEYS)
            or not isinstance(component_roles, dict)
            or len(component_roles) != 4
            or set(component_roles) != {"ces", "cps", "jolts", "claims"}
            or not all(isinstance(item, dict) for item in component_roles.values())
        ):
            return None
        witnesses = audited.get("input_runs")
        if not isinstance(witnesses, list) or len(witnesses) != 2:
            return None
        by_role = {
            str(item.get("role") or ""): item for item in witnesses if isinstance(item, dict)
        }
        if set(by_role) != {"bls", "dol"}:
            return None
        runs = {
            role: IngestionRun.objects.filter(pk=witness.get("ingestion_run_id"))
            .select_related("source")
            .first()
            for role, witness in by_role.items()
        }
        if any(run is None for run in runs.values()):
            return None
        evidence = _validate_employment_runs(runs["bls"], runs["dol"])
        if (
            _run_reference(evidence.bls) != by_role["bls"]
            or _run_reference(evidence.dol) != by_role["dol"]
        ):
            return None
        expected, expected_as_of, expected_quality = _build_employment_payload(
            evidence,
            publication_batch_id=snapshot.batch_id,
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


def _is_employment_bls_dataset(dataset: str) -> bool:
    return dataset == "series:" + ",".join(EMPLOYMENT_BLS_REQUEST_SERIES)


def _attempt_reference(run: IngestionRun) -> dict[str, Any]:
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
        "refresh_cycle_id": str((run.metadata or {}).get("refresh_cycle_id") or ""),
    }


def _latest_attempts(*, lock: bool = False) -> dict[str, IngestionRun] | None:
    bls_query = IngestionRun.objects.filter(
        source__key=EMPLOYMENT_BLS_SOURCE,
        dataset="series:" + ",".join(EMPLOYMENT_BLS_REQUEST_SERIES),
    )
    dol_query = IngestionRun.objects.filter(
        source__key=EMPLOYMENT_DOL_SOURCE,
        dataset=EMPLOYMENT_DOL_DATASET,
    )
    if lock:
        bls_query = bls_query.select_for_update(of=("self",))
        dol_query = dol_query.select_for_update(of=("self",))
    bls = bls_query.select_related("source").order_by("-started_at", "-id").first()
    dol = dol_query.select_related("source").order_by("-started_at", "-id").first()
    return {"bls": bls, "dol": dol} if bls is not None and dol is not None else None


def _validated_failure_marker(
    marker: Any,
    *,
    replay: EmploymentEvidence,
) -> dict[str, IngestionRun]:
    reason_code = marker.get("reason_code") if isinstance(marker, dict) else None
    if reason_code not in {
        "latest-attempt-incomplete",
        "publication-postcondition",
    }:
        raise ValueError("employment retained-failure marker is missing")
    attempts = marker.get("attempts")
    checked_at = _parse_datetime(marker.get("checked_at"))
    replay_runs = {"bls": replay.bls.run, "dol": replay.dol.run}
    if not isinstance(attempts, dict) or set(attempts) != {"bls", "dol"}:
        raise ValueError("employment retained-failure marker is invalid")
    marker_runs: dict[str, IngestionRun] = {}
    for role, reference in attempts.items():
        try:
            run_id = int(reference.get("ingestion_run_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("employment retained-failure run identity is invalid") from exc
        run = IngestionRun.objects.filter(pk=run_id).select_related("source").first()
        if (
            run is None
            or _attempt_reference(run) != reference
            or (
                role == "bls"
                and (
                    run.source.key != EMPLOYMENT_BLS_SOURCE
                    or not _is_employment_bls_dataset(run.dataset)
                )
            )
            or (
                role == "dol"
                and (
                    run.source.key != EMPLOYMENT_DOL_SOURCE or run.dataset != EMPLOYMENT_DOL_DATASET
                )
            )
        ):
            raise ValueError("employment retained-failure run witness is invalid")
        marker_runs[role] = run
    statuses = {run.status for run in marker_runs.values()}
    if (
        checked_at is None
        or checked_at > timezone.now() + timedelta(minutes=5)
        or (
            reason_code == "latest-attempt-incomplete"
            and (
                not statuses
                & {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}
                or not statuses
                <= {
                    IngestionRun.Status.SUCCESS,
                    IngestionRun.Status.FAILED,
                    IngestionRun.Status.PARTIAL,
                }
            )
        )
        or (
            reason_code == "publication-postcondition"
            and statuses != {IngestionRun.Status.SUCCESS}
        )
        or any(run.started_at < replay_runs[role].started_at for role, run in marker_runs.items())
        or any(
            run.completed_at is not None and checked_at < run.completed_at
            for run in marker_runs.values()
        )
    ):
        raise ValueError("employment retained-failure marker is invalid")
    return marker_runs


def _attempt_not_after(left: IngestionRun, right: IngestionRun) -> bool:
    return (left.started_at, left.pk) <= (right.started_at, right.pk)


def _validate_transition_marker(
    marker: Any,
    *,
    latest: dict[str, IngestionRun],
    replay: EmploymentEvidence,
) -> None:
    if marker is None:
        return
    marker_runs = _validated_failure_marker(marker, replay=replay)
    if any(not _attempt_not_after(marker_runs[role], latest[role]) for role in latest):
        raise ValueError("employment transition marker is ahead of latest attempts")


def _employment_public_state(
    snapshot: DashboardSnapshot,
    replay: SimpleNamespace,
) -> str:
    latest = _latest_attempts()
    if latest is None:
        raise ValueError("employment latest attempts are missing")
    replay_runs = {"bls": replay.evidence.bls.run, "dol": replay.evidence.dol.run}
    marker = (snapshot.data or {}).get("refresh_failure")
    if all(latest[role].pk == replay_runs[role].pk for role in replay_runs):
        if marker is not None:
            raise ValueError("employment current snapshot carries a failure marker")
        deadline = _parse_datetime(replay.expected_data.get("fresh_until"))
        if deadline is None:
            raise ValueError("employment freshness deadline is invalid")
        return "natural_expiry" if timezone.now() > deadline else "current_candidate"
    if any(latest[role].started_at < replay_runs[role].started_at for role in replay_runs):
        raise ValueError("employment attempt chronology regressed")
    statuses = {run.status for run in latest.values()}
    if statuses & {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}:
        if any(
            run.status == IngestionRun.Status.RUNNING
            and run.started_at < timezone.now() - EMPLOYMENT_RUNNING_TIMEOUT
            for run in latest.values()
        ):
            raise ValueError("employment mixed failure transition timed out")
        if marker is not None:
            marker_runs = _validated_failure_marker(
                marker,
                replay=replay.evidence,
            )
            if (
                marker.get("reason_code") == "latest-attempt-incomplete"
                and all(marker_runs[role].pk == latest[role].pk for role in latest)
            ):
                return "retained_failure"
            if any(not _attempt_not_after(marker_runs[role], latest[role]) for role in latest):
                raise ValueError("employment failure marker chronology regressed")
        newest = max(run.completed_at or run.started_at for run in latest.values())
        if newest < timezone.now() - EMPLOYMENT_RUNNING_TIMEOUT:
            raise ValueError("employment uncoordinated failure transition timed out")
        return "transition_pending"
    if IngestionRun.Status.RUNNING in statuses:
        _validate_transition_marker(
            marker,
            latest=latest,
            replay=replay.evidence,
        )
        if any(
            run.status == IngestionRun.Status.RUNNING
            and run.started_at < timezone.now() - EMPLOYMENT_RUNNING_TIMEOUT
            for run in latest.values()
        ):
            raise ValueError("employment running transition timed out")
        return "transition_pending"
    if statuses == {IngestionRun.Status.SUCCESS}:
        if marker is not None:
            marker_runs = _validated_failure_marker(marker, replay=replay.evidence)
            if (
                marker.get("reason_code") == "publication-postcondition"
                and all(marker_runs[role].pk == latest[role].pk for role in latest)
            ):
                return "retained_failure"
            if any(
                not _attempt_not_after(marker_runs[role], latest[role])
                for role in latest
            ):
                raise ValueError("employment success marker chronology regressed")
        newest = max(run.completed_at or run.started_at for run in latest.values())
        if newest < timezone.now() - EMPLOYMENT_RUNNING_TIMEOUT:
            raise ValueError("employment unpublished success transition timed out")
        return "transition_pending"
    raise ValueError("employment latest attempts have unsupported states")


def employment_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    replay = _employment_snapshot_static_replay(snapshot)
    if replay is None:
        return False
    try:
        state = _employment_public_state(snapshot, replay)
    except (TypeError, ValueError):
        return False
    expected = replay.expected_quality
    if state == "current_candidate" and snapshot.quality_status != expected:
        return False
    if state == "retained_failure":
        if snapshot.quality_status != Observation.Quality.STALE:
            return False
    elif state in {"natural_expiry", "transition_pending"}:
        if snapshot.quality_status not in {expected, Observation.Quality.STALE}:
            return False
    snapshot.employment_publication_state = state
    return True


def select_public_employment_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    queryset = candidates
    if queryset is None:
        queryset = (
            DashboardSnapshot.objects.filter(
                key="employment",
                is_published=True,
                data__contract_version=EMPLOYMENT_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")[:50]
        )
    for candidate in queryset:
        if employment_snapshot_is_publicly_displayable(candidate):
            presented = deepcopy(candidate)
            presented.data = deepcopy(candidate.data or {})
            presented.employment_publication_state = getattr(
                candidate, "employment_publication_state", None
            )
            if presented.employment_publication_state != "retained_failure":
                presented.data.pop("refresh_failure", None)
            return presented
    return None


def _store_metric_rows(snapshot: DashboardSnapshot) -> None:
    for metric in snapshot.data["metrics"]:
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if value_date is None or as_of is None or fetched_at is None:
            raise ValueError("employment metric timestamp is invalid")
        MetricSnapshot.objects.create(
            key=f"employment-{metric['key']}",
            label=metric["label"],
            value=Decimal(str(metric["value"])),
            display_value=metric["display_value"],
            change=(Decimal(str(metric["change"])) if metric.get("change") is not None else None),
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


def _published_input_run_pair(snapshot: DashboardSnapshot) -> dict[str, int] | None:
    witnesses = (snapshot.data or {}).get("input_runs")
    if not isinstance(witnesses, list) or len(witnesses) != 2:
        return None
    try:
        pair = {
            str(item["role"]): int(item["ingestion_run_id"])
            for item in witnesses
            if isinstance(item, dict)
        }
    except (KeyError, TypeError, ValueError):
        return None
    return pair if set(pair) == {"bls", "dol"} else None


def publish_employment_revision(
    *,
    bls_run: IngestionRun,
    dol_run: IngestionRun,
    publication_batch_id: uuid.UUID | None = None,
) -> DashboardSnapshot | None:
    internal = ensure_source("internal")
    with transaction.atomic():
        requested_run_ids = {bls_run.pk, dol_run.pk}
        run_sources = list(
            IngestionRun.objects.filter(pk__in=requested_run_ids).values_list(
                "pk", "source_id"
            )
        )
        if {run_id for run_id, _source_id in run_sources} != requested_run_ids:
            raise ValueError("employment publisher input run is missing")
        source_ids = {internal.pk, *(source_id for _run_id, source_id in run_sources)}
        list(
            Source.objects.select_for_update()
            .filter(pk__in=source_ids)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        locked_by_id = {
            run.pk: run
            for run in IngestionRun.objects.select_for_update(of=("self",))
            .filter(pk__in=requested_run_ids)
            .order_by("pk")
        }
        locked = {
            "bls": locked_by_id[bls_run.pk],
            "dol": locked_by_id[dol_run.pk],
        }
        latest = _latest_attempts(lock=True)
        if latest is None or any(latest[role].pk != locked[role].pk for role in locked):
            raise ValueError("employment publisher requires both latest attempts")
        evidence = _validate_employment_runs(locked["bls"], locked["dol"], lock=True)
        target_pair = {role: run.pk for role, run in locked.items()}
        existing = [
            candidate
            for candidate in (
                DashboardSnapshot.objects.select_for_update(of=("self",))
                .filter(
                    key="employment",
                    is_published=True,
                    data__contract_version=EMPLOYMENT_CONTRACT_VERSION,
                )
                .select_related("source")
                .order_by("-created_at", "-id")
            )
            if _published_input_run_pair(candidate) == target_pair
        ]
        if len(existing) > 1:
            raise ValueError("employment input pair has multiple publication revisions")
        if existing:
            if _employment_snapshot_static_replay(existing[0]) is None:
                raise ValueError("employment existing revision is not replayable")
            if (existing[0].data or {}).get("refresh_failure") is not None:
                raise ValueError("employment current revision carries a failure marker")
            return None
        batch_id = publication_batch_id or uuid.uuid4()
        data, as_of, quality = _build_employment_payload(
            evidence,
            publication_batch_id=batch_id,
        )
        snapshot = DashboardSnapshot.objects.create(
            key="employment",
            title=EMPLOYMENT_TITLE,
            summary=EMPLOYMENT_SUMMARY,
            as_of=as_of,
            batch_id=batch_id,
            quality_status=quality,
            data=data,
            source=internal,
            is_published=True,
        )
        _store_metric_rows(snapshot)
        if _employment_snapshot_static_replay(snapshot) is None:
            raise EmploymentPublicationPostconditionError(
                "employment publication postcondition failed"
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
        or (
            "最新 BLS/DOL 就业刷新未形成同一周期的完整可重放批次；"
            "页面保留上一版已审计快照，不发布半成品数据。"
        ),
        "attempts": {role: _attempt_reference(run) for role, run in latest.items()},
    }
    snapshot.data = data
    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])


def _retain_publication_postcondition(
    latest: dict[str, IngestionRun],
    error: Exception,
) -> bool:
    target_pair = {role: run.pk for role, run in latest.items()}
    previous = None
    with transaction.atomic():
        locked_latest = _latest_attempts(lock=True)
        if locked_latest is None or any(
            locked_latest[role].pk != latest[role].pk for role in latest
        ):
            raise ValueError("employment attempts changed after publication rollback")
        candidates = (
            DashboardSnapshot.objects.select_for_update(of=("self",))
            .filter(
                key="employment",
                is_published=True,
                data__contract_version=EMPLOYMENT_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")
        )
        for candidate in candidates:
            if _published_input_run_pair(candidate) == target_pair:
                continue
            try:
                replay = _employment_snapshot_static_replay(candidate)
            except Exception:
                # Retention is best-effort after the original publication
                # failure. If an older revision cannot be replayed, the
                # coordinator must re-raise that original failure unchanged.
                continue
            if replay is not None:
                previous = candidate
                break
        if previous is not None:
            _mark_retained_failure(
                previous,
                locked_latest,
                reason_code="publication-postcondition",
                reason=(
                    "最新完整 BLS/DOL 成功批次未通过 current_candidate 发布后置校验；"
                    f"本次 revision 已回滚并保留上一版：{error}"
                ),
            )
    if previous is None:
        return False
    selected = select_public_employment_snapshot()
    if (
        selected is None
        or getattr(selected, "employment_publication_state", None)
        != "retained_failure"
    ):
        raise ValueError("employment publication-postcondition marker did not replay")
    return True


def coordinate_employment_dashboard(
    runs: Iterable[IngestionRun] | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    _ = runs
    latest = _latest_attempts()
    if latest is None:
        return [], {"employment"}
    if all(
        run.status == IngestionRun.Status.SUCCESS and run.row_count > 0 for run in latest.values()
    ):
        cycles = {
            str((run.metadata or {}).get("refresh_cycle_id") or "") for run in latest.values()
        }
        if len(cycles) != 1 or not next(iter(cycles)):
            return [], {"employment"}
        target_pair = {role: run.pk for role, run in latest.items()}
        selected = select_public_employment_snapshot()
        if (
            selected is not None
            and getattr(selected, "employment_publication_state", None)
            == "natural_expiry"
            and _published_input_run_pair(selected) == target_pair
        ):
            return [], {"employment"}
        try:
            with transaction.atomic():
                published = publish_employment_revision(
                    bls_run=latest["bls"],
                    dol_run=latest["dol"],
                )
                selected = select_public_employment_snapshot()
                state = getattr(selected, "employment_publication_state", None)
                if (
                    published is None
                    and state == "natural_expiry"
                    and selected is not None
                    and _published_input_run_pair(selected) == target_pair
                ):
                    return [], {"employment"}
                if (
                    selected is None
                    or state != "current_candidate"
                ):
                    raise EmploymentPublicationPostconditionError(
                        "employment publication is not the current replayable revision"
                    )
                return ([published] if published is not None else []), set()
        except Exception as exc:
            if _retain_publication_postcondition(latest, exc):
                return [], {"employment"}
            raise
    if any(run.status == IngestionRun.Status.RUNNING for run in latest.values()):
        return [], {"employment"}
    if not any(
        run.status in {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}
        for run in latest.values()
    ):
        raise ValueError("employment latest attempts have unsupported terminal states")
    with transaction.atomic():
        locked_latest = _latest_attempts(lock=True)
        if locked_latest is None or any(
            locked_latest[role].pk != latest[role].pk for role in latest
        ):
            raise ValueError("employment attempts changed during failure coordination")
        previous = next(
            (
                candidate
                for candidate in DashboardSnapshot.objects.select_for_update(
                    of=("self",)
                )
                .filter(
                    key="employment",
                    is_published=True,
                    data__contract_version=EMPLOYMENT_CONTRACT_VERSION,
                )
                .exclude(source__key="demo-market")
                .select_related("source")
                .order_by("-created_at", "-id")[:50]
                if _employment_snapshot_static_replay(candidate) is not None
            ),
            None,
        )
        if previous is not None:
            _mark_retained_failure(previous, locked_latest)
    if previous is not None and select_public_employment_snapshot() is None:
        raise ValueError("employment retained failure marker did not replay")
    return [], {"employment"}
