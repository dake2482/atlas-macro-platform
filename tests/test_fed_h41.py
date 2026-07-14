from __future__ import annotations

import io
import zipfile
from decimal import Decimal

import httpx
import pytest
from django.db import transaction

from research.fed_h41 import H41_TARGET_SERIES, FederalReserveH41Provider
from research.models import DashboardSnapshot, Observation, RawArtifact
from research.official_data import _store_h41_observations, publish_official_dashboards
from research.services import begin_ingestion, record_provider_result
from research.tasks import refresh_h41_sources


def _archive(
    xml: str,
    *,
    member_name: str = "H41_data.xml",
    extra_member_names: tuple[str, ...] = (),
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, xml)
        for extra_member_name in extra_member_names:
            archive.writestr(extra_member_name, xml)
    return buffer.getvalue()


def _duplicate_series(xml: str, board_id: str) -> str:
    marker = f'<kf:Series SERIES_NAME="{board_id}"'
    start = xml.index(marker)
    end = xml.index("</kf:Series>", start) + len("</kf:Series>")
    return f"{xml[:end]}{xml[start:end]}{xml[end:]}"


def _fixture_xml() -> str:
    series = []
    for index, (board_id, target) in enumerate(H41_TARGET_SERIES.items(), start=1):
        observations = (
            '<frb:Obs OBS_STATUS="A" OBS_VALUE="6735609" TIME_PERIOD="2026-07-08" />'
            '<frb:Obs OBS_STATUS="NA" OBS_VALUE="-9999" TIME_PERIOD="2026-07-15" />'
            if board_id == "RESPPMA_N.WW"
            else (f'<frb:Obs OBS_STATUS="A" OBS_VALUE="{index * 100}" TIME_PERIOD="2026-07-08" />')
        )
        dimensions = (
            'CATEGORY="LIABCAP" SUBCATEGORY="OFDRB" COMPONENT="RBFRB" '
            'DISTRIBUTION="TOT" SERIESTYPE="L"'
            if board_id == "RESH4R_N.WW"
            else 'CATEGORY="ASSET" SUBCATEGORY="TEST" COMPONENT="TEST" '
            'DISTRIBUTION="TOT" SERIESTYPE="L"'
        )
        series.append(
            f"""
            <kf:Series SERIES_NAME="{board_id}" FREQ="19" {dimensions}
                UNIT="Currency" UNIT_MULT="1000000" CURRENCY="USD">
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
    assert missing["metadata"]["raw_value"] == "-9999"


def test_h41_provider_reports_absent_requested_series_without_discarding_records():
    xml = _fixture_xml().replace('SERIES_NAME="RESH4SCS_N.WW"', 'SERIES_NAME="OTHER_N.WW"')
    provider = FederalReserveH41Provider(client=_client(_archive(xml)))

    result = provider.h41()

    assert not result.ok
    assert "must appear exactly once" in result.error


def test_h41_provider_returns_failure_for_invalid_archive():
    provider = FederalReserveH41Provider(client=_client(b"not a zip archive"))

    result = provider.h41()

    assert not result.ok
    assert "BadZipFile" in result.error
    assert result.metadata["source_url"].endswith("rel=H41&filetype=zip")


def test_h41_provider_rejects_duplicate_archive_member_and_series():
    duplicate_member = FederalReserveH41Provider(
        client=_client(
            _archive(
                _fixture_xml(), extra_member_names=("nested/H41_data.xml",)
            )
        )
    ).h41()
    duplicate_series = FederalReserveH41Provider(
        client=_client(
            _archive(_duplicate_series(_fixture_xml(), "RESH4R_N.WW"))
        )
    ).h41()

    assert not duplicate_member.ok
    assert "exactly one H41_data.xml" in duplicate_member.error
    assert not duplicate_series.ok
    assert "must appear exactly once" in duplicate_series.error


def test_h41_provider_rejects_duplicate_observation_date():
    duplicate = (
        '<frb:Obs OBS_STATUS="A" OBS_VALUE="1" '
        'TIME_PERIOD="2026-07-08" />'
    )
    xml = _fixture_xml().replace(
        "</kf:Series>", f"{duplicate}</kf:Series>", 1
    )
    result = FederalReserveH41Provider(client=_client(_archive(xml))).h41()

    assert not result.ok
    assert "duplicate observation" in result.error


@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-07-07"])
def test_h41_provider_rejects_invalid_or_non_wednesday_observation(bad_date):
    xml = _fixture_xml().replace("2026-07-15", bad_date)
    result = FederalReserveH41Provider(client=_client(_archive(xml))).h41()

    assert not result.ok
    assert "observation" in result.error


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [
        ("SERIESTYPE", "L"),
        ("CATEGORY", "LIABCAP"),
        ("SUBCATEGORY", "OFDRB"),
        ("COMPONENT", "RBFRB"),
        ("DISTRIBUTION", "TOT"),
    ],
)
def test_h41_provider_rejects_reserves_semantic_dimension_drift(
    dimension, expected
):
    xml = _fixture_xml()
    start = xml.index('<kf:Series SERIES_NAME="RESH4R_N.WW"')
    end = xml.index("</kf:Series>", start)
    reserves_series = xml[start:end].replace(
        f'{dimension}="{expected}"', f'{dimension}="DRIFT"'
    )
    xml = f"{xml[:start]}{reserves_series}{xml[end:]}"
    result = FederalReserveH41Provider(client=_client(_archive(xml))).h41()

    assert not result.ok
    assert "semantic dimension drift" in result.error
    assert dimension in result.error


def test_h41_provider_rejects_common_dimension_drift():
    xml = _fixture_xml().replace('FREQ="19"', 'FREQ="52"', 1)
    result = FederalReserveH41Provider(client=_client(_archive(xml))).h41()

    assert not result.ok
    assert "semantic dimension drift" in result.error
    assert "FREQ" in result.error


@pytest.mark.parametrize(
    "prepared_at",
    ["", "not-a-timestamp", "2026-07-01T00:00:00Z", "2999-01-01T00:00:00Z"],
)
def test_h41_provider_rejects_invalid_future_or_regressed_prepared_at(prepared_at):
    xml = _fixture_xml().replace("2026-07-09T12:16:12Z", prepared_at)
    result = FederalReserveH41Provider(client=_client(_archive(xml))).h41()

    assert not result.ok
    assert "Prepared timestamp" in result.error


def test_h41_provider_accepts_release_lag_boundary_and_rejects_stalled_series():
    boundary_xml = (
        _fixture_xml()
        .replace("2026-07-09T12:16:12Z", "2026-07-08T12:16:12Z")
        .replace('TIME_PERIOD="2026-07-08"', 'TIME_PERIOD="2026-07-01"')
    )
    stalled_xml = boundary_xml.replace(
        "2026-07-08T12:16:12Z", "2026-07-09T12:16:12Z"
    )

    boundary = FederalReserveH41Provider(
        client=_client(_archive(boundary_xml))
    ).h41()
    stalled = FederalReserveH41Provider(
        client=_client(_archive(stalled_xml))
    ).h41()

    assert boundary.ok
    assert not stalled.ok
    assert "too old" in stalled.error
    assert "8 days" in stalled.error


def test_h41_provider_rejects_stalled_reserves_when_other_series_are_current():
    xml = _fixture_xml()
    marker = '<kf:Series SERIES_NAME="RESH4R_N.WW"'
    start = xml.index(marker)
    end = xml.index("</kf:Series>", start)
    reserves_series = xml[start:end].replace(
        'TIME_PERIOD="2026-07-08"', 'TIME_PERIOD="2026-07-01"'
    )
    xml = f"{xml[:start]}{reserves_series}{xml[end:]}"

    result = FederalReserveH41Provider(client=_client(_archive(xml))).h41()

    assert not result.ok
    assert "RESH4R_N.WW" in result.error
    assert "too old" in result.error
    assert "8 days" in result.error


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


def test_h41_task_raises_when_required_reserves_publication_is_stale(monkeypatch):
    summary = {
        "runs": [{"source": "federal-reserve", "dataset": "h41"}],
        "dashboard_keys": ["fed-balance-sheet"],
        "stale_dashboard_keys": ["reserves"],
    }
    monkeypatch.setattr("research.tasks.refresh_h41_data", lambda: summary)

    with pytest.raises(RuntimeError, match="required reserves v1"):
        refresh_h41_sources.run()


def test_h41_task_raises_when_required_balance_sheet_publication_is_stale(
    monkeypatch,
):
    summary = {
        "runs": [{"source": "federal-reserve", "dataset": "h41"}],
        "dashboard_keys": ["reserves"],
        "stale_dashboard_keys": ["fed-balance-sheet"],
    }
    monkeypatch.setattr("research.tasks.refresh_h41_data", lambda: summary)

    with pytest.raises(RuntimeError, match="required fed-balance-sheet v1"):
        refresh_h41_sources.run()


@pytest.mark.django_db
def test_h41_superseded_run_cannot_clobber_newer_persisted_batch():
    old_result = FederalReserveH41Provider(
        client=_client(_archive(_fixture_xml()))
    ).h41()
    old_run = begin_ingestion(
        "federal-reserve",
        "h41",
        metadata={"fetched_at": old_result.fetched_at.isoformat()},
    )
    new_result = FederalReserveH41Provider(
        client=_client(_archive(_fixture_xml()))
    ).h41()
    new_run = record_provider_result(
        new_result, persist=_store_h41_observations
    )
    before_batches = set(
        Observation.objects.filter(series__key="wrbwfrbl").values_list(
            "batch_id", flat=True
        )
    )
    before_artifacts = RawArtifact.objects.count()

    with transaction.atomic(), pytest.raises(ValueError, match="superseded h41"):
        _store_h41_observations(old_result, old_run.source, old_run)

    assert new_run.status == "success"
    assert before_batches == {new_run.batch_id}
    assert set(
        Observation.objects.filter(series__key="wrbwfrbl").values_list(
            "batch_id", flat=True
        )
    ) == before_batches
    assert RawArtifact.objects.count() == before_artifacts


@pytest.mark.django_db
def test_h41_later_attempt_cannot_persist_regressed_release_time():
    baseline_result = FederalReserveH41Provider(
        client=_client(_archive(_fixture_xml()))
    ).h41()
    baseline_run = record_provider_result(
        baseline_result, persist=_store_h41_observations
    )
    regressed_xml = _fixture_xml().replace(
        "2026-07-09T12:16:12Z", "2026-07-08T12:16:12Z"
    )
    regressed_result = FederalReserveH41Provider(
        client=_client(_archive(regressed_xml))
    ).h41()

    rejected_run = record_provider_result(
        regressed_result, persist=_store_h41_observations
    )

    assert baseline_run.status == "success"
    assert rejected_run.status == "failed"
    assert "release time regressed" in rejected_run.error
    assert set(
        Observation.objects.filter(series__key="wrbwfrbl").values_list(
            "batch_id", flat=True
        )
    ) == {baseline_run.batch_id}
    assert RawArtifact.objects.filter(run=rejected_run).count() == 0


@pytest.mark.django_db
def test_h41_invalid_nonempty_observation_release_watermark_fails_closed():
    baseline_result = FederalReserveH41Provider(
        client=_client(_archive(_fixture_xml()))
    ).h41()
    baseline_run = record_provider_result(
        baseline_result, persist=_store_h41_observations
    )
    run_metadata = dict(baseline_run.metadata)
    run_metadata.pop("source_release_time", None)
    baseline_run.metadata = run_metadata
    baseline_run.save(update_fields=["metadata", "updated_at"])
    observation = Observation.objects.filter(
        batch_id=baseline_run.batch_id, series__key="wrbwfrbl"
    ).first()
    observation_metadata = dict(observation.metadata)
    observation_metadata["source_release_time"] = "not-a-timestamp"
    observation.metadata = observation_metadata
    observation.save(update_fields=["metadata", "updated_at"])

    candidate_result = FederalReserveH41Provider(
        client=_client(_archive(_fixture_xml()))
    ).h41()
    rejected_run = record_provider_result(
        candidate_result, persist=_store_h41_observations
    )

    assert rejected_run.status == "failed"
    assert "invalid nonempty observation" in rejected_run.error
    assert set(
        Observation.objects.filter(series__key="wrbwfrbl").values_list(
            "batch_id", flat=True
        )
    ) == {baseline_run.batch_id}
    assert RawArtifact.objects.filter(run=rejected_run).count() == 0


@pytest.mark.django_db
def test_h41_persistence_rejects_stalled_reserves_when_walcl_is_current():
    result = FederalReserveH41Provider(
        client=_client(_archive(_fixture_xml()))
    ).h41()
    reserves = next(
        record
        for record in result.records
        if record["source_series_id"] == "RESH4R_N.WW"
    )
    reserves["date"] = "2026-07-01"

    run = record_provider_result(result, persist=_store_h41_observations)

    assert run.status == "failed"
    assert "RESH4R_N.WW" in run.error
    assert "too old" in run.error
    assert not Observation.objects.filter(batch_id=run.batch_id).exists()
    assert not RawArtifact.objects.filter(run=run).exists()


@pytest.mark.django_db
def test_h41_ingestion_persists_fingerprint_but_generic_publisher_cannot_bypass():
    provider = FederalReserveH41Provider(client=_client(_archive(_fixture_xml())))
    result = provider.h41()

    run = record_provider_result(result, persist=_store_h41_observations)
    dashboards = publish_official_dashboards()

    assert run.status == "success"
    assert run.row_count == 6
    artifact = RawArtifact.objects.get(run=run)
    assert artifact.sha256 == result.metadata["archive_sha256"]
    assert f"sha256={artifact.sha256}" in artifact.uri
    observation = Observation.objects.filter(
        batch_id=run.batch_id, series__key="wrbwfrbl"
    ).latest("value_date")
    assert observation.metadata["source_release_time"] == (
        "2026-07-09T12:16:12+00:00"
    )
    assert observation.metadata["release_freshness_days"] == 8
    assert all(item.key != "fed-balance-sheet" for item in dashboards)
    assert not DashboardSnapshot.objects.filter(key="fed-balance-sheet").exists()
