from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import httpx
import pytest

from research.fed_h10 import (
    H10_SOURCE_ATTRIBUTES,
    H10_TARGET_SERIES,
    FederalReserveH10Provider,
)
from research.models import RawArtifact
from research.official_data import (
    _coordinate_assets_fx_dashboard,
    _store_h10_observations,
    publish_official_dashboards,
)
from research.services import record_provider_result


def _archive(xml: str, member_name: str = "H10_data.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, xml)
    return buffer.getvalue()


def _xml(*, omit: str | None = None) -> str:
    series = []
    for index, (board_id, target) in enumerate(H10_TARGET_SERIES.items(), start=1):
        if board_id == omit:
            continue
        missing = (
            '<frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="2026-07-11" />'
            if index == 1
            else ""
        )
        attributes = H10_SOURCE_ATTRIBUTES[board_id]
        series.append(
            f"""
            <kf:Series SERIES_NAME="{board_id}" FREQ="{attributes['FREQ']}"
                CURRENCY="{attributes['CURRENCY']}" FX="{attributes['FX']}"
                UNIT="{attributes['UNIT']}" UNIT_MULT="{attributes['UNIT_MULT']}">
              <frb:Annotations><common:Annotation>
                <common:AnnotationText>{target["name"]}</common:AnnotationText>
              </common:Annotation></frb:Annotations>
              <frb:Obs OBS_STATUS="A" OBS_VALUE="{index}.25" TIME_PERIOD="2026-07-10" />
              {missing}
            </kf:Series>
            """
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <message:MessageGroup
      xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
      xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
      xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
      xmlns:kf="http://www.federalreserve.gov/structure/compact/H10_H10">
      <message:Header><message:Prepared>2026-07-11T12:00:00Z</message:Prepared></message:Header>
      <frb:DataSet>{"".join(series)}</frb:DataSet>
    </message:MessageGroup>"""


def _client(content: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datadownload/Output.aspx"
        assert dict(request.url.params) == {"rel": "H10", "filetype": "zip"}
        return httpx.Response(200, content=content)

    return httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))


def test_h10_provider_normalizes_four_series_and_preserves_quote_convention():
    result = FederalReserveH10Provider(client=_client(_archive(_xml()))).h10()

    assert result.ok
    assert result.row_count == 5
    assert result.metadata["found_series"] == sorted(H10_TARGET_SERIES)
    assert result.metadata["quality_status"] == "complete_with_missing_observations"
    euro = next(record for record in result.records if record["series_id"] == "H10-EURUSD")
    assert euro["value"] == Decimal("2.25")
    assert euro["source_series_id"] == "RXI$US_N.B.EU"
    assert euro["metadata"]["quote_convention"] == "U.S. dollars per euro"


def test_h10_provider_uses_status_to_reject_numeric_missing_sentinel():
    result = FederalReserveH10Provider(client=_client(_archive(_xml()))).h10()

    missing = next(record for record in result.records if record["status"] == "ND")
    assert missing["metadata"]["raw_value"] == "-9999"
    assert missing["value"] is None
    assert missing["is_missing"] is True
    assert result.metadata["status_counts"] == {"A": 4, "ND": 1}


def test_h10_provider_rejects_absent_required_series():
    omitted = "RXI_N.B.JA"
    result = FederalReserveH10Provider(
        client=_client(_archive(_xml(omit=omitted)))
    ).h10()

    assert not result.ok
    assert f"missing required H.10 series: {omitted}" in result.error


def test_h10_provider_rejects_invalid_archive_and_unknown_series():
    invalid = FederalReserveH10Provider(client=_client(b"not a zip")).h10()
    assert not invalid.ok
    assert "BadZipFile" in invalid.error

    unsupported = FederalReserveH10Provider(client=_client(b"unused")).h10(
        series_ids=["UNKNOWN"]
    )
    assert not unsupported.ok
    assert unsupported.error == "unsupported H.10 series: UNKNOWN"


@pytest.mark.django_db
def test_h10_ingestion_never_uses_generic_assets_fx_publication():
    result = FederalReserveH10Provider(client=_client(_archive(_xml()))).h10()
    run = record_provider_result(result, persist=_store_h10_observations)

    generic_dashboards = publish_official_dashboards()
    dashboards, stale = _coordinate_assets_fx_dashboard([run])

    assert run.status == "success"
    artifact = RawArtifact.objects.get(run=run)
    assert artifact.sha256 == result.metadata["archive_sha256"]
    assert all(item.key != "assets-fx" for item in generic_dashboards)
    assert dashboards == []
    assert stale == {"assets-fx"}
