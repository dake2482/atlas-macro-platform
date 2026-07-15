"""Celery entry points for scheduled, lineage-aware refresh jobs."""

from __future__ import annotations

from datetime import date
from typing import Any

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .berkshire_letters import refresh_berkshire_letters
from .github_catalog import GITHUB_PROJECT_SEEDS
from .models import (
    DashboardSnapshot,
    GeneratedAnalysis,
    IngestionRun,
    MetricSnapshot,
)
from .official_data import (
    refresh_credit_official_data,
    refresh_h8_data,
    refresh_h10_data,
    refresh_h41_data,
    refresh_macro_official_data,
    refresh_official_data,
    refresh_prates_data,
    refresh_treasury_curve_data,
)
from .official_news import (
    BLSReleaseProvider,
    SECPressReleaseProvider,
    TreasuryPressReleaseProvider,
    store_official_news,
)
from .providers import CFTCProvider, GitHubProvider, ProviderResult
from .sec_company_facts import refresh_sec_company_data
from .services import (
    record_provider_result,
    store_cftc_positions,
    store_github_repository,
    summarize_runs,
)
from .thesis_publication import (
    DAILY_EVIDENCE_CONTRACT_VERSION,
    latest_ready_daily_evidence,
    publish_daily_evidence_snapshot,
    validate_daily_evidence_snapshot,
)

DEFAULT_FRED_SERIES = (
    "DGS3MO",
    "DGS2",
    "DGS5",
    "DGS10",
    "DGS30",
    "WALCL",
    "RRPONTSYD",
    "WTREGEN",
)


def _setting_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _skip(source: str, dataset: str, reason: str) -> IngestionRun:
    return record_provider_result(ProviderResult.skip(source, dataset, reason))


@shared_task(name="research.tasks.refresh_official_sources")
def refresh_official_sources() -> dict[str, Any]:
    """Refresh direct, public-display-safe official sources and dashboards."""

    summary = refresh_official_data()
    if "fed-balance-sheet" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but the required fed-balance-sheet v1 "
            "atomic publication failed"
        )
    if "subsurface" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but the required subsurface v1 "
            "atomic publication failed"
        )
    if "operations" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but the required operations v1 "
            "atomic publication failed"
        )
    if "assets-fx" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but the required assets-fx v1 "
            "snapshot is stale or unavailable"
        )
    if "fx-vol" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but the required fx-vol v1 "
            "snapshot is stale or unavailable"
        )
    if "global-dollar" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but the required global-dollar v1 "
            "atomic publication failed"
        )
    if "transmission-chain" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "Official ingestion completed but transmission-chain v1 could not "
            "publish or retain a fully audited current snapshot"
        )
    return summary


@shared_task(name="research.tasks.refresh_h41_sources")
def refresh_h41_sources() -> dict[str, Any]:
    """Refresh the weekly Federal Reserve H.4.1 DDP archive."""

    summary = refresh_h41_data()
    if "reserves" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.4.1 ingestion completed but the required reserves v1 atomic "
            "publication failed"
        )
    if "fed-balance-sheet" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.4.1 ingestion completed but the required fed-balance-sheet v1 "
            "atomic publication failed"
        )
    if "transmission-chain" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.4.1 ingestion completed but transmission-chain v1 remained stale"
        )
    return summary


@shared_task(name="research.tasks.refresh_h8_sources")
def refresh_h8_sources() -> dict[str, Any]:
    """Refresh weekly Federal Reserve H.8 commercial-bank assets."""

    summary = refresh_h8_data()
    if "reserves" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.8 ingestion completed but the required reserves v1 atomic "
            "publication failed"
        )
    if "transmission-chain" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.8 ingestion completed but transmission-chain v1 remained stale"
        )
    return summary


@shared_task(name="research.tasks.refresh_prates_sources")
def refresh_prates_sources() -> dict[str, Any]:
    """Refresh daily IORB directly from the Federal Reserve PRATES package."""

    summary = refresh_prates_data()
    if "subsurface" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "PRATES ingestion completed but the required subsurface v1 "
            "atomic publication failed"
        )
    if "transmission-chain" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "PRATES ingestion completed but transmission-chain v1 remained stale"
        )
    return summary


@shared_task(name="research.tasks.refresh_h10_sources")
def refresh_h10_sources() -> dict[str, Any]:
    """Refresh Board H.10 daily FX reference series."""

    summary = refresh_h10_data()
    if "assets-fx" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.10 ingestion completed but the required assets-fx v1 "
            "snapshot is stale or unavailable"
        )
    if "fx-vol" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.10 ingestion completed but the required fx-vol v1 "
            "snapshot is stale or unavailable"
        )
    if "global-dollar" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.10 ingestion completed but the required global-dollar v1 "
            "atomic publication failed"
        )
    if "transmission-chain" in summary.get("stale_dashboard_keys", []):
        raise RuntimeError(
            "H.10 ingestion completed but transmission-chain v1 remained stale"
        )
    return summary


@shared_task(name="research.tasks.refresh_treasury_curve_sources")
def refresh_treasury_curve_sources() -> dict[str, Any]:
    """Refresh only the current-year nominal and real Treasury XML shards."""

    current_year = timezone.now().year
    summary = refresh_treasury_curve_data(
        start_year=current_year,
        end_year=current_year,
    )
    stale = {
        "yield-curve",
        "real-rates",
        "rates",
    } & set(summary.get("stale_dashboard_keys", []))
    if stale:
        raise RuntimeError(
            "Treasury curve v2 has no new or independently revalidatable retained "
            "publication for: " + ", ".join(sorted(stale))
        )
    return summary


@shared_task(name="research.tasks.refresh_credit_official_sources")
def refresh_credit_official_sources() -> dict[str, Any]:
    """Refresh Treasury HQM and Federal Reserve SLOOS official proxies."""

    summary = refresh_credit_official_data()
    stale = summary.get("stale_dashboard_keys", [])
    if stale:
        raise RuntimeError(
            "Credit Official v1 has no new or independently revalidatable retained "
            "publication for: " + ", ".join(sorted(stale))
        )
    return summary


@shared_task(
    name="research.tasks.refresh_macro_official_sources",
    soft_time_limit=240,
    time_limit=300,
)
def refresh_macro_official_sources() -> dict[str, Any]:
    """Refresh BEA, Census, G.19 and NY Fed consumer releases as one gated batch."""

    return refresh_macro_official_data()


@shared_task(name="research.tasks.refresh_crypto_sources")
def refresh_crypto_sources() -> dict[str, Any]:
    """Do not ingest restricted exchange data into the public production database."""

    return summarize_runs(
        [
            _skip(
                "okx",
                "public-market-data",
                "Written public-display and redistribution permission is not configured",
            ),
            _skip(
                "deribit",
                "public-market-data",
                "Written public-display and derived-data permission is not configured",
            ),
        ]
    )


@shared_task(name="research.tasks.refresh_filing_sources")
def refresh_filing_sources() -> dict[str, Any]:
    return refresh_sec_company_data()


@shared_task(name="research.tasks.refresh_github_sources")
def refresh_github_sources() -> dict[str, Any]:
    configured_repositories = _setting_list("GITHUB_REPOSITORIES")
    seed_categories = dict(GITHUB_PROJECT_SEEDS)
    repositories = configured_repositories or list(seed_categories)
    provider = GitHubProvider()
    runs = []
    try:
        for repo in repositories:
            result = provider.repository(repo)
            if result.ok:
                for record in result.records:
                    record["category"] = seed_categories.get(repo, "Configured repository")
            runs.append(record_provider_result(result, persist=store_github_repository))
    finally:
        provider.close()
    return summarize_runs(runs)


@shared_task(name="research.tasks.refresh_news_sources")
def refresh_news_sources() -> dict[str, Any]:
    """Refresh metadata-only government feeds from an explicit source whitelist."""

    providers = [
        (SECPressReleaseProvider(), (("press_releases", {}),)),
        (TreasuryPressReleaseProvider(), (("press_releases", {}),)),
        (
            BLSReleaseProvider(),
            tuple(
                ("releases", {"feed_name": feed_name})
                for feed_name in (
                    "employment-situation",
                    "job-openings",
                    "consumer-prices",
                    "producer-prices",
                )
            ),
        ),
    ]
    runs = []
    try:
        for provider, calls in providers:
            for method_name, kwargs in calls:
                result = getattr(provider, method_name)(**kwargs)
                runs.append(record_provider_result(result, persist=store_official_news))
    finally:
        for provider, _ in providers:
            provider.close()
    return summarize_runs(runs)


@shared_task(name="research.tasks.refresh_berkshire_letter_sources")
def refresh_berkshire_letter_sources() -> dict[str, Any]:
    """Refresh first-party shareholder-letter link metadata only."""

    return refresh_berkshire_letters()


@shared_task(name="research.tasks.refresh_market_sources")
def refresh_market_sources() -> dict[str, Any]:
    return summarize_runs(
        [
            _skip(
                "market-data",
                "daily-bars",
                "No production market-data license/provider is configured",
            )
        ]
    )


@shared_task(name="research.tasks.refresh_cftc_sources")
def refresh_cftc_sources() -> dict[str, Any]:
    provider = CFTCProvider()
    runs = []
    try:
        start_date = f"{max(timezone.localdate().year - 5, 2000)}-01-01"
        for report_type in ("tff-futures", "tff-combined"):
            result = provider.positions(report_type=report_type, start_date=start_date)
            runs.append(record_provider_result(result, persist=store_cftc_positions))
    finally:
        provider.close()
    return summarize_runs(runs)


@shared_task(name="research.tasks.publish_daily_evidence")
def publish_daily_evidence() -> dict[str, Any]:
    """Freeze a complete current component set or record an explicit partial run."""

    current_time = timezone.now()
    result = ProviderResult(
        provider="internal",
        dataset="daily-evidence-v1",
        records=[{"attempted_at": current_time.isoformat()}],
        fetched_at=current_time,
        metadata={"contract_version": DAILY_EVIDENCE_CONTRACT_VERSION},
    )

    def persist(_: ProviderResult, __: Any, run: IngestionRun) -> int:
        outcome = publish_daily_evidence_snapshot(
            now=current_time,
            batch_id=run.batch_id,
        )
        if not outcome.ok:
            result.metadata.update(
                {
                    "quality_status": "partial",
                    "reasons": list(outcome.errors),
                    "reason": "; ".join(outcome.errors),
                }
            )
            return 0
        result.metadata.update(
            {
                "dashboard_id": outcome.snapshot.pk,
                "dashboard_batch_id": str(outcome.snapshot.batch_id),
                "created": outcome.created,
                "research_date": outcome.snapshot.data["research_date"],
                "fingerprint": outcome.snapshot.data["fingerprint"],
            }
        )
        return 1

    return summarize_runs([record_provider_result(result, persist=persist)])


@shared_task(name="research.tasks.generate_daily_research")
def generate_daily_research() -> dict[str, Any]:
    """Create a deterministic draft only after a complete daily-evidence batch.

    A model provider can replace this service later.  Until then the generated
    copy is explicitly labelled as a system summary and keeps evidence IDs.
    """

    current_time = timezone.now()
    today = timezone.localdate(current_time)
    latest, readiness_errors = latest_ready_daily_evidence(now=current_time)
    if latest is None:
        return summarize_runs(
            [
                _skip(
                    "internal",
                    f"daily-research:{today}",
                    "; ".join(readiness_errors) or "No complete daily-evidence v1 batch",
                )
            ]
        )

    latest_data = dict(latest.data or {})
    research_date = date.fromisoformat(latest_data["research_date"])
    result = ProviderResult(
        provider="internal",
        dataset=f"daily-research:{research_date}",
        records=[{"dashboard_id": latest.pk, "batch_id": str(latest.batch_id)}],
        metadata={
            "dashboard_id": latest.pk,
            "dashboard_key": latest.key,
            "dashboard_batch_id": str(latest.batch_id),
            "contract_version": DAILY_EVIDENCE_CONTRACT_VERSION,
            "fingerprint": latest_data["fingerprint"],
        },
    )

    def persist(_: ProviderResult, __: Any, ___: IngestionRun) -> int:
        def contract_id(raw_id: Any) -> int | None:
            if isinstance(raw_id, bool):
                return None
            if isinstance(raw_id, int):
                return raw_id if 0 < raw_id <= (2**63) - 1 else None
            if isinstance(raw_id, str):
                stripped = raw_id.strip()
                if (
                    stripped
                    and len(stripped) <= 19
                    and stripped.isascii()
                    and stripped.isdigit()
                ):
                    parsed = int(stripped)
                    return parsed if 0 < parsed <= (2**63) - 1 else None
            return None

        locked = (
            DashboardSnapshot.objects.select_for_update(of=("self",))
            .select_related("source")
            .filter(key="daily-evidence")
            .order_by("-created_at", "-id")
            .first()
        )
        if (
            locked is None
            or locked.pk != latest.pk
            or locked.batch_id != latest.batch_id
            or not isinstance(locked.data, dict)
            or locked.data.get("fingerprint") != latest_data["fingerprint"]
        ):
            raise ValueError("a newer or changed daily-evidence candidate won the publication mutex")
        component_ids = []
        references = locked.data.get("component_snapshots")
        if isinstance(references, list):
            for reference in references:
                raw_id = reference.get("snapshot_id") if isinstance(reference, dict) else None
                normalized_id = contract_id(raw_id)
                if normalized_id is not None:
                    component_ids.append(normalized_id)
        metric_ids = [
            normalized_id
            for raw_id in locked.data.get("evidence_metric_ids", [])
            if (normalized_id := contract_id(raw_id)) is not None
        ] if isinstance(locked.data.get("evidence_metric_ids"), list) else []
        list(
            DashboardSnapshot.objects.select_for_update()
            .filter(pk__in=component_ids)
            .order_by("pk")
        )
        list(
            MetricSnapshot.objects.select_for_update()
            .filter(pk__in=metric_ids)
            .order_by("pk")
        )
        errors = validate_daily_evidence_snapshot(
            locked,
            now=current_time,
            require_current_components=True,
            require_latest_snapshot=True,
        )
        if errors:
            raise ValueError(
                "daily-evidence changed before draft persistence: " + "; ".join(errors)
            )
        locked_research_date = date.fromisoformat(locked.data["research_date"])
        if locked_research_date != research_date:
            raise ValueError("daily-evidence research date changed before persistence")
        slug = f"daily-system-summary-{research_date.isoformat()}"
        analysis = GeneratedAnalysis.objects.select_for_update().filter(slug=slug).first()
        if analysis and analysis.review_status != GeneratedAnalysis.ReviewStatus.DRAFT:
            result.metadata.update(
                {
                    "quality_status": "partial",
                    "skipped": True,
                    "skip_reason": "existing analysis is human or AI controlled",
                    "existing_review_status": analysis.review_status,
                }
            )
            return 0
        desired = {
            "title": f"{research_date.isoformat()} 数据批次摘要",
            "body": locked.summary or "当日数据批次已完成，等待人工研判。",
            "model_name": "deterministic-system-summary",
            "prompt_version": "daily-evidence-v1",
            "review_status": GeneratedAnalysis.ReviewStatus.DRAFT,
            "evidence": [
                {
                    "type": "dashboard_snapshot",
                    "key": locked.key,
                    "contract_version": DAILY_EVIDENCE_CONTRACT_VERSION,
                    "id": locked.pk,
                    "batch_id": str(locked.batch_id),
                    "fingerprint": locked.data["fingerprint"],
                    "component_batches": locked.data["component_batches"],
                    "metric_ids": locked.data["evidence_metric_ids"],
                }
            ],
            "data_as_of": locked.as_of,
            "stale": False,
        }
        if analysis is None:
            GeneratedAnalysis.objects.create(
                slug=slug,
                generated_at=current_time,
                **desired,
            )
        else:
            unchanged = all(getattr(analysis, field) == value for field, value in desired.items())
            if unchanged:
                return 1
            for field, value in desired.items():
                setattr(analysis, field, value)
            analysis.generated_at = current_time
            analysis.save(update_fields=[*desired, "generated_at", "updated_at"])
        return 1

    return summarize_runs([record_provider_result(result, persist=persist)])
