"""Strict, replayable publication contracts for official macro child pages."""

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

from .macro_releases import BEAGDPReleaseProvider
from .models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    ReleaseVintageObservation,
    Source,
    SourceLicense,
)
from .raw_evidence import EVIDENCE_BUNDLE_CONTENT_TYPE, parse_evidence_bundle
from .services import ensure_source, public_source_notices

GDP_CONTRACT_VERSION = 2
GDP_FORMULA_VERSION = "bea-release-evidence-v2"
GDP_SOURCE_KEY = "bea-release"
GDP_DATASET = "gdp-release-workbooks"
GDP_TITLE = "GDP 与增长"
GDP_SUMMARY = (
    "实际 GDP、GDI、PCE、分项增速与贡献均来自 BEA 官方发布工作簿；"
    "当前指标取同一可重放采集批次的最新季度，独立 vintage 数据层"
    "同时保留 Advance、Second、Third 与后续 Revised 修订路径。"
)
GDP_RUNNING_TIMEOUT = timedelta(hours=2)

GDP_METRIC_DEFINITIONS = (
    ("bea-a191rl", "实际 GDP 增速", 2, "%"),
    ("bea-dpcerl", "实际 PCE 增速", 2, "%"),
    ("bea-gdp-nominal-saar", "名义 GDP", 1, " USD bn"),
    ("bea-gdi-real-growth-saar", "实际 GDI 增速", 2, "%"),
    ("bea-pce-goods-growth", "商品消费增速", 2, "%"),
    ("bea-pce-services-growth", "服务消费增速", 2, "%"),
    ("bea-gpdi-growth", "私人国内投资增速", 2, "%"),
    ("bea-pce-contribution", "消费贡献", 2, "pp"),
    ("bea-gpdi-contribution", "投资贡献", 2, "pp"),
    ("bea-net-exports-contribution", "净出口贡献", 2, "pp"),
    ("bea-government-contribution", "政府贡献", 2, "pp"),
)
GDP_REQUIRED_METRIC_KEYS = frozenset(item[0] for item in GDP_METRIC_DEFINITIONS)
GDP_REQUIRED_CHART_KEYS = frozenset(
    {"gdp-growth-history", "gdp-vintage-trail"}
)
GDP_REQUIRED_SECTION_KEYS = frozenset({"gdp-vintage-ledger"})
GDP_PAYLOAD_KEYS = frozenset(
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
        "input_run",
        "fallback_state",
        "fallback_source",
        "semantic_boundary",
        "fingerprint",
        "payload_integrity_hash",
    }
)


class GDPPublicationPostconditionError(ValueError):
    """A completed GDP run could not become the current public revision."""


@dataclass(frozen=True)
class GDPReleaseEvidence:
    run: IngestionRun
    artifact: RawArtifact
    fetched_at: datetime
    records: tuple[dict[str, Any], ...]
    vintages: tuple[dict[str, Any], ...]
    latest_period: date
    latest_release_date: date


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
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
    require_storage: bool = False,
) -> SourceLicense | None:
    today = timezone.localdate()
    query = SourceLicense.objects.filter(
        source=source,
        is_current=True,
        status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
        public_display_allowed=True,
        derived_display_allowed=True,
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=today),
        Q(valid_until__isnull=True) | Q(valid_until__gte=today),
    )
    if require_storage:
        query = query.filter(historical_storage_allowed=True)
    if lock:
        query = query.select_for_update(of=("self",))
    return query.order_by("-created_at", "-pk").first()


def _quarter_label(period: date) -> str:
    return f"{period.year}Q{((period.month - 1) // 3) + 1}"


def _validate_gdp_semantic_alignment(
    records: Iterable[dict[str, Any]],
    vintages: Iterable[dict[str, Any]],
    metadata: dict[str, Any],
) -> date:
    """Prove that the two BEA workbooks describe one current release."""

    record_latest: dict[str, date] = {}
    for record in records:
        key = str(record.get("series_id") or "").lower()
        try:
            period = date.fromisoformat(str(record.get("date") or ""))
        except ValueError as exc:
            raise ValueError("GDP observation period is invalid") from exc
        record_latest[key] = max(period, record_latest.get(key, period))

    real_gdp_vintages: list[tuple[date, date, str]] = []
    for record in vintages:
        if str(record.get("series_id") or "").lower() != "bea-a191rl":
            continue
        try:
            value_period = date.fromisoformat(str(record.get("date") or ""))
            release_date = date.fromisoformat(
                str(record.get("release_date") or "")
            )
        except ValueError as exc:
            raise ValueError("GDP release-vintage identity is invalid") from exc
        estimate_round = str(record.get("estimate_round") or "").strip()
        if not estimate_round:
            raise ValueError("GDP release-vintage estimate round is missing")
        real_gdp_vintages.append((value_period, release_date, estimate_round))
    if not real_gdp_vintages:
        raise ValueError("GDP release-vintage evidence lacks real GDP")

    latest_period = max(item[0] for item in real_gdp_vintages)
    latest_period_entries = [
        item for item in real_gdp_vintages if item[0] == latest_period
    ]
    latest_release_date = max(item[1] for item in latest_period_entries)
    latest_rounds = {
        item[2]
        for item in latest_period_entries
        if item[1] == latest_release_date
    }
    if len(latest_rounds) != 1:
        raise ValueError("GDP latest vintage estimate round is ambiguous")
    latest_round = next(iter(latest_rounds))
    expected_comparison = {
        "comparison_quarter": _quarter_label(latest_period),
        "comparison_release_date": latest_release_date.isoformat(),
        "comparison_estimate_round": latest_round,
    }
    if any(metadata.get(key) != value for key, value in expected_comparison.items()):
        raise ValueError(
            "GDP comparison workbook does not match the latest vintage release"
        )

    required_latest = {
        key: period
        for key, period in record_latest.items()
        if key in GDP_REQUIRED_METRIC_KEYS
    }
    if (
        set(required_latest) != GDP_REQUIRED_METRIC_KEYS
        or set(required_latest.values()) != {latest_period}
    ):
        raise ValueError("GDP headline metrics do not share the current release quarter")
    return latest_period


def _fresh_until(observation: Observation) -> datetime:
    metadata = observation.metadata or {}
    release_date = metadata.get("source_release_time") or metadata.get(
        "source_revision_date"
    )
    release_days = metadata.get("release_freshness_days")
    if release_date and release_days:
        released_at = _parse_datetime(release_date)
        if released_at is not None:
            try:
                return released_at + timedelta(days=int(release_days))
            except (TypeError, ValueError, OverflowError):
                pass
    value_date = observation.value_date
    if observation.series.frequency == "quarterly":
        month = ((value_date.month - 1) // 3 + 1) * 3
        day = calendar.monthrange(value_date.year, month)[1]
        period_end = value_date.replace(month=month, day=day)
        return period_end + timedelta(days=120)
    if observation.series.frequency == "monthly":
        day = calendar.monthrange(value_date.year, value_date.month)[1]
        return value_date.replace(day=day) + timedelta(days=45)
    if observation.series.frequency == "annual":
        return value_date.replace(month=12, day=31) + timedelta(days=400)
    return value_date + timedelta(days=4)


def _record_contract(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("series_id") or "").lower(),
        str(record.get("date") or ""),
        f"{Decimal(str(record.get('value'))):.8f}",
        _canonical(record.get("metadata") or {}),
    )


def _vintage_contract(
    record: dict[str, Any],
) -> tuple[str, str, str, str, str, str, str]:
    return (
        str(record.get("series_id") or "").lower(),
        str(record.get("date") or ""),
        str(record.get("release_date") or ""),
        str(record.get("estimate_round") or ""),
        str(record.get("vintage_label") or ""),
        f"{Decimal(str(record.get('value'))):.8f}",
        _canonical(record.get("metadata") or {}),
    )


def _run_reference(evidence: GDPReleaseEvidence) -> dict[str, Any]:
    run = evidence.run
    return {
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
        "latest_release_date": evidence.latest_release_date.isoformat(),
    }


def _validate_gdp_release_run(
    run: IngestionRun,
    *,
    lock: bool = False,
) -> GDPReleaseEvidence:
    if (
        run.source.key != GDP_SOURCE_KEY
        or run.dataset != GDP_DATASET
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        or run.started_at is None
        or run.completed_at is None
        or run.completed_at < run.started_at
    ):
        raise ValueError("GDP requires one successful exact BEA release run")
    if _current_license(
        run.source,
        lock=lock,
        require_storage=True,
    ) is None:
        raise ValueError("GDP source lacks current public and storage permission")

    metadata = dict(run.metadata or {})
    fetched_at = _parse_datetime(metadata.get("fetched_at"))
    if (
        metadata.get("provider") != GDP_SOURCE_KEY
        or fetched_at is None
        or fetched_at > run.completed_at + timedelta(minutes=5)
        or fetched_at > timezone.now() + timedelta(minutes=5)
    ):
        raise ValueError("GDP run fetch timeline or provider identity is invalid")
    artifacts_query = RawArtifact.objects.filter(run=run).order_by("pk")
    if lock:
        artifacts_query = artifacts_query.select_for_update()
    artifacts = list(artifacts_query)
    if len(artifacts) != 1:
        raise ValueError("GDP run requires exactly one private evidence bundle")
    artifact = artifacts[0]
    digest = str(metadata.get("sha256") or "").lower()
    try:
        declared_size = int(metadata.get("byte_length"))
    except (TypeError, ValueError) as exc:
        raise ValueError("GDP evidence byte length metadata is invalid") from exc
    expected_uri = f"private://{GDP_SOURCE_KEY}/{digest[:2]}/{digest}.bin"
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
        raise ValueError("GDP evidence artifact database witness is invalid")
    try:
        payload = _artifact_path(digest).read_bytes()
    except OSError as exc:
        raise ValueError("GDP private evidence bytes are unavailable") from exc
    if len(payload) != declared_size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("GDP private evidence bytes are missing or tampered")

    bundle = parse_evidence_bundle(
        payload,
        expected_provider=GDP_SOURCE_KEY,
        expected_dataset=GDP_DATASET,
    )
    roles = {"release-page", "vintage-workbook", "comparison-workbook"}
    if (
        set(bundle.responses) != roles
        or metadata.get("evidence_bundle_schema")
        != bundle.manifest.get("schema_version")
        or metadata.get("evidence_roles") != sorted(roles)
        or metadata.get("response_count") != len(roles)
        or metadata.get("unique_blob_count")
        != len(bundle.manifest.get("blobs") or [])
    ):
        raise ValueError("GDP evidence bundle manifest no longer matches the run")
    records, supplemental, replay_metadata = (
        BEAGDPReleaseProvider.replay_evidence_bundle(payload)
    )
    if any(metadata.get(key) != value for key, value in replay_metadata.items()):
        raise ValueError("GDP run metadata no longer replays from exact evidence")
    vintages = list(supplemental.get("release_vintages") or [])
    if not records or not vintages:
        raise ValueError("GDP evidence lacks normalized rows or release vintages")
    latest_period = _validate_gdp_semantic_alignment(records, vintages, metadata)

    expected_latest: dict[str, date] = {}
    for record in records:
        try:
            period = date.fromisoformat(str(record.get("date") or ""))
            Decimal(str(record.get("value")))
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ValueError("GDP replayed observation is malformed") from exc
        key = str(record.get("series_id") or "").lower()
        expected_latest[key] = max(period, expected_latest.get(key, period))
    latest_release_date = max(
        date.fromisoformat(str(record.get("release_date") or ""))
        for record in vintages
    )
    if metadata.get("latest_value_dates") != {
        key: period.isoformat() for key, period in sorted(expected_latest.items())
    } or metadata.get("latest_release_date") != latest_release_date.isoformat():
        raise ValueError("GDP durable release watermark no longer matches evidence")

    observation_base = Observation.objects.filter(batch_id=run.batch_id)
    vintage_base = ReleaseVintageObservation.objects.filter(batch_id=run.batch_id)
    if lock:
        # Lock only the base tables. Nullable ``series``/``fallback_source``
        # joins cannot be targets of PostgreSQL FOR UPDATE.
        list(
            observation_base.select_for_update()
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        list(
            vintage_base.select_for_update()
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    observation_query = observation_base.select_related(
        "series", "source", "fallback_source"
    ).order_by("series__key", "value_date", "pk")
    vintage_query = vintage_base.select_related(
        "series", "source", "fallback_source"
    ).order_by("series__key", "value_date", "release_date", "estimate_round")
    observations = list(observation_query)
    stored_vintages = list(vintage_query)
    expected_observations = sorted(_record_contract(record) for record in records)
    actual_observations = sorted(
        (
            item.series.key,
            item.value_date.date().isoformat(),
            f"{item.value:.8f}",
            _canonical(item.metadata or {}),
        )
        for item in observations
        if item.instrument_id is None
        and item.series.source_id == run.source_id
        and item.source_id == run.source_id
        and item.series.frequency == "quarterly"
        and item.as_of == item.value_date
        and item.fetched_at == fetched_at
        and item.fallback_source_id is None
        and item.quality_status == Observation.Quality.FRESH
    )
    expected_vintages = sorted(_vintage_contract(record) for record in vintages)
    actual_vintages = sorted(
        (
            item.series.key,
            item.value_date.date().isoformat(),
            item.release_date.isoformat(),
            item.estimate_round,
            item.vintage_label,
            f"{item.value:.8f}",
            _canonical(item.metadata or {}),
        )
        for item in stored_vintages
        if item.series.source_id == run.source_id
        and item.source_id == run.source_id
        and item.series.frequency == "quarterly"
        and item.as_of.date() == item.release_date
        and item.fetched_at == fetched_at
        and item.fallback_source_id is None
        and item.quality_status == Observation.Quality.FRESH
        and item.license_scope == run.source.license_scope[:240]
    )
    if (
        len(observations) != len(records)
        or len(stored_vintages) != len(vintages)
        or expected_observations != actual_observations
        or expected_vintages != actual_vintages
        or run.row_count != len(records) + len(vintages)
    ):
        raise ValueError("GDP normalized database rows do not match exact evidence")
    return GDPReleaseEvidence(
        run=run,
        artifact=artifact,
        fetched_at=fetched_at,
        records=tuple(records),
        vintages=tuple(vintages),
        latest_period=latest_period,
        latest_release_date=latest_release_date,
    )


def _lineage(observation: Observation) -> dict[str, Any]:
    return {
        "series_key": observation.series.key,
        "source_key": observation.source.key,
        "source_name": observation.source.name,
        "value_date": observation.value_date.isoformat(),
        "as_of": observation.as_of.isoformat(),
        "fetched_at": observation.fetched_at.isoformat(),
        "batch_id": str(observation.batch_id),
        "quality_status": observation.quality_status,
        "license_scope": observation.source.license_scope,
        "fallback_source": None,
    }


def _metric(
    rows: list[Observation],
    *,
    key: str,
    label: str,
    decimals: int,
    suffix: str,
    license_scope: str,
) -> dict[str, Any]:
    current = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    value = current.value
    change = value - previous.value if previous is not None else None
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": f"{value:,.{decimals}f}{suffix}",
        "change": round(float(change), decimals) if change is not None else None,
        "change_unit": "pp" if suffix == "%" else suffix,
        "unit": suffix,
        "quality_status": current.quality_status,
        "source": current.source.name,
        "source_key": current.source.key,
        "source_keys": [current.source.key],
        "fallback_source": None,
        "license_scope": license_scope,
        "as_of": current.as_of.isoformat(),
        "value_date": current.value_date.isoformat(),
        "fetched_at": current.fetched_at.isoformat(),
        "fresh_until": _fresh_until(current).isoformat(),
        "batch_id": str(current.batch_id),
        "metadata": deepcopy(current.metadata or {}),
    }


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


def _build_gdp_payload(
    evidence: GDPReleaseEvidence,
    *,
    publication_batch_id: uuid.UUID,
) -> tuple[dict[str, Any], datetime, str]:
    source = evidence.run.source
    licence = _current_license(source, require_storage=True)
    if licence is None:
        raise ValueError("GDP source licence is no longer publicly displayable")
    observations = list(
        Observation.objects.filter(
            source=source,
            batch_id=evidence.run.batch_id,
            series__key__in=GDP_REQUIRED_METRIC_KEYS,
        )
        .select_related("series", "source", "fallback_source")
        .order_by("series__key", "value_date", "pk")
    )
    by_series: dict[str, list[Observation]] = {}
    for observation in observations:
        by_series.setdefault(observation.series.key, []).append(observation)
    if set(by_series) != GDP_REQUIRED_METRIC_KEYS:
        raise ValueError("GDP exact metric series coverage is incomplete")
    latest_metric_dates = {rows[-1].value_date.date() for rows in by_series.values()}
    if latest_metric_dates != {evidence.latest_period}:
        raise ValueError("GDP headline metrics do not share the current release quarter")
    metrics = [
        _metric(
            by_series[key],
            key=key,
            label=label,
            decimals=decimals,
            suffix=suffix,
            license_scope=licence.scope,
        )
        for key, label, decimals, suffix in GDP_METRIC_DEFINITIONS
    ]

    growth_dates = sorted(
        {
            item.value_date
            for item in by_series["bea-a191rl"]
        }
        & {item.value_date for item in by_series["bea-dpcerl"]}
    )[-24:]
    if not growth_dates:
        raise ValueError("GDP growth chart has no common GDP/PCE periods")
    gdp_by_date = {item.value_date: item for item in by_series["bea-a191rl"]}
    pce_by_date = {item.value_date: item for item in by_series["bea-dpcerl"]}
    growth_rows = []
    for period in growth_dates:
        gdp = gdp_by_date[period]
        pce = pce_by_date[period]
        growth_rows.append(
            {
                "date": period.date().isoformat(),
                "实际 GDP": float(gdp.value),
                "实际 PCE": float(pce.value),
                "_source_keys": [source.key],
                "_lineage": {
                    "实际 GDP": _lineage(gdp),
                    "实际 PCE": _lineage(pce),
                },
            }
        )
    latest_growth = [gdp_by_date[growth_dates[-1]], pce_by_date[growth_dates[-1]]]
    growth_chart = {
        "key": "gdp-growth-history",
        "title": "实际 GDP 与实际 PCE 增速",
        "description": "季调年化环比，单位：%。",
        "kind": "line",
        "data": growth_rows,
        "source_keys": [source.key],
        "license_scopes": [f"{source.name}: {licence.scope}"],
        "fallback_sources": [],
        "as_of": min(item.as_of for item in latest_growth).isoformat(),
        "fetched_at": max(item.fetched_at for item in latest_growth).isoformat(),
        "fresh_until": min(_fresh_until(item) for item in latest_growth).isoformat(),
        "quality_status": _quality(
            [{"quality_status": item.quality_status} for item in latest_growth]
        ),
        "batch_ids": [str(evidence.run.batch_id)],
        "frequency": "quarterly",
    }

    vintages = list(
        ReleaseVintageObservation.objects.filter(
            source=source,
            batch_id=evidence.run.batch_id,
            series__key="bea-a191rl",
        )
        .select_related("series", "source", "fallback_source")
        .order_by("value_date", "release_date", "pk")
    )
    periods: dict[datetime, list[ReleaseVintageObservation]] = {}
    for item in vintages:
        periods.setdefault(item.value_date, []).append(item)
    if not periods:
        raise ValueError("GDP release-vintage coverage is empty")
    latest_period = max(periods)
    latest_entries = periods[latest_period]
    current = gdp_by_date.get(latest_period)
    if current is None:
        raise ValueError("GDP latest vintage has no normalized current observation")

    def vintage_lineage(item: ReleaseVintageObservation) -> dict[str, Any]:
        return {
            "series_key": item.series.key,
            "source_key": item.source.key,
            "source_name": item.source.name,
            "value_date": item.value_date.isoformat(),
            "as_of": item.as_of.isoformat(),
            "release_date": item.release_date.isoformat(),
            "estimate_round": item.estimate_round,
            "fetched_at": item.fetched_at.isoformat(),
            "batch_id": str(item.batch_id),
            "quality_status": item.quality_status,
            "license_scope": item.license_scope,
            "fallback_source": None,
        }

    quarter = f"{latest_period.year}Q{((latest_period.month - 1) // 3) + 1}"
    vintage_chart = {
        "key": "gdp-vintage-trail",
        "title": f"{quarter} 实际 GDP 估算修订",
        "description": "按 BEA 官方发布日期展示每轮季调年化环比估算，单位：%。",
        "kind": "line",
        "panel_class": "lg:col-span-2",
        "data": [
            {
                "date": f"{item.vintage_label}\n{item.release_date:%m-%d}",
                "实际 GDP": float(item.value),
                "_source_keys": [source.key],
                "_lineage": {"实际 GDP": vintage_lineage(item)},
            }
            for item in latest_entries
        ],
        "source_keys": [source.key],
        "license_scopes": [f"{source.name}: {licence.scope}"],
        "fallback_sources": [],
        "as_of": latest_entries[-1].as_of.isoformat(),
        "fetched_at": max(item.fetched_at for item in latest_entries).isoformat(),
        "fresh_until": _fresh_until(current).isoformat(),
        "quality_status": current.quality_status,
        "batch_ids": [str(evidence.run.batch_id)],
        "frequency": "quarterly",
    }
    section_rows = []
    for period in sorted(periods, reverse=True)[:8]:
        entries = periods[period]
        first, latest = entries[0], entries[-1]
        revision = latest.value - first.value
        section_rows.append(
            {
                "label": f"{period.year}Q{((period.month - 1) // 3) + 1}",
                "display_value": " → ".join(f"{item.value:.2f}%" for item in entries),
                "status": (
                    " → ".join(item.vintage_label for item in entries)
                    + f"；累计修订 {revision:+.2f}pp"
                ),
                "description": " · ".join(
                    f"{item.vintage_label} {item.release_date.isoformat()}"
                    for item in entries
                ),
                "source": source.name,
                "source_key": source.key,
                "source_keys": [source.key],
                "as_of": latest.as_of.isoformat(),
                "fetched_at": latest.fetched_at.isoformat(),
                "quality_status": latest.quality_status,
                "license_scope": licence.scope,
                "fallback_source": None,
                "batch_id": str(latest.batch_id),
            }
        )
    vintage_section = {
        "key": "gdp-vintage-ledger",
        "title": "GDP 发布轮次与修订路径",
        "description": (
            f"当前官方工作簿保留 {len(vintages):,} 条实际 GDP 发布记录；"
            "表格展示最近 8 个观察季度，箭头严格按发布日期排序。"
        ),
        "rows": section_rows,
        "status": current.quality_status,
        "full_width": True,
        "source_key": source.key,
        "source_keys": [source.key],
        "as_of": latest_entries[-1].as_of.isoformat(),
        "fetched_at": latest_entries[-1].fetched_at.isoformat(),
        "fresh_until": _fresh_until(current).isoformat(),
        "quality_status": current.quality_status,
        "batch_id": str(evidence.run.batch_id),
    }
    charts = [growth_chart, vintage_chart]
    sections = [vintage_section]
    items = [*metrics, *charts, *sections]
    as_of = min(
        value
        for item in [*metrics, *charts]
        if (value := _parse_datetime(item.get("as_of"))) is not None
    )
    snapshot_data = {
        "demo": False,
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": sections,
        "component_batches": [str(evidence.run.batch_id)],
        "source_keys": [source.key],
        "required_notices": public_source_notices([source.key]),
        "fresh_until": min(
            str(item["fresh_until"])
            for item in items
            if item.get("fresh_until")
        ),
        "publication_batch_id": str(publication_batch_id),
        "contract_version": GDP_CONTRACT_VERSION,
        "formula_version": GDP_FORMULA_VERSION,
        "required_metric_keys": sorted(GDP_REQUIRED_METRIC_KEYS),
        "required_chart_keys": sorted(GDP_REQUIRED_CHART_KEYS),
        "required_section_keys": sorted(GDP_REQUIRED_SECTION_KEYS),
        "input_run": _run_reference(evidence),
        "fallback_state": "none",
        "fallback_source": None,
        "semantic_boundary": (
            "数值与修订轮次仅复述所引 BEA 工作簿；不是预测、实时 GDP、"
            "衰退判定或交易信号。"
        ),
    }
    fingerprint_payload = deepcopy(snapshot_data)
    fingerprint_payload.pop("publication_batch_id")
    snapshot_data["fingerprint"] = hashlib.sha256(
        _canonical(
            {
                "title": GDP_TITLE,
                "summary": GDP_SUMMARY,
                "data": fingerprint_payload,
            }
        ).encode()
    ).hexdigest()
    snapshot_data["payload_integrity_hash"] = hashlib.sha256(
        _canonical(
            {
                "title": GDP_TITLE,
                "summary": GDP_SUMMARY,
                "data": snapshot_data,
            }
        ).encode()
    ).hexdigest()
    return snapshot_data, as_of, _quality([*metrics, *charts])


def _metric_metadata(metric: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    input_batch_id = str(metric.get("batch_id") or "")
    input_lineage = [
        {
            "series_key": metric["key"],
            "source_key": metric["source_key"],
            "value_date": metric["value_date"],
            "as_of": metric["as_of"],
            "fetched_at": metric["fetched_at"],
            "batch_id": input_batch_id,
            "quality_status": metric["quality_status"],
            "fallback_source": metric.get("fallback_source"),
            "license_scope": metric.get("license_scope"),
        }
    ]
    return {
        "dashboard_key": "gdp",
        "metric_key": metric["key"],
        "source_keys": list(metric.get("source_keys") or []),
        "component_batch_id": input_batch_id,
        "input_batch_id": input_batch_id,
        "input_batch_ids": [input_batch_id],
        "input_lineage": input_lineage,
        "input_metadata": deepcopy(metric.get("metadata") or {}),
        "contract_version": GDP_CONTRACT_VERSION,
        "formula_version": GDP_FORMULA_VERSION,
        "fingerprint": data["fingerprint"],
        "payload_integrity_hash": data["payload_integrity_hash"],
    }


def _metric_rows_match(snapshot: DashboardSnapshot, data: dict[str, Any]) -> bool:
    rows = list(
        MetricSnapshot.objects.filter(
            batch_id=snapshot.batch_id,
        )
        .select_related("source", "fallback_source")
        .order_by("key")
    )
    metrics = {item["key"]: item for item in data["metrics"]}
    if {row.key for row in rows} != {f"gdp-{key}" for key in metrics}:
        return False
    for row in rows:
        metric = metrics[row.key.removeprefix("gdp-")]
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if (
            value_date is None
            or as_of is None
            or fetched_at is None
            or row.label != metric["label"]
            or row.value != Decimal(str(metric["value"]))
            or row.display_value != metric["display_value"]
            or row.change
            != (
                Decimal(str(metric["change"]))
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


def _gdp_snapshot_static_replay(
    snapshot: DashboardSnapshot,
) -> SimpleNamespace | None:
    try:
        data = dict(snapshot.data or {})
        audited_data = dict(data)
        audited_data.pop("refresh_failure", None)
        metrics = audited_data.get("metrics")
        charts = audited_data.get("charts")
        sections = audited_data.get("sections")
        if (
            snapshot.key != "gdp"
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or snapshot.title != GDP_TITLE
            or snapshot.summary != GDP_SUMMARY
            or set(audited_data) != GDP_PAYLOAD_KEYS
            or audited_data.get("contract_version") != GDP_CONTRACT_VERSION
            or audited_data.get("formula_version") != GDP_FORMULA_VERSION
            or audited_data.get("demo") is not False
            or audited_data.get("fallback_state") != "none"
            or audited_data.get("fallback_source") is not None
            or audited_data.get("publication_batch_id") != str(snapshot.batch_id)
            or set(audited_data.get("required_metric_keys") or [])
            != GDP_REQUIRED_METRIC_KEYS
            or set(audited_data.get("required_chart_keys") or [])
            != GDP_REQUIRED_CHART_KEYS
            or set(audited_data.get("required_section_keys") or [])
            != GDP_REQUIRED_SECTION_KEYS
            or not _exact_keyed_items(metrics, GDP_REQUIRED_METRIC_KEYS)
            or not _exact_keyed_items(charts, GDP_REQUIRED_CHART_KEYS)
            or not _exact_keyed_items(sections, GDP_REQUIRED_SECTION_KEYS)
        ):
            return None
        witness = audited_data.get("input_run")
        if not isinstance(witness, dict):
            return None
        run = (
            IngestionRun.objects.filter(pk=witness.get("ingestion_run_id"))
            .select_related("source")
            .first()
        )
        if run is None:
            return None
        evidence = _validate_gdp_release_run(run)
        if _run_reference(evidence) != witness:
            return None
        expected, expected_as_of, expected_quality = _build_gdp_payload(
            evidence,
            publication_batch_id=snapshot.batch_id,
        )
        if (
            audited_data != expected
            or snapshot.as_of != expected_as_of
            or not _metric_rows_match(snapshot, expected)
        ):
            return None
        return SimpleNamespace(
            data=data,
            run=run,
            expected_data=expected,
            expected_quality=expected_quality,
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


def replay_gdp_snapshot(snapshot: DashboardSnapshot) -> SimpleNamespace | None:
    """Replay one immutable GDP revision without consulting live acquisition state."""

    return _gdp_snapshot_static_replay(snapshot)


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
    }


def _latest_attempt(*, lock: bool = False) -> IngestionRun | None:
    query = IngestionRun.objects.filter(
        source__key=GDP_SOURCE_KEY,
        dataset=GDP_DATASET,
    ).select_related("source")
    if lock:
        query = query.select_for_update(of=("self",))
    return query.order_by("-started_at", "-id").first()


def _previous_attempt(
    attempt: IngestionRun,
    *,
    lock: bool = False,
) -> IngestionRun | None:
    query = IngestionRun.objects.filter(
        source__key=GDP_SOURCE_KEY,
        dataset=GDP_DATASET,
    ).filter(
        Q(started_at__lt=attempt.started_at)
        | Q(started_at=attempt.started_at, id__lt=attempt.id)
    )
    if lock:
        query = query.select_for_update(of=("self",))
    return query.select_related("source").order_by("-started_at", "-id").first()


def _validated_failure_marker(
    marker: Any,
    *,
    replay_run: IngestionRun,
) -> IngestionRun:
    if not isinstance(marker, dict):
        raise ValueError("GDP retained-failure marker is missing or invalid")
    reason_code = marker.get("reason_code")
    if reason_code not in {
        "latest-attempt-incomplete",
        "publication-postcondition",
    }:
        raise ValueError("GDP retained-failure reason is invalid")
    attempt_reference = marker.get("attempt")
    try:
        attempt_id = int((attempt_reference or {}).get("ingestion_run_id"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("GDP retained-failure attempt identity is invalid") from exc
    attempt = (
        IngestionRun.objects.filter(
            pk=attempt_id,
            source__key=GDP_SOURCE_KEY,
            dataset=GDP_DATASET,
        )
        .select_related("source")
        .first()
    )
    checked_at = _parse_datetime(marker.get("checked_at"))
    if (
        attempt is None
        or attempt_reference != _attempt_reference(attempt)
        or checked_at is None
        or attempt.started_at <= replay_run.started_at
        or (attempt.completed_at is not None and checked_at < attempt.completed_at)
        or checked_at > timezone.now() + timedelta(minutes=5)
        or (
            reason_code == "latest-attempt-incomplete"
            and attempt.status
            not in {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}
        )
        or (
            reason_code == "publication-postcondition"
            and attempt.status != IngestionRun.Status.SUCCESS
        )
    ):
        raise ValueError("GDP retained-failure marker is missing or invalid")
    return attempt


def _gdp_public_state(snapshot: DashboardSnapshot, replay: SimpleNamespace) -> str:
    latest = _latest_attempt()
    if latest is None:
        raise ValueError("GDP latest attempt is missing")
    marker = (snapshot.data or {}).get("refresh_failure")
    if latest.pk == replay.run.pk:
        if marker is not None:
            raise ValueError("GDP current snapshot carries an obsolete failure marker")
        fresh_until = _parse_datetime(replay.expected_data.get("fresh_until"))
        if fresh_until is None:
            raise ValueError("GDP snapshot freshness deadline is invalid")
        return "natural_expiry" if timezone.now() > fresh_until else "current_candidate"
    if latest.started_at < replay.run.started_at:
        raise ValueError("GDP latest attempt chronology regressed")
    if latest.status == IngestionRun.Status.RUNNING:
        if latest.started_at < timezone.now() - GDP_RUNNING_TIMEOUT:
            raise ValueError("GDP running attempt exceeded its transition timeout")
        previous = _previous_attempt(latest)
        if marker is None:
            if previous is None or previous.pk != replay.run.pk:
                raise ValueError("GDP transition does not follow this published revision")
        else:
            marker_attempt = _validated_failure_marker(
                marker,
                replay_run=replay.run,
            )
            if previous is None or marker_attempt.pk != previous.pk:
                raise ValueError("GDP transition failure marker is not the prior attempt")
        return "transition_pending"
    if latest.status in {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL}:
        marker_attempt = _validated_failure_marker(marker, replay_run=replay.run)
        if (
            marker.get("reason_code") != "latest-attempt-incomplete"
            or marker_attempt.pk != latest.pk
        ):
            raise ValueError("GDP retained-failure marker is not the latest attempt")
        return "retained_failure"
    if latest.status == IngestionRun.Status.SUCCESS:
        if marker is not None:
            marker_attempt = _validated_failure_marker(marker, replay_run=replay.run)
            if (
                marker.get("reason_code") == "publication-postcondition"
                and marker_attempt.pk == latest.pk
            ):
                return "retained_failure"
            if (marker_attempt.started_at, marker_attempt.pk) >= (
                latest.started_at,
                latest.pk,
            ):
                raise ValueError("GDP success marker chronology regressed")
        completed_at = latest.completed_at or latest.started_at
        if completed_at < timezone.now() - GDP_RUNNING_TIMEOUT:
            raise ValueError("GDP unpublished success transition timed out")
        return "transition_pending"
    raise ValueError("GDP latest attempt has an unsupported state")


def gdp_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    replay = _gdp_snapshot_static_replay(snapshot)
    if replay is None:
        return False
    try:
        state = _gdp_public_state(snapshot, replay)
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
    snapshot.gdp_publication_state = state
    return True


def select_public_gdp_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    queryset = candidates
    if queryset is None:
        queryset = (
            DashboardSnapshot.objects.filter(
                key="gdp",
                is_published=True,
                data__contract_version=GDP_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")[:50]
        )
    for candidate in queryset:
        if gdp_snapshot_is_publicly_displayable(candidate):
            presented = deepcopy(candidate)
            presented.data = deepcopy(candidate.data or {})
            presented.gdp_publication_state = getattr(
                candidate,
                "gdp_publication_state",
                None,
            )
            if presented.gdp_publication_state != "retained_failure":
                presented.data.pop("refresh_failure", None)
            return presented
    return None


def _store_metric_rows(snapshot: DashboardSnapshot) -> None:
    for metric in snapshot.data["metrics"]:
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if value_date is None or as_of is None or fetched_at is None:
            raise ValueError("GDP metric timestamp is invalid")
        source = Source.objects.get(key=metric["source_key"])
        MetricSnapshot.objects.create(
            key=f"gdp-{metric['key']}",
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
            source=source,
            fallback_source=None,
            quality_status=metric["quality_status"],
            license_scope=str(metric["license_scope"])[:120],
            metadata=_metric_metadata(metric, snapshot.data),
        )


def publish_gdp_revision(
    *,
    run: IngestionRun,
    publication_batch_id: uuid.UUID | None = None,
) -> DashboardSnapshot | None:
    """Append one GDP revision rebuilt only from the supplied immutable run."""

    internal = ensure_source("internal")
    with transaction.atomic():
        source_id = IngestionRun.objects.filter(pk=run.pk).values_list(
            "source_id", flat=True
        ).first()
        if source_id is None:
            raise ValueError("GDP publisher input run is missing")
        list(
            Source.objects.select_for_update()
            .filter(pk__in={internal.pk, source_id})
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        locked_run = IngestionRun.objects.select_for_update(of=("self",)).get(
            pk=run.pk
        )
        latest = _latest_attempt(lock=True)
        if latest is None or latest.pk != locked_run.pk:
            raise ValueError("GDP publisher requires the latest acquisition attempt")
        evidence = _validate_gdp_release_run(locked_run, lock=True)
        existing = list(
            DashboardSnapshot.objects.select_for_update(of=("self",))
            .filter(
                key="gdp",
                is_published=True,
                data__contract_version=GDP_CONTRACT_VERSION,
                data__input_run__ingestion_run_id=locked_run.pk,
            )
            .select_related("source")
            .order_by("-created_at", "-id")[:2]
        )
        if len(existing) > 1:
            raise ValueError("GDP run already has multiple publication revisions")
        if existing:
            if _gdp_snapshot_static_replay(existing[0]) is None:
                raise ValueError("GDP existing revision for this run is not replayable")
            if (existing[0].data or {}).get("refresh_failure") is not None:
                raise ValueError("GDP existing current revision has a failure marker")
            return None
        batch_id = publication_batch_id or uuid.uuid4()
        data, as_of, quality = _build_gdp_payload(
            evidence,
            publication_batch_id=batch_id,
        )
        snapshot = DashboardSnapshot.objects.create(
            key="gdp",
            title=GDP_TITLE,
            summary=GDP_SUMMARY,
            as_of=as_of,
            batch_id=batch_id,
            quality_status=quality,
            data=data,
            source=internal,
            is_published=True,
        )
        _store_metric_rows(snapshot)
        if _gdp_snapshot_static_replay(snapshot) is None:
            raise GDPPublicationPostconditionError(
                "GDP publication postcondition failed"
            )
        return snapshot


def _mark_retained_failure(
    snapshot: DashboardSnapshot,
    attempt: IngestionRun,
    *,
    reason_code: str = "latest-attempt-incomplete",
    reason: str | None = None,
) -> None:
    data = deepcopy(snapshot.data or {})
    data["refresh_failure"] = {
        "reason_code": reason_code,
        "checked_at": (attempt.completed_at or timezone.now()).isoformat(),
        "reason": reason
        or (
            "最新 BEA GDP 刷新未产生可重放的完整批次；页面保留上一版"
            "已审计快照，不发布半成品数据。"
        ),
        "attempt": _attempt_reference(attempt),
    }
    snapshot.data = data
    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])


def _retain_publication_postcondition(
    latest: IngestionRun,
    error: GDPPublicationPostconditionError,
) -> bool:
    previous = None
    with transaction.atomic():
        locked_latest = _latest_attempt(lock=True)
        if locked_latest is None or locked_latest.pk != latest.pk:
            raise ValueError("GDP attempt changed after publication rollback")
        previous = next(
            (
                candidate
                for candidate in DashboardSnapshot.objects.select_for_update(
                    of=("self",)
                )
                .filter(
                    key="gdp",
                    is_published=True,
                    data__contract_version=GDP_CONTRACT_VERSION,
                )
                .exclude(source__key="demo-market")
                .select_related("source")
                .order_by("-created_at", "-id")
                if _gdp_snapshot_static_replay(candidate) is not None
                and (candidate.data or {})["input_run"]["ingestion_run_id"]
                != latest.pk
            ),
            None,
        )
        if previous is not None:
            _mark_retained_failure(
                previous,
                locked_latest,
                reason_code="publication-postcondition",
                reason=(
                    "最新完整 BEA GDP 成功批次未通过 current_candidate 发布后置校验；"
                    f"本次 revision 已回滚并保留上一版：{error}"
                ),
            )
    if previous is None:
        return False
    selected = select_public_gdp_snapshot()
    if (
        selected is None
        or getattr(selected, "gdp_publication_state", None) != "retained_failure"
    ):
        raise ValueError("GDP publication-postcondition marker did not replay")
    return True


def coordinate_gdp_dashboard(
    runs: Iterable[IngestionRun] | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    """Publish a complete GDP child or retain the last replayable revision."""

    _ = runs  # Database latest-attempt truth wins over an in-memory run list.
    latest = _latest_attempt()
    if latest is None:
        return [], {"gdp"}
    if latest.status == IngestionRun.Status.SUCCESS and latest.row_count > 0:
        selected = select_public_gdp_snapshot()
        if (
            selected is not None
            and getattr(selected, "gdp_publication_state", None)
            == "natural_expiry"
            and (selected.data or {})["input_run"]["ingestion_run_id"]
            == latest.pk
        ):
            return [], {"gdp"}
        try:
            with transaction.atomic():
                published = publish_gdp_revision(run=latest)
                selected = select_public_gdp_snapshot()
                state = getattr(selected, "gdp_publication_state", None)
                if (
                    published is None
                    and state == "natural_expiry"
                    and selected is not None
                    and (selected.data or {})["input_run"]["ingestion_run_id"]
                    == latest.pk
                ):
                    return [], {"gdp"}
                if (
                    selected is None
                    or state != "current_candidate"
                ):
                    raise GDPPublicationPostconditionError(
                        "GDP successful publication is not current_candidate"
                    )
                return ([published] if published is not None else []), set()
        except GDPPublicationPostconditionError as exc:
            if _retain_publication_postcondition(latest, exc):
                return [], {"gdp"}
            raise
    if latest.status == IngestionRun.Status.RUNNING:
        # Another worker owns the transition. Keep the prior immutable
        # revision unmarked; the selector exposes transition_pending.
        return [], {"gdp"}
    if latest.status not in {
        IngestionRun.Status.FAILED,
        IngestionRun.Status.PARTIAL,
    }:
        raise ValueError("GDP latest attempt has an unsupported terminal state")
    with transaction.atomic():
        locked_latest = _latest_attempt(lock=True)
        if locked_latest is None or locked_latest.pk != latest.pk:
            raise ValueError("GDP latest attempt changed during failure coordination")
        previous = next(
            (
                candidate
                for candidate in DashboardSnapshot.objects.select_for_update(
                    of=("self",)
                )
                .filter(
                    key="gdp",
                    is_published=True,
                    data__contract_version=GDP_CONTRACT_VERSION,
                )
                .exclude(source__key="demo-market")
                .select_related("source")
                .order_by("-created_at", "-id")[:50]
                if _gdp_snapshot_static_replay(candidate) is not None
            ),
            None,
        )
        if previous is not None:
            _mark_retained_failure(previous, locked_latest)
    if previous is not None:
        if select_public_gdp_snapshot() is None:
            raise ValueError("GDP retained-failure marker did not replay")
    return [], {"gdp"}
