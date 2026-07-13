from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import httpx
import pytest

from research.fed_h41 import H41_TARGET_SERIES, FederalReserveH41Provider
from research.models import RawArtifact
from research.official_data import _store_h41_observations, publish_official_dashboards
from research.services import record_provider_result


def _archive(xml: str, *, member_name: str = "H41_data.xml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, xml)
    return buffer.getvalue()


def _fixture_xml() -> str:
    series = []
    for index, (board_id, target) in enumerate(H41_TARGET_SERIES.items(), start=1):
        observations = (
            '<frb:Obs OBS_STATUS="A" OBS_VALUE="6735609" TIME_PERIOD="2026-07-08" />'
            '<frb:Obs OBS_STATUS="NA" TIME_PERIOD="2026-07-15" />'
            if board_id == "RESPPMA_N.WW"
            else (f'<frb:Obs OBS_STATUS="A" OBS_VALUE="{index * 100}" TIME_PERIOD="2026-07-08" />')
        )
        series.append(
            f"""
            <kf:Series SERIES_NAME="{board_id}" FREQ="19" CATEGORY="ASSET"
                SUBCATEGORY="TEST" COMPONENT="TEST" DISTRIBUTION="TOT"
                SERIESTYPE="L" UNIT="Currency" UNIT_MULT="1000000" CURRENCY="USD">
              <frb:Annotations><common:Annotation>
                <common:AnnotationType>Short Description</common:AnnotationType>
                <common:AnnotationText>{target["name"]}</common:AnnotationText>
              </common:Annotation></frb:Annotations>
              {observations}
            </kf:Series>
            """
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <message:MessageGroup
      xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
      xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
      xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
      xmlns:kf="http://www.federalreserve.gov/structure/compact/H41_H41">
      <message:Header><message:Prepared>2026-07-09T12:16:12Z</message:Prepared></message:Header>
      <frb:DataSet>{"".join(series)}</frb:DataSet>
    </message:MessageGroup>
    """


def _client(content: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datadownload/Output.aspx"
        assert dict(request.url.params) == {"rel": "H41", "filetype": "zip"}
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "application/x-zip-compressed"},
        )

    return httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(handler))


def test_h41_provider_streams_archive_and_keeps_missing_observations():
    content = _archive(_fixture_xml())
    provider = FederalReserveH41Provider(client=_client(content))

    result = provider.h41()

    assert result.ok
    assert result.row_count == 7
    assert result.metadata["archive_size"] == len(content)
    assert result.metadata["prepared_at"] == "2026-07-09T12:16:12Z"
    assert result.metadata["found_series"] == sorted(H41_TARGET_SERIES)
    assert result.metadata["missing_series"] == []
    assert result.metadata["status_counts"] == {"A": 6, "NA": 1}
    assert result.metadata["missing_observation_count"] == 1
    assert result.metadata["quality_status"] == "complete_with_missing_observations"

    total_assets = next(
        record
        for record in result.records
        if record["source_series_id"] == "RESPPMA_N.WW" and record["status"] == "A"
    )
    assert total_assets["series_id"] == "WALCL"
    assert total_assets["value"] == Decimal("6735609")
    assert total_assets["metadata"]["unit_multiplier"] == "1000000"
    assert total_assets["metadata"]["board_series_id"] == "RESPPMA_N.WW"

    missing = next(record for record in result.records if record["status"] == "NA")
    assert missing["date"] == "2026-07-15"
    assert missing["value"] is None
    assert missing["is_missing"] is True
    assert missing["status_label"] == "Not available"
    assert missing["metadata"]["raw_value"] is None


def test_h41_provider_reports_absent_requested_series_without_discarding_records():
    xml = _fixture_xml().replace('SERIES_NAME="RESH4SCS_N.WW"', 'SERIES_NAME="OTHER_N.WW"')
    provider = FederalReserveH41Provider(client=_client(_archive(xml)))

    result = provider.h41()

    assert result.ok
    assert result.metadata["missing_series"] == ["RESH4SCS_N.WW"]
    assert result.metadata["quality_status"] == "partial"
    assert all(record["source_series_id"] != "RESH4SCS_N.WW" for record in result.records)


def test_h41_provider_returns_failure_for_invalid_archive():
    provider = FederalReserveH41Provider(client=_client(b"not a zip archive"))

    result = provider.h41()

    assert not result.ok
    assert "BadZipFile" in result.error
    assert result.metadata["source_url"].endswith("rel=H41&filetype=zip")


def test_h41_provider_rejects_non_catalogued_series_before_network_call():
    provider = FederalReserveH41Provider(client=_client(b"unused"))

    result = provider.h41(series_ids=["UNKNOWN_SERIES"])

    assert not result.ok
    assert result.error == "unsupported H.4.1 series: UNKNOWN_SERIES"


def test_h41_provider_rejects_an_explicit_empty_series_selection():
    provider = FederalReserveH41Provider(client=_client(b"unused"))

    result = provider.h41(series_ids=[])

    assert not result.ok
    assert result.error == "no H.4.1 series requested"


@pytest.mark.django_db
def test_h41_ingestion_persists_fingerprint_and_publishes_balance_sheet():
    provider = FederalReserveH41Provider(client=_client(_archive(_fixture_xml())))
    result = provider.h41()

    run = record_provider_result(result, persist=_store_h41_observations)
    dashboards = publish_official_dashboards()

    assert run.status == "success"
    assert run.row_count == 6
    artifact = RawArtifact.objects.get(run=run)
    assert artifact.sha256 == result.metadata["archive_sha256"]
    assert f"sha256={artifact.sha256}" in artifact.uri
    balance_sheet = next(item for item in dashboards if item.key == "fed-balance-sheet")
    metrics = {item["key"]: item for item in balance_sheet.data["metrics"]}
    assert metrics["walcl"]["display_value"] == "6.74 USD tn"
    assert metrics["wrbwfrbl"]["source_key"] == "federal-reserve"
