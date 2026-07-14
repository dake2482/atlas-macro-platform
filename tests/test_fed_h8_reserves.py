from __future__ import annotations

import hashlib
import io
import zipfile
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from django.db import transaction

from atlasmacro import settings
from research.data_catalog import DATA_REQUIREMENTS
from research.fed_h8 import H8_TARGET_SERIES, FederalReserveH8Provider
from research.models import (
    DashboardSnapshot,
    MetricSnapshot,
    Observation,
    RawArtifact,
    SourceLicense,
)
from research.official_data import (
    H41_PUBLICATION_KEYS,
    RESERVES_CONTRACT_VERSION,
    RESERVES_REQUIRED_CHART_KEYS,
    RESERVES_REQUIRED_METRIC_KEYS,
    _coordinate_reserves_dashboard,
    _fresh_until,
    _reserves_page_contract_is_buildable,
    _store_h8_observations,
    _store_h41_observations,
    publish_official_dashboards,
)
from research.providers import ProviderResult
from research.services import begin_ingestion, ensure_source, record_provider_result
from research.tasks import refresh_h8_sources

FIXED_NOW = datetime(2026, 7, 12, 16, 30, tzinfo=UTC)
LATEST_WEDNESDAY = date(2026, 7, 8)


def _archive(
    xml: str,
    *,
    member_name: str = "H8_data.xml",
    extra_member_names: tuple[str, ...] = (),
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, xml)
        for extra_member_name in extra_member_names:
            archive.writestr(extra_member_name, xml)
    return buffer.getvalue()


def _duplicate_series(xml: str) -> str:
    start = xml.index("<kf:Series")
    end = xml.index("</kf:Series>", start) + len("</kf:Series>")
    return f"{xml[:end]}{xml[start:end]}{xml[end:]}"


def _h8_xml(*, series_id: str = "B1151NCBA") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <message:MessageGroup
      xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
      xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
      xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
      xmlns:kf="http://www.federalreserve.gov/structure/compact/H8_H8">
      <message:Header>
        <message:Prepared>2026-07-10T20:15:00Z</message:Prepared>
      </message:Header>
      <frb:DataSet>
        <kf:Series SERIES_NAME="{series_id}" BG="CB" CATEGORY="A"
          CURRENCY="USD" FREQ="19" H8_UNITS="LEVEL"
          ITEM="1151" SA="SA" UNIT="Currency"
          UNIT_MULT="1000000">
          <frb:Annotations><common:Annotation>
            <common:AnnotationText>Total assets, all commercial banks</common:AnnotationText>
          </common:Annotation></frb:Annotations>
          <frb:Obs OBS_STATUS="A" OBS_VALUE="25509970.7" TIME_PERIOD="2026-07-08" />
          <frb:Obs OBS_STATUS="ND" OBS_VALUE="-9999" TIME_PERIOD="2026-07-15" />
        </kf:Series>
      </frb:DataSet>
    </message:MessageGroup>
    """


def _client(content: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datadownload/Output.aspx"
        assert dict(request.url.params) == {"rel": "H8", "filetype": "zip"}
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "application/x-zip-compressed"},
        )

    return httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )


def test_h8_provider_streams_archive_and_preserves_board_lineage():
    content = _archive(_h8_xml())
    result = FederalReserveH8Provider(client=_client(content)).h8()

    assert result.ok
    assert result.dataset == "h8"
    assert result.row_count == 2
    assert result.metadata == {
        **result.metadata,
        "archive_size": len(content),
        "archive_sha256": hashlib.sha256(content).hexdigest(),
        "archive_member": "H8_data.xml",
        "prepared_at": "2026-07-10T20:15:00Z",
        "requested_series": ["B1151NCBA"],
        "found_series": ["B1151NCBA"],
        "missing_series": [],
        "quality_status": "complete_with_missing_observations",
        "status_counts": {"A": 1, "ND": 1},
        "missing_observation_count": 1,
    }
    current, unavailable = result.records
    assert current["series_id"] == "H8-B1151NCBA"
    assert current["source_series_id"] == "B1151NCBA"
    assert current["value"] == Decimal("25509970.7")
    assert current["metadata"]["unit_multiplier"] == "1000000"
    assert current["metadata"]["h8_units"] == "LEVEL"
    assert current["metadata"]["seasonal_adjustment"] == "SA"
    assert current["metadata"]["source_release_time"] == (
        "2026-07-10T20:15:00+00:00"
    )
    assert current["metadata"]["release_freshness_days"] == 8
    assert set(current["metadata"]["board_dimensions"]) == {
        "BG",
        "CATEGORY",
        "CURRENCY",
        "FREQ",
        "H8_UNITS",
        "ITEM",
        "SA",
        "UNIT",
        "UNIT_MULT",
    }
    assert unavailable["status"] == "ND"
    assert unavailable["value"] is None
    assert unavailable["is_missing"] is True
    assert unavailable["metadata"]["raw_value"] == "-9999"


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (
            lambda content: FederalReserveH8Provider(
                client=_client(content), max_archive_bytes=len(content) - 1
            ),
            "compressed-size limit",
        ),
        (
            lambda content: FederalReserveH8Provider(
                client=_client(content), max_xml_bytes=16
            ),
            "expanded-size limit",
        ),
    ],
)
def test_h8_provider_enforces_archive_limits(provider, message):
    content = _archive(_h8_xml())
    result = provider(content).h8()
    assert not result.ok
    assert message in result.error


def test_h8_provider_reports_missing_series_and_invalid_archives():
    missing = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml(series_id="OTHER")))
    ).h8()
    invalid = FederalReserveH8Provider(client=_client(b"not a zip")).h8()
    unsupported = FederalReserveH8Provider(client=_client(b"unused")).h8(
        series_ids=["UNKNOWN"]
    )

    assert not missing.ok
    assert "must appear exactly once" in missing.error
    assert invalid.error.startswith("BadZipFile:")
    assert unsupported.error == "unsupported H.8 series: UNKNOWN"
    assert set(H8_TARGET_SERIES) == {"B1151NCBA"}


def test_h8_provider_rejects_duplicate_archive_member_and_series():
    duplicate_member = FederalReserveH8Provider(
        client=_client(
            _archive(_h8_xml(), extra_member_names=("nested/H8_data.xml",))
        )
    ).h8()
    duplicate_series = FederalReserveH8Provider(
        client=_client(_archive(_duplicate_series(_h8_xml())))
    ).h8()

    assert not duplicate_member.ok
    assert "exactly one H8_data.xml" in duplicate_member.error
    assert not duplicate_series.ok
    assert "must appear exactly once" in duplicate_series.error


def test_h8_provider_rejects_duplicate_observation_date():
    duplicate = (
        '<frb:Obs OBS_STATUS="A" OBS_VALUE="1" '
        'TIME_PERIOD="2026-07-08" />'
    )
    xml = _h8_xml().replace("</kf:Series>", f"{duplicate}</kf:Series>")
    result = FederalReserveH8Provider(client=_client(_archive(xml))).h8()

    assert not result.ok
    assert "duplicate observation" in result.error


@pytest.mark.parametrize("bad_date", ["not-a-date", "2026-07-07"])
def test_h8_provider_rejects_invalid_or_non_wednesday_observation(bad_date):
    xml = _h8_xml().replace("2026-07-15", bad_date)
    result = FederalReserveH8Provider(client=_client(_archive(xml))).h8()

    assert not result.ok
    assert "observation" in result.error


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [
        ("BG", "CB"),
        ("CATEGORY", "A"),
        ("CURRENCY", "USD"),
        ("FREQ", "19"),
        ("H8_UNITS", "LEVEL"),
        ("ITEM", "1151"),
        ("SA", "SA"),
        ("UNIT", "Currency"),
        ("UNIT_MULT", "1000000"),
    ],
)
def test_h8_provider_rejects_semantic_dimension_drift(dimension, expected):
    xml = _h8_xml().replace(f'{dimension}="{expected}"', f'{dimension}="DRIFT"')
    result = FederalReserveH8Provider(client=_client(_archive(xml))).h8()

    assert not result.ok
    assert "semantic dimension drift" in result.error
    assert dimension in result.error


@pytest.mark.parametrize(
    "prepared_at",
    ["", "not-a-timestamp", "2026-07-01T00:00:00Z", "2999-01-01T00:00:00Z"],
)
def test_h8_provider_rejects_invalid_future_or_regressed_prepared_at(prepared_at):
    xml = _h8_xml().replace("2026-07-10T20:15:00Z", prepared_at)
    result = FederalReserveH8Provider(client=_client(_archive(xml))).h8()

    assert not result.ok
    assert "Prepared timestamp" in result.error


def test_h8_provider_accepts_release_lag_boundary_and_rejects_stalled_series():
    boundary_xml = (
        _h8_xml()
        .replace("2026-07-10T20:15:00Z", "2026-07-06T20:15:00Z")
        .replace("2026-07-08", "2026-06-24")
    )
    stalled_xml = boundary_xml.replace(
        "2026-07-06T20:15:00Z", "2026-07-07T20:15:00Z"
    )

    boundary = FederalReserveH8Provider(
        client=_client(_archive(boundary_xml))
    ).h8()
    stalled = FederalReserveH8Provider(
        client=_client(_archive(stalled_xml))
    ).h8()

    assert boundary.ok
    assert not stalled.ok
    assert "too old" in stalled.error
    assert "13 days" in stalled.error


@pytest.mark.django_db
def test_h8_store_preserves_archive_hash_and_weekly_usd_million_contract():
    result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    run = record_provider_result(result, persist=_store_h8_observations)

    assert run.status == "success"
    assert run.row_count == 1
    artifact = RawArtifact.objects.get(run=run)
    assert artifact.sha256 == result.metadata["archive_sha256"]
    observation = Observation.objects.get(
        series__key="h8-b1151ncba", batch_id=run.batch_id
    )
    assert observation.series.frequency == "weekly"
    assert observation.series.unit == "USD millions"
    assert observation.value == Decimal("25509970.7")
    assert observation.metadata["board_series_id"] == "B1151NCBA"
    assert observation.metadata["source_release_time"] == (
        "2026-07-10T20:15:00+00:00"
    )
    assert observation.metadata["release_freshness_days"] == 8


@pytest.mark.django_db
def test_h8_superseded_run_cannot_clobber_newer_persisted_batch():
    old_result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    old_run = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"fetched_at": old_result.fetched_at.isoformat()},
    )
    new_result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    new_run = record_provider_result(new_result, persist=_store_h8_observations)
    before_batches = set(
        Observation.objects.filter(series__key="h8-b1151ncba").values_list(
            "batch_id", flat=True
        )
    )
    before_artifacts = RawArtifact.objects.count()

    with transaction.atomic(), pytest.raises(ValueError, match="superseded h8"):
        _store_h8_observations(old_result, old_run.source, old_run)

    assert new_run.status == "success"
    assert before_batches == {new_run.batch_id}
    assert set(
        Observation.objects.filter(series__key="h8-b1151ncba").values_list(
            "batch_id", flat=True
        )
    ) == before_batches
    assert RawArtifact.objects.count() == before_artifacts
    assert _coordinate_reserves_dashboard([old_run]) == ([], set())


@pytest.mark.django_db
def test_h8_later_attempt_cannot_persist_regressed_release_time():
    baseline_result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    baseline_run = record_provider_result(
        baseline_result, persist=_store_h8_observations
    )
    regressed_xml = _h8_xml().replace(
        "2026-07-10T20:15:00Z", "2026-07-09T20:15:00Z"
    )
    regressed_result = FederalReserveH8Provider(
        client=_client(_archive(regressed_xml))
    ).h8()

    rejected_run = record_provider_result(
        regressed_result, persist=_store_h8_observations
    )

    assert baseline_run.status == "success"
    assert rejected_run.status == "failed"
    assert "release time regressed" in rejected_run.error
    assert set(
        Observation.objects.filter(series__key="h8-b1151ncba").values_list(
            "batch_id", flat=True
        )
    ) == {baseline_run.batch_id}
    assert RawArtifact.objects.filter(run=rejected_run).count() == 0


@pytest.mark.django_db
def test_h8_running_release_watermark_closes_finish_ingestion_toctou_gap():
    newer_result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    running_run = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"fetched_at": newer_result.fetched_at.isoformat()},
    )
    with transaction.atomic():
        _store_h8_observations(newer_result, running_run.source, running_run)
    running_run.refresh_from_db()
    assert running_run.status == "running"
    assert running_run.metadata["source_release_time"] == (
        "2026-07-10T20:15:00+00:00"
    )

    older_xml = _h8_xml().replace(
        "2026-07-10T20:15:00Z", "2026-07-09T20:15:00Z"
    )
    older_result = FederalReserveH8Provider(
        client=_client(_archive(older_xml))
    ).h8()
    latest_run = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"fetched_at": older_result.fetched_at.isoformat()},
    )
    before_artifacts = RawArtifact.objects.count()

    with transaction.atomic(), pytest.raises(
        ValueError, match="durable release watermark"
    ):
        _store_h8_observations(older_result, latest_run.source, latest_run)

    assert set(
        Observation.objects.filter(series__key="h8-b1151ncba").values_list(
            "batch_id", flat=True
        )
    ) == {running_run.batch_id}
    assert RawArtifact.objects.count() == before_artifacts
    latest_run.refresh_from_db()
    assert "source_release_time" not in latest_run.metadata


@pytest.mark.django_db
def test_h8_observation_release_watermark_survives_missing_legacy_run_metadata():
    newer_result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    running_run = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"fetched_at": newer_result.fetched_at.isoformat()},
    )
    with transaction.atomic():
        _store_h8_observations(newer_result, running_run.source, running_run)
    metadata = dict(running_run.metadata)
    metadata.pop("source_release_time", None)
    running_run.metadata = metadata
    running_run.save(update_fields=["metadata", "updated_at"])

    older_xml = _h8_xml().replace(
        "2026-07-10T20:15:00Z", "2026-07-09T20:15:00Z"
    )
    older_result = FederalReserveH8Provider(
        client=_client(_archive(older_xml))
    ).h8()
    latest_run = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"fetched_at": older_result.fetched_at.isoformat()},
    )

    with transaction.atomic(), pytest.raises(
        ValueError, match="durable release watermark"
    ):
        _store_h8_observations(older_result, latest_run.source, latest_run)

    assert set(
        Observation.objects.filter(series__key="h8-b1151ncba").values_list(
            "batch_id", flat=True
        )
    ) == {running_run.batch_id}


@pytest.mark.django_db
def test_h8_invalid_nonempty_run_release_watermark_fails_closed():
    previous = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"source_release_time": "not-a-timestamp"},
    )
    result = FederalReserveH8Provider(
        client=_client(_archive(_h8_xml()))
    ).h8()
    latest = begin_ingestion(
        "federal-reserve",
        "h8",
        metadata={"fetched_at": result.fetched_at.isoformat()},
    )

    with transaction.atomic(), pytest.raises(
        ValueError, match="invalid nonempty run"
    ):
        _store_h8_observations(result, latest.source, latest)

    assert previous.status == "running"
    assert not Observation.objects.filter(series__key="h8-b1151ncba").exists()
    assert not RawArtifact.objects.exists()


def _weekly_records(
    series_id: str,
    *,
    count: int = 60,
    end: date = LATEST_WEDNESDAY,
    proportional: bool = False,
) -> list[dict]:
    start = end - timedelta(weeks=count - 1)
    rows = []
    for index in range(count):
        period = start + timedelta(weeks=index)
        assets = Decimal("24000000") + Decimal(index * 32000)
        if series_id == "WRBWFRBL":
            value = (
                assets * Decimal("0.13")
                if proportional
                else Decimal("3000000")
                + Decimal(index * index * 137)
                + Decimal(index * 4100)
            )
            board_id = "RESH4R_N.WW"
        else:
            value = assets
            board_id = "B1151NCBA"
        rows.append(
            {
                "series_id": series_id,
                "source_series_id": board_id,
                "date": period.isoformat(),
                "value": value,
                "metadata": {
                    "board_series_id": board_id,
                    "raw_value": str(value),
                    "unit_multiplier": "1000000",
                    "currency": "USD",
                },
            }
        )
    return rows


def _record_reserves_runs(
    *,
    count: int = 60,
    end: date = LATEST_WEDNESDAY,
    h41_end: date | None = None,
    h8_end: date | None = None,
    proportional: bool = False,
    h41_metadata: dict | None = None,
    h8_metadata: dict | None = None,
):
    fetched_at = datetime(2026, 7, 10, 20, tzinfo=UTC)
    h41 = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="h41",
            fetched_at=fetched_at,
            records=_weekly_records(
                "WRBWFRBL",
                count=count,
                end=h41_end or end,
                proportional=proportional,
            ),
            metadata={
                "reserves_refresh_id": "h41-fixture",
                "prepared_at": "2026-07-10T19:00:00+00:00",
                **(h41_metadata or {}),
            },
        ),
        persist=_store_h41_observations,
    )
    h8 = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="h8",
            fetched_at=fetched_at,
            records=_weekly_records(
                "H8-B1151NCBA",
                count=count,
                end=h8_end or end,
                proportional=proportional,
            ),
            metadata={
                "reserves_refresh_id": "h8-fixture",
                "prepared_at": "2026-07-10T19:30:00+00:00",
                **(h8_metadata or {}),
            },
        ),
        persist=_store_h8_observations,
    )
    return {"h41": h41, "h8": h8}


def _delete_common_period(runs, period: date) -> None:
    value_date = datetime.combine(period, datetime.min.time(), tzinfo=UTC)
    for run in runs.values():
        Observation.objects.filter(
            batch_id=run.batch_id,
            value_date=value_date,
        ).delete()


@pytest.fixture(autouse=True)
def _fixed_official_now(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)


@pytest.mark.django_db
def test_reserves_v1_publishes_two_exact_batches_and_recomputable_statistics():
    runs = _record_reserves_runs()
    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])

    assert stale == set()
    assert [item.key for item in dashboards] == ["reserves"]
    snapshot = DashboardSnapshot.objects.get(key="reserves")
    data = snapshot.data
    assert data["contract_version"] == RESERVES_CONTRACT_VERSION
    assert data["common_effective_date"] == LATEST_WEDNESDAY.isoformat()
    assert set(data["component_batches"]) == {
        str(runs["h41"].batch_id),
        str(runs["h8"].batch_id),
    }
    metrics = {item["key"]: item for item in data["metrics"]}
    assert set(metrics) == set(RESERVES_REQUIRED_METRIC_KEYS)
    assert {item["key"] for item in data["charts"]} == set(
        RESERVES_REQUIRED_CHART_KEYS
    )

    reserve = Decimal(str(metrics["reserve-balances"]["value"]))
    assets = Decimal(str(metrics["commercial-bank-assets"]["value"]))
    ratio = Decimal(
        str(metrics["reserve-commercial-bank-assets-ratio"]["value"])
    )
    assert reserve > Decimal("3")
    assert assets > Decimal("24")
    assert ratio.quantize(Decimal("0.000001")) == (
        Decimal("100") * reserve / assets
    ).quantize(Decimal("0.000001"))

    change_metric = metrics["reserve-ratio-8w-change"]
    current_inputs = change_metric["metadata"]["input_lineage"]
    lagged_inputs = change_metric["metadata"]["previous_input_lineage"]
    current_ratio = (
        Decimal("100")
        * Decimal(current_inputs[0]["value_usd_tn"])
        / Decimal(current_inputs[1]["value_usd_tn"])
    )
    lagged_ratio = (
        Decimal("100")
        * Decimal(lagged_inputs[0]["value_usd_tn"])
        / Decimal(lagged_inputs[1]["value_usd_tn"])
    )
    assert Decimal(str(change_metric["value"])).quantize(
        Decimal("0.000001")
    ) == (current_ratio - lagged_ratio).quantize(Decimal("0.000001"))
    assert change_metric["metadata"]["formula"] == (
        "ratio(t) - ratio(t-56 calendar days)"
    )
    assert change_metric["metadata"]["lag_calendar_days"] == 56
    assert (
        date.fromisoformat(current_inputs[0]["value_date"][:10])
        - date.fromisoformat(lagged_inputs[0]["value_date"][:10])
        == timedelta(days=56)
    )

    zscore = metrics["reserve-ratio-8w-zscore"]
    sample = zscore["metadata"]["sample_changes"]
    sample_values = [Decimal(item["value"]) for item in sample]
    mean = sum(sample_values) / Decimal(len(sample_values))
    std = (
        sum((value - mean) ** 2 for value in sample_values)
        / Decimal(len(sample_values))
    ).sqrt()
    expected_zscore = (Decimal(str(change_metric["value"])) - mean) / std
    assert len(sample) == 52
    assert zscore["metadata"]["sample_window"] == (
        "Recent three-year window: minimum 52 and maximum 156 strict "
        "56-calendar-day change samples."
    )
    assert zscore["metadata"]["minimum_sample_count"] == 52
    assert zscore["metadata"]["maximum_sample_count"] == 156
    assert zscore["metadata"]["population_standard_deviation"] is True
    assert Decimal(str(zscore["value"])).quantize(
        Decimal("0.000001")
    ) == expected_zscore.quantize(Decimal("0.000001"))
    assert _reserves_page_contract_is_buildable(
        data["metrics"], data["charts"], data
    )

    for chart in data["charts"]:
        assert set(chart["batch_ids"]) == set(data["component_batches"])
        assert chart["as_of"]
        assert chart["fetched_at"]
        assert chart["fresh_until"]
        assert chart["quality_status"] in {"fresh", "estimated"}
        assert chart["frequency"] == "weekly"
        assert chart["time_axis"] == "date"
        assert chart["license_scopes"]
        assert chart["fallback_sources"] == []
        for row in chart["data"]:
            numeric_fields = {
                key for key in row if key not in {"date", "_lineage", "_source_keys"}
            }
            assert numeric_fields == set(row["_lineage"])
            assert all(
                lineage["batch_id"] and lineage["license_scope"]
                for lineage in row["_lineage"].values()
            )

    stored_zscore = MetricSnapshot.objects.get(
        key="reserves-reserve-ratio-8w-zscore", batch_id=snapshot.batch_id
    )
    assert stored_zscore.metadata["sample_count"] == 52
    assert stored_zscore.metadata["sample_changes"] == sample


@pytest.mark.django_db
def test_reserves_generic_publication_cannot_publish_legacy_single_source_snapshot():
    runs = _record_reserves_runs()

    assert "reserves" not in H41_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"reserves"}) == []
    assert publish_official_dashboards() == [] or not DashboardSnapshot.objects.filter(
        key="reserves"
    ).exists()
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()

    _coordinate_reserves_dashboard([runs["h8"]])
    assert DashboardSnapshot.objects.filter(
        key="reserves", data__contract_version=1
    ).count() == 1


@pytest.mark.django_db
def test_reserves_route_hides_legacy_snapshot_without_v1_contract(client):
    DashboardSnapshot.objects.create(
        key="reserves",
        title="legacy reserves",
        as_of=FIXED_NOW,
        quality_status=Observation.Quality.FRESH,
        summary="legacy single-source snapshot",
        data={
            "demo": False,
            "metrics": [
                {
                    "key": "wrbwfrbl",
                    "label": "legacy reserve status",
                    "value": 999.99,
                }
            ],
            "source_keys": ["internal"],
        },
        source=ensure_source("internal"),
        is_published=True,
    )

    response = client.get("/liquidity/reserves/")
    content = response.content.decode()

    assert response.status_code == 200
    assert not any(
        context.get("snapshot") is not None
        for context in response.context
        if "snapshot" in context
    )
    assert "999.99" not in content
    assert "legacy reserve status" not in content
    assert "本页尚无通过来源许可与质量检查的可发布快照" in content


@pytest.mark.django_db
def test_reserves_route_hides_malformed_v1_admin_snapshot(client):
    snapshot = DashboardSnapshot.objects.create(
        key="reserves",
        title="malformed v1",
        as_of=FIXED_NOW,
        quality_status=Observation.Quality.FRESH,
        summary="MALFORMED-V1-MARKER",
        data={
            "contract_version": 1,
            "publication_batch_id": "not-the-snapshot-batch",
            "component_batches": ["one", "two"],
            "source_keys": ["federal-reserve", "internal"],
            "metrics": [],
            "charts": [],
        },
        source=ensure_source("internal"),
        is_published=True,
    )
    assert snapshot.data["contract_version"] == 1

    response = client.get("/liquidity/reserves/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "MALFORMED-V1-MARKER" not in content
    assert "本页尚无通过来源许可与质量检查的可发布快照" in content


@pytest.mark.django_db
def test_reserves_route_renders_retained_stale_v1_lineage_and_failure(client):
    runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([runs["h8"]])
    failed = record_provider_result(
        ProviderResult.failure("federal-reserve", "h8", "H8-UPSTREAM-DOWN")
    )
    _coordinate_reserves_dashboard([failed])

    response = client.get("/liquidity/reserves/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "H8-UPSTREAM-DOWN" in content
    assert "federal-reserve" in content
    assert "许可：" in content
    assert "许可：—" not in content
    assert "fallback：无" in content


@pytest.mark.django_db
def test_reserves_same_content_refresh_is_idempotent_and_clears_failure():
    runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([runs["h8"]])
    first = DashboardSnapshot.objects.get(key="reserves")

    failed = record_provider_result(
        ProviderResult.failure("federal-reserve", "h8", "temporary failure")
    )
    dashboards, stale = _coordinate_reserves_dashboard([failed])
    first.refresh_from_db()
    assert dashboards == []
    assert stale == {"reserves"}
    assert first.quality_status == Observation.Quality.STALE
    assert any(
        item["component"] == "h8"
        for item in first.data["refresh_failure"]["components"]
    )

    recovered = _record_reserves_runs()
    dashboards, stale = _coordinate_reserves_dashboard([recovered["h8"]])
    first.refresh_from_db()
    assert dashboards == []
    assert stale == set()
    assert DashboardSnapshot.objects.filter(key="reserves").count() == 1
    assert first.quality_status == Observation.Quality.ESTIMATED
    assert "refresh_failure" not in first.data
    assert set(first.data["component_batches"]) == {
        str(recovered["h41"].batch_id),
        str(recovered["h8"].batch_id),
    }


@pytest.mark.django_db
def test_reserves_superseded_trigger_replay_is_a_pure_noop(monkeypatch):
    original_runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([original_runs["h8"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves")
    before_data = deepcopy(snapshot.data)
    before_quality = snapshot.quality_status
    before_updated_at = snapshot.updated_at

    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: FIXED_NOW + timedelta(hours=1),
    )
    _record_reserves_runs()
    dashboards, stale = _coordinate_reserves_dashboard([original_runs["h8"]])

    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == set()
    assert snapshot.data == before_data
    assert snapshot.quality_status == before_quality
    assert snapshot.updated_at == before_updated_at


@pytest.mark.django_db
def test_reserves_duplicate_trigger_remains_a_refresh_failure():
    runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([runs["h8"]])

    dashboards, stale = _coordinate_reserves_dashboard(
        [runs["h8"], runs["h8"]]
    )

    snapshot = DashboardSnapshot.objects.get(key="reserves")
    assert dashboards == []
    assert stale == {"reserves"}
    assert snapshot.quality_status == Observation.Quality.STALE
    assert "duplicate-trigger" in str(snapshot.data["refresh_failure"])


@pytest.mark.django_db
@pytest.mark.parametrize("failure_kind", ["failed", "partial", "empty"])
def test_reserves_latest_incomplete_attempt_retains_previous_snapshot(failure_kind):
    runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([runs["h8"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves")

    if failure_kind == "failed":
        trigger = record_provider_result(
            ProviderResult.failure("federal-reserve", "h8", "archive unavailable")
        )
    elif failure_kind == "partial":
        trigger = record_provider_result(
            ProviderResult(
                provider="federal-reserve",
                dataset="h8",
                records=_weekly_records("H8-B1151NCBA"),
                metadata={"missing_series": ["B1151NCBA"]},
            ),
            persist=_store_h8_observations,
        )
    else:
        trigger = record_provider_result(
            ProviderResult(provider="federal-reserve", dataset="h8", records=[]),
            persist=_store_h8_observations,
        )

    dashboards, stale = _coordinate_reserves_dashboard([trigger])
    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == {"reserves"}
    assert DashboardSnapshot.objects.filter(key="reserves").count() == 1
    assert snapshot.quality_status == Observation.Quality.STALE
    assert snapshot.data["refresh_failure"]["components"]


@pytest.mark.django_db
def test_reserves_rejects_stale_fallback_and_revoked_licence():
    runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([runs["h8"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves")
    fallback = ensure_source("internal")
    latest_h8 = Observation.objects.filter(batch_id=runs["h8"].batch_id).latest(
        "value_date"
    )
    latest_h8.fallback_source = fallback
    latest_h8.quality_status = Observation.Quality.FALLBACK
    latest_h8.save(update_fields=["fallback_source", "quality_status", "updated_at"])

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])
    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == {"reserves"}
    assert snapshot.quality_status == Observation.Quality.STALE

    latest_h8.fallback_source = None
    latest_h8.quality_status = Observation.Quality.FRESH
    latest_h8.save(update_fields=["fallback_source", "quality_status", "updated_at"])
    licence = SourceLicense.objects.get(
        source__key="federal-reserve", is_current=True
    )
    licence.status = "restricted"
    licence.public_display_allowed = False
    licence.derived_display_allowed = False
    licence.save(
        update_fields=[
            "status",
            "public_display_allowed",
            "derived_display_allowed",
            "updated_at",
        ]
    )

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])
    assert dashboards == []
    assert stale == {"reserves"}
    assert DashboardSnapshot.objects.filter(key="reserves").count() == 1


@pytest.mark.django_db
def test_reserves_rejects_insufficient_sample_and_zero_variance():
    insufficient = _record_reserves_runs(count=59)
    dashboards, stale = _coordinate_reserves_dashboard([insufficient["h8"]])
    assert dashboards == []
    assert stale == {"reserves"}
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()

    constant = _record_reserves_runs(proportional=True)
    dashboards, stale = _coordinate_reserves_dashboard([constant["h8"]])
    assert dashboards == []
    assert stale == {"reserves"}
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()


@pytest.mark.django_db
def test_reserves_8w_change_uses_exact_calendar_lag_across_a_missing_week():
    runs = _record_reserves_runs(count=70)
    missing_period = LATEST_WEDNESDAY - timedelta(weeks=4)
    _delete_common_period(runs, missing_period)

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])

    assert stale == set()
    snapshot = dashboards[0]
    metrics = {item["key"]: item for item in snapshot.data["metrics"]}
    change = metrics["reserve-ratio-8w-change"]
    current_date = date.fromisoformat(change["value_date"][:10])
    previous_date = date.fromisoformat(
        change["metadata"]["previous_input_lineage"][0]["value_date"][:10]
    )
    assert current_date - previous_date == timedelta(days=56)
    assert previous_date == LATEST_WEDNESDAY - timedelta(days=56)

    sample = metrics["reserve-ratio-8w-zscore"]["metadata"]["sample_changes"]
    assert all(
        date.fromisoformat(item["value_date"])
        - date.fromisoformat(item["lagged_value_date"])
        == timedelta(days=56)
        for item in sample
    )
    ratio_chart = next(
        item
        for item in snapshot.data["charts"]
        if item["key"] == "reserve-ratio-history"
    )
    for row in ratio_chart["data"]:
        if "8-week change" not in row:
            continue
        lineage = row["_lineage"]["8-week change"]
        chart_current = date.fromisoformat(
            lineage["input_lineage"][0]["value_date"][:10]
        )
        chart_previous = date.fromisoformat(
            lineage["previous_input_lineage"][0]["value_date"][:10]
        )
        assert chart_current - chart_previous == timedelta(days=56)


@pytest.mark.django_db
def test_reserves_fails_closed_when_current_exact_56_day_lag_is_missing():
    baseline = _record_reserves_runs(count=70)
    _coordinate_reserves_dashboard([baseline["h8"]])
    snapshot = DashboardSnapshot.objects.get(key="reserves")

    candidate = _record_reserves_runs(count=70)
    _delete_common_period(
        candidate, LATEST_WEDNESDAY - timedelta(days=56)
    )
    dashboards, stale = _coordinate_reserves_dashboard([candidate["h8"]])

    snapshot.refresh_from_db()
    assert dashboards == []
    assert stale == {"reserves"}
    assert snapshot.quality_status == Observation.Quality.STALE
    assert "exact 56-calendar-day input pair" in str(
        snapshot.data["refresh_failure"]
    )


@pytest.mark.django_db
def test_reserves_uses_exact_batch_release_time_when_latest_dates_differ(
    monkeypatch,
):
    now = datetime(2026, 7, 14, 12, tzinfo=UTC)
    monkeypatch.setattr("research.official_data.timezone.now", lambda: now)
    runs = _record_reserves_runs(
        count=61,
        h41_end=LATEST_WEDNESDAY,
        h8_end=LATEST_WEDNESDAY - timedelta(weeks=1),
        h41_metadata={"prepared_at": "2026-07-09T12:00:00+00:00"},
        h8_metadata={"prepared_at": "2026-07-10T10:00:00+00:00"},
    )

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])

    assert stale == set()
    assert dashboards[0].data["common_effective_date"] == "2026-07-01"
    h41_common = Observation.objects.get(
        batch_id=runs["h41"].batch_id,
        series__key="wrbwfrbl",
        value_date=datetime(2026, 7, 1, tzinfo=UTC),
    )
    h8_latest = Observation.objects.get(
        batch_id=runs["h8"].batch_id,
        series__key="h8-b1151ncba",
        value_date=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert _fresh_until(h41_common) == datetime(
        2026, 7, 17, 12, tzinfo=UTC
    )
    assert _fresh_until(h8_latest) == datetime(
        2026, 7, 18, 10, tzinfo=UTC
    )
    assert now < _fresh_until(h41_common)
    assert now < _fresh_until(h8_latest)

    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: datetime(2026, 7, 18, 10, 1, tzinfo=UTC),
    )
    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])
    assert dashboards == []
    assert stale == {"reserves"}


@pytest.mark.django_db
def test_reserves_rejects_legacy_batch_with_stalled_latest_wrbwfrbl():
    runs = _record_reserves_runs(count=61)
    Observation.objects.filter(
        batch_id=runs["h41"].batch_id,
        series__key="wrbwfrbl",
        value_date=datetime.combine(
            LATEST_WEDNESDAY, datetime.min.time(), tzinfo=UTC
        ),
    ).delete()

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])

    assert dashboards == []
    assert stale == {"reserves"}
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("component", ["h41", "h8"])
def test_reserves_invalid_future_prepared_at_fails_closed(component):
    kwargs = {
        f"{component}_metadata": {
            "prepared_at": "2999-01-01T00:00:00+00:00"
        }
    }
    runs = _record_reserves_runs(**kwargs)

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])

    assert runs[component].status == "failed"
    assert dashboards == []
    assert stale == {"reserves"}
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()

@pytest.mark.django_db
def test_reserves_rejects_candidate_date_regression_and_rolls_back_bad_postcondition(
    monkeypatch,
):
    runs = _record_reserves_runs()
    _coordinate_reserves_dashboard([runs["h8"]])
    original = DashboardSnapshot.objects.get(key="reserves")

    regressed = _record_reserves_runs(end=LATEST_WEDNESDAY - timedelta(weeks=1))
    dashboards, stale = _coordinate_reserves_dashboard([regressed["h8"]])
    original.refresh_from_db()
    assert dashboards == []
    assert stale == {"reserves"}
    assert DashboardSnapshot.objects.filter(key="reserves").count() == 1
    assert original.data["common_effective_date"] == LATEST_WEDNESDAY.isoformat()

    newer = _record_reserves_runs()
    monkeypatch.setattr(
        "research.official_data._reserves_snapshot_contract_is_valid",
        lambda *args, **kwargs: False,
    )
    dashboards, stale = _coordinate_reserves_dashboard([newer["h8"]])
    assert dashboards == []
    assert stale == {"reserves"}
    assert DashboardSnapshot.objects.filter(key="reserves").count() == 1


@pytest.mark.django_db
def test_reserves_rejects_future_fetched_numeric_observation():
    runs = _record_reserves_runs()
    future = LATEST_WEDNESDAY + timedelta(weeks=1)
    for series_key, run in (
        ("wrbwfrbl", runs["h41"]),
        ("h8-b1151ncba", runs["h8"]),
    ):
        series = Observation.objects.filter(series__key=series_key).first().series
        Observation.objects.create(
            series=series,
            value=Decimal("99999999"),
            value_date=datetime.combine(future, datetime.min.time(), tzinfo=UTC),
            as_of=datetime.combine(future, datetime.min.time(), tzinfo=UTC),
            fetched_at=FIXED_NOW + timedelta(minutes=1),
            batch_id=run.batch_id,
            source=run.source,
            quality_status=Observation.Quality.FRESH,
            metadata={"unit_multiplier": "1000000"},
        )

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])
    assert dashboards == []
    assert stale == {"reserves"}
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()


@pytest.mark.django_db
def test_reserves_rejects_future_run_retrieval_time():
    runs = _record_reserves_runs()
    metadata = dict(runs["h8"].metadata)
    metadata["fetched_at"] = (FIXED_NOW + timedelta(minutes=1)).isoformat()
    runs["h8"].metadata = metadata
    runs["h8"].save(update_fields=["metadata", "updated_at"])

    dashboards, stale = _coordinate_reserves_dashboard([runs["h8"]])

    assert dashboards == []
    assert stale == {"reserves"}
    assert not DashboardSnapshot.objects.filter(key="reserves").exists()


def test_h8_task_and_weekly_beat_are_wired(monkeypatch):
    expected = {
        "runs": [{"source": "federal-reserve", "dataset": "h8"}],
        "dashboard_keys": ["reserves"],
        "stale_dashboard_keys": [],
    }
    monkeypatch.setattr("research.tasks.refresh_h8_data", lambda: expected)

    assert refresh_h8_sources.run() == expected
    beat = settings.CELERY_BEAT_SCHEDULE["refresh-h8-weekly"]
    assert beat["task"] == "research.tasks.refresh_h8_sources"
    assert str(beat["schedule"]) == "<crontab: 20 6 * * sat (m/h/dM/MY/d)>"


def test_h8_task_raises_when_required_reserves_publication_is_stale(monkeypatch):
    summary = {
        "runs": [{"source": "federal-reserve", "dataset": "h8"}],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["reserves"],
    }
    monkeypatch.setattr("research.tasks.refresh_h8_data", lambda: summary)

    with pytest.raises(RuntimeError, match="required reserves v1"):
        refresh_h8_sources.run()


def test_reserves_catalog_tracks_live_rate_spreads_and_method_gaps():
    requirements = {item["key"]: item for item in DATA_REQUIREMENTS}

    assert requirements["fed-reserve-balances"]["status"] == "live"
    assert requirements["fed-h8-commercial-bank-assets"]["status"] == "live"
    assert requirements["reserves-coverage-proxy"]["status"] == "live"
    assert (
        requirements["reserves-like-for-like-adequacy-method"]["status"]
        == "needs_source"
    )
    rate_gap = requirements["reserves-sofr-tbill-spreads"]
    assert rate_gap["status"] == "live"
    assert "13-week T-bill" in rate_gap["metric_name"]
    assert "SOFR−IORB" in rate_gap["metric_name"]
    assert (
        "treasury-bill-rates:13w-coupon-equivalent" in rate_gap["source_name"]
    )
    assert (
        requirements["reserves-intermediation-status-method"]["status"]
        == "needs_source"
    )
