"""Strict H.10 realized-volatility publication contract.

The only numerical volatility surface that Atlas can currently reproduce from
an immutable, public-display-safe acquisition is Federal Reserve H.10.  This
module deliberately does not publish VIX, MOVE or implied-volatility proxies.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from collections.abc import Iterable, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from xml.etree import ElementTree

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    Source,
    SourceLicense,
)
from .services import ensure_source, public_source_notices

FX_VOL_CONTRACT_VERSION = 1
FX_VOL_FORMULA_VERSION = "federal-reserve-h10-realized-vol-v1"
FX_VOL_PUBLICATION_KEYS = frozenset({"fx-vol"})
FX_VOL_TITLE = "H.10 外汇实现波动率"
FX_VOL_SUMMARY = (
    "Federal Reserve H.10 官方参考序列的 20 日与 60 日年化实现波动率。"
    "这是 Atlas Macro 的透明历史计算，不是 FX 期权隐含波动率、风险逆转、"
    "butterfly、可执行报价或交易信号。"
)
FX_VOL_FORMULA = (
    "100 * sqrt(252) * sample_std(ln(P_t / P_t-1)); "
    "N+1 valid levels, N returns, denominator N-1"
)
FX_VOL_WINDOWS = (20, 60)
FX_VOL_HISTORY_POINTS = 260
FX_VOL_MINIMUM_COMMON_LEVELS = FX_VOL_HISTORY_POINTS + max(FX_VOL_WINDOWS)
FX_VOL_REQUIRED_SERIES = (
    "h10-broad-dollar",
    "h10-eurusd",
    "h10-usdcny",
    "h10-usdjpy",
)
FX_VOL_REQUIRED_METRIC_KEYS = frozenset(
    {
        "h10-broad-dollar-rv20",
        "h10-eurusd-rv20",
        "h10-usdcny-rv20",
        "h10-usdjpy-rv20",
    }
)
FX_VOL_REQUIRED_CHART_KEYS = frozenset(
    {
        "h10-fx-realized-volatility-20d",
        "h10-fx-realized-volatility-60d",
    }
)
FX_VOL_REQUIRED_SECTION_KEYS = frozenset(
    {
        "latest-h10-realized-volatility",
        "h10-rv-source-methodology",
        "licensed-fx-volatility-gaps",
    }
)
FX_VOL_SEMANTIC_BOUNDARY = (
    "H.10 reference-level realized volatility only; not option implied "
    "volatility, VIX, MOVE, CVOL, executable FX, forward/NDF, skew, a risk "
    "score or a trade signal."
)
FX_VOL_LABELS = {
    "h10-broad-dollar": "H.10 Broad Dollar",
    "h10-eurusd": "H.10 EUR/USD",
    "h10-usdcny": "H.10 USD/CNY",
    "h10-usdjpy": "H.10 USD/JPY",
}
FX_VOL_QUOTE_LABELS = {
    "h10-broad-dollar": "Jan 2006 = 100 index",
    "h10-eurusd": "U.S. dollars per euro",
    "h10-usdcny": "Chinese yuan per U.S. dollar",
    "h10-usdjpy": "Japanese yen per U.S. dollar",
}
FX_VOL_LATEST_COLUMNS = (
    "reference",
    "rv-20d",
    "rv-60d",
    "change-20d-pp",
    "value-date",
    "quote-convention",
    "quality",
    "batch",
)
FX_VOL_METHOD_COLUMNS = (
    "source-dataset",
    "run-batch",
    "prepared",
    "fetched",
    "common-latest-date",
    "fresh-until",
    "archive",
    "member",
    "common-observations",
    "formula",
    "licence",
    "fallback",
)
FX_VOL_GAP_COLUMNS = (
    "market-data",
    "status",
    "public-value",
    "purchase-guidance",
)
FX_VOL_GAPS = (
    (
        "FX ATM implied volatility",
        "PURCHASE_REQUIRED",
        "LSEG, Bloomberg or CME FX/CVOL with website-display rights",
    ),
    (
        "25-delta risk reversal and butterfly",
        "PURCHASE_REQUIRED",
        "Licensed OTC composite or exchange-derived volatility surface",
    ),
    (
        "Executable institutional spot",
        "PURCHASE_REQUIRED",
        "CME EBS, Cboe FX, LSEG or Bloomberg Enterprise",
    ),
    (
        "FX forwards and NDF",
        "PURCHASE_REQUIRED",
        "Venue or enterprise feed with storage and derived-display rights",
    ),
    (
        "Cross-currency basis and FX-swap implied funding",
        "LICENSE_REVIEW",
        "LSEG, Bloomberg or another licensed derived-display product",
    ),
)
FX_VOL_PAYLOAD_KEYS = frozenset(
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
        "formulas",
        "input_run",
        "component_value_dates",
        "component_fetched_at",
        "acquisition_artifact",
        "license_decisions",
        "required_metric_keys",
        "required_chart_keys",
        "required_section_keys",
        "semantic_boundary",
        "fingerprint",
        "payload_integrity_hash",
    }
)


def annualized_realized_volatility(
    levels: Sequence[Decimal | int | float | str],
    *,
    window: int,
) -> Decimal:
    """Return annualized percentage RV using exactly ``window`` log returns."""

    if window < 2 or len(levels) != window + 1:
        raise ValueError("realized volatility requires exactly N+1 levels and N >= 2")
    try:
        values = [Decimal(str(value)) for value in levels]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("realized-volatility levels must be finite decimals") from exc
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("realized-volatility levels must be finite and positive")
    with localcontext() as context:
        context.prec = 42
        returns = [
            (right / left).ln()
            for left, right in zip(values[:-1], values[1:], strict=True)
        ]
        mean = sum(returns, Decimal("0")) / Decimal(window)
        sample_variance = sum(
            ((value - mean) ** 2 for value in returns), Decimal("0")
        ) / Decimal(window - 1)
        return Decimal("100") * Decimal("252").sqrt() * sample_variance.sqrt()


def _rolling_realized_volatility(
    observations: Sequence[Observation],
    *,
    window: int,
) -> list[tuple[date, Decimal, Sequence[Observation]]]:
    """Calculate every rolling RV while taking each expensive log only once."""

    if window < 2 or len(observations) < window + 1:
        raise ValueError("rolling realized volatility has insufficient observations")
    values = [Decimal(str(item.value)) for item in observations]
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("rolling realized-volatility levels must be finite and positive")
    results: list[tuple[date, Decimal, Sequence[Observation]]] = []
    with localcontext() as context:
        context.prec = 42
        returns = [
            (right / left).ln()
            for left, right in zip(values[:-1], values[1:], strict=True)
        ]
        rolling_sum = sum(returns[:window], Decimal("0"))
        rolling_squares = sum(
            (value * value for value in returns[:window]), Decimal("0")
        )
        annualizer = Decimal("100") * Decimal("252").sqrt()
        for end_index in range(window, len(observations)):
            if end_index > window:
                outgoing = returns[end_index - window - 1]
                incoming = returns[end_index - 1]
                rolling_sum += incoming - outgoing
                rolling_squares += incoming * incoming - outgoing * outgoing
            variance_numerator = (
                rolling_squares
                - rolling_sum * rolling_sum / Decimal(window)
            )
            if variance_numerator < 0:
                if abs(variance_numerator) > Decimal("1e-36"):
                    raise ValueError("rolling sample variance became negative")
                variance_numerator = Decimal("0")
            realized = annualizer * (
                variance_numerator / Decimal(window - 1)
            ).sqrt()
            selected = observations[end_index - window : end_index + 1]
            results.append(
                (
                    observations[end_index].value_date.date(),
                    _quantized(realized),
                    selected,
                )
            )
    return results


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _current_license(source: Source) -> SourceLicense:
    today = timezone.localdate()
    license_row = (
        SourceLicense.objects.filter(source=source, is_current=True)
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=today))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=today))
        .order_by("-created_at", "-pk")
        .first()
    )
    if (
        license_row is None
        or license_row.status
        not in {Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED}
        or not license_row.public_display_allowed
        or not license_row.derived_display_allowed
        or not license_row.historical_storage_allowed
    ):
        raise ValueError(
            f"{source.key} lacks current public, derived and storage rights"
        )
    return license_row


def _cell(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "cell": {"kind": "text", "value": str(value)}}


def _columns(keys: Sequence[str], labels: Sequence[str]) -> list[dict[str, str]]:
    return [
        {"key": key, "label": label}
        for key, label in zip(keys, labels, strict=True)
    ]


def _metric_key(series_key: str) -> str:
    return f"{series_key}-rv20"


def _quantized(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"))


def _display_pct(value: Decimal) -> str:
    return f"{value:.2f}%"


def _compact_window_lineage(
    *,
    run: IngestionRun,
    series_key: str,
    observations: Sequence[Observation],
    component: dict[str, Any],
    federal_scope: str,
    internal_scope: str,
    window: int,
) -> dict[str, Any]:
    return {
        "series_key": series_key,
        "source_key": "internal",
        "source_keys": ["federal-reserve", "internal"],
        "license_scopes": {
            "federal-reserve": federal_scope,
            "internal": internal_scope,
        },
        "formula": FX_VOL_FORMULA,
        "window": window,
        "sample_count": window,
        "window_start": observations[0].value_date.isoformat(),
        "window_end": observations[-1].value_date.isoformat(),
        "value_date": observations[-1].value_date.isoformat(),
        "as_of": observations[-1].as_of.isoformat(),
        "fetched_at": component["fetched_at"].isoformat(),
        "fresh_until": component["fresh_until"].isoformat(),
        "run_id": run.pk,
        "batch_id": str(run.batch_id),
        "acquisition_sha256": component["artifact_sha256"],
        "archive_member_sha256": component["member_sha256"],
        "quality_status": Observation.Quality.ESTIMATED,
        "fallback_source": None,
    }


def _semantic_projection(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": data["contract_version"],
        "formula_version": data["formula_version"],
        "formulas": data["formulas"],
        "semantic_boundary": data["semantic_boundary"],
        "metrics": [
            {
                "key": item["key"],
                "value": item["value"],
                "change": item["change"],
                "value_date": item["value_date"],
            }
            for item in data["metrics"]
        ],
        "charts": [
            {
                "key": item["key"],
                "data": [
                    {
                        key: value
                        for key, value in row.items()
                        if not key.startswith("_")
                        and key not in {"lineage", "source_keys", "batch_id", "quality_status", "fallback_source"}
                    }
                    for row in item["data"]
                ],
            }
            for item in data["charts"]
        ],
    }


def _integrity_projection(data: dict[str, Any]) -> dict[str, Any]:
    projected = deepcopy(data)
    projected.pop("payload_integrity_hash", None)
    projected.pop("refresh_failure", None)
    return projected


def _build_fx_vol_payload(
    run: IngestionRun,
    *,
    publication_batch_id: uuid.UUID,
    allow_expired: bool,
) -> tuple[dict[str, Any], datetime]:
    # Local imports prevent a module cycle while sharing the neutral H.10
    # acquisition truth with assets-fx and global-dollar.
    from .official_data import _h10_direct_lineage, _validated_h10_component

    if (
        run.source.key != "federal-reserve"
        or run.dataset != "h10"
        or run.status != IngestionRun.Status.SUCCESS
        or run.row_count <= 0
    ):
        raise ValueError("fx-vol requires one successful non-empty H.10 run")
    sources = {
        source.key: source
        for source in Source.objects.filter(
            key__in={"federal-reserve", "internal"}
        )
    }
    if set(sources) != {"federal-reserve", "internal"}:
        raise ValueError("fx-vol sources are incomplete")
    licenses = {key: _current_license(source) for key, source in sources.items()}
    by_series, component = _validated_h10_component(
        run,
        allow_expired=allow_expired,
        require_observations=True,
    )
    if set(by_series) != set(FX_VOL_REQUIRED_SERIES):
        raise ValueError("fx-vol H.10 series set changed")
    maps: dict[str, dict[date, Observation]] = {}
    for series_key in FX_VOL_REQUIRED_SERIES:
        mapped: dict[date, Observation] = {}
        for observation in by_series[series_key]:
            value_date = observation.value_date.date()
            if value_date in mapped:
                raise ValueError("fx-vol common-date input is duplicated")
            if not observation.value.is_finite() or observation.value <= 0:
                raise ValueError("fx-vol input level is nonfinite or nonpositive")
            mapped[value_date] = observation
        maps[series_key] = mapped
    common_dates = sorted(set.intersection(*(set(rows) for rows in maps.values())))
    if len(common_dates) < FX_VOL_MINIMUM_COMMON_LEVELS:
        raise ValueError(
            "fx-vol requires at least 320 common valid H.10 observations"
        )

    rolling: dict[int, dict[str, list[tuple[date, Decimal, Sequence[Observation]]]]] = {
        window: {series_key: [] for series_key in FX_VOL_REQUIRED_SERIES}
        for window in FX_VOL_WINDOWS
    }
    for series_key in FX_VOL_REQUIRED_SERIES:
        common_observations = [maps[series_key][item] for item in common_dates]
        for window in FX_VOL_WINDOWS:
            rolling[window][series_key] = _rolling_realized_volatility(
                common_observations,
                window=window,
            )
    if any(
        len(rolling[window][series_key]) < FX_VOL_HISTORY_POINTS
        for window in FX_VOL_WINDOWS
        for series_key in FX_VOL_REQUIRED_SERIES
    ):
        raise ValueError("fx-vol rolling histories are incomplete")

    federal_scope = licenses["federal-reserve"].scope
    internal_scope = licenses["internal"].scope
    latest_date = common_dates[-1]
    metrics: list[dict[str, Any]] = []
    metric_by_series: dict[str, dict[str, Any]] = {}
    for series_key in FX_VOL_REQUIRED_SERIES:
        previous_row, current_row = rolling[20][series_key][-2:]
        current_date, current_value, current_observations = current_row
        previous_date, previous_value, previous_observations = previous_row
        change = _quantized(current_value - previous_value)
        input_lineage = [
            _h10_direct_lineage(
                observation,
                run=run,
                scope=federal_scope,
                fresh_until=component["fresh_until"],
                acquisition_sha256=component["artifact_sha256"],
                member_sha256=component["member_sha256"],
            )
            for observation in current_observations
        ]
        metric = {
            "key": _metric_key(series_key),
            "label": f"{FX_VOL_LABELS[series_key]} 20D RV",
            "value": float(current_value),
            "display_value": _display_pct(current_value),
            "change": float(change),
            "unit": "% annualized",
            "quality_status": Observation.Quality.ESTIMATED,
            "source": "Atlas Macro over Federal Reserve H.10",
            "source_key": "internal",
            "source_keys": ["federal-reserve", "internal"],
            "license_scope": internal_scope,
            "value_date": current_observations[-1].value_date.isoformat(),
            "as_of": current_observations[-1].as_of.isoformat(),
            "fetched_at": component["fetched_at"].isoformat(),
            "fresh_until": component["fresh_until"].isoformat(),
            "batch_id": str(run.batch_id),
            "fallback_source": None,
            "metadata": {
                "formula": FX_VOL_FORMULA,
                "formula_version": FX_VOL_FORMULA_VERSION,
                "window": 20,
                "annualization_factor": 252,
                "standard_deviation": "sample",
                "sample_count": 20,
                "change_formula": "RV20_t - RV20_t-1",
                "change_unit": "pp",
                "change_quality_status": Observation.Quality.ESTIMATED,
                "calculation_owner": "Atlas Macro",
                "input_series": [series_key],
                "quote_convention": FX_VOL_QUOTE_LABELS[series_key],
                "current_window_start": current_observations[0].value_date.isoformat(),
                "current_window_end": current_observations[-1].value_date.isoformat(),
                "previous_window_start": previous_observations[0].value_date.isoformat(),
                "previous_window_end": previous_observations[-1].value_date.isoformat(),
                "previous_value_date": previous_date.isoformat(),
                "current_value_date": current_date.isoformat(),
                "input_run_id": run.pk,
                "input_batch_ids": [str(run.batch_id)],
                "input_lineage": input_lineage,
                "acquisition_sha256": component["artifact_sha256"],
                "archive_member_sha256": component["member_sha256"],
                "federal_reserve_license_scope": federal_scope,
                "internal_license_scope": internal_scope,
            },
        }
        metrics.append(metric)
        metric_by_series[series_key] = metric

    charts: list[dict[str, Any]] = []
    for window in FX_VOL_WINDOWS:
        rows: list[dict[str, Any]] = []
        window_results = {
            series_key: {
                result_date: (value, observations)
                for result_date, value, observations in rolling[window][series_key]
            }
            for series_key in FX_VOL_REQUIRED_SERIES
        }
        chart_dates = common_dates[-FX_VOL_HISTORY_POINTS:]
        for result_date in chart_dates:
            row: dict[str, Any] = {
                "date": result_date.isoformat(),
                "source_keys": ["federal-reserve", "internal"],
                "batch_id": str(run.batch_id),
                "quality_status": Observation.Quality.ESTIMATED,
                "fallback_source": None,
                "lineage": {},
                "_source_keys": ["federal-reserve", "internal"],
                "_lineage": {},
            }
            for series_key in FX_VOL_REQUIRED_SERIES:
                value, observations = window_results[series_key][result_date]
                label = FX_VOL_LABELS[series_key]
                lineage = _compact_window_lineage(
                    run=run,
                    series_key=series_key,
                    observations=observations,
                    component=component,
                    federal_scope=federal_scope,
                    internal_scope=internal_scope,
                    window=window,
                )
                row[label] = float(value)
                row["lineage"][label] = lineage
                row["_lineage"][label] = lineage
            rows.append(row)
        charts.append(
            {
                "key": f"h10-fx-realized-volatility-{window}d",
                "title": f"H.10 reference-level {window}D realized volatility",
                "description": (
                    f"Latest {FX_VOL_HISTORY_POINTS} common valid dates; annualized %, "
                    "sample standard deviation, no interpolation or forward fill."
                ),
                "kind": "line",
                "data": rows,
                "source_keys": ["federal-reserve", "internal"],
                "batch_ids": [str(run.batch_id)],
                "as_of": maps[FX_VOL_REQUIRED_SERIES[0]][latest_date].as_of.isoformat(),
                "fetched_at": component["fetched_at"].isoformat(),
                "fresh_until": component["fresh_until"].isoformat(),
                "quality_status": Observation.Quality.ESTIMATED,
                "frequency": "daily-valid-observation",
                "time_axis": "date",
                "tab": f"{window}d",
                "window": window,
                "formula": FX_VOL_FORMULA,
                "license_scopes": [
                    f"Federal Reserve: {federal_scope}",
                    f"Atlas Macro: {internal_scope}",
                ],
                "fallback_sources": [],
            }
        )

    latest_rows: list[dict[str, Any]] = []
    for series_key in FX_VOL_REQUIRED_SERIES:
        current_20 = rolling[20][series_key][-1][1]
        previous_20 = rolling[20][series_key][-2][1]
        current_60 = rolling[60][series_key][-1][1]
        row = {
            "reference": FX_VOL_LABELS[series_key],
            "rv-20d": _display_pct(current_20),
            "rv-60d": _display_pct(current_60),
            "change-20d-pp": f"{current_20 - previous_20:+.2f}pp",
            "value-date": latest_date.isoformat(),
            "quote-convention": FX_VOL_QUOTE_LABELS[series_key],
            "quality": Observation.Quality.ESTIMATED,
            "batch": str(run.batch_id),
            "lineage": metric_by_series[series_key]["metadata"],
            "fallback_source": None,
        }
        row["cells_list"] = [_cell(key, row[key]) for key in FX_VOL_LATEST_COLUMNS]
        latest_rows.append(row)

    method_row = {
        "source-dataset": "Federal Reserve / h10",
        "run-batch": f"{run.pk} / {run.batch_id}",
        "prepared": component["prepared_at"].isoformat(),
        "fetched": component["fetched_at"].isoformat(),
        "common-latest-date": latest_date.isoformat(),
        "fresh-until": component["fresh_until"].isoformat(),
        "archive": f"{component['artifact_sha256']} / {component['artifact_size']} bytes",
        "member": f"{component['member_sha256']} / {component['member_size']} bytes",
        "common-observations": len(common_dates),
        "formula": FX_VOL_FORMULA,
        "licence": f"Federal Reserve: {federal_scope}; Atlas Macro: {internal_scope}",
        "fallback": "none",
        "lineage": {
            "run_id": run.pk,
            "batch_id": str(run.batch_id),
            "artifact_id": component["artifact"].pk,
        },
        "fallback_source": None,
    }
    method_row["cells_list"] = [_cell(key, method_row[key]) for key in FX_VOL_METHOD_COLUMNS]
    gap_rows: list[dict[str, Any]] = []
    for market_data, status, guidance in FX_VOL_GAPS:
        row = {
            "market-data": market_data,
            "status": status,
            "public-value": "—",
            "purchase-guidance": guidance,
            "lineage": {"contract": "procurement-boundary"},
            "fallback_source": None,
        }
        row["cells_list"] = [_cell(key, row[key]) for key in FX_VOL_GAP_COLUMNS]
        gap_rows.append(row)

    sections = [
        {
            "key": "latest-h10-realized-volatility",
            "title": "最新 H.10 实现波动率",
            "description": "20D/60D 均按共同有效观察计算，单位为年化百分比。",
            "columns": _columns(
                FX_VOL_LATEST_COLUMNS,
                ("参考序列", "20D RV", "60D RV", "20D 日变", "数值日", "官方报价方向", "质量", "输入批次"),
            ),
            "rows": latest_rows,
            "source_keys": ["federal-reserve", "internal"],
            "batch_id": str(run.batch_id),
            "fresh_until": component["fresh_until"].isoformat(),
            "status": Observation.Quality.ESTIMATED,
            "fallback_source": None,
            "full_width": True,
        },
        {
            "key": "h10-rv-source-methodology",
            "title": "来源、新鲜度与计算方法",
            "description": "从私有内容寻址 H.10 ZIP 重放全部输入；不读取页面缓存或演示数据。",
            "columns": _columns(
                FX_VOL_METHOD_COLUMNS,
                ("来源 / 数据集", "run / batch", "Prepared", "抓取", "共同最新日", "有效至", "ZIP SHA / 大小", "XML SHA / 大小", "共同观察数", "公式", "许可", "fallback"),
            ),
            "rows": [method_row],
            "source_keys": ["federal-reserve", "internal"],
            "batch_id": str(run.batch_id),
            "fresh_until": component["fresh_until"].isoformat(),
            "status": Observation.Quality.ESTIMATED,
            "fallback_source": None,
            "full_width": True,
        },
        {
            "key": "licensed-fx-volatility-gaps",
            "title": "尚未授权的外汇波动率数据",
            "description": "以下项目不以 H.10 实现波动率替代，也不发布伪造的 IV-RV 差值。",
            "columns": _columns(
                FX_VOL_GAP_COLUMNS,
                ("市场数据", "状态", "公开值", "采购建议"),
            ),
            "rows": gap_rows,
            "source_keys": [],
            "batch_id": str(run.batch_id),
            "fresh_until": component["fresh_until"].isoformat(),
            "status": "procurement",
            "fallback_source": None,
            "full_width": True,
        },
    ]

    payload: dict[str, Any] = {
        "demo": False,
        "metrics": metrics,
        "charts": charts,
        "chart_data": charts[0]["data"],
        "sections": sections,
        "component_batches": [str(run.batch_id)],
        "source_keys": ["federal-reserve", "internal"],
        "required_notices": public_source_notices(
            {"federal-reserve", "internal"}
        ),
        "fresh_until": component["fresh_until"].isoformat(),
        "publication_batch_id": str(publication_batch_id),
        "contract_version": FX_VOL_CONTRACT_VERSION,
        "formula_version": FX_VOL_FORMULA_VERSION,
        "formulas": {
            "rv20": FX_VOL_FORMULA,
            "rv60": FX_VOL_FORMULA,
            "change": "RV20_t - RV20_t-1",
        },
        "input_run": {
            "id": run.pk,
            "source": run.source.key,
            "dataset": run.dataset,
            "batch_id": str(run.batch_id),
            "row_count": run.row_count,
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        },
        "component_value_dates": {
            series_key: maps[series_key][latest_date].value_date.isoformat()
            for series_key in FX_VOL_REQUIRED_SERIES
        },
        "component_fetched_at": component["fetched_at"].isoformat(),
        "acquisition_artifact": {
            "artifact_id": component["artifact"].pk,
            "private_uri": component["artifact"].uri,
            "sha256": component["artifact_sha256"],
            "size_bytes": component["artifact_size"],
            "member_name": component["member_name"],
            "member_sha256": component["member_sha256"],
            "member_size_bytes": component["member_size"],
        },
        "license_decisions": [
            {
                "source_key": key,
                "license_id": licenses[key].pk,
                "scope": licenses[key].scope,
                "public_display_allowed": True,
                "derived_display_allowed": True,
                "historical_storage_allowed": True,
            }
            for key in ("federal-reserve", "internal")
        ],
        "required_metric_keys": sorted(FX_VOL_REQUIRED_METRIC_KEYS),
        "required_chart_keys": sorted(FX_VOL_REQUIRED_CHART_KEYS),
        "required_section_keys": sorted(FX_VOL_REQUIRED_SECTION_KEYS),
        "semantic_boundary": FX_VOL_SEMANTIC_BOUNDARY,
        "fingerprint": "",
        "payload_integrity_hash": "",
    }
    payload["fingerprint"] = _sha256(_semantic_projection(payload))
    payload["payload_integrity_hash"] = _sha256(_integrity_projection(payload))
    return payload, maps[FX_VOL_REQUIRED_SERIES[0]][latest_date].as_of


def _metric_metadata(
    metric: dict[str, Any],
    *,
    fingerprint: str,
    payload_integrity_hash: str,
) -> dict[str, Any]:
    return {
        **deepcopy(metric["metadata"]),
        "source_keys": metric["source_keys"],
        "fallback_source": metric["fallback_source"],
        "publication_fingerprint": fingerprint,
        "payload_integrity_hash": payload_integrity_hash,
        "public_snapshot": True,
    }


def _metric_rows_match(snapshot: DashboardSnapshot, data: dict[str, Any]) -> bool:
    stored = {
        row.key: row
        for row in MetricSnapshot.objects.filter(batch_id=snapshot.batch_id)
    }
    expected_keys = {f"fx-vol-{item['key']}" for item in data["metrics"]}
    if set(stored) != expected_keys:
        return False
    for metric in data["metrics"]:
        row = stored.get(f"fx-vol-{metric['key']}")
        if row is None:
            return False
        value_date = _parse_datetime(metric["value_date"])
        as_of = _parse_datetime(metric["as_of"])
        fetched_at = _parse_datetime(metric["fetched_at"])
        if (
            value_date is None
            or as_of is None
            or fetched_at is None
            or row.label != metric["label"]
            or row.value != Decimal(str(metric["value"])).quantize(Decimal("0.00000001"))
            or row.display_value != metric["display_value"]
            or row.change != Decimal(str(metric["change"])).quantize(Decimal("0.000001"))
            or row.unit != metric["unit"][:30]
            or row.value_date != value_date
            or row.as_of != as_of
            or row.fetched_at != fetched_at
            or row.source.key != "internal"
            or row.fallback_source_id is not None
            or row.quality_status != Observation.Quality.ESTIMATED
            or row.license_scope != metric["license_scope"][:120]
            or row.metadata
            != _metric_metadata(
                metric,
                fingerprint=data["fingerprint"],
                payload_integrity_hash=data["payload_integrity_hash"],
            )
        ):
            return False
    return True


def _payload_shape_is_exact(data: dict[str, Any]) -> bool:
    expected_keys = set(FX_VOL_PAYLOAD_KEYS)
    if "refresh_failure" in data:
        expected_keys.add("refresh_failure")
    if (
        set(data) != expected_keys
        or data.get("contract_version") != FX_VOL_CONTRACT_VERSION
        or data.get("formula_version") != FX_VOL_FORMULA_VERSION
        or data.get("semantic_boundary") != FX_VOL_SEMANTIC_BOUNDARY
        or data.get("required_metric_keys") != sorted(FX_VOL_REQUIRED_METRIC_KEYS)
        or data.get("required_chart_keys") != sorted(FX_VOL_REQUIRED_CHART_KEYS)
        or data.get("required_section_keys") != sorted(FX_VOL_REQUIRED_SECTION_KEYS)
        or data.get("demo") is not False
        or set(item.get("key") for item in data.get("metrics", []))
        != set(FX_VOL_REQUIRED_METRIC_KEYS)
        or set(item.get("key") for item in data.get("charts", []))
        != set(FX_VOL_REQUIRED_CHART_KEYS)
        or set(item.get("key") for item in data.get("sections", []))
        != set(FX_VOL_REQUIRED_SECTION_KEYS)
        or any(len(item.get("data", [])) != FX_VOL_HISTORY_POINTS for item in data.get("charts", []))
        or data.get("chart_data") != data.get("charts", [{}])[0].get("data")
        or data.get("fingerprint") != _sha256(_semantic_projection(data))
        or data.get("payload_integrity_hash") != _sha256(_integrity_projection(data))
    ):
        return False
    section_map = {item["key"]: item for item in data["sections"]}
    contracts = (
        ("latest-h10-realized-volatility", FX_VOL_LATEST_COLUMNS, 4),
        ("h10-rv-source-methodology", FX_VOL_METHOD_COLUMNS, 1),
        ("licensed-fx-volatility-gaps", FX_VOL_GAP_COLUMNS, len(FX_VOL_GAPS)),
    )
    for section_key, columns, expected_rows in contracts:
        section = section_map[section_key]
        if tuple(item.get("key") for item in section.get("columns", [])) != columns:
            return False
        rows = section.get("rows", [])
        if len(rows) != expected_rows:
            return False
        for row in rows:
            if tuple(item.get("key") for item in row.get("cells_list", [])) != columns:
                return False
            if any(
                not isinstance(item.get("cell"), dict)
                or item["cell"].get("kind") != "text"
                or item["cell"].get("value") != str(row[item["key"]])
                for item in row["cells_list"]
            ):
                return False
    return True


def _embedded_run(snapshot: DashboardSnapshot) -> IngestionRun | None:
    data = snapshot.data if isinstance(snapshot.data, dict) else {}
    run_data = data.get("input_run")
    if not isinstance(run_data, dict):
        return None
    try:
        run_id = int(run_data.get("id"))
    except (TypeError, ValueError):
        return None
    return IngestionRun.objects.filter(pk=run_id).select_related("source").first()


def _base_snapshot_is_valid(snapshot: DashboardSnapshot) -> bool:
    try:
        if (
            snapshot.key != "fx-vol"
            or not snapshot.is_published
            or snapshot.title != FX_VOL_TITLE
            or snapshot.summary != FX_VOL_SUMMARY
            or snapshot.source.key != "internal"
            or snapshot.quality_status
            not in {Observation.Quality.ESTIMATED, Observation.Quality.STALE}
        ):
            return False
        data = deepcopy(snapshot.data or {})
        if not _payload_shape_is_exact(data):
            return False
        has_failure_marker = "refresh_failure" in data
        if snapshot.quality_status != (
            Observation.Quality.STALE
            if has_failure_marker
            else Observation.Quality.ESTIMATED
        ):
            return False
        run = _embedded_run(snapshot)
        if run is None:
            return False
        expected, expected_as_of = _build_fx_vol_payload(
            run,
            publication_batch_id=snapshot.batch_id,
            allow_expired=True,
        )
        data.pop("refresh_failure", None)
        return bool(
            data == expected
            and snapshot.as_of == expected_as_of
            and _metric_rows_match(snapshot, expected)
        )
    except (
        ArithmeticError,
        AttributeError,
        ElementTree.ParseError,
        IndexError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        return False


def _failure_marker_matches_latest(
    marker: Any,
    latest: IngestionRun,
) -> bool:
    if not isinstance(marker, dict) or set(marker) != {
        "checked_at",
        "reason_code",
        "attempt",
    }:
        return False
    attempt = marker.get("attempt")
    checked_at = _parse_datetime(marker.get("checked_at"))
    terminal_at = latest.completed_at or latest.started_at
    return bool(
        isinstance(attempt, dict)
        and set(attempt)
        == {"id", "status", "row_count", "batch_id", "error"}
        and attempt.get("id") == latest.pk
        and attempt.get("status") == latest.status
        and attempt.get("row_count") == latest.row_count
        and attempt.get("batch_id") == str(latest.batch_id)
        and attempt.get("error") == latest.error[:320]
        and marker.get("reason_code") == "latest-attempt-incomplete"
        and checked_at is not None
        and checked_at >= terminal_at
        and checked_at <= timezone.now()
    )


def fx_vol_snapshot_is_publicly_displayable(
    snapshot: DashboardSnapshot,
) -> bool:
    if not _base_snapshot_is_valid(snapshot):
        return False
    run = _embedded_run(snapshot)
    if run is None:
        return False
    from .official_data import _latest_h10_attempt

    latest = _latest_h10_attempt()
    if latest is None:
        return False
    if latest.pk == run.pk:
        if latest.status != IngestionRun.Status.SUCCESS or latest.row_count <= 0:
            return False
        if "refresh_failure" in (snapshot.data or {}):
            return False
        fresh_until = _parse_datetime((snapshot.data or {}).get("fresh_until"))
        if fresh_until is None:
            return False
        snapshot.fx_vol_state = (
            "natural_expiry"
            if fresh_until < timezone.now()
            else "current_candidate"
        )
        return True
    if latest.started_at < run.started_at:
        return False
    if latest.status == IngestionRun.Status.RUNNING:
        snapshot.fx_vol_state = "transition_pending"
        return True
    if latest.status in {IngestionRun.Status.FAILED, IngestionRun.Status.PARTIAL} or latest.row_count <= 0:
        if not _failure_marker_matches_latest(
            (snapshot.data or {}).get("refresh_failure"), latest
        ):
            return False
        snapshot.fx_vol_state = "retained_failure"
        return True
    return False


def select_public_fx_vol_snapshot(
    candidates: Iterable[DashboardSnapshot] | None = None,
) -> DashboardSnapshot | None:
    rows = candidates
    if rows is None:
        rows = (
            DashboardSnapshot.objects.filter(
                key="fx-vol",
                is_published=True,
                data__contract_version=FX_VOL_CONTRACT_VERSION,
            )
            .exclude(source__key="demo-market")
            .select_related("source")
            .order_by("-created_at", "-id")[:50]
        )
    for candidate in rows:
        try:
            if fx_vol_snapshot_is_publicly_displayable(candidate):
                state = getattr(candidate, "fx_vol_state", None)
                presented = deepcopy(candidate)
                presented.data = deepcopy(candidate.data or {})
                if state != "retained_failure":
                    presented.data.pop("refresh_failure", None)
                if state != "current_candidate":
                    presented.quality_status = Observation.Quality.STALE
                presented.fx_vol_state = state
                return presented
        except (
            ArithmeticError,
            AttributeError,
            ElementTree.ParseError,
            IndexError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            zipfile.BadZipFile,
        ):
            continue
    return None


def _store_snapshot(run: IngestionRun) -> DashboardSnapshot:
    publication_batch_id = uuid.uuid4()
    payload, snapshot_as_of = _build_fx_vol_payload(
        run,
        publication_batch_id=publication_batch_id,
        allow_expired=False,
    )
    internal = Source.objects.get(key="internal")
    internal_license = _current_license(internal)
    snapshot = DashboardSnapshot.objects.create(
        key="fx-vol",
        title=FX_VOL_TITLE,
        as_of=snapshot_as_of,
        batch_id=publication_batch_id,
        quality_status=Observation.Quality.ESTIMATED,
        summary=FX_VOL_SUMMARY,
        data=payload,
        source=internal,
        is_published=True,
    )
    for metric in payload["metrics"]:
        MetricSnapshot.objects.create(
            key=f"fx-vol-{metric['key']}",
            label=metric["label"],
            value=Decimal(str(metric["value"])).quantize(Decimal("0.00000001")),
            display_value=metric["display_value"],
            change=Decimal(str(metric["change"])).quantize(Decimal("0.000001")),
            unit=metric["unit"][:30],
            value_date=_parse_datetime(metric["value_date"]),
            as_of=_parse_datetime(metric["as_of"]),
            fetched_at=_parse_datetime(metric["fetched_at"]),
            batch_id=publication_batch_id,
            source=internal,
            fallback_source=None,
            quality_status=Observation.Quality.ESTIMATED,
            license_scope=internal_license.scope[:120],
            metadata=_metric_metadata(
                metric,
                fingerprint=payload["fingerprint"],
                payload_integrity_hash=payload["payload_integrity_hash"],
            ),
        )
    return snapshot


def _mark_retained_stale(latest_attempt: IngestionRun, *, reason_code: str) -> None:
    candidates = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="fx-vol",
            is_published=True,
            data__contract_version=FX_VOL_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")[:50]
    )
    retained = next(
        (
            candidate
            for candidate in candidates
            if (embedded := _embedded_run(candidate)) is not None
            and embedded.pk != latest_attempt.pk
            and _base_snapshot_is_valid(candidate)
        ),
        None,
    )
    if retained is None:
        return
    data = deepcopy(retained.data or {})
    data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason_code": reason_code,
        "attempt": {
            "id": latest_attempt.pk,
            "status": latest_attempt.status,
            "row_count": latest_attempt.row_count,
            "batch_id": str(latest_attempt.batch_id),
            "error": latest_attempt.error[:320],
        },
    }
    retained.data = data
    retained.quality_status = Observation.Quality.STALE
    retained.save(update_fields=["data", "quality_status", "updated_at"])


@transaction.atomic
def coordinate_fx_vol_dashboard(
    trigger_runs: Iterable[IngestionRun] = (),
) -> tuple[list[DashboardSnapshot], set[str]]:
    """Publish fx-vol from the latest exact H.10 run or retain visibly stale."""

    for source_key in ("federal-reserve", "internal"):
        ensure_source(source_key)
    list(
        Source.objects.select_for_update()
        .filter(key__in={"federal-reserve", "internal"})
        .order_by("key")
        .values_list("pk", flat=True)
    )
    list(
        SourceLicense.objects.select_for_update()
        .filter(
            source__key__in={"federal-reserve", "internal"},
            is_current=True,
        )
        .order_by("source__key")
        .values_list("pk", flat=True)
    )
    from .official_data import _latest_h10_attempt

    relevant = [
        run
        for run in trigger_runs
        if run.source.key == "federal-reserve" and run.dataset == "h10"
    ]
    latest = _latest_h10_attempt(lock=True)
    if relevant and (latest is None or any(run.pk != latest.pk for run in relevant)):
        return [], set()
    if latest is None:
        return [], {"fx-vol"}
    IngestionRun.objects.select_for_update().get(pk=latest.pk)
    if latest.status == IngestionRun.Status.RUNNING:
        return [], {"fx-vol"}
    if latest.status != IngestionRun.Status.SUCCESS or latest.row_count <= 0:
        _mark_retained_stale(latest, reason_code="latest-attempt-incomplete")
        return [], {"fx-vol"}
    candidates = list(
        DashboardSnapshot.objects.select_for_update()
        .filter(
            key="fx-vol",
            is_published=True,
            data__contract_version=FX_VOL_CONTRACT_VERSION,
        )
        .exclude(source__key="demo-market")
        .select_related("source")
        .order_by("-created_at", "-id")[:50]
    )
    list(
        MetricSnapshot.objects.select_for_update()
        .filter(batch_id__in=[candidate.batch_id for candidate in candidates])
        .order_by("batch_id", "key")
        .values_list("pk", flat=True)
    )
    existing = next(
        (
            candidate
            for candidate in candidates
            if (embedded := _embedded_run(candidate)) is not None
            and embedded.pk == latest.pk
            and _base_snapshot_is_valid(candidate)
            and "refresh_failure" not in (candidate.data or {})
        ),
        None,
    )
    if existing is not None:
        return [], set()
    try:
        with transaction.atomic():
            published = _store_snapshot(latest)
            current = _latest_h10_attempt(lock=True)
            if current is None or current.pk != latest.pk:
                raise ValueError("H.10 run was superseded before fx-vol commit")
            selected = select_public_fx_vol_snapshot()
            if (
                selected is None
                or selected.pk != published.pk
                or getattr(selected, "fx_vol_state", None) != "current_candidate"
            ):
                raise ValueError("fx-vol publication postcondition failed")
    except (
        ArithmeticError,
        ElementTree.ParseError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
    ):
        _mark_retained_stale(latest, reason_code="candidate-or-publication-validation")
        return [], {"fx-vol"}
    return [published], set()
