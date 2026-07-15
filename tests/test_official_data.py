from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest
from django.utils import timezone

from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    SeriesDefinition,
    Source,
    SourceLicense,
)
from research.official_data import (
    _fresh_until,
    _has_publishable_run,
    _metric,
    _publish_dashboard,
    publish_official_dashboards,
)
from research.providers import (
    BLSProvider,
    CFTCProvider,
    NYFedMarketsProvider,
    ProviderResult,
    TreasuryRatesProvider,
)
from research.services import ensure_source, record_provider_result, store_series_observations


def _client(handler):
    return httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))


def test_ny_fed_provider_normalizes_reference_rate_metadata():
    def handler(request):
        assert request.url.path.endswith("/api/rates/secured/sofr/last/2.json")
        return httpx.Response(
            200,
            json={
                "refRates": [
                    {
                        "effectiveDate": "2026-07-09",
                        "type": "SOFR",
                        "percentRate": 3.53,
                        "percentPercentile99": 3.65,
                        "volumeInBillions": 3126,
                    }
                ]
            },
        )

    provider = NYFedMarketsProvider(client=_client(handler))
    result = provider.sofr(limit=2)

    assert result.ok
    assert result.records[0]["series_id"] == "SOFR"
    assert result.records[0]["value"] == Decimal("3.53")
    assert result.records[0]["metadata"]["percentPercentile99"] == 3.65


def test_ny_fed_provider_normalizes_reverse_repo_results_without_inventing_propositions():
    def handler(request):
        assert request.url.path.endswith("/api/rp/reverserepo/fixed/results/last/2.json")
        return httpx.Response(
            200,
            json={
                "repo": {
                    "operations": [
                        {
                            "operationId": "RP 071026 26",
                            "auctionStatus": "Results",
                            "operationDate": "2026-07-10",
                            "settlementDate": "2026-07-10",
                            "maturityDate": "2026-07-13",
                            "operationType": "Reverse Repo",
                            "operationMethod": "Fixed Rate",
                            "termCalenderDays": 3,
                            "participatingCpty": 3,
                            "acceptedCpty": 3,
                            "totalAmtSubmitted": 545000000,
                            "totalAmtAccepted": 545000000,
                            "details": [
                                {
                                    "securityType": "Treasury",
                                    "amtAccepted": 545000000,
                                    "percentOfferingRate": 3.50,
                                    "percentAwardRate": 3.50,
                                }
                            ],
                        }
                    ]
                }
            },
        )

    result = NYFedMarketsProvider(client=_client(handler)).reverse_repo_results(limit=2)
    records = {item["series_id"]: item for item in result.records}

    assert result.ok
    assert records["ONRRP"]["value"] == Decimal("545")
    assert records["ONRRP-RATE"]["value"] == Decimal("3.5")
    assert records["ONRRP-PARTICIPANTS"]["value"] == Decimal("3")
    assert records["ONRRP"]["metadata"]["term_calendar_days"] == 3
    assert not any(item["series_id"].startswith("ONRRP-MMF") for item in result.records)
    assert result.metadata["terms_url"].endswith("/privacy/termsofuse")


def test_ny_fed_provider_aggregates_both_daily_standing_repo_windows():
    operations = [
        {
            "operationId": "RP 070726 25",
            "operationDate": "2026-07-07",
            "operationType": "Repo",
            "operationMethod": "Full Allotment",
            "releaseTime": "08:15",
            "totalAmtAccepted": 0,
            "details": [
                {
                    "securityType": security_type,
                    "amtAccepted": 0,
                    "percentOfferingRate": 3.75,
                }
                for security_type in ("Treasury", "Agency", "Mortgage-Backed")
            ],
        },
        {
            "operationId": "RP 070726 27",
            "operationDate": "2026-07-07",
            "operationType": "Repo",
            "operationMethod": "Full Allotment",
            "releaseTime": "13:30",
            "totalAmtAccepted": 3000000,
            "details": [
                {
                    "securityType": security_type,
                    "amtAccepted": 1000000,
                    "percentOfferingRate": 3.75,
                }
                for security_type in ("Treasury", "Agency", "Mortgage-Backed")
            ],
        },
    ]

    def handler(request):
        assert request.url.path.endswith("/api/rp/repo/allotment/results/last/4.json")
        return httpx.Response(200, json={"repo": {"operations": operations}})

    result = NYFedMarketsProvider(client=_client(handler)).standing_repo_results(limit=4)
    records = {item["series_id"]: item for item in result.records}

    assert result.ok
    assert records["SRP"]["value"] == Decimal("3")
    assert records["SRP"]["metadata"]["operation_count"] == 2
    assert records["SRP-TREASURY"]["value"] == Decimal("1")
    assert records["SRP-AGENCY"]["value"] == Decimal("1")
    assert records["SRP-MBS"]["value"] == Decimal("1")
    assert records["SRP-RATE"]["value"] == Decimal("3.75")


def test_ny_fed_provider_normalizes_soma_summary_components_in_usd_millions():
    payload = {
        "soma": {
            "summary": [
                {
                    "asOfDate": "2026-07-08",
                    "mbs": "1940863715777.00",
                    "cmbs": "7533878406.10",
                    "tips": "282633819500",
                    "frn": "",
                    "tipsInflationCompensation": "109065609408.32",
                    "notesbonds": "3593418008900",
                    "bills": "499248926700",
                    "agencies": "2347000000",
                    "total": "6344428175083.10",
                }
            ]
        }
    }

    def handler(request):
        assert request.url.path.endswith("/api/soma/summary.json")
        return httpx.Response(200, json=payload)

    result = NYFedMarketsProvider(client=_client(handler)).soma_summary()
    records = {item["series_id"]: item for item in result.records}

    assert result.ok
    assert records["SOMA-TOTAL"]["value"] == Decimal("6344428.1750831")
    assert records["SOMA-BILLS"]["value"] == Decimal("499248.9267")
    assert "SOMA-FRN" not in records
    assert records["SOMA-TOTAL"]["metadata"]["publication_frequency"] == "weekly"


def test_ny_fed_provider_calculates_active_usd_fx_swaps_and_separates_small_value():
    operations = [
        {
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "European Central Bank",
            "currency": "USD",
            "tradeDate": "2026-07-08",
            "settlementDate": "2026-07-09",
            "maturityDate": "2026-07-16",
            "termInDays": 7,
            "amount": 128000000,
            "interestRate": 3.88,
            "isSmallValue": "",
        },
        {
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "Bank of England",
            "currency": "USD",
            "tradeDate": "2026-07-08",
            "settlementDate": "2026-07-09",
            "maturityDate": "2026-07-16",
            "termInDays": 7,
            "amount": 5000000,
            "interestRate": 3.88,
            "isSmallValue": "",
        },
        {
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "Bank of Japan",
            "currency": "USD",
            "tradeDate": "2026-07-07",
            "settlementDate": "2026-07-09",
            "maturityDate": "2026-07-16",
            "termInDays": 7,
            "amount": 2000000,
            "interestRate": 3.88,
            "isSmallValue": "",
        },
        {
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "Swiss National Bank",
            "currency": "USD",
            "tradeDate": "2026-07-09",
            "settlementDate": "2026-07-10",
            "maturityDate": "2026-07-17",
            "termInDays": 7,
            "amount": 50000,
            "interestRate": 3.88,
            "isSmallValue": "Y",
        },
        {
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "European Central Bank",
            "currency": "USD",
            "tradeDate": "2026-07-01",
            "settlementDate": "2026-07-02",
            "maturityDate": "2026-07-09",
            "termInDays": 7,
            "amount": 170000000,
            "interestRate": 3.88,
            "isSmallValue": "",
        },
    ]

    def handler(request):
        assert request.url.path.endswith("/api/fxs/usdollar/last/5.json")
        return httpx.Response(200, json={"fxSwaps": {"operations": operations}})

    result = NYFedMarketsProvider(client=_client(handler)).usd_fx_swaps(limit=5, as_of="2026-07-12")
    records = {item["series_id"]: item for item in result.records}

    assert result.ok
    assert records["FXSWAP-USD-OUTSTANDING"]["value"] == Decimal("135.05")
    assert records["FXSWAP-USD-OUTSTANDING-SMALL-VALUE"]["value"] == Decimal("0.05")
    assert records["FXSWAP-USD-ECB-OUTSTANDING"]["value"] == Decimal("128")
    assert records["FXSWAP-USD-BOE-OUTSTANDING"]["value"] == Decimal("5")
    assert records["FXSWAP-USD-BOJ-OUTSTANDING"]["value"] == Decimal("2")
    assert records["FXSWAP-USD-SNB-OUTSTANDING"]["value"] == Decimal("0.05")
    assert records["FXSWAP-USD-OUTSTANDING"]["metadata"]["formula"] == (
        "settlementDate <= as_of < maturityDate"
    )


@pytest.mark.django_db
def test_wrong_dataset_same_named_swap_series_cannot_publish_global_dollar_v1():
    fetched_at = datetime(2026, 7, 12, 1, tzinfo=UTC)
    result = ProviderResult(
        provider="ny-fed-markets",
        dataset="desk-integration-fixture",
        fetched_at=fetched_at,
        records=[
            {"series_id": "ONRRP", "date": "2026-07-10", "value": Decimal("545")},
            {
                "series_id": "ONRRP-RATE",
                "date": "2026-07-10",
                "value": Decimal("3.50"),
            },
            {
                "series_id": "ONRRP-PARTICIPANTS",
                "date": "2026-07-10",
                "value": Decimal("3"),
            },
            {"series_id": "SRP", "date": "2026-07-10", "value": Decimal("0")},
            {
                "series_id": "SRP-RATE",
                "date": "2026-07-10",
                "value": Decimal("3.75"),
            },
            {
                "series_id": "SOMA-TOTAL",
                "date": "2026-07-08",
                "value": Decimal("6344428"),
            },
            {
                "series_id": "FXSWAP-USD-OUTSTANDING",
                "date": "2026-07-12",
                "value": Decimal("135"),
            },
            {
                "series_id": "FXSWAP-USD-OUTSTANDING-SMALL-VALUE",
                "date": "2026-07-12",
                "value": Decimal("0"),
            },
        ],
    )
    record_provider_result(result, persist=store_series_observations)

    dashboards = {item.key: item for item in publish_official_dashboards()}

    assert "global-dollar" not in dashboards
    assert "operations" not in dashboards
    assert "rrp-tga" not in dashboards


def test_treasury_provider_normalizes_nominal_curve_xml():
    values = {
        field: (
            "4.21"
            if series_id == "UST-2Y"
            else "4.56"
            if series_id == "UST-10Y"
            else "4.00"
        )
        for field, series_id in TreasuryRatesProvider.NOMINAL_FIELDS.items()
    }
    fields = "".join(
        f'<d:{field} m:type="Edm.Double">{value}</d:{field}>'
        for field, value in values.items()
    )
    payload = f"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
      <title>DailyTreasuryYieldCurveRateData</title>
      <id>https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml-item?data=daily_treasury_yield_curve</id>
      <updated>2026-07-10T19:00:00Z</updated>
      <entry><content type="application/xml"><m:properties>
        <d:NEW_DATE m:type="Edm.DateTime">2026-07-10T00:00:00</d:NEW_DATE>
        {fields}
      </m:properties></content></entry>
    </feed>"""

    provider = TreasuryRatesProvider(
        client=_client(
            lambda _: httpx.Response(
                200,
                text=payload,
                headers={"content-type": "text/xml; charset=UTF-8"},
            )
        )
    )
    result = provider.yield_curve(year=2026)

    assert result.ok
    normalized = {
        item["series_id"]: item["value"] for item in result.records
    }
    assert len(normalized) == len(TreasuryRatesProvider.NOMINAL_FIELDS)
    assert normalized["UST-2Y"] == Decimal("4.21")
    assert normalized["UST-10Y"] == Decimal("4.56")
    assert result.raw_bytes == payload.encode()


def test_bls_provider_normalizes_monthly_series():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "Results": {
                    "series": [
                        {
                            "seriesID": "LNS14000000",
                            "data": [
                                {
                                    "year": "2026",
                                    "period": "M06",
                                    "periodName": "June",
                                    "value": "4.2",
                                    "latest": "true",
                                    "footnotes": [
                                        {"code": "P", "text": "preliminary"}
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    provider = BLSProvider(client=_client(handler))
    result = provider.series(["LNS14000000"], start_year=2026, end_year=2026)

    assert result.ok
    assert result.records[0]["date"] == "2026-06-01"
    assert result.records[0]["value"] == Decimal("4.2")
    assert result.records[0]["quality_status"] == "estimated"
    assert result.records[0]["metadata"]["preliminary"] is True
    assert result.metadata["messages"] == []


def test_bls_provider_marks_an_absent_requested_series_partial():
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "status": "REQUEST_SUCCEEDED",
                "message": ["non-fatal catalog warning"],
                "Results": {
                    "series": [
                        {
                            "seriesID": "LNS14000000",
                            "data": [
                                {
                                    "year": "2026",
                                    "period": "M06",
                                    "periodName": "June",
                                    "value": "4.2",
                                    "footnotes": [],
                                }
                            ],
                        }
                    ]
                },
            },
        )

    provider = BLSProvider(client=_client(handler))
    result = provider.series(
        ["LNS14000000", "LNS11300000"],
        start_year=2026,
        end_year=2026,
    )

    assert result.ok
    assert result.metadata["quality_status"] == "partial"
    assert result.metadata["missing_series"] == ["LNS11300000"]
    assert result.metadata["messages"] == ["non-fatal catalog warning"]


def test_cftc_provider_expands_trader_groups_and_keeps_tuesday_date():
    payload = [
        {
            ":created_at": "2026-07-10T19:31:01.434Z",
            ":updated_at": "2026-07-10T19:31:01.434Z",
            "report_date_as_yyyy_mm_dd": "2026-07-07T00:00:00.000",
            "market_and_exchange_names": "E-MINI S&P 500 - CME",
            "contract_market_name": "E-MINI S&P 500",
            "cftc_contract_market_code": "13874A",
            "open_interest_all": "1000",
            "dealer_positions_long_all": "120",
            "dealer_positions_short_all": "180",
            "asset_mgr_positions_long": "600",
            "asset_mgr_positions_short": "200",
            "lev_money_positions_long": "100",
            "lev_money_positions_short": "300",
            "other_rept_positions_long": "80",
            "other_rept_positions_short": "60",
            "nonrept_positions_long_all": "100",
            "nonrept_positions_short_all": "160",
        }
    ]

    def handler(request):
        assert ":created_at" in request.url.params["$select"]
        assert request.url.params["$where"].endswith("'2024-01-01T00:00:00.000'")
        return httpx.Response(200, json=payload)

    provider = CFTCProvider(client=_client(handler))
    result = provider.positions(start_date="2024-01-01")

    assert result.ok
    assert result.row_count == 5
    assert {item["trader_group"] for item in result.records} == {
        "dealer",
        "asset-manager",
        "leveraged-money",
        "other-reportables",
        "non-reportables",
    }
    assert {item["report_date"] for item in result.records} == {"2026-07-07"}
    assert {item["published_at"] for item in result.records} == {"2026-07-10T19:31:01.434Z"}
    assert {item["market_name"] for item in result.records} == {"E-MINI S&P 500 - CME"}
    assert result.metadata["publication_timestamp_field"] == ":created_at"


@pytest.mark.django_db
def test_demo_seed_is_not_rendered_on_public_pages(client, seeded_platform):
    home = client.get("/").content.decode()
    rates = client.get("/rates/yield-curve/").content.decode()
    news = client.get("/news/").content.decode()

    assert "演示日报" not in home
    assert "4.51%" not in rates
    assert "Atlas Demo Wire" not in news
    assert "没有真实数据时显示空缺" in rates


@pytest.mark.django_db
def test_public_dashboard_publisher_requires_approved_source_licence():
    source = Source.objects.create(
        key="test-approved-official",
        name="Approved Official Fixture",
        license_status=Source.LicenseStatus.OPEN,
        redistribution_allowed=True,
    )
    SourceLicense.objects.create(
        source=source,
        status=Source.LicenseStatus.OPEN,
        scope="Fixture public display",
        public_display_allowed=True,
        derived_display_allowed=True,
        historical_storage_allowed=True,
        redistribution_allowed=True,
    )
    for key, values in {
        "sofr": (3.58, 3.53),
        "effr": (3.62, 3.62),
        "iorb": (3.65, 3.65),
    }.items():
        series = SeriesDefinition.objects.create(
            key=key,
            name=key.upper(),
            unit="%",
            source=source,
        )
        for day, value in enumerate(values, start=8):
            value_date = datetime(2026, 7, day, tzinfo=UTC)
            Observation.objects.create(
                series=series,
                value=Decimal(str(value)),
                value_date=value_date,
                as_of=value_date,
                fetched_at=value_date,
                batch_id=uuid.uuid4(),
                source=source,
            )

    approved = _publish_dashboard(
        key="approved-source-fixture",
        title="Approved source fixture",
        summary="Fixture",
        metrics=[
            _metric("sofr", "SOFR", suffix="%"),
            _metric("iorb", "IORB", suffix="%"),
        ],
        batch_id=uuid.uuid4(),
    )

    assert approved is not None
    assert approved.data["demo"] is False
    assert {item["label"] for item in approved.data["metrics"]} == {
        "SOFR",
        "IORB",
    }
    assert all(
        "Approved Official Fixture" in item["source"]
        for item in approved.data["metrics"]
    )


@pytest.mark.django_db
def test_empty_real_snapshot_never_inherits_registry_demo_metrics(client):
    source = Source.objects.create(
        key="empty-real-snapshot",
        name="Empty real snapshot fixture",
        license_status=Source.LicenseStatus.OPEN,
        redistribution_allowed=True,
    )
    now = datetime.now(UTC)
    DashboardSnapshot.objects.create(
        key="rates",
        title="Rates empty fixture",
        as_of=now,
        source=source,
        is_published=True,
        data={"demo": False, "metrics": [], "chart_data": [], "sections": []},
    )

    content = client.get("/rates/").content.decode()

    assert "5,482.31" not in content
    assert "42,48,45" not in content
    assert "清洁室演示快照" not in content


@pytest.mark.django_db
def test_ai_hub_does_not_claim_seed_coverage_when_public_sets_are_empty(client, seeded_platform):
    content = client.get("/ai-industry/").content.decode()

    assert '<p class="metric-value">45</p>' not in content
    assert '<p class="metric-value">219</p>' not in content
    assert "待接入" in content


def _licensed_source(
    key: str,
    *,
    current_status: str = Source.LicenseStatus.OPEN,
    public_display_allowed: bool = True,
    valid_until=None,
    include_historical_open: bool = False,
) -> Source:
    source = Source.objects.create(
        key=key,
        name=f"{key} fixture",
        license_status=current_status,
        redistribution_allowed=public_display_allowed,
    )
    if include_historical_open:
        SourceLicense.objects.create(
            source=source,
            is_current=False,
            status=Source.LicenseStatus.OPEN,
            scope="Historical public-display decision",
            public_display_allowed=True,
            redistribution_allowed=True,
        )
    SourceLicense.objects.create(
        source=source,
        is_current=True,
        status=current_status,
        scope="Current fixture decision",
        public_display_allowed=public_display_allowed,
        redistribution_allowed=public_display_allowed,
        valid_until=valid_until,
    )
    return source


@pytest.mark.django_db
def test_unchanged_official_value_does_not_publish_duplicate_dashboard_snapshots():
    value_date = timezone.now().astimezone(
        ZoneInfo("America/New_York")
    ).date().isoformat()
    first_fetched_at = timezone.now() - timedelta(hours=1)
    first_run = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="dedup-fixture:first",
            fetched_at=first_fetched_at,
            records=[
                {"series_id": "SOFR", "date": value_date, "value": "3.53"},
                {"series_id": "EFFR", "date": value_date, "value": "3.62"},
                {"series_id": "IORB", "date": value_date, "value": "3.65"},
            ],
        ),
        persist=store_series_observations,
    )
    first_snapshot = _publish_dashboard(
        key="generic-dedup-fixture",
        title="Generic dedup fixture",
        summary="Fixture",
        metrics=[
            _metric("SOFR", "SOFR", suffix="%"),
            _metric("EFFR", "EFFR", suffix="%"),
            _metric("IORB", "IORB", suffix="%"),
        ],
        batch_id=uuid.uuid4(),
    )
    snapshot_count = DashboardSnapshot.objects.count()

    second_run = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="dedup-fixture:second",
            fetched_at=timezone.now(),
            records=[
                {"series_id": "SOFR", "date": value_date, "value": "3.53"},
                {"series_id": "EFFR", "date": value_date, "value": "3.62"},
                {"series_id": "IORB", "date": value_date, "value": "3.65"},
            ],
        ),
        persist=store_series_observations,
    )
    second_snapshot = _publish_dashboard(
        key="generic-dedup-fixture",
        title="Generic dedup fixture",
        summary="Fixture",
        metrics=[
            _metric("SOFR", "SOFR", suffix="%"),
            _metric("EFFR", "EFFR", suffix="%"),
            _metric("IORB", "IORB", suffix="%"),
        ],
        batch_id=uuid.uuid4(),
    )

    assert first_run.batch_id != second_run.batch_id
    assert first_run.status == IngestionRun.Status.SUCCESS
    assert second_run.status == IngestionRun.Status.SUCCESS
    assert first_snapshot is not None
    assert second_snapshot is None
    assert DashboardSnapshot.objects.count() == snapshot_count


@pytest.mark.django_db
def test_provider_result_with_zero_rows_is_partial_not_success():
    run = record_provider_result(
        ProviderResult(provider="internal", dataset="zero-row-fixture", records=[])
    )

    assert run.status == IngestionRun.Status.PARTIAL
    assert run.row_count == 0
    assert run.metadata["quality_reason"] == "provider returned no persistable rows"


@pytest.mark.django_db
def test_dashboard_publication_guard_requires_the_entire_refresh_group():
    successful = record_provider_result(
        ProviderResult(
            provider="internal",
            dataset="complete-refresh-component",
            records=[{"value": 1}],
        )
    )
    failed = record_provider_result(
        ProviderResult.failure(
            "internal",
            "failed-refresh-component",
            "upstream unavailable",
        )
    )

    assert successful.status == IngestionRun.Status.SUCCESS
    assert failed.status == IngestionRun.Status.FAILED
    assert _has_publishable_run([successful]) is True
    assert _has_publishable_run([successful, failed]) is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    "metadata",
    [
        {"quality_status": "partial"},
        {"missing_series": ["EXPECTED-SERIES"]},
    ],
)
def test_provider_partial_quality_metadata_maps_to_partial_run(metadata):
    run = record_provider_result(
        ProviderResult(
            provider="internal",
            dataset="partial-metadata-fixture",
            records=[{"value": 1}],
            metadata=metadata,
        )
    )

    assert run.status == IngestionRun.Status.PARTIAL
    assert run.row_count == 1


@pytest.mark.django_db
def test_monthly_and_quarterly_freshness_start_from_period_end():
    source = _licensed_source("freshness-period-end")
    monthly_series = SeriesDefinition.objects.create(
        key="monthly-period-start",
        name="Monthly period start",
        unit="index",
        frequency="monthly",
        source=source,
    )
    quarterly_series = SeriesDefinition.objects.create(
        key="quarterly-period-start",
        name="Quarterly period start",
        unit="%",
        frequency="quarterly",
        source=source,
    )
    monthly_value_date = datetime(2026, 6, 1, tzinfo=UTC)
    quarterly_value_date = datetime(2026, 4, 1, tzinfo=UTC)
    monthly = Observation.objects.create(
        series=monthly_series,
        value="100",
        value_date=monthly_value_date,
        as_of=monthly_value_date,
        fetched_at=monthly_value_date,
        source=source,
    )
    quarterly = Observation.objects.create(
        series=quarterly_series,
        value="2.1",
        value_date=quarterly_value_date,
        as_of=quarterly_value_date,
        fetched_at=quarterly_value_date,
        source=source,
    )

    assert _fresh_until(monthly) == datetime(2026, 8, 14, tzinfo=UTC)
    assert _fresh_until(quarterly) == datetime(2026, 10, 28, tzinfo=UTC)


@pytest.mark.django_db
def test_daily_freshness_keeps_weekend_data_valid_until_new_york_release_window():
    source = _licensed_source("daily-release-window")
    series = SeriesDefinition.objects.create(
        key="daily-release-window",
        name="Daily release window",
        unit="%",
        frequency="daily",
        source=source,
    )
    value_date = datetime(2026, 7, 9, tzinfo=UTC)
    observation = Observation.objects.create(
        series=series,
        value="3.62",
        value_date=value_date,
        as_of=value_date,
        fetched_at=datetime(2026, 7, 10, 13, tzinfo=UTC),
        source=source,
    )

    assert _fresh_until(observation) == datetime(2026, 7, 13, 14, tzinfo=UTC)


@pytest.mark.django_db
@pytest.mark.parametrize("decision", ["restricted", "expired"])
def test_current_restricted_or_expired_licence_suppresses_old_dashboard(client, decision):
    expired_at = timezone.localdate() - timedelta(days=1) if decision == "expired" else None
    status = (
        Source.LicenseStatus.RESTRICTED if decision == "restricted" else Source.LicenseStatus.OPEN
    )
    source = _licensed_source(
        f"dashboard-{decision}",
        current_status=status,
        public_display_allowed=decision == "expired",
        valid_until=expired_at,
        include_historical_open=True,
    )
    DashboardSnapshot.objects.create(
        key="operations",
        title="Old licensed dashboard",
        as_of=timezone.now() - timedelta(days=2),
        source=source,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [source.key],
            "metrics": [
                {
                    "label": "Revoked metric",
                    "display_value": "MUST-NOT-RENDER",
                    "source_key": source.key,
                }
            ],
        },
    )

    response = client.get("/liquidity/operations/")

    assert response.status_code == 200
    assert "MUST-NOT-RENDER" not in response.content.decode()


@pytest.mark.django_db
def test_top_level_source_keys_cannot_hide_revoked_metric_source(client):
    allowed = _licensed_source("allowed-top-level")
    revoked = _licensed_source(
        "revoked-metric-source",
        current_status=Source.LicenseStatus.RESTRICTED,
        public_display_allowed=False,
        include_historical_open=True,
    )
    DashboardSnapshot.objects.create(
        key="rates",
        title="Mixed-source dashboard",
        as_of=timezone.now(),
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Mixed licence metric",
                    "display_value": "REVOKED-SOURCE-MUST-NOT-RENDER",
                    "source_key": revoked.key,
                }
            ],
        },
    )

    response = client.get("/rates/")

    assert response.status_code == 200
    assert "REVOKED-SOURCE-MUST-NOT-RENDER" not in response.content.decode()


@pytest.mark.django_db
def test_chart_lineage_cannot_hide_revoked_source(client):
    allowed = _licensed_source("allowed-chart-shell")
    revoked = _licensed_source(
        "revoked-chart-source",
        current_status=Source.LicenseStatus.RESTRICTED,
        public_display_allowed=False,
        include_historical_open=True,
    )
    DashboardSnapshot.objects.create(
        key="rates",
        title="Mixed-source chart",
        as_of=timezone.now(),
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Allowed shell metric",
                    "display_value": "1.00%",
                    "source_key": allowed.key,
                }
            ],
            "charts": [
                {
                    "key": "hidden-revoked-lineage",
                    "title": "MUST-NOT-RENDER-CHART",
                    "data": [
                        {
                            "date": "2026-07-01",
                            "Restricted value": 99,
                            "_source_keys": [revoked.key],
                        }
                    ],
                }
            ],
        },
    )

    response = client.get("/rates/")

    assert response.status_code == 200
    body = response.content.decode()
    assert "MUST-NOT-RENDER-CHART" not in body
    assert "Restricted value" not in body


@pytest.mark.django_db
def test_fallback_source_is_in_metric_lineage_and_recursive_licence_gate(client):
    allowed = _licensed_source("allowed-primary-source")
    revoked = _licensed_source(
        "revoked-fallback-source",
        current_status=Source.LicenseStatus.RESTRICTED,
        public_display_allowed=False,
        include_historical_open=True,
    )
    series = SeriesDefinition.objects.create(
        key="fallback-lineage-series",
        name="Fallback lineage fixture",
        unit="%",
        frequency="daily",
        source=allowed,
    )
    now = timezone.now()
    Observation.objects.create(
        series=series,
        value="1.25",
        value_date=now,
        as_of=now,
        fetched_at=now,
        source=allowed,
        fallback_source=revoked,
    )

    metric = _metric("fallback-lineage-series", "Fallback metric", suffix="%")
    assert metric["fallback_source"] == revoked.key
    assert set(metric["source_keys"]) == {allowed.key, revoked.key}

    DashboardSnapshot.objects.create(
        key="rates",
        title="Fallback-only hidden source",
        as_of=now,
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Fallback-only forbidden metric",
                    "display_value": "FALLBACK-MUST-NOT-RENDER",
                    "source_key": allowed.key,
                    "fallback_source": revoked.key,
                }
            ],
        },
    )

    response = client.get("/rates/")
    assert response.status_code == 200
    assert "FALLBACK-MUST-NOT-RENDER" not in response.content.decode()


@pytest.mark.django_db
def test_transmission_route_rejects_safe_and_revoked_legacy_snapshots(client):
    allowed = _licensed_source("safe-snapshot-source")
    revoked = _licensed_source(
        "newer-revoked-chart-source",
        current_status=Source.LicenseStatus.RESTRICTED,
        public_display_allowed=False,
        include_historical_open=True,
    )
    now = timezone.now()
    DashboardSnapshot.objects.create(
        key="transmission-chain",
        title="Previous safe snapshot",
        as_of=now - timedelta(days=1),
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Safe metric",
                    "display_value": "SAFE-SNAPSHOT-RENDERS",
                    "source_key": allowed.key,
                }
            ],
            "chart_data": [],
        },
    )
    DashboardSnapshot.objects.create(
        key="transmission-chain",
        title="Newer unsafe snapshot",
        as_of=now,
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Unsafe metric",
                    "display_value": "UNSAFE-SNAPSHOT-MUST-NOT-RENDER",
                    "source_key": allowed.key,
                }
            ],
            "charts": [
                {
                    "title": "Unsafe chart",
                    "data": [{"date": "2026-07-01", "value": 1}],
                    "fallback_source": revoked.key,
                }
            ],
        },
    )

    response = client.get("/liquidity/transmission-chain/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "SAFE-SNAPSHOT-RENDERS" not in body
    assert "UNSAFE-SNAPSHOT-MUST-NOT-RENDER" not in body
    assert "本页尚无通过来源许可与质量检查的可发布快照" in body


@pytest.mark.django_db
def test_transmission_route_never_selects_unversioned_mixed_frequency_legacy(client):
    allowed = _licensed_source("mixed-frequency-snapshot-source")
    now = timezone.now()
    DashboardSnapshot.objects.create(
        key="transmission-chain",
        title="Older monthly-only snapshot",
        as_of=now,
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Old monthly metric",
                    "display_value": "OLD-MONTHLY-SNAPSHOT",
                    "source_key": allowed.key,
                }
            ],
            "chart_data": [],
        },
    )
    DashboardSnapshot.objects.create(
        key="transmission-chain",
        title="Latest mixed-frequency snapshot",
        as_of=now - timedelta(days=90),
        source=allowed,
        is_published=True,
        data={
            "demo": False,
            "source_keys": [allowed.key],
            "metrics": [
                {
                    "label": "Latest mixed-frequency metric",
                    "display_value": "LATEST-MIXED-FREQUENCY-SNAPSHOT",
                    "source_key": allowed.key,
                }
            ],
            "chart_data": [],
        },
    )

    body = client.get("/liquidity/transmission-chain/").content.decode()

    assert "LATEST-MIXED-FREQUENCY-SNAPSHOT" not in body
    assert "OLD-MONTHLY-SNAPSHOT" not in body


@pytest.mark.django_db
def test_legacy_chart_data_footer_uses_chart_lineage_not_all_page_sources(client):
    shell = _licensed_source("legacy-chart-shell")
    chart_source = _licensed_source("legacy-chart-only")
    metric_source = _licensed_source("legacy-metric-only")
    DashboardSnapshot.objects.create(
        key="consumer",
        title="Legacy chart snapshot",
        as_of=timezone.now(),
        source=shell,
        is_published=True,
        data={
            "demo": False,
            "contract_version": 1,
            "source_keys": [chart_source.key, metric_source.key],
            "metrics": [
                {
                    "label": "Legacy metric",
                    "display_value": "1.00%",
                    "source_key": metric_source.key,
                }
            ],
            "chart_data": [
                {
                    "date": "2026-07-01",
                    "Rate": 1,
                    "_source_keys": [chart_source.key],
                }
            ],
        },
    )

    body = client.get("/economy/consumer/").content.decode()
    chart_footer = body.split('<footer class="source-line', 1)[1].split(
        "</footer>", 1
    )[0]
    assert chart_source.name in chart_footer
    assert metric_source.name not in chart_footer


def _dashboard_metric_contract(*, source_key: str | None) -> dict:
    now = timezone.now()
    payload = {
        "key": "verified-metric",
        "label": "Verified metric",
        "value": "1.0000000000000002",
        "display_value": "1.00",
        "unit": "index",
        "value_date": now.isoformat(),
        "as_of": now.isoformat(),
        "fetched_at": now.isoformat(),
        "batch_id": str(uuid.uuid4()),
        "quality_status": Observation.Quality.FRESH,
        "source_keys": [source_key] if source_key else [],
        "fresh_until": (now + timedelta(days=1)).isoformat(),
    }
    if source_key is not None:
        payload["source_key"] = source_key
    return payload


@pytest.mark.django_db
@pytest.mark.parametrize("source_key", [None, "unknown-source-key"])
def test_dashboard_publisher_rejects_missing_or_unknown_metric_source(source_key):
    ensure_source("internal")

    with pytest.raises(ValueError, match="source"):
        _publish_dashboard(
            key="strict-source-contract",
            title="Strict source contract",
            summary="Fixture",
            metrics=[_dashboard_metric_contract(source_key=source_key)],
            batch_id=uuid.uuid4(),
        )


@pytest.mark.django_db
def test_dashboard_publisher_injects_exact_source_licence_scope():
    source = ensure_source("internal")

    snapshot = _publish_dashboard(
        key="licence-scope-contract",
        title="Licence scope contract",
        summary="Fixture",
        metrics=[_dashboard_metric_contract(source_key=source.key)],
        batch_id=uuid.uuid4(),
    )

    assert snapshot is not None
    assert snapshot.data["metrics"][0]["license_scope"] == source.license_scope[:120]
    metric = snapshot.data["metrics"][0]
    normalized = MetricSnapshot.objects.get(
        key="licence-scope-contract-verified-metric",
        batch_id=snapshot.batch_id,
    )
    assert metric["source_key"] == source.key
    assert normalized.license_scope == source.license_scope[:120]


@pytest.mark.django_db
def test_append_only_recovery_never_leaves_same_lineage_revision_stale():
    source = ensure_source("internal")
    metric = _dashboard_metric_contract(source_key=source.key)
    extra_data = {
        "contract_version": 1,
        "formula_version": "official-evidence-chain-v1",
        "semantic_manifest": {"fixture": "same-lineage-recovery"},
    }
    first = _publish_dashboard(
        key="reserves",
        title="Append-only fixture",
        summary="Fixture",
        metrics=[metric],
        extra_data=extra_data,
        batch_id=uuid.uuid4(),
    )
    assert first is not None
    first_data = dict(first.data)
    first_data["refresh_failure"] = {
        "checked_at": timezone.now().isoformat(),
        "reason_code": "fixture",
    }
    first.data = first_data
    first.quality_status = Observation.Quality.STALE
    first.save(update_fields=["data", "quality_status", "updated_at"])

    recovered = _publish_dashboard(
        key="reserves",
        title="Append-only fixture",
        summary="Fixture",
        metrics=[metric],
        extra_data=extra_data,
        batch_id=uuid.uuid4(),
    )

    assert recovered is not None
    assert recovered.pk != first.pk
    assert recovered.batch_id != first.batch_id
    assert "refresh_failure" not in recovered.data
    first.refresh_from_db()
    assert first.quality_status == Observation.Quality.STALE
    assert "refresh_failure" in first.data
