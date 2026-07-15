"""Strict, replayable Credit Official v1 publication contract.

The module deliberately keeps credit publication separate from the generic
dashboard builder.  Treasury HQM and Federal Reserve SLOOS are independently
validated children; the overview only copies already-audited child payloads.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .credit_official import FederalReserveSLOOSProvider, TreasuryHQMProvider
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
from .services import (
    ensure_source,
    persist_private_raw_artifact,
    public_source_notices,
    store_series_observations,
)

CREDIT_CONTRACT_VERSION = 1
CREDIT_FORMULA_VERSION = "treasury-hqm-fed-sloos-v1"
CREDIT_PUBLICATION_KEYS = frozenset({"credit", "credit-spreads", "credit-stress"})

HQM_SOURCE = "us-treasury-hqm"
HQM_DATASET = "monthly-average-par-yields"
SLOOS_SOURCE = "federal-reserve-sloos"
SLOOS_DATASET = "quarterly-series"

HQM_REQUIRED_SERIES = (
    "HQM-PAR-2Y",
    "HQM-PAR-5Y",
    "HQM-PAR-10Y",
    "HQM-PAR-30Y",
)
HQM_METRIC_BY_SERIES = {
    "HQM-PAR-2Y": "hqm-par-2y",
    "HQM-PAR-5Y": "hqm-par-5y",
    "HQM-PAR-10Y": "hqm-par-10y",
    "HQM-PAR-30Y": "hqm-par-30y",
}
HQM_LABELS = {
    "HQM-PAR-2Y": "HQM 2Y Par Yield",
    "HQM-PAR-5Y": "HQM 5Y Par Yield",
    "HQM-PAR-10Y": "HQM 10Y Par Yield",
    "HQM-PAR-30Y": "HQM 30Y Par Yield",
}

SLOOS_REQUIRED_SERIES = (
    "SUBLPDMBS_XWB_N.Q",
    "SUBLPDMBD_XWB_N.Q",
    "SUBLPDMHS_XWB_N.Q",
    "SUBLPDMHD_XWB_N.Q",
    "SUBLPDCILS_N.Q",
    "SUBLPDCISS_N.Q",
)
SLOOS_METRIC_BY_SERIES = {
    "SUBLPDMBS_XWB_N.Q": "sloos-business-standards-weighted",
    "SUBLPDMBD_XWB_N.Q": "sloos-business-demand-weighted",
    "SUBLPDMHS_XWB_N.Q": "sloos-household-standards-weighted",
    "SUBLPDMHD_XWB_N.Q": "sloos-household-demand-weighted",
    "SUBLPDCILS_N.Q": "sloos-ci-large-standards",
    "SUBLPDCISS_N.Q": "sloos-ci-small-standards",
}
SLOOS_LABELS = {
    "SUBLPDMBS_XWB_N.Q": "Business Lending Standards (weighted)",
    "SUBLPDMBD_XWB_N.Q": "Business Loan Demand (weighted)",
    "SUBLPDMHS_XWB_N.Q": "Household Lending Standards (weighted)",
    "SUBLPDMHD_XWB_N.Q": "Household Loan Demand (weighted)",
    "SUBLPDCILS_N.Q": "Large / Middle-Market C&I Standards",
    "SUBLPDCISS_N.Q": "Small-Firm C&I Standards",
}
SLOOS_POSITIVE_MEANING = {
    "SUBLPDMBS_XWB_N.Q": "Positive means net tightening",
    "SUBLPDMBD_XWB_N.Q": "Positive means net stronger demand",
    "SUBLPDMHS_XWB_N.Q": "Positive means net tightening",
    "SUBLPDMHD_XWB_N.Q": "Positive means net stronger demand",
    "SUBLPDCILS_N.Q": "Positive means net tightening",
    "SUBLPDCISS_N.Q": "Positive means net tightening",
}

CREDIT_SPREAD_METRICS = frozenset(HQM_METRIC_BY_SERIES.values())
CREDIT_SPREAD_CHARTS = frozenset(
    {"hqm-latest-par-yield-curve", "hqm-par-yield-history"}
)
CREDIT_SPREAD_SECTIONS = (
    "recent-hqm-observations",
    "hqm-source-freshness-methodology",
    "licensed-spread-data-gaps",
)
CREDIT_STRESS_METRICS = frozenset(SLOOS_METRIC_BY_SERIES.values())
CREDIT_STRESS_CHARTS = frozenset(
    {"sloos-lending-standards-history", "sloos-loan-demand-history"}
)
CREDIT_STRESS_SECTIONS = (
    "latest-sloos-survey-table",
    "sloos-source-freshness-methodology",
    "licensed-credit-stress-gaps",
)
CREDIT_OVERVIEW_METRICS = frozenset(
    {
        "overview-hqm-10y",
        "overview-hqm-30y",
        "overview-sloos-business-standards",
        "overview-sloos-business-demand",
    }
)
CREDIT_OVERVIEW_CHARTS = frozenset(
    {"credit-overview-hqm-history", "credit-overview-sloos-standards-history"}
)
CREDIT_OVERVIEW_SECTIONS = (
    "credit-component-ledger",
    "credit-semantic-boundary",
    "licensed-credit-market-gaps",
)

# Rendered table schemas are part of the public v1 contract.  Keep these
# tuples centralized so builders and validators cannot silently drift apart.
CREDIT_SPREAD_SECTION_COLUMNS = {
    "recent-hqm-observations": (
        "date",
        "hqm-2y",
        "hqm-5y",
        "hqm-10y",
        "hqm-30y",
        "quality",
        "batch",
        "artifact",
    ),
    "hqm-source-freshness-methodology": (
        "source-dataset",
        "run-batch",
        "fetched",
        "latest-month",
        "fresh-until",
        "artifact-sha-size",
        "rows",
        "licence",
        "fallback",
    ),
    "licensed-spread-data-gaps": (
        "market-data",
        "status",
        "public-value",
        "provider-guidance",
    ),
}
CREDIT_STRESS_SECTION_COLUMNS = {
    "latest-sloos-survey-table": (
        "metric",
        "value",
        "previous",
        "change-pp",
        "positive-meaning",
        "value-date",
        "quality",
        "batch",
    ),
    "sloos-source-freshness-methodology": (
        "source-dataset",
        "run-batch",
        "file-prepared",
        "fetched",
        "latest-quarter",
        "fresh-until",
        "archive-sha-size",
        "member-sha-size",
        "rows",
        "licence",
        "fallback",
    ),
    "licensed-credit-stress-gaps": (
        "market-data",
        "status",
        "public-value",
        "provider-guidance",
    ),
}
CREDIT_OVERVIEW_SECTION_COLUMNS = {
    "credit-component-ledger": (
        "component",
        "snapshot-batch",
        "payload-hashes",
        "input-run-batch",
        "value-date",
        "fetched",
        "fresh-until",
        "artifact",
        "quality",
        "licence",
        "fallback",
    ),
    "credit-semantic-boundary": (
        "evidence",
        "can-state",
        "cannot-state",
        "status",
        "source",
    ),
    "licensed-credit-market-gaps": (
        "market-data",
        "status",
        "public-value",
        "provider-guidance",
    ),
}
CREDIT_SECTION_COLUMNS = {
    **CREDIT_SPREAD_SECTION_COLUMNS,
    **CREDIT_STRESS_SECTION_COLUMNS,
    **CREDIT_OVERVIEW_SECTION_COLUMNS,
}

HQM_CHANGE_FORMULA = "100 * (Y_t - Y_previous)"
SLOOS_CHANGE_FORMULA = "V_t - V_previous"
NEW_YORK = ZoneInfo("America/New_York")
FUTURE_TOLERANCE = timedelta(minutes=5)

CHILD_TITLES = {
    "credit-spreads": "Treasury HQM 企业债收益率代理",
    "credit-stress": "银行信贷压力代理",
}
CHILD_SUMMARIES = {
    "credit-spreads": (
        "U.S. Treasury HQM 月度高质量企业债 par yield 曲线；"
        "它不是国债利差、ICE BofA OAS 或 CDS 报价。"
    ),
    "credit-stress": (
        "Federal Reserve SLOOS 季度银行调查，只展示贷款标准与需求"
        "净百分比；不生成 0–100 或任何综合信用压力分数。"
    ),
}
OVERVIEW_TITLE = "美国信用市场"
OVERVIEW_SUMMARY = (
    "本页只原子组合同一 credit refresh cycle 的 Treasury HQM 与 "
    "Federal Reserve SLOOS 严格子快照，不推导 OAS、CDS 或综合压力分。"
)

REFRESH_FAILURE_REASONS = {
    "latest-attempt-incomplete": "Latest required official input attempt is not successful and complete.",
    "candidate-validation": "Latest official input failed exact replay or publication validation.",
    "candidate-regression": "Latest official input regressed behind the retained strict snapshot.",
    "cycle-incomplete": "HQM and SLOOS strict children do not belong to one complete refresh cycle.",
    "publication-postcondition": "Credit Official v1 publication postcondition failed.",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC)
        if parsed.tzinfo is None
        else parsed.astimezone(UTC)
    )


def _normalized_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_credit_timeline(
    *,
    fetched_at: datetime,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    file_prepared_at: Any = None,
) -> datetime | None:
    """Reject future or internally impossible acquisition/run chronology."""

    fetched = _normalized_datetime(fetched_at)
    started = _normalized_datetime(started_at)
    completed = _normalized_datetime(completed_at)
    now = timezone.now().astimezone(UTC)
    if fetched is None or fetched > now + FUTURE_TOLERANCE:
        raise ValueError("credit fetched_at is in the future")
    if started is not None:
        if started > now + FUTURE_TOLERANCE:
            raise ValueError("credit run started_at is in the future")
    if completed is not None:
        if completed > now + FUTURE_TOLERANCE:
            raise ValueError("credit run completed_at is in the future")
        if started is None or completed < started:
            raise ValueError("credit run completed_at chronology is invalid")
        if fetched > completed + FUTURE_TOLERANCE:
            raise ValueError("credit fetched_at is after run completion")
    if started is not None and fetched > started + FUTURE_TOLERANCE:
        raise ValueError("credit fetched_at is after run started_at")
    prepared = None
    if file_prepared_at is not None:
        prepared = _parse_datetime(file_prepared_at)
        if prepared is None:
            raise ValueError("credit file_prepared_at is invalid")
        if prepared > now + FUTURE_TOLERANCE:
            raise ValueError("credit file_prepared_at is in the future")
        if prepared > fetched:
            raise ValueError("credit file_prepared_at is after fetched_at")
    return prepared


def _fresh_until(value_date: date, days: int) -> datetime:
    """Return the exclusive NY instant after the contractual deadline date."""

    deadline = value_date + timedelta(days=days)
    return datetime.combine(deadline + timedelta(days=1), time.min, tzinfo=NEW_YORK).astimezone(
        UTC
    )


def _cell(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "cell": {"kind": "text", "value": str(value)}}


def _row_with_cells(row: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    row["cells_list"] = [_cell(key, row.get(key, "")) for key in columns]
    return row


def _artifact_path(digest: str) -> Path:
    root = Path(
        getattr(settings, "RAW_ARTIFACT_ROOT", settings.BASE_DIR / "data" / "artifacts")
    )
    return root / digest[:2] / f"{digest}.bin"


def _record_contract(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("series_id") or ""),
        str(record.get("date") or "")[:10],
        f"{Decimal(str(record.get('value'))):.8f}",
        _canonical(record.get("metadata") or {}),
    )


def persist_credit_official_result(
    result: ProviderResult, source: Source, run: IngestionRun
) -> int:
    """Atomically retain one exact file and batch-preserved normalized rows."""

    expected = {
        (HQM_SOURCE, HQM_DATASET): set(HQM_REQUIRED_SERIES),
        (SLOOS_SOURCE, SLOOS_DATASET): set(SLOOS_REQUIRED_SERIES),
    }.get((source.key, result.dataset))
    if (
        expected is None
        or result.provider != source.key
        or not isinstance(result.raw_bytes, (bytes, bytearray))
        or not result.raw_bytes
    ):
        raise ValueError("credit official result has an invalid source, dataset or raw file")
    refresh_id = str((result.metadata or {}).get("credit_refresh_id") or "").strip()
    if not refresh_id:
        raise ValueError("credit_refresh_id is required")
    _validate_credit_timeline(
        fetched_at=result.fetched_at,
        started_at=run.started_at,
        file_prepared_at=(
            (result.metadata or {}).get("file_prepared_at")
            if source.key == SLOOS_SOURCE
            else None
        ),
    )
    incoming = [_record_contract(record) for record in result.records]
    identities = {(item[0], item[1]) for item in incoming}
    if len(incoming) != len(set(incoming)) or len(incoming) != len(identities):
        raise ValueError("credit official result contains duplicate normalized rows")
    if {item[0] for item in incoming} != expected:
        raise ValueError("credit official result required series set is not exact")

    if source.key == HQM_SOURCE:
        replayed, evidence = TreasuryHQMProvider.parse_workbook_bytes(bytes(result.raw_bytes))
        declared = result.metadata.get("workbook_validation")
        if declared != evidence.get("workbook_validation"):
            raise ValueError("HQM workbook validation metadata does not match exact bytes")
    else:
        replayed, evidence = FederalReserveSLOOSProvider.parse_archive_bytes(
            bytes(result.raw_bytes), series_ids=SLOOS_REQUIRED_SERIES
        )
        for key in (
            "archive_member_name",
            "archive_member_size",
            "archive_member_sha256",
            "file_prepared_at",
            "found_series",
        ):
            if result.metadata.get(key) != evidence.get(key):
                raise ValueError(f"SLOOS {key} metadata does not match exact bytes")
    if sorted(incoming) != sorted(_record_contract(record) for record in replayed):
        raise ValueError("normalized observations do not replay from the exact raw file")

    artifact = persist_private_raw_artifact(run=run, result=result, namespace=source.key)
    run.metadata = {**dict(run.metadata or {}), **dict(result.metadata or {})}
    run.save(update_fields=["metadata", "updated_at"])
    count = store_series_observations(result, source, run, preserve_batches=True)
    if count != len(incoming) or RawArtifact.objects.filter(run=run).count() != 1:
        raise ValueError("credit official raw artifact and normalized rows were not atomic")
    if artifact.sha256 != hashlib.sha256(bytes(result.raw_bytes)).hexdigest():
        raise ValueError("credit official artifact digest mismatch")
    return count


def _effective_scope(source: Source) -> str | None:
    today = timezone.localdate()
    licence = (
        source.licenses.filter(
            is_current=True,
            status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
            public_display_allowed=True,
            derived_display_allowed=True,
            historical_storage_allowed=True,
        )
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=today))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today))
        .first()
    )
    return licence.scope if licence is not None else None


@dataclass(frozen=True)
class CreditRunEvidence:
    run: IngestionRun
    artifact: RawArtifact
    observations: dict[str, list[Observation]]
    raw_sha256: str
    raw_size: int
    fetched_at: datetime
    refresh_id: str
    fresh_until: datetime
    latest_date: date
    common_dates: tuple[date, ...]
    scopes: dict[str, str]


@dataclass(frozen=True)
class CreditLockedInputs:
    """Stable database state protected by the caller's transaction locks."""

    run_id: int
    artifact_rows: tuple[tuple[Any, ...], ...]
    observation_rows: tuple[tuple[Any, ...], ...]


def _credit_run_input_state(
    run: IngestionRun, *, lock: bool
) -> CreditLockedInputs:
    artifact_query = RawArtifact.objects.filter(run_id=run.pk).order_by("pk")
    observation_query = Observation.objects.filter(batch_id=run.batch_id).order_by(
        "series_id", "value_date", "pk"
    )
    if lock:
        if not transaction.get_connection().in_atomic_block:
            raise RuntimeError("credit input locks require an atomic transaction")
        artifact_query = artifact_query.select_for_update()
        observation_query = observation_query.select_for_update()
    artifact_rows = tuple(
        artifact_query.values_list(
            "pk", "run_id", "uri", "sha256", "content_type", "size_bytes"
        )
    )
    observation_rows = tuple(
        (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            _canonical(row[11]),
        )
        for row in observation_query.values_list(
            "pk",
            "series_id",
            "instrument_id",
            "value",
            "value_date",
            "as_of",
            "fetched_at",
            "batch_id",
            "source_id",
            "fallback_source_id",
            "quality_status",
            "metadata",
        )
    )
    return CreditLockedInputs(
        run_id=run.pk,
        artifact_rows=artifact_rows,
        observation_rows=observation_rows,
    )


def _lock_credit_run_inputs(run: IngestionRun) -> CreditLockedInputs:
    return _credit_run_input_state(run, lock=True)


def _assert_credit_run_inputs_unchanged(
    run: IngestionRun, locked: CreditLockedInputs
) -> None:
    if run.pk != locked.run_id or _credit_run_input_state(run, lock=False) != locked:
        raise ValueError("credit locked raw artifact or observation rows changed")


def _lock_credit_run_set(
    runs: dict[str, IngestionRun],
) -> dict[str, CreditLockedInputs]:
    return {key: _lock_credit_run_inputs(runs[key]) for key in sorted(runs)}


def _assert_credit_run_set_unchanged(
    runs: dict[str, IngestionRun], locked: dict[str, CreditLockedInputs]
) -> None:
    if set(runs) != set(locked):
        raise ValueError("credit locked input run set changed")
    for key in sorted(runs):
        _assert_credit_run_inputs_unchanged(runs[key], locked[key])


def _run_reference(evidence: CreditRunEvidence) -> dict[str, Any]:
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
        "credit_refresh_id": evidence.refresh_id,
        "artifact_id": evidence.artifact.pk,
        "artifact_uri": evidence.artifact.uri,
        "artifact_sha256": evidence.raw_sha256,
        "artifact_size": evidence.raw_size,
    }


def _latest_attempt(source_key: str, dataset: str, *, lock: bool = False) -> IngestionRun | None:
    query = IngestionRun.objects.select_related("source").filter(
        source__key=source_key, dataset=dataset
    )
    if lock:
        query = query.select_for_update()
    return query.order_by("-started_at", "-id").first()


def _load_run_evidence(
    run: IngestionRun,
    *,
    page_key: str,
    allow_expired: bool,
) -> CreditRunEvidence:
    if page_key == "credit-spreads":
        source_key, dataset = HQM_SOURCE, HQM_DATASET
        required = set(HQM_REQUIRED_SERIES)
        freshness_days = 62
    elif page_key == "credit-stress":
        source_key, dataset = SLOOS_SOURCE, SLOOS_DATASET
        required = set(SLOOS_REQUIRED_SERIES)
        freshness_days = 150
    else:
        raise ValueError("unknown credit child page")
    if (
        run.source.key != source_key
        or run.dataset != dataset
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
    ):
        raise ValueError("credit child requires one successful non-empty exact run")
    metadata = dict(run.metadata or {})
    refresh_id = str(metadata.get("credit_refresh_id") or "").strip()
    fetched_at = _parse_datetime(metadata.get("fetched_at"))
    if not refresh_id or fetched_at is None:
        raise ValueError("credit run identity metadata is incomplete")
    if run.started_at is None or run.completed_at is None:
        raise ValueError("credit successful run chronology is incomplete")
    _validate_credit_timeline(
        fetched_at=fetched_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        file_prepared_at=(
            metadata.get("file_prepared_at")
            if page_key == "credit-stress"
            else None
        ),
    )
    artifacts = list(RawArtifact.objects.filter(run=run).order_by("pk"))
    if len(artifacts) != 1:
        raise ValueError("credit run requires exactly one private raw artifact")
    artifact = artifacts[0]
    digest = str(metadata.get("sha256") or "").lower()
    try:
        declared_size = int(metadata.get("byte_length"))
    except (TypeError, ValueError) as exc:
        raise ValueError("credit raw byte length metadata is invalid") from exc
    expected_uri = f"private://{source_key}/{digest[:2]}/{digest}.bin"
    if (
        len(digest) != 64
        or artifact.sha256 != digest
        or artifact.size_bytes != declared_size
        or artifact.uri != expected_uri
        or artifact.content_type != str(metadata.get("content_type") or "application/octet-stream")
        or not str(metadata.get("endpoint") or "")
    ):
        raise ValueError("credit raw artifact database evidence is invalid")
    payload = _artifact_path(digest).read_bytes()
    if len(payload) != declared_size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("credit raw artifact bytes are missing or tampered")

    if page_key == "credit-spreads":
        replayed, evidence = TreasuryHQMProvider.parse_workbook_bytes(payload)
        if (
            metadata.get("file_type") != "application/vnd.ms-excel"
            or metadata.get("workbook_file_type") != "xls"
            or metadata.get("workbook_validation") != evidence.get("workbook_validation")
        ):
            raise ValueError("HQM workbook evidence changed")
    else:
        replayed, evidence = FederalReserveSLOOSProvider.parse_archive_bytes(
            payload, series_ids=SLOOS_REQUIRED_SERIES
        )
        if set(metadata.get("requested_series") or []) != required:
            raise ValueError("SLOOS requested series set is not exact")
        for key in (
            "archive_member_name",
            "archive_member_size",
            "archive_member_sha256",
            "file_prepared_at",
            "found_series",
        ):
            if metadata.get(key) != evidence.get(key):
                raise ValueError("SLOOS archive evidence changed")
    if {str(record.get("series_id")) for record in replayed} != required:
        raise ValueError("credit replayed series set is not exact")

    rows = list(
        Observation.objects.filter(batch_id=run.batch_id)
        .select_related("series", "source", "fallback_source")
        .order_by("series__key", "value_date", "pk")
    )
    if len(rows) != len(replayed) or len(rows) != run.row_count:
        raise ValueError("credit normalized row count does not match exact raw file")
    stored_contract = [
        (
            row.series.key.upper(),
            row.value_date.date().isoformat(),
            f"{row.value:.8f}",
            _canonical(row.metadata or {}),
        )
        for row in rows
        if row.series is not None
        and row.series.source_id == run.source_id
        and row.source_id == run.source_id
        and row.instrument_id is None
        and row.as_of == row.value_date
        and row.fetched_at == fetched_at
        and row.quality_status == Observation.Quality.FRESH
        and row.fallback_source_id is None
    ]
    if len(stored_contract) != len(rows) or sorted(stored_contract) != sorted(
        _record_contract(record) for record in replayed
    ):
        raise ValueError("credit normalized observations were tampered")
    by_series: dict[str, list[Observation]] = {key: [] for key in required}
    for row in rows:
        assert row.series is not None
        by_series[row.series.key.upper()].append(row)
    if set(by_series) != required or any(not values for values in by_series.values()):
        raise ValueError("credit normalized required series are missing")
    date_sets = [{item.value_date.date() for item in by_series[key]} for key in required]
    common_dates = tuple(sorted(set.intersection(*date_sets)))
    minimum = 120 if page_key == "credit-spreads" else 80
    if len(common_dates) < minimum:
        raise ValueError("credit child lacks the minimum common valid history")
    latest_date = common_dates[-1]
    if latest_date > timezone.now().astimezone(NEW_YORK).date() or latest_date > fetched_at.astimezone(
        NEW_YORK
    ).date():
        raise ValueError("credit child latest value date is in the future")
    fresh_until = _fresh_until(latest_date, freshness_days)
    if not allow_expired and fresh_until <= timezone.now():
        raise ValueError("credit child exact input is naturally expired")
    required_sources = {source_key, "internal"}
    sources = {item.key: item for item in Source.objects.filter(key__in=required_sources)}
    if set(sources) != required_sources:
        raise ValueError("credit child sources are missing")
    scopes = {key: _effective_scope(value) for key, value in sources.items()}
    if any(scope is None for scope in scopes.values()):
        raise ValueError("credit child source licence is not currently effective")
    return CreditRunEvidence(
        run=run,
        artifact=artifact,
        observations=by_series,
        raw_sha256=digest,
        raw_size=declared_size,
        fetched_at=fetched_at,
        refresh_id=refresh_id,
        fresh_until=fresh_until,
        latest_date=latest_date,
        common_dates=common_dates,
        scopes={key: str(value) for key, value in scopes.items()},
    )


def _direct_lineage(item: Observation, evidence: CreditRunEvidence) -> dict[str, Any]:
    return {
        "series_id": item.series.key.upper() if item.series else "",
        "source_key": evidence.run.source.key,
        "value": f"{item.value:.8f}",
        "unit": "%",
        "value_date": item.value_date.isoformat(),
        "fetched_at": item.fetched_at.isoformat(),
        "run_id": evidence.run.pk,
        "batch_id": str(evidence.run.batch_id),
        "artifact_id": evidence.artifact.pk,
        "artifact_uri": evidence.artifact.uri,
        "artifact_sha256": evidence.raw_sha256,
        "quality_status": Observation.Quality.FRESH,
        "license_scope": evidence.scopes[evidence.run.source.key],
        "fallback_source": None,
    }


def _observation_maps(evidence: CreditRunEvidence) -> dict[str, dict[date, Observation]]:
    return {
        key: {item.value_date.date(): item for item in values}
        for key, values in evidence.observations.items()
    }


def _metric(
    *,
    key: str,
    label: str,
    previous: Observation,
    current: Observation,
    evidence: CreditRunEvidence,
    formula: str,
    change_unit: str,
    multiplier: Decimal,
) -> dict[str, Any]:
    change = multiplier * (current.value - previous.value)
    return {
        "key": key,
        "label": label,
        "value": float(current.value),
        "display_value": f"{current.value:.2f}%",
        "change": float(change),
        "unit": "%",
        "source": evidence.run.source.name,
        "source_key": evidence.run.source.key,
        "source_keys": [evidence.run.source.key, "internal"],
        "quality_status": Observation.Quality.FRESH,
        "license_scope": evidence.scopes[evidence.run.source.key],
        "value_date": current.value_date.isoformat(),
        "as_of": current.as_of.isoformat(),
        "fetched_at": current.fetched_at.isoformat(),
        "fresh_until": evidence.fresh_until.isoformat(),
        "batch_id": str(evidence.run.batch_id),
        "fallback_source": None,
        "metadata": {
            "formula": formula,
            "formula_version": CREDIT_FORMULA_VERSION,
            "change_unit": change_unit,
            "change_quality_status": Observation.Quality.ESTIMATED,
            "calculation_owner": "Atlas Macro",
            "current_value": f"{current.value:.8f}",
            "previous_value": f"{previous.value:.8f}",
            "current_value_date": current.value_date.isoformat(),
            "previous_value_date": previous.value_date.isoformat(),
            "input_lineage": [
                _direct_lineage(previous, evidence),
                _direct_lineage(current, evidence),
            ],
            "input_run_id": evidence.run.pk,
            "input_batch_ids": [str(evidence.run.batch_id)],
            "artifact_sha256": evidence.raw_sha256,
        },
    }


def _chart_shell(
    *,
    key: str,
    title: str,
    description: str,
    data: list[dict[str, Any]],
    evidence: CreditRunEvidence,
    series_keys: tuple[str, ...],
    x_key: str = "date",
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "description": description,
        "kind": "line",
        "data": data,
        "source_keys": [evidence.run.source.key],
        "license_scopes": [
            f"{evidence.run.source.name}: {evidence.scopes[evidence.run.source.key]}"
        ],
        "batch_ids": [str(evidence.run.batch_id)],
        "as_of": datetime.combine(evidence.latest_date, time.min, tzinfo=UTC).isoformat(),
        "fetched_at": evidence.fetched_at.isoformat(),
        "fresh_until": evidence.fresh_until.isoformat(),
        "quality_status": Observation.Quality.FRESH,
        "fallback_source": None,
        "fallback_sources": [],
        "lineage_mode": "per-point",
        "time_axis": x_key,
        "x_key": x_key,
        "series_keys": list(series_keys),
    }


def _gap_section(
    *,
    key: str,
    title: str,
    rows: tuple[tuple[str, str, str], ...],
    internal_scope: str,
) -> dict[str, Any]:
    columns = CREDIT_SECTION_COLUMNS[key]
    rendered = []
    for market_data, status, guidance in rows:
        rendered.append(
            _row_with_cells(
                {
                    "market-data": market_data,
                    "status": status,
                    "public-value": "Not published",
                    "provider-guidance": guidance,
                    "value": None,
                    "source_key": "internal",
                    "source_keys": ["internal"],
                    "quality_status": Observation.Quality.ESTIMATED,
                    "license_scope": internal_scope,
                    "fallback_source": None,
                },
                columns,
            )
        )
    return {
        "key": key,
        "title": title,
        "description": "No numeric proxy is inserted for a licensed or review-gated dataset.",
        "columns": [
            {"key": column, "label": label}
            for column, label in zip(
                columns,
                ("Market data", "Status", "Public value", "Provider guidance"),
                strict=True,
            )
        ],
        "rows": rendered,
        "source_keys": ["internal"],
        "quality_status": Observation.Quality.ESTIMATED,
        "license_scope": internal_scope,
        "fallback_source": None,
    }


def _semantic_fingerprint(page_key: str, data: dict[str, Any]) -> str:
    semantic = {
        "page_key": page_key,
        "contract_version": CREDIT_CONTRACT_VERSION,
        "formula_version": CREDIT_FORMULA_VERSION,
        "metrics": [
            {
                "key": item.get("key"),
                "value": item.get("value"),
                "change": item.get("change"),
                "value_date": item.get("value_date"),
            }
            for item in data.get("metrics", [])
        ],
        "charts": [
            {
                "key": item.get("key"),
                "data": [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"lineage", "_lineage", "source_keys", "batch_id"}
                    }
                    for row in item.get("data", [])
                ],
            }
            for item in data.get("charts", [])
        ],
        "semantic_boundary": data.get("semantic_boundary"),
    }
    return _sha256_json(semantic)


def _payload_hash(data: dict[str, Any]) -> str:
    audited = {
        key: value
        for key, value in data.items()
        if key
        not in {"payload_integrity_hash", "refresh_failure", "presentation_quality"}
    }
    return _sha256_json(audited)


def _build_credit_child(
    run: IngestionRun,
    *,
    page_key: str,
    publication_batch_id: uuid.UUID,
    allow_expired: bool,
) -> tuple[dict[str, Any], CreditRunEvidence]:
    evidence = _load_run_evidence(run, page_key=page_key, allow_expired=allow_expired)
    maps = _observation_maps(evidence)
    latest, previous = evidence.common_dates[-1], evidence.common_dates[-2]
    metrics: list[dict[str, Any]] = []

    if page_key == "credit-spreads":
        for series_id in HQM_REQUIRED_SERIES:
            metrics.append(
                _metric(
                    key=HQM_METRIC_BY_SERIES[series_id],
                    label=HQM_LABELS[series_id],
                    previous=maps[series_id][previous],
                    current=maps[series_id][latest],
                    evidence=evidence,
                    formula=HQM_CHANGE_FORMULA,
                    change_unit="bp",
                    multiplier=Decimal("100"),
                )
            )
        curve_rows = []
        for series_id in HQM_REQUIRED_SERIES:
            item = maps[series_id][latest]
            curve_rows.append(
                {
                    "tenor": series_id.removeprefix("HQM-PAR-"),
                    "value_date": latest.isoformat(),
                    "HQM par yield": float(item.value),
                    "source_keys": [HQM_SOURCE],
                    "batch_id": str(run.batch_id),
                    "lineage": _direct_lineage(item, evidence),
                    "_lineage": {"HQM par yield": _direct_lineage(item, evidence)},
                }
            )
        history_rows = []
        for period in evidence.common_dates[-120:]:
            row: dict[str, Any] = {
                "date": period.isoformat(),
                "source_keys": [HQM_SOURCE],
                "batch_id": str(run.batch_id),
                "_lineage": {},
            }
            for series_id in HQM_REQUIRED_SERIES:
                label = series_id.removeprefix("HQM-PAR-")
                item = maps[series_id][period]
                row[label] = float(item.value)
                row["_lineage"][label] = _direct_lineage(item, evidence)
            history_rows.append(row)
        charts = [
            _chart_shell(
                key="hqm-latest-par-yield-curve",
                title="Latest HQM Monthly-Average Par Yield Curve",
                description="Four official Treasury HQM maturity points at one common month.",
                data=curve_rows,
                evidence=evidence,
                x_key="tenor",
                series_keys=("HQM par yield",),
            ),
            _chart_shell(
                key="hqm-par-yield-history",
                title="Treasury HQM Par Yield History",
                description="Exactly the latest 120 common valid months; no interpolation or fill.",
                data=history_rows,
                evidence=evidence,
                series_keys=("2Y", "5Y", "10Y", "30Y"),
            ),
        ]
        recent_columns = CREDIT_SPREAD_SECTION_COLUMNS["recent-hqm-observations"]
        recent_rows = []
        for period in evidence.common_dates[-24:]:
            observations = {key: maps[key][period] for key in HQM_REQUIRED_SERIES}
            recent_rows.append(
                _row_with_cells(
                    {
                        "date": period.isoformat(),
                        "hqm-2y": f"{observations['HQM-PAR-2Y'].value:.2f}%",
                        "hqm-5y": f"{observations['HQM-PAR-5Y'].value:.2f}%",
                        "hqm-10y": f"{observations['HQM-PAR-10Y'].value:.2f}%",
                        "hqm-30y": f"{observations['HQM-PAR-30Y'].value:.2f}%",
                        "quality": Observation.Quality.FRESH,
                        "batch": str(run.batch_id),
                        "artifact": evidence.raw_sha256,
                        "source_key": HQM_SOURCE,
                        "source_keys": [HQM_SOURCE],
                        "batch_id": str(run.batch_id),
                        "quality_status": Observation.Quality.FRESH,
                        "license_scope": evidence.scopes[HQM_SOURCE],
                        "fallback_source": None,
                        "lineage": {
                            key: _direct_lineage(value, evidence)
                            for key, value in observations.items()
                        },
                    },
                    recent_columns,
                )
            )
        sections = [
            {
                "key": "recent-hqm-observations",
                "title": "Recent HQM Observations",
                "description": "Exactly 24 common months with row-level batch and artifact lineage.",
                "columns": [
                    {"key": key, "label": label}
                    for key, label in zip(
                        recent_columns,
                        (
                            "Date",
                            "HQM 2Y",
                            "HQM 5Y",
                            "HQM 10Y",
                            "HQM 30Y",
                            "Quality",
                            "Batch",
                            "Artifact SHA-256",
                        ),
                        strict=True,
                    )
                ],
                "rows": recent_rows,
                "source_keys": [HQM_SOURCE],
                "batch_ids": [str(run.batch_id)],
                "as_of": datetime.combine(latest, time.min, tzinfo=UTC).isoformat(),
                "fetched_at": evidence.fetched_at.isoformat(),
                "fresh_until": evidence.fresh_until.isoformat(),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": evidence.scopes[HQM_SOURCE],
                "fallback_source": None,
            },
        ]
        method_columns = CREDIT_SPREAD_SECTION_COLUMNS[
            "hqm-source-freshness-methodology"
        ]
        method_row = _row_with_cells(
            {
                "source-dataset": f"{HQM_SOURCE} / {HQM_DATASET}",
                "run-batch": f"run {run.pk} / {run.batch_id}",
                "fetched": evidence.fetched_at.isoformat(),
                "latest-month": latest.isoformat(),
                "fresh-until": evidence.fresh_until.isoformat(),
                "artifact-sha-size": (
                    f"{evidence.raw_sha256} ({evidence.raw_size} bytes)"
                ),
                "rows": str(run.row_count),
                "licence": evidence.scopes[HQM_SOURCE],
                "fallback": "None",
                "source_key": HQM_SOURCE,
                "source_keys": [HQM_SOURCE, "internal"],
                "batch_id": str(run.batch_id),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": evidence.scopes[HQM_SOURCE],
                "fallback_source": None,
                "lineage": _run_reference(evidence),
            },
            method_columns,
        )
        sections.append(
            {
                "key": "hqm-source-freshness-methodology",
                "title": "HQM Source, Artifact, Freshness and Method",
                "columns": [{"key": key, "label": key.replace("-", " ").title()} for key in method_columns],
                "rows": [method_row],
                "source_keys": [HQM_SOURCE, "internal"],
                "batch_ids": [str(run.batch_id)],
                "as_of": datetime.combine(latest, time.min, tzinfo=UTC).isoformat(),
                "fetched_at": evidence.fetched_at.isoformat(),
                "fresh_until": evidence.fresh_until.isoformat(),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": evidence.scopes[HQM_SOURCE],
                "fallback_source": None,
            }
        )
        sections.append(
            _gap_section(
                key="licensed-spread-data-gaps",
                title="Licensed Spread Data Gaps",
                rows=(
                    ("ICE BofA IG/HY and rating-bucket OAS", "PURCHASE_REQUIRED", "ICE Data Indices display, history and derived rights"),
                    ("FINRA TRACE bond pricing / liquidity", "PURCHASE_REQUIRED", "Bulk storage and public derived-display licence"),
                    ("Licensed all-in yield / ETF market proxies", "PURCHASE_REQUIRED", "Market-data caching and website-display rights"),
                ),
                internal_scope=evidence.scopes["internal"],
            )
        )
        formulas = {key: HQM_CHANGE_FORMULA for key in CREDIT_SPREAD_METRICS}
        semantic_boundary = {
            "may_state": "Monthly-average high-quality corporate-bond par yields by HQM maturity.",
            "must_not_state": "Treasury spread, ICE BofA OAS, rating-bucket spread or CDS quote.",
        }
        required_metrics, required_charts, required_sections = (
            CREDIT_SPREAD_METRICS,
            CREDIT_SPREAD_CHARTS,
            CREDIT_SPREAD_SECTIONS,
        )
    else:
        for series_id in SLOOS_REQUIRED_SERIES:
            metrics.append(
                _metric(
                    key=SLOOS_METRIC_BY_SERIES[series_id],
                    label=SLOOS_LABELS[series_id],
                    previous=maps[series_id][previous],
                    current=maps[series_id][latest],
                    evidence=evidence,
                    formula=SLOOS_CHANGE_FORMULA,
                    change_unit="pp",
                    multiplier=Decimal("1"),
                )
            )
            metrics[-1]["metadata"]["positive_meaning"] = SLOOS_POSITIVE_MEANING[series_id]
            metrics[-1]["metadata"]["board_series_id"] = series_id
        standards = (
            "SUBLPDMBS_XWB_N.Q",
            "SUBLPDMHS_XWB_N.Q",
            "SUBLPDCILS_N.Q",
            "SUBLPDCISS_N.Q",
        )
        demand = ("SUBLPDMBD_XWB_N.Q", "SUBLPDMHD_XWB_N.Q")

        def history(series_ids: tuple[str, ...]) -> list[dict[str, Any]]:
            common = sorted(set.intersection(*({*maps[key]} for key in series_ids)))[-80:]
            if len(common) != 80:
                raise ValueError("SLOOS chart requires exactly 80 common valid quarters")
            result = []
            for period in common:
                row: dict[str, Any] = {
                    "date": period.isoformat(),
                    "source_keys": [SLOOS_SOURCE],
                    "batch_id": str(run.batch_id),
                    "_lineage": {},
                }
                for series_id in series_ids:
                    label = SLOOS_LABELS[series_id]
                    item = maps[series_id][period]
                    row[label] = float(item.value)
                    row["_lineage"][label] = _direct_lineage(item, evidence)
                result.append(row)
            return result

        charts = [
            _chart_shell(
                key="sloos-lending-standards-history",
                title="SLOOS Lending Standards History",
                description="Exactly 80 common valid quarters for four standards series.",
                data=history(standards),
                evidence=evidence,
                series_keys=tuple(SLOOS_LABELS[key] for key in standards),
            ),
            _chart_shell(
                key="sloos-loan-demand-history",
                title="SLOOS Loan Demand History",
                description="Exactly 80 common valid quarters for two weighted demand series.",
                data=history(demand),
                evidence=evidence,
                series_keys=tuple(SLOOS_LABELS[key] for key in demand),
            ),
        ]
        table_columns = CREDIT_STRESS_SECTION_COLUMNS["latest-sloos-survey-table"]
        survey_rows = []
        for series_id in SLOOS_REQUIRED_SERIES:
            current = maps[series_id][latest]
            old = maps[series_id][previous]
            survey_rows.append(
                _row_with_cells(
                    {
                        "metric": SLOOS_LABELS[series_id],
                        "value": f"{current.value:.2f}%",
                        "previous": f"{old.value:.2f}%",
                        "change-pp": f"{current.value - old.value:+.2f} pp",
                        "positive-meaning": SLOOS_POSITIVE_MEANING[series_id],
                        "value-date": latest.isoformat(),
                        "quality": Observation.Quality.FRESH,
                        "batch": str(run.batch_id),
                        "source_key": SLOOS_SOURCE,
                        "source_keys": [SLOOS_SOURCE, "internal"],
                        "batch_id": str(run.batch_id),
                        "quality_status": Observation.Quality.FRESH,
                        "license_scope": evidence.scopes[SLOOS_SOURCE],
                        "fallback_source": None,
                        "lineage": [_direct_lineage(old, evidence), _direct_lineage(current, evidence)],
                    },
                    table_columns,
                )
            )
        sections = [
            {
                "key": "latest-sloos-survey-table",
                "title": "Latest SLOOS Survey Table",
                "description": "Six exact Board series at one common valid survey quarter.",
                "columns": [{"key": key, "label": key.replace("-", " ").title()} for key in table_columns],
                "rows": survey_rows,
                "source_keys": [SLOOS_SOURCE, "internal"],
                "batch_ids": [str(run.batch_id)],
                "as_of": datetime.combine(latest, time.min, tzinfo=UTC).isoformat(),
                "fetched_at": evidence.fetched_at.isoformat(),
                "fresh_until": evidence.fresh_until.isoformat(),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": evidence.scopes[SLOOS_SOURCE],
                "fallback_source": None,
            }
        ]
        method_columns = CREDIT_STRESS_SECTION_COLUMNS[
            "sloos-source-freshness-methodology"
        ]
        metadata = dict(run.metadata or {})
        method_row = _row_with_cells(
            {
                "source-dataset": f"{SLOOS_SOURCE} / {SLOOS_DATASET}",
                "run-batch": f"run {run.pk} / {run.batch_id}",
                "file-prepared": metadata.get("file_prepared_at"),
                "fetched": evidence.fetched_at.isoformat(),
                "latest-quarter": latest.isoformat(),
                "fresh-until": evidence.fresh_until.isoformat(),
                "archive-sha-size": (
                    f"{evidence.raw_sha256} ({evidence.raw_size} bytes)"
                ),
                "member-sha-size": (
                    f"{metadata.get('archive_member_name')} / "
                    f"{metadata.get('archive_member_sha256')} "
                    f"({metadata.get('archive_member_size')} bytes)"
                ),
                "rows": str(run.row_count),
                "licence": evidence.scopes[SLOOS_SOURCE],
                "fallback": "None",
                "source_key": SLOOS_SOURCE,
                "source_keys": [SLOOS_SOURCE],
                "batch_id": str(run.batch_id),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": evidence.scopes[SLOOS_SOURCE],
                "fallback_source": None,
                "lineage": _run_reference(evidence),
            },
            method_columns,
        )
        sections.append(
            {
                "key": "sloos-source-freshness-methodology",
                "title": "SLOOS Source, Artifact, Freshness and Method",
                "columns": [{"key": key, "label": key.replace("-", " ").title()} for key in method_columns],
                "rows": [method_row],
                "source_keys": [SLOOS_SOURCE],
                "batch_ids": [str(run.batch_id)],
                "as_of": datetime.combine(latest, time.min, tzinfo=UTC).isoformat(),
                "fetched_at": evidence.fetched_at.isoformat(),
                "fresh_until": evidence.fresh_until.isoformat(),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": evidence.scopes[SLOOS_SOURCE],
                "fallback_source": None,
            }
        )
        sections.append(
            _gap_section(
                key="licensed-credit-stress-gaps",
                title="Licensed Credit-Stress Gaps",
                rows=(
                    ("Chicago Fed NFCI / ANFCI", "LICENSE_REVIEW", "Written commercial republication permission"),
                    ("ICE OAS", "PURCHASE_REQUIRED", "Index history, storage and public display rights"),
                    ("FINRA TRACE", "PURCHASE_REQUIRED", "Bulk storage and public derived-display rights"),
                    ("CDX / single-name CDS", "PURCHASE_REQUIRED", "Composite history and display rights"),
                    ("Complete five-factor stress history", "PURCHASE_REQUIRED", "Licensed component histories before calculation"),
                    ("ETF / market proxies", "PURCHASE_REQUIRED", "Market-data caching and website-display rights"),
                ),
                internal_scope=evidence.scopes["internal"],
            )
        )
        formulas = {key: SLOOS_CHANGE_FORMULA for key in CREDIT_STRESS_METRICS}
        semantic_boundary = {
            "may_state": "Quarterly net percentages for exact SLOOS standards and demand measures.",
            "must_not_state": "Market quote, NFCI, CDS, OAS, trading signal or composite stress score.",
        }
        required_metrics, required_charts, required_sections = (
            CREDIT_STRESS_METRICS,
            CREDIT_STRESS_CHARTS,
            CREDIT_STRESS_SECTIONS,
        )

    data: dict[str, Any] = {
        "demo": False,
        "contract_version": CREDIT_CONTRACT_VERSION,
        "formula_version": CREDIT_FORMULA_VERSION,
        "formulas": formulas,
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": sections,
        "required_metric_keys": sorted(required_metrics),
        "required_chart_keys": sorted(required_charts),
        "required_section_keys": list(required_sections),
        "semantic_boundary": semantic_boundary,
        "publication_batch_id": str(publication_batch_id),
        "credit_refresh_id": evidence.refresh_id,
        "refresh_cycle_id": evidence.refresh_id,
        "input_run": _run_reference(evidence),
        "component_run": _run_reference(evidence),
        "input_runs": [_run_reference(evidence)],
        "component_batches": [str(run.batch_id)],
        "component_dates": {"latest": evidence.latest_date.isoformat()},
        "artifact_refs": [
            {
                "id": evidence.artifact.pk,
                "uri": evidence.artifact.uri,
                "sha256": evidence.raw_sha256,
                "size_bytes": evidence.raw_size,
            }
        ],
        "source_keys": [evidence.run.source.key, "internal"],
        "licence_decisions": [
            {"source_key": key, "scope": value} for key, value in sorted(evidence.scopes.items())
        ],
        "required_notices": public_source_notices([evidence.run.source.key, "internal"]),
        "fresh_until": evidence.fresh_until.isoformat(),
        "as_of": datetime.combine(evidence.latest_date, time.min, tzinfo=UTC).isoformat(),
        "fetched_at": evidence.fetched_at.isoformat(),
        "file_prepared_at": (run.metadata or {}).get("file_prepared_at"),
        "fallback_state": "none",
        "fallback_source": None,
        "generated_at": (run.completed_at or evidence.fetched_at).isoformat(),
    }
    data["fingerprint"] = _semantic_fingerprint(page_key, data)
    data["semantic_fingerprint"] = data["fingerprint"]
    data["payload_integrity_hash"] = _payload_hash(data)
    return data, evidence


def _metric_row_metadata(
    metric: dict[str, Any], *, fingerprint: str, payload_hash: str
) -> dict[str, Any]:
    return {
        "metric_payload": deepcopy(metric),
        "formula": (metric.get("metadata") or {}).get("formula"),
        "formula_version": CREDIT_FORMULA_VERSION,
        "change_unit": (metric.get("metadata") or {}).get("change_unit"),
        "change_quality_status": Observation.Quality.ESTIMATED,
        "calculation_owner": "Atlas Macro",
        "input_lineage": (metric.get("metadata") or {}).get("input_lineage", []),
        "semantic_fingerprint": fingerprint,
        "publication_fingerprint": fingerprint,
        "payload_integrity_hash": payload_hash,
        "public_snapshot": True,
    }


def _store_metric_rows(
    *, page_key: str, data: dict[str, Any], batch_id: uuid.UUID
) -> None:
    if MetricSnapshot.objects.filter(batch_id=batch_id).exists():
        raise ValueError("credit publication batch already has normalized metric rows")
    source_keys = {str(item.get("source_key") or "") for item in data["metrics"]}
    sources = {item.key: item for item in Source.objects.filter(key__in=source_keys)}
    if set(sources) != source_keys:
        raise ValueError("credit metric source is missing")
    for metric in data["metrics"]:
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        if value_date is None or as_of is None or fetched_at is None:
            raise ValueError("credit metric dates are incomplete")
        MetricSnapshot.objects.create(
            key=f"{page_key}-{metric['key']}",
            label=str(metric["label"]),
            value=Decimal(str(metric["value"])),
            display_value=str(metric["display_value"]),
            change=(
                Decimal(str(metric["change"]))
                if metric.get("change") is not None
                else None
            ),
            unit="%",
            value_date=value_date,
            as_of=as_of,
            fetched_at=fetched_at,
            batch_id=batch_id,
            source=sources[str(metric["source_key"])],
            fallback_source=None,
            quality_status=Observation.Quality.FRESH,
            license_scope=str(metric.get("license_scope") or "")[:120],
            metadata=_metric_row_metadata(
                metric,
                fingerprint=str(data["fingerprint"]),
                payload_hash=str(data["payload_integrity_hash"]),
            ),
        )


def _metric_rows_match(
    snapshot: DashboardSnapshot, *, page_key: str, data: dict[str, Any]
) -> bool:
    rows = list(
        MetricSnapshot.objects.filter(batch_id=snapshot.batch_id)
        .select_related("source", "fallback_source")
        .order_by("key")
    )
    expected_keys = {f"{page_key}-{item['key']}" for item in data.get("metrics", [])}
    by_key = {item.key: item for item in rows}
    if len(rows) != len(expected_keys) or set(by_key) != expected_keys:
        return False
    for metric in data.get("metrics", []):
        row = by_key[f"{page_key}-{metric['key']}"]
        value_date = _parse_datetime(metric.get("value_date"))
        as_of = _parse_datetime(metric.get("as_of"))
        fetched_at = _parse_datetime(metric.get("fetched_at"))
        change = metric.get("change")
        if not (
            row.label == metric.get("label")
            and row.value is not None
            and row.value.quantize(Decimal("0.00000001"))
            == Decimal(str(metric.get("value"))).quantize(Decimal("0.00000001"))
            and row.display_value == metric.get("display_value")
            and (
                row.change.quantize(Decimal("0.000001"))
                if row.change is not None
                else None
            )
            == (
                Decimal(str(change)).quantize(Decimal("0.000001"))
                if change is not None
                else None
            )
            and row.unit == "%"
            and row.value_date == value_date
            and row.as_of == as_of
            and row.fetched_at == fetched_at
            and row.source.key == metric.get("source_key")
            and row.fallback_source is None
            and row.quality_status == Observation.Quality.FRESH
            and row.license_scope == str(metric.get("license_scope") or "")[:120]
            and row.metadata
            == _metric_row_metadata(
                metric,
                fingerprint=str(data["fingerprint"]),
                payload_hash=str(data["payload_integrity_hash"]),
            )
        ):
            return False
    return True


def _child_contract_sets(page_key: str) -> tuple[set[str], set[str], list[str]]:
    if page_key == "credit-spreads":
        return (
            set(CREDIT_SPREAD_METRICS),
            set(CREDIT_SPREAD_CHARTS),
            list(CREDIT_SPREAD_SECTIONS),
        )
    if page_key == "credit-stress":
        return (
            set(CREDIT_STRESS_METRICS),
            set(CREDIT_STRESS_CHARTS),
            list(CREDIT_STRESS_SECTIONS),
        )
    raise ValueError("unknown credit child page")


def _embedded_child_run(
    snapshot: DashboardSnapshot, *, page_key: str
) -> IngestionRun | None:
    data = dict(snapshot.data or {})
    first = data.get("input_run")
    second = data.get("component_run")
    if not isinstance(first, dict) or first != second:
        return None
    run = (
        IngestionRun.objects.select_related("source")
        .filter(pk=first.get("ingestion_run_id"))
        .first()
    )
    if run is None:
        return None
    try:
        evidence = _load_run_evidence(run, page_key=page_key, allow_expired=True)
    except (ArithmeticError, KeyError, OSError, TypeError, ValueError, zipfile.BadZipFile):
        return None
    if first != _run_reference(evidence):
        return None
    return run


def credit_child_snapshot_base_is_valid(
    snapshot: DashboardSnapshot, *, page_key: str
) -> bool:
    """Validate an embedded child revision without requiring its run to be latest."""

    try:
        if page_key not in {"credit-spreads", "credit-stress"}:
            return False
        data = dict(snapshot.data or {})
        required_metrics, required_charts, required_sections = _child_contract_sets(
            page_key
        )
        if (
            snapshot.key != page_key
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or snapshot.title != CHILD_TITLES[page_key]
            or snapshot.summary != CHILD_SUMMARIES[page_key]
            or data.get("demo") is not False
            or data.get("contract_version") != CREDIT_CONTRACT_VERSION
            or data.get("formula_version") != CREDIT_FORMULA_VERSION
            or data.get("publication_batch_id") != str(snapshot.batch_id)
            or data.get("fallback_state") != "none"
            or data.get("fallback_source") is not None
            or {item.get("key") for item in data.get("metrics", [])}
            != required_metrics
            or {item.get("key") for item in data.get("charts", [])}
            != required_charts
            or [item.get("key") for item in data.get("sections", [])]
            != required_sections
            or data.get("required_metric_keys") != sorted(required_metrics)
            or data.get("required_chart_keys") != sorted(required_charts)
            or data.get("required_section_keys") != required_sections
            or data.get("semantic_fingerprint") != data.get("fingerprint")
            or data.get("fingerprint") != _semantic_fingerprint(page_key, data)
            or data.get("payload_integrity_hash") != _payload_hash(data)
        ):
            return False
        for section in data.get("sections", []):
            expected_columns = CREDIT_SECTION_COLUMNS.get(str(section.get("key") or ""))
            columns = tuple(item.get("key") for item in section.get("columns", []))
            if (
                expected_columns is None
                or columns != expected_columns
                or section.get("rows")
                and (
                    not columns
                    or any(
                        [item.get("key") for item in row.get("cells_list", [])]
                        != list(columns)
                        for row in section["rows"]
                    )
                )
            ):
                return False
        run = _embedded_child_run(snapshot, page_key=page_key)
        if run is None:
            return False
        expected, _evidence = _build_credit_child(
            run,
            page_key=page_key,
            publication_batch_id=snapshot.batch_id,
            allow_expired=True,
        )
        actual = {
            key: value
            for key, value in data.items()
            if key not in {"refresh_failure", "presentation_quality"}
        }
        expected_core = {
            key: value
            for key, value in expected.items()
            if key not in {"refresh_failure", "presentation_quality"}
        }
        as_of = _parse_datetime(data.get("as_of"))
        if (
            actual != expected_core
            or as_of is None
            or snapshot.as_of != as_of
            or snapshot.quality_status
            not in {Observation.Quality.ESTIMATED, Observation.Quality.STALE}
            or not _metric_rows_match(snapshot, page_key=page_key, data=data)
        ):
            return False
        return True
    except (
        ArithmeticError,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        return False


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
        "credit_refresh_id": str((run.metadata or {}).get("credit_refresh_id") or ""),
    }


def _refresh_failure_valid(
    snapshot: DashboardSnapshot,
    *,
    page_key: str,
    latest: IngestionRun,
) -> bool:
    marker = (snapshot.data or {}).get("refresh_failure")
    if not isinstance(marker, dict) or set(marker) != {
        "checked_at",
        "reason_code",
        "reason",
        "attempt",
    }:
        return False
    checked_at = _parse_datetime(marker.get("checked_at"))
    reason_code = marker.get("reason_code")
    embedded = _embedded_child_run(snapshot, page_key=page_key)
    return bool(
        checked_at is not None
        and checked_at <= timezone.now() + timedelta(minutes=5)
        and checked_at >= snapshot.created_at
        and reason_code in REFRESH_FAILURE_REASONS
        and marker.get("reason") == REFRESH_FAILURE_REASONS[reason_code]
        and marker.get("attempt") == _attempt_reference(latest)
        and latest.status != IngestionRun.Status.RUNNING
        and embedded is not None
        and (latest.started_at, latest.pk) > (embedded.started_at, embedded.pk)
    )


def _child_public_state(
    snapshot: DashboardSnapshot, *, page_key: str, latest: IngestionRun | None
) -> str | None:
    if latest is None or not credit_child_snapshot_base_is_valid(snapshot, page_key=page_key):
        return None
    embedded = _embedded_child_run(snapshot, page_key=page_key)
    if embedded is None:
        return None
    deadline = _parse_datetime((snapshot.data or {}).get("fresh_until"))
    expired = deadline is None or deadline <= timezone.now()
    if latest.pk == embedded.pk:
        if (
            latest.status == IngestionRun.Status.SUCCESS
            and latest.row_count > 0
            and (snapshot.data or {}).get("refresh_failure") is None
            and snapshot.quality_status == Observation.Quality.ESTIMATED
        ):
            return "natural_expiry" if expired else "current_candidate"
        return None
    if (latest.started_at, latest.pk) <= (embedded.started_at, embedded.pk):
        return None
    if latest.status == IngestionRun.Status.RUNNING:
        return "transition_pending"
    if _refresh_failure_valid(snapshot, page_key=page_key, latest=latest):
        return "retained_failure"
    return None


def select_public_credit_child_snapshot(
    page_key: str,
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    if page_key == "credit-spreads":
        source_key, dataset = HQM_SOURCE, HQM_DATASET
    elif page_key == "credit-stress":
        source_key, dataset = SLOOS_SOURCE, SLOOS_DATASET
    else:
        return None
    if candidates is None:
        candidates = (
            DashboardSnapshot.objects.filter(
                key=page_key,
                is_published=True,
                data__contract_version=CREDIT_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")[:50]
        )
    latest = _latest_attempt(source_key, dataset)
    for candidate in candidates:
        state = _child_public_state(candidate, page_key=page_key, latest=latest)
        if state is None:
            continue
        presented = deepcopy(candidate)
        presented.data = deepcopy(candidate.data or {})
        if state != "retained_failure":
            presented.data.pop("refresh_failure", None)
        if state != "current_candidate":
            presented.quality_status = Observation.Quality.STALE
        presented.credit_publication_state = state
        return presented
    return None


def select_public_credit_spreads_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    return select_public_credit_child_snapshot("credit-spreads", candidates)


def select_public_credit_stress_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    return select_public_credit_child_snapshot("credit-stress", candidates)


def credit_spreads_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    return select_public_credit_spreads_snapshot([snapshot]) is not None


def credit_stress_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    return select_public_credit_stress_snapshot([snapshot]) is not None


def _mark_child_failure(*, page_key: str, latest: IngestionRun, reason_code: str) -> None:
    if latest.status == IngestionRun.Status.RUNNING:
        return
    candidates = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key=page_key,
            is_published=True,
            data__contract_version=CREDIT_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")[:50]
    )
    list(
        MetricSnapshot.objects.select_for_update()
        .filter(batch_id__in=[item.batch_id for item in candidates])
        .order_by("batch_id", "key")
        .values_list("pk", flat=True)
    )
    retained = next(
        (
            item
            for item in candidates
            if (
                (embedded := _embedded_child_run(item, page_key=page_key))
                is not None
                and embedded.pk != latest.pk
                and (latest.started_at, latest.pk) > (embedded.started_at, embedded.pk)
                and credit_child_snapshot_base_is_valid(item, page_key=page_key)
            )
        ),
        None,
    )
    if retained is None:
        return
    data = dict(retained.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason_code": reason_code,
        "reason": REFRESH_FAILURE_REASONS[reason_code],
        "attempt": _attempt_reference(latest),
    }
    retained.data = data
    retained.quality_status = Observation.Quality.STALE
    retained.save(update_fields=["data", "quality_status", "updated_at"])


_CREDIT_PUBLISH_CAPABILITY = object()


def _publish_credit_child_revision(
    *,
    page_key: str,
    run: IngestionRun,
    batch_id: uuid.UUID,
    _capability: object | None = None,
) -> DashboardSnapshot:
    if _capability is not _CREDIT_PUBLISH_CAPABILITY:
        raise ValueError("credit child revisions require the private dedicated capability")
    data, _evidence = _build_credit_child(
        run,
        page_key=page_key,
        publication_batch_id=batch_id,
        allow_expired=False,
    )
    as_of = _parse_datetime(data["as_of"])
    if as_of is None:
        raise ValueError("credit child as-of is missing")
    _store_metric_rows(page_key=page_key, data=data, batch_id=batch_id)
    return DashboardSnapshot.objects.create(
        key=page_key,
        title=CHILD_TITLES[page_key],
        as_of=as_of,
        batch_id=batch_id,
        quality_status=Observation.Quality.ESTIMATED,
        summary=CHILD_SUMMARIES[page_key],
        data=data,
        source=ensure_source("internal"),
        is_published=True,
    )


@transaction.atomic
def publish_credit_spreads_revision(
    *, run: IngestionRun, batch_id: uuid.UUID
) -> DashboardSnapshot:
    latest = _latest_attempt(HQM_SOURCE, HQM_DATASET, lock=True)
    if latest is None or latest.pk != run.pk:
        raise ValueError("credit-spreads publisher requires the latest HQM attempt")
    locked_inputs = _lock_credit_run_inputs(latest)
    published = _publish_credit_child_revision(
        page_key="credit-spreads",
        run=latest,
        batch_id=batch_id,
        _capability=_CREDIT_PUBLISH_CAPABILITY,
    )
    _assert_credit_run_inputs_unchanged(latest, locked_inputs)
    return published


@transaction.atomic
def publish_credit_stress_revision(
    *, run: IngestionRun, batch_id: uuid.UUID
) -> DashboardSnapshot:
    latest = _latest_attempt(SLOOS_SOURCE, SLOOS_DATASET, lock=True)
    if latest is None or latest.pk != run.pk:
        raise ValueError("credit-stress publisher requires the latest SLOOS attempt")
    locked_inputs = _lock_credit_run_inputs(latest)
    published = _publish_credit_child_revision(
        page_key="credit-stress",
        run=latest,
        batch_id=batch_id,
        _capability=_CREDIT_PUBLISH_CAPABILITY,
    )
    _assert_credit_run_inputs_unchanged(latest, locked_inputs)
    return published


_publish_credit_spreads_revision = publish_credit_spreads_revision
_publish_credit_stress_revision = publish_credit_stress_revision


@transaction.atomic
def coordinate_credit_child(
    page_key: str, trigger_runs: Iterable[IngestionRun] = ()
) -> tuple[list[DashboardSnapshot], set[str]]:
    if page_key == "credit-spreads":
        source_key, dataset = HQM_SOURCE, HQM_DATASET
    elif page_key == "credit-stress":
        source_key, dataset = SLOOS_SOURCE, SLOOS_DATASET
    else:
        raise ValueError("unknown credit child page")
    for key in (source_key, "internal"):
        ensure_source(key)
    list(
        Source.objects.select_for_update()
        .filter(key__in={source_key, "internal"})
        .order_by("key")
        .values_list("pk", flat=True)
    )
    list(
        SourceLicense.objects.select_for_update()
        .filter(source__key__in={source_key, "internal"}, is_current=True)
        .order_by("source__key")
        .values_list("pk", flat=True)
    )
    relevant = [
        run
        for run in trigger_runs
        if run.source.key == source_key and run.dataset == dataset
    ]
    latest = _latest_attempt(source_key, dataset, lock=True)
    if relevant and (latest is None or any(item.pk != latest.pk for item in relevant)):
        selected = select_public_credit_child_snapshot(page_key)
        return [], ({page_key} if selected is None else set())
    if latest is None:
        return [], {page_key}
    if latest.status == IngestionRun.Status.RUNNING:
        selected = select_public_credit_child_snapshot(page_key)
        return [], ({page_key} if selected is None else set())
    if latest.status != IngestionRun.Status.SUCCESS or latest.row_count <= 0:
        _mark_child_failure(
            page_key=page_key,
            latest=latest,
            reason_code="latest-attempt-incomplete",
        )
        selected = select_public_credit_child_snapshot(page_key)
        return [], ({page_key} if selected is None else set())

    locked_inputs = _lock_credit_run_inputs(latest)

    candidates = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key=page_key,
            is_published=True,
            data__contract_version=CREDIT_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")[:50]
    )
    list(
        MetricSnapshot.objects.select_for_update()
        .filter(batch_id__in=[item.batch_id for item in candidates])
        .order_by("batch_id", "key")
        .values_list("pk", flat=True)
    )
    existing = next(
        (
            item
            for item in candidates
            if _embedded_child_run(item, page_key=page_key) == latest
            and credit_child_snapshot_base_is_valid(item, page_key=page_key)
            and (item.data or {}).get("refresh_failure") is None
        ),
        None,
    )
    if existing is not None:
        return [], set()
    try:
        candidate_data, evidence = _build_credit_child(
            latest,
            page_key=page_key,
            publication_batch_id=uuid.uuid4(),
            allow_expired=False,
        )
        _assert_credit_run_inputs_unchanged(latest, locked_inputs)
    except (
        ArithmeticError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        _mark_child_failure(
            page_key=page_key, latest=latest, reason_code="candidate-validation"
        )
        selected = select_public_credit_child_snapshot(page_key)
        return [], ({page_key} if selected is None else set())
    previous = next(
        (
            item
            for item in candidates
            if credit_child_snapshot_base_is_valid(item, page_key=page_key)
            and _embedded_child_run(item, page_key=page_key) != latest
        ),
        None,
    )
    if previous is not None:
        previous_date = str((previous.data or {}).get("component_dates", {}).get("latest") or "")
        if previous_date and evidence.latest_date.isoformat() < previous_date:
            _mark_child_failure(
                page_key=page_key, latest=latest, reason_code="candidate-regression"
            )
            selected = select_public_credit_child_snapshot(page_key)
            return [], ({page_key} if selected is None else set())
    try:
        with transaction.atomic():
            publisher = (
                publish_credit_spreads_revision
                if page_key == "credit-spreads"
                else publish_credit_stress_revision
            )
            _assert_credit_run_inputs_unchanged(latest, locked_inputs)
            published = publisher(
                run=latest,
                batch_id=uuid.UUID(candidate_data["publication_batch_id"]),
            )
            _assert_credit_run_inputs_unchanged(latest, locked_inputs)
            current = _latest_attempt(source_key, dataset, lock=True)
            selected = select_public_credit_child_snapshot(page_key)
            if (
                current is None
                or current.pk != latest.pk
                or selected is None
                or getattr(selected, "credit_publication_state", None)
                != "current_candidate"
                or _embedded_child_run(selected, page_key=page_key) != latest
                or not credit_child_snapshot_base_is_valid(selected, page_key=page_key)
            ):
                raise ValueError("credit child post-publication selector check failed")
    except (
        ArithmeticError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        _mark_child_failure(
            page_key=page_key,
            latest=latest,
            reason_code="publication-postcondition",
        )
        selected = select_public_credit_child_snapshot(page_key)
        return [], ({page_key} if selected is None else set())
    return [published], set()


def _child_snapshot_reference(snapshot: DashboardSnapshot) -> dict[str, Any]:
    data = dict(snapshot.data or {})
    run_ref = deepcopy(data.get("input_run") or {})
    return {
        "page_key": snapshot.key,
        "snapshot_id": snapshot.pk,
        "publication_batch_id": str(snapshot.batch_id),
        "payload_integrity_hash": data.get("payload_integrity_hash"),
        "semantic_fingerprint": data.get("semantic_fingerprint"),
        "contract_version": data.get("contract_version"),
        "formula_version": data.get("formula_version"),
        "credit_refresh_id": data.get("credit_refresh_id"),
        "as_of": data.get("as_of"),
        "fetched_at": data.get("fetched_at"),
        "fresh_until": data.get("fresh_until"),
        "input_run": run_ref,
        "artifact_refs": deepcopy(data.get("artifact_refs") or []),
    }


def _component_snapshot_from_reference(reference: Any) -> DashboardSnapshot | None:
    if not isinstance(reference, dict):
        return None
    child = (
        DashboardSnapshot.objects.select_related("source")
        .filter(pk=reference.get("snapshot_id"))
        .first()
    )
    if (
        child is None
        or reference != _child_snapshot_reference(child)
        or child.key not in {"credit-spreads", "credit-stress"}
        or not credit_child_snapshot_base_is_valid(child, page_key=child.key)
    ):
        return None
    return child


def _build_credit_overview(
    children: dict[str, DashboardSnapshot], *, publication_batch_id: uuid.UUID
) -> dict[str, Any]:
    if set(children) != {"credit-spreads", "credit-stress"}:
        raise ValueError("credit overview requires both strict child snapshots")
    if any(
        not credit_child_snapshot_base_is_valid(child, page_key=key)
        for key, child in children.items()
    ):
        raise ValueError("credit overview child base validation failed")
    spread_data = dict(children["credit-spreads"].data or {})
    stress_data = dict(children["credit-stress"].data or {})
    refresh_ids = {
        str(spread_data.get("credit_refresh_id") or ""),
        str(stress_data.get("credit_refresh_id") or ""),
    }
    if len(refresh_ids) != 1 or not next(iter(refresh_ids)):
        raise ValueError("credit overview children are from different refresh cycles")
    refresh_id = next(iter(refresh_ids))
    spread_metrics = {item["key"]: item for item in spread_data["metrics"]}
    stress_metrics = {item["key"]: item for item in stress_data["metrics"]}
    specs = (
        (
            "overview-hqm-10y",
            "HQM 10Y Par Yield",
            children["credit-spreads"],
            spread_metrics["hqm-par-10y"],
        ),
        (
            "overview-hqm-30y",
            "HQM 30Y Par Yield",
            children["credit-spreads"],
            spread_metrics["hqm-par-30y"],
        ),
        (
            "overview-sloos-business-standards",
            "SLOOS Business Lending Standards",
            children["credit-stress"],
            stress_metrics["sloos-business-standards-weighted"],
        ),
        (
            "overview-sloos-business-demand",
            "SLOOS Business Loan Demand",
            children["credit-stress"],
            stress_metrics["sloos-business-demand-weighted"],
        ),
    )
    metrics = []
    for key, label, child, source_metric in specs:
        copied = deepcopy(source_metric)
        copied["key"] = key
        copied["label"] = label
        copied.setdefault("metadata", {})["component_page_key"] = child.key
        copied["metadata"]["component_snapshot_id"] = child.pk
        copied["metadata"]["component_metric_key"] = source_metric["key"]
        copied["metadata"]["component_publication_batch_id"] = str(child.batch_id)
        copied["metadata"]["component_payload_integrity_hash"] = (
            child.data or {}
        ).get("payload_integrity_hash")
        metrics.append(copied)

    spread_chart = next(
        item for item in spread_data["charts"] if item["key"] == "hqm-par-yield-history"
    )
    stress_chart = next(
        item
        for item in stress_data["charts"]
        if item["key"] == "sloos-lending-standards-history"
    )
    charts = []
    for key, title, child, source_chart in (
        (
            "credit-overview-hqm-history",
            "Treasury HQM Par Yield History",
            children["credit-spreads"],
            spread_chart,
        ),
        (
            "credit-overview-sloos-standards-history",
            "SLOOS Lending Standards History",
            children["credit-stress"],
            stress_chart,
        ),
    ):
        copied = deepcopy(source_chart)
        copied["key"] = key
        copied["title"] = title
        copied["component_snapshot"] = _child_snapshot_reference(child)
        charts.append(copied)

    child_refs = [_child_snapshot_reference(children[key]) for key in sorted(children)]
    scopes: dict[str, str] = {}
    for source in Source.objects.filter(key__in={HQM_SOURCE, SLOOS_SOURCE, "internal"}):
        scope = _effective_scope(source)
        if scope is None:
            raise ValueError("credit overview source licence is not currently effective")
        scopes[source.key] = scope
    if set(scopes) != {HQM_SOURCE, SLOOS_SOURCE, "internal"}:
        raise ValueError("credit overview required sources are missing")

    ledger_columns = CREDIT_OVERVIEW_SECTION_COLUMNS["credit-component-ledger"]
    ledger_rows = []
    for key in ("credit-spreads", "credit-stress"):
        child = children[key]
        child_data = dict(child.data or {})
        run_ref = dict(child_data["input_run"])
        ledger_rows.append(
            _row_with_cells(
                {
                    "component": key,
                    "snapshot-batch": f"snapshot {child.pk} / {child.batch_id}",
                    "payload-hashes": (
                        f"payload {child_data['payload_integrity_hash']} / "
                        f"semantic {child_data['semantic_fingerprint']}"
                    ),
                    "input-run-batch": (
                        f"run {run_ref['ingestion_run_id']} / {run_ref['batch_id']}"
                    ),
                    "value-date": child_data["as_of"],
                    "fetched": child_data["fetched_at"],
                    "fresh-until": child_data["fresh_until"],
                    "artifact": (
                        f"{run_ref['artifact_sha256']} "
                        f"({run_ref['artifact_size']} bytes)"
                    ),
                    "quality": Observation.Quality.ESTIMATED,
                    "licence": "; ".join(
                        f"{item['source_key']}: {item['scope']}"
                        for item in child_data["licence_decisions"]
                    ),
                    "fallback": child_data["fallback_state"],
                    "source_keys": child_data["source_keys"],
                    "batch_id": str(child.batch_id),
                    "quality_status": Observation.Quality.ESTIMATED,
                    "fallback_source": None,
                    "lineage": _child_snapshot_reference(child),
                },
                ledger_columns,
            )
        )
    boundary_columns = CREDIT_OVERVIEW_SECTION_COLUMNS["credit-semantic-boundary"]
    boundary_rows = [
        _row_with_cells(
            {
                "evidence": "Treasury HQM",
                "can-state": "High-quality corporate-bond monthly-average par yields.",
                "cannot-state": "Treasury spread, OAS, rating bucket or CDS quote.",
                "status": "PUBLIC_OFFICIAL_PROXY",
                "source": HQM_SOURCE,
                "source_keys": [HQM_SOURCE, "internal"],
                "quality_status": Observation.Quality.ESTIMATED,
                "fallback_source": None,
            },
            boundary_columns,
        ),
        _row_with_cells(
            {
                "evidence": "Federal Reserve SLOOS",
                "can-state": "Quarterly bank survey net percentages for standards and demand.",
                "cannot-state": "Market price, NFCI, composite stress score or trade signal.",
                "status": "PUBLIC_OFFICIAL_PROXY",
                "source": SLOOS_SOURCE,
                "source_keys": [SLOOS_SOURCE, "internal"],
                "quality_status": Observation.Quality.ESTIMATED,
                "fallback_source": None,
            },
            boundary_columns,
        ),
    ]
    sections = [
        {
            "key": "credit-component-ledger",
            "title": "Credit Component Ledger",
            "description": "Exactly two independently replayable strict child revisions.",
            "columns": [{"key": key, "label": key.replace("-", " ").title()} for key in ledger_columns],
            "rows": ledger_rows,
            "source_keys": [HQM_SOURCE, SLOOS_SOURCE, "internal"],
            "batch_ids": [str(children[key].batch_id) for key in sorted(children)],
            "quality_status": Observation.Quality.ESTIMATED,
            "fallback_source": None,
        },
        {
            "key": "credit-semantic-boundary",
            "title": "Credit Semantic Boundary",
            "description": "What each official child can and cannot support.",
            "columns": [{"key": key, "label": key.replace("-", " ").title()} for key in boundary_columns],
            "rows": boundary_rows,
            "source_keys": [HQM_SOURCE, SLOOS_SOURCE, "internal"],
            "quality_status": Observation.Quality.ESTIMATED,
            "fallback_source": None,
        },
        _gap_section(
            key="licensed-credit-market-gaps",
            title="Licensed Credit-Market Gaps",
            rows=(
                ("ICE OAS and rating buckets", "PURCHASE_REQUIRED", "Index history, storage and public display rights"),
                ("FINRA TRACE pricing / liquidity", "PURCHASE_REQUIRED", "Bulk storage and derived-display rights"),
                ("Chicago Fed NFCI / ANFCI", "LICENSE_REVIEW", "Written commercial republication permission"),
                ("CDX / single-name CDS", "PURCHASE_REQUIRED", "Composite history and public display rights"),
                ("Credit issuance database", "PURCHASE_REQUIRED", "Structured issuance storage and display rights"),
                ("Ratings / default events", "PURCHASE_REQUIRED", "Structured event history and display rights"),
                ("ETF / market prices", "PURCHASE_REQUIRED", "Market-data caching and website-display rights"),
            ),
            internal_scope=scopes["internal"],
        ),
    ]
    as_of_values = [_parse_datetime((child.data or {}).get("as_of")) for child in children.values()]
    fetched_values = [
        _parse_datetime((child.data or {}).get("fetched_at")) for child in children.values()
    ]
    freshness_values = [
        _parse_datetime((child.data or {}).get("fresh_until")) for child in children.values()
    ]
    if any(value is None for value in [*as_of_values, *fetched_values, *freshness_values]):
        raise ValueError("credit overview child dates are incomplete")
    as_of = min(value for value in as_of_values if value is not None)
    fetched_at = max(value for value in fetched_values if value is not None)
    fresh_until = min(value for value in freshness_values if value is not None)
    data: dict[str, Any] = {
        "demo": False,
        "contract_version": CREDIT_CONTRACT_VERSION,
        "formula_version": CREDIT_FORMULA_VERSION,
        "formulas": {
            "overview-hqm-10y": HQM_CHANGE_FORMULA,
            "overview-hqm-30y": HQM_CHANGE_FORMULA,
            "overview-sloos-business-standards": SLOOS_CHANGE_FORMULA,
            "overview-sloos-business-demand": SLOOS_CHANGE_FORMULA,
        },
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": sections,
        "required_metric_keys": sorted(CREDIT_OVERVIEW_METRICS),
        "required_chart_keys": sorted(CREDIT_OVERVIEW_CHARTS),
        "required_section_keys": list(CREDIT_OVERVIEW_SECTIONS),
        "semantic_boundary": {
            "may_state": "The two official child facts with their independent frequencies and dates.",
            "must_not_state": "OAS, CDS, issuance, ratings/default events or a composite stress score.",
        },
        "publication_batch_id": str(publication_batch_id),
        "credit_refresh_id": refresh_id,
        "refresh_cycle_id": refresh_id,
        "component_snapshots": child_refs,
        "component_batches": [str(children[key].batch_id) for key in sorted(children)],
        "component_dates": {
            key: {
                "as_of": (children[key].data or {}).get("as_of"),
                "fetched_at": (children[key].data or {}).get("fetched_at"),
                "fresh_until": (children[key].data or {}).get("fresh_until"),
            }
            for key in sorted(children)
        },
        "artifact_refs": [
            item
            for key in sorted(children)
            for item in deepcopy((children[key].data or {}).get("artifact_refs") or [])
        ],
        "source_keys": [HQM_SOURCE, SLOOS_SOURCE, "internal"],
        "licence_decisions": [
            {"source_key": key, "scope": value} for key, value in sorted(scopes.items())
        ],
        "required_notices": public_source_notices([HQM_SOURCE, SLOOS_SOURCE, "internal"]),
        "fresh_until": fresh_until.isoformat(),
        "as_of": as_of.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "fallback_state": "none",
        "fallback_source": None,
        "generated_at": fetched_at.isoformat(),
    }
    data["fingerprint"] = _semantic_fingerprint("credit", data)
    data["semantic_fingerprint"] = data["fingerprint"]
    data["payload_integrity_hash"] = _payload_hash(data)
    return data


def _overview_children(snapshot: DashboardSnapshot) -> dict[str, DashboardSnapshot] | None:
    references = (snapshot.data or {}).get("component_snapshots")
    if not isinstance(references, list) or len(references) != 2:
        return None
    children: dict[str, DashboardSnapshot] = {}
    for reference in references:
        child = _component_snapshot_from_reference(reference)
        if child is None or child.key in children:
            return None
        children[child.key] = child
    return children if set(children) == {"credit-spreads", "credit-stress"} else None


def _lock_credit_child_snapshot_rows(
    children: dict[str, DashboardSnapshot],
) -> dict[str, DashboardSnapshot]:
    """Lock the exact child revisions and normalized metrics used by the parent."""

    if set(children) != {"credit-spreads", "credit-stress"}:
        raise ValueError("credit overview child lock requires the exact child set")
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("credit child snapshot locks require an atomic transaction")
    expected_ids = {key: child.pk for key, child in children.items()}
    locked_rows = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(pk__in=expected_ids.values())
        .select_related("source")
        .order_by("key", "pk")
    )
    locked = {child.key: child for child in locked_rows}
    if (
        set(locked) != {"credit-spreads", "credit-stress"}
        or {key: child.pk for key, child in locked.items()} != expected_ids
    ):
        raise ValueError("credit overview child snapshot rows changed before lock")
    list(
        MetricSnapshot.objects.select_for_update()
        .filter(batch_id__in=[locked[key].batch_id for key in sorted(locked)])
        .order_by("batch_id", "key", "pk")
        .values_list("pk", flat=True)
    )
    return locked


def credit_snapshot_base_is_valid(snapshot: DashboardSnapshot) -> bool:
    """Validate an overview revision and both embedded child bases independently."""

    try:
        data = dict(snapshot.data or {})
        if (
            snapshot.key != "credit"
            or not snapshot.is_published
            or snapshot.source.key != "internal"
            or snapshot.title != OVERVIEW_TITLE
            or snapshot.summary != OVERVIEW_SUMMARY
            or data.get("demo") is not False
            or data.get("contract_version") != CREDIT_CONTRACT_VERSION
            or data.get("formula_version") != CREDIT_FORMULA_VERSION
            or data.get("publication_batch_id") != str(snapshot.batch_id)
            or data.get("fallback_state") != "none"
            or data.get("fallback_source") is not None
            or {item.get("key") for item in data.get("metrics", [])}
            != set(CREDIT_OVERVIEW_METRICS)
            or {item.get("key") for item in data.get("charts", [])}
            != set(CREDIT_OVERVIEW_CHARTS)
            or [item.get("key") for item in data.get("sections", [])]
            != list(CREDIT_OVERVIEW_SECTIONS)
            or data.get("required_metric_keys") != sorted(CREDIT_OVERVIEW_METRICS)
            or data.get("required_chart_keys") != sorted(CREDIT_OVERVIEW_CHARTS)
            or data.get("required_section_keys") != list(CREDIT_OVERVIEW_SECTIONS)
            or data.get("semantic_fingerprint") != data.get("fingerprint")
            or data.get("fingerprint") != _semantic_fingerprint("credit", data)
            or data.get("payload_integrity_hash") != _payload_hash(data)
        ):
            return False
        for section in data.get("sections", []):
            expected_columns = CREDIT_SECTION_COLUMNS.get(str(section.get("key") or ""))
            columns = tuple(item.get("key") for item in section.get("columns", []))
            if (
                expected_columns is None
                or columns != expected_columns
                or section.get("rows")
                and any(
                    [item.get("key") for item in row.get("cells_list", [])]
                    != list(columns)
                    for row in section["rows"]
                )
            ):
                return False
        children = _overview_children(snapshot)
        if children is None:
            return False
        expected = _build_credit_overview(
            children, publication_batch_id=snapshot.batch_id
        )
        actual = {
            key: value
            for key, value in data.items()
            if key not in {"refresh_failure", "presentation_quality"}
        }
        expected_core = {
            key: value
            for key, value in expected.items()
            if key not in {"refresh_failure", "presentation_quality"}
        }
        as_of = _parse_datetime(data.get("as_of"))
        return bool(
            actual == expected_core
            and as_of is not None
            and snapshot.as_of == as_of
            and snapshot.quality_status
            in {Observation.Quality.ESTIMATED, Observation.Quality.STALE}
            and _metric_rows_match(snapshot, page_key="credit", data=data)
        )
    except (
        ArithmeticError,
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        return False


def _parent_failure_valid(
    snapshot: DashboardSnapshot,
    *,
    latest_runs: dict[str, IngestionRun | None],
) -> bool:
    marker = (snapshot.data or {}).get("refresh_failure")
    if not isinstance(marker, dict) or set(marker) != {
        "checked_at",
        "reason_code",
        "reason",
        "attempts",
    }:
        return False
    checked_at = _parse_datetime(marker.get("checked_at"))
    reason_code = marker.get("reason_code")
    attempts = [
        _attempt_reference(latest_runs[key])
        for key in ("credit-spreads", "credit-stress")
        if latest_runs.get(key) is not None
    ]
    children = _overview_children(snapshot)
    if children is None:
        return False
    embedded_runs = {
        key: _embedded_child_run(child, page_key=key) for key, child in children.items()
    }
    return bool(
        checked_at is not None
        and checked_at <= timezone.now() + timedelta(minutes=5)
        and checked_at >= snapshot.created_at
        and reason_code in REFRESH_FAILURE_REASONS
        and marker.get("reason") == REFRESH_FAILURE_REASONS[reason_code]
        and marker.get("attempts") == attempts
        and all(run is not None for run in latest_runs.values())
        and any(
            latest_runs[key] is not None
            and embedded_runs[key] is not None
            and (latest_runs[key].started_at, latest_runs[key].pk)
            > (embedded_runs[key].started_at, embedded_runs[key].pk)
            for key in latest_runs
        )
        and not any(
            run is not None and run.status == IngestionRun.Status.RUNNING
            for run in latest_runs.values()
        )
    )


def _overview_public_state(
    snapshot: DashboardSnapshot,
    *,
    latest_runs: dict[str, IngestionRun | None],
) -> str | None:
    if not credit_snapshot_base_is_valid(snapshot) or any(
        value is None for value in latest_runs.values()
    ):
        return None
    children = _overview_children(snapshot)
    if children is None:
        return None
    current_children = {
        "credit-spreads": select_public_credit_spreads_snapshot(),
        "credit-stress": select_public_credit_stress_snapshot(),
    }
    if all(
        child is not None
        and getattr(child, "credit_publication_state", None)
        in {"current_candidate", "natural_expiry"}
        and child.pk == children[key].pk
        for key, child in current_children.items()
    ):
        deadline = _parse_datetime((snapshot.data or {}).get("fresh_until"))
        return (
            "natural_expiry"
            if deadline is None or deadline <= timezone.now()
            else "current_candidate"
        )
    embedded_runs = {
        key: _embedded_child_run(child, page_key=key) for key, child in children.items()
    }
    if any(run is None for run in embedded_runs.values()):
        return None
    if any(
        latest_runs[key] is not None
        and latest_runs[key].status == IngestionRun.Status.RUNNING
        and (latest_runs[key].started_at, latest_runs[key].pk)
        > (embedded_runs[key].started_at, embedded_runs[key].pk)
        for key in latest_runs
    ):
        return "transition_pending"
    if _parent_failure_valid(snapshot, latest_runs=latest_runs):
        return "retained_failure"
    return None


def select_public_credit_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    if candidates is None:
        candidates = (
            DashboardSnapshot.objects.filter(
                key="credit",
                is_published=True,
                data__contract_version=CREDIT_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")[:50]
        )
    latest_runs = {
        "credit-spreads": _latest_attempt(HQM_SOURCE, HQM_DATASET),
        "credit-stress": _latest_attempt(SLOOS_SOURCE, SLOOS_DATASET),
    }
    for candidate in candidates:
        state = _overview_public_state(candidate, latest_runs=latest_runs)
        if state is None:
            continue
        presented = deepcopy(candidate)
        presented.data = deepcopy(candidate.data or {})
        if state != "retained_failure":
            presented.data.pop("refresh_failure", None)
        if state != "current_candidate":
            presented.quality_status = Observation.Quality.STALE
        presented.credit_publication_state = state
        return presented
    return None


def credit_snapshot_is_publicly_displayable(snapshot: DashboardSnapshot) -> bool:
    return select_public_credit_snapshot([snapshot]) is not None


def _mark_parent_failure(
    *, latest_runs: dict[str, IngestionRun], reason_code: str
) -> None:
    if any(run.status == IngestionRun.Status.RUNNING for run in latest_runs.values()):
        return
    candidates = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="credit",
            is_published=True,
            data__contract_version=CREDIT_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")[:50]
    )
    list(
        MetricSnapshot.objects.select_for_update()
        .filter(batch_id__in=[item.batch_id for item in candidates])
        .order_by("batch_id", "key")
        .values_list("pk", flat=True)
    )
    retained = next((item for item in candidates if credit_snapshot_base_is_valid(item)), None)
    if retained is None:
        return
    data = dict(retained.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason_code": reason_code,
        "reason": REFRESH_FAILURE_REASONS[reason_code],
        "attempts": [
            _attempt_reference(latest_runs[key])
            for key in ("credit-spreads", "credit-stress")
        ],
    }
    retained.data = data
    retained.quality_status = Observation.Quality.STALE
    retained.save(update_fields=["data", "quality_status", "updated_at"])


def _publish_credit_overview_revision(
    *,
    children: dict[str, DashboardSnapshot],
    batch_id: uuid.UUID,
    _capability: object | None = None,
) -> DashboardSnapshot:
    if _capability is not _CREDIT_PUBLISH_CAPABILITY:
        raise ValueError("credit overview requires the private dedicated capability")
    data = _build_credit_overview(children, publication_batch_id=batch_id)
    as_of = _parse_datetime(data["as_of"])
    if as_of is None:
        raise ValueError("credit overview as-of is missing")
    _store_metric_rows(page_key="credit", data=data, batch_id=batch_id)
    return DashboardSnapshot.objects.create(
        key="credit",
        title=OVERVIEW_TITLE,
        as_of=as_of,
        batch_id=batch_id,
        quality_status=Observation.Quality.ESTIMATED,
        summary=OVERVIEW_SUMMARY,
        data=data,
        source=ensure_source("internal"),
        is_published=True,
    )


@transaction.atomic
def publish_credit_revision(
    *, children: dict[str, DashboardSnapshot], batch_id: uuid.UUID
) -> DashboardSnapshot:
    if set(children) != {"credit-spreads", "credit-stress"}:
        raise ValueError("credit publisher requires the exact child set")
    latest_runs = {
        "credit-spreads": _latest_attempt(HQM_SOURCE, HQM_DATASET, lock=True),
        "credit-stress": _latest_attempt(SLOOS_SOURCE, SLOOS_DATASET, lock=True),
    }
    if any(run is None for run in latest_runs.values()):
        raise ValueError("credit publisher requires both latest official attempts")
    concrete_runs = {
        key: run for key, run in latest_runs.items() if run is not None
    }
    locked_inputs = _lock_credit_run_set(concrete_runs)
    locked_children = _lock_credit_child_snapshot_rows(children)
    selected = {
        "credit-spreads": select_public_credit_spreads_snapshot(
            [locked_children["credit-spreads"]]
        ),
        "credit-stress": select_public_credit_stress_snapshot(
            [locked_children["credit-stress"]]
        ),
    }
    if any(
        current is None
        or current.pk != children[key].pk
        or getattr(current, "credit_publication_state", None) != "current_candidate"
        for key, current in selected.items()
    ):
        raise ValueError("credit publisher requires both current strict child revisions")
    _assert_credit_run_set_unchanged(concrete_runs, locked_inputs)
    published = _publish_credit_overview_revision(
        children=locked_children,
        batch_id=batch_id,
        _capability=_CREDIT_PUBLISH_CAPABILITY,
    )
    _assert_credit_run_set_unchanged(concrete_runs, locked_inputs)
    return published


_publish_credit_revision = publish_credit_revision


@transaction.atomic
def coordinate_credit_overview() -> tuple[list[DashboardSnapshot], set[str]]:
    for key in (HQM_SOURCE, SLOOS_SOURCE, "internal"):
        ensure_source(key)
    list(
        Source.objects.select_for_update()
        .filter(key__in={HQM_SOURCE, SLOOS_SOURCE, "internal"})
        .order_by("key")
        .values_list("pk", flat=True)
    )
    list(
        SourceLicense.objects.select_for_update()
        .filter(
            source__key__in={HQM_SOURCE, SLOOS_SOURCE, "internal"},
            is_current=True,
        )
        .order_by("source__key")
        .values_list("pk", flat=True)
    )
    latest_runs = {
        "credit-spreads": _latest_attempt(HQM_SOURCE, HQM_DATASET, lock=True),
        "credit-stress": _latest_attempt(SLOOS_SOURCE, SLOOS_DATASET, lock=True),
    }
    if any(run is None for run in latest_runs.values()):
        return [], ({"credit"} if select_public_credit_snapshot() is None else set())
    concrete_runs = {key: value for key, value in latest_runs.items() if value is not None}
    locked_inputs = _lock_credit_run_set(concrete_runs)
    children = {
        "credit-spreads": select_public_credit_spreads_snapshot(),
        "credit-stress": select_public_credit_stress_snapshot(),
    }
    locked_children: dict[str, DashboardSnapshot] | None = None
    if all(child is not None for child in children.values()):
        locked_children = _lock_credit_child_snapshot_rows(
            {key: child for key, child in children.items() if child is not None}
        )
        children = {
            "credit-spreads": select_public_credit_spreads_snapshot(
                [locked_children["credit-spreads"]]
            ),
            "credit-stress": select_public_credit_stress_snapshot(
                [locked_children["credit-stress"]]
            ),
        }
    publishable = bool(
        all(child is not None for child in children.values())
        and all(
            getattr(child, "credit_publication_state", None) == "current_candidate"
            for child in children.values()
        )
        and len(
            {
                str((child.data or {}).get("credit_refresh_id") or "")
                for child in children.values()
            }
        )
        == 1
        and all((child.data or {}).get("credit_refresh_id") for child in children.values())
    )
    if not publishable:
        _assert_credit_run_set_unchanged(concrete_runs, locked_inputs)
        if not any(run.status == IngestionRun.Status.RUNNING for run in concrete_runs.values()):
            reason = (
                "latest-attempt-incomplete"
                if any(
                    run.status != IngestionRun.Status.SUCCESS or run.row_count <= 0
                    for run in concrete_runs.values()
                )
                else "cycle-incomplete"
            )
            _mark_parent_failure(latest_runs=concrete_runs, reason_code=reason)
        selected = select_public_credit_snapshot()
        return [], ({"credit"} if selected is None else set())
    if locked_children is None:
        raise ValueError("credit overview current child locks are missing")
    strict_children = locked_children
    candidates = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="credit",
            is_published=True,
            data__contract_version=CREDIT_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")[:50]
    )
    list(
        MetricSnapshot.objects.select_for_update()
        .filter(batch_id__in=[item.batch_id for item in candidates])
        .order_by("batch_id", "key")
        .values_list("pk", flat=True)
    )
    child_ids = {key: child.pk for key, child in strict_children.items()}
    existing = next(
        (
            item
            for item in candidates
            if credit_snapshot_base_is_valid(item)
            and {
                key: child.pk
                for key, child in (_overview_children(item) or {}).items()
            }
            == child_ids
            and (item.data or {}).get("refresh_failure") is None
        ),
        None,
    )
    if existing is not None:
        return [], set()
    try:
        with transaction.atomic():
            _assert_credit_run_set_unchanged(concrete_runs, locked_inputs)
            published = publish_credit_revision(
                children=strict_children,
                batch_id=uuid.uuid4(),
            )
            _assert_credit_run_set_unchanged(concrete_runs, locked_inputs)
            still_latest = {
                "credit-spreads": _latest_attempt(HQM_SOURCE, HQM_DATASET, lock=True),
                "credit-stress": _latest_attempt(SLOOS_SOURCE, SLOOS_DATASET, lock=True),
            }
            selected = select_public_credit_snapshot()
            if (
                any(
                    still_latest[key] is None
                    or still_latest[key].pk != concrete_runs[key].pk
                    for key in concrete_runs
                )
                or selected is None
                or getattr(selected, "credit_publication_state", None)
                != "current_candidate"
                or selected.pk != published.pk
                or not credit_snapshot_base_is_valid(selected)
            ):
                raise ValueError("credit overview post-publication selector check failed")
    except (
        ArithmeticError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        _mark_parent_failure(
            latest_runs=concrete_runs, reason_code="publication-postcondition"
        )
        selected = select_public_credit_snapshot()
        return [], ({"credit"} if selected is None else set())
    return [published], set()


def coordinate_credit_dashboards(
    trigger_runs: Iterable[IngestionRun] = (),
) -> tuple[list[DashboardSnapshot], set[str]]:
    """Publish both strict children, then atomically compose their same-cycle parent."""

    runs = list(trigger_runs)
    published: list[DashboardSnapshot] = []
    unavailable: set[str] = set()
    for page_key in ("credit-spreads", "credit-stress"):
        child_published, child_unavailable = coordinate_credit_child(page_key, runs)
        published.extend(child_published)
        unavailable.update(child_unavailable)
    parent_published, parent_unavailable = coordinate_credit_overview()
    published.extend(parent_published)
    unavailable.update(parent_unavailable)
    return published, unavailable


# Public aliases keep naming consistent with other strict page families.
_coordinate_credit_dashboard = coordinate_credit_overview
_coordinate_credit_dashboards = coordinate_credit_dashboards


def _coordinate_credit_spreads_dashboard(
    runs: Iterable[IngestionRun] = (),
) -> tuple[list[DashboardSnapshot], set[str]]:
    return coordinate_credit_child("credit-spreads", runs)


def _coordinate_credit_stress_dashboard(
    runs: Iterable[IngestionRun] = (),
) -> tuple[list[DashboardSnapshot], set[str]]:
    return coordinate_credit_child("credit-stress", runs)
