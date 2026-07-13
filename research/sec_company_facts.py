"""Clean-room SEC company-facts ingestion, normalization, and publication.

The module deliberately separates immutable SEC observations from the small
publication projection used by the public pages.  The normalizer is pure and
accepts only annual USD us-gaap facts with the reviewed fiscal-year endings.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    Company,
    DashboardSnapshot,
    FinancialFact,
    IngestionRun,
    Observation,
    RawArtifact,
    SECCompanyFact,
    Source,
    SourceLicense,
    SupplyChainNode,
)
from .providers import ProviderResult, SECProvider
from .services import begin_ingestion, ensure_source, finish_ingestion


@dataclass(frozen=True, slots=True)
class ReviewedCompany:
    slug: str
    cik: str
    ticker: str
    fiscal_year_end_month: int
    fiscal_year_end_day: int
    name: str
    name_en: str
    description: str
    investor_relations_url: str
    exchange: str = "Nasdaq"
    country: str = "USA"
    currency: str = "USD"
    node_slug: str = "cloud-providers"

    @property
    def normalized_cik(self) -> str:
        return str(self.cik).zfill(10)


REVIEWED_COMPANIES: tuple[ReviewedCompany, ...] = (
    ReviewedCompany(
        "microsoft", "0000789019", "MSFT", 6, 30, "Microsoft", "Microsoft",
        "软件、云服务与数据中心基础设施公司。", "https://www.microsoft.com/en-us/Investor/",
    ),
    ReviewedCompany(
        "alphabet", "0001652044", "GOOGL", 12, 31, "Alphabet", "Alphabet",
        "互联网、云服务与人工智能产品公司。", "https://abc.xyz/investor/",
    ),
    ReviewedCompany(
        "amazon", "0001018724", "AMZN", 12, 31, "Amazon", "Amazon",
        "电商、云服务与数字广告公司。", "https://www.amazon.com/ir",
    ),
    ReviewedCompany(
        "meta", "0001326801", "META", 12, 31, "Meta Platforms", "Meta Platforms",
        "社交平台、广告与计算基础设施公司。", "https://investor.atmeta.com/",
    ),
)

# Explicit aliases make the reviewed boundary easy for callers and tests to
# discover without allowing an environment variable to expand it silently.
SEC_COMPANY_ALLOWLIST = {item.slug: item for item in REVIEWED_COMPANIES}
SEC_COMPANIES = SEC_COMPANY_ALLOWLIST
REVIEWED_COMPANY_SLUGS = frozenset(SEC_COMPANY_ALLOWLIST)
REVIEWED_COMPANY_CIKS = frozenset(item.normalized_cik for item in REVIEWED_COMPANIES)

REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
NET_INCOME_CONCEPT = "NetIncomeLoss"
CFO_CONCEPTS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
COST_CONCEPTS = ("CostOfRevenue", "CostOfGoodsAndServicesSold")
CAPEX_CONCEPTS = {
    "amazon": ("PaymentsToAcquireProductiveAssets", "sec-cash-productive-assets"),
    "default": ("PaymentsToAcquirePropertyPlantAndEquipment", "sec-cash-ppe"),
}


class NormalizedFacts(list[dict[str, Any]]):
    """List-compatible normalizer result with rejection diagnostics."""

    def __init__(self, rows: list[dict[str, Any]], diagnostics: dict[str, Any]):
        super().__init__(rows)
        self.diagnostics = diagnostics


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _as_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fact_identity_hash(
    *, company: ReviewedCompany, concept: str, unit: str, value: Decimal,
    period_start: date, period_end: date, form: str, filed_at: date,
    accession_number: str, frame: str = "",
) -> str:
    identity = {
        "cik": company.normalized_cik,
        "concept": concept,
        "unit": unit,
        "value": str(value),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "form": form,
        "filed_at": filed_at.isoformat(),
        "accession_number": accession_number,
        "frame": frame,
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def normalize_annual_facts(
    payload: Mapping[str, Any], company: ReviewedCompany | str,
) -> NormalizedFacts:
    """Normalize a SEC companyfacts payload without database or network I/O.

    Returned rows are accepted narrow facts, not selected projections.  Every
    rejected row is represented in diagnostics, allowing ingestion runs to
    explain why quarterly, YTD, non-USD, and malformed values were excluded.
    """

    spec = SEC_COMPANY_ALLOWLIST[company] if isinstance(company, str) else company
    facts = payload.get("facts", {}) if isinstance(payload, Mapping) else {}
    us_gaap = facts.get("us-gaap", {}) if isinstance(facts, Mapping) else {}
    wanted = set(REVENUE_CONCEPTS) | {NET_INCOME_CONCEPT, "GrossProfit", *CFO_CONCEPTS, *COST_CONCEPTS}
    wanted.add(CAPEX_CONCEPTS.get(spec.slug, CAPEX_CONCEPTS["default"])[0])
    rows: list[dict[str, Any]] = []
    rejected: dict[str, int] = defaultdict(int)
    for concept in sorted(wanted):
        concept_payload = us_gaap.get(concept, {})
        units = concept_payload.get("units", {}) if isinstance(concept_payload, Mapping) else {}
        for unit, unit_rows in units.items():
            if unit != "USD":
                rejected["non_usd"] += len(unit_rows) if isinstance(unit_rows, list) else 1
                continue
            for raw in unit_rows if isinstance(unit_rows, list) else []:
                if raw.get("form") not in {"10-K", "10-K/A"}:
                    rejected["non_annual_form"] += 1
                    continue
                if raw.get("fp") != "FY":
                    rejected["non_fy"] += 1
                    continue
                start = _as_date(raw.get("start"))
                end = _as_date(raw.get("end"))
                filed = _as_date(raw.get("filed"))
                value = _as_decimal(raw.get("val"))
                if not start or not end or not filed or value is None:
                    rejected["invalid_date_or_value"] += 1
                    continue
                duration = (end - start).days
                if not 300 <= duration <= 400:
                    rejected["invalid_duration"] += 1
                    continue
                expected = date(end.year, spec.fiscal_year_end_month, spec.fiscal_year_end_day)
                if abs((end - expected).days) > 7:
                    rejected["wrong_fiscal_year_end"] += 1
                    continue
                accession = str(raw.get("accn") or "").strip()
                if not accession:
                    rejected["missing_accession"] += 1
                    continue
                frame = str(raw.get("frame") or "")
                row = {
                    "company_slug": spec.slug,
                    "cik": spec.normalized_cik,
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "unit": "USD",
                    "value": str(value),
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "fiscal_year": end.year,
                    "fiscal_period": "FY",
                    "form": raw["form"],
                    "filed_at": filed.isoformat(),
                    "accession_number": accession,
                    "frame": frame,
                    "identity_hash": fact_identity_hash(
                        company=spec, concept=concept, unit="USD", value=value,
                        period_start=start, period_end=end, form=raw["form"],
                        filed_at=filed, accession_number=accession, frame=frame,
                    ),
                    "metadata": {"raw_fy": raw.get("fy"), "raw_fp": raw.get("fp"), "accepted": True},
                }
                rows.append(row)
    diagnostics = {
        "accepted_count": len(rows),
        "rejected": dict(sorted(rejected.items())),
        "accepted_concepts": sorted({row["concept"] for row in rows}),
        "rules": {"taxonomy": "us-gaap", "unit": "USD", "forms": ["10-K", "10-K/A"], "fp": "FY"},
    }
    return NormalizedFacts(rows, diagnostics)


def _selection_key(row: dict[str, Any], priority: int) -> tuple[str, int, str]:
    return (str(row["filed_at"]), -priority, str(row["accession_number"]))


def _pick(rows: list[dict[str, Any]], concepts: tuple[str, ...]) -> dict[str, Any] | None:
    candidates = [row for row in rows if row["concept"] in concepts]
    if not candidates:
        return None
    return max(candidates, key=lambda row: _selection_key(row, concepts.index(row["concept"])))


def is_exact_reviewed_sec_identity(
    *, slug: str, sec_cik: str, source_key: str | None = None
) -> bool:
    """Return whether a company uses one reviewed slug/CIK identity."""

    spec = SEC_COMPANY_ALLOWLIST.get(str(slug))
    if spec is None or str(sec_cik) != spec.normalized_cik:
        return False
    return source_key is None or source_key == "sec"


def is_exact_reviewed_sec_company(company: Company) -> bool:
    source_key = getattr(getattr(company, "source", None), "key", None)
    return is_exact_reviewed_sec_identity(
        slug=company.slug, sec_cik=company.sec_cik, source_key=source_key or ""
    )


class SelectedMetrics(dict[int, dict[str, Any]]):
    """Selected publication window plus the diagnostics that explain it."""

    def __init__(self, *args: Any, diagnostics: dict[str, Any] | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.diagnostics = diagnostics or {}


def select_annual_metrics(
    normalized: list[dict[str, Any]], company: ReviewedCompany | str,
) -> SelectedMetrics:
    """Select the newest exactly-five-year consecutive publication window.

    Accepted facts remain in ``normalized``.  An incomplete latest observed
    year blocks selection entirely so an older window cannot be presented as
    a successful refresh.
    """

    spec = SEC_COMPANY_ALLOWLIST[company] if isinstance(company, str) else company
    by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized:
        by_year[int(row["fiscal_year"])].append(row)
    complete_by_year: dict[int, dict[str, Any]] = {}
    incomplete_years: list[int] = []
    capex_concept, capex_definition = CAPEX_CONCEPTS.get(spec.slug, CAPEX_CONCEPTS["default"])
    for year, rows in sorted(by_year.items()):
        revenue = _pick(rows, REVENUE_CONCEPTS)
        net_income = _pick(rows, (NET_INCOME_CONCEPT,))
        cfo = _pick(rows, (CFO_CONCEPTS[0],)) or _pick(rows, (CFO_CONCEPTS[1],))
        capex = _pick(rows, (capex_concept,))
        gross = _pick(rows, ("GrossProfit",))
        derivation: dict[str, Any] | None = None
        if gross is None and revenue is not None:
            cost_candidates = []
            for cost in rows:
                if cost["concept"] not in COST_CONCEPTS:
                    continue
                if any(
                    cost[field] != revenue[field]
                    for field in ("period_start", "period_end", "accession_number", "form")
                ):
                    continue
                cost_candidates.append(cost)
            if cost_candidates:
                cost = max(
                    cost_candidates,
                    key=lambda row: _selection_key(
                        row, COST_CONCEPTS.index(row["concept"])
                    ),
                )
                gross = {
                    **revenue,
                    "concept": "GrossProfitDerived",
                    "value": str(Decimal(revenue["value"]) - Decimal(cost["value"])),
                    "identity_hash": "",
                }
                derivation = {
                    "method": "revenue-minus-cost",
                    "input_concepts": [revenue["concept"], cost["concept"]],
                    "input_identity_hashes": [revenue["identity_hash"], cost["identity_hash"]],
                }
        if not all((revenue, gross, net_income, cfo, capex)):
            incomplete_years.append(year)
            continue
        complete_by_year[year] = {
            "revenue": revenue,
            "gross_profit": gross,
            "net_income": net_income,
            "cfo": cfo,
            "capex": capex,
            "capex_definition": capex_definition,
            "gross_profit_derivation": derivation,
            "selection_diagnostics": {
                "newest_filed_wins": True,
                "selected_concepts": {key: value["concept"] for key, value in {
                    "revenue": revenue, "gross_profit": gross, "net_income": net_income,
                    "cfo": cfo, "capex": capex,
                }.items()},
            },
        }
    latest_observed = max(by_year, default=None)
    latest_observed_year_incomplete = (
        latest_observed is not None and latest_observed not in complete_by_year
    )
    if latest_observed_year_incomplete:
        return SelectedMetrics(
            diagnostics={
                "selected_years": [],
                "latest_observed_fiscal_year": latest_observed,
                "latest_observed_year_incomplete": True,
                "incomplete_years": sorted(incomplete_years),
            }
        )
    latest_window = (
        list(range(latest_observed - 4, latest_observed + 1))
        if latest_observed is not None
        else []
    )
    windows = [latest_window] if latest_window and all(
        year in complete_by_year for year in latest_window
    ) else []
    if not windows:
        return SelectedMetrics(
            diagnostics={
                "selected_years": [],
                "latest_observed_fiscal_year": latest_observed,
                "latest_observed_year_incomplete": False,
                "incomplete_years": sorted(incomplete_years),
                "consecutive_windows": [],
                "latest_window_years": latest_window,
                "latest_window_complete": False,
            }
        )
    selected_years = latest_window
    selected = SelectedMetrics(
        {year: complete_by_year[year] for year in selected_years},
        diagnostics={
            "selected_years": selected_years,
            "latest_observed_fiscal_year": latest_observed,
            "latest_observed_year_incomplete": False,
            "incomplete_years": sorted(incomplete_years),
            "consecutive_windows": windows,
            "latest_window_years": latest_window,
            "latest_window_complete": True,
        },
    )
    for year in selected_years:
        selected[year]["selection_diagnostics"] = {
            **selected[year]["selection_diagnostics"],
            "window_years": selected_years,
        }
    return selected


def complete_five_year_metrics(metrics: Mapping[int, Any]) -> bool:
    diagnostics = getattr(metrics, "diagnostics", {})
    if not diagnostics or diagnostics.get("latest_observed_year_incomplete"):
        return False
    years = sorted(int(year) for year in metrics)
    latest_observed = diagnostics.get("latest_observed_fiscal_year")
    selected_years = diagnostics.get("selected_years")
    return bool(
        len(years) == 5
        and years == list(range(years[0], years[-1] + 1))
        and latest_observed == years[-1]
        and selected_years == years
        and diagnostics.get("latest_window_complete") is True
    )


def _raw_bytes(result: ProviderResult) -> bytes:
    if not isinstance(result.raw_bytes, (bytes, bytearray)) or not result.raw_bytes:
        raise ValueError("SEC ingestion requires non-empty exact response bytes")
    return bytes(result.raw_bytes)


def persist_raw_artifact(*, run: IngestionRun, result: ProviderResult) -> RawArtifact:
    """Atomically write exact response bytes to a private content-addressed path."""

    payload = _raw_bytes(result)
    digest = hashlib.sha256(payload).hexdigest()
    metadata = result.metadata
    if "byte_length" in metadata:
        try:
            declared_length = int(metadata["byte_length"])
        except (TypeError, ValueError) as exc:
            raise ValueError("SEC provider byte_length metadata is invalid") from exc
        if declared_length != len(payload):
            raise ValueError("SEC provider byte_length does not match raw response bytes")
    if "sha256" in metadata and str(metadata["sha256"]).lower() != digest:
        raise ValueError("SEC provider sha256 does not match raw response bytes")
    root = Path(getattr(settings, "RAW_ARTIFACT_ROOT", settings.BASE_DIR / "data" / "artifacts"))
    target = root / digest[:2] / f"{digest}.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != payload:
            raise ValueError("existing content-addressed SEC artifact bytes do not match digest")
    else:
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    try:
        return RawArtifact.objects.create(
            run=run,
            uri=f"private://sec/{digest[:2]}/{digest}.bin",
            sha256=digest,
            content_type=str(metadata.get("content_type") or "application/json"),
            size_bytes=len(payload),
        )
    except Exception:
        # The content-addressed file is intentionally retained.  A later
        # locked garbage-collection pass may remove bytes with no references.
        raise


def _remove_orphan_artifact(digest: str, path: Path | None = None) -> None:
    """Compatibility no-op; orphan cleanup belongs to a separate safe GC job."""

    return None


def _ensure_company(spec: ReviewedCompany, source: Source) -> Company:
    node, _ = SupplyChainNode.objects.get_or_create(
        slug=spec.node_slug,
        defaults={
            "name": "云服务商",
            "layer": "cloud",
            "description": "云计算与企业基础设施公司。",
            "source_note": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        },
    )
    company, _ = Company.objects.get_or_create(
        slug=spec.slug,
        defaults={
            "name": spec.name,
            "name_en": spec.name_en,
            "ticker": spec.ticker,
            "exchange": spec.exchange,
            "country": spec.country,
            "currency": spec.currency,
            "primary_node": node,
            "description": spec.description,
            "data_source_note": "SEC EDGAR annual company facts and first-party investor relations",
            "investor_relations_url": spec.investor_relations_url,
            "sec_cik": spec.normalized_cik,
            "source": source,
            "is_published": False,
        },
    )
    # Catalog synchronization may enrich an unpublished row, but never grants
    # publication to a row that did not pass a complete batch.
    fields = {
        "name": spec.name, "name_en": spec.name_en, "ticker": spec.ticker,
        "exchange": spec.exchange, "country": spec.country, "currency": spec.currency,
        "primary_node": node, "description": spec.description,
        "data_source_note": "SEC EDGAR annual company facts and first-party investor relations",
        "investor_relations_url": spec.investor_relations_url, "sec_cik": spec.normalized_cik,
        "source": source,
    }
    changed = []
    for field, value in fields.items():
        if getattr(company, field) != value:
            setattr(company, field, value)
            changed.append(field)
    if changed:
        company.save(update_fields=changed + ["updated_at"])
    return company


def _validate_submission(payload: Mapping[str, Any], spec: ReviewedCompany) -> None:
    cik = SECProvider.normalize_cik(payload.get("cik") or payload.get("cik_str") or "")
    if cik != spec.normalized_cik:
        raise ValueError("SEC submission CIK does not match reviewed company")
    tickers = {str(value).upper() for value in payload.get("tickers", [])}
    if spec.ticker not in tickers:
        raise ValueError("SEC submission ticker does not match reviewed company")
    if not str(payload.get("name") or payload.get("entityName") or "").strip():
        raise ValueError("SEC submission entity name is missing")


def _validate_companyfacts(payload: Mapping[str, Any], spec: ReviewedCompany) -> None:
    cik = SECProvider.normalize_cik(payload.get("cik") or payload.get("cik_str") or "")
    if cik != spec.normalized_cik:
        raise ValueError("SEC companyfacts CIK does not match reviewed company")
    entity_name = str(payload.get("entityName") or payload.get("name") or "").strip().lower()
    if not entity_name or spec.name.lower() not in entity_name:
        raise ValueError("SEC companyfacts entity name does not match reviewed company")


def _persist_facts(
    *, company: Company, spec: ReviewedCompany, result: ProviderResult,
    run: IngestionRun, artifact: RawArtifact, source: Source, license_row: SourceLicense,
) -> tuple[int, NormalizedFacts, SelectedMetrics]:
    payload = result.records[0] if result.records else {}
    _validate_companyfacts(payload, spec)
    normalized = normalize_annual_facts(payload, spec)
    for row in normalized:
        SECCompanyFact.objects.get_or_create(
            identity_hash=row["identity_hash"],
            defaults={
                "company": company,
                "source": source,
                "source_license": license_row,
                "ingestion_run": run,
                "raw_artifact": artifact,
                "taxonomy": row["taxonomy"],
                "concept": row["concept"],
                "unit": row["unit"],
                "value": Decimal(row["value"]),
                "period_start": date.fromisoformat(row["period_start"]),
                "period_end": date.fromisoformat(row["period_end"]),
                "fiscal_year": row["fiscal_year"],
                "fiscal_period": row["fiscal_period"],
                "form": row["form"],
                "filed_at": date.fromisoformat(row["filed_at"]),
                "accession_number": row["accession_number"],
                "frame": row["frame"],
                "fetched_at": result.fetched_at,
                "quality_status": Observation.Quality.FRESH,
                "license_scope": license_row.scope,
                "metadata": row["metadata"],
            },
        )
    return len(normalized), normalized, select_annual_metrics(normalized, spec)


def _effective_sec_license_queryset(source: Source, *, for_update: bool = False):
    today = timezone.localdate()
    queryset = (
        SourceLicense.objects.filter(
            source_id=source.pk,
            is_current=True,
            status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
        )
        .filter(
            models_q_valid_from(today),
            models_q_valid_until(today),
        )
        .order_by("-created_at", "-pk")
    )
    return queryset.select_for_update() if for_update else queryset


def models_q_valid_from(today: date):
    from django.db.models import Q

    return Q(valid_from__isnull=True) | Q(valid_from__lte=today)


def models_q_valid_until(today: date):
    from django.db.models import Q

    return Q(valid_until__isnull=True) | Q(valid_until__gte=today)


def _current_sec_license(
    source: Source, *, for_update: bool = False
) -> SourceLicense | None:
    return _effective_sec_license_queryset(source, for_update=for_update).first()


def _license_is_public(license_row: SourceLicense | None) -> bool:
    if license_row is None:
        return False
    return (
        license_row.is_current
        and license_row.status in {Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED}
        and license_row.public_display_allowed
        and (license_row.valid_from is None or license_row.valid_from <= timezone.localdate())
        and (license_row.valid_until is None or license_row.valid_until >= timezone.localdate())
    )


def _license_is_storage_allowed(license_row: SourceLicense | None) -> bool:
    """Return whether exact historical bytes may be retained for SEC refresh."""

    if license_row is None:
        return False
    today = timezone.localdate()
    return bool(
        license_row.is_current
        and license_row.status in {Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED}
        and license_row.historical_storage_allowed
        and (license_row.valid_from is None or license_row.valid_from <= today)
        and (license_row.valid_until is None or license_row.valid_until >= today)
    )


def _license_is_publishable(license_row: SourceLicense | None) -> bool:
    return bool(license_row and _license_is_public(license_row)
                and license_row.derived_display_allowed)


def _mark_refresh_failure(
    *, failure: dict[str, Any], companies: list[Company], requested_slugs: list[str] | None = None
) -> None:
    # The publication transaction may have rolled back new UUIDs. Re-read the
    # active rows so stale marking can never target an in-memory rolled-back
    # batch.
    requested = (
        set(SEC_COMPANY_ALLOWLIST) if requested_slugs is None else set(requested_slugs)
    )
    active_companies = (
        Company.objects.filter(
            is_published=True,
            source__key="sec",
            slug__in=requested,
            publication_batch_id__isnull=False,
        )
        .select_related("source")
        .select_for_update()
    )
    active_batch_ids: set[uuid.UUID] = set()
    for company in active_companies:
        if not is_exact_reviewed_sec_company(company):
            continue
        batch_id = company.publication_batch_id
        if batch_id is not None:
            active_batch_ids.add(batch_id)
        company.quality_status = Observation.Quality.STALE
        company.save(update_fields=["quality_status", "updated_at"])
        company.financials.filter(publication_batch_id=batch_id).update(
            quality_status=Observation.Quality.STALE
        )
    current = (
        DashboardSnapshot.objects.filter(
            key="supply-chain-demand",
            is_published=True,
            source__key="sec",
            batch_id__in=active_batch_ids,
        )
        .select_for_update()
        .order_by("-created_at", "-as_of")
        .first()
    )
    if current is not None:
        data = dict(current.data or {})
        _mark_snapshot_payload_stale(data)
        data["refresh_failure"] = failure
        current.data = data
        current.quality_status = Observation.Quality.STALE
        current.save(update_fields=["data", "quality_status", "updated_at"])


def _mark_snapshot_payload_stale(value: Any) -> None:
    """Mark retained component quality without changing its historical values."""

    if isinstance(value, list):
        for item in value:
            _mark_snapshot_payload_stale(item)
        return
    if not isinstance(value, dict):
        return
    quality_status = value.get("quality_status")
    if isinstance(quality_status, str) and quality_status in {
        Observation.Quality.FRESH,
        Observation.Quality.STALE,
    }:
        value["quality_status"] = Observation.Quality.STALE
    quality_value = value.get("quality")
    if isinstance(quality_value, str) and quality_value in {
        Observation.Quality.FRESH,
        Observation.Quality.STALE,
    }:
        value["quality"] = Observation.Quality.STALE
    quality_cell = quality_value
    if isinstance(quality_cell, dict) and isinstance(quality_cell.get("value"), str):
        quality_cell["value"] = quality_cell["value"].replace(
            Observation.Quality.FRESH,
            Observation.Quality.STALE,
            1,
        )
    for item in value.values():
        _mark_snapshot_payload_stale(item)


def _failure(reason: str, details: list[dict[str, Any]]) -> dict[str, Any]:
    return {"reason": reason[:240], "sources": details[:20], "sanitized": True}


def _projection_source_url(spec: ReviewedCompany, row: dict[str, Any]) -> str:
    accession = row["accession_number"].replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(spec.normalized_cik)}/{accession}/"


def _fact_url(spec: ReviewedCompany, row: dict[str, Any]) -> str:
    return _projection_source_url(spec, row)


def _fact_record(
    *, row: dict[str, Any], facts: dict[str, SECCompanyFact], run: IngestionRun,
    artifact: RawArtifact, fetched_at: Any, license_row: SourceLicense,
) -> dict[str, Any]:
    identity = str(row.get("identity_hash") or "")
    fact = facts.get(identity)
    if fact is None:
        raise ValueError(f"current SEC fact is missing: {identity}")
    return {
        "source_key": fact.source.key,
        "source_fact_id": fact.pk,
        "source_fact_identity": fact.identity_hash,
        "ingestion_run_batch_id": str(run.batch_id),
        "raw_artifact_id": artifact.pk,
        "raw_artifact_sha256": artifact.sha256,
        "raw_artifact_uri": artifact.uri,
        "period_start": fact.period_start.isoformat(),
        "period_end": fact.period_end.isoformat(),
        "form": fact.form,
        "accession_number": fact.accession_number,
        "filed_at": fact.filed_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "quality": fact.quality_status,
        "quality_status": fact.quality_status,
        "license_scope": license_row.scope,
        "fallback_source": None,
    }


def _build_chart(
    *, key: str, title: str, unit: str, rows: list[dict[str, Any]], specs: list[ReviewedCompany],
    value_key: str, source: Source, fetched_at_by_slug: Mapping[str, Any],
    batch_id: uuid.UUID, license_scope: str,
) -> dict[str, Any]:
    years = sorted({int(row["fiscal_year"]) for row in rows})
    by_company_year = {(row["company"], int(row["fiscal_year"])): row for row in rows}
    labels = [f"FY{year}" for year in years]
    series = []
    series_lineage: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        values = []
        lineage = []
        for year in years:
            row = by_company_year.get((spec.slug, year), {})
            value = row.get(value_key)
            values.append(float(value) if value is not None else None)
            if row:
                lineage.append(
                    {
                        "company": spec.slug,
                        "fiscal_year": year,
                        "value_date": row.get("period_end"),
                        "fetched_at": row.get("fetched_at"),
                        "publication_batch_id": row.get("publication_batch_id", str(batch_id)),
                        "quality_status": row.get("quality_status", row.get("quality")),
                        "license_scope": row.get("license_scope", license_scope),
                        "fallback_source": row.get("fallback_source"),
                        "source_key": row.get("source_key", source.key),
                        "source_fact_ids": row.get("source_fact_ids", {}),
                    }
                )
        series_lineage[spec.slug] = lineage
        series.append(
            {"name": spec.name, "company": spec.slug, "data": values, "lineage": lineage}
        )
    lineage_rows = []
    for year in years:
        year_rows = [by_company_year[(spec.slug, year)] for spec in specs if (spec.slug, year) in by_company_year]
        latest_period = max((row["period_end"] for row in year_rows), default=None)
        lineage_rows.append({
            "fiscal_year": year,
            "period_end": latest_period,
            "date": latest_period,
            "lineage": year_rows,
        })
    latest_period = max((row["period_end"] for row in rows), default=None)
    component_fetched_at = max(
        (fetched_at_by_slug[spec.slug] for spec in specs if spec.slug in fetched_at_by_slug),
        default=None,
    )
    return {
        "key": key,
        "title": title,
        "kind": "line",
        "time_axis": "date",
        "unit": unit,
        "data": {"labels": labels, "series": series, "_rows": lineage_rows, "time_axis": "date", "unit": unit},
        "source_key": source.key,
        "source_keys": [source.key],
        "source_name": source.name,
        "as_of": latest_period,
        "value_date": latest_period,
        "fetched_at": component_fetched_at.isoformat() if component_fetched_at else None,
        "quality_status": Observation.Quality.FRESH,
        "publication_batch_id": str(batch_id),
        "license_scope": license_scope,
        "fallback_source": None,
        "lineage": series_lineage,
    }


def _table_row(row: dict[str, Any], *, source: Source, batch_id: uuid.UUID) -> dict[str, Any]:
    cells = {
        "company": {"kind": "text", "value": row["company"]},
        "period": {"kind": "text", "value": f"FY{row['fiscal_year']} / {row['period_end']}"},
        "revenue": {"kind": "text", "value": f"${row['values_usd_m']['revenue']}m"},
        "gross": {"kind": "text", "value": f"${row['values_usd_m']['gross_profit']}m / {row['derived_metrics']['gross_margin']:.2f}%"},
        "net_income": {"kind": "text", "value": f"${row['values_usd_m']['net_income']}m"},
        "cfo": {"kind": "text", "value": f"${row['values_usd_m']['cfo']}m"},
        "capex": {"kind": "text", "value": f"${row['values_usd_m']['capital_expenditures']}m"},
        "capex_ratio": {"kind": "text", "value": f"{row['derived_metrics']['capex_revenue']:.2f}%"},
        "definition": {"kind": "text", "value": row["capex_definition_label"]},
        "filing": {"kind": "url", "label": f"{row['capex_source']['accession_number']} · {row['capex_source']['form']} · filed {row['capex_source']['filed_at']}", "href": row["capex_source_url"]},
        "fetched": {"kind": "text", "value": row["fetched_at"]},
        "quality": {"kind": "badge", "value": f"{row['quality_status']} · batch {batch_id} · {source.name}"},
    }
    return {
        "cells": cells,
        "cells_list": [
            {"label": label, "cell": cells[key]}
            for key, label in (
                ("company", "公司"), ("period", "财年 / 期末"), ("revenue", "收入"),
                ("gross", "毛利 / 毛利率"), ("net_income", "净利润"), ("cfo", "CFO"),
                ("capex", "现金资本开支"), ("capex_ratio", "CapEx/收入"),
                ("definition", "定义"), ("filing", "申报"), ("fetched", "抓取"),
                ("quality", "质量 / 批次 / 来源"),
            )
        ],
        "lineage": row["lineage"],
    }


DEMAND_CHART_KEYS = frozenset(
    {"reported-capex", "capex-intensity", "financial-capacity"}
)


def _is_numeric_payload(value: Any) -> bool:
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _contains_fallback(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key in ("fallback_source", "fallback_source_key"):
            if value.get(key) not in (None, "", False):
                return True
        return any(_contains_fallback(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_fallback(item) for item in value)
    return False


def validate_public_supply_chain_demand_snapshot(snapshot: DashboardSnapshot) -> list[str]:
    """Return contract violations for a public SEC demand snapshot.

    This is intentionally shared by publication consumers and tests.  An
    empty list means the snapshot is complete, numerically real, and safe to
    expose under the current public-and-derived licence.
    """

    errors: list[str] = []
    data = snapshot.data if isinstance(snapshot.data, Mapping) else {}
    required_slugs = tuple(item.slug for item in REVIEWED_COMPANIES)
    if snapshot.key != "supply-chain-demand":
        errors.append("snapshot key is not supply-chain-demand")
    if not snapshot.is_published:
        errors.append("snapshot is not published")
    if snapshot.source_id is None or getattr(snapshot.source, "key", None) != "sec":
        errors.append("snapshot source is not sec")
    license_row = (
        _current_sec_license(snapshot.source)
        if snapshot.source_id is not None and getattr(snapshot.source, "key", None) == "sec"
        else None
    )
    if not _license_is_publishable(license_row):
        errors.append("SEC public-and-derived licence is not effective")
    if snapshot.quality_status not in {Observation.Quality.FRESH, Observation.Quality.STALE}:
        errors.append("snapshot quality is not fresh or stale")
    if data.get("contract_version") != 1:
        errors.append("contract_version is not 1")
    if data.get("complete") is not True:
        errors.append("snapshot is not marked complete")
    if str(data.get("publication_batch_id") or "") != str(snapshot.batch_id):
        errors.append("snapshot publication batch lineage is missing")
    published_slugs = data.get("required_company_slugs", ())
    if (
        not isinstance(published_slugs, (list, tuple))
        or len(published_slugs) != len(required_slugs)
        or set(published_slugs) != set(required_slugs)
    ):
        errors.append("required company slugs are not the reviewed four")
    if set(data.get("source_keys", ())) != {"sec"}:
        errors.append("snapshot source keys are incomplete")
    if _contains_fallback(data):
        errors.append("snapshot contains fallback lineage")

    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) != 20:
        errors.append("snapshot does not contain exactly 20 rows")
        rows = rows if isinstance(rows, list) else []
    by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    snapshot_batch_id = str(snapshot.batch_id)
    for row in rows:
        if not isinstance(row, Mapping):
            errors.append("snapshot row is not an object")
            continue
        slug = str(row.get("company") or "")
        if slug not in required_slugs:
            errors.append("snapshot row uses an unreviewed company")
            continue
        by_company[slug].append(dict(row))
        if str(row.get("publication_batch_id") or row.get("batch_id") or "") != snapshot_batch_id:
            errors.append("snapshot row batch differs from snapshot batch")
        if (
            row.get("source_key") != "sec"
            or not row.get("source_fact_ids")
            or not row.get("fetched_at")
            or not row.get("license_scope")
            or row.get("fallback_source") not in (None, "")
        ):
            errors.append("snapshot row source lineage is missing")
        if row.get("quality_status", row.get("quality")) not in {
            Observation.Quality.FRESH,
            Observation.Quality.STALE,
        }:
            errors.append("snapshot row quality is not acceptable")
        values = row.get("values_usd_m")
        if not isinstance(values, Mapping) or any(
            not _is_numeric_payload(values.get(key))
            for key in ("revenue", "gross_profit", "net_income", "cfo", "capital_expenditures")
        ):
            errors.append("snapshot row values are not numeric")
        if not isinstance(row.get("lineage"), Mapping) or not row.get("lineage"):
            errors.append("snapshot row lineage is missing")

    diagnostics = data.get("selection_diagnostics")
    if not isinstance(diagnostics, Mapping):
        errors.append("snapshot selection diagnostics are missing")
        diagnostics = {}
    for slug in required_slugs:
        company_rows = by_company.get(slug, [])
        if len(company_rows) != 5:
            errors.append(f"snapshot does not contain five rows for {slug}")
            continue
        try:
            years = sorted(
                int(row["fiscal_year"])
                for row in company_rows
                if row.get("fiscal_year") is not None
            )
        except (TypeError, ValueError, KeyError):
            years = []
        company_diagnostics = diagnostics.get(slug)
        latest_observed = (
            company_diagnostics.get("latest_observed_fiscal_year")
            if isinstance(company_diagnostics, Mapping)
            else None
        )
        if len(years) != 5 or len(set(years)) != 5 or years != list(range(years[0], years[-1] + 1)):
            errors.append(f"snapshot years are not consecutive for {slug}")
        try:
            latest_observed_year = int(latest_observed)
        except (TypeError, ValueError):
            latest_observed_year = None
        if latest_observed_year is None or years[-1:] != [latest_observed_year]:
            errors.append(f"snapshot years do not end at latest observed year for {slug}")
        if (
            not isinstance(company_diagnostics, Mapping)
            or company_diagnostics.get("latest_observed_year_incomplete") is True
            or company_diagnostics.get("latest_window_complete") is not True
        ):
            errors.append(f"snapshot selection window is not complete for {slug}")

    metrics = data.get("metrics")
    if not isinstance(metrics, list) or len(metrics) != 4:
        errors.append("snapshot does not contain four KPI cards")
    else:
        metric_slugs = {str(item.get("company") or "") for item in metrics if isinstance(item, Mapping)}
        if metric_slugs != set(required_slugs):
            errors.append("KPI company lineage is incomplete")
        for metric in metrics:
            if not isinstance(metric, Mapping):
                errors.append("KPI is not an object")
                continue
            if not _is_numeric_payload(metric.get("value")):
                errors.append("KPI value is not numeric")
            if (
                not metric.get("source")
                or metric.get("source_key") != "sec"
                or metric.get("as_of") is None
                or metric.get("value_date") is None
            ):
                errors.append("KPI source/as_of keys are missing")
            if str(metric.get("publication_batch_id") or metric.get("batch_id") or "") != snapshot_batch_id:
                errors.append("KPI batch lineage is missing")
            if not metric.get("fetched_at") or not metric.get("license_scope"):
                errors.append("KPI fetched/licence lineage is missing")
            if metric.get("fallback_source") not in (None, ""):
                errors.append("KPI contains fallback lineage")

    charts = data.get("charts")
    if not isinstance(charts, list) or len(charts) != 3:
        errors.append("snapshot does not contain exactly three charts")
        charts = charts if isinstance(charts, list) else []
    chart_keys = {str(chart.get("key") or "") for chart in charts if isinstance(chart, Mapping)}
    if chart_keys != set(DEMAND_CHART_KEYS):
        errors.append("expected chart keys are incomplete")
    for chart in charts:
        if not isinstance(chart, Mapping):
            errors.append("chart is not an object")
            continue
        chart_data = chart.get("data")
        series = chart_data.get("series") if isinstance(chart_data, Mapping) else None
        if (
            not chart.get("fetched_at")
            or not chart.get("license_scope")
            or chart.get("fallback_source") not in (None, "")
        ):
            errors.append("chart component lineage is missing or fallback")
        if chart.get("source_key") != "sec" or str(chart.get("publication_batch_id") or "") != snapshot_batch_id:
            errors.append("chart source/batch lineage is missing")
        if not isinstance(series, list) or not series:
            errors.append("chart has no non-empty series")
            continue
        series_companies = set()
        for item in series:
            if not isinstance(item, Mapping) or not isinstance(item.get("data"), list) or len(item["data"]) != 5:
                errors.append("chart series is incomplete")
                continue
            series_companies.add(str(item.get("company") or ""))
            if any(not _is_numeric_payload(value) for value in item["data"]):
                errors.append("chart contains a non-numeric value")
            lineage = item.get("lineage")
            if not isinstance(lineage, list) or len(lineage) != 5 or any(
                not isinstance(component, Mapping)
                or not component.get("fetched_at")
                or not component.get("value_date")
                or component.get("source_key") != "sec"
                or str(component.get("publication_batch_id") or "") != snapshot_batch_id
                or not component.get("license_scope")
                or not component.get("source_fact_ids")
                or component.get("quality_status")
                not in {Observation.Quality.FRESH, Observation.Quality.STALE}
                or component.get("fallback_source") not in (None, "")
                for component in lineage
            ):
                errors.append("chart series lineage is missing")
        if series_companies != set(required_slugs):
            errors.append("chart series company lineage is incomplete")
    return sorted(set(errors))


def is_valid_public_supply_chain_demand_snapshot(snapshot: DashboardSnapshot) -> bool:
    return not validate_public_supply_chain_demand_snapshot(snapshot)


def select_public_supply_chain_demand_snapshot(candidates=None) -> DashboardSnapshot | None:
    """Select the newest valid demand snapshot, retaining an older valid one."""

    if candidates is None:
        candidates = (
            DashboardSnapshot.objects.filter(
                key="supply-chain-demand", is_published=True
            )
            .select_related("source")
            .order_by("-created_at", "-as_of")
        )
    for candidate in candidates:
        if is_valid_public_supply_chain_demand_snapshot(candidate):
            return candidate
    return None


def _lineage_table_row(
    row: dict[str, Any], *, source: Source, batch_id: uuid.UUID
) -> dict[str, Any]:
    cells = {
        "company": {"kind": "text", "value": row["company"]},
        "period": {"kind": "text", "value": f"FY{row['fiscal_year']} / {row['period_end']}"},
        "fetched": {"kind": "text", "value": row["fetched_at"]},
        "quality": {"kind": "badge", "value": row["quality_status"]},
        "license": {"kind": "text", "value": row["license_scope"]},
        "fallback": {"kind": "text", "value": row["fallback_source"] or "—"},
        "batch": {"kind": "text", "value": str(batch_id)},
        "source": {"kind": "text", "value": source.name},
    }
    return {
        "cells": cells,
        "cells_list": [
            {"label": label, "cell": cells[key]}
            for key, label in (
                ("company", "公司"),
                ("period", "财年 / 数值日期"),
                ("fetched", "实际抓取"),
                ("quality", "质量"),
                ("license", "许可范围"),
                ("fallback", "fallback"),
                ("batch", "发布批次"),
                ("source", "来源"),
            )
        ],
    }


def _publish_batch(
    *, specs: list[ReviewedCompany], companies: dict[str, Company], runs: dict[str, dict[str, IngestionRun]],
    normalized_by_slug: dict[str, list[dict[str, Any]]], metrics_by_slug: dict[str, dict[int, dict[str, Any]]],
    artifacts: dict[str, dict[str, RawArtifact]], source: Source, refresh_cycle_id: str,
    fetched_at_by_slug: dict[str, Any],
) -> uuid.UUID:
    source = Source.objects.select_for_update().get(pk=source.pk)
    # Lock the effective licence row itself.  Locking Source alone does not
    # serialize an administrator's revocation/update of SourceLicense.
    license_row = _current_sec_license(source, for_update=True)
    if not _license_is_publishable(license_row):
        raise ValueError("SEC public-and-derived-display licence is not currently effective")
    current_runs: dict[str, dict[str, IngestionRun]] = {}
    current_artifacts: dict[str, dict[str, RawArtifact]] = {}
    for spec in specs:
        current_runs[spec.slug] = {}
        current_artifacts[spec.slug] = {}
        for endpoint in ("submissions", "companyfacts"):
            run = IngestionRun.objects.select_for_update().get(pk=runs[spec.slug][endpoint].pk)
            artifact = RawArtifact.objects.select_for_update().get(
                pk=artifacts[spec.slug][endpoint].pk, run=run
            )
            if run.status != IngestionRun.Status.SUCCESS or artifact.sha256 != artifacts[spec.slug][endpoint].sha256:
                raise ValueError(f"current SEC input run/artifact validation failed for {spec.slug}:{endpoint}")
            current_runs[spec.slug][endpoint] = run
            current_artifacts[spec.slug][endpoint] = artifact
        company = Company.objects.select_for_update().get(pk=companies[spec.slug].pk)
        if (
            company.source_id != source.id
            or company.fallback_source_id
            or not is_exact_reviewed_sec_company(company)
            or not complete_five_year_metrics(metrics_by_slug[spec.slug])
        ):
            raise ValueError(f"publication contract failed for {spec.slug}")
        for row in normalized_by_slug[spec.slug]:
            fact = SECCompanyFact.objects.filter(identity_hash=row["identity_hash"]).first()
            if fact is None or fact.fallback_source_id or fact.quality_status == Observation.Quality.ERROR:
                raise ValueError(f"immutable SEC fact validation failed for {spec.slug}")
    batch_id = uuid.uuid4()
    rows_for_snapshot: list[dict[str, Any]] = []
    now = timezone.now()
    selection_diagnostics: dict[str, Any] = {}
    for spec in specs:
        company = companies[spec.slug]
        metrics = metrics_by_slug[spec.slug]
        years = sorted(metrics)
        previous_revenue: Decimal | None = None
        previous_capex: Decimal | None = None
        company_fetched_at = fetched_at_by_slug[spec.slug]
        facts = {
            fact.identity_hash: fact
            for fact in SECCompanyFact.objects.filter(
                company=company, identity_hash__in=[
                    identity for row in normalized_by_slug[spec.slug] for identity in [row["identity_hash"]]
                ]
            )
        }
        selection_diagnostics[spec.slug] = metrics.diagnostics
        for year in years:
            selected = metrics[year]
            revenue = Decimal(selected["revenue"]["value"]) / Decimal(1_000_000)
            gross = Decimal(selected["gross_profit"]["value"]) / Decimal(1_000_000)
            net_income = Decimal(selected["net_income"]["value"]) / Decimal(1_000_000)
            cfo = Decimal(selected["cfo"]["value"]) / Decimal(1_000_000)
            capex = abs(Decimal(selected["capex"]["value"])) / Decimal(1_000_000)
            capex_fact = facts[selected["capex"]["identity_hash"]]
            period_start = date.fromisoformat(selected["revenue"]["period_start"])
            period_end = date.fromisoformat(selected["revenue"]["period_end"])
            growth = ((revenue - previous_revenue) / previous_revenue * 100) if previous_revenue else None
            capex_yoy = ((capex - previous_capex) / previous_capex * 100) if previous_capex else None
            fact_lineage = {}
            for metric_name in ("revenue", "net_income", "cfo", "capex"):
                fact_lineage[metric_name] = _fact_record(
                    row=selected[metric_name], facts=facts,
                    run=current_runs[spec.slug]["companyfacts"],
                    artifact=current_artifacts[spec.slug]["companyfacts"],
                    fetched_at=company_fetched_at, license_row=license_row,
                )
            derivation = dict(selected["gross_profit_derivation"] or {})
            if derivation:
                derivation["input_fact_ids"] = {
                    identity: facts[identity].pk for identity in derivation.get("input_identity_hashes", []) if identity in facts
                }
                derivation["input_lineage"] = [
                    _fact_record(
                        row=next(item for item in normalized_by_slug[spec.slug] if item["identity_hash"] == identity),
                        facts=facts, run=current_runs[spec.slug]["companyfacts"],
                        artifact=current_artifacts[spec.slug]["companyfacts"],
                        fetched_at=company_fetched_at, license_row=license_row,
                    )
                    for identity in derivation.get("input_identity_hashes", [])
                ]
                fact_lineage["gross_profit"] = derivation["input_lineage"]
            else:
                fact_lineage["gross_profit"] = _fact_record(
                    row=selected["gross_profit"], facts=facts,
                    run=current_runs[spec.slug]["companyfacts"],
                    artifact=current_artifacts[spec.slug]["companyfacts"],
                    fetched_at=company_fetched_at, license_row=license_row,
                )
            capex_url = _fact_url(spec, selected["capex"])
            projection = FinancialFact.objects.create(
                company=company, fiscal_year=year, period_start=period_start, period_end=period_end,
                fiscal_period="FY", revenue_usd_m=revenue, revenue_growth=growth,
                gross_profit_usd_m=gross, gross_margin=(gross / revenue * 100 if revenue else None),
                net_income_usd_m=net_income, operating_cash_flow_usd_m=cfo,
                capital_expenditures_usd_m=capex, capex_intensity=(capex / revenue * 100 if revenue else None),
                capex_definition=selected["capex_definition"], capex_source_fact=capex_fact,
                accession_number=selected["revenue"]["accession_number"], form=selected["revenue"]["form"],
                source_url=_projection_source_url(spec, selected["revenue"]), source=source,
                publication_batch_id=batch_id, filed_at=date.fromisoformat(selected["revenue"]["filed_at"]),
                capex_source_url=capex_url, fetched_at=company_fetched_at, quality_status=Observation.Quality.FRESH,
                license_scope=license_row.scope, fallback_source=None,
                metadata={
                    "input_fact_identities": {
                        key: selected[key]["identity_hash"] for key in ("revenue", "gross_profit", "net_income", "cfo", "capex")
                    },
                    "input_fact_ids": {
                        key: facts[selected[key]["identity_hash"]].pk
                        for key in ("revenue", "net_income", "cfo", "capex")
                    },
                    "gross_profit_derivation": derivation,
                    "lineage": fact_lineage,
                    "selection_diagnostics": selected["selection_diagnostics"],
                    "derived_metrics": {"revenue_growth": float(growth) if growth is not None else None, "gross_margin": float(gross / revenue * 100) if revenue else None, "capex_revenue": float(capex / revenue * 100) if revenue else None, "capex_yoy": float(capex_yoy) if capex_yoy is not None else None},
                    "capex_definition_label": (
                        "Cash payments to acquire productive assets (broader than PP&E; SEC us-gaap)"
                        if spec.slug == "amazon"
                        else "Cash payments to acquire property, plant and equipment (SEC us-gaap)"
                    ),
                    "ai_specific": False,
                },
            )
            rows_for_snapshot.append({
                "company": spec.slug, "cik": spec.normalized_cik, "fiscal_year": year,
                "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
                "values_usd_m": {"revenue": float(revenue), "gross_profit": float(gross), "net_income": float(net_income), "cfo": float(cfo), "capital_expenditures": float(capex)},
                "unit": "USD millions", "filed_at": selected["revenue"]["filed_at"],
                "accession_number": selected["revenue"]["accession_number"], "form": selected["revenue"]["form"],
                "source_url": projection.source_url, "concepts": selected["selection_diagnostics"]["selected_concepts"],
                "source_fact_ids": projection.metadata["input_fact_ids"], "source_fact_identities": projection.metadata["input_fact_identities"], "fetched_at": company_fetched_at.isoformat(),
                "quality": Observation.Quality.FRESH, "quality_status": Observation.Quality.FRESH,
                "license_scope": license_row.scope, "fallback_source": None,
                "source_key": source.key, "source_name": source.name,
                "publication_batch_id": str(batch_id), "batch_id": str(batch_id),
                "capex_definition": selected["capex_definition"], "capex_definition_label": projection.metadata["capex_definition_label"],
                "capex_source": fact_lineage["capex"], "capex_source_url": capex_url, "lineage": fact_lineage,
                "derived_metrics": {"revenue_growth": float(growth) if growth is not None else None, "gross_margin": float(gross / revenue * 100) if revenue else None, "capex_revenue": float(capex / revenue * 100) if revenue else None, "capex_yoy": float(capex_yoy) if capex_yoy is not None else None},
                "capex_yoy": float(capex_yoy) if capex_yoy is not None else None, "ai_specific": False,
            })
            previous_revenue = revenue
            previous_capex = capex
        company.data_as_of = max(date.fromisoformat(metrics[year]["revenue"]["period_end"]) for year in years)
        company.price = None
        company.market_cap_usd_m = None
        company.return_1m = None
        company.return_6m = None
        company.revenue_growth = None
        company.gross_margin = None
        company.pe = None
        company.ps = None
        company.rating = ""
        company.quality_grade = ""
        company.publication_batch_id = batch_id
        company.fetched_at = company_fetched_at
        company.quality_status = Observation.Quality.FRESH
        company.license_scope = license_row.scope
        company.is_published = True
        company.fallback_source = None
        company.save(update_fields=["data_as_of", "price", "market_cap_usd_m", "return_1m", "return_6m", "revenue_growth", "gross_margin", "pe", "ps", "rating", "quality_grade", "publication_batch_id", "fetched_at", "quality_status", "license_scope", "is_published", "fallback_source", "updated_at"])
    raw_artifact_rows = [
        {"company": slug, "endpoint": endpoint, "id": artifact.pk, "sha256": artifact.sha256, "uri": artifact.uri}
        for slug, artifacts_for_company in current_artifacts.items() for endpoint, artifact in artifacts_for_company.items()
    ]
    input_runs = [
        {"company": spec.slug, "submissions": str(current_runs[spec.slug]["submissions"].batch_id), "companyfacts": str(current_runs[spec.slug]["companyfacts"].batch_id)}
        for spec in specs
    ]
    snapshot_payload = {
        "contract_version": 1, "required_company_slugs": [spec.slug for spec in specs],
        "input_run_batch_ids": input_runs, "input_run_batches": [value for item in input_runs for value in (item["submissions"], item["companyfacts"])], "raw_artifacts": raw_artifact_rows, "rows": rows_for_snapshot,
        "lineage": rows_for_snapshot, "selection_diagnostics": selection_diagnostics,
        "refresh_cycle_id": refresh_cycle_id, "ai_specific": False, "complete": True,
        "source_key": source.key, "source_keys": [source.key],
        "license_scope": license_row.scope, "publication_batch_id": str(batch_id),
    }
    snapshot_payload["fingerprint"] = hashlib.sha256(_canonical_json(snapshot_payload).encode()).hexdigest()
    DashboardSnapshot.objects.create(
        key="supply-chain-demand", title="AI 下游需求", as_of=now,
        batch_id=batch_id, quality_status=Observation.Quality.FRESH,
        summary="公司层面的 SEC 现金资本开支事实/代理指标，不是 AI-only CapEx。Amazon 使用更宽的 productive-assets 标签。",
        data={
            **snapshot_payload,
            "metrics": [
                {
                    "company": spec.slug,
                    "label": f"{spec.name} 最新现金资本开支",
                    "value": latest_row["values_usd_m"]["capital_expenditures"],
                    "display_value": f"${latest_row['values_usd_m']['capital_expenditures']:,.2f}m",
                    "unit": "USD millions",
                    "source": source.name,
                    "source_key": source.key,
                    "source_name": source.name,
                    "as_of": latest_row["period_end"],
                    "value_date": latest_row["period_end"],
                    "fetched_at": latest_row["fetched_at"],
                    "quality_status": latest_row["quality_status"],
                    "license_scope": latest_row["license_scope"],
                    "fallback_source": latest_row["fallback_source"],
                    "publication_batch_id": str(batch_id),
                    "batch_id": str(batch_id),
                    "ai_specific": False,
                }
                for spec in specs
                for latest_row in [max(
                    (row for row in rows_for_snapshot if row["company"] == spec.slug),
                    key=lambda row: int(row["fiscal_year"]),
                )]
            ],
            "charts": [
                _build_chart(key="reported-capex", title="披露现金资本开支", unit="USD millions", rows=[{**row, "reported_capex_value": row["values_usd_m"]["capital_expenditures"]} for row in rows_for_snapshot], specs=specs, value_key="reported_capex_value", source=source, fetched_at_by_slug=fetched_at_by_slug, batch_id=batch_id, license_scope=license_row.scope),
                _build_chart(key="capex-intensity", title="资本开支 / 收入", unit="percent", rows=[{**row, "capex_intensity_value": row["derived_metrics"]["capex_revenue"]} for row in rows_for_snapshot], specs=specs, value_key="capex_intensity_value", source=source, fetched_at_by_slug=fetched_at_by_slug, batch_id=batch_id, license_scope=license_row.scope),
                _build_chart(key="financial-capacity", title="财务承载能力（经营现金流减现金资本开支）", unit="USD millions", rows=[{**row, "capacity_value": row["values_usd_m"]["cfo"] - row["values_usd_m"]["capital_expenditures"]} for row in rows_for_snapshot], specs=specs, value_key="capacity_value", source=source, fetched_at_by_slug=fetched_at_by_slug, batch_id=batch_id, license_scope=license_row.scope),
            ],
            "sections": [
                {"title": "逐年明细", "body": "以下为公司层面的 SEC 年报现金资本开支事实/代理指标；不推断 AI-only CapEx、GPU 数量、租赁或项目级 AI 拆分。", "columns": [{"key": "company", "label": "公司"}, {"key": "period", "label": "财年 / 期末"}, {"key": "revenue", "label": "收入"}, {"key": "gross", "label": "毛利 / 毛利率"}, {"key": "net_income", "label": "净利润"}, {"key": "cfo", "label": "CFO"}, {"key": "capex", "label": "现金资本开支"}, {"key": "capex_ratio", "label": "CapEx/收入"}, {"key": "definition", "label": "定义"}, {"key": "filing", "label": "申报"}, {"key": "fetched", "label": "抓取"}, {"key": "quality", "label": "质量 / 批次 / 来源"}], "rows": [_table_row(row, source=source, batch_id=batch_id) for row in rows_for_snapshot]},
                {"title": "组件级发布血缘", "body": "每一行保留实际财务事实的数值日期、抓取时间、质量、许可、fallback 与完整发布批次；图表也复用同一组件血缘。", "columns": [{"key": "company", "label": "公司"}, {"key": "period", "label": "财年 / 数值日期"}, {"key": "fetched", "label": "实际抓取"}, {"key": "quality", "label": "质量"}, {"key": "license", "label": "许可范围"}, {"key": "fallback", "label": "fallback"}, {"key": "batch", "label": "发布批次"}, {"key": "source", "label": "来源"}], "rows": [_lineage_table_row(row, source=source, batch_id=batch_id) for row in rows_for_snapshot]},
            ],
        },
        source=source, is_published=True,
    )
    return batch_id


def refresh_sec_company_data(*, company_slugs: list[str] | tuple[str, ...] | None = None, publish: bool = True, provider: SECProvider | None = None) -> dict[str, Any]:
    """Fetch, persist, validate, and optionally atomically publish SEC data."""

    requested = list(company_slugs) if company_slugs is not None else [spec.slug for spec in REVIEWED_COMPANIES]
    specs = [SEC_COMPANY_ALLOWLIST[slug] for slug in requested if slug in SEC_COMPANY_ALLOWLIST]
    unknown = [slug for slug in requested if slug not in SEC_COMPANY_ALLOWLIST]
    source = Source.objects.filter(key="sec").first()
    if source is None or not source.licenses.filter(is_current=True).exists():
        source = ensure_source("sec")
    companies: dict[str, Company] = {}
    refresh_cycle_id = str(uuid.uuid4())
    details: list[dict[str, Any]] = []
    runs: dict[str, dict[str, IngestionRun]] = {}
    artifacts: dict[str, dict[str, RawArtifact]] = defaultdict(dict)
    fetched_at_by_slug: dict[str, Any] = {}
    normalized_by_slug: dict[str, list[dict[str, Any]]] = {}
    metrics_by_slug: dict[str, SelectedMetrics] = {}
    client = provider or SECProvider()
    owns_provider = provider is None
    try:
        for spec in specs:
            runs[spec.slug] = {}
            for endpoint in ("submissions", "companyfacts"):
                method_name = "company_facts" if endpoint == "companyfacts" else endpoint
                artifact: RawArtifact | None = None
                try:
                    result = getattr(client, method_name)(spec.normalized_cik)
                except Exception as exc:
                    result = ProviderResult.failure("sec", f"{endpoint}:{spec.normalized_cik}", f"{type(exc).__name__}: {exc}")
                    result.fetched_at = timezone.now()
                run = begin_ingestion(source, f"{endpoint}:{spec.slug}", metadata={"refresh_cycle_id": refresh_cycle_id, "cik": spec.normalized_cik, "fetched_at": result.fetched_at.isoformat()})
                runs[spec.slug][endpoint] = run
                detail = {"company": spec.slug, "endpoint": endpoint, "status": "success", "error": ""}
                if result.skipped:
                    finish_ingestion(run, status=IngestionRun.Status.PARTIAL, metadata={"skipped": True, **result.metadata})
                    detail.update(status="partial", error="SEC identity is not configured")
                    details.append(detail)
                    continue
                if result.error:
                    finish_ingestion(run, status=IngestionRun.Status.FAILED, error=result.error)
                    detail.update(status="failed", error="SEC endpoint request failed")
                    details.append(detail)
                    continue
                try:
                    if endpoint == "companyfacts" and runs[spec.slug].get("submissions", None) is None:
                        raise ValueError("SEC submissions identity validation is required first")
                    if endpoint == "companyfacts" and runs[spec.slug]["submissions"].status != IngestionRun.Status.SUCCESS:
                        raise ValueError("SEC companyfacts skipped because submissions identity failed")
                    with transaction.atomic():
                        license_row = _current_sec_license(source, for_update=True)
                        if not _license_is_storage_allowed(license_row):
                            raise ValueError("SEC historical storage is not currently licensed")
                        artifact = persist_raw_artifact(run=run, result=result)
                        if endpoint == "submissions":
                            payload = result.records[0] if result.records else {}
                            _validate_submission(payload, spec)
                            companies[spec.slug] = _ensure_company(spec, source)
                            finish_ingestion(run, status=IngestionRun.Status.SUCCESS, row_count=1, metadata={"raw_artifact_id": artifact.pk, "raw_artifact_sha256": artifact.sha256, "refresh_cycle_id": refresh_cycle_id})
                        else:
                            count, normalized, metrics = _persist_facts(company=companies[spec.slug], spec=spec, result=result, run=run, artifact=artifact, source=source, license_row=license_row)
                            normalized_by_slug[spec.slug] = normalized
                            metrics_by_slug[spec.slug] = metrics
                            fetched_at_by_slug[spec.slug] = result.fetched_at
                            finish_ingestion(run, status=IngestionRun.Status.SUCCESS if count else IngestionRun.Status.PARTIAL, row_count=count, metadata={"raw_artifact_id": artifact.pk, "raw_artifact_sha256": artifact.sha256, "raw_artifact_uri": artifact.uri, "normalization": normalized.diagnostics, "selection": metrics.diagnostics, "refresh_cycle_id": refresh_cycle_id})
                        artifacts[spec.slug][endpoint] = artifact
                    details.append(detail)
                except Exception as exc:
                    finish_ingestion(run, status=IngestionRun.Status.FAILED, error=f"{type(exc).__name__}: {exc}", metadata={"refresh_cycle_id": refresh_cycle_id})
                    detail.update(status="failed", error=str(exc)[:240])
                    details.append(detail)
    finally:
        if owns_provider:
            client.close()
    request_data_complete = bool(specs) and not unknown and all(
        runs.get(spec.slug, {}).get(endpoint, None)
        and runs[spec.slug][endpoint].status == IngestionRun.Status.SUCCESS
        for spec in specs
        for endpoint in ("submissions", "companyfacts")
    ) and all(complete_five_year_metrics(metrics_by_slug.get(spec.slug, {})) for spec in specs)
    complete = not unknown and len(specs) == len(REVIEWED_COMPANIES) and all(
        runs.get(spec.slug, {}).get(endpoint, None) and runs[spec.slug][endpoint].status == IngestionRun.Status.SUCCESS
        for spec in specs for endpoint in ("submissions", "companyfacts")
    ) and all(complete_five_year_metrics(metrics_by_slug.get(spec.slug, {})) for spec in specs)
    failure = None
    batch_id = None
    full_request = set(requested) == {spec.slug for spec in REVIEWED_COMPANIES} and len(requested) == len(REVIEWED_COMPANIES)
    if publish and full_request and complete:
        try:
            with transaction.atomic():
                batch_id = _publish_batch(specs=specs, companies=companies, runs=runs, normalized_by_slug=normalized_by_slug, metrics_by_slug=metrics_by_slug, artifacts=artifacts, source=source, refresh_cycle_id=refresh_cycle_id, fetched_at_by_slug=fetched_at_by_slug)
        except Exception as exc:
            failure = _failure("SEC publication batch failed completeness or licence validation", [{"source": "sec", "status": "failed", "error": str(exc)[:240]}])
    elif publish and not request_data_complete:
        failure = _failure("SEC refresh did not produce a complete four-company five-year batch", details)
    if failure:
        with transaction.atomic():
            _mark_refresh_failure(
                failure=failure,
                companies=list(companies.values()),
                requested_slugs=[spec.slug for spec in specs],
            )
    return {
        "refresh_cycle_id": refresh_cycle_id, "batch_id": str(batch_id) if batch_id else None,
        "published": bool(batch_id), "complete": complete, "runs": details,
        "failure": failure, "company_slugs": requested,
    }
