from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import httpx
import pytest

from research.fed_prates import FederalReservePRATESProvider, _decimal_or_none
from research.models import RawArtifact
from research.official_data import _store_prates_observations, publish_official_dashboards
from research.providers import ProviderResult
from research.services import record_provider_result, store_series_observations


def _archive(xml: str, member_name: str = "PRATES_data.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, xml)
    return buffer.getvalue()


def _xml(series_name: str = "RESBM_N.D") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <message:MessageGroup
      xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
      xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
      xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
      xmlns:kf="http://www.federalreserve.gov/structure/compact/PRATES_PRATES_POLICY_RATES">
      <message:Header><message:Prepared>2026-07-10T15:44:00Z</message:Prepared></message:Header>
      <frb:DataSet>
        <kf:Series SERIES_NAME="{series_name}" FREQ="8" UNIT="Percent"
          UNIT_MULT="1" CURRENCY="NA" INT_RATES_PAID="IOR">
          <frb:Annotations><common:Annotation>
            <common:AnnotationText>Interest rate on reserve balances (IORB rate)</common:AnnotationText>
          </common:Annotation></frb:Annotations>
          <frb:Obs OBS_STATUS="A" OBS_VALUE="3.65" TIME_PERIOD="2026-07-10" />
        </kf:Series>
      </frb:DataSet>
    </message:MessageGroup>"""


def _client(content: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datadownload/Output.aspx"
        assert dict(request.url.params) == {"rel": "PRATES", "filetype": "zip"}
        return httpx.Response(200, content=content)

    return httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))


def test_prates_provider_normalizes_iorb_and_preserves_board_lineage():
    result = FederalReservePRATESProvider(client=_client(_archive(_xml()))).iorb()

    assert result.ok
    assert result.row_count == 1
    assert result.metadata["found_series"] == ["RESBM_N.D"]
    assert result.metadata["missing_series"] == []
    record = result.records[0]
    assert record["series_id"] == "IORB"
    assert record["value"] == Decimal("3.65")
    assert record["metadata"]["board_series_id"] == "RESBM_N.D"
    assert record["metadata"]["unit"] == "Percent"


def test_prates_provider_marks_missing_required_series_partial():
    result = FederalReservePRATESProvider(
        client=_client(_archive(_xml("OTHER_N.D")))
    ).iorb()

    assert result.ok
    assert result.row_count == 0
    assert result.metadata["missing_series"] == ["RESBM_N.D"]
    assert result.metadata["quality_status"] == "partial"


def test_prates_provider_rejects_archive_without_data_member():
    result = FederalReservePRATESProvider(
        client=_client(_archive(_xml(), "WRONG.xml"))
    ).iorb()

    assert not result.ok
    assert "PRATES_data.xml is missing" in result.error


def test_prates_status_rejects_numeric_missing_sentinel():
    assert _decimal_or_none("-9999", "ND") is None
    assert _decimal_or_none("3.65", "A") == Decimal("3.65")


@pytest.mark.django_db
def test_prates_ingestion_cannot_bypass_coordinated_contracts():
    result = FederalReservePRATESProvider(client=_client(_archive(_xml()))).iorb()
    iorb_run = record_provider_result(result, persist=_store_prates_observations)
    sofr_run = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="test:sofr-distribution",
            records=[
                {
                    "series_id": "SOFR",
                    "date": "2026-07-10",
                    "value": "3.53",
                    "metadata": {"percentPercentile99": "3.72"},
                }
            ],
        ),
        persist=store_series_observations,
    )

    dashboards = publish_official_dashboards()

    assert iorb_run.status == "success"
    assert sofr_run.status == "success"
    artifact = RawArtifact.objects.get(run=iorb_run)
    assert artifact.sha256 == result.metadata["archive_sha256"]
    assert not any(item.key == "fed-funds" for item in dashboards)
    assert not any(item.key == "subsurface" for item in dashboards)
