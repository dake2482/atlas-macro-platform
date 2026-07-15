from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from django.conf import settings

from research.data_catalog import DATA_REQUIREMENTS
from research.fed_h10 import (
    H10_SOURCE_ATTRIBUTES,
    H10_TARGET_SERIES,
    FederalReserveH10Provider,
)
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    Instrument,
    MetricSnapshot,
    Observation,
    SourceLicense,
)
from research.official_data import (
    ASSETS_FX_CONTRACT_VERSION,
    ASSETS_FX_FORMULA_VERSION,
    ASSETS_FX_REQUIRED_CHART_KEYS,
    ASSETS_FX_REQUIRED_METRIC_KEYS,
    ASSETS_FX_REQUIRED_SECTION_KEYS,
    INDEPENDENT_PUBLICATION_KEYS,
    _assets_fx_content_fingerprint,
    _assets_fx_page_data,
    _assets_fx_payload_integrity_hash,
    _coordinate_assets_fx_dashboard,
    _publish_dashboard,
    _publish_dashboard_core,
    _store_h10_observations,
    assets_fx_snapshot_is_publicly_displayable,
    publish_official_dashboards,
    select_public_assets_fx_snapshot,
)
from research.providers import ProviderResult
from research.services import (
    begin_ingestion,
    ensure_source,
    finish_ingestion,
    record_provider_result,
)


def _dates(count: int = 300, *, end: date = date(2026, 7, 10)) -> tuple[str, ...]:
    start = end - timedelta(days=count - 1)
    return tuple((start + timedelta(days=index)).isoformat() for index in range(count))


def _h10_archive(
    *,
    dates: tuple[str, ...] | None = None,
    prepared_at: str = "2026-07-13T17:45:44",
) -> bytes:
    periods = dates or _dates()
    starts = {
        "JRXWTFB_N.B": Decimal("115"),
        "RXI$US_N.B.EU": Decimal("1.05"),
        "RXI_N.B.CH": Decimal("7.00"),
        "RXI_N.B.JA": Decimal("140"),
    }
    steps = {
        "JRXWTFB_N.B": Decimal("0.01"),
        "RXI$US_N.B.EU": Decimal("0.0001"),
        "RXI_N.B.CH": Decimal("0.001"),
        "RXI_N.B.JA": Decimal("0.02"),
    }
    series_blocks = []
    for board_id, target in H10_TARGET_SERIES.items():
        attrs = H10_SOURCE_ATTRIBUTES[board_id]
        observations = "".join(
            (
                '<frb:Obs OBS_STATUS="A" '
                f'OBS_VALUE="{starts[board_id] + steps[board_id] * index}" '
                f'TIME_PERIOD="{period}" />'
            )
            for index, period in enumerate(periods)
        )
        series_blocks.append(
            f"""
            <kf:Series SERIES_NAME="{board_id}" FREQ="{attrs['FREQ']}"
                CURRENCY="{attrs['CURRENCY']}" FX="{attrs['FX']}"
                UNIT="{attrs['UNIT']}" UNIT_MULT="{attrs['UNIT_MULT']}">
              <frb:Annotations><common:Annotation>
                <common:AnnotationText>{target['name']}</common:AnnotationText>
              </common:Annotation></frb:Annotations>
              {observations}
            </kf:Series>
            """
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <message:MessageGroup
      xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
      xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
      xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
      xmlns:kf="http://www.federalreserve.gov/structure/compact/H10_H10">
      <message:Header><message:Prepared>{prepared_at}</message:Prepared></message:Header>
      <frb:DataSet>{''.join(series_blocks)}</frb:DataSet>
    </message:MessageGroup>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("H10_data.xml", xml)
    return buffer.getvalue()


def _h10_result(*, raw_bytes: bytes | None = None) -> ProviderResult:
    raw = raw_bytes or _h10_archive()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datadownload/Output.aspx"
        return httpx.Response(200, content=raw)

    client = httpx.Client(
        base_url="https://www.federalreserve.example.test",
        transport=httpx.MockTransport(handler),
    )
    return FederalReserveH10Provider(client=client).h10()


def _rendered_chart_rows(response) -> list[dict]:
    chart = response.context["charts"][0]
    dom_id = str(chart["dom_id"])
    match = re.search(
        rf'<script id="{re.escape(dom_id)}" type="application/json">(.*?)</script>',
        response.content.decode(),
        re.DOTALL,
    )
    assert match is not None
    rows = json.loads(match.group(1))
    assert isinstance(rows, list)
    return rows


@pytest.fixture
def published_assets_fx(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    result = _h10_result()
    assert result.ok
    run = record_provider_result(result, persist=_store_h10_observations)
    assert run.status == IngestionRun.Status.SUCCESS, run.error
    dashboards, stale = _coordinate_assets_fx_dashboard([run])
    assert stale == set(), _assets_fx_page_data(run)[1]
    assert len(dashboards) == 1
    return dashboards[0], run, tmp_path, result.raw_bytes


@pytest.mark.django_db
def test_assets_fx_v1_publishes_exact_4_2_3_contract(published_assets_fx):
    snapshot, run, _root, _raw = published_assets_fx
    data = snapshot.data
    assert data["contract_version"] == ASSETS_FX_CONTRACT_VERSION
    assert data["formula_version"] == ASSETS_FX_FORMULA_VERSION
    assert {item["key"] for item in data["metrics"]} == set(
        ASSETS_FX_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in data["charts"]} == set(
        ASSETS_FX_REQUIRED_CHART_KEYS
    )
    assert {item["key"] for item in data["sections"]} == set(
        ASSETS_FX_REQUIRED_SECTION_KEYS
    )
    charts = {item["key"]: item for item in data["charts"]}
    sections = {item["key"]: item for item in data["sections"]}
    assert len(charts["fx-broad-dollar-history"]["data"]) == 260
    assert len(
        charts["fx-major-reference-rates-usd-strength-rebased"]["data"]
    ) == 120
    assert len(sections["recent-h10-reference-observations"]["rows"]) == 20
    assert len(sections["source-freshness-methodology"]["rows"]) == 1
    assert len(sections["licensed-fx-market-gaps"]["rows"]) == 6
    assert MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).count() == 4
    assert snapshot.as_of == min(
        datetime.fromisoformat(metric["value_date"])
        for metric in data["metrics"]
    )
    assert data["component_batches"] == [str(run.batch_id)]
    assert assets_fx_snapshot_is_publicly_displayable(snapshot)
    assert select_public_assets_fx_snapshot([snapshot]) == snapshot
    assert snapshot.assets_fx_state == "current_candidate"


@pytest.mark.django_db
def test_metric_level_and_change_semantics_are_exact(published_assets_fx):
    snapshot, _run, _root, _raw = published_assets_fx
    for metric in snapshot.data["metrics"]:
        metadata = metric["metadata"]
        expected = Decimal("100") * (
            Decimal(metadata["current_value"])
            / Decimal(metadata["previous_value"])
            - Decimal("1")
        )
        assert Decimal(str(metric["change"])).quantize(
            Decimal("0.000001")
        ) == expected.quantize(Decimal("0.000001"))
        assert metric["source_key"] == "federal-reserve"
        assert metric["quality_status"] == "fresh"
        assert set(metric["source_keys"]) == {"federal-reserve", "internal"}
        assert metadata["change_quality_status"] == "estimated"
        assert metadata["change_calculation_owner"] == "Atlas Macro"
        row = MetricSnapshot.objects.get(
            batch_id=snapshot.batch_id,
            key=f"assets-fx-{metric['key']}",
        )
        assert row.source.key == "federal-reserve"
        assert row.quality_status == "fresh"
        assert row.change is not None


@pytest.mark.django_db
def test_generic_publication_bypasses_all_reject_assets_fx(published_assets_fx):
    snapshot, _run, _root, _raw = published_assets_fx
    assert "assets-fx" in INDEPENDENT_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"assets-fx"}) == []
    assert all(item.key != "assets-fx" for item in publish_official_dashboards())
    kwargs = {
        "key": "assets-fx",
        "title": "forged",
        "summary": "forged",
        "metrics": [],
        "batch_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError, match="dedicated v1 publisher"):
        _publish_dashboard(**kwargs)
    with pytest.raises(ValueError, match="dedicated v1 publisher"):
        _publish_dashboard_core(**kwargs)
    assert DashboardSnapshot.objects.filter(key="assets-fx").count() == 1
    assert select_public_assets_fx_snapshot([snapshot]) == snapshot


@pytest.mark.django_db
def test_new_same_value_h10_run_is_append_only_revision(published_assets_fx):
    first, old_run, _root, raw = published_assets_fx
    replacement = record_provider_result(
        _h10_result(raw_bytes=raw),
        persist=_store_h10_observations,
    )
    assert replacement.status == IngestionRun.Status.SUCCESS, replacement.error
    assert Observation.objects.filter(batch_id=old_run.batch_id).count() == old_run.row_count
    assert (
        Observation.objects.filter(batch_id=replacement.batch_id).count()
        == replacement.row_count
    )
    dashboards, stale = _coordinate_assets_fx_dashboard([replacement])
    assert stale == set()
    assert len(dashboards) == 1
    second = dashboards[0]
    assert second.pk != first.pk
    assert second.batch_id != first.batch_id
    assert second.data["fingerprint"] == first.data["fingerprint"]
    assert second.data["payload_integrity_hash"] != first.data["payload_integrity_hash"]
    assert DashboardSnapshot.objects.filter(key="assets-fx").count() == 2
    repeated, stale = _coordinate_assets_fx_dashboard([replacement])
    assert repeated == []
    assert stale == set()

    newer = begin_ingestion("federal-reserve", "h10")
    before_dashboards = list(
        DashboardSnapshot.objects.filter(key="assets-fx")
        .order_by("pk")
        .values_list("pk", "quality_status", "data")
    )
    before_metrics = MetricSnapshot.objects.count()
    replayed, replay_stale = _coordinate_assets_fx_dashboard([replacement])
    assert newer.status == IngestionRun.Status.RUNNING
    assert replayed == []
    assert replay_stale == set()
    assert before_dashboards == list(
        DashboardSnapshot.objects.filter(key="assets-fx")
        .order_by("pk")
        .values_list("pk", "quality_status", "data")
    )
    assert MetricSnapshot.objects.count() == before_metrics


@pytest.mark.django_db
def test_selector_rejects_observation_metric_payload_and_license_tamper(
    published_assets_fx,
):
    snapshot, run, _root, _raw = published_assets_fx
    observation = Observation.objects.filter(batch_id=run.batch_id).first()
    assert observation is not None
    original_value = observation.value
    observation.value += Decimal("1")
    observation.save(update_fields=["value", "updated_at"])
    assert select_public_assets_fx_snapshot([snapshot]) is None
    observation.value = original_value
    observation.save(update_fields=["value", "updated_at"])

    normalized = MetricSnapshot.objects.get(
        batch_id=snapshot.batch_id,
        key="assets-fx-h10-eurusd",
    )
    normalized.display_value = "forged"
    normalized.save(update_fields=["display_value", "updated_at"])
    assert select_public_assets_fx_snapshot([snapshot]) is None

    normalized.display_value = next(
        item["display_value"]
        for item in snapshot.data["metrics"]
        if item["key"] == "h10-eurusd"
    )
    normalized.save(update_fields=["display_value", "updated_at"])
    original_data = deepcopy(snapshot.data)
    tampered = deepcopy(original_data)
    tampered["sections"][2]["rows"][0]["lineage"]["numeric_value"] = 7
    tampered["fingerprint"] = _assets_fx_content_fingerprint(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=tampered,
    )
    tampered["payload_integrity_hash"] = _assets_fx_payload_integrity_hash(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=tampered,
    )
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_assets_fx_snapshot([snapshot]) is None

    snapshot.data = original_data
    snapshot.save(update_fields=["data", "updated_at"])
    licence = SourceLicense.objects.get(
        source__key="federal-reserve", is_current=True
    )
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])
    assert select_public_assets_fx_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_running_failure_and_natural_expiry_use_exact_states(
    published_assets_fx,
    client,
):
    snapshot, _run, _root, _raw = published_assets_fx
    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "upstream timeout"),
        persist=_store_h10_observations,
    )
    assert _coordinate_assets_fx_dashboard([failure]) == ([], {"assets-fx"})
    snapshot.refresh_from_db()
    stored_marker = deepcopy(snapshot.data["refresh_failure"])

    running = begin_ingestion("federal-reserve", "h10")
    transition = select_public_assets_fx_snapshot([snapshot])
    assert transition == snapshot
    assert transition.assets_fx_state == "transition_pending"
    assert "refresh_failure" not in transition.data
    assert snapshot.data["refresh_failure"] == stored_marker

    response = client.get("/assets/fx/")
    assert response.status_code == 200
    assert response.context["refresh_failure"] is None
    assert stored_marker["reason"] not in response.content.decode()

    dashboards, stale = _coordinate_assets_fx_dashboard([running])
    assert dashboards == []
    assert stale == {"assets-fx"}
    persisted = DashboardSnapshot.objects.get(pk=snapshot.pk)
    assert persisted.data["refresh_failure"] == stored_marker


@pytest.mark.django_db
def test_terminal_failure_binds_retained_marker(published_assets_fx, client):
    snapshot, _run, _root, _raw = published_assets_fx
    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "upstream timeout"),
        persist=_store_h10_observations,
    )
    dashboards, stale = _coordinate_assets_fx_dashboard([failure])
    assert dashboards == []
    assert stale == {"assets-fx"}
    snapshot.refresh_from_db()
    assert snapshot.data["refresh_failure"]["attempt"]["ingestion_run_id"] == failure.pk
    selected = select_public_assets_fx_snapshot([snapshot])
    assert selected == snapshot
    assert selected.assets_fx_state == "retained_failure"
    marker = selected.data["refresh_failure"]
    response = client.get("/assets/fx/")
    assert response.status_code == 200
    assert response.context["refresh_failure"] == marker
    assert marker["reason"] in response.content.decode()


@pytest.mark.django_db
def test_forged_historical_and_quality_poison_never_supply_page_failure(
    published_assets_fx,
    client,
):
    snapshot, _run, _root, _raw = published_assets_fx
    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "terminal failure"),
        persist=_store_h10_observations,
    )
    assert _coordinate_assets_fx_dashboard([failure]) == ([], {"assets-fx"})
    snapshot.refresh_from_db()
    valid_data = deepcopy(snapshot.data)
    valid_marker = deepcopy(valid_data["refresh_failure"])

    forged = deepcopy(valid_data)
    forged["refresh_failure"]["reason"] = "FORGED_MARKER_DO_NOT_RENDER"
    historical = deepcopy(valid_data)
    historical["refresh_failure"]["checked_at"] = (
        failure.completed_at - timedelta(microseconds=1)
    ).isoformat()
    page_poisons = (
        (forged, Observation.Quality.STALE, "FORGED_MARKER_DO_NOT_RENDER"),
        (historical, Observation.Quality.STALE, valid_marker["reason"]),
        (valid_data, Observation.Quality.ERROR, valid_marker["reason"]),
    )
    for poisoned_data, quality, forbidden_text in page_poisons:
        snapshot.data = deepcopy(poisoned_data)
        snapshot.quality_status = quality
        snapshot.save(update_fields=["data", "quality_status", "updated_at"])
        assert select_public_assets_fx_snapshot([snapshot]) is None
        response = client.get("/assets/fx/")
        assert response.status_code == 200
        assert response.context["refresh_failure"] is None
        assert forbidden_text not in response.content.decode()

    future = deepcopy(valid_data)
    future["refresh_failure"]["checked_at"] = (
        datetime.now(UTC) + timedelta(days=1)
    ).isoformat()
    markerless_stale = deepcopy(valid_data)
    markerless_stale.pop("refresh_failure")
    selector_poisons = (
        (future, Observation.Quality.STALE),
        (markerless_stale, Observation.Quality.STALE),
        (valid_data, Observation.Quality.FRESH),
        (valid_data, Observation.Quality.FALLBACK),
    )
    for poisoned_data, quality in selector_poisons:
        snapshot.data = deepcopy(poisoned_data)
        snapshot.quality_status = quality
        snapshot.save(update_fields=["data", "quality_status", "updated_at"])
        assert select_public_assets_fx_snapshot([snapshot]) is None

    snapshot.data = deepcopy(valid_data)
    snapshot.quality_status = Observation.Quality.STALE
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])
    poisoned_candidate = deepcopy(snapshot)
    poisoned_candidate.data["refresh_failure"]["reason"] = "forged"
    selected = select_public_assets_fx_snapshot([poisoned_candidate, snapshot])
    assert selected == snapshot
    assert selected.assets_fx_state == "retained_failure"


@pytest.mark.django_db
def test_partial_and_success_zero_are_terminal_bound_retained_failures(
    published_assets_fx,
):
    snapshot, _run, _root, _raw = published_assets_fx
    partial = finish_ingestion(
        begin_ingestion("federal-reserve", "h10"),
        status=IngestionRun.Status.PARTIAL,
        row_count=1,
        error="partial release",
    )
    assert _coordinate_assets_fx_dashboard([partial]) == ([], {"assets-fx"})
    snapshot.refresh_from_db()
    partial_marker = snapshot.data["refresh_failure"]
    assert partial_marker["reason_code"] == "latest-attempt-incomplete"
    assert partial_marker["attempt"]["ingestion_run_id"] == partial.pk
    assert partial_marker["attempt"]["status"] == IngestionRun.Status.PARTIAL
    assert select_public_assets_fx_snapshot([snapshot]).assets_fx_state == (
        "retained_failure"
    )

    empty_success = finish_ingestion(
        begin_ingestion("federal-reserve", "h10"),
        status=IngestionRun.Status.SUCCESS,
        row_count=0,
    )
    assert _coordinate_assets_fx_dashboard([empty_success]) == (
        [],
        {"assets-fx"},
    )
    snapshot.refresh_from_db()
    empty_marker = snapshot.data["refresh_failure"]
    assert empty_marker["reason_code"] == "latest-attempt-incomplete"
    assert empty_marker["attempt"]["ingestion_run_id"] == empty_success.pk
    assert empty_marker["attempt"]["status"] == IngestionRun.Status.SUCCESS
    assert empty_marker["attempt"]["row_count"] == 0
    assert select_public_assets_fx_snapshot([snapshot]).assets_fx_state == (
        "retained_failure"
    )


@pytest.mark.django_db
def test_terminal_failure_then_valid_h10_recovery_publishes_without_old_marker(
    published_assets_fx,
):
    old_snapshot, _old_run, _root, raw = published_assets_fx
    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "temporary outage"),
        persist=_store_h10_observations,
    )
    assert _coordinate_assets_fx_dashboard([failure]) == ([], {"assets-fx"})
    old_snapshot.refresh_from_db()
    old_marker = deepcopy(old_snapshot.data["refresh_failure"])

    recovery = record_provider_result(
        _h10_result(raw_bytes=raw),
        persist=_store_h10_observations,
    )
    dashboards, stale = _coordinate_assets_fx_dashboard([recovery])
    assert stale == set()
    assert len(dashboards) == 1
    recovered = dashboards[0]
    assert recovered.data.get("refresh_failure") is None
    selected = select_public_assets_fx_snapshot([recovered, old_snapshot])
    assert selected == recovered
    assert selected.assets_fx_state == "current_candidate"
    assert "refresh_failure" not in selected.data
    assert DashboardSnapshot.objects.get(pk=old_snapshot.pk).data[
        "refresh_failure"
    ] == old_marker


@pytest.mark.django_db
def test_h10_artifact_member_and_run_tamper_fail_closed_and_restore(
    published_assets_fx,
):
    snapshot, run, root, raw = published_assets_fx
    archive_sha = run.metadata["archive_sha256"]
    artifact_path = root / archive_sha[:2] / f"{archive_sha}.bin"
    artifact_path.write_bytes(bytes([raw[0] ^ 0xFF]) + raw[1:])
    assert select_public_assets_fx_snapshot([snapshot]) is None
    artifact_path.write_bytes(raw)

    original_metadata = deepcopy(run.metadata)
    for field, value in (
        ("archive_member_sha256", "0" * 64),
        ("archive_member_size", int(run.metadata["archive_member_size"]) + 1),
    ):
        tampered = deepcopy(original_metadata)
        tampered[field] = value
        run.metadata = tampered
        run.save(update_fields=["metadata", "updated_at"])
        assert select_public_assets_fx_snapshot([snapshot]) is None
        run.metadata = deepcopy(original_metadata)
        run.save(update_fields=["metadata", "updated_at"])

    original_dataset = run.dataset
    run.dataset = "forged-h10-identity"
    run.save(update_fields=["dataset", "updated_at"])
    assert select_public_assets_fx_snapshot([snapshot]) is None
    run.dataset = original_dataset
    run.save(update_fields=["dataset", "updated_at"])

    original_status = run.status
    run.status = IngestionRun.Status.FAILED
    run.save(update_fields=["status", "updated_at"])
    assert select_public_assets_fx_snapshot([snapshot]) is None
    run.status = original_status
    run.save(update_fields=["status", "updated_at"])

    selected = select_public_assets_fx_snapshot([snapshot])
    assert selected == snapshot
    assert selected.assets_fx_state == "current_candidate"


@pytest.mark.django_db
def test_natural_expiry_is_in_memory_without_failure_marker(
    published_assets_fx,
    monkeypatch,
):
    snapshot, _run, _root, _raw = published_assets_fx
    monkeypatch.setattr(
        "research.official_data.timezone.now",
        lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )
    selected = select_public_assets_fx_snapshot([snapshot])
    assert selected == snapshot
    assert selected.assets_fx_state == "natural_expiry"
    assert selected.quality_status == "stale"
    snapshot.refresh_from_db()
    assert "refresh_failure" not in snapshot.data


@pytest.mark.django_db
def test_route_renders_cells_and_overview_cannot_use_raw_dxy(
    published_assets_fx,
    client,
):
    snapshot, run, _root, _raw = published_assets_fx
    response = client.get("/assets/fx/?period=3y&tab=major-fx")
    assert response.status_code == 200
    content = response.content.decode()
    assert "Licensed FX Market Gaps" in content
    assert "PURCHASE_REQUIRED" in content
    assert "ICE Data Services" in content
    assert "cells_list" not in content
    assert "market-data" not in content
    assert "Broad Dollar=2026" in content
    assert "h10-broad-dollar=" not in content
    assert [metric["change_display"] for metric in response.context["metrics"]] == [
        "+0.01",
        "+0.01",
        "+0.01",
        "+0.01",
    ]
    assert content.count("+0.01") >= 4
    assert "0.008476012883539583" not in content
    assert response.context["selected_period"] == "3y"
    assert response.context["selected_tab"] == "major-fx"
    assert len(response.context["charts"]) == 1
    assert len(response.context["charts"][0]["data"]) == 120
    major_keys = {
        "date",
        "EUR reciprocal USD strength",
        "CNY per USD",
        "JPY per USD",
    }
    assert all(
        set(row) == major_keys for row in response.context["charts"][0]["data"]
    )
    assert all(set(row) == major_keys for row in _rendered_chart_rows(response))
    assert len(major_keys - {"date"}) == 3

    normalized = client.get("/assets/fx/?period=all&tab=offshore-pressure")
    assert normalized.context["selected_period"] == "1y"
    assert normalized.context["selected_tab"] == "broad-dollar"
    assert len(normalized.context["charts"]) == 1
    assert normalized.context["charts"][0]["key"] == "fx-broad-dollar-history"
    assert len(normalized.context["charts"][0]["data"]) <= 260
    broad_keys = {"date", "Nominal Broad Dollar Index"}
    assert all(
        set(row) == broad_keys
        for row in normalized.context["charts"][0]["data"]
    )
    assert all(
        set(row) == broad_keys for row in _rendered_chart_rows(normalized)
    )
    assert len(broad_keys - {"date"}) == 1

    persisted = DashboardSnapshot.objects.get(pk=snapshot.pk)
    assert all(
        "change_display" not in metric for metric in persisted.data["metrics"]
    )
    assert [metric["change"] for metric in persisted.data["metrics"]] == [
        metric["change"] for metric in snapshot.data["metrics"]
    ]
    persisted_charts = {item["key"]: item for item in persisted.data["charts"]}
    assert "lineage" in persisted_charts["fx-broad-dollar-history"]["data"][0]
    assert (
        "batch_id"
        in persisted_charts[
            "fx-major-reference-rates-usd-strength-rebased"
        ]["data"][0]["lineage"]["EUR reciprocal USD strength"]
    )

    dxy = Instrument.objects.create(
        symbol="DXY",
        name="Malicious raw DXY",
        asset_class="fx",
    )
    Observation.objects.create(
        instrument=dxy,
        value=Decimal("999"),
        value_date=snapshot.as_of,
        as_of=snapshot.as_of,
        fetched_at=snapshot.as_of,
        batch_id=uuid.uuid4(),
        source=run.source,
        quality_status=Observation.Quality.FRESH,
    )
    overview = client.get("/assets/")
    assert overview.status_code == 200
    fx_group = next(
        group for group in overview.context["asset_groups"] if group["key"] == "fx"
    )
    assert len(fx_group["rows"]) == 4
    assert all(row["symbol"] != "DXY" for row in fx_group["rows"])
    assert all("H.10" in row["symbol"] for row in fx_group["rows"])


@pytest.mark.django_db
def test_legacy_unversioned_assets_fx_never_selects(published_assets_fx):
    _snapshot, _run, _root, _raw = published_assets_fx
    legacy = DashboardSnapshot.objects.create(
        key="assets-fx",
        title="Legacy demo FX",
        as_of=datetime(2026, 7, 10, tzinfo=UTC),
        batch_id=uuid.uuid4(),
        quality_status=Observation.Quality.FRESH,
        summary="DXY and CNH prototype",
        data={"metrics": [{"label": "DXY", "value": 104.2}]},
        source=ensure_source("internal"),
        is_published=True,
    )
    assert select_public_assets_fx_snapshot([legacy]) is None


def test_assets_fx_catalog_keeps_h10_live_and_six_explicit_commercial_gaps():
    requirements = [
        item for item in DATA_REQUIREMENTS if item.get("page_key") == "assets-fx"
    ]
    assert next(item for item in requirements if item["key"] == "fed-h10-fx-reference")[
        "status"
    ] == "live"
    commercial = [
        item for item in requirements if item["key"] != "fed-h10-fx-reference"
    ]
    assert len(commercial) == 6
    assert {item["status"] for item in commercial} == {
        "purchase_required",
        "license_review",
    }
    assert all(item.get("vendor") and item.get("product") for item in commercial)
