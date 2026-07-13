from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from research.models import CFTCPosition, IngestionRun, Observation, Source
from research.providers import ProviderResult
from research.services import ensure_source, record_provider_result, store_cftc_positions
from research.tasks import refresh_cftc_sources


def _position_record(**overrides):
    record = {
        "report_type": "tff-futures",
        "report_date": "2026-07-07",
        "published_at": "2026-07-10T19:31:01.434Z",
        "source_updated_at": "2026-07-10T19:31:01.434Z",
        "market_code": "13874A",
        "market_name": "E-MINI S&P 500 - CME",
        "trader_group": "asset-manager",
        "long_positions": 600,
        "short_positions": 200,
        "open_interest": 1000,
    }
    record.update(overrides)
    return record


@pytest.mark.django_db
def test_cftc_store_preserves_release_revision_and_fetch_timestamps():
    fetched_at = datetime(2026, 7, 10, 20, 5, tzinfo=UTC)
    result = ProviderResult(
        provider="cftc",
        dataset="cot:tff-futures",
        fetched_at=fetched_at,
        records=[_position_record()],
        metadata={"quality_status": "complete"},
    )

    run = record_provider_result(result, persist=store_cftc_positions)
    row = CFTCPosition.objects.get()

    assert run.status == IngestionRun.Status.SUCCESS
    assert row.report_date.isoformat() == "2026-07-07"
    assert row.published_at == datetime(2026, 7, 10, 19, 31, 1, 434000, tzinfo=UTC)
    assert row.source_updated_at == row.published_at
    assert row.fetched_at == fetched_at
    assert row.quality_status == Observation.Quality.FRESH
    assert row.source.licenses.get(is_current=True).public_display_allowed is True

    revised_result = ProviderResult(
        provider="cftc",
        dataset="cot:tff-futures",
        fetched_at=fetched_at + timedelta(hours=1),
        records=[
            _position_record(
                source_updated_at="2026-07-10T20:15:00Z",
                long_positions=650,
            )
        ],
        metadata={"quality_status": "complete"},
    )
    revised_run = record_provider_result(revised_result, persist=store_cftc_positions)
    row.refresh_from_db()

    assert revised_run.status == IngestionRun.Status.SUCCESS
    assert CFTCPosition.objects.count() == 1
    assert row.long_positions == 650
    assert row.source_updated_at == datetime(2026, 7, 10, 20, 15, tzinfo=UTC)
    assert row.batch_id == revised_run.batch_id


@pytest.mark.django_db
def test_cftc_missing_release_timestamp_is_partial_and_never_invented():
    result = ProviderResult(
        provider="cftc",
        dataset="cot:tff-futures",
        records=[_position_record(published_at=None, source_updated_at=None)],
        metadata={
            "quality_status": "partial",
            "missing_publication_timestamps": 1,
        },
    )

    run = record_provider_result(result, persist=store_cftc_positions)
    row = CFTCPosition.objects.get()

    assert run.status == IngestionRun.Status.PARTIAL
    assert row.published_at is None
    assert row.source_updated_at is None
    assert row.quality_status == Observation.Quality.ERROR


@pytest.mark.django_db
def test_cftc_page_uses_real_batch_dates_percentiles_and_history(client):
    source = ensure_source("cftc")
    now = timezone.now()
    latest_date = timezone.localdate() - timedelta(days=(timezone.localdate().weekday() - 1) % 7)
    published_at = now - timedelta(days=1)
    for index in range(30):
        report_date = latest_date - timedelta(weeks=29 - index)
        CFTCPosition.objects.create(
            report_type="tff-futures",
            report_date=report_date,
            published_at=(
                published_at if index == 29 else published_at - timedelta(weeks=29 - index)
            ),
            source_updated_at=(
                published_at if index == 29 else published_at - timedelta(weeks=29 - index)
            ),
            market_code="13874A",
            market_name="E-MINI S&P 500 - CME",
            trader_group="asset-manager",
            long_positions=500 + index * 10,
            short_positions=300,
            open_interest=2000,
            fetched_at=now,
            source=source,
        )

    response = client.get("/assets/equities/positioning/")
    body = response.content.decode()
    release_et = published_at.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")

    assert response.status_code == 200
    assert latest_date.isoformat() in body
    assert release_et in body
    assert "E-MINI S&amp;P 500 - CME" in body
    assert "极度净多" in body
    assert "100%" in body
    assert "cot-history-chart" in body
    assert "不使用演示持仓" not in body
    assert "U.S. Commodity Futures Trading Commission" in body


@pytest.mark.django_db
def test_cftc_page_hides_rows_after_current_licence_is_restricted(client):
    source = ensure_source("cftc")
    now = timezone.now()
    CFTCPosition.objects.create(
        report_type="tff-futures",
        report_date=timezone.localdate(),
        published_at=now,
        source_updated_at=now,
        market_code="13874A",
        market_name="LICENSE-GATED CONTRACT",
        trader_group="asset-manager",
        long_positions=600,
        short_positions=200,
        open_interest=1000,
        fetched_at=now,
        source=source,
    )
    current_licence = source.licenses.get(is_current=True)
    current_licence.status = Source.LicenseStatus.RESTRICTED
    current_licence.public_display_allowed = False
    current_licence.save(update_fields=["status", "public_display_allowed", "updated_at"])

    body = client.get("/assets/equities/positioning/").content.decode()

    assert "LICENSE-GATED CONTRACT" not in body
    assert "暂无通过当前来源许可校验的 CFTC TFF 批次" in body


def test_cftc_task_refreshes_both_tff_reports_with_five_year_history():
    with (
        patch("research.tasks.CFTCProvider") as provider_class,
        patch(
            "research.tasks.record_provider_result",
            side_effect=["futures-run", "combined-run"],
        ) as record_result,
        patch(
            "research.tasks.summarize_runs",
            return_value={"runs": [], "row_count": 2, "failed": 0, "partial": 0},
        ) as summarize,
        patch("research.tasks.timezone.localdate") as localdate,
    ):
        provider = provider_class.return_value
        provider.positions.side_effect = [
            ProviderResult(provider="cftc", dataset="cot:tff-futures", records=[{}]),
            ProviderResult(provider="cftc", dataset="cot:tff-combined", records=[{}]),
        ]
        localdate.return_value = datetime(2026, 7, 12, tzinfo=UTC).date()
        summary = refresh_cftc_sources()

    assert summary["row_count"] == 2
    assert provider.positions.call_args_list == [
        call(report_type="tff-futures", start_date="2021-01-01"),
        call(report_type="tff-combined", start_date="2021-01-01"),
    ]
    assert record_result.call_count == 2
    summarize.assert_called_once_with(["futures-run", "combined-run"])
    provider.close.assert_called_once_with()
