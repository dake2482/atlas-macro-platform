from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from research import credit_official
from research.credit_official import (
    ChicagoFedNFCIProvider,
    FederalReserveSLOOSProvider,
    TreasuryHQMProvider,
)
from research.official_data import publish_official_dashboards
from research.providers import ProviderResult
from research.services import record_provider_result, store_series_observations


def _client(handler):
    return httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )


def test_chicago_fed_nfci_normalizes_weekly_indexes_and_blocks_public_display():
    payload = """Friday_of_Week,NFCI,ANFCI,Risk,Credit,Leverage,Nonfinancial_Leverage
06/26/2026,-0.505,-0.492,-0.592,-0.030,0.421,-0.390
07/03/2026,-0.515,-0.506,-0.594,-0.034,0.404,-0.386
"""

    def handler(request):
        assert request.url.path == "/NFCI/nfci-data-series-csv.csv"
        return httpx.Response(200, text=payload)

    result = ChicagoFedNFCIProvider(client=_client(handler)).weekly_indexes()

    assert result.ok
    assert result.row_count == 12
    latest = {item["series_id"]: item for item in result.records if item["date"] == "2026-07-03"}
    assert latest["NFCI"]["value"] == Decimal("-0.515")
    assert latest["NFCI-CREDIT"]["value"] == Decimal("-0.034")
    assert latest["NFCI"]["metadata"]["frequency"] == "weekly"
    assert latest["NFCI"]["metadata"]["date_convention"] == "week ending Friday"
    assert result.metadata["license_status"] == "review"
    assert result.metadata["public_display_allowed"] is False
    assert "written permission" in result.metadata["license_note"]


def _sloos_zip() -> bytes:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<message:MessageGroup
  xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
  xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
  xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
  xmlns:kf="http://www.federalreserve.gov/structure/compact/SLOOS_SLOOS">
  <message:Header>
    <message:Prepared>2026-04-30T15:25:34</message:Prepared>
  </message:Header>
  <kf:DataSet>
    <kf:Series CURRENCY="NA" FREQ="162" LOANGROUP="BUS" MEASURE="STND"
      PANEL="DOM" SERIES_NAME="SUBLPDMBS_XWB_N.Q" UNIT="Percent" UNIT_MULT="1">
      <frb:Annotations>
        <common:Annotation>
          <common:AnnotationType>Short Description</common:AnnotationType>
          <common:AnnotationText>Net percentage tightening business-loan standards</common:AnnotationText>
        </common:Annotation>
      </frb:Annotations>
      <frb:Obs OBS_STATUS="A" OBS_VALUE="-1.2" TIME_PERIOD="2025-12-31" />
      <frb:Obs OBS_STATUS="A" OBS_VALUE="1.5" TIME_PERIOD="2026-06-30" />
    </kf:Series>
    <kf:Series CURRENCY="NA" FREQ="162" LOANGROUP="BUS" MEASURE="DEMAND"
      PANEL="DOM" SERIES_NAME="SUBLPDMBD_XWB_N.Q" UNIT="Percent" UNIT_MULT="1">
      <frb:Annotations>
        <common:Annotation>
          <common:AnnotationType>Short Description</common:AnnotationType>
          <common:AnnotationText>Net percentage stronger business-loan demand</common:AnnotationText>
        </common:Annotation>
      </frb:Annotations>
      <frb:Obs OBS_STATUS="A" OBS_VALUE="6.2" TIME_PERIOD="2026-06-30" />
    </kf:Series>
  </kf:DataSet>
</message:MessageGroup>
"""
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SLOOS_data.xml", xml)
    return target.getvalue()


def test_sloos_normalizes_selected_board_series_with_open_licence_metadata():
    payload = _sloos_zip()

    def handler(request):
        assert request.url.path == "/datadownload/Output.aspx"
        assert request.url.params["rel"] == "SLOOS"
        assert request.url.params["filetype"] == "zip"
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "application/x-zip-compressed"},
        )

    provider = FederalReserveSLOOSProvider(client=_client(handler))
    result = provider.quarterly_series(series_ids=("SUBLPDMBS_XWB_N.Q", "SUBLPDMBD_XWB_N.Q"))

    assert result.ok
    assert result.row_count == 3
    latest = {item["series_id"]: item for item in result.records if item["date"] == "2026-06-30"}
    standards = latest["SUBLPDMBS_XWB_N.Q"]
    assert standards["value"] == Decimal("1.5")
    assert standards["metadata"]["unit"] == "Percent"
    assert standards["metadata"]["frequency"] == "quarterly"
    assert standards["metadata"]["measure"] == "STND"
    assert standards["metadata"]["loan_group"] == "BUS"
    assert standards["metadata"]["observation_status"] == "A"
    assert standards["metadata"]["description"].startswith("Net percentage")
    assert result.metadata["prepared_at"] == "2026-04-30T15:25:34"
    assert result.metadata["license_status"] == "open"
    assert result.metadata["public_display_allowed"] is True
    assert result.metadata["missing_series"] == []


def test_sloos_rejects_invalid_archive():
    def handler(_request):
        return httpx.Response(200, content=b"not-a-zip")

    result = FederalReserveSLOOSProvider(client=_client(handler)).quarterly_series(
        series_ids=("SUBLPDMBS_XWB_N.Q",)
    )

    assert not result.ok
    assert "BadZipFile" in result.error


def test_sloos_rejects_an_explicitly_empty_series_selection():
    def handler(_request):
        pytest.fail("empty selection must fail before making a request")

    result = FederalReserveSLOOSProvider(client=_client(handler)).quarterly_series(series_ids=())

    assert not result.ok
    assert result.error == "series_ids cannot be empty"


class _FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.nrows = len(rows)
        self.ncols = max(map(len, rows))

    def cell_value(self, row, column):
        values = self.rows[row]
        return values[column] if column < len(values) else ""


class _FakeWorkbook:
    datemode = 0

    def __init__(self, sheet):
        self.sheet = sheet

    def sheet_by_index(self, index):
        assert index == 0
        return self.sheet


@pytest.fixture
def hqm_workbook():
    return _FakeWorkbook(
        _FakeSheet(
            [
                ["The Treasury High Quality Market Corporate Bond Yield Curve"],
                ["Monthly Average Par Yields, Percent"],
                [],
                ["Date", "", "Maturity", "", "", ""],
                ["", "", "2 Years", "5 Years", "10 Years", "30 Years"],
                [],
                ["May 2026", "", 4.83, 5.01, 5.31, 5.74],
                ["Jun 2026", "", 4.79, 4.98, 5.29, 5.72],
            ]
        )
    )


def test_treasury_hqm_normalizes_monthly_par_curve(monkeypatch, hqm_workbook):
    def handler(request):
        assert request.url.path == "/system/files/226/hqm_qh_pars.xls"
        return httpx.Response(200, content=b"official-xls-fixture")

    def fake_open_workbook(*, file_contents, on_demand):
        assert file_contents == b"official-xls-fixture"
        assert on_demand is True
        return hqm_workbook

    monkeypatch.setattr(credit_official.xlrd, "open_workbook", fake_open_workbook)
    result = TreasuryHQMProvider(client=_client(handler)).par_yields()

    assert result.ok
    assert result.row_count == 8
    june = {item["series_id"]: item for item in result.records if item["date"] == "2026-06-30"}
    assert june["HQM-PAR-2Y"]["value"] == Decimal("4.79")
    assert june["HQM-PAR-30Y"]["value"] == Decimal("5.72")
    assert june["HQM-PAR-10Y"]["metadata"]["maturity_years"] == 10
    assert june["HQM-PAR-10Y"]["metadata"]["unit"] == "percent"
    assert june["HQM-PAR-10Y"]["metadata"]["date_convention"] == "reference month end"
    assert june["HQM-PAR-10Y"]["metadata"]["not_oas"] is True
    assert result.metadata["tenors"] == [2, 5, 10, 30]
    assert result.metadata["license_status"] == "open"
    assert result.metadata["public_display_allowed"] is True
    assert "USCODE-2024-title17-chap1-sec105" in result.metadata["copyright_basis_url"]


@pytest.mark.django_db
def test_official_credit_proxies_publish_without_impersonating_oas():
    fetched_at = datetime(2026, 7, 12, tzinfo=UTC)
    hqm = ProviderResult(
        provider="us-treasury-hqm",
        dataset="hqm-fixture",
        fetched_at=fetched_at,
        records=[
            {"series_id": f"HQM-PAR-{tenor}Y", "date": "2026-06-30", "value": value}
            for tenor, value in ((2, "4.46"), (5, "4.70"), (10, "5.18"), (30, "5.78"))
        ],
    )
    sloos = ProviderResult(
        provider="federal-reserve-sloos",
        dataset="sloos-fixture",
        fetched_at=fetched_at,
        records=[
            {
                "series_id": series_id,
                "date": "2026-06-30",
                "value": value,
            }
            for series_id, value in (
                ("SUBLPDMBS_XWB_N.Q", "1.5"),
                ("SUBLPDMBD_XWB_N.Q", "6.2"),
                ("SUBLPDCILS_N.Q", "9.1"),
                ("SUBLPDCISS_N.Q", "10.4"),
            )
        ],
    )
    record_provider_result(hqm, persist=store_series_observations)
    record_provider_result(sloos, persist=store_series_observations)

    dashboards = {item.key: item for item in publish_official_dashboards()}

    assert {"credit", "credit-spreads", "credit-stress"} <= dashboards.keys()
    assert "不是 OAS" in dashboards["credit-spreads"].summary
    spread_metrics = {
        item["key"]: item for item in dashboards["credit-spreads"].data["metrics"]
    }
    assert spread_metrics["hqm-par-10y"]["display_value"] == "5.18%"
    stress_metrics = {
        item["key"]: item for item in dashboards["credit-stress"].data["metrics"]
    }
    assert stress_metrics["sublpdmbs_xwb_n.q"]["source_key"] == "federal-reserve-sloos"
