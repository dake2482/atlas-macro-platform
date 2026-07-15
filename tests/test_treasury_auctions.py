from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from django.utils import timezone

from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    SourceLicense,
    TreasuryAuction,
)
from research.official_data import (
    AUCTION_CONTRACT_VERSION,
    CORE_PUBLICATION_KEYS,
    RRP_TGA_CONTRACT_VERSION,
    _coordinate_auction_dashboard,
    _coordinate_rrp_tga_dashboard,
    _rrp_tga_snapshot_contract_is_valid,
    publish_official_dashboards,
)
from research.providers import FiscalDataProvider, ProviderResult
from research.services import (
    ensure_source,
    record_provider_result,
    store_series_observations,
    store_treasury_auctions,
)

TODAY = date(2026, 7, 13)
FROZEN_OFFICIAL_DATA_NOW = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_official_data_now(monkeypatch):
    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: FROZEN_OFFICIAL_DATA_NOW,
    )


def _auction_row(**overrides):
    row = {
        "record_date": "2026-07-10",
        "cusip": "912797AA1",
        "security_type": "Bill",
        "security_term": "4-Week",
        "announcemt_date": "2026-07-09",
        "auction_date": "2026-07-13",
        "issue_date": "2026-07-16",
        "maturity_date": "2026-08-13",
        "offering_amt": "95000000000",
        "total_tendered": "250000000000",
        "total_accepted": "95000000000",
        "bid_to_cover_ratio": "2.63",
        "high_yield": "3.44",
        "indirect_bidder_accepted": "50000000000",
        "direct_bidder_accepted": "5000000000",
        "primary_dealer_accepted": "40000000000",
    }
    row.update(overrides)
    return row


def _payload(rows, *, total_pages=1, total_count=None, count=None):
    total_count = len(rows) if total_count is None else total_count
    count = len(rows) if count is None else count
    return {
        "data": rows,
        "meta": {
            "count": count,
            "total-count": total_count,
            "total-pages": total_pages,
        },
    }


def _client(handler):
    return httpx.Client(
        base_url="https://api.fiscaldata.treasury.gov",
        transport=httpx.MockTransport(handler),
    )


def test_provider_uses_bounded_half_open_windows_fields_meta_and_deduplication():
    requests = []

    def handler(request):
        requests.append(request)
        query = request.url.params
        assert query["page[size]"] == "1000"
        assert query["fields"].split(",") == [
            "record_date",
            "cusip",
            "security_type",
            "security_term",
            "announcemt_date",
            "auction_date",
            "issue_date",
            "maturity_date",
            "offering_amt",
            "total_tendered",
            "total_accepted",
            "bid_to_cover_ratio",
            "high_yield",
            "indirect_bidder_accepted",
            "direct_bidder_accepted",
            "primary_dealer_accepted",
        ]
        if query["sort"] == "auction_date,cusip":
            assert query["filter"] == (
                "auction_date:gte:2026-04-14,auction_date:lt:2026-07-27"
            )
            return httpx.Response(200, json=_payload([_auction_row()]))
        assert query["sort"] == "issue_date,auction_date,cusip"
        assert query["filter"] == (
            "issue_date:gte:2026-07-13,issue_date:lt:2026-07-27"
        )
        return httpx.Response(
            200,
            json=_payload(
                [
                    _auction_row(
                        total_tendered="null",
                        total_accepted="null",
                        bid_to_cover_ratio="null",
                        high_yield="null",
                        indirect_bidder_accepted="null",
                        direct_bidder_accepted="null",
                        primary_dealer_accepted="null",
                    )
                ]
            ),
        )

    result = FiscalDataProvider(client=_client(handler)).treasury_auctions(
        as_of_date=TODAY
    )

    assert len(requests) == 2
    assert result.ok
    assert result.metadata["coverage_complete"] is True
    assert result.metadata["as_of_date_et"] == "2026-07-13"
    assert result.metadata["merged_record_count"] == 1
    assert result.metadata["deduplicated_record_count"] == 1
    assert len(result.records) == 1
    assert result.records[0]["bid_to_cover_ratio"] == Decimal("2.63")
    assert result.records[0]["record_date"] == "2026-07-10"


def test_provider_accepts_official_complete_empty_slices_with_zero_pages():
    provider = FiscalDataProvider(
        client=_client(
            lambda request: httpx.Response(
                200,
                json=_payload([], total_pages=0, total_count=0, count=0),
            )
        )
    )

    result = provider.treasury_auctions(as_of_date=TODAY)

    assert result.ok
    assert result.records == []
    assert result.metadata["coverage_complete"] is True
    assert result.metadata["allow_empty_success"] is True
    assert all(item["coverage_complete"] for item in result.metadata["slices"])


@pytest.mark.parametrize(
    "payload",
    [
        _payload([_auction_row()], count=0),
        _payload([{"cusip": "", "auction_date": "2026-07-13"}]),
        _payload([_auction_row(auction_date="not-a-date")]),
        {"data": [_auction_row()], "meta": {"count": 1}},
    ],
)
def test_provider_fails_closed_on_incomplete_meta_or_rejected_rows(payload):
    result = FiscalDataProvider(
        client=_client(lambda request: httpx.Response(200, json=payload))
    ).treasury_auctions(as_of_date=TODAY)

    assert result.records == []
    assert result.metadata["coverage_complete"] is False
    assert result.metadata["quality_status"] == "partial"
    assert result.metadata["slices"][0]["coverage_complete"] is False


def test_provider_rejects_conflicting_duplicate_identity():
    call_count = 0

    def handler(request):
        nonlocal call_count
        call_count += 1
        amount = "95000000000" if call_count == 1 else "96000000000"
        return httpx.Response(
            200,
            json=_payload([_auction_row(offering_amt=amount)]),
        )

    result = FiscalDataProvider(client=_client(handler)).treasury_auctions(
        as_of_date=TODAY
    )

    assert not result.ok
    assert "conflicting duplicate auction identity" in result.error
    assert result.metadata["coverage_complete"] is False


@pytest.mark.parametrize("case", ["auction-outside", "issue-date-missing"])
def test_provider_rejects_rows_outside_their_exact_slice(case):
    def handler(request):
        if request.url.params["sort"] == "auction_date,cusip":
            rows = (
                [_auction_row(auction_date="2026-04-13")]
                if case == "auction-outside"
                else []
            )
        else:
            rows = (
                [_auction_row(issue_date=None)]
                if case == "issue-date-missing"
                else []
            )
        return httpx.Response(
            200,
            json=_payload(
                rows,
                total_pages=1 if rows else 0,
                total_count=len(rows),
                count=len(rows),
            ),
        )

    result = FiscalDataProvider(client=_client(handler)).treasury_auctions(
        as_of_date=TODAY
    )

    assert result.records == []
    assert result.metadata["coverage_complete"] is False
    rejected = [
        item for item in result.metadata["slices"] if item["rejected_count"]
    ]
    assert len(rejected) == 1
    assert rejected[0]["normalized_count"] == 0


@pytest.mark.django_db
def test_store_reuses_one_aware_fetch_time_batch_and_reconciles_complete_window():
    source = ensure_source("treasury-fiscal-data")
    old = TreasuryAuction.objects.create(
        cusip="OLD",
        security_type="Bill",
        security_term="8-Week",
        auction_date=date(2026, 7, 14),
        issue_date=date(2026, 7, 16),
        fetched_at=timezone.now(),
        source=source,
    )
    fetched_at = datetime(2026, 7, 13, 10, 30)
    result = ProviderResult(
        provider="treasury-fiscal-data",
        dataset="treasury-securities-auctions",
        fetched_at=fetched_at,
        records=[
            {
                "cusip": "NEW1",
                "security_type": "Bill",
                "security_term": "4-Week",
                "announcement_date": "2026-07-09",
                "auction_date": "2026-07-13",
                "issue_date": "2026-07-16",
                "maturity_date": "2026-08-13",
                "offering_amt": Decimal("95000000000"),
                "total_tendered": Decimal("250000000000"),
                "total_accepted": Decimal("95000000000"),
                "bid_to_cover_ratio": Decimal("2.63"),
                "high_yield": Decimal("3.44"),
                "indirect_bidder_accepted": Decimal("50000000000"),
                "direct_bidder_accepted": Decimal("5000000000"),
                "primary_dealer_accepted": Decimal("40000000000"),
            },
            {
                "cusip": "NEW2",
                "security_type": "Note",
                "security_term": "3-Year",
                "announcement_date": "2026-07-09",
                "auction_date": "2026-07-14",
                "issue_date": "2026-07-16",
                "maturity_date": "2029-07-16",
                "offering_amt": Decimal("58000000000"),
                "total_tendered": None,
                "total_accepted": None,
                "bid_to_cover_ratio": None,
                "high_yield": None,
                "indirect_bidder_accepted": None,
                "direct_bidder_accepted": None,
                "primary_dealer_accepted": None,
            },
        ],
        metadata={
            "coverage_complete": True,
            "allow_empty_success": True,
            "as_of_date_et": TODAY.isoformat(),
            "timezone": "America/New_York",
            "merged_record_count": 2,
            "deduplicated_record_count": 2,
            "slices": [
                {
                    "name": "auction_window",
                    "date_field": "auction_date",
                    "lower": "2026-04-14",
                    "upper_exclusive": "2026-07-27",
                    "returned_count": 2,
                    "normalized_count": 2,
                    "rejected_count": 0,
                    "total_count": 2,
                    "count": 2,
                    "total_pages": 1,
                    "page_size": 1000,
                    "coverage_complete": True,
                },
                {
                    "name": "issue_window",
                    "date_field": "issue_date",
                    "lower": "2026-07-13",
                    "upper_exclusive": "2026-07-27",
                    "returned_count": 2,
                    "normalized_count": 2,
                    "rejected_count": 0,
                    "total_count": 2,
                    "count": 2,
                    "total_pages": 1,
                    "page_size": 1000,
                    "coverage_complete": True,
                },
            ],
        },
    )

    run = record_provider_result(result, persist=store_treasury_auctions)

    assert run.status == IngestionRun.Status.SUCCESS
    assert run.row_count == 2
    stored = list(TreasuryAuction.objects.filter(batch_id=run.batch_id))
    assert len(stored) == 2
    assert {item.fetched_at for item in stored} == {
        fetched_at.replace(tzinfo=UTC)
    }
    assert not TreasuryAuction.objects.filter(pk=old.pk).exists()
    licence = SourceLicense.objects.get(source=source, is_current=True)
    assert licence.terms_url == (
        "https://www.treasurydirect.gov/legal-information/developers/"
        "web-api-terms/"
    )
    assert "not affiliated with or endorsed" in licence.required_notice
    assert "marks" in licence.scope


def _stored_record(
    *,
    cusip: str,
    auction_date: str,
    issue_date: str,
    offering: str,
    bid_to_cover: str | None,
    term: str,
    announcement_date: str = "2026-07-09",
):
    return {
        "cusip": cusip,
        "security_type": "Bill" if "Week" in term else "Note",
        "security_term": term,
        "announcement_date": announcement_date,
        "auction_date": auction_date,
        "issue_date": issue_date,
        "maturity_date": "2029-07-30",
        "offering_amt": Decimal(offering),
        "total_tendered": (
            Decimal(offering) * Decimal("2.5") if bid_to_cover else None
        ),
        "total_accepted": Decimal(offering) if bid_to_cover else None,
        "bid_to_cover_ratio": (
            Decimal(bid_to_cover) if bid_to_cover else None
        ),
        "high_yield": Decimal("3.45") if bid_to_cover else None,
        "indirect_bidder_accepted": None,
        "direct_bidder_accepted": None,
        "primary_dealer_accepted": None,
    }


def _complete_run(*, cycle: str = "cycle-1", records=None, fetched_at=None):
    records = records if records is not None else [
        # Today's result must appear only in results, while its future issue
        # date remains in the settlement table and gross issue totals.
        _stored_record(
            cusip="TODAY",
            auction_date="2026-07-13",
            issue_date="2026-07-16",
            offering="95000000000",
            bid_to_cover="2.63",
            term="4-Week",
        ),
        _stored_record(
            cusip="NEXT",
            auction_date="2026-07-14",
            issue_date="2026-07-16",
            offering="58000000000",
            bid_to_cover=None,
            term="3-Year",
        ),
        # Completed before the window but issuing today must be retained.
        _stored_record(
            cusip="ISSUE-TODAY",
            auction_date="2026-07-09",
            issue_date="2026-07-13",
            offering="72000000000",
            bid_to_cover="2.51",
            term="8-Week",
        ),
        _stored_record(
            cusip="RECENT",
            auction_date="2026-07-12",
            issue_date="2026-07-12",
            offering="39000000000",
            bid_to_cover="2.42",
            term="10-Year",
        ),
        # auction_date is inside the formal 14-day window; issue_date is the
        # exclusive upper boundary and must not enter issue totals/table.
        _stored_record(
            cusip="UPPER",
            auction_date="2026-07-26",
            issue_date="2026-07-27",
            offering="20000000000",
            bid_to_cover=None,
            term="2-Year",
        ),
    ]
    issue_count = sum(
        date(2026, 7, 13)
        <= date.fromisoformat(str(item["issue_date"]))
        < date(2026, 7, 27)
        for item in records
        if item.get("issue_date")
    )
    fetched_at = fetched_at or datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    return record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="treasury-securities-auctions",
            records=records,
            fetched_at=fetched_at,
            metadata={
                "refresh_cycle_id": cycle,
                "coverage_complete": True,
                "allow_empty_success": True,
                "as_of_date_et": TODAY.isoformat(),
                "timezone": "America/New_York",
                "merged_record_count": len(records),
                "deduplicated_record_count": issue_count,
                "slices": [
                    {
                        "name": "auction_window",
                        "date_field": "auction_date",
                        "lower": "2026-04-14",
                        "upper_exclusive": "2026-07-27",
                        "returned_count": len(records),
                        "normalized_count": len(records),
                        "rejected_count": 0,
                        "total_count": len(records),
                        "count": len(records),
                        "total_pages": 1 if records else 0,
                        "page_size": 1000,
                        "coverage_complete": True,
                    },
                    {
                        "name": "issue_window",
                        "date_field": "issue_date",
                        "lower": "2026-07-13",
                        "upper_exclusive": "2026-07-27",
                        "returned_count": issue_count,
                        "normalized_count": issue_count,
                        "rejected_count": 0,
                        "total_count": issue_count,
                        "count": issue_count,
                        "total_pages": 1 if issue_count else 0,
                        "page_size": 1000,
                        "coverage_complete": True,
                    },
                ],
            },
        ),
        persist=store_treasury_auctions,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "corruption",
    [
        "wide-window",
        "reversed-window",
        "wrong-date-field",
        "corrupt-merged-count",
        "negative-count",
        "missing-timezone",
    ],
)
def test_store_rejects_corrupt_reconciliation_contract_before_mutation(corruption):
    good = _complete_run(cycle="stored-good")
    before = {
        (item.cusip, item.auction_date): item.batch_id
        for item in TreasuryAuction.objects.all()
    }
    metadata = deepcopy(good.metadata)
    returned_total = sum(item["returned_count"] for item in metadata["slices"])
    metadata["merged_record_count"] = 0
    metadata["deduplicated_record_count"] = returned_total
    if corruption == "wide-window":
        metadata["slices"][0]["upper_exclusive"] = "2030-01-01"
    elif corruption == "reversed-window":
        metadata["slices"][0]["lower"] = "2026-07-28"
    elif corruption == "wrong-date-field":
        metadata["slices"][1]["date_field"] = "auction_date"
    elif corruption == "corrupt-merged-count":
        metadata["merged_record_count"] = len(before)
    elif corruption == "negative-count":
        metadata["slices"][0].update(
            {
                "returned_count": -1,
                "normalized_count": -1,
                "total_count": -1,
                "count": -1,
            }
        )
    elif corruption == "missing-timezone":
        metadata.pop("timezone")
    candidate = ProviderResult(
        provider="treasury-fiscal-data",
        dataset="treasury-securities-auctions",
        records=[],
        fetched_at=datetime(2026, 7, 13, 10, 10, tzinfo=UTC),
        metadata=metadata,
    )

    run = record_provider_result(candidate, persist=store_treasury_auctions)

    assert run.status == IngestionRun.Status.FAILED
    after = {
        (item.cusip, item.auction_date): item.batch_id
        for item in TreasuryAuction.objects.all()
    }
    assert after == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fetched_at",
    [
        datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    ],
)
def test_store_rejects_future_or_wrong_et_fetch_before_mutation(fetched_at):
    good = _complete_run(cycle="stored-good")
    before = {
        (item.cusip, item.auction_date): item.batch_id
        for item in TreasuryAuction.objects.all()
    }
    metadata = deepcopy(good.metadata)
    returned_total = sum(item["returned_count"] for item in metadata["slices"])
    metadata["merged_record_count"] = 0
    metadata["deduplicated_record_count"] = returned_total

    run = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="treasury-securities-auctions",
            records=[],
            fetched_at=fetched_at,
            metadata=metadata,
        ),
        persist=store_treasury_auctions,
    )

    assert run.status == IngestionRun.Status.FAILED
    after = {
        (item.cusip, item.auction_date): item.batch_id
        for item in TreasuryAuction.objects.all()
    }
    assert after == before


@pytest.mark.django_db
def test_store_rejects_duplicate_identity_before_upsert_or_reconciliation():
    good = _complete_run(cycle="stored-good")
    before = {
        (item.cusip, item.auction_date): item.batch_id
        for item in TreasuryAuction.objects.all()
    }
    duplicate = _stored_record(
        cusip="DUP",
        auction_date="2026-07-14",
        issue_date="2026-07-28",
        offering="58000000000",
        bid_to_cover=None,
        term="3-Year",
    )
    metadata = deepcopy(good.metadata)
    auction_slice, issue_slice = metadata["slices"]
    auction_slice.update(
        {
            "returned_count": 2,
            "normalized_count": 2,
            "total_count": 2,
            "count": 2,
            "total_pages": 1,
        }
    )
    issue_slice.update(
        {
            "returned_count": 0,
            "normalized_count": 0,
            "total_count": 0,
            "count": 0,
            "total_pages": 0,
        }
    )
    metadata["merged_record_count"] = 2
    metadata["deduplicated_record_count"] = 0

    run = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="treasury-securities-auctions",
            records=[duplicate, dict(duplicate)],
            fetched_at=datetime(2026, 7, 13, 10, 10, tzinfo=UTC),
            metadata=metadata,
        ),
        persist=store_treasury_auctions,
    )

    assert run.status == IngestionRun.Status.FAILED
    after = {
        (item.cusip, item.auction_date): item.batch_id
        for item in TreasuryAuction.objects.all()
    }
    assert after == before


@pytest.mark.django_db
def test_auction_v1_publishes_exact_batch_half_open_sections_and_lineage(client):
    run = _complete_run()

    dashboards, stale = _coordinate_auction_dashboard(
        [run], as_of_date=TODAY
    )

    assert stale == set()
    assert len(dashboards) == 1
    snapshot = DashboardSnapshot.objects.get(
        key="auctions", data__contract_version=AUCTION_CONTRACT_VERSION
    )
    data = snapshot.data
    assert data["component_batches"] == [str(run.batch_id)]
    assert data["as_of_date_et"] == TODAY.isoformat()
    metrics = {item["key"]: item for item in data["metrics"]}
    assert metrics["days-to-next-auction"]["value"] == 1.0
    assert "2026-07-14" in metrics["days-to-next-auction"]["display_value"]
    assert metrics["formal-auction-gross-7d"]["value"] == 58.0
    assert metrics["issue-gross-7d"]["value"] == 225.0
    assert metrics["issue-gross-14d"]["value"] == 225.0
    assert metrics["latest-bid-to-cover"]["value"] == 2.63
    for metric in metrics.values():
        assert metric["batch_id"] == str(run.batch_id)
        assert metric["license_scope"]
        assert "fallback_source" in metric

    sections = {item["key"]: item for item in data["sections"]}
    formal_keys = {row["key"] for row in sections["formal-auctions-14d"]["rows"]}
    issue_keys = {row["key"] for row in sections["issue-settlement-14d"]["rows"]}
    result_keys = {row["key"] for row in sections["recent-results-90d"]["rows"]}
    assert "TODAY-2026-07-13" not in formal_keys
    assert "TODAY-2026-07-13" in result_keys
    assert "TODAY-2026-07-13" in issue_keys
    assert "ISSUE-TODAY-2026-07-09" in issue_keys
    assert "UPPER-2026-07-26" not in issue_keys
    today_result = next(
        row
        for row in sections["recent-results-90d"]["rows"]
        if row["key"] == "TODAY-2026-07-13"
    )
    assert today_result["display_lineage"]["field"] == "bid_to_cover_ratio"
    assert {
        item["field"] for item in today_result["additional_lineage"]
    } == {"offering_amount", "high_yield"}
    for section in sections.values():
        for row in section["rows"]:
            assert row["batch_id"] == str(run.batch_id)
            assert row["license_scope"]
            assert row["fallback_source"] is None
            assert row["input_lineage"]

    normalized = MetricSnapshot.objects.get(
        key="auctions-issue-gross-7d", batch_id=snapshot.batch_id
    )
    assert normalized.value == Decimal("225.0")
    assert len(normalized.metadata["input_lineage"]) == 3

    response = client.get("/rates/auctions/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "未来 14 天发行/结算日历" in body
    assert "许可" in body
    assert "fallback 无" in body
    assert f"批次：{run.batch_id}" in body
    assert "许可：" in body
    assert "fallback：无" in body
    assert "未来 14 天净抽水" not in body
    assert "未来 TGA 流入" not in body


@pytest.mark.django_db
def test_missing_result_offering_amount_retains_previous_complete_snapshot():
    good = _complete_run(cycle="good")
    _coordinate_auction_dashboard([good], as_of_date=TODAY)
    snapshot = DashboardSnapshot.objects.get(key="auctions")
    original_batches = list(snapshot.data["component_batches"])
    malformed = _stored_record(
        cusip="MISSING-AMOUNT",
        auction_date="2026-07-13",
        issue_date="2026-07-14",
        offering="1",
        bid_to_cover="2.50",
        term="4-Week",
    )
    malformed["offering_amt"] = None
    candidate = _complete_run(cycle="missing-amount", records=[malformed])

    dashboards, stale = _coordinate_auction_dashboard(
        [candidate], as_of_date=TODAY
    )

    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == {"auctions"}
    assert snapshot.quality_status == "stale"
    assert snapshot.data["component_batches"] == original_batches
    assert "missing or non-positive offering amount" in str(
        snapshot.data["refresh_failure"]
    )


@pytest.mark.django_db
def test_complete_empty_windows_publish_honest_zero_aggregates():
    run = _complete_run(records=[])
    # Both official slices are complete empty windows.
    run.metadata["slices"][1].update(
        {
            "returned_count": 0,
            "normalized_count": 0,
            "total_count": 0,
            "count": 0,
            "total_pages": 0,
        }
    )
    run.save(update_fields=["metadata", "updated_at"])

    dashboards, stale = _coordinate_auction_dashboard(
        [run], as_of_date=TODAY
    )

    assert stale == set()
    assert len(dashboards) == 1
    metrics = {
        item["key"]: item
        for item in DashboardSnapshot.objects.get(key="auctions").data["metrics"]
    }
    assert metrics["formal-auction-gross-7d"]["value"] == 0.0
    assert metrics["issue-gross-7d"]["value"] == 0.0
    assert metrics["issue-gross-14d"]["value"] == 0.0
    assert metrics["days-to-next-auction"]["value"] is None
    assert metrics["latest-bid-to-cover"]["value"] is None


@pytest.mark.django_db
def test_failed_latest_run_stales_previous_and_old_replay_is_noop():
    successful = _complete_run()
    _coordinate_auction_dashboard([successful], as_of_date=TODAY)
    snapshot = DashboardSnapshot.objects.get(key="auctions")

    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="treasury-securities-auctions",
            error="upstream 500",
        ),
        persist=store_treasury_auctions,
    )
    dashboards, stale = _coordinate_auction_dashboard(
        [failed], as_of_date=TODAY
    )
    snapshot.refresh_from_db()
    failure = snapshot.data["refresh_failure"]
    assert dashboards == []
    assert stale == {"auctions"}
    assert snapshot.quality_status == "stale"

    dashboards, stale = _coordinate_auction_dashboard(
        [successful], as_of_date=TODAY
    )
    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == set()
    assert snapshot.data["refresh_failure"] == failure


@pytest.mark.django_db
def test_same_value_recovery_refreshes_lineage_without_duplicate_snapshot():
    first = _complete_run(cycle="first")
    _coordinate_auction_dashboard([first], as_of_date=TODAY)
    snapshot = DashboardSnapshot.objects.get(key="auctions")
    original_id = snapshot.pk
    failed = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="treasury-securities-auctions",
            error="temporary failure",
        ),
        persist=store_treasury_auctions,
    )
    _coordinate_auction_dashboard([failed], as_of_date=TODAY)

    recovered = _complete_run(
        cycle="recovered",
        fetched_at=datetime(2026, 7, 13, 10, 5, tzinfo=UTC),
    )
    dashboards, stale = _coordinate_auction_dashboard(
        [recovered], as_of_date=TODAY
    )

    snapshot.refresh_from_db()
    assert dashboards == []  # same fingerprint updates the existing snapshot
    assert stale == set()
    assert snapshot.pk == original_id
    assert "refresh_failure" not in snapshot.data
    assert snapshot.quality_status != "stale"
    assert snapshot.data["component_batches"] == [str(recovered.batch_id)]
    assert DashboardSnapshot.objects.filter(key="auctions").count() == 1


@pytest.mark.django_db
def test_revoked_treasury_display_licence_retains_previous_stale():
    good = _complete_run(cycle="good")
    _coordinate_auction_dashboard([good], as_of_date=TODAY)
    current = SourceLicense.objects.get(
        source__key="treasury-fiscal-data", is_current=True
    )
    current.public_display_allowed = False
    current.derived_display_allowed = False
    current.reviewed_by = "licence-admin"
    current.reviewed_at = timezone.now()
    current.save(
        update_fields=[
            "public_display_allowed",
            "derived_display_allowed",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )
    candidate = _complete_run(cycle="unlicensed")

    dashboards, stale = _coordinate_auction_dashboard(
        [candidate], as_of_date=TODAY
    )

    snapshot = DashboardSnapshot.objects.get(key="auctions")
    assert dashboards == []
    assert stale == {"auctions"}
    assert snapshot.quality_status == "stale"
    assert "unlicensed" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
def test_generic_publisher_cannot_bypass_auction_coordinator():
    assert "auctions" not in CORE_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"auctions"}) == []
    assert not DashboardSnapshot.objects.filter(key="auctions").exists()


def _rrp_tga_runs(*, cycle: str, fetched_at=None):
    fetched_at = fetched_at or datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    onrrp_records = []
    for value_date, balance, rate, participants in (
        ("2026-07-09", "700", "3.50", "4"),
        ("2026-07-10", "545", "3.50", "3"),
    ):
        onrrp_records.extend(
            [
                {
                    "series_id": "ONRRP",
                    "date": value_date,
                    "value": Decimal(balance),
                },
                {
                    "series_id": "ONRRP-RATE",
                    "date": value_date,
                    "value": Decimal(rate),
                },
                {
                    "series_id": "ONRRP-PARTICIPANTS",
                    "date": value_date,
                    "value": Decimal(participants),
                },
            ]
        )
    onrrp = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            records=onrrp_records,
            fetched_at=fetched_at,
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )
    tga = record_provider_result(
        ProviderResult(
            provider="treasury-fiscal-data",
            dataset="daily-treasury-statement:tga",
            records=[
                {
                    "series_id": "TGA",
                    "date": "2026-07-09",
                    "value": Decimal("770000"),
                },
                {
                    "series_id": "TGA",
                    "date": "2026-07-10",
                    "value": Decimal("760000"),
                },
            ],
            fetched_at=fetched_at,
            metadata={"refresh_cycle_id": cycle},
        ),
        persist=store_series_observations,
    )
    return onrrp, tga


@pytest.mark.django_db
def test_rrp_tga_v1_requires_one_cycle_and_preserves_exact_component_lineage(client):
    cycle = "rrp-cycle"
    onrrp, tga = _rrp_tga_runs(cycle=cycle)
    auctions = _complete_run(cycle=cycle)

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )

    assert stale == set()
    assert len(dashboards) == 1
    snapshot = DashboardSnapshot.objects.get(
        key="rrp-tga", data__contract_version=RRP_TGA_CONTRACT_VERSION
    )
    data = snapshot.data
    assert set(data["component_batches"]) == {
        str(onrrp.batch_id),
        str(tga.batch_id),
        str(auctions.batch_id),
    }
    assert data["refresh_cycle_id"] == cycle
    metrics = {item["key"]: item for item in data["metrics"]}
    assert metrics["onrrp"]["value"] == 0.545
    assert metrics["onrrp-rate"]["value"] == 3.5
    assert metrics["onrrp-participants"]["value"] == 3.0
    assert metrics["tga"]["value"] == 760.0
    assert metrics["issue-gross-7d"]["value"] == 225.0
    assert metrics["issue-gross-14d"]["value"] == 225.0
    for metric in metrics.values():
        assert metric["license_scope"]
        assert metric["fallback_source"] is None
        assert metric["metadata"]["input_lineage"]
    assert {item["key"] for item in data["charts"]} == {
        "rrp-tga-history",
        "gross-issue-calendar",
    }
    issue_section = next(
        item
        for item in data["sections"]
        if item["key"] == "issue-settlement-14d"
    )
    assert issue_section["rows"]
    assert all(
        row["batch_id"] == str(auctions.batch_id)
        for row in issue_section["rows"]
    )

    response = client.get("/liquidity/rrp-tga/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "未来 14 天发行/结算日历" in body
    assert "不同有效日不强行拼成净流动性" in body
    assert "未来 14 天净抽水" not in body
    assert "14D 净冲击" not in body
    assert "批次：" in body and str(onrrp.batch_id) in body
    assert f"批次：{auctions.batch_id}" in body
    assert "许可：" in body
    assert "fallback：无" in body


@pytest.mark.django_db
def test_rrp_tga_mixed_cycle_retains_previous_complete_snapshot_stale():
    onrrp, tga = _rrp_tga_runs(cycle="good")
    auctions = _complete_run(cycle="good")
    _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )
    snapshot = DashboardSnapshot.objects.get(key="rrp-tga")
    original_batches = list(snapshot.data["component_batches"])

    newer_onrrp, newer_tga = _rrp_tga_runs(cycle="new")
    mismatched_auctions = _complete_run(cycle="different")
    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [newer_onrrp, newer_tga, mismatched_auctions], as_of_date=TODAY
    )

    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == {"rrp-tga"}
    assert snapshot.quality_status == "stale"
    assert snapshot.data["component_batches"] == original_batches
    assert "invalid-cycle" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
def test_rrp_tga_latest_failure_stales_and_old_cycle_replay_is_noop():
    onrrp, tga = _rrp_tga_runs(cycle="good")
    auctions = _complete_run(cycle="good")
    _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )
    snapshot = DashboardSnapshot.objects.get(key="rrp-tga")
    failed = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            error="upstream 500",
        ),
        persist=store_series_observations,
    )

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [failed], as_of_date=TODAY
    )
    snapshot.refresh_from_db()
    failure = snapshot.data["refresh_failure"]
    assert dashboards == []
    assert stale == {"rrp-tga"}

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )
    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == set()
    assert snapshot.data["refresh_failure"] == failure


@pytest.mark.django_db
def test_generic_publisher_cannot_bypass_rrp_tga_coordinator():
    assert "rrp-tga" not in CORE_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"rrp-tga"}) == []
    assert not DashboardSnapshot.objects.filter(key="rrp-tga").exists()


@pytest.mark.django_db
def test_rrp_tga_revoked_required_licence_retains_previous_stale():
    onrrp, tga = _rrp_tga_runs(cycle="good")
    auctions = _complete_run(cycle="good")
    _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )
    licence = SourceLicense.objects.get(
        source__key="ny-fed-markets", is_current=True
    )
    licence.public_display_allowed = False
    licence.derived_display_allowed = False
    licence.reviewed_by = "licence-admin"
    licence.reviewed_at = timezone.now()
    licence.save(
        update_fields=[
            "public_display_allowed",
            "derived_display_allowed",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )
    candidate_onrrp, candidate_tga = _rrp_tga_runs(cycle="revoked")
    candidate_auctions = _complete_run(cycle="revoked")

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [candidate_onrrp, candidate_tga, candidate_auctions], as_of_date=TODAY
    )

    snapshot = DashboardSnapshot.objects.get(key="rrp-tga")
    assert dashboards == []
    assert stale == {"rrp-tga"}
    assert snapshot.quality_status == "stale"
    assert "unlicensed" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
def test_rrp_tga_rejects_fallback_historical_point_and_keeps_previous():
    onrrp, tga = _rrp_tga_runs(cycle="good")
    auctions = _complete_run(cycle="good")
    _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )
    candidate_onrrp, candidate_tga = _rrp_tga_runs(cycle="fallback-history")
    candidate_auctions = _complete_run(cycle="fallback-history")
    historical = Observation.objects.get(
        series__key="onrrp",
        batch_id=candidate_onrrp.batch_id,
        value_date__date=date(2026, 7, 9),
    )
    historical.fallback_source = ensure_source("internal")
    historical.quality_status = Observation.Quality.FALLBACK
    historical.save(
        update_fields=["fallback_source", "quality_status", "updated_at"]
    )

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [candidate_onrrp, candidate_tga, candidate_auctions], as_of_date=TODAY
    )

    snapshot = DashboardSnapshot.objects.get(key="rrp-tga")
    assert dashboards == []
    assert stale == {"rrp-tga"}
    assert snapshot.quality_status == "stale"
    assert "history point is fallback" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
def test_rrp_tga_same_value_recovery_updates_all_batches_and_normalized_lineage():
    first_onrrp, first_tga = _rrp_tga_runs(cycle="first")
    first_auctions = _complete_run(cycle="first")
    _coordinate_rrp_tga_dashboard(
        [first_onrrp, first_tga, first_auctions], as_of_date=TODAY
    )
    snapshot = DashboardSnapshot.objects.get(key="rrp-tga")
    original_id = snapshot.pk
    failed = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            error="temporary failure",
        ),
        persist=store_series_observations,
    )
    _coordinate_rrp_tga_dashboard([failed], as_of_date=TODAY)

    recovered_onrrp, recovered_tga = _rrp_tga_runs(
        cycle="recovered",
        fetched_at=datetime(2026, 7, 13, 10, 5, tzinfo=UTC),
    )
    recovered_auctions = _complete_run(
        cycle="recovered",
        fetched_at=datetime(2026, 7, 13, 10, 5, tzinfo=UTC),
    )
    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [recovered_onrrp, recovered_tga, recovered_auctions],
        as_of_date=TODAY,
    )

    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == set()
    assert snapshot.pk == original_id
    assert "refresh_failure" not in snapshot.data
    expected_batches = {
        str(recovered_onrrp.batch_id),
        str(recovered_tga.batch_id),
        str(recovered_auctions.batch_id),
    }
    assert set(snapshot.data["component_batches"]) == expected_batches
    assert DashboardSnapshot.objects.filter(key="rrp-tga").count() == 1
    normalized = MetricSnapshot.objects.get(
        key="rrp-tga-onrrp", batch_id=snapshot.batch_id
    )
    assert normalized.metadata["component_batch_id"] == str(
        recovered_onrrp.batch_id
    )
    assert normalized.metadata["input_lineage"][0]["batch_id"] == str(
        recovered_onrrp.batch_id
    )


@pytest.mark.django_db
def test_rrp_tga_rejects_duplicate_exact_batch_value_date():
    onrrp, tga = _rrp_tga_runs(cycle="duplicate")
    auctions = _complete_run(cycle="duplicate")
    original = Observation.objects.get(
        series__key="onrrp",
        batch_id=onrrp.batch_id,
        value_date__date=date(2026, 7, 10),
    )
    Observation.objects.create(
        series=original.series,
        value=original.value,
        value_date=original.value_date,
        as_of=original.as_of,
        fetched_at=original.fetched_at,
        batch_id=original.batch_id,
        source=original.source,
        quality_status=original.quality_status,
    )

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )

    assert dashboards == []
    assert stale == {"rrp-tga"}
    assert not DashboardSnapshot.objects.filter(key="rrp-tga").exists()


@pytest.mark.django_db
def test_rrp_tga_postcondition_rejects_corrupt_chart_point_lineage():
    onrrp, tga = _rrp_tga_runs(cycle="postcondition")
    auctions = _complete_run(cycle="postcondition")
    _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )
    snapshot = DashboardSnapshot.objects.get(key="rrp-tga")
    data = deepcopy(snapshot.data)
    history = next(
        item for item in data["charts"] if item["key"] == "rrp-tga-history"
    )
    point = next(row for row in history["data"] if "ON RRP" in row)
    point["_lineage"]["ON RRP"]["fallback_source"] = "corrupt-fallback"
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])

    assert not _rrp_tga_snapshot_contract_is_valid(
        snapshot,
        selected={"onrrp": onrrp, "tga": tga, "auctions": auctions},
        today_et=TODAY,
    )


@pytest.mark.django_db
def test_rrp_tga_rejects_any_future_exact_batch_history_point():
    onrrp, tga = _rrp_tga_runs(cycle="future")
    auctions = _complete_run(cycle="future")
    original = Observation.objects.get(
        series__key="onrrp",
        batch_id=onrrp.batch_id,
        value_date__date=date(2026, 7, 10),
    )
    Observation.objects.create(
        series=original.series,
        value=original.value,
        value_date=datetime(2026, 7, 14, tzinfo=UTC),
        as_of=datetime(2026, 7, 14, tzinfo=UTC),
        fetched_at=original.fetched_at,
        batch_id=original.batch_id,
        source=original.source,
        quality_status=Observation.Quality.FRESH,
    )

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )

    assert dashboards == []
    assert stale == {"rrp-tga"}
    assert not DashboardSnapshot.objects.filter(key="rrp-tga").exists()


@pytest.mark.django_db
def test_rrp_operation_components_must_share_one_current_value_date():
    onrrp, tga = _rrp_tga_runs(cycle="misaligned")
    auctions = _complete_run(cycle="misaligned")
    rate = Observation.objects.get(
        series__key="onrrp-rate",
        batch_id=onrrp.batch_id,
        value_date__date=date(2026, 7, 10),
    )
    rate.value_date = datetime(2026, 7, 11, tzinfo=UTC)
    rate.as_of = rate.value_date
    rate.save(update_fields=["value_date", "as_of", "updated_at"])

    dashboards, stale = _coordinate_rrp_tga_dashboard(
        [onrrp, tga, auctions], as_of_date=TODAY
    )

    assert dashboards == []
    assert stale == {"rrp-tga"}
    assert not DashboardSnapshot.objects.filter(key="rrp-tga").exists()
