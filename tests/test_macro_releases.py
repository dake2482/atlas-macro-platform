from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from openpyxl import Workbook

from research.consumer_credit import (
    G19_SERIES,
    HHDC_BALANCE_SERIES,
    HHDC_DELINQUENCY_SERIES,
)
from research.data_catalog import DATA_REQUIREMENTS
from research.macro_releases import (
    BEA_GDP_PAGE,
    BEA_PIO_PAGE,
    BEA_PIO_SECTION2_WORKBOOK,
    BEA_VINTAGE_WORKBOOK,
    CENSUS_MARTS_CURRENT_WORKBOOK,
    CENSUS_MARTS_INDEX,
    XLSX_CONTENT_TYPE,
    BEAGDPReleaseProvider,
    BEAPIOReleaseProvider,
    CensusMARTSReleaseProvider,
)
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    Observation,
    RawArtifact,
    ReleaseVintageObservation,
)
from research.official_data import (
    MACRO_PUBLICATION_GROUPS,
    _keys_with_current_required_batches,
    _mark_latest_dashboards_stale,
    _publishable_keys_for_source_groups,
    _record_census_revision_witness,
    _store_release_workbook_observations,
    publish_official_dashboards,
)
from research.providers import ProviderResult
from research.services import record_provider_result, store_series_observations


def _workbook_bytes(workbook: Workbook) -> bytes:
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _consumer_credit_results() -> tuple[ProviderResult, ProviderResult]:
    g19_records = []
    for period, offset in (("2026-04-01", Decimal("0")), ("2026-05-01", Decimal("1"))):
        for index, (_, (series_id, _)) in enumerate(G19_SERIES.items(), start=1):
            g19_records.append(
                {
                    "series_id": series_id,
                    "date": period,
                    "value": Decimal(index) + offset,
                }
            )
    household_records = []
    household_series = [
        *HHDC_BALANCE_SERIES.values(),
        *HHDC_DELINQUENCY_SERIES.values(),
    ]
    for period, offset in (("2025-12-31", Decimal("0")), ("2026-03-31", Decimal("1"))):
        for index, series_id in enumerate(household_series, start=1):
            household_records.append(
                {
                    "series_id": series_id,
                    "date": period,
                    "value": Decimal(index) + offset,
                }
            )
    return (
        ProviderResult(
            provider="federal-reserve-g19",
            dataset="g19-fixture",
            records=g19_records,
        ),
        ProviderResult(
            provider="ny-fed-household-credit",
            dataset="hhdc-fixture",
            records=household_records,
        ),
    )


def _census_api_result() -> ProviderResult:
    records = []
    values = {
        "2026-03-01": ("754013", "1.7", "4.2"),
        "2026-04-01": ("757036", "0.4", "4.8"),
        "2026-05-01": ("763705", "0.9", "6.9"),
    }
    for period, (level, mom, yoy) in values.items():
        records.extend(
            [
                {
                    "series_id": "CENSUS-MRTS-44X72-SM-SA",
                    "date": period,
                    "value": level,
                },
                {
                    "series_id": "CENSUS-MRTS-44X72-SM-SA-MOM",
                    "date": period,
                    "value": mom,
                },
                {
                    "series_id": "CENSUS-MRTS-44X72-SM-SA-YOY",
                    "date": period,
                    "value": yoy,
                },
            ]
        )
    return ProviderResult(
        provider="census",
        dataset="marts:44X72:SM:yes",
        records=records,
    )


def _bea_vintage_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Vintage History"
    sheet.append(["Last Updated June 25, 2026"])
    sheet.append(["2026Q1"])
    sheet.append([None, "Vintage", "GDP", "GDI", "Real GDP", "Real GDI", "Release Date"])
    sheet.append([None, "Third", "31,865.7", "31,574.2", "2.1", "1.2", "Jun 25, 2026"])
    sheet.append([None, "Second", "31,819.5", "31,539.4", "1.6", "0.9", "May 28, 2026"])
    sheet.append(["2025Q4"])
    sheet.append([None, "Vintage", "GDP", "GDI", "Real GDP", "Real GDI", "Release Date"])
    sheet.append(
        [
            None,
            "Revised",
            "31,422.5",
            "31,199.9",
            "0.5",
            "1.6",
            "May 28, 2026 GDP not open for revision",
        ]
    )
    return _workbook_bytes(workbook)


def _bea_comparison_workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "GDPhistQ"
    sheet["A1"] = "June 25, 2026"
    sheet["A2"] = (
        "2026Q1 (Third Estimate) Comparisons -- Percent Change from Preceding Period "
        "in Real Gross Domestic Product and Related Measures"
    )
    rows = [
        ("Gross domestic product (GDP)", 2.1, "2025Q4", 0.5),
        ("Personal consumption expenditures", 0.5, "2025Q4", 1.9),
        ("Goods", 0.5, "2025Q4", 0.3),
        ("Services", 0.5, "2025Q4", 2.7),
        ("Gross private domestic investment", 7.9, "2025Q4", 2.3),
        ("Fixed investment", 6.5, "2025Q4", 1.5),
        ("Exports", 5.4, "2025Q4", -3.1),
        ("Imports", 1.8, "2025Q4", -1.2),
        ("Government consumption expenditures and gross investment", 1.2, "2025Q4", 0.4),
    ]
    for row_number, (label, current, previous_period, previous) in enumerate(rows, start=6):
        sheet.cell(row_number, 1, label)
        sheet.cell(row_number, 2, current)
        sheet.cell(row_number, 5, previous_period)
        sheet.cell(row_number, 6, previous)
    sheet.cell(
        20,
        1,
        "2026Q1 (Third Estimate) Comparisons -- Contributions to Percent Change "
        "in Real Gross Domestic Product",
    )
    contribution_rows = [
        ("Personal consumption expenditures", 0.37, "2025Q4", 1.30),
        ("Gross private domestic investment", 1.35, "2025Q4", 0.40),
        ("Net exports of goods and services", -0.37, "2025Q4", 0.46),
        ("Government consumption expenditures and gross investment", 0.74, "2025Q4", -0.99),
    ]
    for row_number, (label, current, previous_period, previous) in enumerate(
        contribution_rows, start=24
    ):
        sheet.cell(row_number, 1, label)
        sheet.cell(row_number, 2, current)
        sheet.cell(row_number, 5, previous_period)
        sheet.cell(row_number, 6, previous)
    return _workbook_bytes(workbook)


def _census_workbook() -> bytes:
    workbook = Workbook()
    sales = workbook.active
    sales.title = "Table 1."
    sales.cell(6, 10, "Adjusted2")
    sales.cell(7, 10, 2026)
    sales.cell(7, 13, 2025)
    for column, label in enumerate(("Apr.3", "Mar.", "Feb.", "Apr.", "Mar."), start=10):
        sales.cell(8, column, label)
    for column, status in enumerate(("(a)", "(p)", "(r)", "(r)", "(r)"), start=10):
        sales.cell(9, column, status)
    sales.cell(11, 2, "Retail & food services, ")
    sales.cell(12, 2, "  total")
    for column, value in enumerate((757085, 753370, 741278, 721903, 723339), start=10):
        sales.cell(12, column, value)

    changes = workbook.create_sheet("Table 2.")
    changes.cell(8, 3, "Apr. 2026 Advance")
    changes.cell(8, 5, "Mar. 2026 Preliminary")
    changes.cell(11, 3, "Mar. 2026")
    changes.cell(11, 4, "Apr. 2025")
    changes.cell(11, 5, "Feb. 2026")
    changes.cell(11, 6, "Mar. 2025")
    changes.cell(14, 2, "Retail & food services, ")
    changes.cell(15, 2, "  total")
    changes.cell(15, 3, 0.5)
    changes.cell(15, 4, 4.9)
    changes.cell(15, 5, 1.6)
    changes.cell(15, 6, 4.2)
    return _workbook_bytes(workbook)


def _census_current_workbook() -> bytes:
    workbook = Workbook()
    sales = workbook.active
    sales.title = "Table 1."
    sales.cell(6, 10, "Adjusted2")
    sales.cell(7, 10, 2026)
    sales.cell(7, 13, 2025)
    for column, label in enumerate(("May.3", "Apr.", "Mar.", "May.", "Apr."), start=10):
        sales.cell(8, column, label)
    for column, status in enumerate(("(a)", "(r)", "(r)", "(r)", "(r)"), start=10):
        sales.cell(9, column, status)
    sales.cell(11, 2, "Retail & food services, ")
    sales.cell(12, 2, "  total")
    for column, value in enumerate((763705, 757036, 754013, 714568, 721903), start=10):
        sales.cell(12, column, value)

    changes = workbook.create_sheet("Table 2.")
    changes.cell(8, 3, "May. 2026 Advance")
    changes.cell(8, 5, "Apr. 2026 Revised")
    changes.cell(11, 3, "Apr. 2026")
    changes.cell(11, 4, "May. 2025")
    changes.cell(11, 5, "Mar. 2026")
    changes.cell(11, 6, "Apr. 2025")
    changes.cell(14, 2, "Retail & food services, ")
    changes.cell(15, 2, "  total")
    changes.cell(15, 3, 0.9)
    changes.cell(15, 4, 6.9)
    changes.cell(15, 5, 0.4)
    changes.cell(15, 6, 4.8)
    return _workbook_bytes(workbook)


def _bea_pio_summary_workbook(*, real_pce: float = 0.3) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "PIOhist_M"
    sheet["G1"] = datetime(2026, 6, 25)
    sheet["A2"] = "May 2026 Personal Income and Outlays"
    sheet["A3"] = "Historical Comparisons"
    sheet["B5"] = datetime(2026, 5, 1)
    sheet["A16"] = "Chained dollars"
    sheet["A20"] = "Percent change from preceding month:"
    sheet["A21"] = "DPI"
    sheet["B21"] = 0.3
    sheet["A22"] = "PCE"
    sheet["B22"] = real_pce
    sheet["A30"] = "Personal saving as a percentage of DPI"
    sheet["A31"] = "Personal saving rate"
    sheet["B31"] = 3.0
    return _workbook_bytes(workbook)


def _bea_pio_section2_workbook(
    *, duplicate_code: bool = False, missing_middle: bool = False
) -> bytes:
    workbook = Workbook()
    section_206 = workbook.active
    section_206.title = "T20600-M"
    section_20801 = workbook.create_sheet("T20801-M")
    section_20806 = workbook.create_sheet("T20806-M")
    section_20804 = workbook.create_sheet("T20804-M")
    for sheet, title in (
        (section_206, "Table 2.6. Personal Income and Its Disposition, Monthly"),
        (
            section_20801,
            "Table 2.8.1. Percent Change From Preceding Period in Real "
            "Personal Consumption Expenditures by Major Type of Product, Monthly",
        ),
        (
            section_20804,
            "Table 2.8.4. Price Indexes for Personal Consumption Expenditures "
            "by Major Type of Product, Monthly",
        ),
        (
            section_20806,
            "Table 2.8.6. Real Personal Consumption Expenditures by Major "
            "Type of Product, Monthly, Chained Dollars",
        ),
    ):
        sheet["A1"] = title
        sheet["A5"] = "Data published June 25, 2026"
    section_206["A2"] = (
        "[Millions of dollars; months are seasonally adjusted at annual rates]"
    )
    section_20801["A2"] = "[Percent]"
    section_20806["A2"] = (
        "[Millions of chained (2017) dollars; seasonally adjusted at annual rates]"
    )
    section_20804["A2"] = "[Index numbers, 2017=100; seasonally adjusted]"

    def monthly_periods(start_year: int, start_month: int) -> list[str]:
        periods = []
        year, month = start_year, start_month
        while (year, month) <= (2026, 5):
            periods.append(f"{year:04d}M{month:02d}")
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
        return periods

    periods_206 = monthly_periods(1959, 1)
    periods_20801 = monthly_periods(1959, 2)
    periods_20804 = monthly_periods(1959, 1)
    periods_20806 = monthly_periods(2007, 1)
    for sheet, periods in (
        (section_206, periods_206),
        (section_20801, periods_20801),
        (section_20804, periods_20804),
        (section_20806, periods_20806),
    ):
        sheet["A3"] = f"Monthly data from {periods[0]} to {periods[-1]}"
        for offset, period in enumerate(periods, start=4):
            sheet.cell(8, offset, period)

    rows_206 = (
        (35, "Disposable personal income", "A067RC", 20000000, 23486851, 23651714, False),
        (43, "Personal saving rate", "A072RC", 5.0, 3.0, 3.0, False),
        (
            47,
            "Real disposable personal income, chained (2017) dollars",
            "A067RX",
            15000000,
            17938761,
            17983827,
            False,
        ),
        (53, "Disposable personal income MoM", "A067RCM", 0.1, -0.1, 0.7, True),
        (
            55,
            "Real disposable personal income, chained (2017) dollars, MoM",
            "A067RM",
            0.1,
            -0.5,
            0.3,
            True,
        ),
    )
    for row_number, label, code, default, previous, current, leading_blank in rows_206:
        section_206.cell(row_number, 2, label)
        section_206.cell(row_number, 3, code)
        for offset, _period in enumerate(periods_206, start=4):
            if leading_blank and offset == 4:
                continue
            section_206.cell(row_number, offset, default)
        section_206.cell(row_number, 3 + len(periods_206) - 1, previous)
        section_206.cell(row_number, 3 + len(periods_206), current)
    if duplicate_code:
        section_206.cell(56, 2, "Duplicate real DPI")
        section_206.cell(56, 3, "A067RM")
        section_206.cell(56, 4, -0.5)
        section_206.cell(56, 5, 0.3)

    section_20801.cell(9, 2, "Personal consumption expenditures")
    section_20801.cell(9, 3, "DPCERAM")
    for offset, _period in enumerate(periods_20801, start=4):
        section_20801.cell(9, offset, 0.1)
    section_20801.cell(9, 3 + len(periods_20801) - 1, 0.0)
    section_20801.cell(9, 3 + len(periods_20801), 0.3)
    if missing_middle:
        section_20801.cell(9, 100, ".....")
    section_20806.cell(9, 2, "Personal consumption expenditures")
    section_20806.cell(9, 3, "DPCERX")
    for offset, _period in enumerate(periods_20806, start=4):
        section_20806.cell(9, offset, 15000000)
    section_20806.cell(9, 3 + len(periods_20806) - 1, 16729609)
    section_20806.cell(9, 3 + len(periods_20806), 16773429)
    section_20804.cell(9, 2, "Personal consumption expenditures (PCE)")
    section_20804.cell(9, 3, "DPCERG")
    section_20804.cell(33, 2, "PCE excluding food and energy")
    section_20804.cell(33, 3, "DPCCRG")
    for offset, _period in enumerate(periods_20804, start=4):
        section_20804.cell(9, offset, 100.0)
        section_20804.cell(33, offset, 200.0)
    section_20804.cell(9, 3 + len(periods_20804) - 12, 120.0)
    section_20804.cell(9, 3 + len(periods_20804) - 6, 124.0)
    section_20804.cell(9, 3 + len(periods_20804) - 3, 125.0)
    section_20804.cell(9, 3 + len(periods_20804) - 1, 129.0)
    section_20804.cell(9, 3 + len(periods_20804), 130.0)
    section_20804.cell(33, 3 + len(periods_20804) - 12, 250.0)
    section_20804.cell(33, 3 + len(periods_20804) - 6, 254.0)
    section_20804.cell(33, 3 + len(periods_20804) - 3, 255.0)
    section_20804.cell(33, 3 + len(periods_20804) - 1, 259.0)
    section_20804.cell(33, 3 + len(periods_20804), 260.0)
    return _workbook_bytes(workbook)


def _bea_client(vintage: bytes, comparisons: bytes) -> httpx.Client:
    comparison_url = "https://www.bea.gov/sites/default/files/2026-06/hist1q26-3rd.xlsx"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BEA_GDP_PAGE:
            return httpx.Response(
                200,
                text=(
                    '<html><a href="/sites/default/files/2026-06/hist1q26-3rd.xlsx">'
                    "Historical Comparisons</a></html>"
                ),
                headers={"content-type": "text/html"},
            )
        if url == BEA_VINTAGE_WORKBOOK:
            return httpx.Response(
                200,
                content=vintage,
                headers={"content-type": XLSX_CONTENT_TYPE, "last-modified": "June 25, 2026"},
            )
        if url == comparison_url:
            return httpx.Response(
                200,
                content=comparisons,
                headers={"content-type": XLSX_CONTENT_TYPE, "last-modified": "June 25, 2026"},
            )
        raise AssertionError(f"unexpected URL: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _census_client(workbook: bytes, *, current_status: int = 403) -> httpx.Client:
    latest = f"{CENSUS_MARTS_INDEX}rs2604.xlsx"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == CENSUS_MARTS_CURRENT_WORKBOOK:
            if current_status == 200:
                return httpx.Response(
                    200,
                    content=workbook,
                    headers={
                        "content-type": XLSX_CONTENT_TYPE,
                        "last-modified": "June 17, 2026",
                    },
                )
            return httpx.Response(current_status, text="current workbook unavailable")
        if url == CENSUS_MARTS_INDEX:
            return httpx.Response(
                200,
                text=(
                    '<html><a href="rs9912.xlsx">old</a>'
                    '<a href="rs2603.xlsx">prior</a>'
                    '<a href="rs2604.xlsx">latest</a></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if url == latest:
            return httpx.Response(
                200,
                content=workbook,
                headers={"content-type": XLSX_CONTENT_TYPE, "last-modified": "May 15, 2026"},
            )
        raise AssertionError(f"unexpected URL: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _bea_pio_client(summary: bytes, section2: bytes) -> httpx.Client:
    summary_url = "https://www.bea.gov/sites/default/files/2026-06/pi0526-hist.xlsx"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BEA_PIO_PAGE:
            return httpx.Response(
                200,
                text=(
                    '<html><a href="/sites/default/files/2026-06/pi0526-hist.xlsx">'
                    "Historical Comparisons</a></html>"
                ),
                headers={"content-type": "text/html"},
            )
        if url == summary_url:
            return httpx.Response(
                200,
                content=summary,
                headers={"content-type": XLSX_CONTENT_TYPE, "last-modified": "June 25, 2026"},
            )
        if url == BEA_PIO_SECTION2_WORKBOOK:
            return httpx.Response(
                200,
                content=section2,
                headers={"content-type": XLSX_CONTENT_TYPE, "last-modified": "June 25, 2026"},
            )
        raise AssertionError(f"unexpected URL: {url}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_bea_release_provider_preserves_vintages_components_and_artifact_hashes():
    vintage = _bea_vintage_workbook()
    comparisons = _bea_comparison_workbook()
    result = BEAGDPReleaseProvider(client=_bea_client(vintage, comparisons)).gdp_pce()

    assert result.ok
    assert result.metadata["quarter_count"] == 2
    assert result.metadata["comparison_quarter"] == "2026Q1"
    assert result.metadata["comparison_release_date"] == "2026-06-25"
    assert result.metadata["comparison_estimate_round"] == "Third"
    assert result.metadata["vintage_release_count"] == 3
    assert result.metadata["vintage_observation_count"] == 12
    assert len(result.metadata["artifacts"]) == 3
    assert result.metadata["artifacts"][1]["sha256"] == hashlib.sha256(vintage).hexdigest()

    by_series_and_date = {
        (item["series_id"], item["date"]): item for item in result.records
    }
    latest_gdp = by_series_and_date[("BEA-A191RL", "2026-01-01")]
    assert latest_gdp["value"] == Decimal("2.1")
    assert latest_gdp["metadata"]["vintage_label"] == "Third"
    assert latest_gdp["metadata"]["estimate_round"] == "Third"
    assert latest_gdp["metadata"]["source_revision_date"] == "2026-06-25"
    gdp_vintages = [
        item
        for item in result.supplemental_records["release_vintages"]
        if item["series_id"] == "BEA-A191RL" and item["date"] == "2026-01-01"
    ]
    assert [item["estimate_round"] for item in gdp_vintages] == ["Third", "Second"]
    assert [item["value"] for item in gdp_vintages] == [Decimal("2.1"), Decimal("1.6")]
    assert [item["release_date"] for item in gdp_vintages] == [
        "2026-06-25",
        "2026-05-28",
    ]
    assert by_series_and_date[("BEA-DPCERL", "2026-01-01")]["value"] == Decimal("0.5")
    assert by_series_and_date[("BEA-DPCERL", "2025-10-01")]["value"] == Decimal("1.9")
    assert by_series_and_date[("BEA-GPDI-GROWTH", "2026-01-01")]["value"] == Decimal("7.9")
    assert by_series_and_date[("BEA-PCE-CONTRIBUTION", "2026-01-01")][
        "value"
    ] == Decimal("0.37")
    assert by_series_and_date[("BEA-NET-EXPORTS-CONTRIBUTION", "2026-01-01")][
        "metadata"
    ]["unit"] == "percentage points contribution to real GDP growth"


def test_bea_pio_provider_parses_full_history_codes_and_cross_checks_summary():
    summary = _bea_pio_summary_workbook()
    section2 = _bea_pio_section2_workbook()
    result = BEAPIOReleaseProvider(
        client=_bea_pio_client(summary, section2)
    ).personal_income_outlays()

    assert result.ok
    assert result.row_count == 6702
    assert result.metadata["latest_value_date"] == "2026-05-01"
    assert result.metadata["source_revision_date"] == "2026-06-25"
    assert result.metadata["summary_cross_check"] == "passed"
    artifacts = {item["url"]: item for item in result.metadata["artifacts"]}
    assert set(artifacts) == {
        BEA_PIO_PAGE,
        "https://www.bea.gov/sites/default/files/2026-06/pi0526-hist.xlsx",
        BEA_PIO_SECTION2_WORKBOOK,
    }
    assert artifacts[BEA_PIO_SECTION2_WORKBOOK]["sha256"] == hashlib.sha256(
        section2
    ).hexdigest()

    by_series_and_date = {
        (item["series_id"], item["date"]): item for item in result.records
    }
    assert len(by_series_and_date) == result.row_count
    assert by_series_and_date[("BEA-REAL-PCE-MOM", "2026-05-01")][
        "value"
    ] == Decimal("0.3")
    assert by_series_and_date[("BEA-REAL-DPI-MOM", "2026-04-01")][
        "value"
    ] == Decimal("-0.5")
    assert by_series_and_date[("BEA-PERSONAL-SAVING-RATE", "2026-05-01")][
        "value"
    ] == Decimal("3")
    assert by_series_and_date[("BEA-DPI-NOMINAL-SAAR", "2026-05-01")][
        "value"
    ] == Decimal("23651714")
    assert by_series_and_date[("BEA-DPI-REAL-SAAR", "2026-05-01")][
        "value"
    ] == Decimal("17983827")
    assert by_series_and_date[("BEA-REAL-PCE-SAAR", "2026-05-01")][
        "value"
    ] == Decimal("16773429")
    assert by_series_and_date[("BEA-PCE-PRICE-INDEX", "2026-05-01")][
        "value"
    ] == Decimal("130")
    assert by_series_and_date[("BEA-CORE-PCE-PRICE-INDEX", "2026-05-01")][
        "value"
    ] == Decimal("260")
    assert by_series_and_date[("BEA-PCE-PRICE-INDEX", "2026-05-01")]["metadata"][
        "official_series_code"
    ] == "DPCERG"
    assert by_series_and_date[("BEA-CORE-PCE-PRICE-INDEX", "2026-05-01")]["metadata"][
        "official_series_code"
    ] == "DPCCRG"
    real_pce = by_series_and_date[("BEA-REAL-PCE-MOM", "2026-05-01")]
    assert real_pce["metadata"]["official_series_code"] == "DPCERAM"
    assert real_pce["metadata"]["vintage_status"] == "current_release_vintage"
    assert by_series_and_date[("BEA-DPI-REAL-SAAR", "2026-05-01")]["metadata"][
        "reference_year"
    ] == 2017


@pytest.mark.parametrize(
    ("summary", "section2", "message"),
    [
        (
            _bea_pio_summary_workbook(real_pce=0.4),
            _bea_pio_section2_workbook(),
            "disagree for BEA-REAL-PCE-MOM",
        ),
        (
            _bea_pio_summary_workbook(),
            _bea_pio_section2_workbook(duplicate_code=True),
            "duplicated NIPA code A067RM",
        ),
        (
            _bea_pio_summary_workbook(),
            _bea_pio_section2_workbook(missing_middle=True),
            "missing/non-numeric value after history began",
        ),
    ],
)
def test_bea_pio_provider_fails_closed_on_inconsistent_workbooks(
    summary, section2, message
):
    result = BEAPIOReleaseProvider(
        client=_bea_pio_client(summary, section2)
    ).personal_income_outlays()

    assert not result.ok
    assert result.records == []
    assert message in result.error


def test_census_release_provider_prefers_current_workbook_and_preserves_status():
    workbook = _census_current_workbook()
    result = CensusMARTSReleaseProvider(
        client=_census_client(workbook, current_status=200)
    ).monthly_retail_sales()

    assert result.ok
    assert result.metadata["workbook_url"] == CENSUS_MARTS_CURRENT_WORKBOOK
    assert result.metadata["workbook_scope"] == "current"
    assert result.metadata["latest_value_date"] == "2026-05-01"
    assert result.metadata["artifacts"][0]["sha256"] == hashlib.sha256(
        workbook
    ).hexdigest()
    by_series_and_date = {
        (item["series_id"], item["date"]): item for item in result.records
    }
    latest = by_series_and_date[("CENSUS-MRTS-44X72-SM-SA", "2026-05-01")]
    assert latest["value"] == 763705
    assert latest["metadata"]["estimate_status"] == "(a)"
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA", "2026-04-01")][
        "value"
    ] == 757036
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA-MOM", "2026-05-01")][
        "value"
    ] == Decimal("0.9")
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA-YOY", "2026-05-01")][
        "value"
    ] == Decimal("6.9")
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA-MOM", "2026-04-01")][
        "value"
    ] == Decimal("0.4")


def test_census_release_provider_falls_back_to_latest_archive_file():
    workbook = _census_workbook()
    result = CensusMARTSReleaseProvider(client=_census_client(workbook)).monthly_retail_sales()

    assert result.ok
    assert result.metadata["workbook_url"].endswith("rs2604.xlsx")
    assert result.metadata["workbook_scope"] == "historical_archive"
    assert result.metadata["latest_value_date"] == "2026-04-01"
    assert result.metadata["artifacts"][1]["sha256"] == hashlib.sha256(workbook).hexdigest()
    by_series_and_date = {
        (item["series_id"], item["date"]): item for item in result.records
    }
    latest = by_series_and_date[("CENSUS-MRTS-44X72-SM-SA", "2026-04-01")]
    assert latest["value"] == 757085
    assert latest["metadata"]["estimate_status"] == "(a)"
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA-MOM", "2026-04-01")][
        "value"
    ] == Decimal("0.5")
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA-YOY", "2026-03-01")][
        "value"
    ] == Decimal("4.2")
    assert by_series_and_date[("CENSUS-MRTS-44X72-SM-SA-MOM", "2026-04-01")][
        "metadata"
    ]["estimate_status"] == "(a)"


@pytest.mark.django_db
def test_consumer_dashboard_refuses_partial_metric_set():
    census = CensusMARTSReleaseProvider(
        client=_census_client(_census_current_workbook(), current_status=200)
    ).monthly_retail_sales()
    record_provider_result(census, persist=_store_release_workbook_observations)

    assert publish_official_dashboards(keys={"consumer"}) == []


@pytest.mark.django_db
def test_release_workbooks_persist_lineage_and_publish_gdp_and_consumer_pages(client):
    bea = BEAGDPReleaseProvider(
        client=_bea_client(_bea_vintage_workbook(), _bea_comparison_workbook())
    ).gdp_pce()
    census_api = _census_api_result()
    census = CensusMARTSReleaseProvider(
        client=_census_client(_census_current_workbook(), current_status=200)
    ).monthly_retail_sales()
    pio = BEAPIOReleaseProvider(
        client=_bea_pio_client(
            _bea_pio_summary_workbook(),
            _bea_pio_section2_workbook(),
        )
    ).personal_income_outlays()
    bea_run = record_provider_result(bea, persist=_store_release_workbook_observations)
    census_api_run = record_provider_result(
        census_api, persist=_store_release_workbook_observations
    )
    census_run = record_provider_result(census, persist=_store_release_workbook_observations)
    pio_run = record_provider_result(pio, persist=_store_release_workbook_observations)
    g19, household = _consumer_credit_results()
    g19_run = record_provider_result(g19, persist=_store_release_workbook_observations)
    household_run = record_provider_result(
        household, persist=_store_release_workbook_observations
    )
    _record_census_revision_witness(
        [census_api_run, census_run, pio_run, g19_run, household_run]
    )
    census_api_run.refresh_from_db()

    dashboards = {
        item.key: item
        for item in publish_official_dashboards(keys={"gdp", "consumer"})
    }

    assert bea_run.status == "success"
    assert census_api_run.status == "success"
    assert census_run.status == "success"
    assert pio_run.status == "success"
    assert g19_run.status == "success"
    assert household_run.status == "success"
    witness = census_api_run.metadata["legacy_revision_witness"]
    assert witness["latest_value_date"] == "2026-05-01"
    assert witness["overlap_count"] == 7
    assert witness["differences"] == []
    assert RawArtifact.objects.filter(run=bea_run).count() == 3
    assert RawArtifact.objects.filter(run=census_run).count() == 1
    assert RawArtifact.objects.filter(run=pio_run).count() == 3
    assert ReleaseVintageObservation.objects.filter(batch_id=bea_run.batch_id).count() == 12
    second_estimate = ReleaseVintageObservation.objects.get(
        batch_id=bea_run.batch_id,
        series__key="bea-a191rl",
        value_date=datetime(2026, 1, 1, tzinfo=UTC),
        release_date="2026-05-28",
    )
    assert second_estimate.value == Decimal("1.6")
    assert second_estimate.estimate_round == "Second"
    assert second_estimate.as_of.date().isoformat() == "2026-05-28"
    assert second_estimate.fetched_at == bea.fetched_at
    assert second_estimate.source.key == "bea-release"
    assert second_estimate.license_scope == second_estimate.source.license_scope
    assert second_estimate.fallback_source is None
    assert second_estimate.quality_status == Observation.Quality.FRESH
    assert set(dashboards) == {"gdp", "consumer"}
    gdp = {item["key"]: item for item in dashboards["gdp"].data["metrics"]}
    consumer = {
        item["key"]: item for item in dashboards["consumer"].data["metrics"]
    }
    assert gdp["bea-a191rl"]["display_value"] == "2.10%"
    assert gdp["bea-dpcerl"]["display_value"] == "0.50%"
    assert gdp["bea-pce-contribution"]["display_value"] == "0.37pp"
    gdp_charts = dashboards["gdp"].data["charts"]
    assert [chart["key"] for chart in gdp_charts] == [
        "gdp-growth-history",
        "gdp-vintage-trail",
    ]
    assert [row["实际 GDP"] for row in gdp_charts[1]["data"]] == [1.6, 2.1]
    assert [row["date"] for row in gdp_charts[1]["data"]] == [
        "Second\n05-28",
        "Third\n06-25",
    ]
    assert gdp_charts[1]["panel_class"] == "lg:col-span-2"
    assert gdp_charts[1]["data"][0]["_lineage"]["实际 GDP"][
        "estimate_round"
    ] == "Second"
    revision_section = dashboards["gdp"].data["sections"][0]
    assert revision_section["title"] == "GDP 发布轮次与修订路径"
    assert revision_section["rows"][0]["display_value"] == "1.60% → 2.10%"
    assert "累计修订 +0.50pp" in revision_section["rows"][0]["status"]
    assert consumer["census-mrts-44x72-sm-sa"]["display_value"] == "763,705 USD mn"
    assert consumer["census-mrts-44x72-sm-sa-mom"]["display_value"] == "0.90%"
    assert consumer["census-mrts-44x72-sm-sa-mom"]["change_unit"] == "pp"
    assert consumer["bea-real-pce-mom"]["display_value"] == "0.30%"
    assert consumer["bea-real-dpi-mom"]["display_value"] == "0.30%"
    assert consumer["bea-personal-saving-rate"]["display_value"] == "3.00%"
    assert consumer["census-mrts-44x72-sm-sa"]["source_key"] == "census-release"
    assert consumer["bea-real-pce-mom"]["source_key"] == "bea-pio-release"
    assert consumer["g19-consumer-credit-outstanding-sa"]["source_key"] == (
        "federal-reserve-g19"
    )
    assert consumer["hhdc-total-debt-balance"]["source_key"] == (
        "ny-fed-household-credit"
    )
    charts = dashboards["consumer"].data["charts"]
    assert [chart["key"] for chart in charts] == [
        "retail-sales",
        "real-consumption-income-momentum",
        "personal-saving-rate",
        "consumer-credit-composition",
        "household-debt-composition",
        "household-debt-delinquency",
    ]
    assert dashboards["consumer"].data["chart_data"] == charts[0]["data"]
    assert dashboards["consumer"].data["contract_version"] == 1
    assert dashboards["consumer"].data["retail_batch_id"] == str(
        census_run.batch_id
    )
    assert charts[0]["source_keys"] == ["census-release"]
    assert charts[1]["source_keys"] == ["bea-pio-release"]
    assert charts[0]["data"][0]["_lineage"]["零售与餐饮服务"][
        "source_key"
    ] == "census-release"
    response = client.get("/economy/consumer/")
    body = response.content.decode()
    assert response.status_code == 200
    assert body.count(" data-chart ") == 6
    assert "dashboard-chart-0" in body
    assert "dashboard-chart-1" in body
    assert "dashboard-chart-2" in body
    assert "dashboard-chart-5" in body
    assert "实际 PCE 环比" in body
    assert "3.00%" in body
    assert "U.S. Bureau of Economic Analysis Personal Income and Outlays Releases" in body
    assert "New York Fed Household Debt and Credit" in body
    assert "来源：Atlas Macro Derived Data" not in body
    gdp_response = client.get("/economy/gdp/")
    gdp_body = gdp_response.content.decode()
    assert gdp_response.status_code == 200
    assert gdp_body.count(" data-chart ") == 2
    assert "GDP 发布轮次与修订路径" in gdp_body
    assert "1.60% → 2.10%" in gdp_body

    runs = [
        bea_run,
        census_api_run,
        census_run,
        pio_run,
        g19_run,
        household_run,
    ]
    assert _keys_with_current_required_batches({"gdp", "consumer"}, runs) == {
        "gdp",
        "consumer",
    }
    census_api_run.dataset = "mrts:44X72:SM:yes"
    census_api_run.save(update_fields=["dataset", "updated_at"])
    assert _keys_with_current_required_batches({"gdp", "consumer"}, runs) == {
        "gdp",
        "consumer",
    }
    census_api_run.dataset = "marts:44X72:SM:yes"
    census_api_run.save(update_fields=["dataset", "updated_at"])
    census_run.dataset = "marts:44X72:SM:yes"
    census_run.save(update_fields=["dataset", "updated_at"])
    assert _keys_with_current_required_batches({"gdp", "consumer"}, runs) == {"gdp"}
    census_run.dataset = "marts:retail-food-services"
    census_run.save(update_fields=["dataset", "updated_at"])
    latest_pio = Observation.objects.filter(
        series__key="bea-real-pce-mom",
        source__key="bea-pio-release",
    ).latest("value_date")
    latest_pio.batch_id = uuid.uuid4()
    latest_pio.save(update_fields=["batch_id", "updated_at"])
    assert _keys_with_current_required_batches({"gdp", "consumer"}, runs) == {
        "gdp"
    }
    _mark_latest_dashboards_stale({"consumer"}, runs)
    stale = DashboardSnapshot.objects.filter(key="consumer").latest("created_at")
    assert stale.quality_status == "stale"
    assert "上一版完整快照" in stale.data["refresh_failure"]["reason"]

    latest_pio.batch_id = pio_run.batch_id
    latest_pio.save(update_fields=["batch_id", "updated_at"])
    latest_g19 = Observation.objects.filter(
        series__key="g19-consumer-credit-outstanding-sa",
        source__key="federal-reserve-g19",
    ).latest("value_date")
    latest_g19.batch_id = uuid.uuid4()
    latest_g19.save(update_fields=["batch_id", "updated_at"])
    assert _keys_with_current_required_batches({"gdp", "consumer"}, runs) == {
        "gdp"
    }
    latest_g19.batch_id = g19_run.batch_id
    latest_g19.save(update_fields=["batch_id", "updated_at"])
    assert publish_official_dashboards(keys={"consumer"}) == []
    recovered = DashboardSnapshot.objects.get(pk=stale.pk)
    assert "refresh_failure" not in recovered.data

    census_api_run.status = IngestionRun.Status.PARTIAL
    census_api_run.row_count = 0
    census_api_run.metadata = {"reason": "CENSUS_API_KEY is not configured"}
    census_api_run.save(
        update_fields=["status", "row_count", "metadata", "updated_at"]
    )
    assert _publishable_keys_for_source_groups(runs, MACRO_PUBLICATION_GROUPS) == {
        "gdp",
        "consumer",
    }


@pytest.mark.django_db
def test_dashboard_deduplicates_same_date_across_provider_sources():
    older_fetch = datetime(2026, 6, 25, tzinfo=UTC)
    newer_fetch = older_fetch + timedelta(days=1)
    record_provider_result(
        ProviderResult(
            provider="bea",
            dataset="api-fixture",
            fetched_at=older_fetch,
            records=[
                {"series_id": "BEA-A191RL", "date": "2026-01-01", "value": "9.9"},
                {"series_id": "BEA-A191RL", "date": "2025-10-01", "value": "1.0"},
            ],
        ),
        persist=store_series_observations,
    )
    record_provider_result(
        ProviderResult(
            provider="bea-release",
            dataset="release-fixture",
            fetched_at=newer_fetch,
            records=[
                {"series_id": "BEA-A191RL", "date": "2026-01-01", "value": "2.1"},
                {"series_id": "BEA-A191RL", "date": "2025-10-01", "value": "0.5"},
            ],
        ),
        persist=store_series_observations,
    )

    dashboard = publish_official_dashboards(keys={"gdp"})[0]
    metric = next(item for item in dashboard.data["metrics"] if item["key"] == "bea-a191rl")
    chart = dashboard.data["chart_data"]

    assert metric["display_value"] == "2.10%"
    assert metric["change"] == 1.6
    assert [row["date"] for row in chart] == ["2025-10-01", "2026-01-01"]


@pytest.mark.django_db
def test_release_persistence_rejects_regressed_latest_month():
    current = ProviderResult(
        provider="census-release",
        dataset="regression-guard-current",
        records=[
            {
                "series_id": "CENSUS-MRTS-44X72-SM-SA",
                "date": "2026-05-01",
                "value": "100",
            }
        ],
    )
    current_run = record_provider_result(
        current, persist=_store_release_workbook_observations
    )
    regressed = ProviderResult(
        provider="census-release",
        dataset="regression-guard-old",
        records=[
            {
                "series_id": "CENSUS-MRTS-44X72-SM-SA",
                "date": "2026-04-01",
                "value": "90",
            }
        ],
    )
    regressed_run = record_provider_result(
        regressed, persist=_store_release_workbook_observations
    )

    assert current_run.status == "success"
    assert regressed_run.status == "failed"
    assert "latest value date regressed" in regressed_run.error
    assert Observation.objects.filter(source__key="census-release").count() == 1


@pytest.mark.django_db
def test_gdp_publication_gate_requires_vintages_from_the_same_release_batch():
    result = BEAGDPReleaseProvider(
        client=_bea_client(_bea_vintage_workbook(), _bea_comparison_workbook())
    ).gdp_pce()
    run = record_provider_result(result, persist=_store_release_workbook_observations)

    assert _keys_with_current_required_batches({"gdp"}, [run]) == {"gdp"}
    ReleaseVintageObservation.objects.filter(batch_id=run.batch_id).delete()
    assert _keys_with_current_required_batches({"gdp"}, [run]) == set()


@pytest.mark.django_db
def test_gdp_vintage_persistence_is_idempotent_and_rebinds_the_refresh_batch():
    first_result = BEAGDPReleaseProvider(
        client=_bea_client(_bea_vintage_workbook(), _bea_comparison_workbook())
    ).gdp_pce()
    first_run = record_provider_result(
        first_result,
        persist=_store_release_workbook_observations,
    )
    second_result = BEAGDPReleaseProvider(
        client=_bea_client(_bea_vintage_workbook(), _bea_comparison_workbook())
    ).gdp_pce()
    second_run = record_provider_result(
        second_result,
        persist=_store_release_workbook_observations,
    )

    assert first_run.status == IngestionRun.Status.SUCCESS
    assert second_run.status == IngestionRun.Status.SUCCESS
    assert ReleaseVintageObservation.objects.count() == 12
    assert set(
        ReleaseVintageObservation.objects.values_list("batch_id", flat=True)
    ) == {second_run.batch_id}


def test_economy_catalog_separates_live_release_data_from_remaining_gaps():
    requirements = {item["key"]: item for item in DATA_REQUIREMENTS}

    assert requirements["bea-gdp-pce"]["status"] == "live"
    assert requirements["bea-gdp-contributions"]["status"] == "live"
    assert requirements["bea-gdp-vintage-trail"]["status"] == "live"
    assert "独立 release-vintage 数据层" in requirements["bea-gdp-vintage-trail"][
        "reason"
    ]
    assert requirements["census-retail"]["status"] == "live"
    assert "发布工作簿" in requirements["census-retail"]["reason"]
    assert requirements["bea-personal-income-outlays"]["status"] == "live"
    assert "Section 2" in requirements["bea-personal-income-outlays"]["reason"]
    assert requirements["bea-pio-vintage-trail"]["status"] == "needs_source"
    assert requirements["census-retail-history"]["status"] == "needs_source"
    assert requirements["consumer-credit-official"]["status"] == "live"
    assert requirements["consumer-credit-vintage-trail"]["status"] == "needs_source"
    assert requirements["consumer-confidence"]["status"] == "purchase_required"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (
            ("success", "success", "success", "success", "success", "success"),
            {"gdp", "consumer"},
        ),
        (
            ("success", "failed", "success", "success", "success", "success"),
            {"gdp", "consumer"},
        ),
        (("success", "success", "failed", "success", "success", "success"), {"gdp"}),
        (("success", "success", "success", "failed", "success", "success"), {"gdp"}),
        (("failed", "success", "success", "success", "success", "success"), {"consumer"}),
        (("success", "success", "success", "success", "partial", "success"), {"gdp"}),
        (("success", "success", "success", "success", "success", "failed"), {"gdp"}),
    ],
)
def test_macro_publication_groups_isolate_unrelated_page_failures(statuses, expected):
    runs = [
        SimpleNamespace(
            source=SimpleNamespace(key=source_key),
            status=status,
            row_count=1 if status == "success" else 0,
        )
        for source_key, status in zip(
            (
                "bea-release",
                "census",
                "census-release",
                "bea-pio-release",
                "federal-reserve-g19",
                "ny-fed-household-credit",
            ),
            statuses,
            strict=True,
        )
    ]

    assert (
        _publishable_keys_for_source_groups(runs, MACRO_PUBLICATION_GROUPS)
        == expected
    )
