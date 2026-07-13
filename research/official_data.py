"""Fetch and publish public-display-safe official data snapshots.

Every displayed metric keeps its own source and value date. Restricted feeds
are deliberately absent from this module so a public dashboard cannot silently
inherit an internal/test-only observation.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone

from .calculations import yield_spread
from .consumer_credit import FederalReserveG19Provider, NYFedHouseholdDebtProvider
from .credit_official import FederalReserveSLOOSProvider, TreasuryHQMProvider
from .fed_h10 import FederalReserveH10Provider
from .fed_h41 import FederalReserveH41Provider
from .fed_prates import FederalReservePRATESProvider
from .labor_official import (
    CONTINUED_4WK,
    CONTINUED_SA,
    INITIAL_4WK,
    INITIAL_SA,
    IUR_SA,
    DOLWeeklyClaimsProvider,
)
from .labor_official import (
    REQUIRED_SERIES as DOL_REQUIRED_SERIES,
)
from .macro_official import CensusMARTSProvider
from .macro_releases import (
    BEAGDPReleaseProvider,
    BEAPIOReleaseProvider,
    CensusMARTSReleaseProvider,
)
from .models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    ReleaseVintageObservation,
    SeriesDefinition,
    Source,
    TreasuryAuction,
)
from .providers import (
    BLSProvider,
    FederalReserveRSSProvider,
    FiscalDataProvider,
    NYFedMarketsProvider,
    TreasuryRatesProvider,
)
from .services import (
    current_display_source_key_sets,
    ensure_source,
    public_display_license_q,
    public_source_notices,
    publicly_displayable_source_keys,
    record_provider_result,
    store_fed_documents,
    store_release_vintage_observations,
    store_series_observations,
    store_treasury_auctions,
)

BLS_SERIES = (
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

FRESHNESS_DAYS = {
    "intraday": 1,
    "daily": 4,
    "weekly": 10,
    "monthly": 45,
    "quarterly": 120,
    "annual": 400,
}

CORE_PUBLICATION_KEYS = frozenset(
    {
        "transmission-chain",
        "operations",
        "global-dollar",
        "subsurface",
    }
)
AUCTION_CONTRACT_VERSION = 1
RRP_TGA_CONTRACT_VERSION = 1
AUCTION_DATASET = (
    "treasury-fiscal-data",
    "treasury-securities-auctions",
)
INDEPENDENT_PUBLICATION_KEYS = frozenset({"auctions", "rrp-tga"})
AUCTION_REQUIRED_METRIC_KEYS = frozenset(
    {
        "days-to-next-auction",
        "formal-auction-gross-7d",
        "issue-gross-7d",
        "issue-gross-14d",
        "latest-bid-to-cover",
    }
)
RRP_TGA_REQUIRED_METRIC_KEYS = frozenset(
    {
        "onrrp",
        "onrrp-rate",
        "onrrp-participants",
        "tga",
        "issue-gross-7d",
        "issue-gross-14d",
    }
)
TREASURY_CURVE_CONTRACT_VERSION = 1
TREASURY_CURVE_HISTORY_YEARS = 5
TREASURY_CURVE_MIN_HISTORY_POINTS = 1000
TREASURY_CURVE_MAX_GAP_DAYS = 10
TREASURY_CURVE_START_TOLERANCE_DAYS = 14
TREASURY_CURVE_PAGE_KEYS = frozenset({"yield-curve", "real-rates"})
TREASURY_NOMINAL_TENORS = (
    "1m",
    "2m",
    "3m",
    "4m",
    "6m",
    "1y",
    "2y",
    "3y",
    "5y",
    "7y",
    "10y",
    "20y",
    "30y",
)
TREASURY_REAL_TENORS = ("5y", "7y", "10y", "20y", "30y")
TREASURY_NOMINAL_SERIES = tuple(f"ust-{tenor}" for tenor in TREASURY_NOMINAL_TENORS)
TREASURY_REAL_SERIES = tuple(f"tips-{tenor}" for tenor in TREASURY_REAL_TENORS)
TREASURY_NOMINAL_HISTORY_SERIES = (
    "ust-3m",
    "ust-2y",
    "ust-5y",
    "ust-10y",
    "ust-30y",
)
TREASURY_REAL_HISTORY_SERIES = ("tips-5y", "tips-10y")
TREASURY_CURVE_DATASET_PREFIXES = {
    "nominal": "daily_treasury_yield_curve",
    "real": "daily_treasury_real_yield_curve",
}
YIELD_CURVE_REQUIRED_METRIC_KEYS = frozenset(
    {"ust-2y", "ust-5y", "ust-10y", "ust-30y", "2s10s", "3m10s", "5s30s"}
)
REAL_RATES_REQUIRED_METRIC_KEYS = frozenset(
    {"tips-5y", "tips-10y", "5y-bei", "10y-bei"}
)
H41_PUBLICATION_KEYS = frozenset({"fed-balance-sheet", "reserves"})
PRATES_PUBLICATION_KEYS = frozenset(
    {"transmission-chain", "subsurface"}
)
H10_PUBLICATION_KEYS = frozenset({"assets-fx"})
CREDIT_PUBLICATION_KEYS = frozenset({"credit", "credit-spreads", "credit-stress"})
CONSUMER_CONTRACT_VERSION = 1
MACRO_PUBLICATION_GROUPS = {
    "gdp": frozenset({"bea-release"}),
    "consumer": frozenset(
        {
            "census-release",
            "bea-pio-release",
            "federal-reserve-g19",
            "ny-fed-household-credit",
        }
    ),
}
MACRO_REQUIRED_DATASETS = {
    "consumer": {
        "census-release": "marts:retail-food-services",
    }
}
EMPLOYMENT_PUBLICATION_GROUPS = {
    "employment": frozenset({"bls", "dol-eta-ui"}),
}
INFLATION_PUBLICATION_GROUPS = {
    "inflation": frozenset({"bls", "bea-pio-release"}),
}
FED_FUNDS_DATASETS = {
    "sofr": ("ny-fed-markets", "reference-rate:sofr"),
    "effr": ("ny-fed-markets", "reference-rate:effr"),
    "iorb": ("federal-reserve", "prates:iorb"),
}
FED_FUNDS_REQUIRED_METRIC_KEYS = frozenset(
    {
        "effr",
        "sofr",
        "iorb",
        "target-lower",
        "target-upper",
        "sofr-effr",
        "sofr-iorb",
        "effr-iorb",
        "effr-volume",
        "sofr-volume",
        "effr-p1-p99-width",
        "sofr-p1-p99-width",
        "effr-corridor-position",
    }
)
LIQUIDITY_CONTRACT_VERSION = 1
LIQUIDITY_DATASETS = {
    "h41": ("federal-reserve", "h41"),
    "onrrp": ("ny-fed-markets", "repo:reverse-repo-fixed-results"),
    "tga": ("treasury-fiscal-data", "daily-treasury-statement:tga"),
}
RRP_TGA_DATASETS = {
    "onrrp": LIQUIDITY_DATASETS["onrrp"],
    "tga": LIQUIDITY_DATASETS["tga"],
    "auctions": AUCTION_DATASET,
}
LIQUIDITY_FED_FUNDS_METRIC_KEYS = frozenset(
    {"sofr", "iorb", "sofr-effr", "sofr-iorb"}
)
LIQUIDITY_REQUIRED_METRIC_KEYS = frozenset(
    {
        "net-liquidity",
        "walcl",
        "wrbwfrbl",
        "onrrp",
        "tga",
        *LIQUIDITY_FED_FUNDS_METRIC_KEYS,
    }
)
ECONOMY_CONTRACT_VERSION = 1
ECONOMY_COMPONENTS = {
    "gdp": {
        "metric_key": "bea-a191rl",
        "metric_label": "实际 GDP 季调年化增速",
        "chart_key": "gdp-growth-history",
        "tab": "growth",
    },
    "employment": {
        "metric_key": "lns14000000",
        "metric_label": "失业率",
        "chart_key": "labor-slack",
        "tab": "labor",
    },
    "inflation": {
        "metric_key": "core-cpi-yoy",
        "metric_label": "核心 CPI 同比",
        "chart_key": "core-cpi-rates",
        "tab": "inflation",
    },
    "consumer": {
        "metric_key": "bea-real-pce-mom",
        "metric_label": "实际 PCE 环比",
        "chart_key": "real-consumption-income-momentum",
        "tab": "consumer",
    },
}
ECONOMY_REQUIRED_METRIC_KEYS = frozenset(
    item["metric_key"] for item in ECONOMY_COMPONENTS.values()
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
INFLATION_REQUIRED_METRIC_KEYS = frozenset(
    {
        "headline-cpi-mom",
        "headline-cpi-yoy",
        "headline-cpi-3m-annualized",
        "headline-cpi-6m-annualized",
        "core-cpi-mom",
        "core-cpi-yoy",
        "core-cpi-3m-annualized",
        "core-cpi-6m-annualized",
        "final-demand-ppi-mom",
        "final-demand-ppi-yoy",
        "final-demand-ppi-3m-annualized",
        "final-demand-ppi-6m-annualized",
        "pce-price-index-mom",
        "pce-price-index-yoy",
        "pce-price-index-3m-annualized",
        "pce-price-index-6m-annualized",
        "core-pce-price-index-mom",
        "core-pce-price-index-yoy",
        "core-pce-price-index-3m-annualized",
        "core-pce-price-index-6m-annualized",
        "shelter-cpi-mom",
        "shelter-cpi-yoy",
        "shelter-cpi-3m-annualized",
        "shelter-cpi-6m-annualized",
        "core-goods-cpi-mom",
        "core-goods-cpi-yoy",
        "core-goods-cpi-3m-annualized",
        "core-goods-cpi-6m-annualized",
        "services-less-energy-cpi-mom",
        "services-less-energy-cpi-yoy",
        "services-less-energy-cpi-3m-annualized",
        "services-less-energy-cpi-6m-annualized",
    }
)
MACRO_REQUIRED_SERIES = {
    "employment": {
        "bls": frozenset(
            {
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
            }
        ),
        "dol-eta-ui": DOL_REQUIRED_SERIES,
    },
    "inflation": {
        "bls": frozenset(
            {
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
            }
        ),
        "bea-pio-release": frozenset(
            {
                "BEA-PCE-PRICE-INDEX",
                "BEA-CORE-PCE-PRICE-INDEX",
            }
        ),
    },
    "gdp": {
        "bea-release": frozenset(
            {
                "BEA-A191RL",
                "BEA-DPCERL",
                "BEA-GDP-NOMINAL-SAAR",
                "BEA-GDI-REAL-GROWTH-SAAR",
                "BEA-PCE-GOODS-GROWTH",
                "BEA-PCE-SERVICES-GROWTH",
                "BEA-GPDI-GROWTH",
                "BEA-PCE-CONTRIBUTION",
                "BEA-GPDI-CONTRIBUTION",
                "BEA-NET-EXPORTS-CONTRIBUTION",
                "BEA-GOVERNMENT-CONTRIBUTION",
            }
        )
    },
    "consumer": {
        "census-release": frozenset(
            {
                "CENSUS-MRTS-44X72-SM-SA",
                "CENSUS-MRTS-44X72-SM-SA-MOM",
                "CENSUS-MRTS-44X72-SM-SA-YOY",
            }
        ),
        "bea-pio-release": frozenset(
            {
                "BEA-REAL-PCE-MOM",
                "BEA-REAL-DPI-MOM",
                "BEA-PERSONAL-SAVING-RATE",
            }
        ),
        "federal-reserve-g19": frozenset(
            {
                "G19-CONSUMER-CREDIT-OUTSTANDING-SA",
                "G19-REVOLVING-CREDIT-OUTSTANDING-SA",
                "G19-NONREVOLVING-CREDIT-OUTSTANDING-SA",
                "G19-CONSUMER-CREDIT-GROWTH-SAAR",
                "G19-REVOLVING-CREDIT-GROWTH-SAAR",
                "G19-NONREVOLVING-CREDIT-GROWTH-SAAR",
            }
        ),
        "ny-fed-household-credit": frozenset(
            {
                "HHDC-TOTAL-DEBT-BALANCE",
                "HHDC-CREDIT-CARD-BALANCE",
                "HHDC-ALL-90D-DELINQUENT",
                "HHDC-CREDIT-CARD-90D-DELINQUENT",
            }
        ),
    },
}
MACRO_REQUIRED_VINTAGE_SERIES = {
    "gdp": {
        "bea-release": frozenset(
            {
                "BEA-A191RL",
                "BEA-GDP-NOMINAL-SAAR",
                "BEA-GDI-NOMINAL-SAAR",
                "BEA-GDI-REAL-GROWTH-SAAR",
            }
        )
    }
}


def _has_publishable_run(runs: Iterable[IngestionRun]) -> bool:
    """Publish only when the whole refresh group is complete and non-empty."""

    completed = list(runs)
    return bool(completed) and all(
        run.status == IngestionRun.Status.SUCCESS and run.row_count > 0
        for run in completed
    )


def _publishable_keys_for_source_groups(
    runs: Iterable[IngestionRun],
    groups: dict[str, frozenset[str]],
) -> set[str]:
    """Return page keys whose exact source group completed in this refresh."""

    by_source: dict[str, list[IngestionRun]] = {}
    for run in runs:
        by_source.setdefault(run.source.key, []).append(run)
    return {
        page_key
        for page_key, required_sources in groups.items()
        if all(
            len(by_source.get(source_key, [])) == 1
            and _has_publishable_run(by_source[source_key])
            for source_key in required_sources
        )
    }


def _keys_with_current_required_batches(
    page_keys: Iterable[str],
    runs: Iterable[IngestionRun],
) -> set[str]:
    """Bind each page's required latest observations to this refresh's batches."""

    run_by_source = {run.source.key: run for run in runs}
    current: set[str] = set()
    for page_key in page_keys:
        page_requirements = MACRO_REQUIRED_SERIES.get(page_key, {})
        page_is_current = bool(page_requirements)
        for source_key, dataset in MACRO_REQUIRED_DATASETS.get(page_key, {}).items():
            run = run_by_source.get(source_key)
            if run is None or run.dataset != dataset:
                page_is_current = False
                break
        if not page_is_current:
            continue
        for source_key, series_keys in page_requirements.items():
            run = run_by_source.get(source_key)
            if run is None:
                page_is_current = False
                break
            expected_batch = str(run.batch_id)
            for series_key in series_keys:
                observation = _real_observations(
                    series_key,
                    source_key=source_key,
                ).first()
                if (
                    observation is None
                    or str(observation.batch_id) != expected_batch
                ):
                    page_is_current = False
                    break
            if not page_is_current:
                break
        for source_key, series_keys in MACRO_REQUIRED_VINTAGE_SERIES.get(
            page_key, {}
        ).items():
            run = run_by_source.get(source_key)
            if run is None:
                page_is_current = False
                break
            stored_series = set(
                ReleaseVintageObservation.objects.filter(
                    source__key=source_key,
                    batch_id=run.batch_id,
                    series__key__in={key.lower() for key in series_keys},
                ).values_list("series__key", flat=True)
            )
            if stored_series != {key.lower() for key in series_keys}:
                page_is_current = False
                break
        if page_is_current:
            current.add(page_key)
    return current


def _mark_latest_dashboards_stale(
    page_keys: Iterable[str],
    runs: Iterable[IngestionRun],
    *,
    groups: dict[str, frozenset[str]] | None = None,
) -> None:
    """Keep the last complete snapshot but expose the failed refresh state."""

    publication_groups = groups or MACRO_PUBLICATION_GROUPS
    runs_by_source = {run.source.key: run for run in runs}
    checked_at = timezone.now().isoformat()
    with transaction.atomic():
        for page_key in page_keys:
            latest = (
                DashboardSnapshot.objects.select_for_update()
                .filter(key=page_key, is_published=True)
                .order_by("-created_at")
                .first()
            )
            if latest is None:
                continue
            required_sources = publication_groups.get(page_key, frozenset())
            source_states = []
            for source_key in sorted(required_sources):
                run = runs_by_source.get(source_key)
                source_states.append(
                    {
                        "source": source_key,
                        "status": run.status if run else "missing",
                        "row_count": run.row_count if run else 0,
                        "error": (
                            (
                                run.error
                                or str((run.metadata or {}).get("reason") or "")
                                or (
                                    "source run did not produce a complete batch"
                                    if run.status != IngestionRun.Status.SUCCESS
                                    else ""
                                )
                            )
                            if run
                            else "source run missing"
                        )[:240],
                    }
                )
            data = dict(latest.data or {})
            data["refresh_failure"] = {
                "checked_at": checked_at,
                "reason": (
                    "最近一次必需数据刷新未通过完整性、时序或本批次一致性检查；"
                    "继续保留上一版完整快照。"
                ),
                "sources": source_states,
            }
            latest.data = data
            latest.quality_status = Observation.Quality.STALE
            latest.save(update_fields=["data", "quality_status", "updated_at"])


def _fresh_until(observation: Observation) -> datetime:
    """Return a deadline from the observation period end, not period start."""

    value_date = observation.value_date
    frequency = observation.series.frequency
    release_date = (observation.metadata or {}).get("source_release_time") or (
        observation.metadata or {}
    ).get("source_revision_date")
    release_freshness_days = (observation.metadata or {}).get(
        "release_freshness_days"
    )
    if release_date and release_freshness_days:
        try:
            released_at = datetime.fromisoformat(str(release_date))
            if released_at.tzinfo is None:
                released_at = released_at.replace(tzinfo=UTC)
            release_deadline = released_at + timedelta(
                days=int(release_freshness_days)
            )
        except (TypeError, ValueError, OverflowError):
            release_deadline = None
        if release_deadline is not None:
            return release_deadline
    if frequency == "monthly":
        day = calendar.monthrange(value_date.year, value_date.month)[1]
        period_end = value_date.replace(day=day)
    elif frequency == "quarterly":
        quarter_end_month = ((value_date.month - 1) // 3 + 1) * 3
        day = calendar.monthrange(value_date.year, quarter_end_month)[1]
        period_end = value_date.replace(month=quarter_end_month, day=day)
    elif frequency == "annual":
        period_end = value_date.replace(month=12, day=31)
    elif frequency == "daily":
        deadline_date = value_date.date() + timedelta(
            days=FRESHNESS_DAYS["daily"]
        )
        return datetime.combine(
            deadline_date,
            time(hour=10),
            tzinfo=ZoneInfo("America/New_York"),
        ).astimezone(UTC)
    else:
        period_end = value_date
    return period_end + timedelta(days=FRESHNESS_DAYS.get(frequency, 4))


def _real_observations(
    series_key: str,
    *,
    source_key: str | None = None,
    batch_id: uuid.UUID | str | None = None,
):
    queryset = (
        Observation.objects.filter(series__key=series_key.lower())
        .exclude(source__key="demo-market")
        .filter(public_display_license_q())
        .select_related("series", "source", "fallback_source")
        .distinct()
    )
    if source_key is not None:
        queryset = queryset.filter(source__key=source_key)
    if batch_id is not None:
        queryset = queryset.filter(batch_id=batch_id)
    return queryset.order_by("-value_date", "-fetched_at", "-id")


def _latest_observations_by_value_date(
    series_key: str,
    *,
    limit: int,
    source_key: str | None = None,
    batch_id: uuid.UUID | str | None = None,
) -> list[Observation]:
    """Return one deterministic latest-source observation per economic date."""

    observations: list[Observation] = []
    seen_dates = set()
    for observation in _real_observations(
        series_key, source_key=source_key, batch_id=batch_id
    ).iterator():
        if observation.value_date in seen_dates:
            continue
        observations.append(observation)
        seen_dates.add(observation.value_date)
        if len(observations) >= limit:
            break
    return observations


def _observation_source_keys(*observations: Observation) -> set[str]:
    keys = {observation.source.key for observation in observations}
    keys.update(
        observation.fallback_source.key
        for observation in observations
        if observation.fallback_source_id
    )
    return keys


def _metric(
    series_key: str,
    label: str,
    *,
    decimals: int = 2,
    suffix: str = "",
    scale: Decimal = Decimal("1"),
    aligned_with: Iterable[str] = (),
    source_key: str | None = None,
    batch_id: uuid.UUID | str | None = None,
) -> dict[str, Any] | None:
    alignment_keys = tuple(dict.fromkeys(aligned_with))
    if alignment_keys:
        observations_by_key = {
            key: {
                item.value_date.date(): item
                for item in _latest_observations_by_value_date(
                    key,
                    limit=2000,
                    source_key=source_key,
                    batch_id=batch_id,
                )
            }
            for key in (series_key, *alignment_keys)
        }
        common_dates = set.intersection(
            *(set(items) for items in observations_by_key.values())
        )
        today_et = timezone.now().astimezone(
            ZoneInfo("America/New_York")
        ).date()
        periods = sorted(
            (period for period in common_dates if period <= today_et),
            reverse=True,
        )
        observations = [
            observations_by_key[series_key][period] for period in periods[:2]
        ]
    else:
        observations = _latest_observations_by_value_date(
            series_key,
            limit=2,
            source_key=source_key,
            batch_id=batch_id,
        )
    if not observations:
        return None
    latest = observations[0]
    value = latest.value * scale
    previous = observations[1].value * scale if len(observations) > 1 else None
    change = value - previous if previous is not None else None
    fresh_until = _fresh_until(latest)
    quality_status = latest.quality_status
    if timezone.now() > fresh_until and quality_status == Observation.Quality.FRESH:
        quality_status = Observation.Quality.STALE
    source_keys = sorted(_observation_source_keys(latest))
    return {
        "key": series_key.lower(),
        "label": label,
        "value": float(value),
        "display_value": f"{value:,.{decimals}f}{suffix}",
        "change": round(float(change), decimals) if change is not None else None,
        "change_unit": "pp" if suffix == "%" else suffix,
        "unit": suffix,
        "quality_status": quality_status,
        "source": (
            f"{latest.source.name}（备用：{latest.fallback_source.name}）"
            if latest.fallback_source_id
            else latest.source.name
        ),
        "source_key": latest.source.key,
        "source_keys": source_keys,
        "fallback_source": (
            latest.fallback_source.key if latest.fallback_source_id else None
        ),
        "as_of": latest.as_of.isoformat(),
        "value_date": latest.value_date.isoformat(),
        "fetched_at": latest.fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(latest.batch_id),
        "metadata": {
            **latest.metadata,
            **(
                {
                    "common_effective_date": latest.value_date.date().isoformat(),
                    "aligned_with": [key.lower() for key in alignment_keys],
                }
                if alignment_keys
                else {}
            ),
        },
    }


def _derived_metric(
    key: str,
    label: str,
    left_key: str,
    right_key: str,
    *,
    basis_points: bool = False,
) -> dict[str, Any] | None:
    left_by_date = {
        item.value_date.date(): item
        for item in _latest_observations_by_value_date(left_key, limit=2000)
    }
    right_by_date = {
        item.value_date.date(): item
        for item in _latest_observations_by_value_date(right_key, limit=2000)
    }
    common_dates = {
        period
        for period in set(left_by_date) & set(right_by_date)
        if period
        <= timezone.now().astimezone(ZoneInfo("America/New_York")).date()
    }
    if not common_dates:
        return None
    common_date = max(common_dates)
    left = left_by_date[common_date]
    right = right_by_date[common_date]
    value = Decimal(str(yield_spread(left.value, right.value, basis_points=basis_points)))
    suffix = "bp" if basis_points else "%"
    left_deadline = _fresh_until(left)
    right_deadline = _fresh_until(right)
    fresh_until = min(left_deadline, right_deadline)
    source_keys = sorted(
        _observation_source_keys(left, right) | {"internal"}
    )
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": f"{value:+,.0f}{suffix}" if basis_points else f"{value:+,.2f}{suffix}",
        "change": None,
        "unit": suffix,
        "quality_status": Observation.Quality.ESTIMATED,
        "source": f"Atlas Macro 计算：{left.source.name} − {right.source.name}",
        "source_key": "internal",
        "source_keys": source_keys,
        "as_of": min(left.as_of, right.as_of).isoformat(),
        "value_date": min(left.value_date, right.value_date).isoformat(),
        "fetched_at": max(left.fetched_at, right.fetched_at).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": f"{left.batch_id},{right.batch_id}",
        "metadata": {
            "formula": f"{left_key} - {right_key}",
            "source_keys": sorted(_observation_source_keys(left, right)),
        },
    }


def _linear_metric(
    key: str,
    label: str,
    terms: tuple[tuple[Decimal, str], ...],
    *,
    scale: Decimal = Decimal("1"),
    decimals: int = 2,
    suffix: str = "",
) -> dict[str, Any] | None:
    """Calculate a transparent linear combination from latest public inputs."""

    inputs: list[tuple[Decimal, Observation]] = []
    for coefficient, series_key in terms:
        observation = _real_observations(series_key).first()
        if observation is None:
            return None
        inputs.append((coefficient, observation))
    value = sum((coefficient * item.value for coefficient, item in inputs), Decimal("0"))
    scaled_value = value * scale
    deadlines = [_fresh_until(item) for _, item in inputs]
    fresh_until = min(deadlines)
    quality = (
        Observation.Quality.STALE if timezone.now() > fresh_until else Observation.Quality.ESTIMATED
    )
    formula = " ".join(
        ("+ " if coefficient > 0 and index else "- " if coefficient < 0 else "") + series_key
        for index, (coefficient, series_key) in enumerate(terms)
    )
    input_source_keys = _observation_source_keys(
        *(observation for _, observation in inputs)
    )
    return {
        "key": key,
        "label": label,
        "value": float(scaled_value),
        "display_value": f"{scaled_value:,.{decimals}f}{suffix}",
        "change": None,
        "unit": suffix,
        "quality_status": quality,
        "source": "Atlas Macro 计算：" + formula,
        "source_key": "internal",
        "source_keys": sorted(input_source_keys | {"internal"}),
        "as_of": min(item.as_of for _, item in inputs).isoformat(),
        "value_date": min(item.value_date for _, item in inputs).isoformat(),
        "fetched_at": max(item.fetched_at for _, item in inputs).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": ",".join(str(item.batch_id) for _, item in inputs),
        "metadata": {
            "formula": formula,
            "input_series": [series_key for _, series_key in terms],
            "source_keys": sorted(input_source_keys),
        },
    }


def _existing(*metrics: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [metric for metric in metrics if metric is not None]


def _curve_rows(prefix: str, tenors: Iterable[str]) -> list[dict[str, Any]]:
    rows = []
    for tenor in tenors:
        item = _metric(f"{prefix}-{tenor}", tenor, suffix="%")
        if item:
            rows.append(item)
    return rows


def _history_rows(
    series: dict[str, str],
    *,
    limit: int = 120,
    require_all: bool = False,
    source_key: str | None = None,
    batch_id: uuid.UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Align public observations by date while preserving semantic series labels."""

    by_date: dict[str, dict[str, Any]] = {}
    for series_key, label in series.items():
        observations = _latest_observations_by_value_date(
            series_key,
            limit=limit,
            source_key=source_key,
            batch_id=batch_id,
        )
        for observation in reversed(observations):
            day = observation.value_date.date().isoformat()
            row = by_date.setdefault(
                day,
                {"date": day, "_source_keys": [], "_lineage": {}},
            )
            row[label] = float(observation.value)
            source_keys = {observation.source.key}
            fallback_key = None
            if observation.fallback_source_id:
                fallback_key = observation.fallback_source.key
                source_keys.add(fallback_key)
            row["_source_keys"] = sorted(
                {*row["_source_keys"], *source_keys}
            )
            row["_lineage"][label] = {
                "series_key": series_key.lower(),
                "source_key": observation.source.key,
                "source_name": observation.source.name,
                "value_date": observation.value_date.isoformat(),
                "as_of": observation.as_of.isoformat(),
                "fetched_at": observation.fetched_at.isoformat(),
                "batch_id": str(observation.batch_id),
                "quality_status": observation.quality_status,
                "license_scope": observation.source.license_scope,
                "fallback_source": fallback_key,
            }
    rows = [by_date[day] for day in sorted(by_date)]
    if require_all:
        required_labels = set(series.values())
        today_et = timezone.now().astimezone(
            ZoneInfo("America/New_York")
        ).date()
        rows = [
            row
            for row in rows
            if required_labels <= set(row)
            and date.fromisoformat(row["date"]) <= today_et
        ]
    return rows


def _history_chart(
    *,
    key: str,
    title: str,
    series: dict[str, str],
    limit: int = 120,
    description: str = "",
    kind: str = "line",
    source_key: str | None = None,
    batch_id: uuid.UUID | str | None = None,
) -> dict[str, Any] | None:
    """Build a chart contract with component-level source and freshness metadata."""

    rows = _history_rows(
        series,
        limit=limit,
        source_key=source_key,
        batch_id=batch_id,
    )
    if not rows:
        return None
    latest = [
        observation
        for series_key in series
        if (
            observation := _real_observations(
                series_key, source_key=source_key, batch_id=batch_id
            ).first()
        )
        is not None
    ]
    if len(latest) != len(series):
        return None
    deadlines = [_fresh_until(observation) for observation in latest]
    quality_statuses = {observation.quality_status for observation in latest}
    if Observation.Quality.ERROR in quality_statuses:
        quality_status = Observation.Quality.ERROR
    elif timezone.now() > min(deadlines) or Observation.Quality.STALE in quality_statuses:
        quality_status = Observation.Quality.STALE
    elif Observation.Quality.FALLBACK in quality_statuses:
        quality_status = Observation.Quality.FALLBACK
    elif quality_statuses == {Observation.Quality.FRESH}:
        quality_status = Observation.Quality.FRESH
    else:
        quality_status = Observation.Quality.ESTIMATED
    source_keys = {
        source_key
        for observation in latest
        for source_key in (
            observation.source.key,
            observation.fallback_source.key if observation.fallback_source_id else None,
        )
        if source_key
    }
    return {
        "key": key,
        "title": title,
        "description": description,
        "kind": kind,
        "data": rows,
        "source_keys": sorted(source_keys),
        "as_of": min(observation.as_of for observation in latest).isoformat(),
        "fetched_at": max(observation.fetched_at for observation in latest).isoformat(),
        "fresh_until": min(deadlines).isoformat(),
        "quality_status": quality_status,
        "batch_ids": sorted({str(observation.batch_id) for observation in latest}),
        "frequency": (
            latest[0].series.frequency
            if len({observation.series.frequency for observation in latest}) == 1
            else ""
        ),
    }


def _previous_month(period: date) -> date:
    if period.month == 1:
        return date(period.year - 1, 12, 1)
    return date(period.year, period.month - 1, 1)


def _employment_observation_map(
    series_key: str, *, limit: int = 84
) -> dict[date, Observation]:
    return {
        observation.value_date.date().replace(day=1): observation
        for observation in _latest_observations_by_value_date(series_key, limit=limit)
    }


def _derived_employment_quality(current: Observation) -> tuple[str, datetime]:
    fresh_until = _fresh_until(current)
    if current.quality_status == Observation.Quality.ERROR:
        return Observation.Quality.ERROR, fresh_until
    if (
        current.quality_status == Observation.Quality.STALE
        or timezone.now() > fresh_until
    ):
        return Observation.Quality.STALE, fresh_until
    if current.quality_status == Observation.Quality.FALLBACK:
        return Observation.Quality.FALLBACK, fresh_until
    return Observation.Quality.ESTIMATED, fresh_until


def _employment_derived_payload(
    *,
    key: str,
    label: str,
    value: Decimal,
    current: Observation,
    inputs: Iterable[Observation],
    formula: str,
    display_value: str,
    unit: str,
) -> dict[str, Any]:
    input_list = list(inputs)
    quality_status, fresh_until = _derived_employment_quality(current)
    input_source_keys = sorted(_observation_source_keys(*input_list))
    input_batch_ids = sorted({str(item.batch_id) for item in input_list})
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": display_value,
        "change": None,
        "unit": unit,
        "quality_status": quality_status,
        "source": "Atlas Macro 计算：" + formula,
        "source_key": "internal",
        "source_keys": sorted({*input_source_keys, "internal"}),
        "as_of": current.as_of.isoformat(),
        "value_date": current.value_date.isoformat(),
        "fetched_at": max(item.fetched_at for item in input_list).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": ",".join(input_batch_ids),
        "metadata": {
            "formula": formula,
            "input_series": sorted({item.series.key for item in input_list}),
            "source_keys": input_source_keys,
            "input_batch_ids": input_batch_ids,
            "input_value_dates": sorted(
                {item.value_date.isoformat() for item in input_list}
            ),
            "input_lineage": [
                {
                    "series_key": item.series.key,
                    "source_key": item.source.key,
                    "source_name": item.source.name,
                    "license_scope": item.source.license_scope,
                    "value_date": item.value_date.isoformat(),
                    "as_of": item.as_of.isoformat(),
                    "fetched_at": item.fetched_at.isoformat(),
                    "batch_id": str(item.batch_id),
                    "quality_status": item.quality_status,
                    "fallback_source": (
                        item.fallback_source.key
                        if item.fallback_source_id
                        else None
                    ),
                }
                for item in input_list
            ],
            "preliminary": bool((current.metadata or {}).get("preliminary")),
        },
    }


def _employment_derived_data() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]]
]:
    """Build exact-month employment metrics and chart rows from BLS levels."""

    payroll = _employment_observation_map("CES0000000001")
    earnings = _employment_observation_map("CES0500000003")

    changes: dict[date, tuple[Decimal, Observation, Observation]] = {}
    for period, current in payroll.items():
        previous = payroll.get(_previous_month(period))
        if previous is not None:
            changes[period] = (current.value - previous.value, current, previous)

    payroll_rows: list[dict[str, Any]] = []
    latest_change_metric = None
    latest_average_metric = None
    latest_payroll_period = max(payroll, default=None)
    for period in sorted(changes):
        value, current, previous = changes[period]
        row: dict[str, Any] = {
            "date": period.isoformat(),
            "非农新增": float(value),
            "_source_keys": ["bls", "internal"],
            "_lineage": {},
        }
        change_payload = _employment_derived_payload(
            key="nonfarm-payroll-change",
            label="非农新增",
            value=value,
            current=current,
            inputs=(current, previous),
            formula="CES0000000001_t - CES0000000001_t-1",
            display_value=f"{value:+,.0f}K",
            unit="K",
        )
        row["_lineage"]["非农新增"] = {
            **change_payload["metadata"],
            "series_key": change_payload["key"],
            "source_key": "internal",
            "source_name": "Atlas Macro Derived Data",
            "value_date": change_payload["value_date"],
            "as_of": change_payload["as_of"],
            "fetched_at": change_payload["fetched_at"],
            "batch_id": change_payload["batch_id"],
            "quality_status": change_payload["quality_status"],
            "license_scope": "Original calculation from attributed BLS inputs",
            "fallback_source": None,
        }

        previous_period = _previous_month(period)
        third_period = _previous_month(previous_period)
        average_points = [
            changes.get(third_period),
            changes.get(previous_period),
            changes.get(period),
        ]
        if all(point is not None for point in average_points):
            complete_points = [point for point in average_points if point is not None]
            average = sum(
                (point[0] for point in complete_points), Decimal("0")
            ) / Decimal("3")
            average_inputs: list[Observation] = []
            for _, point_current, point_previous in complete_points:
                average_inputs.extend((point_current, point_previous))
            average_payload = _employment_derived_payload(
                key="nonfarm-payroll-change-3m",
                label="非农新增 3M 均值",
                value=average,
                current=current,
                inputs=average_inputs,
                formula="mean(最近 3 个自然月非农就业增量)",
                display_value=f"{average:+,.0f}K",
                unit="K",
            )
            row["3M 均值"] = float(average)
            row["_lineage"]["3M 均值"] = {
                **average_payload["metadata"],
                "series_key": average_payload["key"],
                "source_key": "internal",
                "source_name": "Atlas Macro Derived Data",
                "value_date": average_payload["value_date"],
                "as_of": average_payload["as_of"],
                "fetched_at": average_payload["fetched_at"],
                "batch_id": average_payload["batch_id"],
                "quality_status": average_payload["quality_status"],
                "license_scope": "Original calculation from attributed BLS inputs",
                "fallback_source": None,
            }
            if period == latest_payroll_period:
                latest_average_metric = average_payload
        payroll_rows.append(row)
        if period == latest_payroll_period:
            latest_change_metric = change_payload

    wage_rows: list[dict[str, Any]] = []
    latest_wage_metric = None
    latest_earnings_period = max(earnings, default=None)
    for period in sorted(earnings):
        current = earnings[period]
        prior_period = date(period.year - 1, period.month, 1)
        prior = earnings.get(prior_period)
        if prior is None or prior.value <= 0:
            continue
        value = (current.value / prior.value - Decimal("1")) * Decimal("100")
        payload = _employment_derived_payload(
            key="average-hourly-earnings-yoy",
            label="平均时薪同比",
            value=value,
            current=current,
            inputs=(current, prior),
            formula="100 * (CES0500000003_t / CES0500000003_t-12 - 1)",
            display_value=f"{value:+,.2f}%",
            unit="%",
        )
        wage_rows.append(
            {
                "date": period.isoformat(),
                "平均时薪同比": float(value),
                "_source_keys": ["bls", "internal"],
                "_lineage": {
                    "平均时薪同比": {
                        **payload["metadata"],
                        "series_key": payload["key"],
                        "source_key": "internal",
                        "source_name": "Atlas Macro Derived Data",
                        "value_date": payload["value_date"],
                        "as_of": payload["as_of"],
                        "fetched_at": payload["fetched_at"],
                        "batch_id": payload["batch_id"],
                        "quality_status": payload["quality_status"],
                        "license_scope": (
                            "Original calculation from attributed BLS inputs"
                        ),
                        "fallback_source": None,
                    }
                },
            }
        )
        if period == latest_earnings_period:
            latest_wage_metric = payload
    metrics = [
        item
        for item in (
            latest_change_metric,
            latest_average_metric,
            latest_wage_metric,
        )
        if item is not None
    ]
    return metrics, [payroll_rows, wage_rows]


def _derived_employment_chart(
    *,
    key: str,
    title: str,
    description: str,
    rows: list[dict[str, Any]],
    series: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not rows:
        return None
    latest_lineage = next(
        (
            lineage
            for lineage in rows[-1].get("_lineage", {}).values()
            if isinstance(lineage, dict)
        ),
        None,
    )
    if latest_lineage is None:
        return None
    chart_data: Any = rows
    if series is not None:
        chart_data = {
            "labels": [row["date"] for row in rows],
            "series": [
                {
                    **definition,
                    "data": [row.get(definition["name"]) for row in rows],
                }
                for definition in series
            ],
            "_rows": rows,
        }
    return {
        "key": key,
        "title": title,
        "description": description,
        "kind": "line",
        "data": chart_data,
        "source_keys": ["bls", "internal"],
        "as_of": latest_lineage["as_of"],
        "fetched_at": latest_lineage["fetched_at"],
        "fresh_until": _fresh_until(
            _real_observations("CES0000000001").first()
            if key == "payroll-change"
            else _real_observations("CES0500000003").first()
        ).isoformat(),
        "quality_status": latest_lineage["quality_status"],
        "batch_ids": list(latest_lineage.get("input_batch_ids", [])),
        "frequency": "monthly",
        "time_axis": "date",
        "tab": "payroll",
    }


def _employment_page_data() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    derived_metrics, (payroll_rows, wage_rows) = _employment_derived_data()
    metrics = _existing(
        *derived_metrics,
        _metric("LNS14000000", "失业率", suffix="%"),
        _metric("LNS11300000", "劳动参与率", suffix="%"),
        _metric("JTS000000000000000JOL", "职位空缺", decimals=0, suffix="K"),
        _metric("JTS000000000000000QUR", "主动离职率", suffix="%"),
        _metric(
            INITIAL_SA,
            "初请失业金",
            decimals=0,
            scale=Decimal("0.001"),
            suffix="K",
        ),
        _metric(
            INITIAL_4WK,
            "初请 4 周均值",
            decimals=0,
            scale=Decimal("0.001"),
            suffix="K",
        ),
        _metric(
            CONTINUED_SA,
            "续请周数",
            decimals=0,
            scale=Decimal("0.001"),
            suffix="K",
        ),
        _metric(IUR_SA, "受保失业率", suffix="%"),
    )
    charts = _existing(
        _derived_employment_chart(
            key="payroll-change",
            title="非农月度增量与 3M 均值",
            description="由 BLS 总非农就业水平按相邻自然月差分，单位：千人。",
            rows=payroll_rows,
            series=[
                {"name": "非农新增", "type": "bar"},
                {"name": "3M 均值", "type": "line", "smooth": True},
            ],
        ),
        _derived_employment_chart(
            key="average-hourly-earnings-yoy",
            title="平均时薪同比",
            description="总私营非农平均时薪与精确 t-12 自然月比较，单位：%。",
            rows=wage_rows,
        ),
        _history_chart(
            key="labor-slack",
            title="失业率与劳动参与率",
            description="BLS 家庭调查月度季调序列，单位：%。",
            series={"LNS14000000": "失业率", "LNS11300000": "劳动参与率"},
            limit=84,
        ),
        _history_chart(
            key="jolts-rates",
            title="JOLTS 劳动力周转率",
            description=(
                "直接使用 BLS 官方 rate 序列，不用四舍五入的 level 重算；"
                "openings 是月末存量，其余是整月流量。单位：%。"
            ),
            series={
                "JTS000000000000000JOR": "职位空缺率",
                "JTS000000000000000HIR": "招聘率",
                "JTS000000000000000QUR": "主动离职率",
                "JTS000000000000000LDR": "裁员解雇率",
            },
            limit=84,
        ),
        _history_chart(
            key="initial-claims",
            title="初请失业金与官方 4 周均值",
            description="DOL 全国季调受保失业申领，单位：份。最新周为 advance。",
            series={INITIAL_SA: "初请", INITIAL_4WK: "4 周均值"},
            limit=320,
        ),
        _history_chart(
            key="continued-claims",
            title="续请周数与官方 4 周均值",
            description=(
                "DOL 全国季调 continued weeks claimed，单位：周次；"
                "不代表唯一领取人数。"
            ),
            series={CONTINUED_SA: "续请周数", CONTINUED_4WK: "4 周均值"},
            limit=320,
        ),
    )
    tab_by_key = {
        "payroll-change": "payroll",
        "average-hourly-earnings-yoy": "payroll",
        "labor-slack": "slack",
        "jolts-rates": "turnover",
        "initial-claims": "claims",
        "continued-claims": "claims",
    }
    for chart in charts:
        chart["time_axis"] = "date"
        chart["tab"] = tab_by_key[chart["key"]]
        if chart["key"] == "continued-claims":
            chart["panel_class"] = "lg:col-span-2"
    jolts_rows = _existing(
        _metric("JTS000000000000000JOL", "职位空缺水平", decimals=0, suffix="K"),
        _metric("JTS000000000000000JOR", "职位空缺率", suffix="%"),
        _metric("JTS000000000000000HIL", "招聘水平", decimals=0, suffix="K"),
        _metric("JTS000000000000000HIR", "招聘率", suffix="%"),
        _metric("JTS000000000000000QUL", "主动离职水平", decimals=0, suffix="K"),
        _metric("JTS000000000000000QUR", "主动离职率", suffix="%"),
        _metric("JTS000000000000000LDL", "裁员与解雇水平", decimals=0, suffix="K"),
        _metric("JTS000000000000000LDR", "裁员与解雇率", suffix="%"),
    )
    sections = [
        {
            "title": "JOLTS 官方水平与比率",
            "description": (
                "职位空缺是月末最后一个工作日的存量；招聘、主动离职和"
                "裁员解雇是整月流量。Rate 为 BLS 官方序列，不由 level 重算。"
            ),
            "rows": jolts_rows,
            "full_width": True,
        },
        {
            "title": "口径、修订与发布节奏",
            "body": (
                "非农新增与时薪同比是 Atlas Macro 对 BLS 官方水平序列的"
                "透明派生；JOLTS rate 直接使用 BLS 发布值。CES 会经历两次月度"
                "修订与年度基准修订，JOLTS 首发值为 preliminary。DOL 初请比续请"
                "领先一个经济周；历史 XML 与当周不可变新闻稿 PDF 交叉校验，"
                "重叠尾部以新闻稿当前 vintage 为准。"
            ),
            "full_width": True,
        }
    ]
    return metrics, charts, sections


def _employment_page_is_buildable() -> bool:
    metrics, charts, _ = _employment_page_data()
    metric_keys = {str(item.get("key") or "") for item in metrics}
    chart_keys = {str(item.get("key") or "") for item in charts}
    return EMPLOYMENT_REQUIRED_METRIC_KEYS <= metric_keys and chart_keys == {
        "payroll-change",
        "average-hourly-earnings-yoy",
        "labor-slack",
        "jolts-rates",
        "initial-claims",
        "continued-claims",
    }


def _month_offset(period: date, months_back: int) -> date:
    """Return the first day of an exact prior calendar month."""

    month_index = period.year * 12 + period.month - 1 - months_back
    return date(month_index // 12, month_index % 12 + 1, 1)


def _inflation_observation_map(
    series_key: str,
    *,
    batch_id: uuid.UUID | str | None,
    limit: int = 84,
) -> dict[date, Observation]:
    """Keep every inflation formula input inside one explicit BLS batch."""

    if batch_id is None:
        return {}
    observations = _real_observations(series_key).filter(batch_id=batch_id)[:limit]
    return {
        observation.value_date.date().replace(day=1): observation
        for observation in observations
    }


def _inflation_rate(
    observations: dict[date, Observation],
    *,
    period: date,
    months: int,
    annualized: bool = False,
    require_contiguous: bool = True,
) -> tuple[Decimal, list[Observation]] | None:
    """Calculate an exact-month index change without nearest-date fallback."""

    offsets = range(months, -1, -1) if require_contiguous else (months, 0)
    points = [observations.get(_month_offset(period, offset)) for offset in offsets]
    if any(point is None or point.value <= 0 for point in points):
        return None
    inputs = [point for point in points if point is not None]
    ratio = inputs[-1].value / inputs[0].value
    if annualized:
        if months <= 0 or 12 % months:
            raise ValueError("annualized inflation horizon must divide 12")
        ratio = ratio ** (12 // months)
    return (ratio - Decimal("1")) * Decimal("100"), inputs


def _derived_inflation_quality(
    current: Observation, inputs: Iterable[Observation]
) -> tuple[str, datetime]:
    """A derived rate is current-vintage estimated unless an input is degraded."""

    input_list = list(inputs)
    statuses = {item.quality_status for item in input_list}
    fresh_until = _fresh_until(current)
    if Observation.Quality.ERROR in statuses:
        return Observation.Quality.ERROR, fresh_until
    if Observation.Quality.STALE in statuses or timezone.now() > fresh_until:
        return Observation.Quality.STALE, fresh_until
    if Observation.Quality.FALLBACK in statuses:
        return Observation.Quality.FALLBACK, fresh_until
    return Observation.Quality.ESTIMATED, fresh_until


def _inflation_payload(
    *,
    key: str,
    label: str,
    value: Decimal,
    current: Observation,
    inputs: Iterable[Observation],
    formula: str,
    seasonal_basis: str,
    change: Decimal | None = None,
) -> dict[str, Any]:
    input_list = list(inputs)
    quality_status, fresh_until = _derived_inflation_quality(current, input_list)
    input_source_keys = sorted(_observation_source_keys(*input_list))
    input_batch_ids = sorted({str(item.batch_id) for item in input_list})
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": f"{value:+,.1f}%",
        "change": round(float(change), 2) if change is not None else None,
        "change_unit": "pp",
        "unit": "%",
        "quality_status": quality_status,
        "source": "Atlas Macro 计算：" + formula,
        "source_key": "internal",
        "source_keys": sorted({*input_source_keys, "internal"}),
        "as_of": current.as_of.isoformat(),
        "value_date": current.value_date.isoformat(),
        "fetched_at": max(item.fetched_at for item in input_list).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": ",".join(input_batch_ids),
        "metadata": {
            "formula": formula,
            "input_series": sorted({item.series.key for item in input_list}),
            "source_keys": input_source_keys,
            "input_batch_ids": input_batch_ids,
            "input_value_dates": sorted(
                {item.value_date.isoformat() for item in input_list}
            ),
            "input_lineage": [
                {
                    "series_key": item.series.key,
                    "source_key": item.source.key,
                    "source_name": item.source.name,
                    "license_scope": item.source.license_scope,
                    "value_date": item.value_date.isoformat(),
                    "as_of": item.as_of.isoformat(),
                    "fetched_at": item.fetched_at.isoformat(),
                    "batch_id": str(item.batch_id),
                    "quality_status": item.quality_status,
                    "fallback_source": (
                        item.fallback_source.key
                        if item.fallback_source_id
                        else None
                    ),
                }
                for item in input_list
            ],
            "preliminary": any(
                bool((item.metadata or {}).get("preliminary"))
                for item in input_list
            ),
            "seasonal_basis": seasonal_basis,
            "calculation_owner": "Atlas Macro",
        },
    }


def _inflation_lineage(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload["metadata"],
        "series_key": payload["key"],
        "source_key": "internal",
        "source_name": "Atlas Macro Derived Data",
        "value_date": payload["value_date"],
        "as_of": payload["as_of"],
        "fetched_at": payload["fetched_at"],
        "fresh_until": payload["fresh_until"],
        "batch_id": payload["batch_id"],
        "quality_status": payload["quality_status"],
        "license_scope": "Original calculation from attributed official inputs",
        "fallback_source": None,
    }


def _inflation_series_data(
    *,
    key_prefix: str,
    label: str,
    seasonally_adjusted_series: str,
    not_seasonally_adjusted_series: str | None,
    batch_id: uuid.UUID | str | None,
    input_source_key: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build exact-month inflation rates for one official price index."""

    sa = _inflation_observation_map(
        seasonally_adjusted_series, batch_id=batch_id
    )
    nsa = (
        _inflation_observation_map(
            not_seasonally_adjusted_series, batch_id=batch_id
        )
        if not_seasonally_adjusted_series
        else sa
    )
    if not sa or not nsa or max(sa) != max(nsa):
        return [], []
    yoy_series = not_seasonally_adjusted_series or seasonally_adjusted_series
    yoy_basis = (
        "not_seasonally_adjusted"
        if not_seasonally_adjusted_series
        else "seasonally_adjusted"
    )

    rate_specs = (
        (
            "mom",
            f"{label} 环比",
            sa,
            1,
            False,
            True,
            "seasonally_adjusted",
            (
                f"100 * ({seasonally_adjusted_series}_t / "
                f"{seasonally_adjusted_series}_t-1 - 1)"
            ),
        ),
        (
            "3m-annualized",
            f"{label} 3M 年化",
            sa,
            3,
            True,
            True,
            "seasonally_adjusted",
            (
                f"100 * (({seasonally_adjusted_series}_t / "
                f"{seasonally_adjusted_series}_t-3)^4 - 1)"
            ),
        ),
        (
            "6m-annualized",
            f"{label} 6M 年化",
            sa,
            6,
            True,
            True,
            "seasonally_adjusted",
            (
                f"100 * (({seasonally_adjusted_series}_t / "
                f"{seasonally_adjusted_series}_t-6)^2 - 1)"
            ),
        ),
        (
            "yoy",
            f"{label} 同比",
            nsa,
            12,
            False,
            False,
            yoy_basis,
            (
                f"100 * ({yoy_series}_t / "
                f"{yoy_series}_t-12 - 1)"
            ),
        ),
    )
    payloads: dict[tuple[date, str], dict[str, Any]] = {}
    values: dict[tuple[date, str], Decimal] = {}
    rows: list[dict[str, Any]] = []
    for period in sorted(set(sa) | set(nsa)):
        row: dict[str, Any] = {
            "date": period.isoformat(),
            "_source_keys": [input_source_key, "internal"],
            "_lineage": {},
        }
        for (
            rate_key,
            rate_label,
            observations,
            months,
            annualized,
            require_contiguous,
            seasonal_basis,
            formula,
        ) in rate_specs:
            result = _inflation_rate(
                observations,
                period=period,
                months=months,
                annualized=annualized,
                require_contiguous=require_contiguous,
            )
            current = observations.get(period)
            if result is None or current is None:
                continue
            value, inputs = result
            payload = _inflation_payload(
                key=f"{key_prefix}-{rate_key}",
                label=rate_label,
                value=value,
                current=current,
                inputs=inputs,
                formula=formula,
                seasonal_basis=seasonal_basis,
            )
            row[rate_label] = float(value)
            row["_lineage"][rate_label] = _inflation_lineage(payload)
            payloads[(period, rate_key)] = payload
            values[(period, rate_key)] = value
        if row["_lineage"]:
            rows.append(row)

    latest_period = max(sa)
    metrics: list[dict[str, Any]] = []
    for rate_key in ("mom", "yoy", "3m-annualized", "6m-annualized"):
        payload = payloads.get((latest_period, rate_key))
        if payload is None:
            continue
        previous_value = values.get((_month_offset(latest_period, 1), rate_key))
        if previous_value is not None:
            payload["change"] = round(
                payload["value"] - float(previous_value), 2
            )
        metrics.append(payload)
    return metrics, rows


def _select_lineage_chart_rows(
    rows: Iterable[dict[str, Any]], fields: Iterable[str]
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    field_list = list(fields)
    for row in rows:
        lineage = row.get("_lineage") or {}
        values = {field: row[field] for field in field_list if field in row}
        if not values:
            continue
        selected.append(
            {
                "date": row["date"],
                **values,
                "_source_keys": list(row.get("_source_keys") or []),
                "_lineage": {
                    field: lineage[field]
                    for field in values
                    if field in lineage
                },
            }
        )
    return selected


def _inflation_market_expectations_from_real_rates() -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    """Reuse the audited real-rates Treasury/TIPS snapshot for inflation BEI."""

    snapshot = _latest_treasury_contract_snapshot("real-rates")
    if snapshot is None:
        return [], [], []
    data = dict(snapshot.data or {})
    if (
        snapshot.source.key == "demo-market"
        or data.get("demo") is not False
        or data.get("refresh_failure")
        or snapshot.quality_status
        in {Observation.Quality.ERROR, Observation.Quality.STALE}
    ):
        return [], [], []
    fresh_until_raw = data.get("fresh_until")
    if fresh_until_raw:
        try:
            fresh_until = datetime.fromisoformat(str(fresh_until_raw))
            if fresh_until.tzinfo is None:
                fresh_until = fresh_until.replace(tzinfo=UTC)
            if timezone.now() > fresh_until:
                return [], [], []
        except ValueError:
            return [], [], []

    metric_by_key = {
        str(item.get("key") or ""): item
        for item in data.get("metrics", [])
        if isinstance(item, dict)
    }
    source_metrics = [
        metric_by_key.get("5y-bei"),
        metric_by_key.get("10y-bei"),
    ]
    if any(metric is None for metric in source_metrics):
        return [], [], []
    metrics: list[dict[str, Any]] = []
    for source_metric in source_metrics:
        metric = deepcopy(source_metric)
        metric["key"] = f"market-{metric['key']}"
        metric["label"] = f"{metric['label']}（Treasury 曲线代理）"
        metric["metadata"] = {
            **dict(metric.get("metadata") or {}),
            "component_page_key": "real-rates",
            "component_snapshot_id": snapshot.pk,
            "component_publication_batch_id": str(snapshot.batch_id),
            "component_fingerprint": data.get("fingerprint"),
            "model_label": (
                "Treasury nominal minus TIPS par curve approximation; "
                "not traded breakeven inflation or 5Y5Y"
            ),
        }
        metric["source"] = "Atlas Macro 计算：Treasury 名义曲线 - TIPS 实际曲线"
        metric["source_keys"] = sorted(
            {*metric.get("source_keys", []), "internal", "us-treasury-rates"}
        )
        metrics.append(metric)

    source_chart = next(
        (
            item
            for item in data.get("charts", [])
            if item.get("key") == "nominal-real-breakeven-history"
        ),
        None,
    )
    if not source_chart:
        return [], [], []
    rows = []
    for row in source_chart.get("data", []):
        filtered = {
            "date": row.get("date"),
            "5Y BEI": row.get("5Y BEI"),
            "10Y BEI": row.get("10Y BEI"),
            "_source_keys": row.get("_source_keys", ["us-treasury-rates", "internal"]),
        }
        if "_batch_ids" in row:
            filtered["_batch_ids"] = row["_batch_ids"]
        rows.append(filtered)
    chart = {
        **deepcopy(source_chart),
        "key": "market-breakeven-inflation",
        "title": "Treasury 曲线派生盈亏平衡通胀",
        "description": (
            "复用实际利率页同一官方 Treasury 名义与 TIPS par curve 快照；"
            "5Y/10Y BEI 为 Atlas 透明代理，不是可交易 breakeven 或 5Y5Y。"
        ),
        "data": rows,
        "tab": "expectations",
        "source_keys": sorted(
            {*source_chart.get("source_keys", []), "internal", "us-treasury-rates"}
        ),
    }
    sections = [
        {
            "title": "市场通胀预期代理口径",
            "body": (
                "5Y/10Y BEI 复用实际利率页同一 Treasury 官方曲线快照，"
                "按名义 par yield 减 TIPS real par yield 计算。它是公开政府"
                "曲线派生代理，不是实时交易 breakeven、远期 5Y5Y 或终端报价。"
            ),
            "status": Observation.Quality.ESTIMATED,
            "fresh_until": data.get("fresh_until"),
            "full_width": True,
        }
    ]
    return metrics, [chart], sections


def _lineage_chart(
    *,
    key: str,
    title: str,
    description: str,
    rows: Iterable[dict[str, Any]],
    fields: Iterable[str],
    tab: str,
    frequency: str = "monthly",
    include_internal: bool = True,
    compact_point_lineage: bool = False,
) -> dict[str, Any] | None:
    chart_rows = _select_lineage_chart_rows(rows, fields)
    if not chart_rows:
        return None
    latest_lineage_by_field = {}
    for field in fields:
        lineage = next(
            (
                row["_lineage"][field]
                for row in reversed(chart_rows)
                if field in (row.get("_lineage") or {})
            ),
            None,
        )
        if lineage is not None:
            latest_lineage_by_field[field] = lineage
    latest_lineages = list(latest_lineage_by_field.values())
    if not latest_lineages:
        return None
    statuses = {item["quality_status"] for item in latest_lineages}
    if Observation.Quality.ERROR in statuses:
        quality_status = Observation.Quality.ERROR
    elif Observation.Quality.STALE in statuses:
        quality_status = Observation.Quality.STALE
    elif Observation.Quality.FALLBACK in statuses:
        quality_status = Observation.Quality.FALLBACK
    elif statuses == {Observation.Quality.FRESH}:
        quality_status = Observation.Quality.FRESH
    else:
        quality_status = Observation.Quality.ESTIMATED

    def lineage_batch_ids(lineage: dict[str, Any]) -> set[str]:
        raw_values = [
            *lineage.get("input_batch_ids", []),
            lineage.get("batch_id"),
        ]
        return {
            item.strip()
            for raw_value in raw_values
            for item in str(raw_value or "").split(",")
            if item.strip()
        }

    rendered_rows = chart_rows
    if compact_point_lineage:
        rendered_rows = []
        for row in chart_rows:
            compact_row = {
                key: value
                for key, value in row.items()
                if key not in {"_lineage", "_source_keys"}
            }
            revision_indicators = {
                field: lineage["revision_indicator"]
                for field, lineage in (row.get("_lineage") or {}).items()
                if lineage.get("revision_indicator")
            }
            footnote_ids = {
                field: lineage["footnote_id"]
                for field, lineage in (row.get("_lineage") or {}).items()
                if lineage.get("footnote_id")
            }
            if revision_indicators:
                compact_row["_revision_indicators"] = revision_indicators
            if footnote_ids:
                compact_row["_footnote_ids"] = footnote_ids
            rendered_rows.append(compact_row)

    return {
        "key": key,
        "title": title,
        "description": description,
        "kind": "line",
        "data": rendered_rows,
        "lineage_mode": (
            "series-batch" if compact_point_lineage else "per-point"
        ),
        "series_lineage": (
            latest_lineage_by_field if compact_point_lineage else {}
        ),
        "source_keys": sorted(
            {
                source_key
                for item in latest_lineages
                for source_key in item.get("source_keys", [])
            }
            | ({"internal"} if include_internal else set())
        ),
        "as_of": min(item["as_of"] for item in latest_lineages),
        "fetched_at": max(item["fetched_at"] for item in latest_lineages),
        "fresh_until": min(item["fresh_until"] for item in latest_lineages),
        "quality_status": quality_status,
        "batch_ids": sorted(
            {
                batch_id
                for item in latest_lineages
                for batch_id in lineage_batch_ids(item)
            }
        ),
        "frequency": frequency,
        "time_axis": "date",
        "tab": tab,
    }


def _treasury_dataset(component: str, year: int) -> str:
    return f"{TREASURY_CURVE_DATASET_PREFIXES[component]}:{year}"


def _latest_treasury_attempt(component: str, year: int) -> IngestionRun | None:
    return (
        IngestionRun.objects.filter(
            source__key="us-treasury-rates",
            dataset=_treasury_dataset(component, year),
        )
        .order_by("-started_at", "-id")
        .first()
    )


def _treasury_run_state(
    component: str,
    year: int,
    run: IngestionRun | None,
    *,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "component": component,
        "year": year,
        "dataset": _treasury_dataset(component, year),
        "status": run.status if run is not None else "missing",
        "row_count": run.row_count if run is not None else 0,
        "batch_id": str(run.batch_id) if run is not None else None,
        "refresh_cycle_id": (
            str((run.metadata or {}).get("refresh_cycle_id") or "")
            if run is not None
            else ""
        ),
        "reason": (
            reason
            or (run.error if run is not None else "required annual dataset attempt is missing")
            or str((run.metadata or {}).get("quality_reason") or "")
        )[:240],
    }


def _select_treasury_curve_runs(
    trigger_runs: Iterable[IngestionRun],
    *,
    end_year: int,
) -> tuple[
    dict[tuple[str, int], IngestionRun] | None,
    list[dict[str, Any]],
    bool,
]:
    treasury_triggers = [
        run
        for run in trigger_runs
        if run.source.key == "us-treasury-rates"
        and any(
            run.dataset.startswith(f"{prefix}:")
            for prefix in TREASURY_CURVE_DATASET_PREFIXES.values()
        )
    ]
    if not treasury_triggers:
        return None, [], False

    selected: dict[tuple[str, int], IngestionRun] = {}
    states: list[dict[str, Any]] = []
    for year in range(end_year - TREASURY_CURVE_HISTORY_YEARS, end_year + 1):
        for component in TREASURY_CURVE_DATASET_PREFIXES:
            run = _latest_treasury_attempt(component, year)
            if (
                run is None
                or run.status != IngestionRun.Status.SUCCESS
                or run.row_count <= 0
            ):
                states.append(
                    _treasury_run_state(
                        component,
                        year,
                        run,
                        reason="latest annual dataset attempt is not complete and successful",
                    )
                )
                continue
            selected[(component, year)] = run
            states.append(_treasury_run_state(component, year, run))

    expected_count = (TREASURY_CURVE_HISTORY_YEARS + 1) * len(
        TREASURY_CURVE_DATASET_PREFIXES
    )
    current_runs = [selected.get((component, end_year)) for component in ("nominal", "real")]
    current_cycles = {
        str((run.metadata or {}).get("refresh_cycle_id") or "")
        for run in current_runs
        if run is not None
    }
    current_trigger_batches = {
        str(run.batch_id)
        for run in treasury_triggers
        if run.dataset in {
            _treasury_dataset("nominal", end_year),
            _treasury_dataset("real", end_year),
        }
    }
    selected_current_batches = {
        str(run.batch_id) for run in current_runs if run is not None
    }
    if (
        len(selected) != expected_count
        or any(run is None for run in current_runs)
        or len(current_cycles) != 1
        or "" in current_cycles
        or current_trigger_batches != selected_current_batches
    ):
        if current_trigger_batches != selected_current_batches:
            states.append(
                {
                    "component": "current-cycle",
                    "year": end_year,
                    "dataset": "nominal+real",
                    "status": "mixed",
                    "row_count": 0,
                    "batch_id": None,
                    "refresh_cycle_id": "",
                    "reason": "current nominal and real runs are not the latest paired trigger batches",
                }
            )
        elif len(current_cycles) != 1 or "" in current_cycles:
            states.append(
                {
                    "component": "current-cycle",
                    "year": end_year,
                    "dataset": "nominal+real",
                    "status": "mixed",
                    "row_count": 0,
                    "batch_id": None,
                    "refresh_cycle_id": ",".join(sorted(current_cycles)),
                    "reason": "current nominal and real runs do not share one refresh cycle",
                }
            )
        return None, states, True
    return selected, states, True


def _treasury_observation_maps(
    selected_runs: dict[tuple[str, int], IngestionRun],
) -> tuple[dict[str, dict[date, Observation]] | None, list[dict[str, Any]]]:
    expected_batches = {
        (component, year): str(run.batch_id)
        for (component, year), run in selected_runs.items()
    }
    batch_ids = [run.batch_id for run in selected_runs.values()]
    series_keys = {*TREASURY_NOMINAL_SERIES, *TREASURY_REAL_SERIES}
    observations = (
        Observation.objects.filter(
            source__key="us-treasury-rates",
            batch_id__in=batch_ids,
            series__key__in=series_keys,
        )
        .filter(public_display_license_q())
        .select_related("series", "source", "fallback_source")
        .order_by("value_date", "fetched_at", "id")
    )
    maps: dict[str, dict[date, Observation]] = {key: {} for key in series_keys}
    failures: list[dict[str, Any]] = []
    today_et = timezone.now().astimezone(ZoneInfo("America/New_York")).date()
    for observation in observations:
        series_key = observation.series.key
        period = observation.value_date.date()
        component = "nominal" if series_key.startswith("ust-") else "real"
        expected_batch = expected_batches.get((component, period.year))
        if period > today_et:
            failures.append(
                {
                    "component": component,
                    "year": period.year,
                    "dataset": _treasury_dataset(component, period.year),
                    "status": "future",
                    "reason": f"future observation {period.isoformat()} is not publishable",
                }
            )
            continue
        if expected_batch != str(observation.batch_id):
            failures.append(
                {
                    "component": component,
                    "year": period.year,
                    "dataset": _treasury_dataset(component, period.year),
                    "status": "mixed",
                    "reason": f"{series_key} {period.isoformat()} is outside its annual batch",
                }
            )
            continue
        if period in maps[series_key]:
            failures.append(
                {
                    "component": component,
                    "year": period.year,
                    "dataset": _treasury_dataset(component, period.year),
                    "status": "duplicate",
                    "reason": f"duplicate {series_key} observation for {period.isoformat()}",
                }
            )
            continue
        if (
            observation.fallback_source_id
            or observation.quality_status != Observation.Quality.FRESH
        ):
            failures.append(
                {
                    "component": component,
                    "year": period.year,
                    "dataset": _treasury_dataset(component, period.year),
                    "status": observation.quality_status,
                    "reason": f"{series_key} {period.isoformat()} is fallback or not fresh",
                }
            )
            continue
        maps[series_key][period] = observation
    missing = [key for key, by_date in maps.items() if not by_date]
    if missing:
        failures.append(
            {
                "component": "series-coverage",
                "year": None,
                "dataset": "treasury-curve-history",
                "status": "missing",
                "reason": "missing exact-batch series: " + ", ".join(sorted(missing)),
            }
        )
    if failures:
        return None, failures
    return maps, []


def _treasury_direct_lineage(
    observation: Observation,
    *,
    fresh_until: datetime,
) -> dict[str, Any]:
    return {
        "series_key": observation.series.key,
        "source_key": observation.source.key,
        "source_name": observation.source.name,
        "source_keys": [observation.source.key],
        "license_scope": observation.source.license_scope,
        "value": str(observation.value),
        "raw_value": str(observation.value),
        "value_date": observation.value_date.isoformat(),
        "as_of": observation.as_of.isoformat(),
        "fetched_at": observation.fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(observation.batch_id),
        "quality_status": observation.quality_status,
        "fallback_source": None,
    }


def _treasury_direct_metric(
    *,
    key: str,
    label: str,
    current: Observation,
    previous: Observation,
    fresh_until: datetime,
) -> dict[str, Any]:
    change_bp = (current.value - previous.value) * Decimal("100")
    current_lineage = _treasury_direct_lineage(current, fresh_until=fresh_until)
    previous_lineage = _treasury_direct_lineage(previous, fresh_until=fresh_until)
    return {
        "key": key,
        "label": label,
        "value": float(current.value),
        "display_value": f"{current.value:.2f}%",
        "change": float(change_bp),
        "change_unit": "bp",
        "unit": "%",
        "quality_status": Observation.Quality.FRESH,
        "source": current.source.name,
        "source_key": current.source.key,
        "source_keys": [current.source.key],
        "fallback_source": None,
        "as_of": current.as_of.isoformat(),
        "value_date": current.value_date.isoformat(),
        "fetched_at": current.fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(current.batch_id),
        "metadata": {
            "input_series": [current.series.key],
            "input_batch_ids": [str(current.batch_id)],
            "input_value_dates": [current.value_date.isoformat()],
            "input_lineage": [current_lineage],
            "previous_value": float(previous.value),
            "previous_value_date": previous.value_date.isoformat(),
            "previous_input_lineage": [previous_lineage],
            "freshness_basis": "latest complete Treasury nominal/real curve date",
        },
    }


def _treasury_derived_metric(
    *,
    key: str,
    label: str,
    current_inputs: tuple[Observation, Observation],
    previous_inputs: tuple[Observation, Observation],
    formula: str,
    basis_points: bool,
    fresh_until: datetime,
) -> dict[str, Any]:
    current_value = current_inputs[0].value - current_inputs[1].value
    previous_value = previous_inputs[0].value - previous_inputs[1].value
    if basis_points:
        current_value *= Decimal("100")
        previous_value *= Decimal("100")
    current_lineage = [
        _treasury_direct_lineage(item, fresh_until=fresh_until)
        for item in current_inputs
    ]
    previous_lineage = [
        _treasury_direct_lineage(item, fresh_until=fresh_until)
        for item in previous_inputs
    ]
    input_batches = sorted({str(item.batch_id) for item in current_inputs})
    source_keys = sorted(_observation_source_keys(*current_inputs) | {"internal"})
    unit = "bp" if basis_points else "%"
    display_value = (
        f"{current_value:+.0f}bp" if basis_points else f"{current_value:.2f}%"
    )
    change = (
        current_value - previous_value
        if basis_points
        else (current_value - previous_value) * Decimal("100")
    )
    return {
        "key": key,
        "label": label,
        "value": float(current_value),
        "display_value": display_value,
        "change": float(change),
        "change_unit": "bp",
        "unit": unit,
        "quality_status": Observation.Quality.ESTIMATED,
        "source": "Atlas Macro 计算（U.S. Treasury 输入）",
        "source_key": "internal",
        "source_keys": source_keys,
        "fallback_source": None,
        "as_of": min(item.as_of for item in current_inputs).isoformat(),
        "value_date": current_inputs[0].value_date.isoformat(),
        "fetched_at": max(item.fetched_at for item in current_inputs).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": ",".join(input_batches),
        "metadata": {
            "formula": formula,
            "calculation_owner": "Atlas Macro",
            "input_series": [item.series.key for item in current_inputs],
            "input_batch_ids": input_batches,
            "input_value_dates": sorted(
                {item.value_date.isoformat() for item in current_inputs}
            ),
            "input_lineage": current_lineage,
            "previous_value": float(previous_value),
            "previous_value_date": previous_inputs[0].value_date.isoformat(),
            "previous_input_lineage": previous_lineage,
            "freshness_basis": "latest complete Treasury nominal/real curve date",
            "model_label": (
                "Atlas spread calculation"
                if basis_points
                else "Atlas breakeven approximation from Treasury par curves"
            ),
        },
    }


def _treasury_series_batch_segments(
    selected_runs: dict[tuple[str, int], IngestionRun],
    maps: dict[str, dict[date, Observation]],
    *,
    components: set[str],
) -> dict[str, list[dict[str, Any]]]:
    segments: dict[str, list[dict[str, Any]]] = {}
    for series_key, by_date in maps.items():
        component = "nominal" if series_key.startswith("ust-") else "real"
        if component not in components:
            continue
        rows = []
        for year in sorted({period.year for period in by_date}):
            periods = sorted(period for period in by_date if period.year == year)
            if not periods:
                continue
            run = selected_runs[(component, year)]
            rows.append(
                {
                    "dataset": run.dataset,
                    "batch_id": str(run.batch_id),
                    "start": periods[0].isoformat(),
                    "end": periods[-1].isoformat(),
                    "row_count": len(periods),
                }
            )
        segments[series_key] = rows
    return segments


def _treasury_chart(
    *,
    key: str,
    title: str,
    description: str,
    rows: list[dict[str, Any]],
    selected_runs: dict[tuple[str, int], IngestionRun],
    maps: dict[str, dict[date, Observation]],
    components: set[str],
    current_inputs: list[Observation],
    fresh_until: datetime,
    tab: str,
    time_axis: str,
    estimated: bool,
) -> dict[str, Any]:
    batch_ids = sorted(
        {
            str(run.batch_id)
            for (component, _year), run in selected_runs.items()
            if component in components
        }
    )
    return {
        "key": key,
        "title": title,
        "description": description,
        "kind": "line",
        "data": rows,
        "lineage_mode": "series-batch-segments",
        "series_batch_lineage": _treasury_series_batch_segments(
            selected_runs, maps, components=components
        ),
        "source_keys": [
            "internal",
            "us-treasury-rates",
        ]
        if estimated
        else ["us-treasury-rates"],
        "as_of": min(item.as_of for item in current_inputs).isoformat(),
        "fetched_at": max(item.fetched_at for item in current_inputs).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "quality_status": (
            Observation.Quality.ESTIMATED
            if estimated
            else Observation.Quality.FRESH
        ),
        "batch_ids": batch_ids,
        "frequency": "daily",
        "time_axis": time_axis,
        "tab": tab,
    }


def _treasury_curve_page_data(
    selected_runs: dict[tuple[str, int], IngestionRun],
) -> tuple[
    dict[
        str,
        tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
            dict[str, Any],
        ],
    ]
    | None,
    list[dict[str, Any]],
]:
    maps, failures = _treasury_observation_maps(selected_runs)
    if maps is None:
        return None, failures
    if not publicly_displayable_source_keys({"us-treasury-rates"}):
        return None, [
            {
                "component": "licence",
                "year": None,
                "dataset": "treasury-curve-history",
                "status": "unlicensed",
                "reason": "Treasury curve source is not publicly displayable",
            }
        ]

    nominal_curve_dates = set.intersection(
        *(set(maps[key]) for key in TREASURY_NOMINAL_SERIES)
    )
    real_curve_dates = set.intersection(
        *(set(maps[key]) for key in TREASURY_REAL_SERIES)
    )
    nominal_history_dates = set.intersection(
        *(set(maps[key]) for key in TREASURY_NOMINAL_HISTORY_SERIES)
    )
    real_history_dates = set.intersection(
        *(set(maps[key]) for key in TREASURY_REAL_HISTORY_SERIES)
    )
    common_dates = sorted(nominal_history_dates & real_history_dates)
    if len(common_dates) < 2:
        return None, [
            {
                "component": "common-date",
                "year": None,
                "dataset": "nominal+real",
                "status": "missing",
                "reason": "Treasury nominal and real curves have fewer than two complete common dates",
            }
        ]
    current_date, previous_date = common_dates[-1], common_dates[-2]
    if (
        not nominal_curve_dates
        or not real_curve_dates
        or max(nominal_curve_dates) != current_date
        or max(real_curve_dates) != current_date
    ):
        return None, [
            {
                "component": "current-curve",
                "year": current_date.year,
                "dataset": "nominal+real",
                "status": "missing",
                "reason": "latest common date does not contain every current curve tenor",
            }
        ]
    all_latest_dates = {
        max(by_date) for by_date in maps.values() if by_date
    }
    if all_latest_dates != {current_date}:
        return None, [
            {
                "component": "common-date",
                "year": current_date.year,
                "dataset": "nominal+real",
                "status": "mixed",
                "reason": "latest required tenors do not share the same complete effective date",
            }
        ]

    window_start = current_date - relativedelta(years=TREASURY_CURVE_HISTORY_YEARS)
    window_dates = [period for period in common_dates if period >= window_start]
    if (
        len(window_dates) < TREASURY_CURVE_MIN_HISTORY_POINTS
        or window_dates[0]
        > window_start + timedelta(days=TREASURY_CURVE_START_TOLERANCE_DAYS)
        or any(
            (right - left).days > TREASURY_CURVE_MAX_GAP_DAYS
            for left, right in zip(window_dates, window_dates[1:], strict=False)
        )
    ):
        return None, [
            {
                "component": "history",
                "year": None,
                "dataset": "treasury-curve-history",
                "status": "incomplete",
                "reason": "five-year complete-date history is too short or has an abnormal gap",
            }
        ]

    current_inputs = [maps[key][current_date] for key in (*TREASURY_NOMINAL_SERIES, *TREASURY_REAL_SERIES)]
    fresh_until = min(_fresh_until(item) for item in current_inputs)
    if timezone.now() > fresh_until:
        return None, [
            {
                "component": "freshness",
                "year": current_date.year,
                "dataset": "nominal+real",
                "status": Observation.Quality.STALE,
                "reason": f"latest complete Treasury curve expired at {fresh_until.isoformat()}",
            }
        ]

    comparison_targets = {
        "当前": current_date,
        "1周前": current_date - timedelta(days=7),
        "1月前": current_date - relativedelta(months=1),
        "3月前": current_date - relativedelta(months=3),
    }
    comparison_dates: dict[str, date] = {}
    for label, target in comparison_targets.items():
        candidates = [
            period for period in nominal_curve_dates if period <= target
        ]
        if not candidates or (target - max(candidates)).days > 10:
            return None, [
                {
                    "component": "curve-comparison",
                    "year": target.year,
                    "dataset": "nominal",
                    "status": "missing",
                    "reason": f"no complete Treasury curve near {label} target {target.isoformat()}",
                }
            ]
        comparison_dates[label] = max(candidates)

    nominal_current = {
        key: maps[key][current_date] for key in TREASURY_NOMINAL_SERIES
    }
    nominal_previous = {
        key: maps[key][previous_date] for key in TREASURY_NOMINAL_SERIES
    }
    real_current = {key: maps[key][current_date] for key in TREASURY_REAL_SERIES}
    real_previous = {key: maps[key][previous_date] for key in TREASURY_REAL_SERIES}

    yield_metrics = [
        _treasury_direct_metric(
            key=key,
            label=label,
            current=nominal_current[key],
            previous=nominal_previous[key],
            fresh_until=fresh_until,
        )
        for key, label in (
            ("ust-2y", "2Y 名义收益率"),
            ("ust-5y", "5Y 名义收益率"),
            ("ust-10y", "10Y 名义收益率"),
            ("ust-30y", "30Y 名义收益率"),
        )
    ]
    spread_specs = (
        ("2s10s", "2s10s", "ust-10y", "ust-2y", "100 × (UST-10Y - UST-2Y)"),
        ("3m10s", "3m10s", "ust-10y", "ust-3m", "100 × (UST-10Y - UST-3M)"),
        ("5s30s", "5s30s", "ust-30y", "ust-5y", "100 × (UST-30Y - UST-5Y)"),
    )
    yield_metrics.extend(
        _treasury_derived_metric(
            key=key,
            label=label,
            current_inputs=(nominal_current[left], nominal_current[right]),
            previous_inputs=(nominal_previous[left], nominal_previous[right]),
            formula=formula,
            basis_points=True,
            fresh_until=fresh_until,
        )
        for key, label, left, right, formula in spread_specs
    )

    real_metrics = [
        _treasury_direct_metric(
            key=key,
            label=label,
            current=real_current[key],
            previous=real_previous[key],
            fresh_until=fresh_until,
        )
        for key, label in (
            ("tips-5y", "5Y 实际利率"),
            ("tips-10y", "10Y 实际利率"),
        )
    ]
    bei_specs = (
        ("5y-bei", "5Y 盈亏平衡通胀", "ust-5y", "tips-5y", "UST-5Y - TIPS-5Y"),
        ("10y-bei", "10Y 盈亏平衡通胀", "ust-10y", "tips-10y", "UST-10Y - TIPS-10Y"),
    )
    real_metrics.extend(
        _treasury_derived_metric(
            key=key,
            label=label,
            current_inputs=(nominal_current[left], real_current[right]),
            previous_inputs=(nominal_previous[left], real_previous[right]),
            formula=formula,
            basis_points=False,
            fresh_until=fresh_until,
        )
        for key, label, left, right, formula in bei_specs
    )

    comparison_rows: list[dict[str, Any]] = []
    for tenor in TREASURY_NOMINAL_TENORS:
        series_key = f"ust-{tenor}"
        row: dict[str, Any] = {"label": tenor.upper()}
        row_batches: set[str] = set()
        for label, period in comparison_dates.items():
            observation = maps[series_key][period]
            row[label] = float(observation.value)
            row_batches.add(str(observation.batch_id))
        row["_batch_ids"] = sorted(row_batches)
        row["_source_keys"] = ["us-treasury-rates"]
        comparison_rows.append(row)

    spread_rows: list[dict[str, Any]] = []
    real_rows: list[dict[str, Any]] = []
    for period in window_dates:
        period_nominal = {
            key: maps[key][period] for key in TREASURY_NOMINAL_HISTORY_SERIES
        }
        period_real = {
            key: maps[key][period] for key in TREASURY_REAL_HISTORY_SERIES
        }
        spread_rows.append(
            {
                "date": period.isoformat(),
                "2s10s": float((period_nominal["ust-10y"].value - period_nominal["ust-2y"].value) * Decimal("100")),
                "3m10s": float((period_nominal["ust-10y"].value - period_nominal["ust-3m"].value) * Decimal("100")),
                "5s30s": float((period_nominal["ust-30y"].value - period_nominal["ust-5y"].value) * Decimal("100")),
                "_batch_ids": sorted({str(item.batch_id) for item in period_nominal.values()}),
                "_source_keys": ["us-treasury-rates", "internal"],
            }
        )
        real_rows.append(
            {
                "date": period.isoformat(),
                "5Y 名义": float(period_nominal["ust-5y"].value),
                "5Y 实际": float(period_real["tips-5y"].value),
                "5Y BEI": float(period_nominal["ust-5y"].value - period_real["tips-5y"].value),
                "10Y 名义": float(period_nominal["ust-10y"].value),
                "10Y 实际": float(period_real["tips-10y"].value),
                "10Y BEI": float(period_nominal["ust-10y"].value - period_real["tips-10y"].value),
                "_batch_ids": sorted({str(item.batch_id) for item in (*period_nominal.values(), *period_real.values())}),
                "_source_keys": ["us-treasury-rates", "internal"],
            }
        )

    yield_charts = [
        _treasury_chart(
            key="nominal-curve-comparison",
            title="当前、1 周、1 月与 3 月前名义曲线",
            description="每个回看点取目标日前最近的完整 Treasury 营业日，单位：%。",
            rows=comparison_rows,
            selected_runs=selected_runs,
            maps=maps,
            components={"nominal"},
            current_inputs=list(nominal_current.values()),
            fresh_until=fresh_until,
            tab="curve",
            time_axis="tenor",
            estimated=False,
        ),
        _treasury_chart(
            key="curve-spreads-history",
            title="关键曲线利差历史",
            description="2s10s、3m10s 与 5s30s 均使用同一财政部曲线日期，单位：bp。",
            rows=spread_rows,
            selected_runs=selected_runs,
            maps=maps,
            components={"nominal"},
            current_inputs=list(nominal_current.values()),
            fresh_until=fresh_until,
            tab="spreads",
            time_axis="date",
            estimated=True,
        ),
    ]
    real_charts = [
        _treasury_chart(
            key="nominal-real-breakeven-history",
            title="名义、实际与盈亏平衡通胀",
            description="BEI 为 Atlas Macro 用同期限 Treasury par curve 名义减实际的近似，单位：%。",
            rows=real_rows,
            selected_runs=selected_runs,
            maps=maps,
            components={"nominal", "real"},
            current_inputs=current_inputs,
            fresh_until=fresh_until,
            tab="decomposition",
            time_axis="date",
            estimated=True,
        )
    ]

    nominal_section_rows = [
        {
            "label": tenor.upper(),
            "display_value": f"{nominal_current[f'ust-{tenor}'].value:.2f}%",
            "quality_status": Observation.Quality.FRESH,
            "source": nominal_current[f"ust-{tenor}"].source.name,
            "source_key": "us-treasury-rates",
            "as_of": nominal_current[f"ust-{tenor}"].as_of.isoformat(),
            "batch_id": str(nominal_current[f"ust-{tenor}"].batch_id),
        }
        for tenor in TREASURY_NOMINAL_TENORS
    ]
    real_section_rows = [
        {
            "label": tenor.upper(),
            "display_value": f"{real_current[f'tips-{tenor}'].value:.2f}%",
            "quality_status": Observation.Quality.FRESH,
            "source": real_current[f"tips-{tenor}"].source.name,
            "source_key": "us-treasury-rates",
            "as_of": real_current[f"tips-{tenor}"].as_of.isoformat(),
            "batch_id": str(real_current[f"tips-{tenor}"].batch_id),
        }
        for tenor in TREASURY_REAL_TENORS
    ]
    common_extra = {
        "contract_version": TREASURY_CURVE_CONTRACT_VERSION,
        "common_effective_date": current_date.isoformat(),
        "history_start": window_dates[0].isoformat(),
        "history_end": window_dates[-1].isoformat(),
        "comparison_dates": {
            label: period.isoformat() for label, period in comparison_dates.items()
        },
        "annual_runs": [
            _treasury_run_state(component, year, run)
            for (component, year), run in sorted(selected_runs.items())
        ],
    }
    prepared = {
        "yield-curve": (
            yield_metrics,
            yield_charts,
            [
                {
                    "title": "财政部名义 Par Yield 曲线",
                    "description": "官方收益率横截面；不是债券或 ETF 价格、久期或总回报。",
                    "rows": nominal_section_rows,
                    "fresh_until": fresh_until.isoformat(),
                    "status": Observation.Quality.FRESH,
                    "full_width": True,
                }
            ],
            {**common_extra, "curve_scope": "nominal"},
        ),
        "real-rates": (
            real_metrics,
            real_charts,
            [
                {
                    "title": "财政部实际 Par Yield 曲线",
                    "description": "BEI 为 Atlas 近似通胀补偿，不是财政部发布的官方 BEI 或 5Y5Y。",
                    "rows": real_section_rows,
                    "fresh_until": fresh_until.isoformat(),
                    "status": Observation.Quality.FRESH,
                    "full_width": True,
                }
            ],
            {
                **common_extra,
                "curve_scope": "nominal-real-breakeven",
                "model_disclaimer": "Atlas breakeven approximation from Treasury par curves; no 5Y5Y is published",
            },
        ),
    }
    return prepared, []


def _inflation_page_data(
    *,
    batch_id: uuid.UUID | str | None,
    bea_pio_batch_id: uuid.UUID | str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    headline_metrics, headline_rows = _inflation_series_data(
        key_prefix="headline-cpi",
        label="CPI",
        seasonally_adjusted_series="CUSR0000SA0",
        not_seasonally_adjusted_series="CUUR0000SA0",
        batch_id=batch_id,
        input_source_key="bls",
    )
    core_metrics, core_rows = _inflation_series_data(
        key_prefix="core-cpi",
        label="核心 CPI",
        seasonally_adjusted_series="CUSR0000SA0L1E",
        not_seasonally_adjusted_series="CUUR0000SA0L1E",
        batch_id=batch_id,
        input_source_key="bls",
    )
    shelter_metrics, shelter_rows = _inflation_series_data(
        key_prefix="shelter-cpi",
        label="住房成本 CPI（Shelter）",
        seasonally_adjusted_series="CUSR0000SAH1",
        not_seasonally_adjusted_series="CUUR0000SAH1",
        batch_id=batch_id,
        input_source_key="bls",
    )
    core_goods_metrics, core_goods_rows = _inflation_series_data(
        key_prefix="core-goods-cpi",
        label="核心商品 CPI",
        seasonally_adjusted_series="CUSR0000SACL1E",
        not_seasonally_adjusted_series="CUUR0000SACL1E",
        batch_id=batch_id,
        input_source_key="bls",
    )
    services_metrics, services_rows = _inflation_series_data(
        key_prefix="services-less-energy-cpi",
        label="服务 CPI（不含能源服务）",
        seasonally_adjusted_series="CUSR0000SASLE",
        not_seasonally_adjusted_series="CUUR0000SASLE",
        batch_id=batch_id,
        input_source_key="bls",
    )
    producer_metrics, producer_rows = _inflation_series_data(
        key_prefix="final-demand-ppi",
        label="最终需求 PPI",
        seasonally_adjusted_series="WPSFD4",
        not_seasonally_adjusted_series="WPUFD4",
        batch_id=batch_id,
        input_source_key="bls",
    )
    pce_metrics, pce_rows = _inflation_series_data(
        key_prefix="pce-price-index",
        label="PCE 价格指数",
        seasonally_adjusted_series="BEA-PCE-PRICE-INDEX",
        not_seasonally_adjusted_series=None,
        batch_id=bea_pio_batch_id,
        input_source_key="bea-pio-release",
    )
    core_pce_metrics, core_pce_rows = _inflation_series_data(
        key_prefix="core-pce-price-index",
        label="核心 PCE 价格指数",
        seasonally_adjusted_series="BEA-CORE-PCE-PRICE-INDEX",
        not_seasonally_adjusted_series=None,
        batch_id=bea_pio_batch_id,
        input_source_key="bea-pio-release",
    )
    expectation_metrics, expectation_charts, expectation_sections = (
        _inflation_market_expectations_from_real_rates()
    )
    charts = _existing(
        _lineage_chart(
            key="headline-cpi-rates",
            title="总体 CPI 通胀率与短周期动能",
            description=(
                "环比与 3M/6M 几何年化使用 BLS 季调指数；"
                "12 个月同比使用未季调指数，单位：%。"
            ),
            rows=headline_rows,
            fields=["CPI 环比", "CPI 同比", "CPI 3M 年化", "CPI 6M 年化"],
            tab="headline",
        ),
        _lineage_chart(
            key="core-cpi-rates",
            title="核心 CPI 通胀率与短周期动能",
            description=(
                "剔除食品和能源；环比与动能用季调指数，同比用未季调指数。"
            ),
            rows=core_rows,
            fields=[
                "核心 CPI 环比",
                "核心 CPI 同比",
                "核心 CPI 3M 年化",
                "核心 CPI 6M 年化",
            ],
            tab="core",
        ),
        _lineage_chart(
            key="shelter-cpi-rates",
            title="住房成本 CPI（Shelter）通胀率与短周期动能",
            description=(
                "BLS Shelter 官方聚合项；环比与动能用季调指数，"
                "同比用未季调指数。"
            ),
            rows=shelter_rows,
            fields=[
                "住房成本 CPI（Shelter） 环比",
                "住房成本 CPI（Shelter） 同比",
                "住房成本 CPI（Shelter） 3M 年化",
                "住房成本 CPI（Shelter） 6M 年化",
            ],
            tab="components",
        ),
        _lineage_chart(
            key="core-goods-cpi-rates",
            title="核心商品 CPI 通胀率与短周期动能",
            description=(
                "BLS Commodities less food and energy commodities 官方聚合项；"
                "不使用 headline-core 残差估算。"
            ),
            rows=core_goods_rows,
            fields=[
                "核心商品 CPI 环比",
                "核心商品 CPI 同比",
                "核心商品 CPI 3M 年化",
                "核心商品 CPI 6M 年化",
            ],
            tab="components",
        ),
        _lineage_chart(
            key="services-less-energy-cpi-rates",
            title="服务 CPI（不含能源服务）通胀率与短周期动能",
            description=(
                "BLS Services less energy services 官方聚合项，仍包含 Shelter；"
                "因此不将其标注为“超级核心”通胀。"
            ),
            rows=services_rows,
            fields=[
                "服务 CPI（不含能源服务） 环比",
                "服务 CPI（不含能源服务） 同比",
                "服务 CPI（不含能源服务） 3M 年化",
                "服务 CPI（不含能源服务） 6M 年化",
            ],
            tab="components",
        ),
        _lineage_chart(
            key="final-demand-ppi-rates",
            title="最终需求 PPI 通胀率与短周期动能",
            description=(
                "环比与动能用季调指数，同比用未季调指数；最近四个月可修订。"
            ),
            rows=producer_rows,
            fields=[
                "最终需求 PPI 环比",
                "最终需求 PPI 同比",
                "最终需求 PPI 3M 年化",
                "最终需求 PPI 6M 年化",
            ],
            tab="producer",
        ),
        _lineage_chart(
            key="pce-price-rates",
            title="PCE 价格指数通胀率与短周期动能",
            description=(
                "使用 BEA PIO Section 2 的 PCE chain-type price index；"
                "环比、同比和短期动能均由同一当前 release vintage 透明计算。"
            ),
            rows=pce_rows,
            fields=[
                "PCE 价格指数 环比",
                "PCE 价格指数 同比",
                "PCE 价格指数 3M 年化",
                "PCE 价格指数 6M 年化",
            ],
            tab="pce",
        ),
        _lineage_chart(
            key="core-pce-price-rates",
            title="核心 PCE 价格指数通胀率与短周期动能",
            description=(
                "剔除食品和能源，使用 BEA PIO Section 2 的核心 PCE price index；"
                "全部绑定同一 BEA PIO release-workbook 批次。"
            ),
            rows=core_pce_rows,
            fields=[
                "核心 PCE 价格指数 环比",
                "核心 PCE 价格指数 同比",
                "核心 PCE 价格指数 3M 年化",
                "核心 PCE 价格指数 6M 年化",
            ],
            tab="pce",
        ),
        *expectation_charts,
    )
    sections = [
        {
            "title": "口径、公式与修订",
            "body": (
                "CPI/PPI 环比与 3M/6M 年化只使用季调指数，同比只使用未季调指数。"
                "Shelter、核心商品和不含能源服务的服务 CPI 使用同样的"
                "BLS 季调/未季调配对和同批次约束。"
                "PCE 与核心 PCE 来自 BEA PIO Section 2 的季调 chain-type price index。"
                "3M/6M 按复合增长率年化，缺失精确自然月时保留图表空档，不做"
                "最近日期替代。CPI 季调因子可年度回修；PPI 和 PCE 发布值可能修订。"
            ),
            "full_width": True,
        },
        {
            "title": "尚未接入的通胀层",
            "body": (
                "真实交易 breakeven、5Y5Y 和完整发布 vintage "
                "尚未进入本页原子快照；其来源状态与后续接入建议见下方数据覆盖台账。"
            ),
            "full_width": True,
        },
        *expectation_sections,
    ]
    return [
        *headline_metrics,
        *core_metrics,
        *shelter_metrics,
        *core_goods_metrics,
        *services_metrics,
        *producer_metrics,
        *pce_metrics,
        *core_pce_metrics,
        *expectation_metrics,
    ], charts, sections


def _inflation_page_is_buildable(
    *,
    batch_id: uuid.UUID | str | None,
    bea_pio_batch_id: uuid.UUID | str | None = None,
) -> bool:
    metrics, charts, _ = _inflation_page_data(
        batch_id=batch_id, bea_pio_batch_id=bea_pio_batch_id
    )
    metric_keys = {str(item.get("key") or "") for item in metrics}
    chart_by_key = {str(item.get("key") or ""): item for item in charts}
    expected_chart_keys = {
        "headline-cpi-rates",
        "core-cpi-rates",
        "shelter-cpi-rates",
        "core-goods-cpi-rates",
        "services-less-energy-cpi-rates",
        "final-demand-ppi-rates",
        "pce-price-rates",
        "core-pce-price-rates",
    }
    allowed_chart_keys = {*expected_chart_keys, "market-breakeven-inflation"}
    if (
        not INFLATION_REQUIRED_METRIC_KEYS <= metric_keys
        or not expected_chart_keys <= set(chart_by_key)
        or not set(chart_by_key) <= allowed_chart_keys
    ):
        return False
    required_latest_fields = {
        "headline-cpi-rates": {
            "CPI 环比",
            "CPI 同比",
            "CPI 3M 年化",
            "CPI 6M 年化",
        },
        "core-cpi-rates": {
            "核心 CPI 环比",
            "核心 CPI 同比",
            "核心 CPI 3M 年化",
            "核心 CPI 6M 年化",
        },
        "shelter-cpi-rates": {
            "住房成本 CPI（Shelter） 环比",
            "住房成本 CPI（Shelter） 同比",
            "住房成本 CPI（Shelter） 3M 年化",
            "住房成本 CPI（Shelter） 6M 年化",
        },
        "core-goods-cpi-rates": {
            "核心商品 CPI 环比",
            "核心商品 CPI 同比",
            "核心商品 CPI 3M 年化",
            "核心商品 CPI 6M 年化",
        },
        "services-less-energy-cpi-rates": {
            "服务 CPI（不含能源服务） 环比",
            "服务 CPI（不含能源服务） 同比",
            "服务 CPI（不含能源服务） 3M 年化",
            "服务 CPI（不含能源服务） 6M 年化",
        },
        "final-demand-ppi-rates": {
            "最终需求 PPI 环比",
            "最终需求 PPI 同比",
            "最终需求 PPI 3M 年化",
            "最终需求 PPI 6M 年化",
        },
        "pce-price-rates": {
            "PCE 价格指数 环比",
            "PCE 价格指数 同比",
            "PCE 价格指数 3M 年化",
            "PCE 价格指数 6M 年化",
        },
        "core-pce-price-rates": {
            "核心 PCE 价格指数 环比",
            "核心 PCE 价格指数 同比",
            "核心 PCE 价格指数 3M 年化",
            "核心 PCE 价格指数 6M 年化",
        },
    }
    for chart_key, required_fields in required_latest_fields.items():
        rows = chart_by_key[chart_key].get("data") or []
        if not rows or not required_fields <= set(rows[-1]):
            return False
    return True


def _fed_funds_observation_map(
    series_key: str,
    *,
    batch_id: uuid.UUID | str | None,
    limit: int = 1000,
) -> dict[date, Observation]:
    if batch_id is None:
        return {}
    return {
        item.value_date.date(): item
        for item in _real_observations(series_key).filter(batch_id=batch_id)[:limit]
    }


def _metadata_decimal(observation: Observation, field: str) -> Decimal | None:
    raw_value = (observation.metadata or {}).get(field)
    if raw_value is None or raw_value == "":
        return None
    try:
        value = Decimal(str(raw_value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _fed_funds_input_lineage(
    observations: Iterable[Observation],
) -> list[dict[str, Any]]:
    return [
        {
            "series_key": item.series.key,
            "source_key": item.source.key,
            "source_name": item.source.name,
            "license_scope": item.source.license_scope,
            "value_date": item.value_date.isoformat(),
            "as_of": item.as_of.isoformat(),
            "fetched_at": item.fetched_at.isoformat(),
            "batch_id": str(item.batch_id),
            "quality_status": item.quality_status,
            "fallback_source": (
                item.fallback_source.key if item.fallback_source_id else None
            ),
            "revision_indicator": (item.metadata or {}).get(
                "revisionIndicator"
            ),
            "footnote_id": (item.metadata or {}).get("footnoteId"),
            "prates_status": (item.metadata or {}).get("prates_status"),
        }
        for item in observations
    ]


def _fed_funds_quality(
    current: Observation,
    inputs: Iterable[Observation],
    *,
    derived: bool,
) -> tuple[str, datetime]:
    input_list = list(inputs)
    statuses = {item.quality_status for item in input_list}
    fresh_until = _fresh_until(current)
    if Observation.Quality.ERROR in statuses:
        return Observation.Quality.ERROR, fresh_until
    if Observation.Quality.STALE in statuses or timezone.now() > fresh_until:
        return Observation.Quality.STALE, fresh_until
    if Observation.Quality.FALLBACK in statuses:
        return Observation.Quality.FALLBACK, fresh_until
    if derived or Observation.Quality.ESTIMATED in statuses:
        return Observation.Quality.ESTIMATED, fresh_until
    return Observation.Quality.FRESH, fresh_until


def _fed_funds_payload(
    *,
    key: str,
    label: str,
    value: Decimal,
    current: Observation,
    inputs: Iterable[Observation],
    unit: str,
    decimals: int,
    derived: bool,
    formula: str | None = None,
    source_field: str | None = None,
) -> dict[str, Any]:
    input_list = list(inputs)
    quality_status, fresh_until = _fed_funds_quality(
        current, input_list, derived=derived
    )
    input_source_keys = sorted(_observation_source_keys(*input_list))
    input_batch_ids = sorted({str(item.batch_id) for item in input_list})
    if unit == "bp":
        display_value = f"{value:+,.{decimals}f}bp"
    elif unit == " USD bn":
        display_value = f"{value:,.{decimals}f} USD bn"
    else:
        display_value = f"{value:,.{decimals}f}{unit}"
    source = (
        "Atlas Macro 计算：" + str(formula)
        if derived
        else current.source.name
        + (f" · {source_field}" if source_field else "")
    )
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": display_value,
        "change": None,
        "change_unit": "",
        "unit": unit,
        "quality_status": quality_status,
        "source": source,
        "source_key": "internal" if derived else current.source.key,
        "source_keys": sorted(
            {*input_source_keys, *({"internal"} if derived else set())}
        ),
        "as_of": current.as_of.isoformat(),
        "value_date": current.value_date.isoformat(),
        "fetched_at": max(item.fetched_at for item in input_list).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": ",".join(input_batch_ids),
        "metadata": {
            "formula": formula,
            "source_field": source_field,
            "common_effective_date": current.value_date.date().isoformat(),
            "input_series": sorted({item.series.key for item in input_list}),
            "source_keys": input_source_keys,
            "input_batch_ids": input_batch_ids,
            "input_value_dates": sorted(
                {item.value_date.isoformat() for item in input_list}
            ),
            "input_lineage": _fed_funds_input_lineage(input_list),
            "revision_indicator": (current.metadata or {}).get(
                "revisionIndicator"
            ),
            "footnote_id": (current.metadata or {}).get("footnoteId"),
            "prates_status": (current.metadata or {}).get("prates_status"),
            "calculation_owner": "Atlas Macro" if derived else None,
        },
    }


def _fed_funds_period_payloads(
    *,
    period: date,
    sofr: Observation,
    effr: Observation,
    iorb: Observation,
) -> dict[str, dict[str, Any]]:
    metadata_values = {
        "target-lower": _metadata_decimal(effr, "targetRateFrom"),
        "target-upper": _metadata_decimal(effr, "targetRateTo"),
        "effr-volume": _metadata_decimal(effr, "volumeInBillions"),
        "sofr-volume": _metadata_decimal(sofr, "volumeInBillions"),
        "effr-p1": _metadata_decimal(effr, "percentPercentile1"),
        "effr-p25": _metadata_decimal(effr, "percentPercentile25"),
        "effr-p75": _metadata_decimal(effr, "percentPercentile75"),
        "effr-p99": _metadata_decimal(effr, "percentPercentile99"),
        "sofr-p1": _metadata_decimal(sofr, "percentPercentile1"),
        "sofr-p25": _metadata_decimal(sofr, "percentPercentile25"),
        "sofr-p75": _metadata_decimal(sofr, "percentPercentile75"),
        "sofr-p99": _metadata_decimal(sofr, "percentPercentile99"),
    }
    if any(value is None for value in metadata_values.values()):
        return {}
    values = {
        key: value
        for key, value in metadata_values.items()
        if value is not None
    }
    target_width = values["target-upper"] - values["target-lower"]
    if (
        target_width <= 0
        or values["effr-volume"] <= 0
        or values["sofr-volume"] <= 0
        or not (
            values["effr-p1"]
            <= values["effr-p25"]
            <= effr.value
            <= values["effr-p75"]
            <= values["effr-p99"]
        )
        or not (
            values["sofr-p1"]
            <= values["sofr-p25"]
            <= sofr.value
            <= values["sofr-p75"]
            <= values["sofr-p99"]
        )
    ):
        return {}
    payloads = {
        "effr": _fed_funds_payload(
            key="effr",
            label="EFFR",
            value=effr.value,
            current=effr,
            inputs=(effr,),
            unit="%",
            decimals=2,
            derived=False,
        ),
        "sofr": _fed_funds_payload(
            key="sofr",
            label="SOFR",
            value=sofr.value,
            current=sofr,
            inputs=(sofr,),
            unit="%",
            decimals=2,
            derived=False,
        ),
        "iorb": _fed_funds_payload(
            key="iorb",
            label="IORB",
            value=iorb.value,
            current=iorb,
            inputs=(iorb,),
            unit="%",
            decimals=2,
            derived=False,
        ),
    }
    direct_metadata_specs = (
        ("target-lower", "目标区间下限", effr, "targetRateFrom", "%", 2),
        ("target-upper", "目标区间上限", effr, "targetRateTo", "%", 2),
        ("effr-volume", "EFFR 成交量", effr, "volumeInBillions", " USD bn", 0),
        ("sofr-volume", "SOFR 成交量", sofr, "volumeInBillions", " USD bn", 0),
        ("effr-p1", "EFFR 1P", effr, "percentPercentile1", "%", 2),
        ("effr-p25", "EFFR 25P", effr, "percentPercentile25", "%", 2),
        ("effr-p75", "EFFR 75P", effr, "percentPercentile75", "%", 2),
        ("effr-p99", "EFFR 99P", effr, "percentPercentile99", "%", 2),
        ("sofr-p1", "SOFR 1P", sofr, "percentPercentile1", "%", 2),
        ("sofr-p25", "SOFR 25P", sofr, "percentPercentile25", "%", 2),
        ("sofr-p75", "SOFR 75P", sofr, "percentPercentile75", "%", 2),
        ("sofr-p99", "SOFR 99P", sofr, "percentPercentile99", "%", 2),
    )
    for key, label, observation, source_field, unit, decimals in direct_metadata_specs:
        payloads[key] = _fed_funds_payload(
            key=key,
            label=label,
            value=values[key],
            current=observation,
            inputs=(observation,),
            unit=unit,
            decimals=decimals,
            derived=False,
            source_field=source_field,
        )
    derived_specs = (
        (
            "sofr-effr",
            "SOFR−EFFR",
            (sofr.value - effr.value) * Decimal("100"),
            (sofr, effr),
            "100 * (SOFR - EFFR)",
            "bp",
            0,
        ),
        (
            "sofr-iorb",
            "SOFR−IORB",
            (sofr.value - iorb.value) * Decimal("100"),
            (sofr, iorb),
            "100 * (SOFR - IORB)",
            "bp",
            0,
        ),
        (
            "effr-iorb",
            "EFFR−IORB",
            (effr.value - iorb.value) * Decimal("100"),
            (effr, iorb),
            "100 * (EFFR - IORB)",
            "bp",
            0,
        ),
        (
            "effr-p1-p99-width",
            "EFFR 1P−99P 宽度",
            (values["effr-p99"] - values["effr-p1"]) * Decimal("100"),
            (effr,),
            "100 * (EFFR_99P - EFFR_1P)",
            "bp",
            0,
        ),
        (
            "sofr-p1-p99-width",
            "SOFR 1P−99P 宽度",
            (values["sofr-p99"] - values["sofr-p1"]) * Decimal("100"),
            (sofr,),
            "100 * (SOFR_99P - SOFR_1P)",
            "bp",
            0,
        ),
        (
            "effr-corridor-position",
            "EFFR 走廊位置",
            (
                (effr.value - values["target-lower"])
                / target_width
                * Decimal("100")
            ),
            (effr,),
            "100 * (EFFR - target_lower) / (target_upper - target_lower)",
            "%",
            1,
        ),
    )
    for key, label, value, inputs, formula, unit, decimals in derived_specs:
        payloads[key] = _fed_funds_payload(
            key=key,
            label=label,
            value=value,
            current=inputs[0],
            inputs=inputs,
            unit=unit,
            decimals=decimals,
            derived=True,
            formula=formula,
        )
    if any(
        payload["metadata"]["common_effective_date"] != period.isoformat()
        for payload in payloads.values()
    ):
        return {}
    return payloads


def _fed_funds_lineage(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload["metadata"]
    input_lineage = metadata["input_lineage"]
    return {
        "series_key": payload["key"],
        "source_key": payload["source_key"],
        "source_name": payload["source"],
        "source_keys": payload["source_keys"],
        "value_date": payload["value_date"],
        "as_of": payload["as_of"],
        "fetched_at": payload["fetched_at"],
        "fresh_until": payload["fresh_until"],
        "batch_id": payload["batch_id"],
        "quality_status": payload["quality_status"],
        "license_scope": (
            "Original calculation from attributed official inputs"
            if payload["source_key"] == "internal"
            else input_lineage[0]["license_scope"]
        ),
        "fallback_source": None,
        "source_field": metadata.get("source_field"),
        "revision_indicator": metadata.get("revision_indicator"),
        "footnote_id": metadata.get("footnote_id"),
        "prates_status": metadata.get("prates_status"),
    }


def _fed_funds_chart_row(
    *,
    period: date,
    payloads: dict[str, dict[str, Any]],
    fields: dict[str, str],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": period.isoformat(),
        "_source_keys": [],
        "_lineage": {},
    }
    for payload_key, label in fields.items():
        payload = payloads[payload_key]
        row[label] = payload["value"]
        row["_lineage"][label] = _fed_funds_lineage(payload)
        row["_source_keys"] = sorted(
            {*row["_source_keys"], *payload["source_keys"]}
        )
    return row


def _fed_funds_page_data(
    *,
    dataset_batches: dict[str, uuid.UUID | str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    batches = dataset_batches or {}
    observations = {
        key: _fed_funds_observation_map(
            key,
            batch_id=batches.get(key),
            limit=2000 if key == "iorb" else 800,
        )
        for key in FED_FUNDS_DATASETS
    }
    if any(not values for values in observations.values()):
        return [], [], []
    today_et = timezone.now().astimezone(
        ZoneInfo("America/New_York")
    ).date()
    market_dates = {
        period
        for period in set(observations["sofr"]) & set(observations["effr"])
        if period <= today_et
    }
    if not market_dates:
        return [], [], []
    latest_market_date = max(market_dates)
    latest_iorb = observations["iorb"].get(latest_market_date)
    if latest_iorb is None or not _fed_funds_period_payloads(
        period=latest_market_date,
        sofr=observations["sofr"][latest_market_date],
        effr=observations["effr"][latest_market_date],
        iorb=latest_iorb,
    ):
        return [], [], []
    periods = sorted(
        period for period in market_dates if period in observations["iorb"]
    )
    period_payloads: dict[date, dict[str, dict[str, Any]]] = {}
    corridor_rows = []
    effr_rows = []
    sofr_rows = []
    for period in periods:
        payloads = _fed_funds_period_payloads(
            period=period,
            sofr=observations["sofr"][period],
            effr=observations["effr"][period],
            iorb=observations["iorb"][period],
        )
        if not payloads:
            continue
        period_payloads[period] = payloads
        corridor_rows.append(
            _fed_funds_chart_row(
                period=period,
                payloads=payloads,
                fields={
                    "target-lower": "目标下限",
                    "target-upper": "目标上限",
                    "iorb": "IORB",
                    "effr": "EFFR",
                    "sofr": "SOFR",
                },
            )
        )
        effr_rows.append(
            _fed_funds_chart_row(
                period=period,
                payloads=payloads,
                fields={
                    "effr-p1": "EFFR 1P",
                    "effr-p25": "EFFR 25P",
                    "effr": "EFFR",
                    "effr-p75": "EFFR 75P",
                    "effr-p99": "EFFR 99P",
                },
            )
        )
        sofr_rows.append(
            _fed_funds_chart_row(
                period=period,
                payloads=payloads,
                fields={
                    "sofr-p1": "SOFR 1P",
                    "sofr-p25": "SOFR 25P",
                    "sofr": "SOFR",
                    "sofr-p75": "SOFR 75P",
                    "sofr-p99": "SOFR 99P",
                },
            )
        )
    if not period_payloads:
        return [], [], []
    latest_period = max(period_payloads)
    if latest_period != latest_market_date:
        return [], [], []
    latest = period_payloads[latest_period]
    previous = period_payloads.get(
        max((period for period in period_payloads if period < latest_period), default=None)
    )
    metric_order = (
        "effr",
        "sofr",
        "iorb",
        "target-lower",
        "target-upper",
        "sofr-effr",
        "sofr-iorb",
        "effr-iorb",
        "effr-volume",
        "sofr-volume",
        "effr-p1-p99-width",
        "sofr-p1-p99-width",
        "effr-corridor-position",
    )
    metrics = [dict(latest[key]) for key in metric_order]
    rate_keys = {"effr", "sofr", "iorb", "target-lower", "target-upper"}
    volume_keys = {"effr-volume", "sofr-volume"}
    for metric in metrics:
        key = metric["key"]
        if previous and key in previous:
            factor = Decimal("100") if key in rate_keys else Decimal("1")
            change = (
                Decimal(str(metric["value"]))
                - Decimal(str(previous[key]["value"]))
            ) * factor
            metric["change"] = round(float(change), 2)
            metric["change_unit"] = (
                " USD bn" if key in volume_keys else "bp" if key != "effr-corridor-position" else "pp"
            )
    charts = _existing(
        _lineage_chart(
            key="policy-corridor",
            title="政策走廊与隔夜市场利率",
            description=(
                "EFFR、SOFR 与 IORB 严格对齐到共同有效日；目标区间来自 NY Fed。单位：%。"
            ),
            rows=corridor_rows,
            fields=["目标下限", "目标上限", "IORB", "EFFR", "SOFR"],
            tab="corridor",
            frequency="daily",
            include_internal=False,
            compact_point_lineage=True,
        ),
        _lineage_chart(
            key="effr-distribution",
            title="EFFR 成交分布",
            description="NY Fed 官方 1P/25P/75P/99P 与发布利率，单位：%。",
            rows=effr_rows,
            fields=["EFFR 1P", "EFFR 25P", "EFFR", "EFFR 75P", "EFFR 99P"],
            tab="effr",
            frequency="daily",
            include_internal=False,
            compact_point_lineage=True,
        ),
        _lineage_chart(
            key="sofr-distribution",
            title="SOFR 成交分布",
            description="NY Fed 官方 1P/25P/75P/99P 与发布利率，单位：%。",
            rows=sofr_rows,
            fields=["SOFR 1P", "SOFR 25P", "SOFR", "SOFR 75P", "SOFR 99P"],
            tab="sofr",
            frequency="daily",
            include_internal=False,
            compact_point_lineage=True,
        ),
    )
    latest_distribution = [
        latest[key]
        for key in (
            "effr-p1",
            "effr-p25",
            "effr",
            "effr-p75",
            "effr-p99",
            "effr-volume",
            "sofr-p1",
            "sofr-p25",
            "sofr",
            "sofr-p75",
            "sofr-p99",
            "sofr-volume",
        )
    ]
    sections = [
        {
            "title": f"{latest_period.isoformat()} 官方分布与成交量",
            "description": (
                "分位与成交量直接取 NY Fed reference-rate 响应；"
                "成交量单位为十亿美元。"
            ),
            "rows": latest_distribution,
            "full_width": True,
        },
        {
            "title": "共同有效日与发布规则",
            "body": (
                "当前值只取 SOFR、EFFR 与 IORB 日期交集中的最新美国东部有效日。"
                "PRATES 已公布的周末或未来 IORB 不会与尚未发布的 NY Fed 利率混算；"
                "任一必需数据集失败时继续保留上一版完整快照并标记 stale。"
            ),
            "full_width": True,
        },
    ]
    return metrics, charts, sections


def _fed_funds_page_contract_is_buildable(
    metrics: list[dict[str, Any]], charts: list[dict[str, Any]]
) -> bool:
    metric_keys = {str(item.get("key") or "") for item in metrics}
    expected_chart_keys = {
        "policy-corridor",
        "effr-distribution",
        "sofr-distribution",
    }
    if (
        not FED_FUNDS_REQUIRED_METRIC_KEYS <= metric_keys
        or {str(item.get("key") or "") for item in charts}
        != expected_chart_keys
    ):
        return False
    value_dates = {item.get("value_date") for item in metrics}
    if len(value_dates) != 1:
        return False
    for item in metrics:
        if item.get("quality_status") in {
            Observation.Quality.ERROR,
            Observation.Quality.STALE,
        }:
            return False
        input_dates = set((item.get("metadata") or {}).get("input_value_dates", []))
        if len(input_dates) != 1 or input_dates != value_dates:
            return False
    for chart in charts:
        rows = chart.get("data") or []
        chart_dates = [
            date.fromisoformat(str(row.get("date")))
            for row in rows
            if isinstance(row, dict) and row.get("date")
        ]
        if len(chart_dates) != len(rows) or not chart_dates:
            return False
        latest_chart_date = max(chart_dates)
        if min(chart_dates) > _month_offset(latest_chart_date, 36):
            return False
    return True


def _fed_funds_page_is_buildable(
    *, dataset_batches: dict[str, uuid.UUID | str] | None
) -> bool:
    metrics, charts, _ = _fed_funds_page_data(dataset_batches=dataset_batches)
    return _fed_funds_page_contract_is_buildable(metrics, charts)


def _fed_funds_run_identity(run: IngestionRun) -> str | None:
    for key, (source_key, dataset) in FED_FUNDS_DATASETS.items():
        if run.source.key == source_key and run.dataset == dataset:
            return key
    return None


def _latest_fed_funds_attempt(identity: str) -> IngestionRun | None:
    source_key, dataset = FED_FUNDS_DATASETS[identity]
    return (
        IngestionRun.objects.filter(source__key=source_key, dataset=dataset)
        .order_by("-started_at", "-id")
        .first()
    )


def _fed_funds_run_state(
    identity: str, run: IngestionRun | None
) -> dict[str, Any]:
    source_key, dataset = FED_FUNDS_DATASETS[identity]
    return {
        "component": identity,
        "source": source_key,
        "dataset": dataset,
        "status": run.status if run else "missing",
        "row_count": run.row_count if run else 0,
        "error": (run.error if run else "required dataset run missing")[:240],
        "batch_id": str(run.batch_id) if run else None,
        "refresh_cycle_id": (
            str((run.metadata or {}).get("refresh_cycle_id") or "")
            if run
            else ""
        ),
    }


def _select_fed_funds_runs(
    trigger_runs: Iterable[IngestionRun],
) -> tuple[dict[str, IngestionRun] | None, list[dict[str, Any]], bool]:
    relevant: dict[str, list[IngestionRun]] = {
        key: [] for key in FED_FUNDS_DATASETS
    }
    for run in trigger_runs:
        identity = _fed_funds_run_identity(run)
        if identity:
            relevant[identity].append(run)
    triggered = any(relevant.values())
    if not triggered:
        return None, [], False

    for identity, runs in relevant.items():
        if not runs:
            continue
        latest_attempt = _latest_fed_funds_attempt(identity)
        if latest_attempt is None or any(
            run.pk != latest_attempt.pk for run in runs
        ):
            return None, [], False

    ny_fed_triggered = bool(relevant["sofr"] or relevant["effr"])
    prates_triggered = bool(relevant["iorb"])
    selected: dict[str, IngestionRun | None] = {}
    for identity in FED_FUNDS_DATASETS:
        if (identity in {"sofr", "effr"} and ny_fed_triggered) or (
            identity == "iorb" and prates_triggered
        ):
            selected[identity] = (
                relevant[identity][0]
                if len(relevant[identity]) == 1
                else None
            )
        else:
            selected[identity] = _latest_fed_funds_attempt(identity)

    states = [
        _fed_funds_run_state(identity, selected[identity])
        for identity in FED_FUNDS_DATASETS
    ]
    if any(
        run is None
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        for run in selected.values()
    ):
        return None, states, True
    sofr_cycle = str(
        (selected["sofr"].metadata or {}).get("refresh_cycle_id") or ""
    )
    effr_cycle = str(
        (selected["effr"].metadata or {}).get("refresh_cycle_id") or ""
    )
    if not sofr_cycle or sofr_cycle != effr_cycle:
        return None, states, True
    complete = {
        key: run for key, run in selected.items() if run is not None
    }
    return complete, states, True


def _mark_fed_funds_stale(
    states: list[dict[str, Any]], *, reason: str
) -> None:
    checked_at = timezone.now().isoformat()
    with transaction.atomic():
        latest = (
            DashboardSnapshot.objects.select_for_update()
            .filter(key="fed-funds", is_published=True)
            .exclude(source__key="demo-market")
            .order_by("-created_at")
            .first()
        )
        if latest is None:
            return
        data = dict(latest.data or {})
        data["refresh_failure"] = {
            "checked_at": checked_at,
            "reason": reason,
            "sources": states,
        }
        latest.data = data
        latest.quality_status = Observation.Quality.STALE
        latest.save(update_fields=["data", "quality_status", "updated_at"])


def _latest_fed_funds_snapshot() -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(key="fed-funds", is_published=True)
        .exclude(source__key="demo-market")
        .order_by("-created_at")
        .first()
    )


def _fed_funds_snapshot_effective_date(
    snapshot: DashboardSnapshot,
) -> date:
    metric_dates = {
        str((item.get("metadata") or {}).get("common_effective_date") or "")
        for item in (snapshot.data or {}).get("metrics", [])
        if (item.get("metadata") or {}).get("common_effective_date")
    }
    if len(metric_dates) == 1:
        try:
            return date.fromisoformat(metric_dates.pop())
        except ValueError:
            pass
    return snapshot.as_of.date()


@transaction.atomic
def _coordinate_fed_funds_dashboard(
    trigger_runs: Iterable[IngestionRun],
) -> tuple[list[DashboardSnapshot], set[str]]:
    list(
        Source.objects.select_for_update()
        .filter(
            key__in={source_key for source_key, _ in FED_FUNDS_DATASETS.values()}
        )
        .order_by("key")
        .values_list("pk", flat=True)
    )
    selected, states, triggered = _select_fed_funds_runs(trigger_runs)
    if not triggered:
        return [], set()
    if selected is None:
        _mark_fed_funds_stale(
            states,
            reason=(
                "最近一次 SOFR、EFFR 或 PRATES 必需数据集未成功完成，或"
                "NY Fed 两条参考利率不属于同一刷新周期；继续保留上一版完整快照。"
            ),
        )
        return [], {"fed-funds"}
    dataset_batches = {
        key: run.batch_id for key, run in selected.items()
    }
    prepared_page_data = _fed_funds_page_data(
        dataset_batches=dataset_batches
    )
    metrics, charts, _ = prepared_page_data
    if not _fed_funds_page_contract_is_buildable(metrics, charts):
        _mark_fed_funds_stale(
            states,
            reason=(
                "三个必需数据集没有可发布的非未来共同有效日，或政策走廊、"
                "分位、成交量、许可及批次完整性检查未通过；继续保留上一版。"
            ),
        )
        return [], {"fed-funds"}
    candidate_date = date.fromisoformat(
        str(metrics[0]["metadata"]["common_effective_date"])
    )
    latest_snapshot = _latest_fed_funds_snapshot()
    if (
        latest_snapshot is not None
        and candidate_date
        < _fed_funds_snapshot_effective_date(latest_snapshot)
    ):
        _mark_fed_funds_stale(
            states,
            reason=(
                "本批次最新共同有效日早于当前已发布快照，拒绝回退并继续"
                "保留上一版完整数据。"
            ),
        )
        return [], {"fed-funds"}
    dashboards = publish_official_dashboards(
        keys={"fed-funds"},
        dataset_batches=dataset_batches,
        prepared_fed_funds_data=prepared_page_data,
    )
    latest_snapshot = _latest_fed_funds_snapshot()
    expected_batches = {str(item) for item in dataset_batches.values()}
    if (
        latest_snapshot is None
        or set((latest_snapshot.data or {}).get("component_batches", []))
        != expected_batches
        or (latest_snapshot.data or {}).get("refresh_failure")
    ):
        _mark_fed_funds_stale(
            states,
            reason=(
                "Fed Funds 发布后置条件未满足，继续保留上一版完整快照并"
                "等待下一次双源刷新。"
            ),
        )
        return [], {"fed-funds"}
    return dashboards, set()


def _parse_payload_datetime(raw_value: Any) -> datetime | None:
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw_value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _economy_component_state(
    page_key: str,
    snapshot: DashboardSnapshot | None,
    *,
    reason: str,
    status: str = "invalid",
) -> dict[str, Any]:
    data = dict(snapshot.data or {}) if snapshot is not None else {}
    return {
        "page_key": page_key,
        "status": status,
        "reason": reason[:320],
        "snapshot_id": snapshot.pk if snapshot is not None else None,
        "publication_batch_id": (
            str(snapshot.batch_id) if snapshot is not None else None
        ),
        "snapshot_quality_status": (
            snapshot.quality_status if snapshot is not None else None
        ),
        "fingerprint": data.get("fingerprint"),
    }


def _latest_economy_component_snapshot(
    page_key: str,
) -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(key=page_key, is_published=True)
        .order_by("-created_at", "-id")
        .first()
    )


def _economy_component_payload(
    page_key: str,
    component: dict[str, str],
    *,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | dict[str, Any]:
    snapshot = _latest_economy_component_snapshot(page_key)
    if snapshot is None:
        return _economy_component_state(
            page_key,
            None,
            reason="latest published component snapshot is missing",
            status="missing",
        )
    data = dict(snapshot.data or {})
    if snapshot.source.key == "demo-market" or data.get("demo") is not False:
        return _economy_component_state(
            page_key,
            snapshot,
            reason="latest component snapshot is demo or lacks an explicit real-data flag",
            status="demo",
        )
    if snapshot.quality_status == Observation.Quality.ERROR:
        return _economy_component_state(
            page_key,
            snapshot,
            reason="latest component snapshot has error quality",
            status=Observation.Quality.ERROR,
        )
    if data.get("publication_batch_id") != str(snapshot.batch_id):
        return _economy_component_state(
            page_key,
            snapshot,
            reason="component publication batch does not match its snapshot batch",
        )
    fingerprint = str(data.get("fingerprint") or "")
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint.lower()
    ):
        return _economy_component_state(
            page_key,
            snapshot,
            reason="component fingerprint is missing or malformed",
        )

    metric_key = component["metric_key"]
    metrics = [
        item
        for item in data.get("metrics", [])
        if isinstance(item, dict) and item.get("key") == metric_key
    ]
    if len(metrics) != 1:
        return _economy_component_state(
            page_key,
            snapshot,
            reason=f"required metric {metric_key} is missing or duplicated",
        )
    chart_key = component["chart_key"]
    charts = [
        item
        for item in data.get("charts", [])
        if isinstance(item, dict) and item.get("key") == chart_key
    ]
    if len(charts) != 1:
        return _economy_component_state(
            page_key,
            snapshot,
            reason=f"required chart {chart_key} is missing or duplicated",
        )
    metric = metrics[0]
    chart = charts[0]
    if metric.get("value") is None or metric.get("unit") != "%":
        return _economy_component_state(
            page_key,
            snapshot,
            reason=f"required metric {metric_key} is not a percentage observation",
        )
    for payload_name, payload in (("metric", metric), ("chart", chart)):
        quality = payload.get("quality_status")
        if quality not in {
            Observation.Quality.FRESH,
            Observation.Quality.ESTIMATED,
            Observation.Quality.FALLBACK,
        }:
            return _economy_component_state(
                page_key,
                snapshot,
                reason=f"selected {payload_name} has invalid {quality} quality",
                status=str(quality or "invalid"),
            )
        deadline = _parse_payload_datetime(payload.get("fresh_until"))
        if deadline is None or deadline < now:
            return _economy_component_state(
                page_key,
                snapshot,
                reason=f"selected {payload_name} freshness deadline is missing or expired",
                status=Observation.Quality.STALE,
            )
        if _parse_payload_datetime(payload.get("as_of")) is None or (
            _parse_payload_datetime(payload.get("fetched_at")) is None
        ):
            return _economy_component_state(
                page_key,
                snapshot,
                reason=f"selected {payload_name} lacks valid as_of or fetched_at lineage",
            )
    if not chart.get("data"):
        return _economy_component_state(
            page_key,
            snapshot,
            reason=f"required chart {chart_key} contains no observations",
        )
    chart_rows = chart.get("data")
    if not isinstance(chart_rows, list) or any(
        not isinstance(row, dict)
        or _parse_payload_datetime(row.get("date")) is None
        for row in chart_rows
    ):
        return _economy_component_state(
            page_key,
            snapshot,
            reason=f"required chart {chart_key} has a non-date observation axis",
        )

    component_batches = {
        str(item) for item in data.get("component_batches", []) if item
    }
    metric_batches = _payload_batch_ids(metric)
    chart_batches = _payload_batch_ids(chart)
    if (
        not component_batches
        or not metric_batches
        or not chart_batches
        or not metric_batches <= component_batches
        or not chart_batches <= component_batches
    ):
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected metric or chart batch lineage is outside the component snapshot",
        )
    source_keys = _payload_source_keys([metric, chart])
    missing_current_batches = []
    selected_batches = metric_batches | chart_batches
    for source_key in sorted(source_keys - {"internal"}):
        latest_successful_batch = _latest_successful_source_batch(source_key)
        if (
            latest_successful_batch is not None
            and str(latest_successful_batch) not in selected_batches
        ):
            missing_current_batches.append(
                {
                    "source": source_key,
                    "latest_batch_id": str(latest_successful_batch),
                }
            )
    if missing_current_batches:
        missing_sources = ", ".join(
            item["source"] for item in missing_current_batches
        )
        return _economy_component_state(
            page_key,
            snapshot,
            reason=(
                "selected component has not inherited the latest successful "
                f"source batch: {missing_sources}"
            ),
            status=Observation.Quality.STALE,
        )
    if not publicly_displayable_source_keys(source_keys):
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected metric or chart source licence is not publicly displayable",
            status="unlicensed",
        )
    refresh_failure = data.get("refresh_failure")
    if isinstance(refresh_failure, dict):
        source_states = refresh_failure.get("sources")
        if not isinstance(source_states, list):
            return _economy_component_state(
                page_key,
                snapshot,
                reason="component refresh failure has no source-level isolation",
                status=Observation.Quality.STALE,
            )
        failed_relevant_sources = [
            state
            for state in source_states
            if isinstance(state, dict)
            and state.get("source") in source_keys
            and state.get("status") != IngestionRun.Status.SUCCESS
        ]
        if failed_relevant_sources:
            failed_names = ", ".join(
                str(item.get("source")) for item in failed_relevant_sources
            )
            return _economy_component_state(
                page_key,
                snapshot,
                reason=f"selected source refresh failed: {failed_names}",
                status=Observation.Quality.STALE,
            )

    metric_snapshot = MetricSnapshot.objects.filter(
        key=f"{page_key}-{metric_key}",
        batch_id=snapshot.batch_id,
    ).select_related("source", "fallback_source").first()
    if metric_snapshot is None:
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected metric has no normalized MetricSnapshot row",
        )
    if metric_snapshot.value is None:
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected normalized metric has no numeric value",
        )
    try:
        payload_value = Decimal(str(metric["value"])).quantize(
            Decimal("0.00000001")
        )
    except (ArithmeticError, TypeError, ValueError):
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected metric value is not a valid decimal",
        )
    metric_value_date = _parse_payload_datetime(
        metric.get("value_date") or metric.get("as_of")
    )
    metric_as_of = _parse_payload_datetime(metric.get("as_of"))
    metric_fetched_at = _parse_payload_datetime(metric.get("fetched_at"))
    fallback_key = (
        metric_snapshot.fallback_source.key
        if metric_snapshot.fallback_source_id
        else None
    )
    if any(
        (
            payload_value != metric_snapshot.value.quantize(Decimal("0.00000001")),
            metric_value_date != metric_snapshot.value_date,
            metric_as_of != metric_snapshot.as_of,
            metric_fetched_at != metric_snapshot.fetched_at,
            metric.get("source_key") != metric_snapshot.source.key,
            metric.get("fallback_source") != fallback_key,
            metric.get("quality_status") != metric_snapshot.quality_status,
            metric.get("unit", "") != metric_snapshot.unit,
            not metric_snapshot.license_scope,
        )
    ):
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected metric JSON and MetricSnapshot lineage do not agree",
        )
    metric_formula = (metric.get("metadata") or {}).get("formula")
    if metric_formula and metric_snapshot.metadata.get("formula") != metric_formula:
        return _economy_component_state(
            page_key,
            snapshot,
            reason="selected metric formula differs from its normalized row",
        )
    normalized_metric_batches = _payload_batch_ids(
        {
            "batch_id": metric_snapshot.metadata.get("component_batch_id"),
            "batch_ids": metric_snapshot.metadata.get("input_batch_ids", []),
            "input_lineage": metric_snapshot.metadata.get("input_lineage", []),
        }
    )
    normalized_metric_sources = {
        metric_snapshot.source.key,
        *(
            [metric_snapshot.fallback_source.key]
            if metric_snapshot.fallback_source_id
            else []
        ),
    }
    normalized_metric_sources.update(
        _payload_source_keys(metric_snapshot.metadata)
    )
    if (
        metric_batches != normalized_metric_batches
        or _payload_source_keys(metric) != normalized_metric_sources
    ):
        return _economy_component_state(
            page_key,
            snapshot,
            reason=(
                "selected metric JSON batch or source lineage differs from its "
                "normalized MetricSnapshot row"
            ),
        )

    copied_metric = deepcopy(metric)
    copied_metric["label"] = component["metric_label"]
    copied_metric["license_scope"] = metric_snapshot.license_scope
    copied_metric_metadata = deepcopy(copied_metric.get("metadata") or {})
    copied_metric_metadata.update(
        {
            "component_page_key": page_key,
            "component_snapshot_id": snapshot.pk,
            "component_publication_batch_id": str(snapshot.batch_id),
            "component_fingerprint": fingerprint,
            "component_metric_snapshot_id": metric_snapshot.pk,
            "component_metric_snapshot_key": metric_snapshot.key,
            "component_metric_snapshot_batch_id": str(metric_snapshot.batch_id),
            "inherited_license_scope": metric_snapshot.license_scope,
        }
    )
    copied_metric["metadata"] = copied_metric_metadata

    copied_chart = deepcopy(chart)
    copied_chart.update(
        {
            "tab": component["tab"],
            "time_axis": "date",
            "component_page_key": page_key,
            "component_snapshot_id": snapshot.pk,
            "component_publication_batch_id": str(snapshot.batch_id),
            "component_fingerprint": fingerprint,
        }
    )
    component_reference = {
        "page_key": page_key,
        "snapshot_id": snapshot.pk,
        "snapshot_batch_id": str(snapshot.batch_id),
        "publication_batch_id": str(snapshot.batch_id),
        "fingerprint": fingerprint,
        "snapshot_quality_status": snapshot.quality_status,
        "selected_metric_key": metric_key,
        "selected_chart_key": chart_key,
        "metric_snapshot_id": metric_snapshot.pk,
        "metric_snapshot_key": metric_snapshot.key,
        "metric_quality_status": metric.get("quality_status"),
        "metric_value_date": metric.get("value_date") or metric.get("as_of"),
        "fresh_until": min(
            _parse_payload_datetime(metric["fresh_until"]),
            _parse_payload_datetime(chart["fresh_until"]),
        ).isoformat(),
        "source_keys": sorted(source_keys),
        "component_batches": sorted(component_batches),
        "selected_batches": sorted(metric_batches | chart_batches),
    }
    return copied_metric, copied_chart, component_reference


def _economy_page_data() -> tuple[
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None,
    list[dict[str, Any]],
]:
    metrics: list[dict[str, Any]] = []
    charts: list[dict[str, Any]] = []
    component_snapshots: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    now = timezone.now()
    for page_key, component in ECONOMY_COMPONENTS.items():
        result = _economy_component_payload(page_key, component, now=now)
        if isinstance(result, dict):
            failures.append(result)
            continue
        metric, chart, component_reference = result
        metrics.append(metric)
        charts.append(chart)
        component_snapshots.append(component_reference)
    if failures:
        return None, failures
    return (
        (
            metrics,
            charts,
            [],
            {
                "contract_version": ECONOMY_CONTRACT_VERSION,
                "component_snapshots": component_snapshots,
            },
        ),
        [],
    )


def _latest_economy_snapshot() -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(
            key="economy",
            is_published=True,
            data__contract_version=ECONOMY_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )


def _mark_economy_stale(
    components: list[dict[str, Any]], *, reason: str
) -> None:
    latest = (
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="economy",
            is_published=True,
            data__contract_version=ECONOMY_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )
    if latest is None:
        return
    component_summary = "；".join(
        (
            f"{item.get('page_key') or 'unknown'}"
            f"[{item.get('status') or 'invalid'}] "
            f"{item.get('reason') or 'unknown failure'}"
        )
        for item in components
    )
    public_reason = (
        f"{reason} 失败组件：{component_summary}"
        if component_summary
        else reason
    )
    data = dict(latest.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason": public_reason,
        "components": components,
    }
    latest.data = data
    latest.quality_status = Observation.Quality.STALE
    latest.save(update_fields=["data", "quality_status", "updated_at"])


@transaction.atomic
def _coordinate_economy_dashboard() -> tuple[
    list[DashboardSnapshot], set[str]
]:
    internal = ensure_source("internal")
    Source.objects.select_for_update().get(pk=internal.pk)
    prepared_data, failures = _economy_page_data()
    if prepared_data is None:
        _mark_economy_stale(
            failures,
            reason=(
                "四个必需经济组件未形成完整、有效且许可可公开的组合；"
                "继续保留上一版完整总览。"
            ),
        )
        return [], {"economy"}
    expected_chart_keys = {
        component["chart_key"] for component in ECONOMY_COMPONENTS.values()
    }
    previous = _latest_economy_snapshot()
    try:
        with transaction.atomic():
            dashboards = publish_official_dashboards(
                keys={"economy"},
                prepared_economy_data=prepared_data,
            )
            latest = _latest_economy_snapshot()
            postcondition_failed = (
                latest is None
                or {
                    str(item.get("key") or "")
                    for item in (latest.data or {}).get("metrics", [])
                }
                != set(ECONOMY_REQUIRED_METRIC_KEYS)
                or {
                    str(item.get("key") or "")
                    for item in (latest.data or {}).get("charts", [])
                }
                != expected_chart_keys
                or {
                    str(item.get("page_key") or "")
                    for item in (latest.data or {}).get(
                        "component_snapshots", []
                    )
                }
                != set(ECONOMY_COMPONENTS)
                or (latest.data or {}).get("refresh_failure")
            )
            if postcondition_failed:
                raise ValueError("economy publication postcondition failed")
    except ValueError:
        _mark_economy_stale(
            [
                _economy_component_state(
                    "economy",
                    previous,
                    reason="publication postcondition failed",
                )
            ],
            reason=(
                "经济总览发布后置条件未满足；继续保留快照并等待下一次"
                "完整组件协调。"
            ),
        )
        return [], {"economy"}
    return dashboards, set()


def _liquidity_run_identity(run: IngestionRun) -> str | None:
    for identity, (source_key, dataset) in LIQUIDITY_DATASETS.items():
        if run.source.key == source_key and run.dataset == dataset:
            return identity
    return None


def _latest_liquidity_attempt(identity: str) -> IngestionRun | None:
    source_key, dataset = LIQUIDITY_DATASETS[identity]
    return (
        IngestionRun.objects.filter(source__key=source_key, dataset=dataset)
        .order_by("-started_at", "-id")
        .first()
    )


def _liquidity_run_state(
    identity: str,
    run: IngestionRun | None,
    *,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    source_key, dataset = LIQUIDITY_DATASETS[identity]
    return {
        "component": identity,
        "kind": "ingestion_run",
        "source": source_key,
        "dataset": dataset,
        "status": status or (run.status if run else "missing"),
        "reason": (
            reason
            or (run.error if run else "required dataset run missing")
        )[:320],
        "ingestion_run_id": run.pk if run else None,
        "batch_id": str(run.batch_id) if run else None,
        "row_count": run.row_count if run else 0,
        "refresh_cycle_id": (
            str((run.metadata or {}).get("refresh_cycle_id") or "")
            if run
            else ""
        ),
        "completed_at": (
            run.completed_at.isoformat() if run and run.completed_at else None
        ),
    }


def _select_liquidity_runs(
    trigger_runs: Iterable[IngestionRun],
) -> tuple[dict[str, IngestionRun] | None, list[dict[str, Any]], bool]:
    runs = list(trigger_runs)
    relevant: dict[str, list[IngestionRun]] = {
        identity: [] for identity in LIQUIDITY_DATASETS
    }
    fed_funds_triggers: list[tuple[str, IngestionRun]] = []
    for run in runs:
        identity = _liquidity_run_identity(run)
        if identity:
            relevant[identity].append(run)
            continue
        fed_funds_identity = _fed_funds_run_identity(run)
        if fed_funds_identity:
            fed_funds_triggers.append((fed_funds_identity, run))
    triggered = any(relevant.values()) or bool(fed_funds_triggers)
    if not triggered:
        return None, [], False

    # Delayed/replayed jobs must not overwrite the state established by a newer
    # attempt for the same exact source+dataset identity.
    for identity, identity_runs in relevant.items():
        if not identity_runs:
            continue
        latest = _latest_liquidity_attempt(identity)
        if latest is None or len(identity_runs) != 1 or identity_runs[0].pk != latest.pk:
            return None, [], False
    for identity, run in fed_funds_triggers:
        latest = _latest_fed_funds_attempt(identity)
        if latest is None or run.pk != latest.pk:
            return None, [], False

    selected = {
        identity: _latest_liquidity_attempt(identity)
        for identity in LIQUIDITY_DATASETS
    }
    states = [
        _liquidity_run_state(identity, selected[identity])
        for identity in LIQUIDITY_DATASETS
    ]
    if any(
        run is None
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        for run in selected.values()
    ):
        return None, states, True

    onrrp_cycle = str(
        (selected["onrrp"].metadata or {}).get("refresh_cycle_id") or ""
    )
    tga_cycle = str(
        (selected["tga"].metadata or {}).get("refresh_cycle_id") or ""
    )
    if not onrrp_cycle or onrrp_cycle != tga_cycle:
        states = [
            (
                _liquidity_run_state(
                    identity,
                    run,
                    status="invalid-cycle",
                    reason=(
                        "ON RRP and TGA are not from the same completed refresh cycle"
                    ),
                )
                if identity in {"onrrp", "tga"}
                else _liquidity_run_state(identity, run)
            )
            for identity, run in selected.items()
        ]
        return None, states, True
    return (
        {identity: run for identity, run in selected.items() if run is not None},
        states,
        True,
    )


def _liquidity_component_failure(
    component: str,
    reason: str,
    *,
    status: str = "invalid",
    snapshot: DashboardSnapshot | None = None,
) -> dict[str, Any]:
    return {
        "component": component,
        "kind": "dashboard_snapshot" if snapshot else "contract",
        "status": status,
        "reason": reason[:320],
        "snapshot_id": snapshot.pk if snapshot else None,
        "publication_batch_id": (
            str(snapshot.batch_id) if snapshot else None
        ),
        "fingerprint": (
            str((snapshot.data or {}).get("fingerprint") or "")
            if snapshot
            else ""
        ),
    }


def _liquidity_observation_map(
    series_key: str, *, batch_id: uuid.UUID | str
) -> dict[date, Observation]:
    observations: dict[date, Observation] = {}
    for item in _real_observations(series_key).filter(batch_id=batch_id):
        observations.setdefault(item.value_date.date(), item)
    return observations


def _liquidity_input_lineage(
    observation: Observation, *, component_fresh_until: datetime
) -> dict[str, Any]:
    fallback_key = (
        observation.fallback_source.key
        if observation.fallback_source_id
        else None
    )
    source_keys = sorted(_observation_source_keys(observation))
    return {
        "series_key": observation.series.key,
        "value": float(observation.value),
        "raw_value": str(observation.value),
        "unit": observation.series.unit,
        "source_key": observation.source.key,
        "source_name": observation.source.name,
        "source_keys": source_keys,
        "license_scope": observation.source.license_scope,
        "value_date": observation.value_date.isoformat(),
        "as_of": observation.as_of.isoformat(),
        "fetched_at": observation.fetched_at.isoformat(),
        "batch_id": str(observation.batch_id),
        "quality_status": observation.quality_status,
        "fallback_source": fallback_key,
        "fresh_until": component_fresh_until.isoformat(),
        "observation_period_fresh_until": _fresh_until(
            observation
        ).isoformat(),
    }


def _liquidity_direct_metric(
    *,
    key: str,
    label: str,
    current: Observation,
    previous: Observation,
    scale: Decimal,
    unit: str,
    decimals: int,
    component_fresh_until: datetime,
    page_fresh_until: datetime,
) -> dict[str, Any]:
    value = current.value * scale
    previous_value = previous.value * scale
    current_lineage = _liquidity_input_lineage(
        current, component_fresh_until=component_fresh_until
    )
    previous_lineage = _liquidity_input_lineage(
        previous, component_fresh_until=component_fresh_until
    )
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": f"{value:,.{decimals}f}{unit}",
        "change": round(float(value - previous_value), decimals),
        "change_unit": unit,
        "unit": unit,
        "quality_status": current.quality_status,
        "source": current.source.name,
        "source_key": current.source.key,
        "source_keys": sorted(_observation_source_keys(current)),
        "fallback_source": (
            current.fallback_source.key
            if current.fallback_source_id
            else None
        ),
        "license_scope": current.source.license_scope,
        "as_of": current.as_of.isoformat(),
        "value_date": current.value_date.isoformat(),
        "fetched_at": current.fetched_at.isoformat(),
        "fresh_until": page_fresh_until.isoformat(),
        "batch_id": str(current.batch_id),
        "metadata": {
            "common_effective_date": current.value_date.date().isoformat(),
            "input_series": [current.series.key],
            "input_batch_ids": [str(current.batch_id)],
            "input_value_dates": [current.value_date.isoformat()],
            "input_lineage": [current_lineage],
            "previous_value": float(previous_value),
            "previous_value_date": previous.value_date.isoformat(),
            "previous_input_lineage": [previous_lineage],
            "freshness_basis": (
                "latest successful component release; displayed value aligned "
                "to the liquidity common date"
            ),
        },
    }


def _liquidity_net_metric(
    *,
    current: dict[str, Observation],
    previous: dict[str, Observation],
    component_deadlines: dict[str, datetime],
    page_fresh_until: datetime,
) -> dict[str, Any]:
    ordered_keys = ("walcl", "onrrp", "tga")
    current_value = (
        current["walcl"].value
        - current["onrrp"].value
        - current["tga"].value
    )
    previous_value = (
        previous["walcl"].value
        - previous["onrrp"].value
        - previous["tga"].value
    )
    scale = Decimal("0.000001")
    current_scaled = current_value * scale
    previous_scaled = previous_value * scale
    input_lineage = [
        _liquidity_input_lineage(
            current[key], component_fresh_until=component_deadlines[key]
        )
        for key in ordered_keys
    ]
    previous_input_lineage = [
        _liquidity_input_lineage(
            previous[key], component_fresh_until=component_deadlines[key]
        )
        for key in ordered_keys
    ]
    source_keys = sorted(
        _observation_source_keys(*(current[key] for key in ordered_keys))
    )
    fallback_keys = sorted(
        {
            current[key].fallback_source.key
            for key in ordered_keys
            if current[key].fallback_source_id
        }
    )
    return {
        "key": "net-liquidity",
        "label": "净流动性代理",
        "value": float(current_scaled),
        "display_value": f"{current_scaled:,.6f} USD tn",
        "change": round(float(current_scaled - previous_scaled), 6),
        "change_unit": " USD tn",
        "unit": " USD tn",
        "quality_status": Observation.Quality.ESTIMATED,
        "source": (
            "Atlas Macro 代理计算：WALCL − ON RRP − TGA（非官方 LPI）"
        ),
        "source_key": "internal",
        "source_keys": sorted({*source_keys, "internal"}),
        "fallback_source": fallback_keys[0] if len(fallback_keys) == 1 else None,
        "license_scope": ensure_source("internal").license_scope,
        "as_of": min(current[key].as_of for key in ordered_keys).isoformat(),
        "value_date": current["walcl"].value_date.isoformat(),
        "fetched_at": max(
            current[key].fetched_at for key in ordered_keys
        ).isoformat(),
        "fresh_until": page_fresh_until.isoformat(),
        "batch_id": ",".join(
            sorted({str(current[key].batch_id) for key in ordered_keys})
        ),
        "metadata": {
            "formula": "WALCL - ONRRP - TGA",
            "calculation_owner": "Atlas Macro",
            "model_label": "transparent liquidity proxy; not official LPI",
            "common_effective_date": current[
                "walcl"
            ].value_date.date().isoformat(),
            "input_series": [current[key].series.key for key in ordered_keys],
            "input_batch_ids": sorted(
                {str(current[key].batch_id) for key in ordered_keys}
            ),
            "input_value_dates": sorted(
                {current[key].value_date.isoformat() for key in ordered_keys}
            ),
            "input_lineage": input_lineage,
            "previous_value": float(previous_scaled),
            "previous_value_date": previous[
                "walcl"
            ].value_date.isoformat(),
            "previous_input_lineage": previous_input_lineage,
            "freshness_basis": (
                "minimum current-release deadline across H.4.1, ON RRP and TGA"
            ),
        },
    }


def _liquidity_fed_funds_component(
    *, now: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any]] | dict[str, Any]:
    snapshot = _latest_fed_funds_snapshot()
    if snapshot is None:
        return _liquidity_component_failure(
            "fed-funds",
            "validated Fed Funds component snapshot is missing",
            status="missing",
        )
    data = dict(snapshot.data or {})
    if snapshot.source.key == "demo-market" or data.get("demo") is not False:
        return _liquidity_component_failure(
            "fed-funds", "Fed Funds component is demo data", status="demo", snapshot=snapshot
        )
    if snapshot.quality_status in {
        Observation.Quality.ERROR,
        Observation.Quality.STALE,
        Observation.Quality.FALLBACK,
    } or data.get("refresh_failure"):
        return _liquidity_component_failure(
            "fed-funds",
            "Fed Funds component has an active failure, stale or fallback state",
            status=snapshot.quality_status,
            snapshot=snapshot,
        )
    if data.get("publication_batch_id") != str(snapshot.batch_id):
        return _liquidity_component_failure(
            "fed-funds",
            "Fed Funds publication batch does not match its snapshot batch",
            snapshot=snapshot,
        )
    fingerprint = str(data.get("fingerprint") or "")
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint.lower()
    ):
        return _liquidity_component_failure(
            "fed-funds", "Fed Funds fingerprint is missing or malformed", snapshot=snapshot
        )

    latest_runs = {
        identity: _latest_fed_funds_attempt(identity)
        for identity in FED_FUNDS_DATASETS
    }
    if any(
        run is None
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
        for run in latest_runs.values()
    ):
        return _liquidity_component_failure(
            "fed-funds",
            "a latest SOFR, EFFR or IORB dataset attempt is not successful",
            status=Observation.Quality.STALE,
            snapshot=snapshot,
        )
    expected_batches = {
        str(run.batch_id) for run in latest_runs.values() if run is not None
    }
    if set(data.get("component_batches", [])) != expected_batches:
        return _liquidity_component_failure(
            "fed-funds",
            "Fed Funds snapshot does not inherit the latest three exact dataset batches",
            status=Observation.Quality.STALE,
            snapshot=snapshot,
        )
    input_specs = {
        "sofr": ("ny-fed-markets", str(latest_runs["sofr"].batch_id)),
        "effr": ("ny-fed-markets", str(latest_runs["effr"].batch_id)),
        "iorb": ("federal-reserve", str(latest_runs["iorb"].batch_id)),
    }
    expected_metric_inputs = {
        "sofr": {"sofr"},
        "iorb": {"iorb"},
        "sofr-effr": {"sofr", "effr"},
        "sofr-iorb": {"sofr", "iorb"},
    }
    expected_formulas = {
        "sofr": None,
        "iorb": None,
        "sofr-effr": "100 * (SOFR - EFFR)",
        "sofr-iorb": "100 * (SOFR - IORB)",
    }

    copied_metrics: list[dict[str, Any]] = []
    metric_snapshot_ids: list[int] = []
    value_dates: set[str] = set()
    for metric_key in sorted(LIQUIDITY_FED_FUNDS_METRIC_KEYS):
        candidates = [
            item
            for item in data.get("metrics", [])
            if isinstance(item, dict) and item.get("key") == metric_key
        ]
        if len(candidates) != 1:
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} is missing or duplicated",
                snapshot=snapshot,
            )
        metric = candidates[0]
        metadata = dict(metric.get("metadata") or {})
        deadline = _parse_payload_datetime(metric.get("fresh_until"))
        metric_date = _parse_payload_datetime(
            metric.get("value_date") or metric.get("as_of")
        )
        metric_as_of = _parse_payload_datetime(metric.get("as_of"))
        metric_fetched_at = _parse_payload_datetime(metric.get("fetched_at"))
        if (
            metric.get("value") is None
            or metric.get("quality_status")
            not in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
            or deadline is None
            or deadline < now
            or metric_date is None
            or metric_as_of is None
            or metric_fetched_at is None
            or metric.get("fallback_source")
        ):
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} is invalid or expired",
                status=Observation.Quality.STALE,
                snapshot=snapshot,
            )
        input_lineage = metadata.get("input_lineage")
        input_batches = {
            str(item) for item in metadata.get("input_batch_ids", []) if item
        }
        input_dates = {
            str(item) for item in metadata.get("input_value_dates", []) if item
        }
        expected_inputs = expected_metric_inputs[metric_key]
        expected_input_batches = {
            input_specs[series_key][1] for series_key in expected_inputs
        }
        if (
            not isinstance(input_lineage, list)
            or len(input_lineage) != len(expected_inputs)
            or input_batches != expected_input_batches
            or set(metadata.get("input_series") or []) != expected_inputs
            or metadata.get("formula") != expected_formulas[metric_key]
            or input_dates != {metric_date.isoformat()}
            or _payload_batch_ids(metric) != input_batches
            or any(
                not isinstance(item, dict)
                or str(item.get("series_key") or "").lower()
                not in expected_inputs
                or item.get("source_key")
                != input_specs[str(item.get("series_key") or "").lower()][0]
                or not item.get("license_scope")
                or item.get("value_date") != metric_date.isoformat()
                or _parse_payload_datetime(item.get("as_of")) is None
                or not item.get("fetched_at")
                or item.get("batch_id")
                != input_specs[str(item.get("series_key") or "").lower()][1]
                or item.get("quality_status")
                not in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
                or item.get("fallback_source")
                for item in input_lineage
            )
            or {
                str(item.get("series_key") or "").lower()
                for item in input_lineage
            }
            != expected_inputs
        ):
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} lacks exact input lineage",
                snapshot=snapshot,
            )
        source_keys = _payload_source_keys(metric)
        if not source_keys or not publicly_displayable_source_keys(source_keys):
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} has an unlicensed source",
                status="unlicensed",
                snapshot=snapshot,
            )
        normalized = (
            MetricSnapshot.objects.filter(
                key=f"fed-funds-{metric_key}", batch_id=snapshot.batch_id
            )
            .select_related("source", "fallback_source")
            .first()
        )
        if normalized is None or normalized.value is None:
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} has no normalized row",
                snapshot=snapshot,
            )
        try:
            payload_value = Decimal(str(metric["value"])).quantize(
                Decimal("0.00000001")
            )
        except (ArithmeticError, TypeError, ValueError):
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} is not numeric",
                snapshot=snapshot,
            )
        if any(
            (
                payload_value
                != normalized.value.quantize(Decimal("0.00000001")),
                metric_date != normalized.value_date,
                metric_as_of != normalized.as_of,
                metric_fetched_at != normalized.fetched_at,
                metric.get("source_key") != normalized.source.key,
                bool(normalized.fallback_source_id),
                metric.get("quality_status") != normalized.quality_status,
                metric.get("unit", "") != normalized.unit,
                not normalized.license_scope,
                {
                    str(item)
                    for item in normalized.metadata.get("input_batch_ids", [])
                    if item
                }
                != input_batches,
                normalized.metadata.get("input_lineage") != input_lineage,
                set(normalized.metadata.get("input_series") or [])
                != expected_inputs,
                normalized.metadata.get("formula")
                != expected_formulas[metric_key],
                normalized.metadata.get("common_effective_date")
                != metric_date.date().isoformat(),
            )
        ):
            return _liquidity_component_failure(
                "fed-funds",
                f"required Fed Funds metric {metric_key} disagrees with its normalized row",
                snapshot=snapshot,
            )
        copied = deepcopy(metric)
        copied_metadata = dict(copied.get("metadata") or {})
        copied_metadata.update(
            {
                "component_page_key": "fed-funds",
                "component_snapshot_id": snapshot.pk,
                "component_publication_batch_id": str(snapshot.batch_id),
                "component_fingerprint": fingerprint,
                "component_metric_snapshot_id": normalized.pk,
                "component_metric_snapshot_key": normalized.key,
                "component_metric_snapshot_batch_id": str(normalized.batch_id),
                "inherited_license_scope": normalized.license_scope,
            }
        )
        copied["metadata"] = copied_metadata
        copied["license_scope"] = normalized.license_scope
        copied_metrics.append(copied)
        metric_snapshot_ids.append(normalized.pk)
        value_dates.add(metric_date.isoformat())
    if len(value_dates) != 1:
        return _liquidity_component_failure(
            "fed-funds",
            "selected Fed Funds metrics do not share one effective date",
            snapshot=snapshot,
        )
    reference = {
        "component": "fed-funds",
        "kind": "dashboard_snapshot",
        "status": "valid",
        "snapshot_id": snapshot.pk,
        "publication_batch_id": str(snapshot.batch_id),
        "fingerprint": fingerprint,
        "component_batches": sorted(expected_batches),
        "metric_snapshot_ids": metric_snapshot_ids,
        "common_effective_date": value_dates.pop(),
    }
    return copied_metrics, reference


def _liquidity_net_from_lineage(lineage: Iterable[dict[str, Any]]) -> Decimal | None:
    items = list(lineage)
    if len(items) != 3:
        return None
    values: dict[str, Decimal] = {}
    try:
        for item in items:
            if not isinstance(item, dict):
                return None
            series_key = str(item.get("series_key") or "").lower()
            raw_value = item.get("raw_value", item.get("value"))
            if not series_key or raw_value is None or series_key in values:
                return None
            value = Decimal(str(raw_value))
            if not value.is_finite():
                return None
            values[series_key] = value
    except (ArithmeticError, TypeError, ValueError):
        return None
    if set(values) != {"walcl", "onrrp", "tga"}:
        return None
    return (
        values["walcl"] - values["onrrp"] - values["tga"]
    ) * Decimal("0.000001")


def _liquidity_page_contract_is_buildable(
    metrics: list[dict[str, Any]],
    charts: list[dict[str, Any]],
    extra_data: dict[str, Any],
) -> bool:
    if extra_data.get("contract_version") != LIQUIDITY_CONTRACT_VERSION:
        return False
    if {str(item.get("key") or "") for item in metrics} != set(
        LIQUIDITY_REQUIRED_METRIC_KEYS
    ):
        return False
    if {str(item.get("key") or "") for item in charts} != {
        "net-liquidity-history"
    }:
        return False
    references = extra_data.get("component_snapshots")
    if not isinstance(references, list) or {
        str(item.get("component") or "")
        for item in references
        if isinstance(item, dict)
    } != {"h41", "onrrp", "tga", "fed-funds"}:
        return False
    if any(
        item.get("value") is None
        or item.get("quality_status")
        not in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
        or not item.get("fresh_until")
        or item.get("fallback_source")
        or not _payload_batch_ids(item)
        or not _payload_source_keys(item)
        for item in metrics
    ):
        return False
    if not publicly_displayable_source_keys(_payload_source_keys(metrics)):
        return False

    def safe_lineage(item: Any) -> bool:
        return bool(
            isinstance(item, dict)
            and item.get("series_key")
            and item.get("source_key")
            and item.get("license_scope")
            and item.get("value_date")
            and item.get("as_of")
            and item.get("fetched_at")
            and item.get("batch_id")
            and item.get("quality_status")
            in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
            and not item.get("fallback_source")
        )

    for metric in metrics:
        metric_metadata = metric.get("metadata") or {}
        for lineage_field in ("input_lineage", "previous_input_lineage"):
            lineage = metric_metadata.get(lineage_field)
            if lineage is not None and (
                not isinstance(lineage, list)
                or not lineage
                or any(not safe_lineage(item) for item in lineage)
            ):
                return False

    by_key = {str(item["key"]): item for item in metrics}
    direct_keys = {"net-liquidity", "walcl", "wrbwfrbl", "onrrp", "tga"}
    common_dates = {
        str((by_key[key].get("metadata") or {}).get("common_effective_date") or "")
        for key in direct_keys
    }
    if len(common_dates) != 1 or common_dates != {
        str(extra_data.get("common_effective_date") or "")
    }:
        return False
    net_metric = by_key["net-liquidity"]
    metadata = net_metric.get("metadata") or {}
    if metadata.get("formula") != "WALCL - ONRRP - TGA":
        return False
    current_value = _liquidity_net_from_lineage(metadata.get("input_lineage") or [])
    previous_value = _liquidity_net_from_lineage(
        metadata.get("previous_input_lineage") or []
    )
    if current_value is None or previous_value is None:
        return False
    if current_value.quantize(Decimal("0.000001")) != Decimal(
        str(net_metric["value"])
    ).quantize(Decimal("0.000001")):
        return False
    if previous_value.quantize(Decimal("0.000001")) != Decimal(
        str(metadata.get("previous_value"))
    ).quantize(Decimal("0.000001")):
        return False
    if {
        str(item.get("value_date") or "")
        for item in metadata.get("input_lineage", [])
    } != {str(net_metric.get("value_date") or "")}:
        return False

    chart = charts[0]
    rows = chart.get("data") or []
    if len(rows) < 2 or chart.get("time_axis") != "date":
        return False
    if max(str(row.get("date") or "") for row in rows) != next(
        iter(common_dates)
    ):
        return False
    expected_fields = {
        "Net Liquidity",
        "Federal Reserve Assets",
        "ON RRP",
        "TGA",
    }
    for row in rows:
        if not expected_fields <= set(row):
            return False
        row_lineage = row.get("_lineage") or {}
        if set(row_lineage) != expected_fields or any(
            not safe_lineage(item) for item in row_lineage.values()
        ):
            return False
        net_lineage = row_lineage.get("Net Liquidity") or {}
        if any(
            not safe_lineage(item)
            for item in net_lineage.get("input_lineage") or []
        ):
            return False
        recomputed = _liquidity_net_from_lineage(
            net_lineage.get("input_lineage") or []
        )
        if recomputed is None or recomputed.quantize(
            Decimal("0.000001")
        ) != Decimal(str(row["Net Liquidity"])).quantize(
            Decimal("0.000001")
        ):
            return False
    run_batches = {
        str(item.get("batch_id"))
        for item in references
        if item.get("kind") == "ingestion_run" and item.get("batch_id")
    }
    if set(chart.get("batch_ids", [])) != run_batches:
        return False
    if set(chart.get("source_keys", [])) != {
        "federal-reserve",
        "ny-fed-markets",
        "treasury-fiscal-data",
        "internal",
    }:
        return False
    return True


def _liquidity_page_data(
    selected_runs: dict[str, IngestionRun],
) -> tuple[
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None,
    list[dict[str, Any]],
]:
    series_batches = {
        "walcl": selected_runs["h41"].batch_id,
        "wrbwfrbl": selected_runs["h41"].batch_id,
        "onrrp": selected_runs["onrrp"].batch_id,
        "tga": selected_runs["tga"].batch_id,
    }
    observations = {
        key: _liquidity_observation_map(key, batch_id=batch_id)
        for key, batch_id in series_batches.items()
    }
    missing = [key for key, values in observations.items() if not values]
    if missing:
        return None, [
            _liquidity_component_failure(
                "direct-inputs",
                "required exact-batch series missing or unlicensed: "
                + ", ".join(missing),
                status="missing",
            )
        ]

    now = timezone.now()
    today_et = now.astimezone(ZoneInfo("America/New_York")).date()
    component_deadlines: dict[str, datetime] = {}
    expected_sources = {
        "walcl": "federal-reserve",
        "wrbwfrbl": "federal-reserve",
        "onrrp": "ny-fed-markets",
        "tga": "treasury-fiscal-data",
    }
    for key, by_date in observations.items():
        nonfuture = [period for period in by_date if period <= today_et]
        if not nonfuture:
            return None, [
                _liquidity_component_failure(
                    key,
                    "exact-batch series has no non-future observation",
                    status="missing",
                )
            ]
        latest = by_date[max(nonfuture)]
        deadline = _fresh_until(latest)
        if (
            latest.source.key != expected_sources[key]
            or latest.batch_id != uuid.UUID(str(series_batches[key]))
            or latest.fallback_source_id
            or latest.quality_status
            not in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
            or deadline < now
        ):
            return None, [
                _liquidity_component_failure(
                    key,
                    "latest exact-batch component is stale, fallback, mixed-batch or from the wrong source",
                    status=Observation.Quality.STALE,
                )
            ]
        component_deadlines[key] = deadline
    if not publicly_displayable_source_keys(set(expected_sources.values())):
        return None, [
            _liquidity_component_failure(
                "direct-inputs",
                "a required direct input licence is not publicly displayable",
                status="unlicensed",
            )
        ]

    common_dates = sorted(
        (
            set(observations["walcl"])
            & set(observations["wrbwfrbl"])
            & set(observations["onrrp"])
            & set(observations["tga"])
        )
        & {period for period in observations["walcl"] if period <= today_et}
    )
    if len(common_dates) < 2:
        return None, [
            _liquidity_component_failure(
                "common-date",
                "the four exact-batch direct series have fewer than two non-future common dates",
                status="missing",
            )
        ]
    displayed_dates = common_dates[-156:]
    for period in displayed_dates:
        for key, by_date in observations.items():
            item = by_date[period]
            if (
                item.source.key != expected_sources[key]
                or str(item.batch_id) != str(series_batches[key])
                or item.fallback_source_id
                or item.quality_status
                not in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
            ):
                return None, [
                    _liquidity_component_failure(
                        key,
                        (
                            f"displayed common-date input {period.isoformat()} is "
                            "fallback, invalid quality, mixed-batch or from the wrong source"
                        ),
                        status=(
                            Observation.Quality.FALLBACK
                            if item.fallback_source_id
                            else Observation.Quality.STALE
                        ),
                    )
                ]
    current_date, previous_date = common_dates[-1], common_dates[-2]
    current = {key: values[current_date] for key, values in observations.items()}
    previous = {key: values[previous_date] for key, values in observations.items()}
    page_fresh_until = min(component_deadlines.values())

    metrics = [
        _liquidity_net_metric(
            current=current,
            previous=previous,
            component_deadlines=component_deadlines,
            page_fresh_until=page_fresh_until,
        ),
        _liquidity_direct_metric(
            key="walcl",
            label="联储总资产（共同日）",
            current=current["walcl"],
            previous=previous["walcl"],
            scale=Decimal("0.000001"),
            unit=" USD tn",
            decimals=6,
            component_fresh_until=component_deadlines["walcl"],
            page_fresh_until=page_fresh_until,
        ),
        _liquidity_direct_metric(
            key="wrbwfrbl",
            label="准备金（共同日）",
            current=current["wrbwfrbl"],
            previous=previous["wrbwfrbl"],
            scale=Decimal("0.000001"),
            unit=" USD tn",
            decimals=6,
            component_fresh_until=component_deadlines["wrbwfrbl"],
            page_fresh_until=page_fresh_until,
        ),
        _liquidity_direct_metric(
            key="onrrp",
            label="ON RRP（共同日）",
            current=current["onrrp"],
            previous=previous["onrrp"],
            scale=Decimal("0.001"),
            unit=" USD bn",
            decimals=3,
            component_fresh_until=component_deadlines["onrrp"],
            page_fresh_until=page_fresh_until,
        ),
        _liquidity_direct_metric(
            key="tga",
            label="TGA（共同日）",
            current=current["tga"],
            previous=previous["tga"],
            scale=Decimal("0.001"),
            unit=" USD bn",
            decimals=3,
            component_fresh_until=component_deadlines["tga"],
            page_fresh_until=page_fresh_until,
        ),
    ]

    fed_funds = _liquidity_fed_funds_component(now=now)
    if isinstance(fed_funds, dict):
        return None, [fed_funds]
    policy_metrics, fed_funds_reference = fed_funds
    policy_by_key = {item["key"]: item for item in policy_metrics}
    metrics.extend(
        policy_by_key[key]
        for key in ("sofr", "iorb", "sofr-effr", "sofr-iorb")
    )

    chart_rows: list[dict[str, Any]] = []
    for period in displayed_dates:
        period_inputs = {
            key: values[period] for key, values in observations.items()
        }
        net_value = (
            period_inputs["walcl"].value
            - period_inputs["onrrp"].value
            - period_inputs["tga"].value
        ) * Decimal("0.000001")
        net_input_lineage = [
            _liquidity_input_lineage(
                period_inputs[key],
                component_fresh_until=component_deadlines[key],
            )
            for key in ("walcl", "onrrp", "tga")
        ]
        net_source_keys = sorted(
            {
                *(
                    source_key
                    for item in net_input_lineage
                    for source_key in item["source_keys"]
                ),
                "internal",
            }
        )
        chart_rows.append(
            {
                "date": period.isoformat(),
                "Net Liquidity": float(net_value),
                "Federal Reserve Assets": float(
                    period_inputs["walcl"].value * Decimal("0.000001")
                ),
                "ON RRP": float(
                    period_inputs["onrrp"].value * Decimal("0.000001")
                ),
                "TGA": float(
                    period_inputs["tga"].value * Decimal("0.000001")
                ),
                "_source_keys": net_source_keys,
                "_lineage": {
                    "Net Liquidity": {
                        "series_key": "net-liquidity",
                        "source_key": "internal",
                        "source_name": ensure_source("internal").name,
                        "source_keys": net_source_keys,
                        "license_scope": ensure_source("internal").license_scope,
                        "value_date": period_inputs[
                            "walcl"
                        ].value_date.isoformat(),
                        "as_of": min(
                            period_inputs[key].as_of
                            for key in ("walcl", "onrrp", "tga")
                        ).isoformat(),
                        "fetched_at": max(
                            period_inputs[key].fetched_at
                            for key in ("walcl", "onrrp", "tga")
                        ).isoformat(),
                        "fresh_until": page_fresh_until.isoformat(),
                        "batch_id": ",".join(
                            sorted(
                                {
                                    str(period_inputs[key].batch_id)
                                    for key in ("walcl", "onrrp", "tga")
                                }
                            )
                        ),
                        "input_batch_ids": sorted(
                            {
                                str(period_inputs[key].batch_id)
                                for key in ("walcl", "onrrp", "tga")
                            }
                        ),
                        "input_lineage": net_input_lineage,
                        "formula": "WALCL - ONRRP - TGA",
                        "quality_status": Observation.Quality.ESTIMATED,
                        "fallback_source": None,
                    },
                    "Federal Reserve Assets": _liquidity_input_lineage(
                        period_inputs["walcl"],
                        component_fresh_until=component_deadlines["walcl"],
                    ),
                    "ON RRP": _liquidity_input_lineage(
                        period_inputs["onrrp"],
                        component_fresh_until=component_deadlines["onrrp"],
                    ),
                    "TGA": _liquidity_input_lineage(
                        period_inputs["tga"],
                        component_fresh_until=component_deadlines["tga"],
                    ),
                },
            }
        )
    chart = _lineage_chart(
        key="net-liquidity-history",
        title="同日净流动性代理",
        description=(
            "WALCL、ON RRP 与 TGA 只在三者共同有效日计算，全部统一为万亿美元；"
            "该序列是 Atlas Macro 代理，不是官方 LPI。"
        ),
        rows=chart_rows,
        fields=(
            "Net Liquidity",
            "Federal Reserve Assets",
            "ON RRP",
            "TGA",
        ),
        tab="net",
        frequency="weekly",
    )
    if chart is None:
        return None, [
            _liquidity_component_failure(
                "chart", "common-date chart contract could not be built"
            )
        ]

    component_references = [
        _liquidity_run_state(identity, selected_runs[identity], status="valid")
        for identity in LIQUIDITY_DATASETS
    ]
    component_references.append(fed_funds_reference)
    sections = [
        {
            "title": "共同有效日与代理口径",
            "body": (
                f"当前代理值使用 {current_date.isoformat()} 的 WALCL、ON RRP 与 TGA；"
                f"前值使用 {previous_date.isoformat()}。不同频率组件绝不各取最新后混算。"
            ),
            "full_width": True,
        },
        {
            "title": "发布失败规则",
            "body": (
                "H.4.1、ON RRP、TGA 或 Fed Funds 任一组件失败、过期、回退、"
                "混批或许可失效时，页面保留上一版完整快照并显示失败组件。"
            ),
            "full_width": True,
        },
    ]
    extra_data = {
        "contract_version": LIQUIDITY_CONTRACT_VERSION,
        "common_effective_date": current_date.isoformat(),
        "component_snapshots": component_references,
        "model_disclaimer": (
            "Atlas Macro transparent proxy; not an official Federal Reserve LPI"
        ),
    }
    prepared = (metrics, [chart], sections, extra_data)
    if not _liquidity_page_contract_is_buildable(
        metrics, [chart], extra_data
    ):
        return None, [
            _liquidity_component_failure(
                "liquidity", "prepared page failed the v1 contract post-build check"
            )
        ]
    return prepared, []


def _latest_liquidity_snapshot() -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(
            key="liquidity",
            is_published=True,
            data__contract_version=LIQUIDITY_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )


def _liquidity_snapshot_effective_date(
    snapshot: DashboardSnapshot,
) -> date:
    raw_value = (snapshot.data or {}).get("common_effective_date")
    try:
        return date.fromisoformat(str(raw_value))
    except (TypeError, ValueError):
        return snapshot.as_of.date()


def _mark_liquidity_stale(
    components: list[dict[str, Any]], *, reason: str
) -> None:
    latest = (
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="liquidity",
            is_published=True,
            data__contract_version=LIQUIDITY_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )
    if latest is None:
        return
    summary = "；".join(
        (
            f"{item.get('component') or 'unknown'}"
            f"[{item.get('status') or 'invalid'}] "
            f"{item.get('reason') or 'unknown failure'}"
        )
        for item in components
    )
    data = dict(latest.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason": f"{reason} 失败组件：{summary}" if summary else reason,
        "components": components,
    }
    latest.data = data
    latest.quality_status = Observation.Quality.STALE
    latest.save(update_fields=["data", "quality_status", "updated_at"])


@transaction.atomic
def _coordinate_liquidity_dashboard(
    trigger_runs: Iterable[IngestionRun],
) -> tuple[list[DashboardSnapshot], set[str]]:
    source_keys = {
        "internal",
        *(source_key for source_key, _ in LIQUIDITY_DATASETS.values()),
        *(source_key for source_key, _ in FED_FUNDS_DATASETS.values()),
    }
    for source_key in sorted(source_keys):
        ensure_source(source_key)
    list(
        Source.objects.select_for_update()
        .filter(key__in=source_keys)
        .order_by("key")
        .values_list("pk", flat=True)
    )
    selected, states, triggered = _select_liquidity_runs(trigger_runs)
    if not triggered:
        return [], set()
    if selected is None:
        _mark_liquidity_stale(
            states,
            reason=(
                "最近一次 H.4.1、ON RRP 或 TGA 必需数据集未成功完成，"
                "或财政与回购组件不属于同一刷新周期；继续保留上一版。"
            ),
        )
        return [], {"liquidity"}
    prepared, failures = _liquidity_page_data(selected)
    if prepared is None:
        _mark_liquidity_stale(
            failures,
            reason=(
                "必需流动性组件未形成同日、同批、有效且许可可公开的完整组合；"
                "继续保留上一版。"
            ),
        )
        return [], {"liquidity"}
    _, _, _, extra_data = prepared
    candidate_date = date.fromisoformat(extra_data["common_effective_date"])
    previous_snapshot = _latest_liquidity_snapshot()
    if (
        previous_snapshot is not None
        and candidate_date
        < _liquidity_snapshot_effective_date(previous_snapshot)
    ):
        failures = [
            _liquidity_component_failure(
                "common-date",
                "candidate common effective date is older than the published v1 snapshot",
                status=Observation.Quality.STALE,
                snapshot=previous_snapshot,
            )
        ]
        _mark_liquidity_stale(
            failures,
            reason="候选共同有效日发生回退；拒绝发布并保留上一版。",
        )
        return [], {"liquidity"}

    expected_batches = {
        str(selected[identity].batch_id) for identity in LIQUIDITY_DATASETS
    }
    fed_reference = next(
        item
        for item in extra_data["component_snapshots"]
        if item.get("component") == "fed-funds"
    )
    expected_batches.update(fed_reference.get("component_batches", []))
    try:
        with transaction.atomic():
            dashboards = publish_official_dashboards(
                keys={"liquidity"}, prepared_liquidity_data=prepared
            )
            latest = _latest_liquidity_snapshot()
            postcondition_failed = (
                latest is None
                or (latest.data or {}).get("publication_batch_id")
                != str(latest.batch_id)
                or {
                    str(item.get("key") or "")
                    for item in (latest.data or {}).get("metrics", [])
                }
                != set(LIQUIDITY_REQUIRED_METRIC_KEYS)
                or {
                    str(item.get("key") or "")
                    for item in (latest.data or {}).get("charts", [])
                }
                != {"net-liquidity-history"}
                or set((latest.data or {}).get("component_batches", []))
                != expected_batches
                or (latest.data or {}).get("common_effective_date")
                != candidate_date.isoformat()
                or (latest.data or {}).get("refresh_failure")
                or not _liquidity_page_contract_is_buildable(
                    list((latest.data or {}).get("metrics", [])),
                    list((latest.data or {}).get("charts", [])),
                    dict(latest.data or {}),
                )
            )
            if not postcondition_failed:
                net_metric = next(
                    item
                    for item in (latest.data or {}).get("metrics", [])
                    if item.get("key") == "net-liquidity"
                )
                normalized = MetricSnapshot.objects.filter(
                    key="liquidity-net-liquidity", batch_id=latest.batch_id
                ).first()
                postcondition_failed = (
                    normalized is None
                    or normalized.value is None
                    or normalized.value.quantize(Decimal("0.000001"))
                    != Decimal(str(net_metric["value"])).quantize(
                        Decimal("0.000001")
                    )
                    or normalized.value_date
                    != _parse_payload_datetime(net_metric.get("value_date"))
                    or normalized.metadata.get("formula")
                    != "WALCL - ONRRP - TGA"
                    or not normalized.metadata.get("input_lineage")
                )
            if postcondition_failed:
                raise ValueError("liquidity publication postcondition failed")
    except ValueError:
        failures = [
            _liquidity_component_failure(
                "liquidity",
                "publication postcondition failed",
                snapshot=previous_snapshot,
            )
        ]
        _mark_liquidity_stale(
            failures,
            reason=(
                "流动性总览发布后置条件未满足；新写入已回滚，继续保留"
                "上一版完整快照。"
            ),
        )
        return [], {"liquidity"}
    return dashboards, set()


def _latest_auction_attempt() -> IngestionRun | None:
    source_key, dataset = AUCTION_DATASET
    return (
        IngestionRun.objects.filter(source__key=source_key, dataset=dataset)
        .order_by("-started_at", "-id")
        .first()
    )


def _auction_run_state(
    run: IngestionRun | None,
    *,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    source_key, dataset = AUCTION_DATASET
    return {
        "component": "auctions",
        "kind": "ingestion_run",
        "source": source_key,
        "dataset": dataset,
        "status": status or (run.status if run else "missing"),
        "reason": (
            reason
            or (run.error if run else "required auction dataset run missing")
        )[:320],
        "ingestion_run_id": run.pk if run else None,
        "batch_id": str(run.batch_id) if run else None,
        "row_count": run.row_count if run else 0,
        "refresh_cycle_id": (
            str((run.metadata or {}).get("refresh_cycle_id") or "")
            if run
            else ""
        ),
        "completed_at": (
            run.completed_at.isoformat() if run and run.completed_at else None
        ),
    }


def _auction_run_contract_error(
    run: IngestionRun,
    *,
    today_et: date,
    now: datetime,
) -> str | None:
    metadata = dict(run.metadata or {})
    if run.status != IngestionRun.Status.SUCCESS:
        return "latest exact auction run did not complete successfully"
    if not metadata.get("coverage_complete"):
        return "bounded auction and issue slices are not both complete"
    if not metadata.get("refresh_cycle_id"):
        return "auction run has no refresh cycle identity"
    if metadata.get("as_of_date_et") != today_et.isoformat():
        return "auction run is not for the current America/New_York date"
    fetched_at = _parse_payload_datetime(metadata.get("fetched_at"))
    if fetched_at is None:
        return "auction run has no valid fetched_at"
    if fetched_at > now + timedelta(minutes=5):
        return "auction run fetched_at is in the future"
    if fetched_at.astimezone(ZoneInfo("America/New_York")).date() != today_et:
        return "auction run was not fetched during the current ET date"
    expected = {
        "auction_window": {
            "lower": (today_et - timedelta(days=90)).isoformat(),
            "upper_exclusive": (today_et + timedelta(days=14)).isoformat(),
            "date_field": "auction_date",
        },
        "issue_window": {
            "lower": today_et.isoformat(),
            "upper_exclusive": (today_et + timedelta(days=14)).isoformat(),
            "date_field": "issue_date",
        },
    }
    slices = {
        str(item.get("name") or ""): item
        for item in metadata.get("slices", [])
        if isinstance(item, dict)
    }
    if set(slices) != set(expected):
        return "auction run does not contain the two required bounded slices"
    for name, bounds in expected.items():
        state = slices[name]
        if not state.get("coverage_complete"):
            return f"{name} is not complete"
        if any(state.get(key) != value for key, value in bounds.items()):
            return f"{name} bounds do not match the current v1 contract"
        if state.get("rejected_count", 0) != 0:
            return f"{name} contains rejected upstream rows"
        returned = state.get("returned_count")
        normalized = state.get("normalized_count", returned)
        total = state.get("total_count")
        count = state.get("count")
        if not all(
            isinstance(value, int)
            for value in (returned, normalized, total, count)
        ):
            return f"{name} lacks complete integer row counts"
        if not returned == normalized == total == count:
            return f"{name} row counts do not reconcile"
        pages = state.get("total_pages")
        if total == 0 and pages not in {0, 1}:
            return f"{name} has invalid empty-page metadata"
        if total > 0 and pages != 1:
            return f"{name} spans more than one fetched page"
    return None


def _select_auction_run(
    trigger_runs: Iterable[IngestionRun],
    *,
    today_et: date,
    now: datetime,
) -> tuple[IngestionRun | None, list[dict[str, Any]], bool]:
    source_key, dataset = AUCTION_DATASET
    relevant = [
        run
        for run in trigger_runs
        if run.source.key == source_key and run.dataset == dataset
    ]
    if not relevant:
        return None, [], False
    latest = _latest_auction_attempt()
    # A delayed/replayed run is a complete no-op. It must not stale or replace
    # the state established by the newer exact source+dataset attempt.
    if latest is None or len(relevant) != 1 or relevant[0].pk != latest.pk:
        return None, [], False
    error = _auction_run_contract_error(latest, today_et=today_et, now=now)
    if error:
        return (
            None,
            [_auction_run_state(latest, status="invalid", reason=error)],
            True,
        )
    return latest, [_auction_run_state(latest, status="valid")], True


def _auction_event_datetime(value: date) -> str:
    return datetime.combine(
        value,
        time.min,
        tzinfo=ZoneInfo("America/New_York"),
    ).isoformat()


def _auction_input_lineage(
    item: TreasuryAuction,
    *,
    event_date: date,
    value: Decimal | None = None,
    field: str | None = None,
    unit: str = "",
    fresh_until: datetime,
) -> dict[str, Any]:
    return {
        "record_identity": f"{item.cusip}:{item.auction_date.isoformat()}",
        "cusip": item.cusip,
        "security_term": item.security_term,
        "field": field,
        "value": float(value) if value is not None else None,
        "raw_value": str(value) if value is not None else None,
        "unit": unit,
        "source_key": item.source.key,
        "source_name": item.source.name,
        "source_keys": [item.source.key],
        "license_scope": item.source.license_scope,
        "event_date": event_date.isoformat(),
        "value_date": _auction_event_datetime(event_date),
        "as_of": item.fetched_at.isoformat(),
        "fetched_at": item.fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(item.batch_id),
        "quality_status": item.quality_status,
        "fallback_source": None,
    }


def _auction_row_payload(
    item: TreasuryAuction,
    *,
    event_date: date,
    display_value: str,
    status: str,
    description: str,
    fresh_until: datetime,
    lineage_field: str = "offering_amount",
    lineage_value: Decimal | None = None,
    lineage_unit: str = "USD gross par",
    additional_lineage_fields: tuple[
        tuple[str, Decimal | None, str], ...
    ] = (),
) -> dict[str, Any]:
    primary_value = (
        item.offering_amount
        if lineage_field == "offering_amount" and lineage_value is None
        else lineage_value
    )
    lineage = _auction_input_lineage(
        item,
        event_date=event_date,
        value=primary_value,
        field=lineage_field,
        unit=lineage_unit,
        fresh_until=fresh_until,
    )
    additional_lineage = [
        _auction_input_lineage(
            item,
            event_date=event_date,
            value=value,
            field=field,
            unit=unit,
            fresh_until=fresh_until,
        )
        for field, value, unit in additional_lineage_fields
        if value is not None
    ]
    return {
        "key": f"{item.cusip}-{item.auction_date.isoformat()}",
        "label": f"{event_date.isoformat()} · {item.security_term}",
        "display_value": display_value,
        "status": status,
        "description": description,
        "source": item.source.name,
        "source_key": item.source.key,
        "source_keys": [item.source.key],
        "value_date": lineage["value_date"],
        "as_of": lineage["as_of"],
        "fetched_at": lineage["fetched_at"],
        "fresh_until": lineage["fresh_until"],
        "batch_id": lineage["batch_id"],
        "quality_status": lineage["quality_status"],
        "license_scope": lineage["license_scope"],
        "fallback_source": None,
        "display_lineage": lineage,
        "additional_lineage": additional_lineage,
        "input_lineage": [lineage, *additional_lineage],
    }


def _auction_derived_metric(
    *,
    key: str,
    label: str,
    value: Decimal | int | None,
    display_value: str,
    unit: str,
    value_date: date,
    fetched_at: datetime,
    fresh_until: datetime,
    batch_id: uuid.UUID,
    formula: str,
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": float(value) if value is not None else None,
        "display_value": display_value,
        "change": None,
        "unit": unit,
        "quality_status": Observation.Quality.ESTIMATED,
        "source": "Atlas Macro 计算：U.S. Treasury FiscalData",
        "source_key": "internal",
        "source_keys": ["internal", "treasury-fiscal-data"],
        "fallback_source": None,
        "value_date": _auction_event_datetime(value_date),
        "as_of": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(batch_id),
        "metadata": {
            "formula": formula,
            "input_series": ["treasury-securities-auctions"],
            "input_batch_ids": [str(batch_id)],
            "input_value_dates": sorted(
                {
                    str(item.get("value_date") or "")
                    for item in inputs
                    if item.get("value_date")
                }
            ),
            "input_lineage": inputs,
            "calculation_owner": "Atlas Macro",
        },
    }


def _auction_direct_metric(
    *,
    key: str,
    label: str,
    value: Decimal | None,
    display_value: str,
    item: TreasuryAuction | None,
    value_date: date,
    fetched_at: datetime,
    fresh_until: datetime,
    batch_id: uuid.UUID,
) -> dict[str, Any]:
    inputs = (
        [
            _auction_input_lineage(
                item,
                event_date=value_date,
                value=value,
                field="bid_to_cover_ratio",
                unit="ratio",
                fresh_until=fresh_until,
            )
        ]
        if item is not None
        else []
    )
    return {
        "key": key,
        "label": label,
        "value": float(value) if value is not None else None,
        "display_value": display_value,
        "change": None,
        "unit": "x",
        "quality_status": Observation.Quality.FRESH,
        "source": "U.S. Department of the Treasury, FiscalData",
        "source_key": "treasury-fiscal-data",
        "source_keys": ["treasury-fiscal-data"],
        "fallback_source": None,
        "value_date": _auction_event_datetime(value_date),
        "as_of": fetched_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(batch_id),
        "metadata": {
            "source_field": "bid_to_cover_ratio",
            "input_series": ["treasury-securities-auctions"],
            "input_batch_ids": [str(batch_id)],
            "input_value_dates": [
                item.auction_date.isoformat() if item is not None else value_date.isoformat()
            ],
            "input_lineage": inputs,
        },
    }


def _auction_page_data(
    run: IngestionRun,
    *,
    today_et: date,
    now: datetime,
) -> tuple[
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None,
    list[dict[str, Any]],
]:
    public_keys, derived_keys = current_display_source_key_sets(
        {"internal", "treasury-fiscal-data"}
    )
    if "treasury-fiscal-data" not in public_keys:
        return None, [
            _auction_run_state(
                run,
                status="unlicensed",
                reason="Treasury source is not currently licensed for public display",
            )
        ]
    if not {"internal", "treasury-fiscal-data"} <= derived_keys:
        return None, [
            _auction_run_state(
                run,
                status="unlicensed",
                reason="required direct or derived display licence is not current",
            )
        ]
    fetched_at = _parse_payload_datetime((run.metadata or {}).get("fetched_at"))
    if fetched_at is None or fetched_at > now + timedelta(minutes=5):
        return None, [
            _auction_run_state(
                run, status="invalid", reason="invalid or future fetched_at"
            )
        ]
    fresh_until = datetime.combine(
        today_et + timedelta(days=2),
        time(hour=10),
        tzinfo=ZoneInfo("America/New_York"),
    ).astimezone(UTC)
    auctions = list(
        TreasuryAuction.objects.filter(batch_id=run.batch_id)
        .filter(public_display_license_q())
        .select_related("source")
        .distinct()
        .order_by("auction_date", "cusip")
    )
    if len(auctions) != run.row_count:
        return None, [
            _auction_run_state(
                run,
                status="invalid",
                reason="exact-batch auction rows do not match run.row_count",
            )
        ]
    identities: set[tuple[str, date]] = set()
    for item in auctions:
        identity = (item.cusip, item.auction_date)
        if identity in identities:
            return None, [
                _auction_run_state(
                    run, status="invalid", reason="duplicate auction identity"
                )
            ]
        identities.add(identity)
        dates = [
            item.announcement_date,
            item.auction_date,
            item.issue_date,
            item.maturity_date,
        ]
        ordered_dates = [value for value in dates if value is not None]
        if ordered_dates != sorted(ordered_dates):
            return None, [
                _auction_run_state(
                    run,
                    status="invalid",
                    reason=f"invalid event date order for {item.cusip}",
                )
            ]
        if (
            item.source.key != "treasury-fiscal-data"
            or item.batch_id != run.batch_id
            or item.quality_status != Observation.Quality.FRESH
            or item.fetched_at != fetched_at
            or item.fetched_at > now + timedelta(minutes=5)
        ):
            return None, [
                _auction_run_state(
                    run,
                    status="invalid",
                    reason="exact-batch row has wrong source, quality, batch or fetched_at",
                )
            ]

    auction_end = today_et + timedelta(days=14)
    issue_end = today_et + timedelta(days=14)
    recent_start = today_et - timedelta(days=90)
    formal = [
        item
        for item in auctions
        if today_et <= item.auction_date < auction_end
        and item.bid_to_cover_ratio is None
        and item.announcement_date is not None
        and item.announcement_date <= today_et
    ]
    issues = [
        item
        for item in auctions
        if item.issue_date is not None and today_et <= item.issue_date < issue_end
    ]
    results = [
        item
        for item in auctions
        if recent_start <= item.auction_date <= today_et
        and item.bid_to_cover_ratio is not None
    ]
    if any(
        item.offering_amount is None or item.offering_amount <= 0
        for item in [*formal, *issues, *results]
    ):
        return None, [
            _auction_run_state(
                run,
                status="invalid",
                reason=(
                    "a published formal, issue or result row has a missing or "
                    "non-positive offering amount"
                ),
            )
        ]

    seven_day_end = today_et + timedelta(days=7)
    formal_7d = [item for item in formal if item.auction_date < seven_day_end]
    issues_7d = [
        item for item in issues if item.issue_date and item.issue_date < seven_day_end
    ]
    gross_formal_7d = sum(
        (item.offering_amount or Decimal("0") for item in formal_7d),
        Decimal("0"),
    )
    gross_issue_7d = sum(
        (item.offering_amount or Decimal("0") for item in issues_7d),
        Decimal("0"),
    )
    gross_issue_14d = sum(
        (item.offering_amount or Decimal("0") for item in issues),
        Decimal("0"),
    )

    def offering_lineage(item: TreasuryAuction, event_date: date) -> dict[str, Any]:
        return _auction_input_lineage(
            item,
            event_date=event_date,
            value=item.offering_amount,
            field="offering_amount",
            unit="USD gross par",
            fresh_until=fresh_until,
        )

    next_item = formal[0] if formal else None
    next_inputs = (
        [offering_lineage(next_item, next_item.auction_date)] if next_item else []
    )
    days_to_next = (next_item.auction_date - today_et).days if next_item else None
    next_display = (
        f"{days_to_next} 天 · {next_item.auction_date.isoformat()} · "
        f"{next_item.security_term}"
        if next_item is not None
        else "未来 14 天无已公告待拍卖项目"
    )
    latest_result = max(results, key=lambda item: (item.auction_date, item.cusip)) if results else None
    latest_btc = latest_result.bid_to_cover_ratio if latest_result else None
    latest_btc_date = latest_result.auction_date if latest_result else today_et
    metrics = [
        _auction_derived_metric(
            key="days-to-next-auction",
            label="距下次正式拍卖",
            value=days_to_next,
            display_value=next_display,
            unit="days",
            value_date=(next_item.auction_date if next_item else today_et),
            fetched_at=fetched_at,
            fresh_until=fresh_until,
            batch_id=run.batch_id,
            formula="next formal auction_date - ET as_of_date",
            inputs=next_inputs,
        ),
        _auction_derived_metric(
            key="formal-auction-gross-7d",
            label="未来 7 天正式拍卖公告面值",
            value=gross_formal_7d / Decimal("1000000000"),
            display_value=f"${gross_formal_7d / Decimal('1000000000'):,.1f}B",
            unit="USD bn gross par",
            value_date=today_et,
            fetched_at=fetched_at,
            fresh_until=fresh_until,
            batch_id=run.batch_id,
            formula="sum(offering_amount) for formal auction_date in [ET today, ET today+7d)",
            inputs=[offering_lineage(item, item.auction_date) for item in formal_7d],
        ),
        _auction_derived_metric(
            key="issue-gross-7d",
            label="未来 7 天发行/结算公告面值",
            value=gross_issue_7d / Decimal("1000000000"),
            display_value=f"${gross_issue_7d / Decimal('1000000000'):,.1f}B",
            unit="USD bn gross par",
            value_date=today_et,
            fetched_at=fetched_at,
            fresh_until=fresh_until,
            batch_id=run.batch_id,
            formula="sum(offering_amount) for issue_date in [ET today, ET today+7d)",
            inputs=[
                offering_lineage(item, item.issue_date)
                for item in issues_7d
                if item.issue_date is not None
            ],
        ),
        _auction_derived_metric(
            key="issue-gross-14d",
            label="未来 14 天发行/结算公告面值",
            value=gross_issue_14d / Decimal("1000000000"),
            display_value=f"${gross_issue_14d / Decimal('1000000000'):,.1f}B",
            unit="USD bn gross par",
            value_date=today_et,
            fetched_at=fetched_at,
            fresh_until=fresh_until,
            batch_id=run.batch_id,
            formula="sum(offering_amount) for issue_date in [ET today, ET today+14d)",
            inputs=[
                offering_lineage(item, item.issue_date)
                for item in issues
                if item.issue_date is not None
            ],
        ),
        _auction_direct_metric(
            key="latest-bid-to-cover",
            label="最近拍卖 Bid-to-Cover",
            value=latest_btc,
            display_value=(f"{latest_btc:.2f}x" if latest_btc is not None else "近 90 天暂无已完成结果"),
            item=latest_result,
            value_date=latest_btc_date,
            fetched_at=fetched_at,
            fresh_until=fresh_until,
            batch_id=run.batch_id,
        ),
    ]

    formal_rows = [
        _auction_row_payload(
            item,
            event_date=item.auction_date,
            display_value=f"${(item.offering_amount or Decimal('0')) / Decimal('1000000000'):,.1f}B",
            status="待拍卖",
            description=(
                f"CUSIP {item.cusip}；公告面值，不代表财政部实际净融资或 TGA 流入"
            ),
            fresh_until=fresh_until,
        )
        for item in formal
    ]
    issue_rows = [
        _auction_row_payload(
            item,
            event_date=item.issue_date,
            display_value=f"${(item.offering_amount or Decimal('0')) / Decimal('1000000000'):,.1f}B",
            status=(
                "已完成拍卖，待发行/结算"
                if item.bid_to_cover_ratio is not None
                else "计划发行/结算"
            ),
            description=(
                f"拍卖日 {item.auction_date.isoformat()}；gross announced face amount，"
                "不是实际现金流、TGA 变动或净流动性预测"
            ),
            fresh_until=fresh_until,
        )
        for item in issues
        if item.issue_date is not None
    ]
    result_rows = [
        _auction_row_payload(
            item,
            event_date=item.auction_date,
            display_value=f"{item.bid_to_cover_ratio:.2f}x",
            status=(f"高收益率 {item.high_yield:.3f}%" if item.high_yield is not None else "结果已公布"),
            description=(
                f"CUSIP {item.cusip}；公告面值 "
                f"${item.offering_amount / Decimal('1000000000'):,.1f}B"
                if item.offering_amount is not None
                else f"CUSIP {item.cusip}；公告面值未提供"
            ),
            fresh_until=fresh_until,
            lineage_field="bid_to_cover_ratio",
            lineage_value=item.bid_to_cover_ratio,
            lineage_unit="ratio",
            additional_lineage_fields=(
                ("offering_amount", item.offering_amount, "USD gross par"),
                ("high_yield", item.high_yield, "%"),
            ),
        )
        for item in sorted(results, key=lambda value: (value.auction_date, value.cusip), reverse=True)
    ]

    chart_rows = []
    for offset in range(14):
        event_date = today_et + timedelta(days=offset)
        daily = [item for item in issues if item.issue_date == event_date]
        daily_total = sum(
            (item.offering_amount or Decimal("0") for item in daily), Decimal("0")
        )
        daily_inputs = [offering_lineage(item, event_date) for item in daily]
        chart_rows.append(
            {
                "date": event_date.isoformat(),
                "Gross announced issue amount": float(
                    daily_total / Decimal("1000000000")
                ),
                "_source_keys": ["internal", "treasury-fiscal-data"],
                "_lineage": {
                    "Gross announced issue amount": {
                        "source_key": "internal",
                        "source_name": ensure_source("internal").name,
                        "source_keys": ["internal", "treasury-fiscal-data"],
                        "license_scope": ensure_source("internal").license_scope,
                        "value_date": _auction_event_datetime(event_date),
                        "as_of": fetched_at.isoformat(),
                        "fetched_at": fetched_at.isoformat(),
                        "fresh_until": fresh_until.isoformat(),
                        "batch_id": str(run.batch_id),
                        "input_batch_ids": [str(run.batch_id)],
                        "input_lineage": daily_inputs,
                        "formula": "sum(offering_amount) grouped by issue_date",
                        "quality_status": Observation.Quality.ESTIMATED,
                        "fallback_source": None,
                    }
                },
            }
        )
    chart = _lineage_chart(
        key="gross-issue-calendar",
        title="未来 14 天发行/结算公告面值",
        description=(
            "按 issue_date 汇总 Treasury offering_amount，单位十亿美元；"
            "这是 gross announced face amount，不是实际现金流、TGA 变动、"
            "净融资或净流动性预测。"
        ),
        rows=chart_rows,
        fields=("Gross announced issue amount",),
        tab="issue-calendar",
        frequency="daily",
    )
    if chart is None:
        return None, [
            _auction_run_state(
                run, status="invalid", reason="gross issue chart contract failed"
            )
        ]
    sections = [
        {
            "key": "formal-auctions-14d",
            "title": "未来 14 天正式拍卖日历",
            "description": "仅含尚未公布结果的正式拍卖；当日结果公布后移入结果表，不重复展示。",
            "rows": formal_rows,
            "status": Observation.Quality.FRESH,
            "source_key": "treasury-fiscal-data",
            "source_keys": ["treasury-fiscal-data"],
            "as_of": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "fresh_until": fresh_until.isoformat(),
            "batch_id": str(run.batch_id),
            "license_scope": ensure_source("treasury-fiscal-data").license_scope,
            "fallback_source": None,
            "full_width": True,
        },
        {
            "key": "issue-settlement-14d",
            "title": "未来 14 天发行/结算日历",
            "description": (
                "保留拍卖已经完成但 issue_date 尚未来临的证券。金额为公告总面值，"
                "不能据此推导 TGA 方向、实际净融资或流动性变化。"
            ),
            "rows": issue_rows,
            "status": Observation.Quality.FRESH,
            "source_key": "treasury-fiscal-data",
            "source_keys": ["treasury-fiscal-data"],
            "as_of": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "fresh_until": fresh_until.isoformat(),
            "batch_id": str(run.batch_id),
            "license_scope": ensure_source("treasury-fiscal-data").license_scope,
            "fallback_source": None,
            "full_width": True,
        },
        {
            "key": "recent-results-90d",
            "title": "近 90 天拍卖结果",
            "description": "Bid-to-Cover 与拍卖高收益率来自同一完整官方批次；不包含真实 WI Tail。",
            "rows": result_rows,
            "status": Observation.Quality.FRESH,
            "source_key": "treasury-fiscal-data",
            "source_keys": ["treasury-fiscal-data"],
            "as_of": fetched_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "fresh_until": fresh_until.isoformat(),
            "batch_id": str(run.batch_id),
            "license_scope": ensure_source("treasury-fiscal-data").license_scope,
            "fallback_source": None,
            "full_width": True,
        },
    ]
    extra_data = {
        "contract_version": AUCTION_CONTRACT_VERSION,
        "as_of_date_et": today_et.isoformat(),
        "timezone": "America/New_York",
        "window_semantics": "half-open [start, end)",
        "coverage_complete": True,
        "component_snapshots": [
            _auction_run_state(run, status="valid")
        ],
        "model_disclaimer": (
            "offering_amount is gross announced face amount; it is not actual cash, "
            "net financing, a TGA forecast or a net-liquidity forecast"
        ),
    }
    return (metrics, [chart], sections, extra_data), []


def _latest_auction_snapshot() -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(
            key="auctions",
            is_published=True,
            data__contract_version=AUCTION_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )


def _mark_auction_stale(
    components: list[dict[str, Any]], *, reason: str
) -> None:
    latest = (
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="auctions",
            is_published=True,
            data__contract_version=AUCTION_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )
    if latest is None:
        return
    component_summary = "；".join(
        f"{item.get('component', 'auctions')}[{item.get('status', 'invalid')}] "
        f"{item.get('reason', 'unknown failure')}"
        for item in components
    )
    data = dict(latest.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason": f"{reason} 失败组件：{component_summary}",
        "components": components,
    }
    latest.data = data
    latest.quality_status = Observation.Quality.STALE
    latest.save(update_fields=["data", "quality_status", "updated_at"])


def _auction_snapshot_contract_is_valid(
    snapshot: DashboardSnapshot,
    *,
    run: IngestionRun,
    today_et: date,
) -> bool:
    data = dict(snapshot.data or {})
    metrics = list(data.get("metrics", []))
    charts = list(data.get("charts", []))
    sections = list(data.get("sections", []))
    if (
        data.get("contract_version") != AUCTION_CONTRACT_VERSION
        or data.get("as_of_date_et") != today_et.isoformat()
        or not data.get("coverage_complete")
        or data.get("refresh_failure")
        or data.get("publication_batch_id") != str(snapshot.batch_id)
        or set(data.get("component_batches", [])) != {str(run.batch_id)}
        or {str(item.get("key") or "") for item in metrics}
        != set(AUCTION_REQUIRED_METRIC_KEYS)
        or {str(item.get("key") or "") for item in charts}
        != {"gross-issue-calendar"}
        or {str(item.get("key") or "") for item in sections}
        != {"formal-auctions-14d", "issue-settlement-14d", "recent-results-90d"}
    ):
        return False
    for item in metrics:
        if (
            not item.get("source_key")
            or not item.get("license_scope")
            or "fallback_source" not in item
            or item.get("batch_id") != str(run.batch_id)
            or not item.get("fetched_at")
            or not item.get("value_date")
        ):
            return False
        if item.get("source_key") == "internal" and not (
            item.get("metadata") or {}
        ).get("input_lineage") and item.get("value") not in {0, 0.0, None}:
            return False
    for section in sections:
        if section.get("batch_id") != str(run.batch_id):
            return False
        for row in section.get("rows", []):
            if (
                row.get("batch_id") != str(run.batch_id)
                or not row.get("license_scope")
                or "fallback_source" not in row
                or not row.get("input_lineage")
            ):
                return False
    chart = charts[0]
    chart_rows = list(chart.get("data", []))
    expected_dates = {
        (today_et + timedelta(days=offset)).isoformat()
        for offset in range(14)
    }
    if {str(row.get("date") or "") for row in chart_rows} != expected_dates:
        return False
    field = "Gross announced issue amount"
    for row in chart_rows:
        lineage = (row.get("_lineage") or {}).get(field)
        if (
            not isinstance(lineage, dict)
            or lineage.get("batch_id") != str(run.batch_id)
            or lineage.get("fallback_source") is not None
            or not lineage.get("license_scope")
            or field not in row
        ):
            return False
    by_date = {
        date.fromisoformat(str(row["date"])): Decimal(str(row[field]))
        for row in chart_rows
    }
    metric_by_key = {item["key"]: item for item in metrics}
    if (
        sum(
            (
                by_date[today_et + timedelta(days=offset)]
                for offset in range(7)
            ),
            Decimal("0"),
        )
        != Decimal(str(metric_by_key["issue-gross-7d"]["value"]))
        or sum(by_date.values(), Decimal("0"))
        != Decimal(str(metric_by_key["issue-gross-14d"]["value"]))
    ):
        return False
    section_by_key = {item["key"]: item for item in sections}
    formal_row_keys = {
        row.get("key")
        for row in section_by_key["formal-auctions-14d"].get("rows", [])
    }
    result_row_keys = {
        row.get("key")
        for row in section_by_key["recent-results-90d"].get("rows", [])
    }
    if formal_row_keys & result_row_keys:
        return False
    normalized = {
        item.key: item
        for item in MetricSnapshot.objects.filter(
            batch_id=snapshot.batch_id,
            key__startswith="auctions-",
        )
    }
    for metric in metrics:
        if metric.get("value") is None:
            continue
        item = normalized.get(f"auctions-{metric['key']}")
        if (
            item is None
            or item.value != Decimal(str(metric["value"]))
            or item.source.key != metric["source_key"]
            or item.fallback_source_id is not None
            or item.value_date
            != _parse_payload_datetime(metric.get("value_date"))
            or item.fetched_at
            != _parse_payload_datetime(metric.get("fetched_at"))
            or item.metadata.get("component_batch_id")
            != metric.get("batch_id")
            or item.metadata.get("input_lineage")
            != (metric.get("metadata") or {}).get("input_lineage")
            or not item.license_scope
        ):
            return False
    return True


@transaction.atomic
def _coordinate_auction_dashboard(
    trigger_runs: Iterable[IngestionRun],
    *,
    as_of_date: date | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    for source_key in ("internal", "treasury-fiscal-data"):
        ensure_source(source_key)
    list(
        Source.objects.select_for_update()
        .filter(key__in=("internal", "treasury-fiscal-data"))
        .order_by("key")
        .values_list("pk", flat=True)
    )
    now = timezone.now()
    today_et = as_of_date or now.astimezone(
        ZoneInfo("America/New_York")
    ).date()
    selected, states, triggered = _select_auction_run(
        trigger_runs, today_et=today_et, now=now
    )
    if not triggered:
        return [], set()
    if selected is None:
        _mark_auction_stale(
            states,
            reason=(
                "最新拍卖刷新未形成当前 ET 日、完整且可公开的双窗口批次；"
                "继续保留上一版完整快照。"
            ),
        )
        return [], {"auctions"}
    prepared, failures = _auction_page_data(
        selected, today_et=today_et, now=now
    )
    if prepared is None:
        _mark_auction_stale(
            failures,
            reason=(
                "拍卖日历未通过精确批次、许可、日期或金额后置检查；"
                "继续保留上一版。"
            ),
        )
        return [], {"auctions"}
    metrics, charts, sections, extra_data = prepared
    previous = _latest_auction_snapshot()
    if previous is not None:
        try:
            previous_date = date.fromisoformat(
                str((previous.data or {}).get("as_of_date_et") or "")
            )
        except ValueError:
            previous_date = previous.as_of.astimezone(
                ZoneInfo("America/New_York")
            ).date()
        if today_et < previous_date:
            _mark_auction_stale(
                [
                    _auction_run_state(
                        selected,
                        status="stale",
                        reason="candidate ET date is older than published v1 snapshot",
                    )
                ],
                reason="候选拍卖日历日期发生回退；拒绝发布并保留上一版。",
            )
            return [], {"auctions"}
    try:
        with transaction.atomic():
            publication_batch = uuid.uuid4()
            snapshot = _publish_dashboard(
                key="auctions",
                title="国债拍卖",
                summary=(
                    "正式拍卖、发行/结算和近 90 天结果来自 Treasury FiscalData "
                    "同一完整批次。offering_amount 只表示公告总面值，不是实际现金、"
                    "TGA 变动、净融资或净流动性预测；官方源不含真实 WI Tail。"
                ),
                metrics=metrics,
                charts=charts,
                sections=sections,
                extra_data=extra_data,
                required_metric_keys=AUCTION_REQUIRED_METRIC_KEYS,
                batch_id=publication_batch,
            )
            latest = _latest_auction_snapshot()
            if latest is None or not _auction_snapshot_contract_is_valid(
                latest, run=selected, today_et=today_et
            ):
                raise ValueError("auction publication postcondition failed")
    except (ValueError, StopIteration):
        _mark_auction_stale(
            [
                _auction_run_state(
                    selected,
                    status="invalid",
                    reason="publication postcondition failed",
                )
            ],
            reason=(
                "拍卖 v1 发布后置条件未满足；新写入已回滚，继续保留"
                "上一版完整快照。"
            ),
        )
        return [], {"auctions"}
    return ([snapshot] if snapshot is not None else []), set()


def _rrp_tga_run_identity(run: IngestionRun) -> str | None:
    for identity, (source_key, dataset) in RRP_TGA_DATASETS.items():
        if run.source.key == source_key and run.dataset == dataset:
            return identity
    return None


def _latest_rrp_tga_attempt(identity: str) -> IngestionRun | None:
    source_key, dataset = RRP_TGA_DATASETS[identity]
    return (
        IngestionRun.objects.filter(source__key=source_key, dataset=dataset)
        .order_by("-started_at", "-id")
        .first()
    )


def _rrp_tga_run_state(
    identity: str,
    run: IngestionRun | None,
    *,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    source_key, dataset = RRP_TGA_DATASETS[identity]
    return {
        "component": identity,
        "kind": "ingestion_run",
        "source": source_key,
        "dataset": dataset,
        "status": status or (run.status if run else "missing"),
        "reason": (
            reason
            or (run.error if run else "required dataset run missing")
        )[:320],
        "ingestion_run_id": run.pk if run else None,
        "batch_id": str(run.batch_id) if run else None,
        "row_count": run.row_count if run else 0,
        "refresh_cycle_id": (
            str((run.metadata or {}).get("refresh_cycle_id") or "")
            if run
            else ""
        ),
        "completed_at": (
            run.completed_at.isoformat() if run and run.completed_at else None
        ),
    }


def _select_rrp_tga_runs(
    trigger_runs: Iterable[IngestionRun],
    *,
    today_et: date,
    now: datetime,
) -> tuple[dict[str, IngestionRun] | None, list[dict[str, Any]], bool]:
    relevant: dict[str, list[IngestionRun]] = {
        identity: [] for identity in RRP_TGA_DATASETS
    }
    for run in trigger_runs:
        identity = _rrp_tga_run_identity(run)
        if identity:
            relevant[identity].append(run)
    if not any(relevant.values()):
        return None, [], False
    for identity, identity_runs in relevant.items():
        if not identity_runs:
            continue
        latest = _latest_rrp_tga_attempt(identity)
        if latest is None or len(identity_runs) != 1 or identity_runs[0].pk != latest.pk:
            return None, [], False
    selected = {
        identity: _latest_rrp_tga_attempt(identity)
        for identity in RRP_TGA_DATASETS
    }
    states = [
        _rrp_tga_run_state(identity, selected[identity])
        for identity in RRP_TGA_DATASETS
    ]
    if any(run is None for run in selected.values()):
        return None, states, True
    complete = {
        identity: run for identity, run in selected.items() if run is not None
    }
    for identity, run in complete.items():
        if run.status != IngestionRun.Status.SUCCESS:
            return None, states, True
        if identity != "auctions" and run.row_count <= 0:
            return None, states, True
        fetched_at = _parse_payload_datetime((run.metadata or {}).get("fetched_at"))
        if (
            fetched_at is None
            or fetched_at > now + timedelta(minutes=5)
            or fetched_at.astimezone(ZoneInfo("America/New_York")).date()
            != today_et
        ):
            return (
                None,
                [
                    _rrp_tga_run_state(
                        key,
                        item,
                        status="invalid",
                        reason=(
                            "required run lacks a current non-future ET fetched_at"
                            if key == identity
                            else None
                        ),
                    )
                    for key, item in complete.items()
                ],
                True,
            )
    auction_error = _auction_run_contract_error(
        complete["auctions"], today_et=today_et, now=now
    )
    if auction_error:
        return (
            None,
            [
                _rrp_tga_run_state(
                    identity,
                    run,
                    status="invalid" if identity == "auctions" else None,
                    reason=auction_error if identity == "auctions" else None,
                )
                for identity, run in complete.items()
            ],
            True,
        )
    cycles = {
        str((run.metadata or {}).get("refresh_cycle_id") or "")
        for run in complete.values()
    }
    if len(cycles) != 1 or not next(iter(cycles), ""):
        return (
            None,
            [
                _rrp_tga_run_state(
                    identity,
                    run,
                    status="invalid-cycle",
                    reason="ON RRP, TGA and auctions are not from one refresh cycle",
                )
                for identity, run in complete.items()
            ],
            True,
        )
    return complete, states, True


def _rrp_tga_observation_metric(
    *,
    key: str,
    label: str,
    observation: Observation,
    scale: Decimal,
    decimals: int,
    unit: str,
    fresh_until: datetime,
) -> dict[str, Any]:
    value = observation.value * scale
    lineage = _liquidity_input_lineage(
        observation, component_fresh_until=fresh_until
    )
    return {
        "key": key,
        "label": label,
        "value": float(value),
        "display_value": f"{value:,.{decimals}f}{unit}",
        "change": None,
        "unit": unit,
        "quality_status": observation.quality_status,
        "source": observation.source.name,
        "source_key": observation.source.key,
        "source_keys": [observation.source.key],
        "fallback_source": None,
        "value_date": observation.value_date.isoformat(),
        "as_of": observation.as_of.isoformat(),
        "fetched_at": observation.fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "batch_id": str(observation.batch_id),
        "metadata": {
            "source_field": observation.series.key,
            "input_series": [observation.series.key],
            "input_batch_ids": [str(observation.batch_id)],
            "input_value_dates": [observation.value_date.isoformat()],
            "input_lineage": [lineage],
        },
    }


def _rrp_tga_page_data(
    selected: dict[str, IngestionRun],
    *,
    today_et: date,
    now: datetime,
) -> tuple[
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None,
    list[dict[str, Any]],
]:
    required_sources = {"internal", "ny-fed-markets", "treasury-fiscal-data"}
    public_keys, derived_keys = current_display_source_key_sets(required_sources)
    if not required_sources <= public_keys or not required_sources <= derived_keys:
        return None, [
            _rrp_tga_run_state(
                identity,
                run,
                status="unlicensed",
                reason="a required direct or derived display licence is not current",
            )
            for identity, run in selected.items()
        ]

    series_batches = {
        "ONRRP": selected["onrrp"].batch_id,
        "ONRRP-RATE": selected["onrrp"].batch_id,
        "ONRRP-PARTICIPANTS": selected["onrrp"].batch_id,
        "TGA": selected["tga"].batch_id,
    }
    expected_sources = {
        "ONRRP": "ny-fed-markets",
        "ONRRP-RATE": "ny-fed-markets",
        "ONRRP-PARTICIPANTS": "ny-fed-markets",
        "TGA": "treasury-fiscal-data",
    }
    observations: dict[str, list[Observation]] = {}
    latest: dict[str, Observation] = {}
    deadlines: dict[str, datetime] = {}
    for series_key, batch_id in series_batches.items():
        rows = list(
            _real_observations(
                series_key,
                source_key=expected_sources[series_key],
                batch_id=batch_id,
            )
        )
        if not rows:
            return None, [
                _rrp_tga_run_state(
                    "onrrp" if series_key.startswith("ONRRP") else "tga",
                    selected[
                        "onrrp" if series_key.startswith("ONRRP") else "tga"
                    ],
                    status="missing",
                    reason=f"exact-batch {series_key} observations are missing",
                )
            ]
        deduped: dict[date, Observation] = {}
        for item in rows:
            if item.value_date.date() > today_et:
                return None, [
                    _rrp_tga_run_state(
                        "onrrp" if series_key.startswith("ONRRP") else "tga",
                        selected[
                            "onrrp" if series_key.startswith("ONRRP") else "tga"
                        ],
                        status="invalid",
                        reason=f"exact-batch {series_key} contains a future value date",
                    )
                ]
            if item.value_date.date() in deduped:
                return None, [
                    _rrp_tga_run_state(
                        "onrrp" if series_key.startswith("ONRRP") else "tga",
                        selected[
                            "onrrp"
                            if series_key.startswith("ONRRP")
                            else "tga"
                        ],
                        status="invalid",
                        reason=(
                            f"exact-batch {series_key} contains duplicate "
                            "observations for one value date"
                        ),
                    )
                ]
            deduped[item.value_date.date()] = item
        if not deduped:
            return None, [
                _rrp_tga_run_state(
                    "onrrp" if series_key.startswith("ONRRP") else "tga",
                    selected[
                        "onrrp" if series_key.startswith("ONRRP") else "tga"
                    ],
                    status="invalid",
                    reason=f"exact-batch {series_key} has no non-future observation",
                )
            ]
        ordered = [deduped[key] for key in sorted(deduped, reverse=True)]
        run = selected["onrrp" if series_key.startswith("ONRRP") else "tga"]
        run_fetched = _parse_payload_datetime((run.metadata or {}).get("fetched_at"))
        if any(
            item.source.key != expected_sources[series_key]
            or item.batch_id != batch_id
            or item.fallback_source_id
            or item.quality_status != Observation.Quality.FRESH
            or item.fetched_at != run_fetched
            for item in ordered
        ):
            return None, [
                _rrp_tga_run_state(
                    "onrrp" if series_key.startswith("ONRRP") else "tga",
                    run,
                    status="invalid",
                    reason=(
                        f"an exact-batch {series_key} history point is fallback, "
                        "invalid quality, mixed-batch or from the wrong source/fetch"
                    ),
                )
            ]
        observations[series_key] = ordered
        current = ordered[0]
        deadline = _fresh_until(current)
        if (
            deadline < now
        ):
            return None, [
                _rrp_tga_run_state(
                    "onrrp" if series_key.startswith("ONRRP") else "tga",
                    run,
                    status="invalid",
                    reason=(
                        f"latest exact-batch {series_key} is stale, fallback, "
                        "mixed-batch or from the wrong source"
                    ),
                )
            ]
        latest[series_key] = current
        deadlines[series_key] = deadline

    onrrp_current_dates = {
        latest[series_key].value_date
        for series_key in ("ONRRP", "ONRRP-RATE", "ONRRP-PARTICIPANTS")
    }
    if len(onrrp_current_dates) != 1:
        return None, [
            _rrp_tga_run_state(
                "onrrp",
                selected["onrrp"],
                status="invalid",
                reason=(
                    "ON RRP balance, rate and participants do not share one "
                    "current operation value date"
                ),
            )
        ]

    auction_prepared, auction_failures = _auction_page_data(
        selected["auctions"], today_et=today_et, now=now
    )
    if auction_prepared is None:
        return None, auction_failures
    auction_metrics, auction_charts, auction_sections, _auction_extra = (
        auction_prepared
    )
    issue_metrics = {
        item["key"]: item
        for item in auction_metrics
        if item["key"] in {"issue-gross-7d", "issue-gross-14d"}
    }
    issue_chart = next(
        item for item in auction_charts if item["key"] == "gross-issue-calendar"
    )
    issue_section = next(
        item
        for item in auction_sections
        if item["key"] == "issue-settlement-14d"
    )

    metrics = [
        _rrp_tga_observation_metric(
            key="onrrp",
            label="ON RRP",
            observation=latest["ONRRP"],
            scale=Decimal("0.001"),
            decimals=3,
            unit=" USD bn",
            fresh_until=deadlines["ONRRP"],
        ),
        _rrp_tga_observation_metric(
            key="onrrp-rate",
            label="ON RRP 利率",
            observation=latest["ONRRP-RATE"],
            scale=Decimal("1"),
            decimals=2,
            unit="%",
            fresh_until=deadlines["ONRRP-RATE"],
        ),
        _rrp_tga_observation_metric(
            key="onrrp-participants",
            label="ON RRP 交易对手",
            observation=latest["ONRRP-PARTICIPANTS"],
            scale=Decimal("1"),
            decimals=0,
            unit=" 家",
            fresh_until=deadlines["ONRRP-PARTICIPANTS"],
        ),
        _rrp_tga_observation_metric(
            key="tga",
            label="TGA",
            observation=latest["TGA"],
            scale=Decimal("0.001"),
            decimals=3,
            unit=" USD bn",
            fresh_until=deadlines["TGA"],
        ),
        issue_metrics["issue-gross-7d"],
        issue_metrics["issue-gross-14d"],
    ]

    history_by_date: dict[date, dict[str, Any]] = {}
    for series_key, label in (("ONRRP", "ON RRP"), ("TGA", "TGA")):
        for item in reversed(observations[series_key][:90]):
            period = item.value_date.date()
            row = history_by_date.setdefault(period, {"date": period.isoformat()})
            row[label] = float(item.value * Decimal("0.001"))
            row.setdefault("_source_keys", []).append(item.source.key)
            row.setdefault("_lineage", {})[label] = _liquidity_input_lineage(
                item, component_fresh_until=deadlines[series_key]
            )
    history = _lineage_chart(
        key="rrp-tga-history",
        title="ON RRP 与 TGA 历史",
        description=(
            "分别保留纽约联储 ON RRP 与 Treasury DTS TGA 的精确本批次历史；"
            "不同有效日不强行拼成净流动性。单位：十亿美元。"
        ),
        rows=[history_by_date[key] for key in sorted(history_by_date)],
        fields=("ON RRP", "TGA"),
        tab="balances",
        frequency="daily",
        include_internal=False,
    )
    if history is None:
        return None, [
            _rrp_tga_run_state(
                "onrrp",
                selected["onrrp"],
                status="invalid",
                reason="exact-batch ON RRP/TGA history chart is not buildable",
            )
        ]
    extra_data = {
        "contract_version": RRP_TGA_CONTRACT_VERSION,
        "as_of_date_et": today_et.isoformat(),
        "timezone": "America/New_York",
        "coverage_complete": True,
        "refresh_cycle_id": str(
            (selected["auctions"].metadata or {}).get("refresh_cycle_id")
        ),
        "component_snapshots": [
            _rrp_tga_run_state(identity, run, status="valid")
            for identity, run in selected.items()
        ],
        "model_disclaimer": (
            "The issue calendar is gross announced face amount, not actual cash, "
            "a TGA forecast, net financing or a net-liquidity forecast."
        ),
    }
    sections = [
        issue_section,
        {
            "key": "interpretation-boundary",
            "title": "口径边界",
            "body": (
                "ON RRP 和 TGA 显示各自官方最近有效值；发行/结算表仅汇总公告总面值。"
                "三者不被合成为未来净抽水，也不预测实际财政现金流。"
            ),
            "status": Observation.Quality.ESTIMATED,
            "source_key": "internal",
            "source_keys": [
                "internal",
                "ny-fed-markets",
                "treasury-fiscal-data",
            ],
            "as_of": min(item["as_of"] for item in metrics),
            "fetched_at": max(item["fetched_at"] for item in metrics),
            "fresh_until": min(item["fresh_until"] for item in metrics),
            "batch_id": ",".join(
                sorted(str(run.batch_id) for run in selected.values())
            ),
            "license_scope": ensure_source("internal").license_scope,
            "fallback_source": None,
            "full_width": True,
        },
    ]
    return (metrics, [history, issue_chart], sections, extra_data), []


def _latest_rrp_tga_snapshot() -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(
            key="rrp-tga",
            is_published=True,
            data__contract_version=RRP_TGA_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )


def _mark_rrp_tga_stale(
    components: list[dict[str, Any]], *, reason: str
) -> None:
    latest = (
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="rrp-tga",
            is_published=True,
            data__contract_version=RRP_TGA_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )
    if latest is None:
        return
    summary = "；".join(
        f"{item.get('component', 'unknown')}[{item.get('status', 'invalid')}] "
        f"{item.get('reason', 'unknown failure')}"
        for item in components
    )
    data = dict(latest.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason": f"{reason} 失败组件：{summary}",
        "components": components,
    }
    latest.data = data
    latest.quality_status = Observation.Quality.STALE
    latest.save(update_fields=["data", "quality_status", "updated_at"])


def _rrp_tga_snapshot_contract_is_valid(
    snapshot: DashboardSnapshot,
    *,
    selected: dict[str, IngestionRun],
    today_et: date,
) -> bool:
    data = dict(snapshot.data or {})
    metrics = list(data.get("metrics", []))
    charts = list(data.get("charts", []))
    expected_batches = {str(run.batch_id) for run in selected.values()}
    if (
        data.get("contract_version") != RRP_TGA_CONTRACT_VERSION
        or data.get("as_of_date_et") != today_et.isoformat()
        or not data.get("coverage_complete")
        or data.get("refresh_failure")
        or data.get("publication_batch_id") != str(snapshot.batch_id)
        or set(data.get("component_batches", [])) != expected_batches
        or {str(item.get("key") or "") for item in metrics}
        != set(RRP_TGA_REQUIRED_METRIC_KEYS)
        or {str(item.get("key") or "") for item in charts}
        != {"rrp-tga-history", "gross-issue-calendar"}
    ):
        return False
    chart_by_key = {item["key"]: item for item in charts}
    for chart in charts:
        if (
            not chart.get("batch_ids")
            or not chart.get("license_scopes")
            or chart.get("fallback_sources")
        ):
            return False
    expected_history = {
        "ON RRP": ("ny-fed-markets", str(selected["onrrp"].batch_id)),
        "TGA": ("treasury-fiscal-data", str(selected["tga"].batch_id)),
    }
    for row in chart_by_key["rrp-tga-history"].get("data", []):
        if not row.get("date"):
            return False
        for field, (source_key, batch_id) in expected_history.items():
            if field not in row:
                continue
            lineage = (row.get("_lineage") or {}).get(field)
            if (
                not isinstance(lineage, dict)
                or lineage.get("source_key") != source_key
                or lineage.get("batch_id") != batch_id
                or lineage.get("fallback_source") is not None
                or not lineage.get("license_scope")
                or not lineage.get("fetched_at")
                or not lineage.get("value_date")
                or str(lineage["value_date"])[:10] != str(row["date"])
            ):
                return False
    issue_chart_rows = list(
        chart_by_key["gross-issue-calendar"].get("data", [])
    )
    expected_issue_dates = {
        (today_et + timedelta(days=offset)).isoformat()
        for offset in range(14)
    }
    issue_field = "Gross announced issue amount"
    if {
        str(row.get("date") or "") for row in issue_chart_rows
    } != expected_issue_dates:
        return False
    for row in issue_chart_rows:
        lineage = (row.get("_lineage") or {}).get(issue_field)
        if (
            issue_field not in row
            or not isinstance(lineage, dict)
            or lineage.get("source_key") != "internal"
            or lineage.get("batch_id") != str(selected["auctions"].batch_id)
            or lineage.get("fallback_source") is not None
            or not lineage.get("license_scope")
            or not lineage.get("fetched_at")
            or not lineage.get("value_date")
        ):
            return False
    metric_by_key = {item["key"]: item for item in metrics}
    issue_by_date = {
        date.fromisoformat(str(row["date"])): Decimal(str(row[issue_field]))
        for row in issue_chart_rows
    }
    if (
        sum(
            (
                issue_by_date[today_et + timedelta(days=offset)]
                for offset in range(7)
            ),
            Decimal("0"),
        )
        != Decimal(str(metric_by_key["issue-gross-7d"]["value"]))
        or sum(issue_by_date.values(), Decimal("0"))
        != Decimal(str(metric_by_key["issue-gross-14d"]["value"]))
    ):
        return False
    for item in metrics:
        if (
            not item.get("license_scope")
            or "fallback_source" not in item
            or not item.get("batch_id")
            or not item.get("fetched_at")
            or not (item.get("metadata") or {}).get("input_lineage")
            and item.get("value") not in {0, 0.0}
        ):
            return False
    issue_section = next(
        (
            item
            for item in data.get("sections", [])
            if item.get("key") == "issue-settlement-14d"
        ),
        None,
    )
    if issue_section is None:
        return False
    for row in issue_section.get("rows", []):
        if (
            row.get("batch_id") != str(selected["auctions"].batch_id)
            or not row.get("license_scope")
            or "fallback_source" not in row
            or not row.get("input_lineage")
        ):
            return False
    normalized = {
        item.key: item
        for item in MetricSnapshot.objects.filter(
            batch_id=snapshot.batch_id, key__startswith="rrp-tga-"
        )
    }
    for metric in metrics:
        stored = normalized.get(f"rrp-tga-{metric['key']}")
        if (
            stored is None
            or stored.value != Decimal(str(metric["value"]))
            or stored.source.key != metric["source_key"]
            or stored.fallback_source_id is not None
            or stored.value_date
            != _parse_payload_datetime(metric.get("value_date"))
            or stored.fetched_at
            != _parse_payload_datetime(metric.get("fetched_at"))
            or stored.metadata.get("component_batch_id")
            != metric.get("batch_id")
            or stored.metadata.get("input_lineage")
            != (metric.get("metadata") or {}).get("input_lineage")
            or not stored.license_scope
        ):
            return False
    return True


@transaction.atomic
def _coordinate_rrp_tga_dashboard(
    trigger_runs: Iterable[IngestionRun],
    *,
    as_of_date: date | None = None,
) -> tuple[list[DashboardSnapshot], set[str]]:
    source_keys = {
        "internal",
        *(source_key for source_key, _dataset in RRP_TGA_DATASETS.values()),
    }
    for source_key in source_keys:
        ensure_source(source_key)
    list(
        Source.objects.select_for_update()
        .filter(key__in=source_keys)
        .order_by("key")
        .values_list("pk", flat=True)
    )
    now = timezone.now()
    today_et = as_of_date or now.astimezone(
        ZoneInfo("America/New_York")
    ).date()
    selected, states, triggered = _select_rrp_tga_runs(
        trigger_runs, today_et=today_et, now=now
    )
    if not triggered:
        return [], set()
    if selected is None:
        _mark_rrp_tga_stale(
            states,
            reason=(
                "ON RRP、TGA 与拍卖日历未形成当前 ET 日同一刷新周期的"
                "三个完整批次；继续保留上一版。"
            ),
        )
        return [], {"rrp-tga"}
    prepared, failures = _rrp_tga_page_data(
        selected, today_et=today_et, now=now
    )
    if prepared is None:
        _mark_rrp_tga_stale(
            failures,
            reason=(
                "RRP/TGA 页面未通过精确批次、许可、新鲜度或血缘检查；"
                "继续保留上一版完整快照。"
            ),
        )
        return [], {"rrp-tga"}
    metrics, charts, sections, extra_data = prepared
    previous = _latest_rrp_tga_snapshot()
    if previous is not None:
        try:
            previous_date = date.fromisoformat(
                str((previous.data or {}).get("as_of_date_et") or "")
            )
        except ValueError:
            previous_date = previous.as_of.astimezone(
                ZoneInfo("America/New_York")
            ).date()
        if today_et < previous_date:
            _mark_rrp_tga_stale(
                [
                    _rrp_tga_run_state(
                        "auctions",
                        selected["auctions"],
                        status="stale",
                        reason="candidate ET date is older than published v1 snapshot",
                    )
                ],
                reason="候选 RRP/TGA 日期发生回退；拒绝发布并保留上一版。",
            )
            return [], {"rrp-tga"}
    try:
        with transaction.atomic():
            publication_batch = uuid.uuid4()
            snapshot = _publish_dashboard(
                key="rrp-tga",
                title="RRP 与 TGA",
                summary=(
                    "ON RRP、TGA 与 Treasury 发行/结算日历只在同一完整刷新周期"
                    "发布。发行金额是公告总面值，不是实际现金、未来 TGA 方向、"
                    "净融资或净流动性预测，也不与不同有效日余额合成净抽水。"
                ),
                metrics=metrics,
                charts=charts,
                sections=sections,
                extra_data=extra_data,
                required_metric_keys=RRP_TGA_REQUIRED_METRIC_KEYS,
                batch_id=publication_batch,
            )
            latest = _latest_rrp_tga_snapshot()
            if latest is None or not _rrp_tga_snapshot_contract_is_valid(
                latest, selected=selected, today_et=today_et
            ):
                raise ValueError("rrp-tga publication postcondition failed")
    except (ValueError, StopIteration):
        _mark_rrp_tga_stale(
            [
                _rrp_tga_run_state(
                    "auctions",
                    selected["auctions"],
                    status="invalid",
                    reason="publication postcondition failed",
                )
            ],
            reason=(
                "RRP/TGA v1 发布后置条件未满足；新写入已回滚，继续保留"
                "上一版完整快照。"
            ),
        )
        return [], {"rrp-tga"}
    return ([snapshot] if snapshot is not None else []), set()


def _latest_treasury_contract_snapshot(page_key: str) -> DashboardSnapshot | None:
    return (
        DashboardSnapshot.objects.filter(
            key=page_key,
            is_published=True,
            data__contract_version=TREASURY_CURVE_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .order_by("-created_at", "-id")
        .first()
    )


def _mark_treasury_curve_dashboards_stale(
    page_keys: Iterable[str],
    components: list[dict[str, Any]],
    *,
    reason: str,
) -> None:
    checked_at = timezone.now().isoformat()
    for page_key in page_keys:
        latest = (
            DashboardSnapshot.objects.select_for_update()
            .filter(
                key=page_key,
                is_published=True,
                data__contract_version=TREASURY_CURVE_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .order_by("-created_at", "-id")
            .first()
        )
        if latest is None:
            continue
        data = dict(latest.data or {})
        data["refresh_failure"] = {
            "checked_at": checked_at,
            "reason": reason,
            "components": components,
            "sources": [
                {
                    "source": item.get("dataset") or item.get("component"),
                    "status": item.get("status") or "invalid",
                    "row_count": item.get("row_count") or 0,
                    "error": item.get("reason") or "",
                }
                for item in components
            ],
        }
        latest.data = data
        latest.quality_status = Observation.Quality.STALE
        latest.save(update_fields=["data", "quality_status", "updated_at"])


def _treasury_prepared_contract_is_buildable(
    page_key: str,
    prepared: tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ],
    *,
    expected_batches: set[str],
) -> bool:
    metrics, charts, _sections, extra_data = prepared
    required_metrics = {
        "yield-curve": YIELD_CURVE_REQUIRED_METRIC_KEYS,
        "real-rates": REAL_RATES_REQUIRED_METRIC_KEYS,
    }.get(page_key)
    required_charts = {
        "yield-curve": {"nominal-curve-comparison", "curve-spreads-history"},
        "real-rates": {"nominal-real-breakeven-history"},
    }.get(page_key)
    if (
        extra_data.get("contract_version") != TREASURY_CURVE_CONTRACT_VERSION
        or not extra_data.get("common_effective_date")
        or not isinstance(extra_data.get("annual_runs"), list)
        or any(
            item.get("status") != IngestionRun.Status.SUCCESS
            or not item.get("batch_id")
            for item in extra_data["annual_runs"]
        )
        or (required_metrics is not None and {item.get("key") for item in metrics} != set(required_metrics))
        or (required_charts is not None and {item.get("key") for item in charts} != required_charts)
        or any(
            item.get("value") is None
            or item.get("fallback_source")
            or item.get("quality_status")
            not in {Observation.Quality.FRESH, Observation.Quality.ESTIMATED}
            or not item.get("fresh_until")
            or not _payload_batch_ids(item)
            or not _payload_source_keys(item)
            for item in metrics
        )
        or any(
            not item.get("data")
            or not item.get("batch_ids")
            or not item.get("fresh_until")
            or item.get("time_axis") not in {"date", "tenor"}
            for item in charts
        )
        or not publicly_displayable_source_keys(
            _payload_source_keys([metrics, charts])
        )
    ):
        return False
    actual_batches = _payload_batch_ids([metrics, charts])
    return actual_batches == expected_batches


def _treasury_rates_overview_prepared(
    prepared: dict[
        str,
        tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
            dict[str, Any],
        ],
    ],
) -> tuple[
    tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None,
    dict[str, Any] | None,
]:
    fed_component = _liquidity_fed_funds_component(now=timezone.now())
    if isinstance(fed_component, dict):
        return None, fed_component
    policy_metrics, policy_reference = fed_component
    yield_metrics, yield_charts, _yield_sections, yield_extra = prepared["yield-curve"]
    real_metrics, real_charts, _real_sections, _real_extra = prepared["real-rates"]
    yield_by_key = {item["key"]: deepcopy(item) for item in yield_metrics}
    real_by_key = {item["key"]: deepcopy(item) for item in real_metrics}
    metrics = [
        *policy_metrics,
        yield_by_key["ust-2y"],
        yield_by_key["ust-10y"],
        yield_by_key["2s10s"],
        real_by_key["tips-10y"],
        real_by_key["10y-bei"],
    ]
    charts = [deepcopy(yield_charts[1]), deepcopy(real_charts[0])]
    for chart in charts:
        chart["tab"] = "treasury"
    extra_data = {
        **deepcopy(yield_extra),
        "curve_scope": "rates-overview",
        "component_snapshots": [policy_reference],
    }
    sections = [
        {
            "title": "组合口径",
            "body": (
                "政策利率继承已验证的 Fed Funds 原子快照；国债、实际利率与 BEI "
                "继承同一 Treasury curve v1 合同。任一子组件失败时保留上一版。"
            ),
            "full_width": True,
        }
    ]
    return (metrics, charts, sections, extra_data), None


@transaction.atomic
def _coordinate_treasury_curve_dashboards(
    trigger_runs: Iterable[IngestionRun],
    *,
    end_year: int,
) -> tuple[list[DashboardSnapshot], set[str]]:
    ensure_source("us-treasury-rates")
    ensure_source("internal")
    list(
        Source.objects.select_for_update()
        .filter(key__in={"us-treasury-rates", "internal"})
        .order_by("key")
        .values_list("pk", flat=True)
    )
    selected, states, triggered = _select_treasury_curve_runs(
        trigger_runs, end_year=end_year
    )
    if not triggered:
        return [], set()
    if selected is None:
        _mark_treasury_curve_dashboards_stale(
            {"yield-curve", "real-rates", "rates"},
            states,
            reason=(
                "Treasury 必需年度数据集未全部成功，或当年名义/实际曲线不属于"
                "同一刷新周期；继续保留上一版完整快照。"
            ),
        )
        return [], {"yield-curve", "real-rates", "rates"}

    prepared, failures = _treasury_curve_page_data(selected)
    if prepared is None:
        _mark_treasury_curve_dashboards_stale(
            {"yield-curve", "real-rates", "rates"},
            failures,
            reason=(
                "Treasury 曲线未形成同日、跨年度批次明确且许可可公开的完整合同；"
                "继续保留上一版。"
            ),
        )
        return [], {"yield-curve", "real-rates", "rates"}

    expected_nominal_batches = {
        str(run.batch_id)
        for (component, _year), run in selected.items()
        if component == "nominal"
    }
    expected_all_batches = {str(run.batch_id) for run in selected.values()}
    if not _treasury_prepared_contract_is_buildable(
        "yield-curve",
        prepared["yield-curve"],
        expected_batches=expected_nominal_batches,
    ) or not _treasury_prepared_contract_is_buildable(
        "real-rates",
        prepared["real-rates"],
        expected_batches=expected_all_batches,
    ):
        failures = [
            {
                "component": "contract",
                "year": end_year,
                "dataset": "treasury-curve-v1",
                "status": "invalid",
                "reason": "prepared Treasury page failed its v1 contract check",
            }
        ]
        _mark_treasury_curve_dashboards_stale(
            {"yield-curve", "real-rates", "rates"},
            failures,
            reason="Treasury v1 发布前置条件未满足；继续保留上一版。",
        )
        return [], {"yield-curve", "real-rates", "rates"}

    candidate_date = date.fromisoformat(
        prepared["yield-curve"][3]["common_effective_date"]
    )
    previous_snapshots = [
        snapshot
        for key in TREASURY_CURVE_PAGE_KEYS
        if (snapshot := _latest_treasury_contract_snapshot(key)) is not None
    ]
    if any(
        candidate_date
        < date.fromisoformat(
            str((snapshot.data or {}).get("common_effective_date"))
        )
        for snapshot in previous_snapshots
    ):
        failures = [
            {
                "component": "common-date",
                "year": end_year,
                "dataset": "treasury-curve-v1",
                "status": Observation.Quality.STALE,
                "reason": "candidate common effective date regressed behind a published v1 snapshot",
            }
        ]
        _mark_treasury_curve_dashboards_stale(
            {"yield-curve", "real-rates", "rates"},
            failures,
            reason="Treasury 候选共同有效日发生回退；拒绝发布并保留上一版。",
        )
        return [], {"yield-curve", "real-rates", "rates"}

    rates_prepared, rates_failure = _treasury_rates_overview_prepared(prepared)
    selected_keys = {"yield-curve", "real-rates"}
    stale_keys: set[str] = set()
    if rates_prepared is not None:
        prepared["rates"] = rates_prepared
        selected_keys.add("rates")
    else:
        stale_keys.add("rates")
        _mark_treasury_curve_dashboards_stale(
            {"rates"},
            [rates_failure or {"component": "fed-funds", "status": "missing"}],
            reason=(
                "利率总览所需 Fed Funds 子快照未通过验证；Treasury 子页照常发布，"
                "总览继续保留上一版。"
            ),
        )

    try:
        with transaction.atomic():
            dashboards = publish_official_dashboards(
                keys=selected_keys,
                prepared_treasury_curve_data=prepared,
            )
            for key in ("yield-curve", "real-rates"):
                latest = _latest_treasury_contract_snapshot(key)
                expected_batches = (
                    expected_nominal_batches
                    if key == "yield-curve"
                    else expected_all_batches
                )
                if (
                    latest is None
                    or latest.data.get("publication_batch_id") != str(latest.batch_id)
                    or latest.data.get("common_effective_date")
                    != candidate_date.isoformat()
                    or latest.data.get("refresh_failure")
                    or set(latest.data.get("component_batches", []))
                    != expected_batches
                    or not _treasury_prepared_contract_is_buildable(
                        key,
                        (
                            list(latest.data.get("metrics", [])),
                            list(latest.data.get("charts", [])),
                            list(latest.data.get("sections", [])),
                            dict(latest.data),
                        ),
                        expected_batches=expected_batches,
                    )
                ):
                    raise ValueError(
                        f"Treasury {key} publication postcondition failed"
                    )
    except ValueError as exc:
        failures = [
            {
                "component": "publication",
                "year": end_year,
                "dataset": "treasury-curve-v1",
                "status": "invalid",
                "reason": str(exc),
            }
        ]
        _mark_treasury_curve_dashboards_stale(
            {"yield-curve", "real-rates", "rates"},
            failures,
            reason=(
                "Treasury 发布后置条件未满足；新写入已回滚，继续保留上一版完整快照。"
            ),
        )
        return [], {"yield-curve", "real-rates", "rates"}
    return dashboards, stale_keys


def _gdp_vintage_chart_and_section() -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Build the public GDP revision trail from one complete BEA workbook batch."""

    latest_run = (
        IngestionRun.objects.filter(
            source__key="bea-release",
            dataset="gdp-release-workbooks",
            status=IngestionRun.Status.SUCCESS,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    if latest_run is None:
        return None, None
    vintages = list(
        ReleaseVintageObservation.objects.filter(
            source=latest_run.source,
            series__key="bea-a191rl",
            batch_id=latest_run.batch_id,
        )
        .filter(public_display_license_q())
        .select_related("series", "source", "fallback_source")
        .order_by("value_date", "release_date", "id")
    )
    if not vintages:
        return None, None
    periods: dict[datetime, list[ReleaseVintageObservation]] = {}
    for item in vintages:
        periods.setdefault(item.value_date, []).append(item)
    latest_period = max(periods)
    current = (
        Observation.objects.filter(
            source=latest_run.source,
            series__key="bea-a191rl",
            batch_id=latest_run.batch_id,
            value_date=latest_period,
        )
        .select_related("series", "source", "fallback_source")
        .first()
    )
    if current is None:
        return None, None
    fresh_until = _fresh_until(current)
    quality_status = current.quality_status
    if timezone.now() > fresh_until and quality_status == Observation.Quality.FRESH:
        quality_status = Observation.Quality.STALE

    def lineage(item: ReleaseVintageObservation) -> dict[str, Any]:
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
            "fallback_source": (
                item.fallback_source.key if item.fallback_source_id else None
            ),
        }

    latest_entries = periods[latest_period]
    chart_rows = [
        {
            "date": f"{item.vintage_label}\n{item.release_date:%m-%d}",
            "实际 GDP": float(item.value),
            "_source_keys": [item.source.key],
            "_lineage": {"实际 GDP": lineage(item)},
        }
        for item in latest_entries
    ]
    latest_item = latest_entries[-1]
    quarter_label = (
        f"{latest_period.year}Q{((latest_period.month - 1) // 3) + 1}"
    )
    chart = {
        "key": "gdp-vintage-trail",
        "title": f"{quarter_label} 实际 GDP 估算修订",
        "description": "按 BEA 官方发布日期展示每轮季调年化环比估算，单位：%。",
        "kind": "line",
        "panel_class": "lg:col-span-2",
        "data": chart_rows,
        "source_keys": [latest_run.source.key],
        "as_of": latest_item.as_of.isoformat(),
        "fetched_at": max(item.fetched_at for item in latest_entries).isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "quality_status": quality_status,
        "batch_ids": [str(latest_run.batch_id)],
    }

    section_rows = []
    for period in sorted(periods, reverse=True)[:8]:
        entries = periods[period]
        first, latest = entries[0], entries[-1]
        revision = latest.value - first.value
        labels = " → ".join(item.vintage_label for item in entries)
        values = " → ".join(f"{item.value:.2f}%" for item in entries)
        release_path = " · ".join(
            f"{item.vintage_label} {item.release_date.isoformat()}" for item in entries
        )
        section_rows.append(
            {
                "label": f"{period.year}Q{((period.month - 1) // 3) + 1}",
                "display_value": values,
                "status": f"{labels}；累计修订 {revision:+.2f}pp",
                "description": release_path,
                "source": latest.source.name,
                "source_key": latest.source.key,
                "source_keys": [latest.source.key],
                "as_of": latest.as_of.isoformat(),
                "fetched_at": latest.fetched_at.isoformat(),
                "quality_status": latest.quality_status,
                "license_scope": latest.license_scope,
                "fallback_source": (
                    latest.fallback_source.key if latest.fallback_source_id else None
                ),
                "batch_id": str(latest.batch_id),
            }
        )
    section = {
        "title": "GDP 发布轮次与修订路径",
        "description": (
            f"当前官方工作簿保留 {len(vintages):,} 条实际 GDP 发布记录；"
            "表格展示最近 8 个观察季度，箭头严格按发布日期排序。"
        ),
        "rows": section_rows,
        "status": quality_status,
        "full_width": True,
        "source_key": latest_run.source.key,
        "source_keys": [latest_run.source.key],
        "as_of": latest_item.as_of.isoformat(),
        "fetched_at": latest_item.fetched_at.isoformat(),
        "fresh_until": fresh_until.isoformat(),
        "quality_status": quality_status,
        "batch_id": str(latest_run.batch_id),
    }
    return chart, section


def _earliest_fresh_until(rows: Iterable[dict[str, Any]]) -> str | None:
    return min(
        (item["fresh_until"] for item in rows if item.get("fresh_until")),
        default=None,
    )


def _sofr_market_metrics() -> list[dict[str, Any]]:
    observations = _latest_observations_by_value_date("SOFR", limit=2)
    if not observations:
        return []
    latest = observations[0]
    previous = observations[1] if len(observations) > 1 else None
    fresh_until = _fresh_until(latest)
    quality = Observation.Quality.STALE if timezone.now() > fresh_until else latest.quality_status
    latest_source_keys = sorted(_observation_source_keys(latest))
    definitions = (
        ("sofr-volume", "SOFR 成交量", "volumeInBillions", " USD bn", 0),
        ("sofr-p99", "SOFR 99P", "percentPercentile99", "%", 2),
    )
    metrics: list[dict[str, Any]] = []
    for key, label, metadata_key, suffix, decimals in definitions:
        raw_value = latest.metadata.get(metadata_key)
        if raw_value is None:
            continue
        value = Decimal(str(raw_value))
        previous_raw = previous.metadata.get(metadata_key) if previous else None
        change = value - Decimal(str(previous_raw)) if previous_raw is not None else None
        metrics.append(
            {
                "key": key,
                "label": label,
                "value": float(value),
                "display_value": f"{value:,.{decimals}f}{suffix}",
                "change": float(change) if change is not None else None,
                "change_unit": "pp" if suffix == "%" else suffix,
                "unit": suffix,
                "quality_status": quality,
                "source": (
                    f"{latest.source.name}（备用：{latest.fallback_source.name}）"
                    if latest.fallback_source_id
                    else latest.source.name
                ),
                "source_key": latest.source.key,
                "source_keys": latest_source_keys,
                "fallback_source": (
                    latest.fallback_source.key
                    if latest.fallback_source_id
                    else None
                ),
                "as_of": latest.as_of.isoformat(),
                "value_date": latest.value_date.isoformat(),
                "fetched_at": latest.fetched_at.isoformat(),
                "fresh_until": fresh_until.isoformat(),
                "batch_id": str(latest.batch_id),
                "metadata": {"upstream_field": metadata_key},
            }
        )
    percentile_99 = latest.metadata.get("percentPercentile99")
    if percentile_99 is not None:
        tail = (Decimal(str(percentile_99)) - latest.value) * Decimal("100")
        metrics.append(
            {
                "key": "sofr-p99-minus-rate",
                "label": "SOFR 99P−SOFR",
                "value": float(tail),
                "display_value": f"{tail:+,.0f}bp",
                "change": None,
                "unit": "bp",
                "quality_status": Observation.Quality.ESTIMATED,
                "source": f"Atlas Macro 计算：{latest.source.name}",
                "source_key": "internal",
                "source_keys": sorted(
                    _observation_source_keys(latest) | {"internal"}
                ),
                "as_of": latest.as_of.isoformat(),
                "value_date": latest.value_date.isoformat(),
                "fetched_at": latest.fetched_at.isoformat(),
                "fresh_until": fresh_until.isoformat(),
                "batch_id": str(latest.batch_id),
                "metadata": {
                    "formula": "SOFR percentPercentile99 - percentRate",
                    "source_keys": sorted(_observation_source_keys(latest)),
                },
            }
        )
        iorb = _real_observations("IORB").filter(
            value_date__date=latest.value_date.date()
        ).first()
        if iorb is not None:
            iorb_tail = (Decimal(str(percentile_99)) - iorb.value) * Decimal("100")
            iorb_fresh_until = min(fresh_until, _fresh_until(iorb))
            input_source_keys = _observation_source_keys(latest, iorb)
            source_keys = sorted(input_source_keys | {"internal"})
            metrics.append(
                {
                    "key": "sofr-p99-minus-iorb",
                    "label": "SOFR 99P−IORB",
                    "value": float(iorb_tail),
                    "display_value": f"{iorb_tail:+,.0f}bp",
                    "change": None,
                    "unit": "bp",
                    "quality_status": (
                        Observation.Quality.STALE
                        if timezone.now() > iorb_fresh_until
                        else Observation.Quality.ESTIMATED
                    ),
                    "source": (
                        f"Atlas Macro 计算：{latest.source.name} 99P − {iorb.source.name} IORB"
                    ),
                    "source_key": "internal",
                    "source_keys": source_keys,
                    "as_of": latest.as_of.isoformat(),
                    "value_date": latest.value_date.isoformat(),
                    "fetched_at": max(latest.fetched_at, iorb.fetched_at).isoformat(),
                    "fresh_until": iorb_fresh_until.isoformat(),
                    "batch_id": f"{latest.batch_id},{iorb.batch_id}",
                    "metadata": {
                        "formula": "SOFR percentPercentile99 - IORB",
                        "source_keys": sorted(input_source_keys),
                        "input_series": ["iorb", "sofr"],
                        "input_batch_ids": sorted(
                            {str(latest.batch_id), str(iorb.batch_id)}
                        ),
                        "input_value_dates": [
                            latest.value_date.isoformat()
                        ],
                    },
                }
            )
    return metrics


def _sofr_market_history(*, limit: int = 120) -> list[dict[str, Any]]:
    rows = []
    for observation in reversed(
        _latest_observations_by_value_date("SOFR", limit=limit)
    ):
        row: dict[str, Any] = {
            "date": observation.value_date.date().isoformat(),
            "SOFR": float(observation.value),
            "_source_keys": sorted(_observation_source_keys(observation)),
        }
        if observation.metadata.get("percentPercentile99") is not None:
            row["99P"] = float(observation.metadata["percentPercentile99"])
        rows.append(row)
    return rows


def _store_board_archive_observations(result, source, run) -> int:
    """Persist Board DDP rows plus an immutable ZIP download fingerprint."""

    row_count = store_series_observations(result, source, run)
    archive_hash = str(result.metadata.get("archive_sha256") or "")
    archive_size = int(result.metadata.get("archive_size") or 0)
    source_url = str(result.metadata.get("source_url") or "")
    if archive_hash and source_url:
        RawArtifact.objects.create(
            run=run,
            uri=f"{source_url}#sha256={archive_hash}",
            sha256=archive_hash,
            content_type="application/zip",
            size_bytes=archive_size,
        )
    return row_count


def _store_series_with_artifacts(result, source, run) -> int:
    """Persist normalized series and immutable response fingerprints."""

    row_count = store_series_observations(result, source, run)
    for artifact in result.metadata.get("artifacts", []):
        url = str(artifact.get("url") or "")
        digest = str(artifact.get("sha256") or "")
        if not url or not digest:
            continue
        RawArtifact.objects.create(
            run=run,
            uri=f"{url}#sha256={digest}",
            sha256=digest,
            content_type=str(
                artifact.get("content_type") or "application/octet-stream"
            ),
            size_bytes=int(artifact.get("size") or 0),
        )
    return row_count


def _store_treasury_curve_observations(result, source, run) -> int:
    """Persist one annual Treasury curve without allowing its stored tail to regress."""

    incoming_series = {
        str(record.get("series_id") or "").lower()
        for record in result.records
        if record.get("series_id") and record.get("date")
    }
    incoming_dates = [
        date.fromisoformat(str(record["date"])[:10])
        for record in result.records
        if record.get("series_id") and record.get("date")
    ]
    requested_year = int((result.metadata or {}).get("requested_year") or 0)
    if not incoming_dates or requested_year <= 0:
        raise ValueError("Treasury annual curve has no dated rows or requested year")
    if {period.year for period in incoming_dates} != {requested_year}:
        raise ValueError("Treasury annual curve contains an out-of-year observation")
    existing_latest = (
        Observation.objects.filter(
            source=source,
            series__key__in=incoming_series,
            value_date__year=requested_year,
        )
        .order_by("-value_date")
        .first()
    )
    if (
        existing_latest is not None
        and max(incoming_dates) < existing_latest.value_date.date()
    ):
        raise ValueError(
            "Treasury annual curve latest date regressed behind the stored official source"
        )
    series_by_key: dict[str, SeriesDefinition] = {}
    for series_key in sorted(incoming_series):
        series_id = series_key.upper()
        series, _created = SeriesDefinition.objects.get_or_create(
            key=series_key,
            defaults={
                "name": series_id.replace("UST-", "U.S. Treasury ").replace(
                    "TIPS-", "Treasury Real "
                ),
                "unit": "%",
                "source": source,
                "frequency": "daily",
                "description": f"Imported directly from {source.name}.",
            },
        )
        series_by_key[series_key] = series

    existing = {
        (item.series.key, item.value_date.date()): item
        for item in Observation.objects.filter(
            source=source,
            series__key__in=incoming_series,
            value_date__year=requested_year,
        ).select_related("series")
    }
    fetched_at = result.fetched_at
    if timezone.is_naive(fetched_at):
        fetched_at = fetched_at.replace(tzinfo=UTC)
    now = timezone.now()
    to_create: list[Observation] = []
    to_update: list[Observation] = []
    for record in result.records:
        series_key = str(record["series_id"]).lower()
        period = date.fromisoformat(str(record["date"])[:10])
        value_date = datetime.combine(period, datetime.min.time(), tzinfo=UTC)
        item = existing.get((series_key, period))
        if item is None:
            to_create.append(
                Observation(
                    series=series_by_key[series_key],
                    instrument=None,
                    value=record["value"],
                    value_date=value_date,
                    as_of=value_date,
                    fetched_at=fetched_at,
                    batch_id=run.batch_id,
                    source=source,
                    fallback_source=None,
                    quality_status=Observation.Quality.FRESH,
                    metadata=dict(record.get("metadata") or {}),
                )
            )
            continue
        item.value = record["value"]
        item.as_of = value_date
        item.fetched_at = fetched_at
        item.batch_id = run.batch_id
        item.fallback_source = None
        item.quality_status = Observation.Quality.FRESH
        item.metadata = dict(record.get("metadata") or {})
        item.updated_at = now
        to_update.append(item)
    if to_create:
        Observation.objects.bulk_create(to_create, batch_size=1000)
    if to_update:
        Observation.objects.bulk_update(
            to_update,
            fields=[
                "value",
                "as_of",
                "fetched_at",
                "batch_id",
                "fallback_source",
                "quality_status",
                "metadata",
                "updated_at",
            ],
            batch_size=1000,
        )
    for artifact in result.metadata.get("artifacts", []):
        url = str(artifact.get("url") or "")
        digest = str(artifact.get("sha256") or "")
        if not url or not digest:
            continue
        RawArtifact.objects.create(
            run=run,
            uri=f"{url}#sha256={digest}",
            sha256=digest,
            content_type=str(
                artifact.get("content_type") or "application/octet-stream"
            ),
            size_bytes=int(artifact.get("size") or 0),
        )
    return len(to_create) + len(to_update)


def _store_h41_observations(result, source, run) -> int:
    """Backward-compatible H.4.1 persistence entry point used by tests/jobs."""

    return _store_board_archive_observations(result, source, run)


def _store_prates_observations(result, source, run) -> int:
    return _store_board_archive_observations(result, source, run)


def _store_h10_observations(result, source, run) -> int:
    return _store_board_archive_observations(result, source, run)


def _store_release_workbook_observations(result, source, run) -> int:
    """Persist normalized release rows plus immutable HTML/XLSX fingerprints."""

    for record in result.records:
        metadata = dict(record.get("metadata") or {})
        if metadata.get("calculation_owner") != "Atlas Macro":
            continue
        input_dates = list(metadata.get("input_value_dates") or [])
        input_values = list(metadata.get("input_values") or [])
        metadata["input_batch_ids"] = [str(run.batch_id)]
        input_series = list(metadata.get("input_series") or [])
        metadata["input_lineage"] = [
            {
                "series_key": str(input_series[0]).lower() if input_series else "",
                "source_key": source.key,
                "source_name": source.name,
                "value_date": str(value_date),
                "value": str(value),
                "fetched_at": result.fetched_at.isoformat(),
                "batch_id": str(run.batch_id),
                "quality_status": Observation.Quality.FRESH,
                "license_scope": source.license_scope,
                "fallback_source": None,
            }
            for value_date, value in zip(input_dates, input_values, strict=False)
        ]
        record["metadata"] = metadata

    incoming_series = {
        str(record.get("series_id") or "").lower()
        for record in result.records
        if record.get("series_id") and record.get("date")
    }
    incoming_dates = [
        datetime.fromisoformat(str(record["date"])[:10])
        for record in result.records
        if record.get("series_id") and record.get("date")
    ]
    existing_latest = (
        Observation.objects.filter(source=source, series__key__in=incoming_series)
        .order_by("-value_date")
        .first()
    )
    if (
        incoming_dates
        and existing_latest is not None
        and max(incoming_dates).date() < existing_latest.value_date.date()
    ):
        raise ValueError(
            "release latest value date regressed behind the stored official source"
        )
    row_count = store_series_observations(result, source, run)
    vintage_count = store_release_vintage_observations(result, source, run)
    if result.dataset == "gdp-release-workbooks" and vintage_count == 0:
        raise ValueError("BEA GDP release contained no persistable vintage observations")
    row_count += vintage_count
    for artifact in result.metadata.get("artifacts", []):
        url = str(artifact.get("url") or "")
        digest = str(artifact.get("sha256") or "")
        if not url or not digest:
            continue
        RawArtifact.objects.create(
            run=run,
            uri=f"{url}#sha256={digest}",
            sha256=digest,
            content_type=str(artifact.get("content_type") or "application/octet-stream"),
            size_bytes=int(artifact.get("size") or 0),
        )
    return row_count


def _record_census_revision_witness(runs: Iterable[IngestionRun]) -> None:
    """Record, but never promote, differences from the older release workbook."""

    run_by_source = {run.source.key: run for run in runs}
    current_run = run_by_source.get("census")
    legacy_run = run_by_source.get("census-release")
    if (
        current_run is None
        or legacy_run is None
        or current_run.status != IngestionRun.Status.SUCCESS
        or legacy_run.status != IngestionRun.Status.SUCCESS
    ):
        return
    series_keys = {
        "census-mrts-44x72-sm-sa",
        "census-mrts-44x72-sm-sa-mom",
        "census-mrts-44x72-sm-sa-yoy",
    }
    current = {
        (item.series.key, item.value_date.date().isoformat()): item.value
        for item in Observation.objects.filter(
            source__key="census",
            batch_id=current_run.batch_id,
            series__key__in=series_keys,
        ).select_related("series")
    }
    legacy = {
        (item.series.key, item.value_date.date().isoformat()): item.value
        for item in Observation.objects.filter(
            source__key="census-release",
            batch_id=legacy_run.batch_id,
            series__key__in=series_keys,
        ).select_related("series")
    }
    overlap = sorted(set(current) & set(legacy))
    differences = [
        {
            "series_key": key[0],
            "value_date": key[1],
            "legacy_value": str(legacy[key]),
            "current_value": str(current[key]),
            "revision_delta": str(current[key] - legacy[key]),
        }
        for key in overlap
        if current[key] != legacy[key]
    ]
    metadata = dict(current_run.metadata or {})
    metadata["legacy_revision_witness"] = {
        "source": "census-release",
        "batch_id": str(legacy_run.batch_id),
        "latest_value_date": max((key[1] for key in legacy), default=None),
        "overlap_count": len(overlap),
        "differences": differences,
        "policy": (
            "Older MARTS workbooks are immutable revision witnesses; "
            "the current API batch always controls public observations."
        ),
    }
    current_run.metadata = metadata
    current_run.save(update_fields=["metadata", "updated_at"])


def _payload_source_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys = {str(value["source_key"])} if value.get("source_key") else set()
        for fallback_field in ("fallback_source", "fallback_source_key"):
            if value.get(fallback_field):
                keys.add(str(value[fallback_field]))
        keys.update(str(item) for item in value.get("source_keys", []) if item)
        keys.update(str(item) for item in value.get("_source_keys", []) if item)
        for nested in value.values():
            keys.update(_payload_source_keys(nested))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for nested in value:
            keys.update(_payload_source_keys(nested))
        return keys
    return set()


def _payload_fallback_source_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        keys = {
            str(value[field])
            for field in ("fallback_source", "fallback_source_key")
            if value.get(field)
        }
        for nested in value.values():
            keys.update(_payload_fallback_source_keys(nested))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for nested in value:
            keys.update(_payload_fallback_source_keys(nested))
        return keys
    return set()


def _payload_batch_ids(value: Any) -> set[str]:
    def normalized(raw: Any) -> set[str]:
        return {
            item.strip()
            for item in str(raw or "").split(",")
            if item.strip()
        }

    if isinstance(value, dict):
        batches = normalized(value.get("batch_id"))
        for item in value.get("batch_ids", []):
            batches.update(normalized(item))
        for nested in value.values():
            batches.update(_payload_batch_ids(nested))
        return batches
    if isinstance(value, list):
        batches: set[str] = set()
        for nested in value:
            batches.update(_payload_batch_ids(nested))
        return batches
    return set()


def _publish_dashboard(
    *,
    key: str,
    title: str,
    summary: str,
    metrics: list[dict[str, Any]],
    chart_data: Any = None,
    charts: list[dict[str, Any]] | None = None,
    sections: list[dict[str, Any]] | None = None,
    extra_data: dict[str, Any] | None = None,
    required_metric_keys: frozenset[str] | None = None,
    batch_id: uuid.UUID,
) -> DashboardSnapshot | None:
    if not metrics:
        return None
    source = ensure_source("internal")
    metric_source_keys = {
        str(item.get("source_key") or "internal")
        for item in metrics
        if isinstance(item, dict)
    }
    source_scopes = {
        item.key: item.license_scope[:120]
        for item in Source.objects.filter(key__in=metric_source_keys)
    }
    normalized_metrics: list[dict[str, Any]] = []
    for raw_metric in metrics:
        metric = deepcopy(raw_metric)
        declared_source_key = str(metric.get("source_key") or "")
        if not declared_source_key:
            raise ValueError("dashboard metric lacks an explicit source key")
        metric_source_key = declared_source_key
        if metric_source_key not in source_scopes:
            raise ValueError(
                f"dashboard metric declares unknown source key: {metric_source_key}"
            )
        metric["source_key"] = metric_source_key
        metric["license_scope"] = source_scopes.get(
            metric_source_key,
            source.license_scope[:120],
        )
        normalized_metrics.append(metric)
    metrics = normalized_metrics
    if required_metric_keys and not required_metric_keys <= {
        str(item.get("key") or "") for item in metrics
    }:
        return None

    normalized_charts = [dict(item) for item in charts or [] if item]
    if not normalized_charts:
        inherited_source_keys = _payload_source_keys(chart_data or [])
        if not inherited_source_keys:
            inherited_source_keys = _payload_source_keys(metrics)
        metric_qualities = {item.get("quality_status") for item in metrics}
        if Observation.Quality.ERROR in metric_qualities:
            inherited_quality = Observation.Quality.ERROR
        elif Observation.Quality.STALE in metric_qualities:
            inherited_quality = Observation.Quality.STALE
        elif Observation.Quality.FALLBACK in metric_qualities:
            inherited_quality = Observation.Quality.FALLBACK
        elif metric_qualities == {Observation.Quality.FRESH}:
            inherited_quality = Observation.Quality.FRESH
        else:
            inherited_quality = Observation.Quality.ESTIMATED
        normalized_charts = [
            {
                "key": "primary",
                "title": "核心趋势",
                "description": "",
                "kind": "line",
                "data": chart_data or [],
                "source_keys": sorted(inherited_source_keys),
                "as_of": min(
                    (item["as_of"] for item in metrics if item.get("as_of")),
                    default=None,
                ),
                "fetched_at": max(
                    (item["fetched_at"] for item in metrics if item.get("fetched_at")),
                    default=None,
                ),
                "fresh_until": min(
                    (item["fresh_until"] for item in metrics if item.get("fresh_until")),
                    default=None,
                ),
                "quality_status": inherited_quality,
                "batch_ids": sorted(_payload_batch_ids(metrics)),
            }
        ]
    for chart in normalized_charts:
        chart.setdefault("data", [])
        chart.setdefault("kind", "line")
        chart.setdefault("title", "趋势")
        chart_source_keys = set(chart.get("source_keys", [])) | _payload_source_keys(
            chart.get("data", [])
        )
        chart["source_keys"] = sorted(chart_source_keys)
        chart_sources = {
            item.key: item
            for item in Source.objects.filter(key__in=chart_source_keys)
        }
        chart["license_scopes"] = [
            f"{chart_sources[key].name}: {chart_sources[key].license_scope}"
            for key in sorted(chart_sources)
        ]
        chart["fallback_sources"] = sorted(
            _payload_fallback_source_keys(chart)
        )

    as_of_values = [
        datetime.fromisoformat(item["as_of"])
        for item in [*metrics, *normalized_charts]
        if item.get("as_of")
    ]
    as_of = min(as_of_values) if as_of_values else timezone.now()
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    component_qualities = {
        item.get("quality_status") for item in [*metrics, *normalized_charts]
    }
    if Observation.Quality.ERROR in component_qualities:
        quality = Observation.Quality.ERROR
    elif Observation.Quality.STALE in component_qualities:
        quality = Observation.Quality.STALE
    elif component_qualities == {Observation.Quality.FRESH}:
        quality = Observation.Quality.FRESH
    else:
        quality = Observation.Quality.ESTIMATED
    component_batches = sorted(
        _payload_batch_ids([metrics, normalized_charts, sections or []])
    )
    source_keys = sorted(
        _payload_source_keys([metrics, normalized_charts, sections or []])
    )
    normalized_extra_data = deepcopy(extra_data or {})
    reserved_extra_keys = {
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
        "fingerprint",
        "refresh_failure",
    }
    if reserved_extra_keys & normalized_extra_data.keys():
        raise ValueError("dashboard extra_data attempted to replace reserved fields")
    snapshot_data = {
        "demo": False,
        "metrics": metrics,
        "charts": normalized_charts,
        "chart_data": normalized_charts[0]["data"],
        "sections": sections or [],
        "component_batches": component_batches,
        "source_keys": source_keys,
        "required_notices": public_source_notices(source_keys),
        "fresh_until": min(
            (
                item["fresh_until"]
                for item in [*metrics, *normalized_charts, *(sections or [])]
                if item.get("fresh_until")
            ),
            default=None,
        ),
        "publication_batch_id": str(batch_id),
        **normalized_extra_data,
    }
    fingerprint_payload = {
        "title": title,
        "summary": summary,
        **snapshot_data,
    }

    def without_volatile_lineage(value: Any) -> Any:
        """Keep the content fingerprint stable across an unchanged re-fetch."""

        if isinstance(value, dict):
            return {
                item_key: without_volatile_lineage(item_value)
                for item_key, item_value in value.items()
                if item_key
                not in {
                    "batch_id",
                    "batch_ids",
                    "_batch_ids",
                    "input_batch_ids",
                    "component_batch_id",
                    "component_batches",
                    "fetched_at",
                    "fingerprint",
                    "fresh_until",
                    "publication_batch_id",
                    "required_notices",
                    "as_of",
                    "component_snapshots",
                    "component_snapshot_id",
                    "component_snapshot_batch_id",
                    "component_snapshot_fingerprint",
                    "component_publication_batch_id",
                    "component_fingerprint",
                    "component_metric_snapshot_id",
                    "component_metric_snapshot_batch_id",
                    "refresh_cycle_id",
                }
            }
        if isinstance(value, list):
            return [without_volatile_lineage(item) for item in value]
        return value

    fingerprint = hashlib.sha256(
        json.dumps(
            without_volatile_lineage(fingerprint_payload),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    snapshot_data["fingerprint"] = fingerprint

    def metric_metadata(item: dict[str, Any]) -> dict[str, Any]:
        component_metadata = item.get("metadata") or {}
        return {
            "component_batch_id": item.get("batch_id"),
            "formula": component_metadata.get("formula"),
            "source_field": component_metadata.get("source_field"),
            "common_effective_date": component_metadata.get(
                "common_effective_date"
            ),
            "input_series": component_metadata.get("input_series", []),
            "source_keys": item.get("source_keys", []),
            "input_batch_ids": component_metadata.get(
                "input_batch_ids", []
            ),
            "input_value_dates": component_metadata.get(
                "input_value_dates", []
            ),
            "input_lineage": component_metadata.get("input_lineage", []),
            "previous_value": component_metadata.get("previous_value"),
            "previous_value_date": component_metadata.get(
                "previous_value_date"
            ),
            "previous_input_lineage": component_metadata.get(
                "previous_input_lineage", []
            ),
            "model_label": component_metadata.get("model_label"),
            "freshness_basis": component_metadata.get("freshness_basis"),
            "seasonal_basis": component_metadata.get("seasonal_basis"),
            "preliminary": bool(component_metadata.get("preliminary")),
            "revision_indicator": component_metadata.get(
                "revision_indicator"
            ),
            "footnote_id": component_metadata.get("footnote_id"),
            "prates_status": component_metadata.get("prates_status"),
            "calculation_owner": component_metadata.get(
                "calculation_owner"
            ),
            "component_page_key": component_metadata.get(
                "component_page_key"
            ),
            "component_snapshot_id": component_metadata.get(
                "component_snapshot_id"
            ),
            "component_publication_batch_id": component_metadata.get(
                "component_publication_batch_id"
            ),
            "component_fingerprint": component_metadata.get(
                "component_fingerprint"
            ),
            "component_metric_snapshot_id": component_metadata.get(
                "component_metric_snapshot_id"
            ),
            "component_metric_snapshot_key": component_metadata.get(
                "component_metric_snapshot_key"
            ),
            "component_metric_snapshot_batch_id": component_metadata.get(
                "component_metric_snapshot_batch_id"
            ),
            "inherited_license_scope": item.get("license_scope")
            or component_metadata.get("inherited_license_scope"),
            "public_snapshot": True,
        }

    def parsed_datetime(raw_value: Any, fallback: datetime) -> datetime:
        if not raw_value:
            return fallback
        parsed = datetime.fromisoformat(str(raw_value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    def store_metric(item: dict[str, Any], metric_batch_id: uuid.UUID) -> None:
        item_value = item.get("value")
        if item_value is None:
            return
        component_source = (
            Source.objects.filter(key=item.get("source_key", "")).first()
            or source
        )
        component_fallback_source = Source.objects.filter(
            key=item.get("fallback_source", "")
        ).first()
        value_date = parsed_datetime(
            item.get("value_date") or item.get("as_of"), as_of
        )
        item_as_of = parsed_datetime(item.get("as_of"), value_date)
        fetched_at = parsed_datetime(item.get("fetched_at"), timezone.now())
        MetricSnapshot.objects.update_or_create(
            key=f"{key}-{item.get('key', item['label']).lower()}",
            batch_id=metric_batch_id,
            defaults={
                "label": item["label"],
                "value": Decimal(str(item_value)),
                "display_value": item.get("display_value", ""),
                "change": (
                    Decimal(str(item["change"]))
                    if item.get("change") is not None
                    else None
                ),
                "unit": item.get("unit", ""),
                "value_date": value_date,
                "as_of": item_as_of,
                "fetched_at": fetched_at,
                "source": component_source,
                "fallback_source": component_fallback_source,
                "quality_status": item.get(
                    "quality_status", Observation.Quality.FRESH
                ),
                "license_scope": component_source.license_scope[:120],
                "metadata": metric_metadata(item),
            },
        )

    latest_query = DashboardSnapshot.objects.filter(key=key, is_published=True)
    if normalized_extra_data.get("contract_version") is not None:
        latest_query = latest_query.filter(
            data__contract_version=normalized_extra_data["contract_version"]
        )
    latest = (
        latest_query.exclude(source__key="demo-market")
        .order_by("-created_at")
        .first()
    )
    if latest and latest.data.get("fingerprint") == fingerprint:
        snapshot_data["publication_batch_id"] = str(latest.batch_id)
        latest.data = snapshot_data
        latest.as_of = as_of
        latest.quality_status = quality
        latest.save(
            update_fields=["data", "as_of", "quality_status", "updated_at"]
        )
        for item in metrics:
            store_metric(item, latest.batch_id)
        return None
    for item in metrics:
        store_metric(item, batch_id)
    return DashboardSnapshot.objects.create(
        key=key,
        title=title,
        as_of=as_of,
        batch_id=batch_id,
        quality_status=quality,
        summary=summary,
        data=snapshot_data,
        source=source,
        is_published=True,
    )


def _latest_successful_source_batch(source_key: str) -> uuid.UUID | None:
    run = (
        IngestionRun.objects.filter(
            source__key=source_key,
            status=IngestionRun.Status.SUCCESS,
            row_count__gt=0,
        )
        .order_by("-completed_at", "-id")
        .first()
    )
    return run.batch_id if run is not None else None


def publish_official_dashboards(
    *,
    keys: Iterable[str] | None = None,
    source_batches: dict[str, uuid.UUID | str] | None = None,
    dataset_batches: dict[str, uuid.UUID | str] | None = None,
    prepared_fed_funds_data: tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]
    | None = None,
    prepared_economy_data: tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None = None,
    prepared_liquidity_data: tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]
    | None = None,
    prepared_treasury_curve_data: dict[
        str,
        tuple[
            list[dict[str, Any]],
            list[dict[str, Any]],
            list[dict[str, Any]],
            dict[str, Any],
        ],
    ]
    | None = None,
) -> list[DashboardSnapshot]:
    """Atomically publish only the dashboards affected by a completed source batch."""

    batch_id = uuid.uuid4()
    selected_keys = set(keys) if keys is not None else None
    if selected_keys is not None:
        selected_keys -= INDEPENDENT_PUBLICATION_KEYS
        if not selected_keys:
            return []
    normalized_source_batches = dict(source_batches or {})
    normalized_dataset_batches = dict(dataset_batches or {})
    treasury_prepared = dict(prepared_treasury_curve_data or {})
    rates_prepared = treasury_prepared.get("rates", ([], [], [], {}))
    yield_curve_prepared = treasury_prepared.get(
        "yield-curve", ([], [], [], {})
    )
    real_rates_prepared = treasury_prepared.get(
        "real-rates", ([], [], [], {})
    )
    if source_batches is None and (
        selected_keys is None or "inflation" in selected_keys
    ):
        latest_bls_batch = _latest_successful_source_batch("bls")
        if latest_bls_batch is not None:
            normalized_source_batches["bls"] = latest_bls_batch
    if source_batches is None and (
        selected_keys is None or "consumer" in selected_keys
    ):
        latest_census_release_batch = _latest_successful_source_batch("census-release")
        if latest_census_release_batch is not None:
            normalized_source_batches["census-release"] = latest_census_release_batch
    hqm_curve = _curve_rows("hqm-par", ("2y", "5y", "10y", "30y"))
    sofr_market_metrics = _sofr_market_metrics()
    consumer_metrics: list[dict[str, Any]] = []
    consumer_charts: list[dict[str, Any]] = []
    employment_metrics: list[dict[str, Any]] = []
    employment_charts: list[dict[str, Any]] = []
    employment_sections: list[dict[str, Any]] = []
    inflation_metrics: list[dict[str, Any]] = []
    inflation_charts: list[dict[str, Any]] = []
    inflation_sections: list[dict[str, Any]] = []
    fed_funds_metrics: list[dict[str, Any]] = []
    fed_funds_charts: list[dict[str, Any]] = []
    fed_funds_sections: list[dict[str, Any]] = []
    economy_metrics: list[dict[str, Any]] = []
    economy_charts: list[dict[str, Any]] = []
    economy_sections: list[dict[str, Any]] = []
    economy_extra_data: dict[str, Any] = {}
    liquidity_metrics: list[dict[str, Any]] = []
    liquidity_charts: list[dict[str, Any]] = []
    liquidity_sections: list[dict[str, Any]] = []
    liquidity_extra_data: dict[str, Any] = {}
    retail_source_key = "census-release"
    retail_batch = normalized_source_batches.get(retail_source_key)
    gdp_vintage_chart: dict[str, Any] | None = None
    gdp_vintage_section: dict[str, Any] | None = None
    if selected_keys is None or "gdp" in selected_keys:
        gdp_vintage_chart, gdp_vintage_section = _gdp_vintage_chart_and_section()
    if selected_keys is None or "consumer" in selected_keys:
        consumer_metrics = _existing(
            (
                _metric(
                    "CENSUS-MRTS-44X72-SM-SA",
                    "零售与餐饮服务",
                    decimals=0,
                    suffix=" USD mn",
                    source_key=retail_source_key,
                    batch_id=retail_batch,
                )
                if retail_batch is not None
                else None
            ),
            (
                _metric(
                    "CENSUS-MRTS-44X72-SM-SA-MOM",
                    "零售环比",
                    suffix="%",
                    source_key=retail_source_key,
                    batch_id=retail_batch,
                )
                if retail_batch is not None
                else None
            ),
            (
                _metric(
                    "CENSUS-MRTS-44X72-SM-SA-YOY",
                    "零售同比",
                    suffix="%",
                    source_key=retail_source_key,
                    batch_id=retail_batch,
                )
                if retail_batch is not None
                else None
            ),
            _metric("BEA-REAL-PCE-MOM", "实际 PCE 环比", suffix="%"),
            _metric("BEA-PERSONAL-SAVING-RATE", "个人储蓄率", suffix="%"),
            _metric("BEA-REAL-DPI-MOM", "实际可支配收入环比", suffix="%"),
            _metric(
                "G19-CONSUMER-CREDIT-OUTSTANDING-SA",
                "G.19 消费者信贷余额",
                scale=Decimal("0.000001"),
                suffix=" USD tn",
            ),
            _metric(
                "G19-CONSUMER-CREDIT-GROWTH-SAAR",
                "G.19 信贷增速",
                suffix="%",
            ),
            _metric(
                "G19-REVOLVING-CREDIT-GROWTH-SAAR",
                "循环信贷增速",
                suffix="%",
            ),
            _metric(
                "G19-NONREVOLVING-CREDIT-GROWTH-SAAR",
                "非循环信贷增速",
                suffix="%",
            ),
            _metric("HHDC-TOTAL-DEBT-BALANCE", "家庭债务余额", suffix=" USD tn"),
            _metric("HHDC-CREDIT-CARD-BALANCE", "信用卡余额", suffix=" USD tn"),
            _metric("HHDC-ALL-90D-DELINQUENT", "全部债务 90+ 天逾期", suffix="%"),
            _metric(
                "HHDC-CREDIT-CARD-90D-DELINQUENT",
                "信用卡 90+ 天逾期",
                suffix="%",
            ),
        )
        consumer_charts = [
            chart
            for chart in (
                _history_chart(
                    key="retail-sales",
                    title="零售与餐饮服务销售",
                    description="季调月度水平，单位：百万美元",
                    series={"CENSUS-MRTS-44X72-SM-SA": "零售与餐饮服务"},
                    limit=36,
                    source_key=retail_source_key,
                    batch_id=retail_batch,
                ),
                _history_chart(
                    key="real-consumption-income-momentum",
                    title="实际消费与收入动能",
                    description="实际 PCE 与实际可支配收入月环比，单位：%",
                    series={
                        "BEA-REAL-PCE-MOM": "实际 PCE 环比",
                        "BEA-REAL-DPI-MOM": "实际 DPI 环比",
                    },
                    limit=120,
                ),
                _history_chart(
                    key="personal-saving-rate",
                    title="个人储蓄率",
                    description="个人储蓄占可支配个人收入，单位：%",
                    series={"BEA-PERSONAL-SAVING-RATE": "个人储蓄率"},
                    limit=120,
                ),
                _history_chart(
                    key="consumer-credit-composition",
                    title="G.19 消费者信贷结构",
                    description=(
                        "季调月度余额，单位：百万美元；"
                        "不含以房地产抵押的贷款"
                    ),
                    series={
                        "G19-REVOLVING-CREDIT-OUTSTANDING-SA": "循环信贷",
                        "G19-NONREVOLVING-CREDIT-OUTSTANDING-SA": "非循环信贷",
                    },
                    limit=120,
                ),
                _history_chart(
                    key="household-debt-composition",
                    title="家庭债务结构",
                    description=(
                        "纽约联储 Consumer Credit Panel / Equifax 季度数据，"
                        "单位：万亿美元"
                    ),
                    series={
                        "HHDC-MORTGAGE-BALANCE": "抵押贷款",
                        "HHDC-HELOC-BALANCE": "HELOC",
                        "HHDC-AUTO-LOAN-BALANCE": "汽车贷款",
                        "HHDC-CREDIT-CARD-BALANCE": "信用卡",
                        "HHDC-STUDENT-LOAN-BALANCE": "学生贷款",
                    },
                    limit=96,
                ),
                _history_chart(
                    key="household-debt-delinquency",
                    title="90+ 天严重逾期率",
                    description="占各类债务余额的比例，单位：%",
                    series={
                        "HHDC-ALL-90D-DELINQUENT": "全部债务",
                        "HHDC-CREDIT-CARD-90D-DELINQUENT": "信用卡",
                        "HHDC-AUTO-90D-DELINQUENT": "汽车贷款",
                        "HHDC-MORTGAGE-90D-DELINQUENT": "抵押贷款",
                    },
                    limit=96,
                ),
            )
            if chart is not None
        ]
    if selected_keys is None or "employment" in selected_keys:
        (
            employment_metrics,
            employment_charts,
            employment_sections,
        ) = _employment_page_data()
    if selected_keys is None or "inflation" in selected_keys:
        (
            inflation_metrics,
            inflation_charts,
            inflation_sections,
        ) = _inflation_page_data(
            batch_id=normalized_source_batches.get("bls"),
            bea_pio_batch_id=normalized_source_batches.get("bea-pio-release"),
        )
    if selected_keys is None or "fed-funds" in selected_keys:
        if prepared_fed_funds_data is not None:
            (
                fed_funds_metrics,
                fed_funds_charts,
                fed_funds_sections,
            ) = prepared_fed_funds_data
        elif dataset_batches:
            (
                fed_funds_metrics,
                fed_funds_charts,
                fed_funds_sections,
            ) = _fed_funds_page_data(
                dataset_batches=normalized_dataset_batches
            )
    if (
        (selected_keys is None or "economy" in selected_keys)
        and prepared_economy_data is not None
    ):
        (
            economy_metrics,
            economy_charts,
            economy_sections,
            economy_extra_data,
        ) = prepared_economy_data
    if (
        (selected_keys is None or "liquidity" in selected_keys)
        and prepared_liquidity_data is not None
    ):
        (
            liquidity_metrics,
            liquidity_charts,
            liquidity_sections,
            liquidity_extra_data,
        ) = prepared_liquidity_data
    dashboards: list[DashboardSnapshot] = []
    definitions = [
        {
            "key": "liquidity",
            "title": "流动性",
            "summary": (
                "WALCL、ON RRP 与 TGA 只在最新非未来共同有效日计算净流动性代理；"
                "该指标是 Atlas Macro 透明计算，不是美联储官方 LPI。政策利率组件继承"
                "已通过原子契约的 Fed Funds 快照。"
            ),
            "metrics": liquidity_metrics,
            "charts": liquidity_charts,
            "sections": liquidity_sections,
            "extra_data": liquidity_extra_data,
            "required_metric_keys": LIQUIDITY_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "transmission-chain",
            "title": "美元流动性传导链",
            "summary": "先显示美国财政与 Repo 官方输入；离岸基差、中介能力与资产反应缺口见下方台账，不发布伪精确总分。",
            "metrics": _existing(
                _metric("WRBWFRBL", "准备金", scale=Decimal("0.000001"), suffix=" USD tn"),
                _metric("TGA", "TGA", scale=Decimal("0.001"), suffix=" USD bn"),
                _metric(
                    "ONRRP", "ON RRP", decimals=3, scale=Decimal("0.001"), suffix=" USD bn"
                ),
                _metric(
                    "SOFR", "SOFR", suffix="%", aligned_with=("EFFR", "IORB")
                ),
                _metric(
                    "IORB", "IORB", suffix="%", aligned_with=("SOFR", "EFFR")
                ),
                _derived_metric("sofr-effr", "SOFR−EFFR", "SOFR", "EFFR", basis_points=True),
                _derived_metric("sofr-iorb", "SOFR−IORB", "SOFR", "IORB", basis_points=True),
                _metric(
                    "FXSWAP-USD-OUTSTANDING",
                    "央行美元互换",
                    decimals=0,
                    suffix=" USD mn",
                ),
            ),
            "chart_data": _history_rows(
                {"SOFR": "SOFR", "EFFR": "EFFR", "IORB": "IORB"},
                require_all=True,
            ),
        },
        {
            "key": "fed-balance-sheet",
            "title": "美联储资产负债表",
            "summary": "六个核心序列直接解析 Federal Reserve H.4.1 DDP；周三观察、周四发布，保留每次 ZIP 哈希。",
            "metrics": _existing(
                _metric("WALCL", "总资产", scale=Decimal("0.000001"), suffix=" USD tn"),
                _metric("WSHOTSL", "美债持有", scale=Decimal("0.000001"), suffix=" USD tn"),
                _metric("WSHOMCB", "MBS 持有", scale=Decimal("0.000001"), suffix=" USD tn"),
                _metric("WRBWFRBL", "准备金", scale=Decimal("0.000001"), suffix=" USD tn"),
            ),
            "chart_data": _history_rows(
                {
                    "WALCL": "总资产",
                    "WSHOTSL": "美债",
                    "WSHOMCB": "MBS",
                    "WRBWFRBL": "准备金",
                },
                limit=104,
            ),
        },
        {
            "key": "operations",
            "title": "公开市场操作",
            "summary": "ON RRP 和常备回购按操作日聚合，常备回购合并早午两场；SOMA 为周三国内证券持仓，不等于 H.4.1 总资产。",
            "metrics": _existing(
                _metric(
                    "ONRRP", "ON RRP", decimals=3, scale=Decimal("0.001"), suffix=" USD bn"
                ),
                _metric("SRP", "常备回购", decimals=0, suffix=" USD mn"),
                _metric("SRP-RATE", "常备回购利率", suffix="%"),
                _metric("SOMA-TOTAL", "SOMA", scale=Decimal("0.000001"), suffix=" USD tn"),
            ),
            "chart_data": _history_rows({"SOMA-TOTAL": "SOMA"}, limit=104),
        },
        {
            "key": "fed-funds",
            "title": "联邦基金利率",
            "summary": (
                "SOFR、EFFR、IORB 与政策目标区间严格对齐到最新非未来共同"
                "有效日；目标上下限、分位和成交量直接来自 NY Fed，IORB 直接"
                "来自 Federal Reserve PRATES，所有差值保留三数据集批次血缘。"
            ),
            "metrics": fed_funds_metrics,
            "charts": fed_funds_charts,
            "sections": fed_funds_sections,
            "required_metric_keys": FED_FUNDS_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "rates",
            "title": "利率",
            "summary": (
                "政策利率继承已验证的 Fed Funds 原子快照；国债、实际利率与"
                "盈亏平衡通胀继承同一 Treasury curve v1 合同。"
            ),
            "metrics": rates_prepared[0],
            "charts": rates_prepared[1],
            "sections": rates_prepared[2],
            "extra_data": rates_prepared[3],
        },
        {
            "key": "assets-fx",
            "title": "外汇",
            "summary": "日频参考值直接来自 Federal Reserve H.10；广义美元指数不是 ICE DXY，参考汇率也不是可交易实时现货或远期报价。",
            "metrics": _existing(
                _metric("H10-BROAD-DOLLAR", "广义美元指数", decimals=2),
                _metric("H10-EURUSD", "EUR/USD 参考汇率", decimals=4),
                _metric("H10-USDCNY", "USD/CNY 参考汇率", decimals=4),
                _metric("H10-USDJPY", "USD/JPY 参考汇率", decimals=4),
            ),
            "chart_data": _history_rows(
                {
                    "H10-BROAD-DOLLAR": "广义美元指数",
                    "H10-EURUSD": "EUR/USD",
                },
                limit=120,
            ),
        },
        {
            "key": "yield-curve",
            "title": "收益率曲线",
            "summary": (
                "财政部名义 Par Yield 曲线按精确年度批次组合；当前、1 周、"
                "1 月与 3 月比较和关键利差均保留可复算血缘。"
            ),
            "metrics": yield_curve_prepared[0],
            "charts": yield_curve_prepared[1],
            "sections": yield_curve_prepared[2],
            "extra_data": yield_curve_prepared[3],
            "required_metric_keys": YIELD_CURVE_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "real-rates",
            "title": "实际利率",
            "summary": (
                "TIPS 实际 Par Yield 直接来自财政部；BEI 为 Atlas Macro 用同期限"
                "名义减实际的透明近似，不冒充官方 5Y5Y。"
            ),
            "metrics": real_rates_prepared[0],
            "charts": real_rates_prepared[1],
            "sections": real_rates_prepared[2],
            "extra_data": real_rates_prepared[3],
            "required_metric_keys": REAL_RATES_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "credit",
            "title": "信用市场",
            "summary": "免费版发布 Treasury HQM 高质量企业债收益率与 Fed SLOOS 贷款标准；它们是信用环境代理，不是 ICE OAS 或 CDS。",
            "metrics": _existing(
                _metric("HQM-PAR-10Y", "HQM 10Y", suffix="%"),
                _metric("HQM-PAR-30Y", "HQM 30Y", suffix="%"),
                _metric("SUBLPDMBS_XWB_N.Q", "企业贷款标准", suffix="%"),
                _metric("SUBLPDMBD_XWB_N.Q", "企业贷款需求", suffix="%"),
            ),
            "chart_data": _history_rows(
                {
                    "SUBLPDMBS_XWB_N.Q": "贷款标准",
                    "SUBLPDMBD_XWB_N.Q": "贷款需求",
                },
                limit=24,
            ),
        },
        {
            "key": "credit-spreads",
            "title": "信用利差与收益率代理",
            "summary": "Treasury HQM 是高质量企业债月均 par yield 曲线，不含国债利差、不是 OAS；ICE 分评级 OAS 仍需商业许可。",
            "metrics": _existing(
                _metric("HQM-PAR-2Y", "HQM 2Y", suffix="%"),
                _metric("HQM-PAR-5Y", "HQM 5Y", suffix="%"),
                _metric("HQM-PAR-10Y", "HQM 10Y", suffix="%"),
                _metric("HQM-PAR-30Y", "HQM 30Y", suffix="%"),
            ),
            "chart_data": [
                {"label": item["label"], "HQM par yield": item["value"]}
                for item in hqm_curve
            ],
            "sections": [
                {
                    "title": "Treasury HQM 月均高质量企业债曲线（代理）",
                    "rows": hqm_curve,
                    "fresh_until": _earliest_fresh_until(hqm_curve),
                    "status": "estimated",
                }
            ],
        },
        {
            "key": "credit-stress",
            "title": "信用压力仪表盘",
            "summary": "SLOOS 为季度银行调查：正值贷款标准表示净收紧，需求项按 Board 原始口径展示。尚未将其合成为自有压力总分。",
            "metrics": _existing(
                _metric("SUBLPDMBS_XWB_N.Q", "企业贷款标准", suffix="%"),
                _metric("SUBLPDMBD_XWB_N.Q", "企业贷款需求", suffix="%"),
                _metric("SUBLPDCILS_N.Q", "大中企业 C&I 标准", suffix="%"),
                _metric("SUBLPDCISS_N.Q", "小企业 C&I 标准", suffix="%"),
            ),
            "chart_data": _history_rows(
                {
                    "SUBLPDMBS_XWB_N.Q": "贷款标准",
                    "SUBLPDMBD_XWB_N.Q": "贷款需求",
                },
                limit=24,
            ),
        },
        {
            "key": "reserves",
            "title": "银行准备金",
            "summary": "准备金余额直接来自 Federal Reserve H.4.1；银行资产占比与充裕度阈值在分母及方法完成前保持空缺。",
            "metrics": _existing(
                _metric("WRBWFRBL", "准备金", scale=Decimal("0.000001"), suffix=" USD tn")
            ),
            "chart_data": _history_rows({"WRBWFRBL": "准备金"}, limit=156),
        },
        {
            "key": "global-dollar",
            "title": "全球美元",
            "summary": "央行美元互换按 settlementDate ≤ as_of < maturityDate 计算在途余额，技术测试单列；跨币种基差仍需授权数据。",
            "metrics": _existing(
                _metric(
                    "FXSWAP-USD-OUTSTANDING",
                    "央行美元互换",
                    decimals=0,
                    suffix=" USD mn",
                ),
                _metric(
                    "FXSWAP-USD-OUTSTANDING-SMALL-VALUE",
                    "其中技术测试",
                    decimals=0,
                    suffix=" USD mn",
                ),
            ),
            "chart_data": _history_rows({"FXSWAP-USD-OUTSTANDING": "在途互换"}, limit=120),
        },
        {
            "key": "subsurface",
            "title": "次表层资金流",
            "summary": "SOFR 尾分位、成交量与常备回购取纽约联储底层数据，IORB 取 Federal Reserve PRATES；早午两场按日合并，小额技术测试不解读为压力。",
            "metrics": _existing(
                *sofr_market_metrics,
                _metric("IORB", "IORB", suffix="%", aligned_with=("SOFR",)),
                _derived_metric("sofr-iorb", "SOFR−IORB", "SOFR", "IORB", basis_points=True),
                _metric("SRP", "常备回购", decimals=0, suffix=" USD mn"),
                _metric("SRP-RATE", "常备回购利率", suffix="%"),
            ),
            "chart_data": _sofr_market_history(),
        },
        {
            "key": "economy",
            "title": "经济数据",
            "summary": (
                "实际 GDP 季调年化增速、失业率、核心 CPI 同比与实际 PCE "
                "环比分别继承 GDP、就业、通胀和消费官方子页的有效日、"
                "抓取时间、公式、许可、质量及输入批次；不同频率不强行"
                "对齐，任一被选组件失效时保留上一版完整总览。"
            ),
            "metrics": economy_metrics,
            "charts": economy_charts,
            "sections": economy_sections,
            "extra_data": economy_extra_data,
            "required_metric_keys": ECONOMY_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "gdp",
            "title": "GDP 与增长",
            "summary": "实际 GDP、GDI、PCE、分项增速与贡献均来自 BEA 官方发布工作簿；当前指标取每季度最新轮次，独立 vintage 数据层同时保留 Advance、Second、Third 与后续 Revised 的完整修订路径。",
            "metrics": _existing(
                _metric("BEA-A191RL", "实际 GDP 增速", suffix="%"),
                _metric("BEA-DPCERL", "实际 PCE 增速", suffix="%"),
                _metric("BEA-GDP-NOMINAL-SAAR", "名义 GDP", decimals=1, suffix=" USD bn"),
                _metric("BEA-GDI-REAL-GROWTH-SAAR", "实际 GDI 增速", suffix="%"),
                _metric("BEA-PCE-GOODS-GROWTH", "商品消费增速", suffix="%"),
                _metric("BEA-PCE-SERVICES-GROWTH", "服务消费增速", suffix="%"),
                _metric("BEA-GPDI-GROWTH", "私人国内投资增速", suffix="%"),
                _metric("BEA-PCE-CONTRIBUTION", "消费贡献", suffix="pp"),
                _metric("BEA-GPDI-CONTRIBUTION", "投资贡献", suffix="pp"),
                _metric(
                    "BEA-NET-EXPORTS-CONTRIBUTION",
                    "净出口贡献",
                    suffix="pp",
                ),
                _metric("BEA-GOVERNMENT-CONTRIBUTION", "政府贡献", suffix="pp"),
            ),
            "charts": _existing(
                _history_chart(
                    key="gdp-growth-history",
                    title="实际 GDP 与实际 PCE 增速",
                    description="季调年化环比，单位：%。",
                    series={
                        "BEA-A191RL": "实际 GDP",
                        "BEA-DPCERL": "实际 PCE",
                    },
                    limit=24,
                )
                or _history_chart(
                    key="gdp-growth-history",
                    title="实际 GDP 增速",
                    description="季调年化环比，单位：%。",
                    series={"BEA-A191RL": "实际 GDP"},
                    limit=24,
                ),
                gdp_vintage_chart,
            ),
            "sections": _existing(gdp_vintage_section),
        },
        {
            "key": "employment",
            "title": "就业",
            "summary": (
                "BLS 非农、家庭调查与 JOLTS 和 DOL 周度受保失业申领"
                "分组发布。非农新增、3M 均值和时薪同比为可复算派生；"
                "所有组件分别保留数值日、抓取时间、批次、初值/修订状态与来源。"
            ),
            "metrics": employment_metrics,
            "charts": employment_charts,
            "sections": employment_sections,
            "required_metric_keys": EMPLOYMENT_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "inflation",
            "title": "通胀",
            "summary": (
                "总体 CPI、核心 CPI、Shelter、核心商品、不含能源服务的服务 CPI "
                "与最终需求 PPI 的环比和短期动能来自"
                "BLS 季调指数，同比来自对应未季调指数。所有变化率按精确"
                "自然月透明计算并绑定同一 BLS 抓取批次；PCE、市场"
                "预期与完整 vintage 缺口在数据台账中单列。"
            ),
            "metrics": inflation_metrics,
            "charts": inflation_charts,
            "sections": inflation_sections,
            "required_metric_keys": INFLATION_REQUIRED_METRIC_KEYS,
        },
        {
            "key": "consumer",
            "title": "消费与零售",
            "summary": (
                "零售与餐饮服务销售来自 Census MARTS 官方发布工作簿；完整"
                "API 历史仍需 CENSUS_API_KEY 后启用。实际 PCE、"
                "实际可支配收入和个人储蓄率来自 BEA 月度 PIO Section 2 工作簿，"
                "并与当月 Historical Comparisons 摘要交叉校验。消费者信贷来自"
                "联储 G.19，家庭债务和逾期率来自 New York Fed Consumer Credit "
                "Panel / Equifax；总量数据不用于推断特定收入群体的压力。消费者"
                "信心因公开再发布许可未就绪，继续保留采购标记。"
            ),
            "metrics": consumer_metrics,
            "charts": consumer_charts,
            "extra_data": {
                "contract_version": CONSUMER_CONTRACT_VERSION,
                "retail_source_key": retail_source_key,
                "retail_batch_id": str(retail_batch) if retail_batch else None,
            },
            "required_metric_keys": frozenset(
                {
                    "census-mrts-44x72-sm-sa",
                    "census-mrts-44x72-sm-sa-mom",
                    "census-mrts-44x72-sm-sa-yoy",
                    "bea-real-pce-mom",
                    "bea-personal-saving-rate",
                    "bea-real-dpi-mom",
                }
            ),
        },
    ]
    with transaction.atomic():
        for definition in definitions:
            if selected_keys is not None and definition["key"] not in selected_keys:
                continue
            snapshot = _publish_dashboard(batch_id=batch_id, **definition)
            if snapshot:
                dashboards.append(snapshot)
    return dashboards


def refresh_official_data(*, current_year: int | None = None) -> dict[str, Any]:
    """Fetch direct official sources, normalize observations, then publish pages."""

    year = current_year or timezone.now().year
    refresh_cycle_id = str(uuid.uuid4())
    runs: list[IngestionRun] = []
    providers = [
        (
            NYFedMarketsProvider(),
            (
                ("sofr", {"limit": 800}),
                ("effr", {"limit": 800}),
                ("reverse_repo_results", {"limit": 120}),
                ("standing_repo_results", {"limit": 240}),
                ("soma_summary", {"limit": 260}),
                ("usd_fx_swaps", {"limit": 500}),
            ),
        ),
        (
            TreasuryRatesProvider(),
            (
                (
                    "yield_curve",
                    {"year": year},
                    _store_treasury_curve_observations,
                ),
                (
                    "real_yield_curve",
                    {"year": year},
                    _store_treasury_curve_observations,
                ),
            ),
        ),
        (
            FiscalDataProvider(),
            (
                ("tga", {"page_size": 400}),
                ("treasury_auctions", {"page_size": 1000}, store_treasury_auctions),
            ),
        ),
        (
            BLSProvider(),
            (
                (
                    "series",
                    {
                        "series_ids": BLS_SERIES,
                        "start_year": max(year - 5, 2000),
                        "end_year": year,
                    },
                ),
            ),
        ),
        (
            BEAPIOReleaseProvider(),
            (
                (
                    "personal_income_outlays",
                    {},
                    _store_release_workbook_observations,
                ),
            ),
        ),
        (
            DOLWeeklyClaimsProvider(),
            (
                (
                    "weekly_claims",
                    {
                        "start_year": max(year - 5, 1967),
                        "end_year": year,
                    },
                    _store_series_with_artifacts,
                ),
            ),
        ),
        (
            FederalReserveRSSProvider(),
            (
                (
                    "feed",
                    {"feed_name": "press-all", "document_type": "news"},
                    store_fed_documents,
                ),
                (
                    "feed",
                    {"feed_name": "press-monetary", "document_type": "statement"},
                    store_fed_documents,
                ),
                (
                    "feed",
                    {"feed_name": "speeches", "document_type": "speech"},
                    store_fed_documents,
                ),
            ),
        ),
    ]
    try:
        for provider, calls in providers:
            for call in calls:
                method_name, kwargs, *persist_override = call
                result = getattr(provider, method_name)(**kwargs)
                result.metadata = {
                    **result.metadata,
                    "refresh_cycle_id": refresh_cycle_id,
                }
                persist = persist_override[0] if persist_override else store_series_observations
                runs.append(record_provider_result(result, persist=persist))
    finally:
        for provider, _ in providers:
            provider.close()
    core_runs = [run for run in runs if run.source.key != "dol-eta-ui"]
    dashboards = (
        publish_official_dashboards(keys=CORE_PUBLICATION_KEYS)
        if _has_publishable_run(core_runs)
        else []
    )
    employment_completed = _publishable_keys_for_source_groups(
        runs, EMPLOYMENT_PUBLICATION_GROUPS
    )
    employment_publishable = _keys_with_current_required_batches(
        employment_completed, runs
    )
    if employment_publishable and not _employment_page_is_buildable():
        employment_publishable = set()
    stale_employment_keys = set(EMPLOYMENT_PUBLICATION_GROUPS) - set(
        employment_publishable
    )
    _mark_latest_dashboards_stale(
        stale_employment_keys,
        runs,
        groups=EMPLOYMENT_PUBLICATION_GROUPS,
    )
    if employment_publishable:
        dashboards.extend(
            publish_official_dashboards(keys=employment_publishable)
        )
    inflation_completed = _publishable_keys_for_source_groups(
        runs, INFLATION_PUBLICATION_GROUPS
    )
    inflation_publishable = _keys_with_current_required_batches(
        inflation_completed, runs
    )
    inflation_bls_runs = [run for run in runs if run.source.key == "bls"]
    inflation_bea_pio_runs = [
        run for run in runs if run.source.key == "bea-pio-release"
    ]
    inflation_batch_id = (
        inflation_bls_runs[0].batch_id
        if len(inflation_bls_runs) == 1
        else None
    )
    inflation_bea_pio_batch_id = (
        inflation_bea_pio_runs[0].batch_id
        if len(inflation_bea_pio_runs) == 1
        else None
    )
    if inflation_publishable and not _inflation_page_is_buildable(
        batch_id=inflation_batch_id,
        bea_pio_batch_id=inflation_bea_pio_batch_id,
    ):
        inflation_publishable = set()
    stale_inflation_keys = set(INFLATION_PUBLICATION_GROUPS) - set(
        inflation_publishable
    )
    _mark_latest_dashboards_stale(
        stale_inflation_keys,
        runs,
        groups=INFLATION_PUBLICATION_GROUPS,
    )
    if (
        inflation_publishable
        and inflation_batch_id is not None
        and inflation_bea_pio_batch_id is not None
    ):
        dashboards.extend(
            publish_official_dashboards(
                keys=inflation_publishable,
                source_batches={
                    "bls": inflation_batch_id,
                    "bea-pio-release": inflation_bea_pio_batch_id,
                },
            )
        )
    stale_dashboard_keys = stale_employment_keys | stale_inflation_keys
    fed_funds_dashboards, stale_fed_funds_keys = (
        _coordinate_fed_funds_dashboard(runs)
    )
    dashboards.extend(fed_funds_dashboards)
    stale_dashboard_keys |= stale_fed_funds_keys
    treasury_dashboards, stale_treasury_keys = (
        _coordinate_treasury_curve_dashboards(runs, end_year=year)
    )
    dashboards.extend(treasury_dashboards)
    stale_dashboard_keys |= stale_treasury_keys
    liquidity_dashboards, stale_liquidity_keys = (
        _coordinate_liquidity_dashboard(runs)
    )
    dashboards.extend(liquidity_dashboards)
    stale_dashboard_keys |= stale_liquidity_keys
    auction_dashboards, stale_auction_keys = _coordinate_auction_dashboard(
        runs
    )
    dashboards.extend(auction_dashboards)
    stale_dashboard_keys |= stale_auction_keys
    rrp_tga_dashboards, stale_rrp_tga_keys = _coordinate_rrp_tga_dashboard(
        runs
    )
    dashboards.extend(rrp_tga_dashboards)
    stale_dashboard_keys |= stale_rrp_tga_keys
    economy_dashboards, stale_economy_keys = _coordinate_economy_dashboard()
    dashboards.extend(economy_dashboards)
    stale_dashboard_keys |= stale_economy_keys
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
            }
            for run in runs
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
        "stale_dashboard_keys": sorted(stale_dashboard_keys),
    }


def refresh_treasury_curve_data(
    *,
    start_year: int | None = None,
    end_year: int | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Backfill explicit annual Treasury curve datasets, then coordinate v1 pages."""

    resolved_end = end_year or timezone.now().year
    resolved_start = (
        start_year
        if start_year is not None
        else resolved_end - TREASURY_CURVE_HISTORY_YEARS
    )
    if resolved_start > resolved_end:
        raise ValueError("Treasury curve start year must not exceed end year")
    if resolved_start < 1990 or resolved_end > timezone.now().year:
        raise ValueError("Treasury curve years are outside the supported historical range")

    refresh_cycle_id = str(uuid.uuid4())
    runs: list[IngestionRun] = []
    for year in range(resolved_start, resolved_end + 1):
        for method_name in ("yield_curve", "real_yield_curve"):
            provider = TreasuryRatesProvider()
            try:
                result = getattr(provider, method_name)(year=year)
            finally:
                provider.close()
            try:
                result.metadata = {
                    **result.metadata,
                    "refresh_cycle_id": refresh_cycle_id,
                    "backfill_start_year": resolved_start,
                    "backfill_end_year": resolved_end,
                }
                runs.append(
                    record_provider_result(
                        result,
                        persist=_store_treasury_curve_observations,
                    )
                )
            finally:
                del result
    if publish:
        dashboards, stale_keys = _coordinate_treasury_curve_dashboards(
            runs,
            end_year=resolved_end,
        )
    else:
        dashboards, stale_keys = [], set()
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
            }
            for run in runs
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
        "stale_dashboard_keys": sorted(stale_keys),
        "start_year": resolved_start,
        "end_year": resolved_end,
        "publish_requested": publish,
    }


def refresh_h41_data() -> dict[str, Any]:
    """Refresh the large weekly H.4.1 package separately from frequent jobs."""

    provider = FederalReserveH41Provider()
    try:
        result = provider.h41()
        run = record_provider_result(result, persist=_store_h41_observations)
    finally:
        provider.close()
    dashboards = (
        publish_official_dashboards(keys=H41_PUBLICATION_KEYS)
        if _has_publishable_run([run])
        else []
    )
    liquidity_dashboards, stale_liquidity_keys = (
        _coordinate_liquidity_dashboard([run])
    )
    dashboards.extend(liquidity_dashboards)
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
                "metadata": run.metadata,
            }
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
        "stale_dashboard_keys": sorted(stale_liquidity_keys),
    }


def refresh_prates_data() -> dict[str, Any]:
    """Refresh the Board's daily IORB series separately from the main batch."""

    provider = FederalReservePRATESProvider()
    try:
        result = provider.iorb()
        result.metadata = {
            **result.metadata,
            "refresh_cycle_id": str(uuid.uuid4()),
        }
        run = record_provider_result(result, persist=_store_prates_observations)
    finally:
        provider.close()
    dashboards = (
        publish_official_dashboards(keys=PRATES_PUBLICATION_KEYS)
        if _has_publishable_run([run])
        else []
    )
    fed_funds_dashboards, stale_fed_funds_keys = (
        _coordinate_fed_funds_dashboard([run])
    )
    dashboards.extend(fed_funds_dashboards)
    liquidity_dashboards, stale_liquidity_keys = (
        _coordinate_liquidity_dashboard([run])
    )
    dashboards.extend(liquidity_dashboards)
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
                "metadata": run.metadata,
            }
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
        "stale_dashboard_keys": sorted(
            stale_fed_funds_keys | stale_liquidity_keys
        ),
    }


def refresh_h10_data() -> dict[str, Any]:
    """Refresh Board H.10 daily reference FX data and publish the FX page."""

    provider = FederalReserveH10Provider()
    try:
        result = provider.h10()
        run = record_provider_result(result, persist=_store_h10_observations)
    finally:
        provider.close()
    dashboards = (
        publish_official_dashboards(keys=H10_PUBLICATION_KEYS)
        if _has_publishable_run([run])
        else []
    )
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
                "metadata": run.metadata,
            }
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
    }


def refresh_credit_official_data() -> dict[str, Any]:
    """Refresh public-display-safe official credit proxies once per day."""

    providers = [
        (FederalReserveSLOOSProvider(), "quarterly_series"),
        (TreasuryHQMProvider(), "par_yields"),
    ]
    runs: list[IngestionRun] = []
    try:
        for provider, method_name in providers:
            result = getattr(provider, method_name)()
            runs.append(record_provider_result(result, persist=store_series_observations))
    finally:
        for provider, _ in providers:
            provider.close()
    dashboards = (
        publish_official_dashboards(keys=CREDIT_PUBLICATION_KEYS)
        if _has_publishable_run(runs)
        else []
    )
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
            }
            for run in runs
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
    }


def refresh_macro_official_data(*, current_year: int | None = None) -> dict[str, Any]:
    """Refresh official growth and consumer sources with page-level quality gates."""

    _ = current_year  # Backward-compatible command/task signature.
    providers = [
        (
            BEAGDPReleaseProvider(),
            "gdp_pce",
            {},
            _store_release_workbook_observations,
        ),
        (
            CensusMARTSProvider(),
            "monthly_retail_sales",
            {"time": "from 1992", "require_complete_history": True},
            _store_release_workbook_observations,
        ),
        (
            CensusMARTSReleaseProvider(),
            "monthly_retail_sales",
            {},
            _store_release_workbook_observations,
        ),
        (
            BEAPIOReleaseProvider(),
            "personal_income_outlays",
            {},
            _store_release_workbook_observations,
        ),
        (
            FederalReserveG19Provider(),
            "consumer_credit",
            {},
            _store_release_workbook_observations,
        ),
        (
            NYFedHouseholdDebtProvider(),
            "household_debt",
            {},
            _store_release_workbook_observations,
        ),
    ]
    runs: list[IngestionRun] = []
    try:
        for provider, method_name, kwargs, persist in providers:
            result = getattr(provider, method_name)(**kwargs)
            runs.append(record_provider_result(result, persist=persist))
    finally:
        for provider, _, _, _ in providers:
            provider.close()
    _record_census_revision_witness(runs)
    completed_keys = _publishable_keys_for_source_groups(
        runs, MACRO_PUBLICATION_GROUPS
    )
    publishable_keys = _keys_with_current_required_batches(completed_keys, runs)
    stale_keys = set(MACRO_PUBLICATION_GROUPS) - publishable_keys
    _mark_latest_dashboards_stale(stale_keys, runs)
    dashboards = (
        publish_official_dashboards(
            keys=publishable_keys,
            source_batches={run.source.key: run.batch_id for run in runs},
        )
        if publishable_keys
        else []
    )
    economy_dashboards, stale_economy_keys = _coordinate_economy_dashboard()
    dashboards.extend(economy_dashboards)
    stale_keys |= stale_economy_keys
    return {
        "runs": [
            {
                "source": run.source.key,
                "dataset": run.dataset,
                "status": run.status,
                "row_count": run.row_count,
                "error": run.error,
                "metadata": run.metadata,
            }
            for run in runs
        ],
        "dashboard_keys": [dashboard.key for dashboard in dashboards],
        "stale_dashboard_keys": sorted(stale_keys),
    }
