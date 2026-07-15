"""Strict append-only publication contract for the official inflation page."""

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

from .employment_contract import EMPLOYMENT_BLS_REQUEST_SERIES
from .macro_releases import BEAPIOReleaseProvider
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
from .raw_evidence import EVIDENCE_BUNDLE_CONTENT_TYPE, parse_evidence_bundle
from .services import SERIES_CATALOG, ensure_source, public_source_notices

INFLATION_CONTRACT_VERSION = 2
INFLATION_FORMULA_VERSION = "bls-bea-price-index-v2"
INFLATION_TITLE = "通胀"
INFLATION_SUMMARY = (
    "BLS CPI/PPI 与 BEA PCE 价格指数按官方季调口径和精确自然月透明计算。"
    "基础快照不包含 Treasury 市场代理，也不把当前修订后历史冒充跨发布 vintage。"
)
INFLATION_RUNNING_TIMEOUT = timedelta(hours=2)
INFLATION_BLS_SOURCE = "bls"
INFLATION_BEA_SOURCE = "bea-pio-release"
INFLATION_BEA_DATASET = "personal-income-outlays-release"
INFLATION_BLS_DATASET = "series:" + ",".join(EMPLOYMENT_BLS_REQUEST_SERIES)

INFLATION_BLS_REQUIRED_SERIES = frozenset(
    {
        "cusr0000sa0",
        "cuur0000sa0",
        "cusr0000sa0l1e",
        "cuur0000sa0l1e",
        "cusr0000sah1",
        "cuur0000sah1",
        "cusr0000sacl1e",
        "cuur0000sacl1e",
        "cusr0000sasle",
        "cuur0000sasle",
        "wpsfd4",
        "wpufd4",
    }
)
INFLATION_BEA_ALL_SERIES = frozenset(
    series_id.lower() for series_id in BEAPIOReleaseProvider.SERIES
)
INFLATION_BEA_REQUIRED_SERIES = frozenset(
    {"bea-pce-price-index", "bea-core-pce-price-index"}
)
INFLATION_PREFIXES = (
    "headline-cpi",
    "core-cpi",
    "shelter-cpi",
    "core-goods-cpi",
    "services-less-energy-cpi",
    "final-demand-ppi",
    "pce-price-index",
    "core-pce-price-index",
)
INFLATION_RATE_SUFFIXES = ("mom", "yoy", "3m-annualized", "6m-annualized")
INFLATION_REQUIRED_METRIC_KEYS = frozenset(
    f"{prefix}-{suffix}"
    for prefix in INFLATION_PREFIXES
    for suffix in INFLATION_RATE_SUFFIXES
)
INFLATION_REQUIRED_CHART_KEYS = frozenset(
    {
        "headline-cpi-rates",
        "core-cpi-rates",
        "shelter-cpi-rates",
        "core-goods-cpi-rates",
        "services-less-energy-cpi-rates",
        "final-demand-ppi-rates",
        "pce-price-rates",
        "core-pce-price-rates",
    }
)
INFLATION_REQUIRED_SECTION_KEYS = frozenset(
    {"inflation-methodology", "inflation-coverage-gaps"}
)
INFLATION_COMPONENT_ROLE_KEYS = frozenset(
    {
        "headline-cpi",
        "core-cpi",
        "shelter",
        "core-goods",
        "services-less-energy",
        "final-demand-ppi",
        "pce",
        "core-pce",
    }
)
INFLATION_PAYLOAD_KEYS = frozenset(
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
        "fallback_state",
        "fallback_source",
        "semantic_boundary",
        "fingerprint",
        "payload_integrity_hash",
    }
)


class InflationPublicationPostconditionError(ValueError):
    """A completed input pair could not become the current public revision."""


@dataclass(frozen=True)
class InflationRunEvidence:
    role: str
    run: IngestionRun
    artifact: RawArtifact
    fetched_at: datetime
    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class InflationEvidence:
    bls: InflationRunEvidence
    bea_pio: InflationRunEvidence


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


def _current_open_license(
    source: Source,
    *,
    lock: bool = False,
) -> SourceLicense | None:
    today = timezone.localdate()
    query = SourceLicense.objects.filter(
        source=source,
        is_current=True,
        status=Source.LicenseStatus.OPEN,
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


def _run_reference(evidence: InflationRunEvidence) -> dict[str, Any]:
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
        raise ValueError("inflation run fetch chronology is invalid")
    query = RawArtifact.objects.filter(run=run).order_by("pk")
    if lock:
        query = query.select_for_update()
    artifacts = list(query)
    if len(artifacts) != 1:
        raise ValueError("inflation run requires exactly one private artifact")
    artifact = artifacts[0]
    digest = str(metadata.get("sha256") or "").lower()
    try:
        declared_size = int(metadata.get("byte_length"))
    except (TypeError, ValueError) as exc:
        raise ValueError("inflation artifact size witness is invalid") from exc
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
        raise ValueError("inflation artifact database witness is invalid")
    try:
        payload = _artifact_path(digest).read_bytes()
    except OSError as exc:
        raise ValueError("inflation private artifact bytes are unavailable") from exc
    if len(payload) != declared_size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("inflation private artifact bytes are missing or tampered")
    return artifact, payload, fetched_at, metadata


def _validate_series_contract(
    observations: Iterable[Observation],
    *,
    source: Source,
    frequency: str,
) -> None:
    for observation in observations:
        if observation.series_id is None:
            raise ValueError("inflation observation lacks a series definition")
        catalogue = SERIES_CATALOG.get(observation.series.key.upper())
        if (
            catalogue is None
            or observation.series.source_id != source.pk
            or observation.series.frequency != frequency
            or observation.series.unit != catalogue[1]
            or catalogue[2] != frequency
        ):
            raise ValueError("inflation series source, unit or frequency drifted")


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
        # PostgreSQL cannot FOR UPDATE the nullable side of select_related joins.
        list(base_query.select_for_update().order_by("pk").values_list("pk", flat=True))
    observations = tuple(
        base_query.select_related("series", "source").order_by(
            "series__key", "value_date", "pk"
        )
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
        raise ValueError("inflation normalized rows do not match exact evidence")
    return observations


def _is_inflation_bls_dataset(dataset: str) -> bool:
    return dataset == INFLATION_BLS_DATASET


def _validate_bls_run(
    run: IngestionRun,
    *,
    lock: bool = False,
) -> InflationRunEvidence:
    if (
        run.source.key != INFLATION_BLS_SOURCE
        or not _is_inflation_bls_dataset(run.dataset)
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        or run.completed_at is None
        or run.completed_at < run.started_at
        or _current_open_license(run.source, lock=lock) is None
    ):
        raise ValueError("inflation requires a complete OPEN-licensed BLS run")
    metadata = dict(run.metadata or {})
    content_type = str(metadata.get("content_type") or "").lower()
    if "json" not in content_type or metadata.get("provider") != INFLATION_BLS_SOURCE:
        raise ValueError("inflation BLS provider or content type is invalid")
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
        raise ValueError("inflation BLS endpoint or request witness is invalid")
    series_ids = witness.get("series_ids")
    try:
        start_year = int(witness.get("start_year"))
        end_year = int(witness.get("end_year"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("inflation BLS request years are invalid") from exc
    if (
        not isinstance(series_ids, list)
        or any(not isinstance(item, str) or not item for item in series_ids)
        or tuple(series_ids) != EMPLOYMENT_BLS_REQUEST_SERIES
        or run.dataset != "series:" + ",".join(series_ids)
    ):
        raise ValueError("inflation BLS request coverage is incomplete or reordered")
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
        raise ValueError("inflation BLS metadata does not replay from exact JSON")
    _validate_observation_batch(
        records,
        run=run,
        source=run.source,
        fetched_at=fetched_at,
        frequency="monthly",
        lock=lock,
    )
    if INFLATION_BLS_REQUIRED_SERIES - {
        str(record.get("series_id") or "").lower() for record in records
    }:
        raise ValueError("inflation BLS evidence lacks required price indexes")
    return InflationRunEvidence(
        role="bls",
        run=run,
        artifact=artifact,
        fetched_at=fetched_at,
        records=tuple(records),
    )


def _validate_bea_pio_run(
    run: IngestionRun,
    *,
    lock: bool = False,
) -> InflationRunEvidence:
    if (
        run.source.key != INFLATION_BEA_SOURCE
        or run.dataset != INFLATION_BEA_DATASET
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        or run.completed_at is None
        or run.completed_at < run.started_at
        or _current_open_license(run.source, lock=lock) is None
    ):
        raise ValueError("inflation requires a complete OPEN-licensed BEA PIO run")
    metadata = dict(run.metadata or {})
    if metadata.get("provider") != INFLATION_BEA_SOURCE:
        raise ValueError("inflation BEA PIO provider identity is invalid")
    artifact, payload, fetched_at, metadata = _load_artifact(
        run,
        expected_content_type=EVIDENCE_BUNDLE_CONTENT_TYPE,
        lock=lock,
    )
    evidence = parse_evidence_bundle(
        payload,
        expected_provider=INFLATION_BEA_SOURCE,
        expected_dataset=INFLATION_BEA_DATASET,
    )
    expected_roles = {"release-page", "summary-workbook", "section2-workbook"}
    if (
        set(evidence.responses) != expected_roles
        or metadata.get("evidence_bundle_schema") != evidence.manifest["schema_version"]
        or metadata.get("evidence_roles") != sorted(expected_roles)
        or int(metadata.get("response_count") or 0) != len(expected_roles)
        or int(metadata.get("unique_blob_count") or 0) != len(evidence.manifest["blobs"])
    ):
        raise ValueError("inflation BEA PIO evidence roles or schema drifted")
    records, replay_metadata = BEAPIOReleaseProvider.replay_evidence_bundle(payload)
    if any(metadata.get(key) != value for key, value in replay_metadata.items()):
        raise ValueError("inflation BEA PIO metadata does not replay from evidence")
    _validate_observation_batch(
        records,
        run=run,
        source=run.source,
        fetched_at=fetched_at,
        frequency="monthly",
        lock=lock,
    )
    replay_series = {str(record.get("series_id") or "").lower() for record in records}
    if replay_series != INFLATION_BEA_ALL_SERIES:
        raise ValueError("inflation BEA PIO evidence lacks the exact nine-series bundle")
    return InflationRunEvidence(
        role="bea-pio",
        run=run,
        artifact=artifact,
        fetched_at=fetched_at,
        records=tuple(records),
    )


def _validate_inflation_runs(
    bls_run: IngestionRun,
    bea_pio_run: IngestionRun,
    *,
    lock: bool = False,
) -> InflationEvidence:
    # BLS and PIO publish on independent clocks. No shared refresh-cycle witness
    # is required or inferred here.
    return InflationEvidence(
        bls=_validate_bls_run(bls_run, lock=lock),
        bea_pio=_validate_bea_pio_run(bea_pio_run, lock=lock),
    )


def _licence_scope(source_key: str, licences: dict[str, SourceLicense]) -> str:
    if source_key == "internal":
        return "Original calculation from attributed BLS and BEA public inputs"
    licence = licences.get(source_key)
    if licence is None:
        raise ValueError("inflation payload references an unlicensed source")
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
    enriched["license_scope"] = _licence_scope(
        str(enriched.get("source_key") or ""), licences
    )
    return enriched


def _component_observations(
    evidence: InflationEvidence,
) -> tuple[list[Observation], list[Observation]]:
    bls = list(
        Observation.objects.filter(
            source=evidence.bls.run.source,
            batch_id=evidence.bls.run.batch_id,
            series__key__in=INFLATION_BLS_REQUIRED_SERIES,
        )
        .select_related("series", "source")
        .order_by("series__key", "value_date", "pk")
    )
    bea = list(
        Observation.objects.filter(
            source=evidence.bea_pio.run.source,
            batch_id=evidence.bea_pio.run.batch_id,
            series__key__in=INFLATION_BEA_REQUIRED_SERIES,
        )
        .select_related("series", "source")
        .order_by("series__key", "value_date", "pk")
    )
    if not bls or not bea:
        raise ValueError("inflation component observations are unavailable")
    return bls, bea


def _build_inflation_payload(
    evidence: InflationEvidence,
    *,
    publication_batch_id: uuid.UUID,
) -> tuple[dict[str, Any], datetime, str]:
    from .official_data import _inflation_page_data

    licences = {
        source.key: licence
        for source in (evidence.bls.run.source, evidence.bea_pio.run.source)
        if (licence := _current_open_license(source)) is not None
    }
    if set(licences) != {INFLATION_BLS_SOURCE, INFLATION_BEA_SOURCE}:
        raise ValueError("inflation licences changed during publication")
    metrics, charts, sections = _inflation_page_data(
        batch_id=evidence.bls.run.batch_id,
        bea_pio_batch_id=evidence.bea_pio.run.batch_id,
        apply_freshness=False,
        include_market_overlay=False,
    )
    if (
        {item.get("key") for item in metrics} != INFLATION_REQUIRED_METRIC_KEYS
        or {item.get("key") for item in charts} != INFLATION_REQUIRED_CHART_KEYS
        or {item.get("key") for item in sections} != INFLATION_REQUIRED_SECTION_KEYS
    ):
        raise ValueError("inflation page builder did not produce the exact base contract")
    metrics = [_enrich_metric(item, licences=licences) for item in metrics]
    for chart in charts:
        source_keys = list(chart.get("source_keys") or [])
        chart["license_scopes"] = [
            f"{key}: {_licence_scope(key, licences)}"
            for key in source_keys
            if key != "internal"
        ]
        chart["fallback_sources"] = []

    bls_observations, bea_observations = _component_observations(evidence)
    all_observations = [*bls_observations, *bea_observations]
    latest_by_series: dict[str, Observation] = {}
    for observation in all_observations:
        current = latest_by_series.get(observation.series.key)
        if current is None or observation.value_date > current.value_date:
            latest_by_series[observation.series.key] = observation
    latest = list(latest_by_series.values())
    section_fresh_until = min(
        str(item["fresh_until"])
        for item in [*metrics, *charts]
        if item.get("fresh_until")
    )
    for section in sections:
        section.update(
            {
                "source_keys": [INFLATION_BLS_SOURCE, INFLATION_BEA_SOURCE, "internal"],
                "license_scopes": [
                    f"{key}: {_licence_scope(key, licences)}"
                    for key in (INFLATION_BLS_SOURCE, INFLATION_BEA_SOURCE)
                ],
                "fallback_sources": [],
                "as_of": min(item.as_of for item in latest).isoformat(),
                "fetched_at": max(item.fetched_at for item in latest).isoformat(),
                "fresh_until": section_fresh_until,
                "quality_status": _quality(
                    [{"quality_status": item.quality_status} for item in latest]
                ),
                "batch_ids": sorted(
                    {str(evidence.bls.run.batch_id), str(evidence.bea_pio.run.batch_id)}
                ),
            }
        )

    role_series = {
        "headline-cpi": ["cusr0000sa0", "cuur0000sa0"],
        "core-cpi": ["cusr0000sa0l1e", "cuur0000sa0l1e"],
        "shelter": ["cusr0000sah1", "cuur0000sah1"],
        "core-goods": ["cusr0000sacl1e", "cuur0000sacl1e"],
        "services-less-energy": ["cusr0000sasle", "cuur0000sasle"],
        "final-demand-ppi": ["wpsfd4", "wpufd4"],
        "pce": ["bea-pce-price-index"],
        "core-pce": ["bea-core-pce-price-index"],
    }
    component_roles = {}
    for role, series_keys in role_series.items():
        is_bea = role in {"pce", "core-pce"}
        run = evidence.bea_pio.run if is_bea else evidence.bls.run
        component_roles[role] = {
            "source_key": INFLATION_BEA_SOURCE if is_bea else INFLATION_BLS_SOURCE,
            "ingestion_run_id": run.pk,
            "batch_id": str(run.batch_id),
            "series_keys": series_keys,
        }

    as_of_values = [
        parsed
        for item in [*metrics, *charts]
        if (parsed := _parse_datetime(item.get("as_of"))) is not None
    ]
    if not as_of_values:
        raise ValueError("inflation payload has no valid observation timestamp")
    data = {
        "demo": False,
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": sections,
        "component_batches": sorted(
            {str(evidence.bls.run.batch_id), str(evidence.bea_pio.run.batch_id)}
        ),
        "source_keys": [INFLATION_BLS_SOURCE, INFLATION_BEA_SOURCE, "internal"],
        "required_notices": public_source_notices(
            [INFLATION_BLS_SOURCE, INFLATION_BEA_SOURCE]
        ),
        "fresh_until": min(
            str(item["fresh_until"])
            for item in [*metrics, *charts, *sections]
            if item.get("fresh_until")
        ),
        "publication_batch_id": str(publication_batch_id),
        "contract_version": INFLATION_CONTRACT_VERSION,
        "formula_version": INFLATION_FORMULA_VERSION,
        "required_metric_keys": sorted(INFLATION_REQUIRED_METRIC_KEYS),
        "required_chart_keys": sorted(INFLATION_REQUIRED_CHART_KEYS),
        "required_section_keys": sorted(INFLATION_REQUIRED_SECTION_KEYS),
        "input_runs": [_run_reference(evidence.bls), _run_reference(evidence.bea_pio)],
        "component_roles": component_roles,
        "fallback_state": "none",
        "fallback_source": None,
        "semantic_boundary": (
            "基础合同仅复述 BLS CPI/PPI 与 BEA PIO 当前 release vintage，并按精确"
            "自然月计算 MoM、YoY、3M 和 6M；Treasury BEI 是 route 动态叠加，"
            "完整跨 release vintage 仍为 NEEDS_SOURCE。"
        ),
    }
    fingerprint_payload = deepcopy(data)
    fingerprint_payload.pop("publication_batch_id")
    data["fingerprint"] = hashlib.sha256(
        _canonical(
            {"title": INFLATION_TITLE, "summary": INFLATION_SUMMARY, "data": fingerprint_payload}
        ).encode()
    ).hexdigest()
    data["payload_integrity_hash"] = hashlib.sha256(
        _canonical({"title": INFLATION_TITLE, "summary": INFLATION_SUMMARY, "data": data}).encode()
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
        raise ValueError("inflation metric lacks exact input lineage")
    return {
        **metadata,
        "dashboard_key": "inflation",
        "metric_key": metric["key"],
        "source_keys": list(metric.get("source_keys") or []),
        "component_batch_id": ",".join(input_batch_ids),
        "input_batch_ids": input_batch_ids,
        "input_lineage": input_lineage,
        "input_metadata": metadata,
        "contract_version": INFLATION_CONTRACT_VERSION,
        "formula_version": INFLATION_FORMULA_VERSION,
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
        f"inflation-{key}" for key in INFLATION_REQUIRED_METRIC_KEYS
    }:
        return False
    for row in rows:
        metric = metrics[row.key.removeprefix("inflation-")]
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


def _inflation_snapshot_static_replay(
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
            snapshot.key != "inflation"
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or snapshot.title != INFLATION_TITLE
            or snapshot.summary != INFLATION_SUMMARY
            or set(audited) != INFLATION_PAYLOAD_KEYS
            or audited.get("contract_version") != INFLATION_CONTRACT_VERSION
            or audited.get("formula_version") != INFLATION_FORMULA_VERSION
            or audited.get("demo") is not False
            or audited.get("fallback_state") != "none"
            or audited.get("fallback_source") is not None
            or audited.get("publication_batch_id") != str(snapshot.batch_id)
            or set(audited.get("required_metric_keys") or [])
            != INFLATION_REQUIRED_METRIC_KEYS
            or set(audited.get("required_chart_keys") or [])
            != INFLATION_REQUIRED_CHART_KEYS
            or set(audited.get("required_section_keys") or [])
            != INFLATION_REQUIRED_SECTION_KEYS
            or not _exact_keyed_items(metrics, INFLATION_REQUIRED_METRIC_KEYS)
            or not _exact_keyed_items(charts, INFLATION_REQUIRED_CHART_KEYS)
            or not _exact_keyed_items(sections, INFLATION_REQUIRED_SECTION_KEYS)
            or not isinstance(component_roles, dict)
            or len(component_roles) != len(INFLATION_COMPONENT_ROLE_KEYS)
            or set(component_roles) != INFLATION_COMPONENT_ROLE_KEYS
            or not all(isinstance(item, dict) for item in component_roles.values())
        ):
            return None
        witnesses = audited.get("input_runs")
        if not isinstance(witnesses, list) or len(witnesses) != 2:
            return None
        by_role = {
            str(item.get("role") or ""): item
            for item in witnesses
            if isinstance(item, dict)
        }
        if set(by_role) != {"bls", "bea-pio"}:
            return None
        runs = {
            role: IngestionRun.objects.filter(pk=witness.get("ingestion_run_id"))
            .select_related("source")
            .first()
            for role, witness in by_role.items()
        }
        if any(run is None for run in runs.values()):
            return None
        evidence = _validate_inflation_runs(runs["bls"], runs["bea-pio"])
        if (
            _run_reference(evidence.bls) != by_role["bls"]
            or _run_reference(evidence.bea_pio) != by_role["bea-pio"]
        ):
            return None
        expected, expected_as_of, expected_quality = _build_inflation_payload(
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
        source__key=INFLATION_BLS_SOURCE,
        dataset=INFLATION_BLS_DATASET,
    )
    bea_query = IngestionRun.objects.filter(
        source__key=INFLATION_BEA_SOURCE,
        dataset=INFLATION_BEA_DATASET,
    )
    if lock:
        bls_query = bls_query.select_for_update(of=("self",))
        bea_query = bea_query.select_for_update(of=("self",))
    bls = bls_query.select_related("source").order_by("-started_at", "-id").first()
    bea = bea_query.select_related("source").order_by("-started_at", "-id").first()
    return {"bls": bls, "bea-pio": bea} if bls is not None and bea is not None else None


def _validated_failure_marker(
    marker: Any,
    *,
    replay: InflationEvidence,
) -> dict[str, IngestionRun]:
    reason_code = marker.get("reason_code") if isinstance(marker, dict) else None
    if reason_code not in {
        "latest-attempt-incomplete",
        "publication-postcondition",
    }:
        raise ValueError("inflation retained-failure marker is missing")
    attempts = marker.get("attempts")
    checked_at = _parse_datetime(marker.get("checked_at"))
    replay_runs = {"bls": replay.bls.run, "bea-pio": replay.bea_pio.run}
    if not isinstance(attempts, dict) or set(attempts) != {"bls", "bea-pio"}:
        raise ValueError("inflation retained-failure marker is invalid")
    marker_runs: dict[str, IngestionRun] = {}
    for role, reference in attempts.items():
        try:
            run_id = int(reference.get("ingestion_run_id"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("inflation retained-failure run identity is invalid") from exc
        run = IngestionRun.objects.filter(pk=run_id).select_related("source").first()
        if (
            run is None
            or _attempt_reference(run) != reference
            or (
                role == "bls"
                and (
                    run.source.key != INFLATION_BLS_SOURCE
                    or not _is_inflation_bls_dataset(run.dataset)
                )
            )
            or (
                role == "bea-pio"
                and (
                    run.source.key != INFLATION_BEA_SOURCE
                    or run.dataset != INFLATION_BEA_DATASET
                )
            )
        ):
            raise ValueError("inflation retained-failure run witness is invalid")
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
        raise ValueError("inflation retained-failure marker is invalid")
    return marker_runs


def _attempt_not_after(left: IngestionRun, right: IngestionRun) -> bool:
    return (left.started_at, left.pk) <= (right.started_at, right.pk)


def _validate_transition_marker(
    marker: Any,
    *,
    latest: dict[str, IngestionRun],
    replay: InflationEvidence,
) -> None:
    if marker is None:
        return
    marker_runs = _validated_failure_marker(marker, replay=replay)
    if any(not _attempt_not_after(marker_runs[role], latest[role]) for role in latest):
        raise ValueError("inflation transition marker is ahead of latest attempts")


def _inflation_public_state(
    snapshot: DashboardSnapshot,
    replay: SimpleNamespace,
) -> str:
    latest = _latest_attempts()
    if latest is None:
        raise ValueError("inflation latest attempts are missing")
    replay_runs = {
        "bls": replay.evidence.bls.run,
        "bea-pio": replay.evidence.bea_pio.run,
    }
    marker = (snapshot.data or {}).get("refresh_failure")
    if all(latest[role].pk == replay_runs[role].pk for role in replay_runs):
        if marker is not None:
            raise ValueError("inflation current snapshot carries a failure marker")
        deadline = _parse_datetime(replay.expected_data.get("fresh_until"))
        if deadline is None:
            raise ValueError("inflation freshness deadline is invalid")
        return "natural_expiry" if timezone.now() > deadline else "current_candidate"
    if any(latest[role].started_at < replay_runs[role].started_at for role in replay_runs):
        raise ValueError("inflation attempt chronology regressed")
    statuses = {run.status for run in latest.values()}
    if statuses & {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}:
        if any(
            run.status == IngestionRun.Status.RUNNING
            and run.started_at < timezone.now() - INFLATION_RUNNING_TIMEOUT
            for run in latest.values()
        ):
            raise ValueError("inflation mixed failure transition timed out")
        if marker is not None:
            marker_runs = _validated_failure_marker(marker, replay=replay.evidence)
            if (
                marker.get("reason_code") == "latest-attempt-incomplete"
                and all(marker_runs[role].pk == latest[role].pk for role in latest)
            ):
                return "retained_failure"
            if any(not _attempt_not_after(marker_runs[role], latest[role]) for role in latest):
                raise ValueError("inflation failure marker chronology regressed")
        newest = max(run.completed_at or run.started_at for run in latest.values())
        if newest < timezone.now() - INFLATION_RUNNING_TIMEOUT:
            raise ValueError("inflation uncoordinated failure transition timed out")
        return "transition_pending"
    if IngestionRun.Status.RUNNING in statuses:
        _validate_transition_marker(marker, latest=latest, replay=replay.evidence)
        if any(
            run.status == IngestionRun.Status.RUNNING
            and run.started_at < timezone.now() - INFLATION_RUNNING_TIMEOUT
            for run in latest.values()
        ):
            raise ValueError("inflation running transition timed out")
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
                raise ValueError("inflation success marker chronology regressed")
        newest = max(run.completed_at or run.started_at for run in latest.values())
        if newest < timezone.now() - INFLATION_RUNNING_TIMEOUT:
            raise ValueError("inflation unpublished success transition timed out")
        return "transition_pending"
    raise ValueError("inflation latest attempts have unsupported states")


def inflation_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    replay = _inflation_snapshot_static_replay(snapshot)
    if replay is None:
        return False
    try:
        state = _inflation_public_state(snapshot, replay)
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
    snapshot.inflation_publication_state = state
    return True


def select_public_inflation_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    queryset = candidates
    if queryset is None:
        queryset = (
            DashboardSnapshot.objects.filter(
                key="inflation",
                is_published=True,
                source__key="internal",
                data__contract_version=INFLATION_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")
        )
    for candidate in queryset:
        if inflation_snapshot_is_publicly_displayable(candidate):
            presented = deepcopy(candidate)
            presented.data = deepcopy(candidate.data or {})
            presented.inflation_publication_state = getattr(
                candidate, "inflation_publication_state", None
            )
            if presented.inflation_publication_state != "retained_failure":
                presented.data.pop("refresh_failure", None)
            return presented
    return None


def _store_metric_rows(snapshot: DashboardSnapshot) -> None:
    for metric in snapshot.data["metrics"]:
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if value_date is None or as_of is None or fetched_at is None:
            raise ValueError("inflation metric timestamp is invalid")
        MetricSnapshot.objects.create(
            key=f"inflation-{metric['key']}",
            label=metric["label"],
            value=Decimal(str(metric["value"])),
            display_value=metric["display_value"],
            change=(
                Decimal(str(metric["change"])) if metric.get("change") is not None else None
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
    return pair if set(pair) == {"bls", "bea-pio"} else None


def publish_inflation_revision(
    *,
    bls_run: IngestionRun,
    bea_pio_run: IngestionRun,
    publication_batch_id: uuid.UUID | None = None,
) -> DashboardSnapshot | None:
    internal = ensure_source("internal")
    with transaction.atomic():
        requested_run_ids = {bls_run.pk, bea_pio_run.pk}
        run_sources = list(
            IngestionRun.objects.filter(pk__in=requested_run_ids).values_list(
                "pk", "source_id"
            )
        )
        if {run_id for run_id, _source_id in run_sources} != requested_run_ids:
            raise ValueError("inflation publisher input run is missing")
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
            "bea-pio": locked_by_id[bea_pio_run.pk],
        }
        latest = _latest_attempts(lock=True)
        if latest is None or any(latest[role].pk != locked[role].pk for role in locked):
            raise ValueError("inflation publisher requires both exact latest attempts")
        evidence = _validate_inflation_runs(
            locked["bls"], locked["bea-pio"], lock=True
        )
        target_pair = {role: run.pk for role, run in locked.items()}
        existing = [
            candidate
            for candidate in (
                DashboardSnapshot.objects.select_for_update(of=("self",))
                .filter(
                    key="inflation",
                    is_published=True,
                    data__contract_version=INFLATION_CONTRACT_VERSION,
                )
                .select_related("source")
                .order_by("-created_at", "-id")
            )
            if _published_input_run_pair(candidate) == target_pair
        ]
        if len(existing) > 1:
            raise ValueError("inflation input pair has multiple publication revisions")
        if existing:
            if _inflation_snapshot_static_replay(existing[0]) is None:
                raise ValueError("inflation existing revision is not replayable")
            if (existing[0].data or {}).get("refresh_failure") is not None:
                raise ValueError("inflation current revision carries a failure marker")
            return None
        batch_id = publication_batch_id or uuid.uuid4()
        data, as_of, quality = _build_inflation_payload(
            evidence,
            publication_batch_id=batch_id,
        )
        snapshot = DashboardSnapshot.objects.create(
            key="inflation",
            title=INFLATION_TITLE,
            summary=INFLATION_SUMMARY,
            as_of=as_of,
            batch_id=batch_id,
            quality_status=quality,
            data=data,
            source=internal,
            is_published=True,
        )
        _store_metric_rows(snapshot)
        if _inflation_snapshot_static_replay(snapshot) is None:
            raise InflationPublicationPostconditionError(
                "inflation publication postcondition failed"
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
            "最新 BLS 或 BEA PIO 刷新未形成两个独立节奏下均可重放的完整批次；"
            "页面保留上一版已审计基础快照，不发布半成品。"
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
            raise ValueError("inflation attempts changed after publication rollback")
        candidates = (
            DashboardSnapshot.objects.select_for_update(of=("self",))
            .filter(
                key="inflation",
                is_published=True,
                data__contract_version=INFLATION_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")
        )
        for candidate in candidates:
            if _published_input_run_pair(candidate) == target_pair:
                continue
            try:
                replay = _inflation_snapshot_static_replay(candidate)
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
                    "最新完整 BLS/BEA PIO 成功批次未通过 current_candidate 发布后置校验；"
                    f"本次 revision 已回滚并保留上一版：{error}"
                ),
            )
    if previous is None:
        return False
    selected = select_public_inflation_snapshot()
    if (
        selected is None
        or getattr(selected, "inflation_publication_state", None)
        != "retained_failure"
    ):
        raise ValueError("inflation publication-postcondition marker did not replay")
    return True


def coordinate_inflation_dashboard(
    runs: Iterable[IngestionRun] | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    _ = runs
    latest = _latest_attempts()
    if latest is None:
        return [], {"inflation"}
    if all(
        run.status == IngestionRun.Status.SUCCESS and run.row_count > 0
        for run in latest.values()
    ):
        target_pair = {role: run.pk for role, run in latest.items()}
        selected = select_public_inflation_snapshot()
        if (
            selected is not None
            and getattr(selected, "inflation_publication_state", None)
            == "natural_expiry"
            and _published_input_run_pair(selected) == target_pair
        ):
            return [], {"inflation"}
        try:
            with transaction.atomic():
                published = publish_inflation_revision(
                    bls_run=latest["bls"],
                    bea_pio_run=latest["bea-pio"],
                )
                selected = select_public_inflation_snapshot()
                state = getattr(selected, "inflation_publication_state", None)
                if (
                    published is None
                    and state == "natural_expiry"
                    and selected is not None
                    and _published_input_run_pair(selected) == target_pair
                ):
                    return [], {"inflation"}
                if (
                    selected is None
                    or state != "current_candidate"
                ):
                    raise InflationPublicationPostconditionError(
                        "inflation publication is not the current replayable revision"
                    )
                return ([published] if published is not None else []), set()
        except Exception as exc:
            if _retain_publication_postcondition(latest, exc):
                return [], {"inflation"}
            raise
    if any(run.status == IngestionRun.Status.RUNNING for run in latest.values()):
        return [], {"inflation"}
    if not any(
        run.status in {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}
        for run in latest.values()
    ):
        raise ValueError("inflation latest attempts have unsupported terminal states")
    previous = None
    with transaction.atomic():
        locked_latest = _latest_attempts(lock=True)
        if locked_latest is None or any(
            locked_latest[role].pk != latest[role].pk for role in latest
        ):
            raise ValueError("inflation attempts changed during failure coordination")
        previous = next(
            (
                candidate
                for candidate in DashboardSnapshot.objects.select_for_update(
                    of=("self",)
                )
                .filter(
                    key="inflation",
                    is_published=True,
                    data__contract_version=INFLATION_CONTRACT_VERSION,
                )
                .exclude(source__key="demo-market")
                .select_related("source")
                .order_by("-created_at", "-id")
                if _inflation_snapshot_static_replay(candidate) is not None
            ),
            None,
        )
        if previous is not None:
            _mark_retained_failure(previous, locked_latest)
    if previous is not None and select_public_inflation_snapshot() is None:
        raise ValueError("inflation retained failure marker did not replay")
    return [], {"inflation"}
