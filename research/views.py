from __future__ import annotations

import json
from copy import copy, deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.postgres.search import (
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramSimilarity,
)
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Count, F, Max, Min, Prefetch, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLPattern, URLResolver
from django.utils import timezone

from .ai_glossary_catalog import AI_GLOSSARY_TERM_SLUGS
from .calculations import percentile_rank
from .models import (
    CFTCPosition,
    CodingAgentProfile,
    Company,
    DashboardSnapshot,
    DataRequirement,
    FedDocument,
    FundLetter,
    GitHubProject,
    GlossaryTerm,
    Instrument,
    MarketBar,
    ModelProfile,
    NewsItem,
    Observation,
    OptionContract,
    ResearchMention,
    Source,
    SourceLicense,
    SupplyChainEdge,
    SupplyChainNode,
    Thesis,
)
from .official_data import (
    _inflation_market_expectations_from_real_rates,
    select_public_assets_fx_snapshot,
    select_public_credit_snapshot,
    select_public_credit_spreads_snapshot,
    select_public_credit_stress_snapshot,
    select_public_fed_balance_sheet_snapshot,
    select_public_global_dollar_snapshot,
    select_public_operations_snapshot,
    select_public_reserves_rate_spreads_snapshot,
    select_public_reserves_snapshot,
    select_public_subsurface_snapshot,
    select_public_transmission_chain_snapshot,
    select_public_treasury_curve_snapshot,
)
from .page_registry import get_page_config
from .public_ai_contract import is_pending_ai_company_contract_slug
from .sec_company_facts import (
    REVIEWED_COMPANIES,
    REVIEWED_COMPANY_CIKS,
    REVIEWED_COMPANY_SLUGS,
    is_exact_reviewed_sec_company,
    select_public_supply_chain_demand_snapshot,
)
from .services import (
    derived_display_license_q,
    public_display_license_q,
    public_source_notices,
    publicly_displayable_source_keys,
)
from .thesis_publication import public_theses
from .volatility_contract import select_public_fx_vol_snapshot


def _breadcrumbs(*items):
    return [{"label": label, "url": url} for label, url in items]


def _snapshot_source_keys(data):
    if isinstance(data, dict):
        keys = {str(data["source_key"])} if data.get("source_key") else set()
        for fallback_field in ("fallback_source", "fallback_source_key"):
            if data.get(fallback_field):
                keys.add(str(data[fallback_field]))
        keys.update(str(key) for key in data.get("source_keys", []) if key)
        keys.update(str(key) for key in data.get("_source_keys", []) if key)
        for value in data.values():
            keys.update(_snapshot_source_keys(value))
        return keys
    if isinstance(data, list):
        keys = set()
        for value in data:
            keys.update(_snapshot_source_keys(value))
        return keys
    return set()


def _chart_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _slice_dashboard_chart(chart: dict, *, months: int, fiscal_years: int | None = None) -> dict:
    """Copy and calendar-slice an explicitly date-indexed chart."""

    sliced = dict(chart)
    if sliced.get("time_axis") != "date":
        return sliced
    data = sliced.get("data")
    if isinstance(data, list):
        dates = [_chart_date(row.get("date")) for row in data if isinstance(row, dict)]
        if len(dates) != len(data) or not dates or any(item is None for item in dates):
            return sliced
        latest = max(item for item in dates if item is not None)
        cutoff = latest - relativedelta(months=months)
        sliced["data"] = [
            dict(row)
            for row, row_date in zip(data, dates, strict=True)
            if row_date is not None and row_date >= cutoff
        ]
        return sliced
    if not isinstance(data, dict) or not isinstance(data.get("_rows"), list):
        return sliced
    rows = data["_rows"]
    if fiscal_years and all(isinstance(row, dict) and row.get("fiscal_year") is not None for row in rows):
        selected_years = sorted({int(row["fiscal_year"]) for row in rows}, reverse=True)[:fiscal_years]
        indices = [index for index, row in enumerate(rows) if int(row["fiscal_year"]) in selected_years]
    else:
        indices = None
    dates = [_chart_date(row.get("date")) for row in rows if isinstance(row, dict)]
    if (indices is None and (len(dates) != len(rows) or not dates or any(item is None for item in dates))):
        return sliced
    if indices is None:
        latest = max(item for item in dates if item is not None)
        cutoff = latest - relativedelta(months=months)
        indices = [index for index, row_date in enumerate(dates) if row_date is not None and row_date >= cutoff]
    copied_data = dict(data)
    copied_data["_rows"] = [dict(rows[index]) for index in indices]
    if isinstance(data.get("series"), list):
        copied_series = []
        for item in data["series"]:
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            values = copied.get("data")
            if isinstance(values, list):
                copied["data"] = [values[index] for index in indices if index < len(values)]
            lineage = copied.get("lineage")
            if isinstance(lineage, list):
                copied["lineage"] = [
                    lineage[index] for index in indices if index < len(lineage)
                ]
            copied_series.append(copied)
        copied_data["series"] = copied_series
    labels = data.get("labels")
    if isinstance(labels, list) and len(labels) == len(rows):
        copied_data["labels"] = [labels[index] for index in indices]
    sliced["data"] = copied_data
    return sliced


def _apply_dashboard_controls(request, config: dict, charts: list[dict]) -> list[dict]:
    period_options = [
        item
        for item in config.get("period_options", [])
        if isinstance(item, dict)
        and item.get("value")
        and isinstance(item.get("months"), int)
    ]
    tab_options = [
        item
        for item in config.get("tab_options", [])
        if isinstance(item, dict) and item.get("value")
    ]
    valid_periods = {str(item["value"]): item for item in period_options}
    valid_tabs = {str(item["value"]): item for item in tab_options}
    default_period = str(config.get("default_period") or "")
    default_tab = str(config.get("default_tab") or "")
    if default_period not in valid_periods:
        default_period = next(iter(valid_periods), "")
    if default_tab not in valid_tabs:
        default_tab = next(iter(valid_tabs), "")
    requested_period = str(request.GET.get("period") or "")
    requested_tab = str(request.GET.get("tab") or "")
    selected_period = (
        requested_period if requested_period in valid_periods else default_period
    )
    selected_tab = requested_tab if requested_tab in valid_tabs else default_tab
    config["period_options"] = period_options
    config["tab_options"] = tab_options
    config["selected_period"] = selected_period
    config["selected_tab"] = selected_tab

    filtered = [dict(chart) for chart in charts]
    if selected_period:
        months = int(valid_periods[selected_period]["months"])
        fiscal_years = valid_periods[selected_period].get("fiscal_years")
        filtered = [_slice_dashboard_chart(chart, months=months, fiscal_years=fiscal_years) for chart in filtered]
    if selected_tab:
        allowed_keys = {
            str(key) for key in valid_tabs[selected_tab].get("chart_keys", []) if key
        }
        if allowed_keys:
            tabbed = [
                chart for chart in filtered if chart.get("key") in allowed_keys
            ]
            if tabbed:
                filtered = tabbed
            else:
                config["selected_tab"] = default_tab
        elif selected_tab != default_tab:
            config["selected_tab"] = default_tab
    return filtered


_ASSETS_FX_CHART_DISPLAY_FIELDS = {
    "fx-broad-dollar-history": (
        "date",
        "Nominal Broad Dollar Index",
    ),
    "fx-major-reference-rates-usd-strength-rebased": (
        "date",
        "EUR reciprocal USD strength",
        "CNY per USD",
        "JPY per USD",
    ),
}


def _assets_fx_charts_for_presentation(charts: list[dict]) -> list[dict]:
    """Keep audited chart lineage in storage but expose display series only."""

    presented = []
    for raw_chart in charts:
        if not isinstance(raw_chart, dict):
            continue
        fields = _ASSETS_FX_CHART_DISPLAY_FIELDS.get(str(raw_chart.get("key") or ""))
        if fields is None:
            continue
        chart = deepcopy(raw_chart)
        chart["data"] = [
            {field: row[field] for field in fields}
            for row in raw_chart.get("data", [])
            if isinstance(row, dict) and all(field in row for field in fields)
        ]
        presented.append(chart)
    return presented


_FX_VOL_CHART_DISPLAY_FIELDS = {
    "h10-fx-realized-volatility-20d": (
        "date",
        "H.10 Broad Dollar",
        "H.10 EUR/USD",
        "H.10 USD/CNY",
        "H.10 USD/JPY",
    ),
    "h10-fx-realized-volatility-60d": (
        "date",
        "H.10 Broad Dollar",
        "H.10 EUR/USD",
        "H.10 USD/CNY",
        "H.10 USD/JPY",
    ),
}


def _fx_vol_charts_for_presentation(charts: list[dict]) -> list[dict]:
    """Render values only while retaining exact rolling lineage in storage."""

    presented = []
    for raw_chart in charts:
        if not isinstance(raw_chart, dict):
            continue
        fields = _FX_VOL_CHART_DISPLAY_FIELDS.get(str(raw_chart.get("key") or ""))
        if fields is None:
            continue
        chart = deepcopy(raw_chart)
        chart["data"] = [
            {field: row[field] for field in fields}
            for row in raw_chart.get("data", [])
            if isinstance(row, dict) and all(field in row for field in fields)
        ]
        presented.append(chart)
    return presented


def _credit_charts_for_presentation(charts: list[dict]) -> list[dict]:
    """Project each audited chart to its declared axis and numeric series only."""

    presented = []
    for raw_chart in charts:
        if not isinstance(raw_chart, dict):
            continue
        chart = deepcopy(raw_chart)
        x_key = str(chart.get("x_key") or chart.get("time_axis") or "date")
        series_keys = [
            str(key)
            for key in chart.get("series_keys", [])
            if isinstance(key, str) and key and key != x_key
        ]
        display_x_key = x_key if x_key in {"date", "label", "name"} else "label"
        projected_rows = []
        for row in chart.get("data", []):
            if not isinstance(row, dict) or x_key not in row:
                continue
            projected = {display_x_key: row[x_key]}
            for series_key in series_keys:
                value = row.get(series_key)
                if isinstance(value, (int, float, Decimal)) and not isinstance(
                    value, bool
                ):
                    projected[series_key] = value
            if len(projected) == len(series_keys) + 1:
                projected_rows.append(projected)
        chart["data"] = projected_rows
        chart["presentation_x_key"] = display_x_key
        presented.append(chart)
    return presented


def _public_theses(*, limit: int | None = None):
    """Return only reports that pass the shared publication-safety contract."""

    return public_theses(limit=limit)


def _thesis_evidence_rows(thesis: Thesis | None) -> list[dict[str, Any]]:
    if thesis is None:
        return []
    snapshot_data = (
        thesis.source_snapshot.data
        if thesis.source_snapshot_id and isinstance(thesis.source_snapshot.data, dict)
        else {}
    )
    frozen_items = snapshot_data.get("evidence_items")
    frozen_by_id: dict[int, dict[str, Any]] = {}
    if isinstance(frozen_items, list):
        for frozen in frozen_items:
            if not isinstance(frozen, dict):
                continue
            metric_id = frozen.get("metric_id")
            if isinstance(metric_id, bool):
                continue
            try:
                normalized_id = int(metric_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if 0 < normalized_id <= (2**63) - 1:
                frozen_by_id[normalized_id] = frozen
    rows: list[dict[str, Any]] = []
    for item in thesis.evidence_items.all():
        if item.snapshot_id is None:
            continue
        frozen = frozen_by_id.get(item.snapshot_id)
        if frozen is None:
            continue
        frozen_display = frozen.get("display_value")
        if frozen_display not in (None, ""):
            display_value = str(frozen_display)
        elif frozen.get("value") is not None:
            display_value = str(frozen["value"])
        else:
            display_value = "—"
        rows.append(
            {
                "label": item.label,
                "body": item.body,
                "value": display_value,
                "value_date": _public_datetime(frozen.get("value_date")),
                "fetched_at": _public_datetime(frozen.get("fetched_at")),
                "batch_id": frozen.get("batch_id"),
                "quality_status": frozen.get("quality_status"),
                "license_scope": frozen.get("license_scope"),
                "source_key": frozen.get("source_key"),
                "source_name": item.source.name,
                "source_url": item.source_url,
            }
        )
    return rows


def _public_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, ZoneInfo("UTC"))
    return parsed


def _thesis_source_keys(thesis: Thesis | None) -> set[str]:
    if thesis is None or thesis.source_snapshot_id is None:
        return set()
    data = thesis.source_snapshot.data
    keys = {thesis.source_snapshot.source.key}
    if isinstance(data, dict) and isinstance(data.get("source_keys"), list):
        keys.update(str(item) for item in data["source_keys"] if isinstance(item, str) and item)
    if isinstance(data, dict) and isinstance(data.get("evidence_items"), list):
        keys.update(
            str(item["source_key"])
            for item in data["evidence_items"]
            if isinstance(item, dict) and isinstance(item.get("source_key"), str)
        )
    return keys


def _thesis_snapshot_metadata(thesis: Thesis) -> dict[str, Any]:
    snapshot = thesis.source_snapshot
    data = snapshot.data if isinstance(snapshot.data, dict) else {}
    deadlines: list[datetime] = []
    evidence_items = data.get("evidence_items")
    stack: list[tuple[Any, int]] = [
        (data.get("component_snapshots"), 0),
        (evidence_items, 0),
    ]
    visited = 0
    while stack and visited < 20_000:
        current, depth = stack.pop()
        visited += 1
        if depth > 32:
            continue
        if isinstance(current, dict):
            if "fresh_until" in current:
                deadline = _public_datetime(current.get("fresh_until"))
                if deadline is not None:
                    deadlines.append(deadline)
            stack.extend(
                (nested, depth + 1)
                for nested in current.values()
                if isinstance(nested, (dict, list))
            )
        elif isinstance(current, list):
            stack.extend(
                (nested, depth + 1)
                for nested in current
                if isinstance(nested, (dict, list))
            )
    licence_scopes = sorted(
        {
            str(item.get("license_scope"))
            for item in evidence_items or []
            if isinstance(item, dict) and item.get("license_scope")
        }
    ) if isinstance(evidence_items, list) else []
    return {
        "id": snapshot.pk,
        "batch_id": snapshot.batch_id,
        "as_of": snapshot.as_of,
        "quality_status": snapshot.quality_status,
        "source_name": snapshot.source.name,
        "license_scope": "；".join(licence_scopes),
        "stale": bool(deadlines and min(deadlines) < timezone.now()),
    }


def _public_news_items():
    return NewsItem.objects.exclude(source_url__icontains="example.com").exclude(
        license_status__in=["synthetic", "restricted", "blocked"]
    )


def _public_research_mentions():
    return ResearchMention.objects.filter(review_status="reviewed").exclude(
        source_url__icontains="example.com"
    )


def _public_fund_letters():
    return FundLetter.objects.exclude(original_url__icontains="example.com").exclude(
        license_status__in=["synthetic", "restricted", "blocked"]
    )


def _public_fed_documents():
    return (
        FedDocument.objects.filter(
            Q(original_url__istartswith="https://www.federalreserve.gov/")
            | Q(original_url__istartswith="https://federalreserve.gov/")
        )
        .exclude(slug__startswith="clean-room-fed-document-")
        .exclude(original_url__icontains="example.com")
    )


def _public_glossary_terms():
    return GlossaryTerm.objects.exclude(source_url__icontains="example.com").exclude(
        slug__startswith="clean-room-term-"
    )


def _public_ai_glossary_terms():
    return _public_glossary_terms().filter(slug__in=AI_GLOSSARY_TERM_SLUGS)


def _public_supply_chain_nodes():
    return (
        SupplyChainNode.objects.exclude(source_note="")
        .exclude(slug__startswith="clean-room-node-")
        .exclude(source_note__icontains="合成演示")
    )


def _public_companies():
    reviewed_identity = Q(
        slug__in=REVIEWED_COMPANY_SLUGS
    ) | Q(sec_cik__in=REVIEWED_COMPANY_CIKS)
    exact_reviewed_identity = Q()
    for spec in REVIEWED_COMPANIES:
        exact_reviewed_identity |= Q(
            slug=spec.slug,
            sec_cik=spec.normalized_cik,
            source__key="sec",
        )
    return (
        Company.objects.filter(
            is_published=True,
            publication_batch_id__isnull=False,
            source__isnull=False,
        )
        .filter(public_display_license_q("source__licenses"))
        .exclude(data_source_note="")
        .exclude(slug__startswith="clean-room-company-")
        .exclude(data_source_note__icontains="合成演示")
        .exclude(investor_relations_url__icontains="example.com")
        .exclude(quality_status=Observation.Quality.ERROR)
        .filter(~reviewed_identity | exact_reviewed_identity)
        .distinct()
    )


def _public_model_profiles():
    return (
        ModelProfile.objects.exclude(sources=[])
        .exclude(slug__startswith="clean-room-model-")
        .order_by(
            F("capability_score").desc(nulls_last=True),
            F("release_date").desc(nulls_last=True),
            "name",
        )
    )


def _public_coding_agents():
    return (
        CodingAgentProfile.objects.exclude(homepage="")
        .exclude(slug__startswith="clean-room-coding-agent-")
        .exclude(homepage__icontains="example.com")
        .order_by(
            F("capability_score").desc(nulls_last=True),
            F("release_date").desc(nulls_last=True),
            "name",
        )
    )


def _public_github_projects():
    return (
        GitHubProject.objects.exclude(repo__startswith="atlas-clean-room/")
        .exclude(homepage__icontains="example.com")
        .filter(public_display_license_q())
        .distinct()
    )


def _text_search(queryset, query: str, fields: list[str], similarity_field: str):
    """Use PostgreSQL FTS/trigram in production and deterministic LIKE in SQLite."""

    if not query:
        return queryset
    if connection.vendor == "postgresql":
        vector = SearchVector(*fields, config="simple")
        search_query = SearchQuery(query, config="simple", search_type="plain")
        return (
            queryset.annotate(
                _search_rank=SearchRank(vector, search_query),
                _trigram_rank=TrigramSimilarity(similarity_field, query),
            )
            .filter(Q(_search_rank__gte=0.01) | Q(_trigram_rank__gte=0.1))
            .order_by("-_search_rank", "-_trigram_rank")
        )
    condition = Q()
    for field in fields:
        condition |= Q(**{f"{field}__icontains": query})
    return queryset.filter(condition)


def _latest_observation(symbol: str):
    return (
        Observation.objects.filter(instrument__symbol=symbol)
        .exclude(source__key="demo-market")
        .filter(public_display_license_q())
        .select_related("instrument", "source", "fallback_source")
        .distinct()
        .order_by("-value_date")
        .first()
    )


def _market_card(symbol: str, fallback_name: str):
    obs = _latest_observation(symbol)
    if not obs:
        return {
            "symbol": symbol,
            "name": fallback_name,
            "value": "—",
            "change": "—",
            "as_of": "等待首批数据",
            "source": "未连接",
            "status": "stale",
        }
    previous = (
        Observation.objects.filter(instrument=obs.instrument, value_date__lt=obs.value_date)
        .exclude(source__key="demo-market")
        .filter(public_display_license_q())
        .distinct()
        .order_by("-value_date")
        .first()
    )
    change = None
    if previous and previous.value:
        change = (obs.value - previous.value) / previous.value * Decimal("100")
    return {
        "symbol": symbol,
        "name": obs.instrument.name,
        "value": f"{obs.value:,.2f}",
        "change": f"{change:+.2f}%" if change is not None else "—",
        "as_of": obs.as_of,
        "source": obs.source.name,
        "status": obs.quality_status,
    }


def home(request):
    theses = _public_theses(limit=1)
    thesis = theses[0] if theses else None
    market_cards = [
        _market_card("SPY", "标普 500 ETF"),
        _market_card("QQQ", "纳斯达克 100 ETF"),
        _market_card("TLT", "长久期美债"),
        _market_card("HYG", "高收益信用"),
        _market_card("CL=F", "WTI 原油"),
        _market_card("BTC-USD", "比特币"),
    ]
    evidence_rows = _thesis_evidence_rows(thesis)
    trigger_items = list(thesis.trigger_items.all()) if thesis else []
    invalidation_record = thesis.invalidation_record if thesis else None
    source_snapshot = thesis.source_snapshot if thesis else None
    snapshot_metadata = _thesis_snapshot_metadata(thesis) if thesis else None
    context = {
        "title": "今日跨资产判断",
        "today": timezone.localdate(),
        "thesis": thesis,
        "current_thesis": thesis,
        "evidence": [
            {
                "label": item["label"],
                "value": item["value"],
                "detail": item["body"],
            }
            for item in evidence_rows[:3]
        ],
        "evidence_items": evidence_rows,
        "trigger_items": trigger_items,
        "invalidation_record": invalidation_record,
        "market_cards": market_cards,
        "news_items": _public_news_items()[:5],
        "research_items": _public_research_mentions()[:4],
        "letters": _public_fund_letters()[:3],
        "breadcrumbs": [],
        "data_sources": Source.objects.exclude(key="demo-market").order_by("name")[:8],
        "as_of": source_snapshot.as_of if source_snapshot else None,
        "source": source_snapshot.source if source_snapshot else None,
        "snapshot_metadata": snapshot_metadata,
        "source_notices": public_source_notices(_thesis_source_keys(thesis)),
    }
    return render(request, "research/home.html", context)


def regime_log(request):
    theses = _public_theses()
    reviewed = [item for item in theses if item.hit_rate is not None]
    returns = [
        item.simulated_return for item in reviewed if item.simulated_return is not None
    ]
    context = {
        "title": "判断复盘账本",
        "current": theses[0] if theses else None,
        "theses": theses[:60],
        "sample_count": len(reviewed),
        "avg_hit": (
            sum((item.hit_rate for item in reviewed), Decimal("0")) / len(reviewed)
            if reviewed
            else None
        ),
        "avg_return": sum(returns, Decimal("0")) / len(returns) if returns else None,
        "breadcrumbs": _breadcrumbs(("首页", "/"), ("复盘账本", "")),
    }
    return render(request, "research/regime_log.html", context)


def daily_list(request):
    reports = list(_public_theses())
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    if query:
        needle = query.casefold()
        reports = [
            item
            for item in reports
            if needle
            in " ".join(
                [
                    item.regime,
                    item.summary,
                    *(evidence.label for evidence in item.evidence_items.all()),
                    *(evidence.body for evidence in item.evidence_items.all()),
                    *(trigger.name for trigger in item.trigger_items.all()),
                    *(trigger.condition for trigger in item.trigger_items.all()),
                ]
            ).casefold()
        ]
    valid_statuses = {value for value, _label in Thesis.Status.choices}
    if status in valid_statuses:
        reports = [item for item in reports if item.status == status]
    else:
        status = ""
    for item in reports:
        item.publication_metadata = _thesis_snapshot_metadata(item)
    page_obj = Paginator(reports, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "research/daily_list.html",
        {
            "title": "每日宏观研究报告",
            "page_obj": page_obj,
            "total_count": len(reports),
            "filters": {"q": query, "status": status},
            "status_choices": Thesis.Status.choices,
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("每日报告", "")),
        },
    )


def daily_detail(request, report_date: str):
    try:
        parsed_date = date.fromisoformat(report_date)
    except ValueError as exc:
        raise Http404("无效报告日期") from exc
    public = _public_theses()
    thesis = next((item for item in public if item.date == parsed_date), None)
    if thesis is None:
        raise Http404("报告不存在或未通过发布安全门")
    previous = next((item for item in public if item.date < thesis.date), None)
    following = next(
        (item for item in reversed(public) if item.date > thesis.date),
        None,
    )
    return render(
        request,
        "research/daily_detail.html",
        {
            "title": f"{thesis.date} · {thesis.regime}",
            "item": thesis,
            "object": thesis,
            "thesis": thesis,
            "previous": previous,
            "following": following,
            "evidence_items": _thesis_evidence_rows(thesis),
            "trigger_items": list(thesis.trigger_items.all()),
            "invalidation_record": thesis.invalidation_record,
            "snapshot_metadata": _thesis_snapshot_metadata(thesis),
            "source_notices": public_source_notices(_thesis_source_keys(thesis)),
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("每日报告", "/daily-report/"), (str(thesis.date), "")
            ),
        },
    )


def assets_overview(request):
    groups = []
    labels = {
        "equity": "美股",
        "etf": "ETF",
        "bond": "债券",
        "commodity": "商品",
        "fx": "外汇",
        "crypto": "加密货币",
    }
    for asset_class, label in labels.items():
        instruments = (
            []
            if asset_class in {"bond", "fx"}
            else list(
                Instrument.objects.filter(asset_class=asset_class)
                .filter(public_display_license_q("observations__source__licenses"))
                .exclude(observations__source__key="demo-market")
                .distinct()[:8]
            )
        )
        rows = []
        for instrument in instruments:
            observations = (
                instrument.observations.exclude(source__key="demo-market")
                .filter(public_display_license_q())
                .distinct()
            )
            latest = observations.order_by("-value_date").first()
            previous = (
                observations.filter(value_date__lt=latest.value_date if latest else timezone.now())
                .order_by("-value_date")
                .first()
            )
            change = None
            if latest and previous and previous.value:
                change = (latest.value - previous.value) / previous.value * Decimal("100")
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "name": instrument.name,
                    "value": latest.value if latest else None,
                    "change": change,
                    "value_display": (
                        f"{latest.value:,.2f}" if latest is not None else "—"
                    ),
                    "change_display": (
                        f"{change:+.2f}%" if change is not None else "—"
                    ),
                    "as_of": latest.as_of if latest else None,
                    "status": latest.quality_status if latest else "stale",
                }
            )
        groups.append(
            {
                "key": asset_class,
                "label": label,
                "rows": rows,
                "deck": "价格、日变动与组件级状态",
                "value_label": "数值",
                "change_label": "变动",
            }
        )

    bond_group = next(group for group in groups if group["key"] == "bond")
    curve_snapshot = select_public_treasury_curve_snapshot("yield-curve")
    if curve_snapshot is not None:
        curve_state = getattr(
            curve_snapshot,
            "treasury_publication_state",
            None,
        )
        curve_metrics = {
            item.get("key"): item
            for item in (curve_snapshot.data or {}).get("metrics", [])
            if isinstance(item, dict)
        }
        bond_rows = []
        for metric_key, symbol, name in (
            ("ust-2y", "UST 2Y", "财政部 2 年期 Par Yield"),
            ("ust-10y", "UST 10Y", "财政部 10 年期 Par Yield"),
            ("2s10s", "2s10s", "10Y − 2Y 曲线利差"),
            ("5s30s", "5s30s", "30Y − 5Y 曲线利差"),
        ):
            metric = curve_metrics.get(metric_key)
            if metric is None:
                continue
            change = metric.get("change")
            change_unit = str(metric.get("change_unit") or "")
            try:
                as_of = datetime.fromisoformat(
                    str(metric.get("value_date") or metric.get("as_of"))
                )
            except (TypeError, ValueError):
                as_of = curve_snapshot.as_of
            bond_rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "value": metric.get("value"),
                    "change": change,
                    "value_display": metric.get("display_value") or "—",
                    "change_display": (
                        f"{float(change):+g}{change_unit}"
                        if change is not None
                        else "—"
                    ),
                    "as_of": as_of,
                    "status": (
                        metric.get("quality_status") or "stale"
                        if curve_state == "current_candidate"
                        else Observation.Quality.STALE
                    ),
                }
            )
        bond_group.update(
            {
                "rows": bond_rows,
                "deck": (
                    "官方 Treasury 收益率与 Atlas 曲线利差；不是 ETF 行情"
                    if curve_state == "current_candidate"
                    else "保留上一版可重验 Treasury 曲线，当前状态已标记 stale"
                ),
                "value_label": "收益率 / 利差",
                "change_label": "较前值",
            }
        )
    else:
        bond_group.update(
            {
                "rows": [],
                "deck": (
                    "严格 Treasury 曲线暂不可用；债券区保持空缺，绝不回退为"
                    "债券或 ETF 价格"
                ),
                "value_label": "收益率 / 利差",
                "change_label": "较前值",
            }
        )
    fx_snapshot = select_public_assets_fx_snapshot()
    if fx_snapshot is not None:
        fx_metrics = {
            item.get("key"): item
            for item in (fx_snapshot.data or {}).get("metrics", [])
            if isinstance(item, dict)
        }
        fx_rows = []
        state = getattr(fx_snapshot, "assets_fx_state", None)
        for metric_key, symbol, name in (
            (
                "h10-broad-dollar",
                "H.10 Broad Dollar",
                "Federal Reserve H.10 Nominal Broad Dollar Index",
            ),
            (
                "h10-eurusd",
                "EUR/USD H.10",
                "Federal Reserve H.10 U.S. dollars per euro reference",
            ),
            (
                "h10-usdcny",
                "USD/CNY H.10",
                "Federal Reserve H.10 Chinese yuan per U.S. dollar reference",
            ),
            (
                "h10-usdjpy",
                "USD/JPY H.10",
                "Federal Reserve H.10 Japanese yen per U.S. dollar reference",
            ),
        ):
            metric = fx_metrics.get(metric_key)
            if metric is None:
                continue
            try:
                as_of = datetime.fromisoformat(
                    str(metric.get("value_date") or metric.get("as_of"))
                )
            except (TypeError, ValueError):
                as_of = fx_snapshot.as_of
            change = metric.get("change")
            fx_rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "value": metric.get("value"),
                    "change": change,
                    "value_display": metric.get("display_value") or "—",
                    "change_display": (
                        f"{float(change):+.2f}%" if change is not None else "—"
                    ),
                    "as_of": as_of,
                    "status": (
                        metric.get("quality_status") or "fresh"
                        if state == "current_candidate"
                        else Observation.Quality.STALE
                    ),
                }
            )
        fx_group = next(group for group in groups if group["key"] == "fx")
        fx_group.update(
            {
                "rows": fx_rows,
                "deck": (
                    "Federal Reserve H.10 daily references; not ICE DXY, "
                    "CNH or executable FX"
                ),
                "value_label": "H.10 reference level",
                "change_label": "Previous valid observation",
            }
        )
    covered_groups = sum(bool(group["rows"]) for group in groups)
    covered_components = sum(len(group["rows"]) for group in groups)
    config = {
        "title": "大类资产",
        "eyebrow": "Cross-Asset Dashboard",
        "description": "把权益、久期、信用、商品、外汇和加密放在同一证据框架下。",
        "metrics": [
            {
                "label": "已覆盖资产类别",
                "display_value": f"{covered_groups} / {len(groups)}",
                "change": "按实际可发布数据计数",
                "status": "fresh" if covered_groups else "stale",
            },
            {
                "label": "已覆盖资产组件",
                "display_value": str(covered_components),
                "change": "通过当前许可与质量校验",
                "status": "fresh" if covered_components else "stale",
            },
            {
                "label": "30 / 90D 相关性",
                "display_value": "—",
                "change": "等待授权行情完整覆盖",
                "status": "stale",
            },
        ],
        "chart_data": [],
        "analysis": (
            "证券、商品和加密行情在外部展示授权完成前保持空缺；"
            "债券组可独立展示已通过许可与质量门的官方 Treasury 收益率组件。"
        ),
        "sections": [],
        "source_notes": [
            "证券与商品行情在外部展示授权完成前保持空缺，不使用合成价格。",
            "债券组展示财政部 Par Yield 和 Atlas 曲线利差，不代表债券或 ETF 价格、久期或总回报。",
        ],
    }
    return render(
        request,
        "research/dashboard.html",
        {
            **config,
            "dashboard": {
                "title": config["title"],
                "summary": config["description"],
                "as_of": None,
                "source": "数据源采购台账",
                "quality_status": "stale",
                "data": {"chart": config["chart_data"]},
            },
            "asset_groups": groups,
            "page_key": "assets",
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("大类资产", "")),
        },
    )


def _compose_reserves_components(
    weekly_snapshot: DashboardSnapshot,
    rate_snapshot: DashboardSnapshot | None,
    *,
    missing_failure: dict[str, Any] | None,
) -> tuple[DashboardSnapshot, set[str]]:
    """Compose validated snapshots for presentation without writing either one."""

    composed = copy(weekly_snapshot)
    weekly_data = deepcopy(dict(weekly_snapshot.data or {}))
    metrics = list(weekly_data.get("metrics") or [])
    charts = list(weekly_data.get("charts") or [])
    sections = list(weekly_data.get("sections") or [])
    source_keys = set(weekly_data.get("source_keys") or [])
    presentation_failure = None
    components = [
        {
            "key": "reserves",
            "snapshot_id": weekly_snapshot.pk,
            "publication_batch_id": str(weekly_snapshot.batch_id),
            "as_of": weekly_snapshot.as_of.isoformat(),
            "created_at": weekly_snapshot.created_at.isoformat(),
            "quality_status": weekly_snapshot.quality_status,
            "common_effective_date": weekly_data.get("common_effective_date"),
            "refresh_failure": weekly_data.get("refresh_failure"),
        }
    ]
    if rate_snapshot is not None:
        rate_data = deepcopy(dict(rate_snapshot.data or {}))
        rate_metrics = list(rate_data.get("metrics") or [])
        rate_charts = list(rate_data.get("charts") or [])
        rate_is_stale = (
            rate_snapshot.quality_status == Observation.Quality.STALE
            or bool(rate_data.get("refresh_failure"))
        )
        if rate_is_stale:
            for component in [*rate_metrics, *rate_charts]:
                metadata = dict(component.get("metadata") or {})
                metadata["upstream_quality_status"] = component.get(
                    "quality_status"
                )
                component["metadata"] = metadata
                component["quality_status"] = Observation.Quality.STALE
        metrics.extend(rate_metrics)
        charts.extend(rate_charts)
        sections.extend(rate_data.get("sections") or [])
        source_keys.update(rate_data.get("source_keys") or [])
        components.append(
            {
                "key": "reserves-rate-spreads",
                "snapshot_id": rate_snapshot.pk,
                "publication_batch_id": str(rate_snapshot.batch_id),
                "as_of": rate_snapshot.as_of.isoformat(),
                "created_at": rate_snapshot.created_at.isoformat(),
                "quality_status": rate_snapshot.quality_status,
                "common_effective_date": rate_data.get("common_effective_date"),
                "refresh_failure": rate_data.get("refresh_failure"),
            }
        )
        if rate_is_stale:
            composed.quality_status = Observation.Quality.STALE
            presentation_failure = rate_data.get("refresh_failure") or {
                "reason": "日频资金利差组件为 stale。"
            }
    else:
        missing_reason = (
            str((missing_failure or {}).get("reason") or "")
            or "尚无通过 reserves-rate-spreads v1 许可与结构校验的组件快照。"
        )
        sections.append(
            {
                "key": "missing-reserves-rate-spreads",
                "title": "日频资金利差组件缺失",
                "body": missing_reason,
                "status": Observation.Quality.STALE,
                "quality_status": Observation.Quality.STALE,
                "full_width": True,
            }
        )
        for key, title in (
            ("reserves-funding-levels", "SOFR、13 周 T-bill 与 IORB"),
            (
                "reserves-sofr-tbill-spread-history",
                "SOFR−13 周 T-bill 历史",
            ),
            ("reserves-sofr-iorb-spread-history", "SOFR−IORB 历史"),
        ):
            charts.append(
                {
                    "key": key,
                    "title": title,
                    "description": "日频资金利差组件缺失；不显示占位数值。",
                    "kind": "line",
                    "data": [],
                    "source_keys": [],
                    "quality_status": Observation.Quality.STALE,
                    "missing_reason": missing_reason,
                    "time_axis": "date",
                    "frequency": "daily",
                    "tab": "funding",
                }
            )
        components.append(
            {
                "key": "reserves-rate-spreads",
                "status": "missing",
                "quality_status": Observation.Quality.STALE,
                "reason": missing_reason,
            }
        )
        composed.quality_status = Observation.Quality.STALE
        presentation_failure = {
            "reason": missing_reason,
            "component": "reserves-rate-spreads",
            "status": "missing",
        }
    weekly_data["metrics"] = metrics
    weekly_data["charts"] = charts
    weekly_data["sections"] = sections
    weekly_data["source_keys"] = sorted(source_keys)
    weekly_data["component_snapshots"] = components
    if presentation_failure is not None and not weekly_data.get("refresh_failure"):
        weekly_data["refresh_failure"] = presentation_failure
    weekly_data["required_notices"] = public_source_notices(source_keys)
    composed.data = weekly_data
    return composed, source_keys


def _transmission_sections_for_presentation(
    sections: list[dict[str, Any]],
    snapshot: DashboardSnapshot,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Overlay live parent state without mutating the hashed evidence ledger."""

    presented = deepcopy(sections)
    state = getattr(snapshot, "transmission_chain_state", None)
    marker = (snapshot.data or {}).get("refresh_failure")
    marker_attempts = (
        marker.get("attempts", []) if isinstance(marker, dict) else []
    )
    changed_components = set(
        getattr(snapshot, "transmission_chain_changed_components", ())
    )
    for section in presented:
        if section.get("key") != "layer-evidence-ledger":
            continue
        for row in section.get("rows", []):
            page_key = str(row.get("layer") or "")
            failed_attempts = [
                item
                for item in marker_attempts
                if isinstance(item, dict)
                and item.get("component_page_key") == page_key
                and (
                    item.get("status") != "success"
                    or int(item.get("row_count") or 0) <= 0
                )
            ]
            deadline = None
            try:
                deadline = datetime.fromisoformat(str(row.get("fresh_until")))
            except (TypeError, ValueError):
                pass
            if deadline is not None and deadline.tzinfo is None:
                deadline = deadline.replace(
                    tzinfo=timezone.get_current_timezone()
                )
            is_expired = deadline is not None and deadline < now
            if state == "current_candidate":
                stale = False
                refresh_state = "none"
            elif state == "natural_expiry":
                stale = is_expired
                refresh_state = "自然过期" if is_expired else "none"
            elif state == "transition_pending":
                stale = is_expired or page_key in changed_components
                refresh_state = (
                    "自然过期"
                    if is_expired
                    else "等待父页原子发布"
                    if page_key in changed_components
                    else "none"
                )
            elif state == "retained_failure":
                component_changed = page_key in changed_components
                stale = bool(failed_attempts) or is_expired or component_changed
                refresh_state = (
                    ", ".join(
                        f"{item.get('input_role')}: {item.get('status')}"
                        for item in failed_attempts
                    )
                    if failed_attempts
                    else "自然过期"
                    if is_expired
                    else str((marker or {}).get("reason_code"))
                    if component_changed
                    else "none"
                )
            else:
                stale = True
                refresh_state = ", ".join(
                    f"{item.get('input_role')}: {item.get('status')}"
                    for item in failed_attempts
                ) or str(
                    (marker or {}).get("reason_code")
                    or "父页保留上一完整版本"
                )
            row["stale"] = "true" if stale else "false"
            row["refresh_failure"] = refresh_state
            for item in row.get("cells_list", []):
                if item.get("key") in {"stale", "refresh_failure"}:
                    item["cell"]["value"] = row[item["key"]]
    return presented


def dashboard_page(request, page_key: str):
    if page_key == "fed-hawkish-dovish":
        return fed_hawkish_dovish(request)
    try:
        config = get_page_config(page_key)
    except KeyError as exc:
        raise Http404("未知仪表盘") from exc
    snapshot_key = str(config.get("snapshot_key") or page_key)
    snapshot_candidates = (
        DashboardSnapshot.objects.filter(key=snapshot_key, is_published=True)
        .filter(Q(data__demo=False) | ~Q(data__has_key="demo"))
        .exclude(source__key="demo-market")
        .select_related("source")
        # A mixed-frequency snapshot uses its oldest component for ``as_of``.
        # Publication time therefore defines snapshot recency; sorting by
        # ``as_of`` can incorrectly resurrect an older monthly-only snapshot
        # after a quarterly source is added to the latest complete batch.
        .order_by("-created_at", "-as_of")
    )
    required_contract_version = config.get("snapshot_contract_version")
    if required_contract_version is not None:
        snapshot_candidates = snapshot_candidates.filter(
            data__contract_version=required_contract_version
        )
    snapshot = None
    snapshot_source_keys: set[str] = set()
    blocked_refresh_failure = None
    stale_notice = None
    treasury_state = None
    if config.get("prose_only_contract"):
        # A prose-only licence or coverage contract never trusts database
        # snapshots.  This makes a deliberately inserted legacy/rogue number
        # ineligible on every such route, not only the original CDS page.
        snapshot = None
    elif snapshot_key == "credit-cds":
        # This route is intentionally prose-only until a licensed CDS/CDX
        # composite product is purchased.  Even a legacy/rogue snapshot is
        # never eligible for presentation.
        snapshot = None
    elif snapshot_key == "credit":
        snapshot = select_public_credit_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            selected_failure = (snapshot.data or {}).get("refresh_failure")
            if isinstance(selected_failure, dict):
                blocked_refresh_failure = selected_failure
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "credit-spreads":
        snapshot = select_public_credit_spreads_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            selected_failure = (snapshot.data or {}).get("refresh_failure")
            if isinstance(selected_failure, dict):
                blocked_refresh_failure = selected_failure
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "credit-stress":
        snapshot = select_public_credit_stress_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            selected_failure = (snapshot.data or {}).get("refresh_failure")
            if isinstance(selected_failure, dict):
                blocked_refresh_failure = selected_failure
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "supply-chain-demand":
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(candidate_failure, dict):
                blocked_refresh_failure = candidate_failure
        snapshot = select_public_supply_chain_demand_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            snapshot_source_keys = {"sec"}
    elif snapshot_key == "transmission-chain":
        snapshot = select_public_transmission_chain_snapshot(
            snapshot_candidates[:50]
        )
        if snapshot is not None:
            selected_failure = (snapshot.data or {}).get("refresh_failure")
            if isinstance(selected_failure, dict):
                blocked_refresh_failure = selected_failure
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "fed-balance-sheet":
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(
                candidate_failure, dict
            ):
                blocked_refresh_failure = candidate_failure
        snapshot = select_public_fed_balance_sheet_snapshot(
            snapshot_candidates[:50]
        )
        if snapshot is not None:
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "subsurface":
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(
                candidate_failure, dict
            ):
                blocked_refresh_failure = candidate_failure
        snapshot = select_public_subsurface_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "operations":
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(
                candidate_failure, dict
            ):
                blocked_refresh_failure = candidate_failure
        snapshot = select_public_operations_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "assets-fx":
        snapshot = select_public_assets_fx_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            if getattr(snapshot, "assets_fx_state", None) == "retained_failure":
                selected_failure = (snapshot.data or {}).get("refresh_failure")
                if isinstance(selected_failure, dict):
                    blocked_refresh_failure = selected_failure
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "fx-vol":
        snapshot = select_public_fx_vol_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            fx_vol_state = getattr(snapshot, "fx_vol_state", None)
            if fx_vol_state == "retained_failure":
                selected_failure = (snapshot.data or {}).get("refresh_failure")
                if isinstance(selected_failure, dict):
                    blocked_refresh_failure = selected_failure
            elif fx_vol_state == "natural_expiry":
                stale_notice = {
                    "reason_code": "natural-expiry",
                    "reason": (
                        "H.10 输入已自然超过声明的新鲜度窗口；页面保留最后一版"
                        "可重验快照，且没有把自然过期伪装成采集失败。"
                    ),
                }
            elif fx_vol_state == "transition_pending":
                stale_notice = {
                    "reason_code": "transition-pending",
                    "reason": (
                        "最新 H.10 刷新批次仍在运行；页面暂时保留上一版可重验"
                        "快照，这不表示采集或完整性校验已经失败。"
                    ),
                }
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key in {"yield-curve", "real-rates", "rates"}:
        snapshot = select_public_treasury_curve_snapshot(
            snapshot_key,
            snapshot_candidates[:50],
        )
        if snapshot is not None:
            treasury_state = getattr(
                snapshot,
                "treasury_publication_state",
                None,
            )
            if treasury_state == "retained_failure":
                selected_failure = (snapshot.data or {}).get("refresh_failure")
                if isinstance(selected_failure, dict):
                    blocked_refresh_failure = selected_failure
            elif treasury_state == "natural_expiry":
                stale_notice = {
                    "reason_code": "natural-expiry",
                    "reason": (
                        "Treasury 最新完整曲线已自然超过声明的新鲜度窗口；页面"
                        "保留最后一版可重验快照，且不把自然过期伪装成采集失败。"
                    ),
                }
            elif treasury_state == "transition_pending":
                stale_notice = {
                    "reason_code": "transition-pending",
                    "reason": (
                        "最新 Treasury 年度分片仍在刷新；页面暂时保留上一版"
                        "可重验快照，这不表示采集或完整性校验失败。"
                    ),
                }
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "global-dollar":
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(
                candidate_failure, dict
            ):
                blocked_refresh_failure = candidate_failure
        snapshot = select_public_global_dollar_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            snapshot_source_keys = _snapshot_source_keys(snapshot.data)
            if snapshot.source_id:
                snapshot_source_keys.add(snapshot.source.key)
    elif snapshot_key == "reserves":
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(
                candidate_failure, dict
            ):
                blocked_refresh_failure = candidate_failure
        snapshot = select_public_reserves_snapshot(snapshot_candidates[:50])
        if snapshot is not None:
            rate_candidates = list(
                DashboardSnapshot.objects.filter(
                    key="reserves-rate-spreads",
                    is_published=True,
                    data__contract_version=1,
                )
                .filter(Q(data__demo=False) | ~Q(data__has_key="demo"))
                .exclude(source__key="demo-market")
                .select_related("source")
                .order_by("-created_at", "-id")
                [:50]
            )
            rate_failure = None
            for candidate in rate_candidates:
                candidate_failure = (candidate.data or {}).get("refresh_failure")
                if rate_failure is None and isinstance(candidate_failure, dict):
                    rate_failure = candidate_failure
            rate_snapshot = select_public_reserves_rate_spreads_snapshot(
                rate_candidates
            )
            snapshot, snapshot_source_keys = _compose_reserves_components(
                snapshot,
                rate_snapshot,
                missing_failure=rate_failure,
            )
    else:
        for candidate in snapshot_candidates[:50]:
            candidate_failure = (candidate.data or {}).get("refresh_failure")
            if blocked_refresh_failure is None and isinstance(
                candidate_failure, dict
            ):
                blocked_refresh_failure = candidate_failure
            candidate_source_keys = _snapshot_source_keys(candidate.data)
            if candidate.source_id:
                candidate_source_keys.add(candidate.source.key)
            if publicly_displayable_source_keys(candidate_source_keys):
                snapshot = candidate
                snapshot_source_keys = candidate_source_keys
                break
    if snapshot:
        snapshot_data = dict(snapshot.data or {})
        if snapshot_key == "inflation":
            snapshot_data["metrics"] = [
                item
                for item in snapshot_data.get("metrics", [])
                if not str(item.get("key") or "").startswith("market-")
            ]
            snapshot_data["charts"] = [
                item
                for item in snapshot_data.get("charts", [])
                if item.get("key") != "market-breakeven-inflation"
            ]
            snapshot_data["sections"] = [
                item
                for item in snapshot_data.get("sections", [])
                if item.get("key") != "market-breakeven-methodology"
                and item.get("title") != "市场通胀预期代理口径"
            ]
            expectation_metrics, expectation_charts, expectation_sections = (
                _inflation_market_expectations_from_real_rates()
            )
            snapshot_data["metrics"] = [
                *snapshot_data["metrics"],
                *expectation_metrics,
            ]
            snapshot_data["charts"] = [
                *snapshot_data["charts"],
                *expectation_charts,
            ]
            snapshot_data["sections"] = [
                *snapshot_data["sections"],
                *expectation_sections,
            ]
            snapshot_source_keys.update(
                _snapshot_source_keys(
                    [
                        expectation_metrics,
                        expectation_charts,
                        expectation_sections,
                    ]
                )
            )
        metrics = [dict(item) for item in snapshot_data.get("metrics", [])]
        if (
            snapshot_key in {"yield-curve", "real-rates", "rates"}
            and treasury_state != "current_candidate"
        ):
            for item in metrics:
                item["quality_status"] = Observation.Quality.STALE
        if snapshot_key == "assets-fx":
            for item in metrics:
                change = item.get("change")
                if isinstance(change, (int, float, Decimal)) and not isinstance(
                    change, bool
                ):
                    item["change_display"] = f"{Decimal(str(change)):+.2f}"
        elif snapshot_key == "fx-vol":
            for item in metrics:
                change = item.get("change")
                if isinstance(change, (int, float, Decimal)) and not isinstance(
                    change, bool
                ):
                    item["change_unit"] = "pp"
                    item["change_display"] = f"{Decimal(str(change)):+.2f}"
        elif snapshot_key in {"credit", "credit-spreads", "credit-stress"}:
            for item in metrics:
                metadata = item.get("metadata")
                change = item.get("change")
                change_unit = (
                    str(metadata.get("change_unit") or "")
                    if isinstance(metadata, dict)
                    else ""
                )
                if (
                    change_unit in {"bp", "pp"}
                    and isinstance(change, (int, float, Decimal))
                    and not isinstance(change, bool)
                ):
                    item["change_unit"] = change_unit
                    item["change_display"] = f"{Decimal(str(change)):+.2f}"
        now = timezone.now()
        for item in metrics:
            fresh_until = item.get("fresh_until")
            if not fresh_until:
                continue
            try:
                deadline = datetime.fromisoformat(fresh_until)
            except (TypeError, ValueError):
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.get_current_timezone())
            if deadline < now:
                item["quality_status"] = "stale"
                snapshot.quality_status = "stale"
        snapshot_data["metrics"] = metrics
        sections = deepcopy(snapshot_data.get("sections", []))
        for section in sections:
            fresh_until = section.get("fresh_until")
            if not fresh_until:
                continue
            try:
                deadline = datetime.fromisoformat(fresh_until)
            except (TypeError, ValueError):
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.get_current_timezone())
            if deadline < now:
                section["status"] = "stale"
                snapshot.quality_status = "stale"
        if snapshot_key == "transmission-chain":
            sections = _transmission_sections_for_presentation(
                sections,
                snapshot,
                now=now,
            )
        snapshot_data["sections"] = sections
        raw_charts = snapshot_data.get("charts")
        if not isinstance(raw_charts, list) or not raw_charts:
            legacy_chart_data = snapshot_data.get("chart_data", [])
            raw_charts = [
                {
                    "key": "primary",
                    "title": "核心趋势",
                    "kind": "line",
                    "data": legacy_chart_data,
                    "source_keys": sorted(
                        _snapshot_source_keys(legacy_chart_data)
                    )
                    or snapshot_data.get("source_keys", []),
                    "as_of": snapshot.as_of.isoformat(),
                    "quality_status": snapshot.quality_status,
                }
            ]
        if snapshot_key == "assets-fx":
            raw_charts = _assets_fx_charts_for_presentation(raw_charts)
        elif snapshot_key == "fx-vol":
            raw_charts = _fx_vol_charts_for_presentation(raw_charts)
        elif snapshot_key in {"credit", "credit-spreads", "credit-stress"}:
            raw_charts = _credit_charts_for_presentation(raw_charts)
        charts = []
        allowed_chart_kinds = {
            "line",
            "bar",
            "area",
            "scatter",
            "pie",
            "gauge",
            "graph",
            "heatmap",
        }
        source_map = {
            source.key: source
            for source in Source.objects.filter(key__in=snapshot_source_keys)
            .filter(public_display_license_q("licenses"))
            .distinct()
        }
        for index, raw_chart in enumerate(raw_charts):
            if not isinstance(raw_chart, dict):
                continue
            chart = dict(raw_chart)
            chart["dom_id"] = f"dashboard-chart-{index}"
            chart["kind"] = (
                chart.get("kind")
                if chart.get("kind") in allowed_chart_kinds
                else "line"
            )
            chart_source_keys = sorted(_snapshot_source_keys(chart))
            if not chart_source_keys:
                chart_source_keys = [
                    key
                    for key in snapshot_data.get("source_keys", [])
                    if key in source_map
                ]
            chart["sources"] = [
                source_map[key] for key in chart_source_keys if key in source_map
            ]
            chart.setdefault("as_of", snapshot.as_of.isoformat())
            chart.setdefault("quality_status", snapshot.quality_status)
            fresh_until = chart.get("fresh_until")
            if fresh_until:
                try:
                    deadline = datetime.fromisoformat(fresh_until)
                except (TypeError, ValueError):
                    deadline = None
                if deadline is not None:
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(
                            tzinfo=timezone.get_current_timezone()
                        )
                    if deadline < now:
                        chart["quality_status"] = "stale"
                        snapshot.quality_status = "stale"
            if chart.get("quality_status") == "stale":
                snapshot.quality_status = "stale"
            charts.append(chart)
        if not charts:
            charts = [
                {
                    "dom_id": "dashboard-chart-0",
                    "title": "核心趋势",
                    "kind": "line",
                    "data": [],
                    "sources": [],
                    "as_of": snapshot.as_of.isoformat(),
                    "quality_status": snapshot.quality_status,
                }
            ]
        snapshot_data["charts"] = raw_charts
        snapshot.data = snapshot_data
        config["snapshot"] = snapshot
        config["refresh_failure"] = (
            blocked_refresh_failure
            if snapshot_key in {"assets-fx", "fx-vol"}
            else snapshot_data.get("refresh_failure")
        )
        config["stale_notice"] = stale_notice
        config["required_notices"] = public_source_notices(snapshot_source_keys)
        config["analysis"] = snapshot.summary or (
            "本批次只发布了通过许可和质量校验的数值，尚未生成经审核的信号解读。"
        )
        config["metrics"] = snapshot_data.get("metrics", [])
        config["charts"] = charts
        config["chart_data"] = charts[0].get("data", [])
        config["sections"] = sections
    elif config.get("prose_only_contract") or page_key == "credit-cds":
        config["metrics"] = []
        config["chart_data"] = []
        config["charts"] = []
        config["sections"] = deepcopy(config.get("sections", []))
        config["analysis"] = config.get("analysis") or (
            "未取得具有公开展示和历史存储权的 CDX/CDS composite 产品，"
            "本页不发布数字、替代指标或 fallback。"
        )
        config["source_notes"] = list(config.get("source_notes", []))
        config["required_notices"] = []
        config["refresh_failure"] = None
        config["stale_notice"] = None
    else:
        static_metrics = config.get("metrics", [])
        requirements = list(DataRequirement.objects.filter(page_key=page_key))
        labels = [item.get("label", "指标") for item in static_metrics]
        if not labels:
            labels = [requirement.metric_name for requirement in requirements[:6]]
        config["metrics"] = [
            {
                "label": label,
                "value": None,
                "display_value": "—",
                "change": None,
                "status": "stale",
                "source": "等待已授权数据源",
            }
            for label in labels
        ]
        config["chart_data"] = []
        config["charts"] = [
            {
                "dom_id": "dashboard-chart-0",
                "title": "核心趋势",
                "kind": "line",
                "data": [],
                "sources": [],
                "quality_status": "stale",
            }
        ]
        config["sections"] = []
        config["analysis"] = (
            "本页尚无通过来源许可与质量检查的可发布快照，也尚未生成"
            "经审核的信号解读。缺失项目和采购建议见页面下方数据覆盖台账。"
        )
        config["source_notes"] = ["没有真实数据时显示空缺，不回退到演示或合成数值。"]
        config["required_notices"] = []
        config["refresh_failure"] = blocked_refresh_failure
    config["charts"] = _apply_dashboard_controls(
        request,
        config,
        list(config.get("charts", [])),
    )
    config["chart_data"] = (
        config["charts"][0].get("data", []) if config["charts"] else []
    )
    config.update(
        {
            "page_key": page_key,
            "dashboard": {
                "title": config["title"],
                "summary": config.get("analysis") or config.get("description", ""),
                "as_of": snapshot.as_of if snapshot else None,
                "source": snapshot.source if snapshot else "数据源覆盖台账",
                "quality_status": snapshot.quality_status if snapshot else "stale",
                "data": {"chart": config.get("chart_data", [])},
            },
            "breadcrumbs": _breadcrumbs(("首页", "/"), (config["title"], "")),
        }
    )
    return render(request, "research/dashboard.html", config)


def options_view(request):
    requested_symbol = request.GET.get("symbol", "SPY").upper()
    available = list(
        Instrument.objects.filter(options__isnull=False)
        .filter(public_display_license_q("options__source__licenses"))
        .exclude(options__source__key="demo-market")
        .distinct()
        .order_by("symbol")
    )
    instrument = next((item for item in available if item.symbol.upper() == requested_symbol), None)
    if not instrument:
        instrument = available[0] if available else None
    contract_query = (
        OptionContract.objects.filter(
            instrument=instrument,
        )
        .filter(public_display_license_q())
        .exclude(source__key="demo-market")
        .distinct()
        .order_by("expiry", "strike")
        if instrument
        else OptionContract.objects.none()
    )
    expiry = request.GET.get("expiry", "").strip()
    if expiry:
        contract_query = contract_query.filter(expiry=expiry)
    contracts = list(contract_query)
    latest = _latest_observation(instrument.symbol) if instrument else None
    spot = float(latest.value) if latest else None
    net_gex = 0.0
    net_dex = 0.0
    call_wall = None
    put_wall = None
    max_call = -1
    max_put = -1
    option_rows = []
    for contract in contracts:
        sign = 1 if contract.option_type == "call" else -1
        gamma = float(contract.gamma or 0)
        delta = float(contract.delta or 0)
        if spot is None:
            continue
        gex = sign * gamma * contract.open_interest * 100 * spot * spot / 100
        dex = sign * delta * contract.open_interest * 100 * spot
        net_gex += gex
        net_dex += dex
        if contract.option_type == "call" and contract.open_interest > max_call:
            max_call = contract.open_interest
            call_wall = contract.strike
        if contract.option_type == "put" and contract.open_interest > max_put:
            max_put = contract.open_interest
            put_wall = contract.strike
        option_rows.append(
            {
                "expiry": contract.expiry,
                "strike": float(contract.strike),
                "type": contract.option_type,
                "oi": contract.open_interest,
                "volume": contract.volume,
                "iv": float(contract.implied_volatility or 0),
                "gex": gex,
                "dex": dex,
            }
        )
    gamma_state = (
        "等待授权期权链" if not option_rows else "正 Gamma" if net_gex >= 0 else "负 Gamma"
    )
    context = {
        "title": "期权市场结构",
        "instrument": instrument,
        "selected_symbol": requested_symbol,
        "available_symbols": available,
        "spot": spot,
        "contracts": contracts,
        "option_rows": option_rows,
        "chart_data": option_rows,
        "net_gex": net_gex if option_rows else None,
        "net_dex": net_dex if option_rows else None,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_state": gamma_state,
        "tabs": ["GEX 分布", "DEX", "IV 期限结构", "Skew", "Vanna / Charm", "OI 热力", "结构解读"],
        "expiries": sorted({contract.expiry for contract in contracts}),
        "breadcrumbs": _breadcrumbs(("首页", "/"), ("大类资产", "/assets/"), ("期权 / GEX", "")),
    }
    return render(request, "research/options.html", context)


def positioning_view(request):
    report_types = [
        ("tff-futures", "TFF Futures Only"),
        ("tff-combined", "TFF Futures + Options"),
    ]
    allowed_reports = {value for value, _ in report_types}
    report = request.GET.get("report", "tff-futures").strip() or "tff-futures"
    if report not in allowed_reports:
        report = "tff-futures"
    query = request.GET.get("q", "").strip()
    trader_group = request.GET.get("group", "").strip()
    group_labels = {
        "dealer": "交易商 / 中介机构",
        "asset-manager": "资产管理机构",
        "leveraged-money": "杠杆资金",
        "other-reportables": "其他需申报交易者",
        "non-reportables": "非申报交易者",
    }
    if trader_group not in group_labels:
        trader_group = ""

    visible_rows = (
        CFTCPosition.objects.filter(report_type=report)
        .filter(public_display_license_q())
        .select_related("source")
        .distinct()
    )
    latest_date = (
        visible_rows.order_by("-report_date").values_list("report_date", flat=True).first()
    )
    latest_all = (
        visible_rows.filter(report_date=latest_date) if latest_date else visible_rows.none()
    )
    latest_rows = latest_all
    if query:
        latest_rows = latest_rows.filter(
            Q(market_name__icontains=query) | Q(market_code__icontains=query)
        )
    if trader_group:
        latest_rows = latest_rows.filter(trader_group=trader_group)
    latest_rows = list(latest_rows.order_by("-open_interest", "market_name", "trader_group")[:40])

    pairs = {(row.market_code, row.trader_group) for row in latest_rows}
    histories: dict[tuple[str, str], list[CFTCPosition]] = {pair: [] for pair in pairs}
    if pairs and latest_date:
        history_rows = visible_rows.filter(
            report_date__lte=latest_date,
            market_code__in={pair[0] for pair in pairs},
            trader_group__in={pair[1] for pair in pairs},
        ).order_by("report_date")
        for history_row in history_rows:
            pair = (history_row.market_code, history_row.trader_group)
            if pair in histories:
                histories[pair].append(history_row)

    positions = []
    for row in latest_rows:
        history = histories[(row.market_code, row.trader_group)][-156:]
        nets = [item.net_position for item in history]
        rank = percentile_rank(nets) if len(nets) >= 26 else None
        previous = history[-2].net_position if len(history) > 1 else None
        weekly_change = row.net_position - previous if previous is not None else None
        if rank is None:
            crowding = "样本不足"
            crowding_level = "unknown"
        elif rank >= 90:
            crowding = "极度净多"
            crowding_level = "extreme"
        elif rank >= 80:
            crowding = "净多拥挤"
            crowding_level = "crowded"
        elif rank <= 10:
            crowding = "极度净空"
            crowding_level = "extreme"
        elif rank <= 20:
            crowding = "净空拥挤"
            crowding_level = "crowded"
        else:
            crowding = "中性"
            crowding_level = "neutral"
        positions.append(
            {
                "name": row.market_name,
                "symbol": row.market_code,
                "group": group_labels[row.trader_group],
                "trader_group": row.trader_group,
                "long_positions": row.long_positions,
                "short_positions": row.short_positions,
                "net_position": row.net_position,
                "weekly_change": weekly_change,
                "percentile": rank,
                "net_oi_pct": (
                    round(row.net_position / row.open_interest * 100, 1)
                    if row.open_interest
                    else None
                ),
                "open_interest": row.open_interest,
                "crowding": crowding,
                "crowding_level": crowding_level,
            }
        )

    focus_market = request.GET.get("market", "").strip()
    focus_group = request.GET.get("focus_group", "").strip()
    focus_pair = (focus_market, focus_group)
    if focus_pair not in histories or not histories.get(focus_pair):
        focus_pair = (
            (positions[0]["symbol"], positions[0]["trader_group"]) if positions else ("", "")
        )
    focus_history = histories.get(focus_pair, [])[-156:]
    focus_row = next(
        (row for row in latest_rows if (row.market_code, row.trader_group) == focus_pair),
        None,
    )
    chart_data = [
        {"date": item.report_date.isoformat(), "净仓": item.net_position} for item in focus_history
    ]

    release = latest_all.aggregate(
        first_published_at=Min("published_at"),
        last_source_updated_at=Max("source_updated_at"),
        last_fetched_at=Max("fetched_at"),
        row_count=Count("pk"),
        published_count=Count("published_at"),
    )
    published_at = release["first_published_at"]
    source_updated_at = release["last_source_updated_at"]
    fetched_at = release["last_fetched_at"]
    if latest_date and (published_at is None or release["published_count"] != release["row_count"]):
        quality_status = "error"
    elif published_at and published_at < timezone.now() - timedelta(days=10):
        quality_status = "stale"
    elif published_at:
        quality_status = "fresh"
    else:
        quality_status = "stale"
    eastern = ZoneInfo("America/New_York")
    published_at_et = published_at.astimezone(eastern) if published_at else None
    source_updated_at_et = source_updated_at.astimezone(eastern) if source_updated_at else None
    source = latest_all.first().source if latest_date else None
    market_open_interest: dict[str, int] = {}
    for market_code, open_interest in latest_all.values_list("market_code", "open_interest"):
        if open_interest is not None:
            market_open_interest[market_code] = open_interest
    metrics = []
    if latest_date:
        common_metric = {
            "quality_status": quality_status,
            "source": source,
            # Keep the report date date-only. ``metric_card`` also supports
            # intraday timestamps and would otherwise apply a time formatter
            # to a ``date`` object.
            "as_of": latest_date.isoformat(),
            "fetched_at": fetched_at,
        }
        metrics = [
            {
                **common_metric,
                "label": "持仓日（通常周二）",
                "display_value": latest_date.strftime("%Y-%m-%d"),
            },
            {
                **common_metric,
                "label": "PRE 发布时间",
                "display_value": (
                    published_at_et.strftime("%m-%d %H:%M ET") if published_at_et else "未提供"
                ),
            },
            {
                **common_metric,
                "label": "当期合约数",
                "display_value": f"{len(market_open_interest):,}",
            },
            {
                **common_metric,
                "label": "总未平仓量",
                "display_value": f"{sum(market_open_interest.values()):,}",
            },
        ]
    return render(
        request,
        "research/positioning.html",
        {
            "title": "CFTC 持仓追踪",
            "positions": positions,
            "report": report,
            "report_types": report_types,
            "group_labels": group_labels,
            "selected_group": trader_group,
            "as_of": latest_date,
            "published_at": published_at,
            "published_at_et": published_at_et,
            "published_at_et_display": (
                published_at_et.strftime("%Y-%m-%d %H:%M ET") if published_at_et else ""
            ),
            "source_updated_at_et": source_updated_at_et,
            "source_updated_at_et_display": (
                source_updated_at_et.strftime("%Y-%m-%d %H:%M ET") if source_updated_at_et else ""
            ),
            "fetched_at": fetched_at,
            "source": source,
            "quality_status": quality_status,
            "metrics": metrics,
            "chart_data": chart_data,
            "focus_market": focus_pair[0],
            "focus_group": focus_pair[1],
            "focus_name": focus_row.market_name if focus_row else "",
            "focus_group_label": group_labels.get(focus_pair[1], ""),
            "percentile_window": 156,
            "required_notices": public_source_notices(["cftc"]) if latest_date else [],
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("大类资产", "/assets/"), ("CFTC 持仓", "")),
        },
    )


def crypto_derivatives(request):
    btc = _market_card("BTC-USD", "比特币")
    return render(
        request,
        "research/crypto_derivatives.html",
        {
            "title": "加密衍生品雷达",
            "btc": btc,
            "score": None,
            "confidence": None,
            "bias_24h": "等待公开展示授权",
            "structure_7d": "暂无可发布快照",
            "layers": [],
            "kpis": [],
            "methodology": (
                "OKX 与 Deribit 公共 API 不自动授予公开再分发权。取得书面授权或采购合规聚合源前，"
                "Funding、OI、IV、Skew、清算与 ETF 流量均保持空缺。"
            ),
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("大类资产", "/assets/"), ("加密衍生品", "")
            ),
        },
    )


def _filter_fed_documents(request, queryset, *, allow_type: bool):
    query = request.GET.get("q", "").strip()
    selected_type = request.GET.get("type", "").strip() if allow_type else ""
    valid_types = {value for value, _label in FedDocument.DocumentType.choices}
    if selected_type in valid_types:
        queryset = queryset.filter(document_type=selected_type)
    else:
        selected_type = ""
    if query:
        analysis_matches = queryset.filter(
            analysis_status__in=(
                FedDocument.AnalysisStatus.AI_GENERATED,
                FedDocument.AnalysisStatus.REVIEWED,
            ),
            summary__icontains=query,
        ).only(
            "id",
            "summary",
            "analysis_status",
            "analysis_model",
            "analysis_prompt_version",
            "analysis_generated_at",
            "analysis_evidence",
            "reviewed_by",
            "reviewed_at",
        )
        public_analysis_ids = [
            document.pk for document in analysis_matches if document.has_public_analysis
        ]
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(speaker__icontains=query)
            | Q(official_description__icontains=query)
            | Q(pk__in=public_analysis_ids)
        )
    return queryset, {"q": query, "type": selected_type}


def _reviewed_fed_average(documents) -> float | None:
    scores = [
        document.hawkish_score
        for document in documents
        if document.analysis_status == FedDocument.AnalysisStatus.REVIEWED
        and document.has_public_score
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def _fed_list_context(*, title: str, mode: str, page_obj, filters: dict, average_score=None):
    return {
        "title": title,
        "mode": mode,
        "page_obj": page_obj,
        "documents": page_obj.object_list,
        "filters": filters,
        "average_score": average_score,
        "type_choices": FedDocument.DocumentType.choices,
        "show_type_filter": mode in {"hub", "hawkish"},
    }


def fed_hub(request):
    documents, filters = _filter_fed_documents(
        request,
        _public_fed_documents(),
        allow_type=True,
    )
    reviewed_average = _reviewed_fed_average(documents)
    page_obj = Paginator(documents, 20).get_page(request.GET.get("page"))
    latest = {
        key: documents.filter(document_type=key).first()
        for key in [
            FedDocument.DocumentType.STATEMENT,
            FedDocument.DocumentType.SPEECH,
            FedDocument.DocumentType.NEWS,
        ]
    }
    return render(
        request,
        "research/fed_list.html",
        {
            **_fed_list_context(
                title="美联储",
                mode="hub",
                page_obj=page_obj,
                filters=filters,
                average_score=reviewed_average,
            ),
            "latest": latest,
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("美联储", "")),
        },
    )


def fed_list(request, doc_type: str):
    valid_types = {choice[0] for choice in FedDocument.DocumentType.choices}
    if doc_type not in valid_types:
        raise Http404("未知文档类型")
    queryset, filters = _filter_fed_documents(
        request,
        _public_fed_documents().filter(document_type=doc_type),
        allow_type=False,
    )
    page_obj = Paginator(queryset, 20).get_page(request.GET.get("page"))
    labels = dict(FedDocument.DocumentType.choices)
    return render(
        request,
        "research/fed_list.html",
        {
            **_fed_list_context(
                title=labels[doc_type],
                mode=doc_type,
                page_obj=page_obj,
                filters=filters,
            ),
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("美联储", "/fed/"), (labels[doc_type], "")),
        },
    )


def fed_hawkish_dovish(request):
    queryset, filters = _filter_fed_documents(
        request,
        _public_fed_documents(),
        allow_type=True,
    )
    analysed_documents = [document for document in queryset if document.has_public_analysis]
    average = _reviewed_fed_average(analysed_documents)
    page_obj = Paginator(analysed_documents, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "research/fed_list.html",
        {
            **_fed_list_context(
                title="鹰鸽追踪",
                mode="hawkish",
                page_obj=page_obj,
                filters=filters,
                average_score=average,
            ),
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("美联储", "/fed/"),
                ("鹰鸽追踪", ""),
            ),
        },
    )


def fed_detail(request, doc_type: str, slug: str):
    document = get_object_or_404(
        _public_fed_documents(),
        document_type=doc_type,
        slug=slug,
    )
    return render(
        request,
        "research/fed_detail.html",
        {
            "title": document.title,
            "item": document,
            "object": document,
            "document": document,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("美联储", "/fed/"),
                (document.get_document_type_display(), ""),
            ),
        },
    )


def _news_queryset(request, semiconductor_only=False, ai_only=False):
    queryset = _public_news_items()
    if semiconductor_only:
        queryset = queryset.filter(
            Q(category__in=["ai", "foundry", "memory", "packaging", "materials", "supply-chain"])
            | Q(themes__icontains="半导体")
        )
    if ai_only:
        queryset = queryset.filter(Q(category="ai") | Q(themes__icontains="AI"))
    query = request.GET.get("q", "").strip()
    source = request.GET.get("source", "").strip()
    category = request.GET.get("category", "").strip()
    if query:
        queryset = _text_search(queryset, query, ["title", "original_title", "summary"], "title")
    if source:
        queryset = queryset.filter(source_name=source)
    if category:
        queryset = queryset.filter(category=category)
    return queryset, {"q": query, "source": source, "category": category}


def news_list(request, semiconductor_only=False, ai_only=False):
    queryset, filters = _news_queryset(request, semiconductor_only, ai_only)
    page_obj = Paginator(queryset, 20).get_page(request.GET.get("page"))
    sources = (
        _public_news_items()
        .order_by("source_name")
        .values_list("source_name", flat=True)
        .distinct()
    )
    categories = (
        _public_news_items().order_by("category").values_list("category", flat=True).distinct()
    )
    title = "AI 资讯" if ai_only else "半导体行业资讯" if semiconductor_only else "新闻 / 事件"
    return render(
        request,
        "research/news_list.html",
        {
            "title": title,
            "page_obj": page_obj,
            "filters": filters,
            "filter_options": {"sources": sources, "categories": categories},
            "semiconductor_only": semiconductor_only,
            "ai_only": ai_only,
            "breadcrumbs": _breadcrumbs(("首页", "/"), (title, "")),
        },
    )


def reports(request, all_reports=False):
    public_research = _public_research_mentions()
    queryset = public_research
    query = request.GET.get("q", "").strip()
    bank = request.GET.get("bank", "").strip()
    category = request.GET.get("category", "").strip()
    stance = request.GET.get("stance", "").strip()
    if query:
        queryset = _text_search(queryset, query, ["title", "summary", "bank"], "title")
    if bank:
        queryset = queryset.filter(bank=bank)
    if category:
        queryset = queryset.filter(category=category)
    if stance:
        queryset = queryset.filter(stance=stance)
    page_obj = Paginator(queryset, 24).get_page(request.GET.get("page"))
    matrix = list(
        public_research.values("category", "stance")
        .annotate(count=Count("id"))
        .order_by("category", "stance")
    )
    banks = public_research.order_by("bank").values_list("bank", flat=True).distinct()
    categories = public_research.order_by("category").values_list("category", flat=True).distinct()
    stances = public_research.order_by("stance").values_list("stance", flat=True).distinct()
    return render(
        request,
        "research/reports.html",
        {
            "title": "全部机构观点" if all_reports else "机构观点合成",
            "all_reports": all_reports,
            "page_obj": page_obj,
            "matrix": matrix,
            "stance_matrix": [
                {
                    "label": item["stance"] or "中性",
                    "stance": item["stance"],
                    "count": item["count"],
                    "banks": [],
                }
                for item in matrix
            ],
            "filters": {"q": query, "bank": bank, "category": category, "stance": stance},
            "filter_options": {"banks": banks, "categories": categories, "stances": stances},
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("研究库", "")),
        },
    )


def fund_letters(request):
    public_letters = _public_fund_letters()
    queryset = public_letters.order_by(
        F("published_at").desc(nulls_last=True),
        "-quarter",
        "fund_name",
    )
    query = request.GET.get("q", "").strip()
    if query:
        queryset = _text_search(
            queryset,
            query,
            ["fund_name", "fund_name_en", "manager", "summary"],
            "fund_name",
        )
    filters = {key: request.GET.get(key, "").strip() for key in ["quarter", "strategy", "stance"]}
    for key, value in filters.items():
        if value:
            queryset = queryset.filter(**{key: value})
    page_obj = Paginator(queryset, 24).get_page(request.GET.get("page"))
    options = {
        key: public_letters.exclude(**{key: ""}).order_by(key).values_list(key, flat=True).distinct()
        for key in ["quarter", "strategy", "stance"]
    }
    fund_count = public_letters.values("fund_name").distinct().count()
    filters["q"] = query
    return render(
        request,
        "research/fund_letters.html",
        {
            "title": "基金信函",
            "page_obj": page_obj,
            "filters": filters,
            "filter_options": options,
            "total_count": queryset.count(),
            "fund_count": fund_count,
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("基金信函", "")),
        },
    )


def fund_letter_detail(request, pk: int):
    public_letters = _public_fund_letters()
    letter = get_object_or_404(public_letters, pk=pk)
    related = (
        public_letters.filter(fund_name=letter.fund_name)
        .exclude(pk=letter.pk)
        .order_by(F("published_at").desc(nulls_last=True), "-quarter")[:8]
    )
    return render(
        request,
        "research/fund_letter_detail.html",
        {
            "title": f"{letter.fund_name} · {letter.quarter}",
            "item": letter,
            "object": letter,
            "letter": letter,
            "related": related,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("基金信函", "/research/fund-letters/"), (letter.fund_name, "")
            ),
        },
    )


def glossary(request, ai_only=False):
    public_terms = _public_ai_glossary_terms() if ai_only else _public_glossary_terms()
    queryset = public_terms
    filters = {
        key: request.GET.get(key, "").strip()
        for key in ["q", "category", "subcategory", "difficulty", "tag"]
    }
    if filters["q"]:
        queryset = _text_search(
            queryset,
            filters["q"],
            ["term", "term_en", "definition"],
            "term",
        )
    for key in ["category", "subcategory", "difficulty"]:
        if filters[key]:
            queryset = queryset.filter(**{key: filters[key]})
    if filters["tag"]:
        queryset = queryset.filter(tags__icontains=filters["tag"])
    options = {
        key: public_terms.order_by(key).values_list(key, flat=True).distinct()
        for key in ["category", "subcategory", "difficulty"]
    }
    return render(
        request,
        "research/glossary.html",
        {
            "title": "AI 专业术语库" if ai_only else "专业术语库",
            "terms": queryset,
            "filters": filters,
            "filter_options": options,
            "ai_only": ai_only,
            "page_description": (
                "用原创中文定义梳理 AI 模型、半导体与算力基础设施概念，并逐项链接一手来源。"
                if ai_only
                else "把指标定义、公式、解释边界与来源放在一起，减少同词异义。"
            ),
            "breadcrumbs": (
                _breadcrumbs(
                    ("首页", "/"),
                    ("AI 产业观察", "/ai-industry/"),
                    ("AI 术语库", ""),
                )
                if ai_only
                else _breadcrumbs(("首页", "/"), ("术语库", ""))
            ),
        },
    )


def ai_glossary_detail(request, slug: str):
    term = get_object_or_404(_public_ai_glossary_terms(), slug=slug)
    return render(
        request,
        "research/glossary.html",
        {
            "title": term.term,
            "terms": [term],
            "filters": {},
            "filter_options": {},
            "ai_only": True,
            "detail_term": term,
            "page_description": term.definition,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("AI 产业观察", "/ai-industry/"),
                ("AI 术语库", reverse("ai-glossary")),
                (term.term, ""),
            ),
        },
    )


def search(request):
    query = request.GET.get("q", "").strip()
    company_results = Company.objects.none()
    news_results = NewsItem.objects.none()
    research_results = ResearchMention.objects.none()
    letter_results = FundLetter.objects.none()
    glossary_results = GlossaryTerm.objects.none()
    if query:
        company_results = _text_search(
            _public_companies(),
            query,
            ["name", "name_en", "ticker", "description"],
            "name",
        )[:10]
        news_results = _text_search(
            _public_news_items(),
            query,
            ["title", "original_title", "summary"],
            "title",
        )[:10]
        research_results = _text_search(
            _public_research_mentions(),
            query,
            ["title", "summary", "bank"],
            "title",
        )[:10]
        letter_results = _text_search(
            _public_fund_letters(),
            query,
            ["fund_name", "fund_name_en", "manager", "summary"],
            "fund_name",
        )[:10]
        glossary_results = _text_search(
            _public_glossary_terms(),
            query,
            ["term", "term_en", "definition"],
            "term",
        )[:10]

    company_results = list(company_results)
    news_results = list(news_results)
    research_results = list(research_results)
    letter_results = list(letter_results)
    glossary_results = list(glossary_results)
    result_count = sum(
        map(
            len,
            [
                company_results,
                news_results,
                research_results,
                letter_results,
                glossary_results,
            ],
        )
    )
    return render(
        request,
        "research/search.html",
        {
            "title": "全站搜索",
            "query": query,
            "results": {},
            "company_results": company_results,
            "news_results": news_results,
            "report_results": research_results + letter_results,
            "glossary_results": glossary_results,
            "result_count": result_count,
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("搜索", "")),
        },
    )


def data_sources(request):
    requirements = DataRequirement.objects.all()
    status_counts = {
        status: requirements.filter(status=status).count()
        for status, _ in DataRequirement.Status.choices
    }
    current_licenses = (
        SourceLicense.objects.filter(is_current=True)
        .exclude(source__key="demo-market")
        .exclude(reviewed_by="clean-room seed policy")
        .exclude(terms_url__icontains="example.com")
        .select_related("source")
    )
    sources = (
        Source.objects.exclude(key="demo-market")
        .exclude(homepage__icontains="example.com")
        .prefetch_related(
            Prefetch("licenses", queryset=current_licenses, to_attr="public_licenses")
        )
    )
    return render(
        request,
        "research/data_sources.html",
        {
            "title": "数据源与采购台账",
            "requirements": requirements,
            "status_counts": status_counts,
            "sources": sources,
            "licenses": current_licenses,
            "breadcrumbs": _breadcrumbs(("首页", "/"), ("数据源与采购", "")),
        },
    )


def ai_hub(request, chain_mode=False):
    public_companies = _public_companies()
    nodes = _public_supply_chain_nodes().annotate(
        company_count=Count("companies", filter=Q(companies__in=public_companies))
    )
    companies = public_companies
    models = _public_model_profiles()
    agents = _public_coding_agents()
    projects = _public_github_projects()
    latest_project = (
        projects.filter(data_as_of__isnull=False)
        .select_related("source")
        .order_by("-data_as_of")
        .first()
    )
    counts = {
        "node_count": nodes.count(),
        "company_count": companies.count(),
        "model_count": models.count(),
        "agent_count": agents.count(),
        "project_count": projects.count(),
    }
    context = {
        "title": "AI 产业链" if chain_mode else "AI 产业观察",
        "chain_mode": chain_mode,
        **counts,
        "stats": [
            {
                "label": "产业节点",
                "display_value": str(counts["node_count"]),
                "source": "已审核产业关系库" if counts["node_count"] else "待接入",
            },
            {
                "label": "覆盖公司",
                "display_value": str(counts["company_count"]),
                "source": "SEC / 公司披露" if counts["company_count"] else "待接入",
            },
            {
                "label": "大模型",
                "display_value": str(counts["model_count"]),
                "source": "厂商官方文档" if counts["model_count"] else "待接入",
            },
            {
                "label": "Coding Agents",
                "display_value": str(counts["agent_count"]),
                "source": "官方文档 / 基准" if counts["agent_count"] else "待接入",
            },
            {
                "label": "应用项目",
                "display_value": str(counts["project_count"]),
                "source": "GitHub REST API" if counts["project_count"] else "待接入",
                "as_of": latest_project.data_as_of if latest_project else None,
            },
        ],
        "source": latest_project.source if latest_project else "数据源覆盖台账",
        "as_of": latest_project.data_as_of if latest_project else None,
        "required_notices": public_source_notices(["github"]) if latest_project else [],
        "top_nodes": nodes.order_by("-narrative_score")[:9],
        "top_companies": companies.order_by("-return_1m")[:8],
        "top_models": models[:4],
        "top_agents": agents[:4],
        "top_projects": projects[:8],
        "news_items": _public_news_items().filter(Q(category="ai") | Q(themes__icontains="AI"))[:5],
        "breadcrumbs": _breadcrumbs(("首页", "/"), ("AI 产业观察", "")),
    }
    return render(request, "research/ai_hub.html", context)


def ai_market_map(request):
    public_companies = _public_companies()
    nodes = (
        _public_supply_chain_nodes()
        .annotate(company_count=Count("companies", filter=Q(companies__in=public_companies)))
        .prefetch_related(
            Prefetch("companies", queryset=public_companies, to_attr="public_companies")
        )
    )
    companies = public_companies
    query = request.GET.get("q", "").strip()
    layer = request.GET.get("layer", "").strip()
    quadrant = request.GET.get("quadrant", "").strip()
    sort = request.GET.get("sort", "narrative").strip()
    if query:
        nodes = nodes.filter(
            Q(name__icontains=query)
            | Q(description__icontains=query)
            | Q(companies__name__icontains=query)
            | Q(companies__ticker__icontains=query)
        ).distinct()
    if layer:
        nodes = nodes.filter(layer=layer)
    if quadrant:
        nodes = nodes.filter(quadrant=quadrant)
    if query:
        companies = companies.filter(
            Q(name__icontains=query)
            | Q(name_en__icontains=query)
            | Q(ticker__icontains=query)
            | Q(primary_node__name__icontains=query)
        )
    if layer:
        companies = companies.filter(primary_node__layer=layer)
    if quadrant:
        companies = companies.filter(primary_node__quadrant=quadrant)
    sort_fields = {
        "name": "name",
        "narrative": "-narrative_score",
        "companies": "-company_count",
        "growth": "-revenue_growth",
        "market_cap": "-market_cap_usd_m",
    }
    nodes = nodes.order_by(sort_fields.get(sort, "-narrative_score"), "name")
    company_sort_fields = {
        "name": "name",
        "market_cap": "-market_cap_usd_m",
        "revenue_growth": "-revenue_growth",
        "gross_margin": "-gross_margin",
        "return_6m": "-return_6m",
        "pe": "pe",
    }
    companies = companies.select_related("primary_node").order_by(
        company_sort_fields.get(sort, "-market_cap_usd_m"), "name"
    )
    page_obj = Paginator(companies, 100).get_page(request.GET.get("page"))
    layers = nodes.order_by("layer").values_list("layer", flat=True).distinct()
    quadrants = nodes.order_by("quadrant").values_list("quadrant", flat=True).distinct()
    return render(
        request,
        "research/ai_market_map.html",
        {
            "title": "AI 产业链资本地图",
            "nodes": nodes,
            "companies": page_obj.object_list,
            "page_obj": page_obj,
            "company_count": companies.count(),
            "layers": layers,
            "quadrants": quadrants,
            "filters": {"q": query, "layer": layer, "quadrant": quadrant, "sort": sort},
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("AI 产业观察", "/ai-industry/"), ("资本地图", "")
            ),
        },
    )


def ai_graph(request):
    public_companies = _public_companies()
    node_query = _public_supply_chain_nodes().annotate(
        company_count=Count("companies", filter=Q(companies__in=public_companies))
    )
    query = request.GET.get("q", "").strip()
    layer = request.GET.get("layer", "").strip()
    confidence = request.GET.get("confidence", "").strip()
    if query:
        node_query = node_query.filter(Q(name__icontains=query) | Q(description__icontains=query))
    if layer:
        node_query = node_query.filter(layer=layer)
    nodes = list(node_query)
    node_ids = [node.pk for node in nodes]
    edge_query = (
        SupplyChainEdge.objects.filter(
            source_node_id__in=node_ids, target_node_id__in=node_ids, reviewed=True
        )
        .exclude(evidence_url="")
        .exclude(evidence_url__icontains="example.com")
    )
    if confidence:
        try:
            edge_query = edge_query.filter(confidence__gte=Decimal(confidence))
        except (ArithmeticError, ValueError):
            pass
    edges = list(edge_query.select_related("source_node", "target_node"))
    graph_nodes = [
        {
            "id": node.slug,
            "name": node.name,
            "category": node.layer,
            "value": node.company_count,
            "url": node.get_absolute_url(),
        }
        for node in nodes
    ]
    graph_edges = [
        {
            "source": edge.source_node.slug,
            "target": edge.target_node.slug,
            "name": edge.relation,
            "confidence": float(edge.confidence),
        }
        for edge in edges
    ]
    return render(
        request,
        "research/ai_graph.html",
        {
            "title": "AI 产业关系图谱",
            "nodes": nodes,
            "companies": public_companies.filter(primary_node_id__in=node_ids).select_related(
                "primary_node"
            ),
            "edges": edges,
            "layers": sorted({node.layer for node in nodes}),
            "filters": {"q": query, "layer": layer, "confidence": confidence},
            "graph_data": {"nodes": graph_nodes, "edges": graph_edges},
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("AI 产业观察", "/ai-industry/"), ("关系图谱", "")
            ),
        },
    )


def ai_node(request, slug: str):
    node = get_object_or_404(_public_supply_chain_nodes(), slug=slug)
    companies = _public_companies().filter(primary_node=node).order_by("-market_cap_usd_m")
    inbound = (
        node.inbound_edges.exclude(evidence_url__icontains="example.com")
        .exclude(evidence_url="")
        .filter(reviewed=True)
        .select_related("source_node")
    )
    outbound = (
        node.outbound_edges.exclude(evidence_url__icontains="example.com")
        .exclude(evidence_url="")
        .filter(reviewed=True)
        .select_related("target_node")
    )
    return render(
        request,
        "research/ai_node.html",
        {
            "title": node.name,
            "item": node,
            "object": node,
            "node": node,
            "companies": companies,
            "inbound_edges": inbound,
            "outbound_edges": outbound,
            "chart_data": [
                (None if value is None or (index == 0 and value == 0) else float(value))
                for index, value in enumerate(
                    [
                        node.narrative_score,
                        node.revenue_growth,
                        node.gross_margin,
                        node.median_pe,
                        node.median_ps,
                    ]
                )
            ],
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("AI 产业观察", "/ai-industry/"),
                ("产业链", "/ai-industry/chain/"),
                (node.name, ""),
            ),
        },
    )


def ai_company(request, slug: str):
    company = (
        _public_companies()
        .select_related("primary_node", "source")
        .filter(slug=slug)
        .first()
    )
    if company is None:
        if not is_pending_ai_company_contract_slug(slug):
            raise Http404("公司档案不存在")
        return render(
            request,
            "research/ai_company_pending.html",
            {
                "title": f"{slug} · 公司数据待接入",
                "contract_slug": slug,
                "breadcrumbs": _breadcrumbs(
                    ("首页", "/"),
                    ("AI 产业观察", "/ai-industry/"),
                    ("公司档案", ""),
                    (slug, ""),
                ),
            },
        )
    reviewed_sec = is_exact_reviewed_sec_company(company)
    derived_allowed = False
    if reviewed_sec and company.source_id:
        derived_allowed = Source.objects.filter(pk=company.source_id).filter(
            derived_display_license_q("licenses")
        ).exists()
    if reviewed_sec:
        financials = (
            company.financials.select_related("source", "capex_source_fact")
            .filter(
                publication_batch_id=company.publication_batch_id,
                source__key="sec",
            )
            .exclude(quality_status=Observation.Quality.ERROR)
            .filter(derived_display_license_q())
            .distinct()
            .order_by("-fiscal_year")[:3]
            if derived_allowed
            else company.financials.none()
        )
    else:
        financials = (
            company.financials.select_related("source")
            .filter(public_display_license_q())
            .exclude(source__key="demo-market")
            .exclude(quality_status=Observation.Quality.ERROR)
            .distinct()
        )
    latest_fact = financials.first()
    if reviewed_sec:
        chart_data = []
    else:
        chart_data = list(
            MarketBar.objects.filter(instrument__symbol=company.ticker)
            .filter(public_display_license_q())
            .exclude(source__key="demo-market")
            .exclude(quality_status=Observation.Quality.ERROR)
            .order_by("value_date")
            .values_list("close", flat=True)[:240]
        )
    related = (
        _public_companies().filter(primary_node=company.primary_node).exclude(pk=company.pk)[:8]
    )
    return render(
        request,
        "research/ai_company.html",
        {
            "title": f"{company.name} / {company.name_en}",
            "item": company,
            "object": company,
            "company": company,
            "financials": financials,
            "latest_fact": latest_fact,
            "related": related,
            "chart_data": chart_data,
            "reviewed_sec": reviewed_sec,
            "sec_projection_allowed": derived_allowed,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("AI 产业观察", "/ai-industry/"),
                ("公司档案", ""),
                (company.name, ""),
            ),
        },
    )


def model_evolution(request):
    models = _public_model_profiles()
    agents = _public_coding_agents()
    model_rows = list(models)
    as_of = max((item.release_date for item in model_rows), default=None)
    capability_data = [
        {
            "name": item.name,
            "Terminal-Bench 2.1": float(item.capability_score),
        }
        for item in model_rows
        if item.capability_score is not None and item.capability_score > 0
    ]
    cost_data = [
        {
            "name": item.name,
            "输入 $/M": float(item.input_price),
            "输出 $/M": float(item.output_price),
        }
        for item in model_rows
        if item.input_price is not None and item.output_price is not None
    ]
    return render(
        request,
        "research/model_evolution.html",
        {
            "title": "大模型演变",
            "models": model_rows,
            "agents": agents,
            "capability_data": capability_data,
            "cost_data": cost_data,
            "metrics": [
                {
                    "label": "官方来源模型",
                    "display_value": str(len(model_rows)),
                    "status": "fresh" if model_rows else "stale",
                    "as_of": as_of,
                },
                {
                    "label": "公开标价模型",
                    "display_value": str(sum(item.input_price is not None for item in model_rows)),
                    "status": "fresh" if model_rows else "stale",
                    "as_of": as_of,
                },
                {
                    "label": "Coding Agents",
                    "display_value": str(agents.count()),
                    "status": "fresh" if agents.exists() else "stale",
                    "as_of": as_of,
                },
                {
                    "label": "统一 Agent 评分",
                    "display_value": "—",
                    "change": "待同批次独立评测",
                    "status": "stale",
                    "as_of": as_of,
                },
            ],
            "source": "厂商官方发布页（人工核验）" if model_rows else None,
            "as_of": as_of,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("AI 产业观察", "/ai-industry/"), ("大模型演变", "")
            ),
        },
    )


def model_detail(request, slug: str):
    public_models = _public_model_profiles()
    profile = get_object_or_404(public_models, slug=slug)
    peers = public_models.exclude(pk=profile.pk)[:5]
    return render(
        request,
        "research/model_detail.html",
        {
            "title": profile.name,
            "item": profile,
            "object": profile,
            "profile": profile,
            "model": profile,
            "peers": peers,
            "benchmark_data": [
                {
                    "label": "Terminal-Bench 2.1",
                    "score": float(profile.capability_score),
                }
            ]
            if profile.capability_score is not None and profile.capability_score > 0
            else [],
            "source": profile.sources[0].get("label") if profile.sources else None,
            "as_of": profile.release_date,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("大模型演变", "/ai-industry/chain/model-evolution/"),
                (profile.name, ""),
            ),
        },
    )


def coding_agent_detail(request, slug: str):
    public_agents = _public_coding_agents()
    profile = get_object_or_404(public_agents, slug=slug)
    peers = public_agents.exclude(pk=profile.pk)[:5]
    return render(
        request,
        "research/coding_agent_detail.html",
        {
            "title": profile.name,
            "item": profile,
            "object": profile,
            "profile": profile,
            "agent": profile,
            "peers": peers,
            "benchmark_data": [],
            "source": profile.homepage,
            "as_of": profile.release_date,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("Coding Agent", "/ai-industry/chain/model-evolution/"),
                (profile.name, ""),
            ),
        },
    )


def coding_agents(request):
    query = request.GET.get("q", "").strip()
    product_type = request.GET.get("type", "").strip()
    public_agents = _public_coding_agents()
    product_types = list(
        public_agents.order_by("product_type")
        .values_list("product_type", flat=True)
        .distinct()
    )
    agents = public_agents
    if query:
        agents = _text_search(
            agents,
            query,
            ["name", "provider", "product_type", "description"],
            "name",
        )
    if product_type:
        agents = agents.filter(product_type=product_type)
    rows = list(agents)
    known_release_dates = [item.release_date for item in rows if item.release_date]
    as_of = max(known_release_dates, default=None)
    return render(
        request,
        "research/coding_agents.html",
        {
            "title": "Coding Agent 目录",
            "agents": rows,
            "total_count": public_agents.count(),
            "filtered_count": len(rows),
            "product_types": product_types,
            "filters": {"q": query, "type": product_type},
            "as_of": as_of,
            "source": "厂商官方产品页（人工核验）" if rows else None,
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"),
                ("AI 产业观察", "/ai-industry/"),
                ("Coding Agents", ""),
            ),
        },
    )


def applications(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    sort = request.GET.get("sort", "momentum").strip()
    public_projects = _public_github_projects().annotate(
        snapshot_count=Count("snapshots", distinct=True)
    )
    categories = list(
        public_projects.exclude(category="")
        .order_by("category")
        .values_list("category", flat=True)
        .distinct()
    )
    projects = public_projects
    if query:
        projects = _text_search(
            projects,
            query,
            ["repo", "description", "category"],
            "repo",
        )
    if category:
        projects = projects.filter(category=category)
    ordering = {
        "stars": ("-stars", "repo"),
        "stars_7d": ("-stars_7d", "-stars", "repo"),
        "momentum": ("-momentum_score", "-stars_7d", "-stars", "repo"),
    }.get(sort, ("-momentum_score", "-stars_7d", "-stars", "repo"))
    projects = projects.order_by(*ordering)
    latest = (
        public_projects.filter(data_as_of__isnull=False)
        .select_related("source")
        .order_by("-data_as_of")
        .first()
    )
    delta_ready_projects = projects.filter(snapshot_count__gte=2)
    category_momentum = list(
        delta_ready_projects.values("category")
        .annotate(value=Sum("stars_7d"))
        .order_by("-value", "category")
    )
    total_stars = projects.aggregate(total=Sum("stars"))["total"] or 0
    weekly_delta = sum(item["value"] or 0 for item in category_momentum)
    has_weekly_delta = delta_ready_projects.exists()
    return render(
        request,
        "research/applications.html",
        {
            "title": "AI 应用开源雷达",
            "projects": projects,
            "top_weekly": delta_ready_projects.filter(stars_7d__gt=0).order_by(
                "-stars_7d", "-stars"
            )[:8],
            "categories": categories,
            "selected_category": category,
            "selected_sort": sort,
            "query": query,
            "total_stars": total_stars,
            "as_of": latest.data_as_of if latest else None,
            "source": latest.source if latest else "数据源覆盖台账",
            "required_notices": public_source_notices(["github"]) if latest else [],
            "chart_data": [
                {"label": item["category"] or "Uncategorised", "value": item["value"] or 0}
                for item in category_momentum
            ],
            "metrics": [
                {
                    "label": "追踪项目",
                    "display_value": str(projects.count()),
                    "source": "GitHub",
                },
                {
                    "label": "覆盖场景",
                    "display_value": str(len(categories)),
                    "source": "Atlas Macro",
                },
                {
                    "label": "累计 Stars",
                    "display_value": f"{total_stars:,}",
                    "source": "GitHub",
                },
                {
                    "label": "7 日新增",
                    "display_value": (f"{weekly_delta:,}" if has_weekly_delta else "—"),
                    "source": "每日快照差值",
                    "change": ("等待第二个每日快照" if not has_weekly_delta else "已对齐两个快照"),
                },
            ],
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("AI 产业观察", "/ai-industry/"), ("AI 应用", "")
            ),
        },
    )


def ai_teardown(request):
    generations = []
    return render(
        request,
        "research/teardown.html",
        {
            "title": "AI 系统价值量拆解",
            "generations": generations,
            "chart_data": generations,
            "sections": [],
            "methodology": "价值量模型尚无逐项可追溯来源，授权数据与人工审核完成前不发布百分比。",
            "breadcrumbs": _breadcrumbs(
                ("首页", "/"), ("AI 产业观察", "/ai-industry/"), ("价值量拆解", "")
            ),
        },
    )


def gone(request, reason="该模块已下线，历史 URL 仅用于兼容。"):
    return render(
        request, "research/gone.html", {"title": "410 · 已下线", "reason": reason}, status=410
    )


_LLMS_EXCLUDED_ROUTE_NAMES = {
    "credit-issuance",
    "credit-events",
    "search",
    "robots",
    "sitemap",
    "llms",
    "manifest",
    "service-worker",
    "offline",
    "health",
}


def _public_static_route_names(patterns=None):
    """Yield argument-free content routes while excluding internal and lifecycle endpoints."""

    if patterns is None:
        patterns = get_resolver().url_patterns
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace == "admin":
                continue
            yield from _public_static_route_names(pattern.url_patterns)
            continue
        if not isinstance(pattern, URLPattern) or not pattern.name:
            continue
        route = str(pattern.pattern)
        if "<" in route or pattern.name in _LLMS_EXCLUDED_ROUTE_NAMES:
            continue
        yield pattern.name


def _llms_label(value):
    return str(value).replace("[", "").replace("]", "").replace("\n", " ").strip()


def llms_txt(request):
    """Publish a database-aware inventory of content URLs suitable for LLM discovery."""

    entries = []
    for route_name in dict.fromkeys(_public_static_route_names()):
        entries.append((route_name.replace("-", " ").title(), reverse(route_name)))

    entries.extend(
        (f"日报 {item.date.isoformat()}", item.get_absolute_url()) for item in _public_theses()
    )
    entries.extend(
        (f"{item.fund_name} {item.quarter}", item.get_absolute_url())
        for item in _public_fund_letters()
    )
    entries.extend((item.name, item.get_absolute_url()) for item in _public_supply_chain_nodes())
    entries.extend((item.name, item.get_absolute_url()) for item in _public_companies())
    for item in _public_fed_documents():
        route_name = {
            FedDocument.DocumentType.STATEMENT: "fed-detail",
            FedDocument.DocumentType.SPEECH: "fed-speech-detail",
            FedDocument.DocumentType.NEWS: "fed-news-detail",
        }[item.document_type]
        entries.append((item.title, reverse(route_name, kwargs={"slug": item.slug})))
    entries.extend(
        (item.name, reverse("model-detail", kwargs={"slug": item.slug}))
        for item in _public_model_profiles()
    )
    entries.extend(
        (item.name, reverse("coding-agent-detail", kwargs={"slug": item.slug}))
        for item in _public_coding_agents()
    )
    entries.extend(
        (f"术语：{item.term}", f"{reverse('glossary')}#{item.slug}")
        for item in _public_glossary_terms().exclude(slug__in=AI_GLOSSARY_TERM_SLUGS)
    )
    entries.extend(
        (
            f"AI 术语：{item.term}",
            reverse("ai-glossary-detail", kwargs={"slug": item.slug}),
        )
        for item in _public_ai_glossary_terms()
    )

    lines = [
        f"# {settings.SITE_NAME}",
        "",
        "> 可追溯的跨资产宏观与 AI 产业研究平台。仅列出当前可公开访问的内容路由。",
        "",
        "## Public content",
        "",
    ]
    seen = set()
    for label, path in entries:
        url = request.build_absolute_uri(path)
        if url in seen:
            continue
        seen.add(url)
        lines.append(f"- [{_llms_label(label)}]({url})")
    lines.append("")
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


def robots_txt(request):
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "Disallow: /internal/",
            "Disallow: /search/",
            "Disallow: /login/",
            "Disallow: /logout/",
            "Disallow: /static/research_pdfs/",
            f"Sitemap: {settings.SITE_URL}/sitemap.xml",
            "",
        ]
    )
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


def _canonical_public_url(path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{settings.SITE_URL.rstrip('/')}{normalized_path}"


def sitemap_xml(request):
    static_names = [
        "home",
        "trade-map",
        "regime-log",
        "daily-list",
        "assets-overview",
        "equities",
        "etfs",
        "options",
        "positioning",
        "bonds",
        "commodities",
        "fx",
        "crypto",
        "crypto-derivatives",
        "rates-overview",
        "fed-funds",
        "yield-curve",
        "auctions",
        "real-rates",
        "expectations",
        "fed-hub",
        "fed-statements",
        "fed-speeches",
        "fed-news",
        "hawkish-dovish",
        "liquidity-overview",
        "transmission-chain",
        "fed-balance-sheet",
        "operations",
        "rrp-tga",
        "reserves",
        "global-dollar",
        "subsurface",
        "economy-overview",
        "gdp",
        "employment",
        "inflation",
        "consumer",
        "volatility-overview",
        "volatility-dashboard",
        "vix",
        "volatility-move",
        "fx-vol",
        "implied-vs-realized",
        "credit-overview",
        "credit-spreads",
        "credit-cds",
        "credit-stress",
        "news",
        "semiconductor-news",
        "reports",
        "reports-all",
        "fund-letters",
        "glossary",
        "data-sources",
        "supply-chain",
        "supply-chain-foundry",
        "supply-chain-packaging",
        "supply-chain-hbm",
        "supply-chain-gpu",
        "supply-chain-demand",
        "ai-hub",
        "ai-market-map",
        "ai-graph",
        "ai-news",
        "ai-chain",
        "semiconductor-chain",
        "model-evolution",
        "coding-agents",
        "applications",
        "ai-glossary",
        "ai-teardown",
    ]
    urls = [_canonical_public_url(reverse(name)) for name in static_names]
    urls.extend(_canonical_public_url(item.get_absolute_url()) for item in _public_theses())
    urls.extend(
        _canonical_public_url(item.get_absolute_url()) for item in _public_fund_letters()
    )
    urls.extend(
        _canonical_public_url(item.get_absolute_url()) for item in _public_supply_chain_nodes()
    )
    urls.extend(_canonical_public_url(item.get_absolute_url()) for item in _public_companies())
    for item in _public_fed_documents():
        route_name = {
            FedDocument.DocumentType.STATEMENT: "fed-detail",
            FedDocument.DocumentType.SPEECH: "fed-speech-detail",
            FedDocument.DocumentType.NEWS: "fed-news-detail",
        }[item.document_type]
        urls.append(_canonical_public_url(reverse(route_name, kwargs={"slug": item.slug})))
    urls.extend(
        _canonical_public_url(reverse("model-detail", kwargs={"slug": item.slug}))
        for item in _public_model_profiles()
    )
    urls.extend(
        _canonical_public_url(reverse("coding-agent-detail", kwargs={"slug": item.slug}))
        for item in _public_coding_agents()
    )
    urls.extend(
        _canonical_public_url(reverse("ai-glossary-detail", kwargs={"slug": item.slug}))
        for item in _public_ai_glossary_terms()
    )
    now = timezone.localdate().isoformat()
    body = "".join(
        f"<url><loc>{escape(url)}</loc><lastmod>{now}</lastmod></url>"
        for url in dict.fromkeys(urls)
    )
    return HttpResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>",
        content_type="application/xml; charset=utf-8",
    )


def manifest(request):
    payload = {
        "name": "Atlas Macro Research",
        "short_name": "Atlas Macro",
        "description": "可追溯的跨资产宏观与 AI 产业研究平台",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#08111d",
        "theme_color": "#0b1625",
        "lang": "zh-CN",
        "icons": [
            {
                "src": "/static/research/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }
    return HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        content_type="application/manifest+json; charset=utf-8",
    )


def service_worker(request):
    content = """
const CACHE = 'atlas-macro-v1';
const OFFLINE = '/offline/';
self.addEventListener('install', event => event.waitUntil(
  caches.open(CACHE).then(cache => cache.addAll([
    OFFLINE,
    '/static/research/css/app.css',
    '/static/research/js/app.js'
  ]))
));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  const excluded = url.pathname.startsWith('/admin/') ||
    url.pathname.startsWith('/internal/');
  if (event.request.method !== 'GET' || excluded) return;
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(caches.match(event.request).then(hit => hit ||
      fetch(event.request).then(response => {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(event.request, copy));
        return response;
      })
    ));
    return;
  }
  event.respondWith(fetch(event.request).then(response => {
    const copy = response.clone();
    caches.open(CACHE).then(cache => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then(hit => hit || caches.match(OFFLINE))));
});
""".strip()
    response = HttpResponse(content, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    return response


def offline(request):
    return render(request, "research/offline.html", {"title": "离线模式"})


def health(request):
    latest_run = (
        DashboardSnapshot.objects.filter(Q(data__demo=False) | ~Q(data__has_key="demo"))
        .exclude(source__key="demo-market")
        .order_by("-updated_at")
        .values("key", "quality_status", "updated_at")
        .first()
    )
    return JsonResponse(
        {
            "status": "ok",
            "service": "atlas-macro",
            "time": timezone.now().isoformat(),
            "latest_snapshot": latest_run,
        }
    )


def page_not_found(request, exception):
    return render(request, "research/404.html", {"title": "页面不存在"}, status=404)
