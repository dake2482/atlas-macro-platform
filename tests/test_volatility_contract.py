from __future__ import annotations

import io
import uuid
import zipfile
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pytest
from django.conf import settings
from django.utils import timezone

from research.data_catalog import DATA_REQUIREMENTS
from research.fed_h10 import (
    H10_SOURCE_ATTRIBUTES,
    H10_TARGET_SERIES,
    FederalReserveH10Provider,
)
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
)
from research.official_data import (
    INDEPENDENT_PUBLICATION_KEYS,
    _publish_dashboard,
    _publish_dashboard_core,
    _store_h10_observations,
    publish_official_dashboards,
)
from research.providers import ProviderResult
from research.services import ensure_source, record_provider_result
from research.volatility_contract import (
    FX_VOL_CONTRACT_VERSION,
    FX_VOL_FORMULA_VERSION,
    FX_VOL_REQUIRED_CHART_KEYS,
    FX_VOL_REQUIRED_METRIC_KEYS,
    FX_VOL_REQUIRED_SECTION_KEYS,
    _rolling_realized_volatility,
    annualized_realized_volatility,
    coordinate_fx_vol_dashboard,
    select_public_fx_vol_snapshot,
)


def _dates(
    count: int = 340,
    *,
    end: date = date(2026, 7, 10),
) -> tuple[str, ...]:
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
    try:
        return FederalReserveH10Provider(client=client).h10()
    finally:
        client.close()


@pytest.fixture
def published_fx_vol(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    result = _h10_result()
    assert result.ok
    run = record_provider_result(result, persist=_store_h10_observations)
    assert run.status == IngestionRun.Status.SUCCESS, run.error
    dashboards, stale = coordinate_fx_vol_dashboard([run])
    assert stale == set()
    assert len(dashboards) == 1
    return dashboards[0], run, result.raw_bytes


def test_realized_volatility_formula_is_sample_based_and_inverse_safe():
    constant = [Decimal("100")] * 21
    assert annualized_realized_volatility(constant, window=20) == 0

    levels = [Decimal("100") * (Decimal("1.002") ** index) for index in range(21)]
    inverse = [Decimal("1") / item for item in levels]
    direct_rv = annualized_realized_volatility(levels, window=20)
    inverse_rv = annualized_realized_volatility(inverse, window=20)
    assert direct_rv.quantize(Decimal("0.000000000001")) == inverse_rv.quantize(
        Decimal("0.000000000001")
    )

    uneven = [Decimal("100") + Decimal(index * index) / Decimal("100") for index in range(21)]
    rv = annualized_realized_volatility(uneven, window=20)
    assert rv > 0
    observations = [
        SimpleNamespace(
            value=value,
            value_date=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        )
        for index, value in enumerate(uneven)
    ]
    rolling = _rolling_realized_volatility(observations, window=20)
    assert rolling[-1][1] == rv.quantize(Decimal("0.00000001"))
    with pytest.raises(ValueError, match=r"exactly N\+1"):
        annualized_realized_volatility(uneven[:-1], window=20)
    with pytest.raises(ValueError, match="positive"):
        annualized_realized_volatility([*uneven[:-1], Decimal("0")], window=20)


@pytest.mark.django_db
def test_fx_vol_publishes_exact_strict_contract(published_fx_vol):
    snapshot, run, _raw = published_fx_vol
    data = snapshot.data
    assert data["contract_version"] == FX_VOL_CONTRACT_VERSION
    assert data["formula_version"] == FX_VOL_FORMULA_VERSION
    assert {item["key"] for item in data["metrics"]} == set(
        FX_VOL_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in data["charts"]} == set(
        FX_VOL_REQUIRED_CHART_KEYS
    )
    assert {item["key"] for item in data["sections"]} == set(
        FX_VOL_REQUIRED_SECTION_KEYS
    )
    assert all(len(item["data"]) == 260 for item in data["charts"])
    assert data["component_batches"] == [str(run.batch_id)]
    assert data["input_run"]["id"] == run.pk
    assert data["acquisition_artifact"]["private_uri"].startswith(
        "private://federal-reserve/"
    )
    assert MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).count() == 4
    assert select_public_fx_vol_snapshot([snapshot]) == snapshot
    assert snapshot.fx_vol_state == "current_candidate"

    for metric in data["metrics"]:
        assert metric["source_key"] == "internal"
        assert metric["quality_status"] == Observation.Quality.ESTIMATED
        assert metric["fallback_source"] is None
        assert metric["metadata"]["window"] == 20
        assert metric["metadata"]["sample_count"] == 20
        assert metric["metadata"]["standard_deviation"] == "sample"
        assert len(metric["metadata"]["input_lineage"]) == 21
        assert metric["metadata"]["change_unit"] == "pp"

    section_map = {item["key"]: item for item in data["sections"]}
    assert len(section_map["latest-h10-realized-volatility"]["rows"]) == 4
    assert len(section_map["h10-rv-source-methodology"]["rows"]) == 1
    assert len(section_map["licensed-fx-volatility-gaps"]["rows"]) == 5


@pytest.mark.django_db
def test_fx_vol_accepts_exact_320_common_levels(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    result = _h10_result(raw_bytes=_h10_archive(dates=_dates(320)))
    assert result.ok
    run = record_provider_result(result, persist=_store_h10_observations)

    dashboards, stale = coordinate_fx_vol_dashboard([run])

    assert stale == set()
    assert len(dashboards) == 1
    charts = {item["key"]: item for item in dashboards[0].data["charts"]}
    assert all(len(chart["data"]) == 260 for chart in charts.values())


@pytest.mark.django_db
def test_fx_vol_generic_writers_are_hard_blocked(published_fx_vol):
    snapshot, _run, _raw = published_fx_vol
    assert "fx-vol" in INDEPENDENT_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"fx-vol"}) == []
    kwargs = {
        "key": "fx-vol",
        "title": "forged",
        "summary": "forged",
        "metrics": [],
        "batch_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError, match="dedicated"):
        _publish_dashboard(**kwargs)
    with pytest.raises(ValueError, match="dedicated"):
        _publish_dashboard_core(**kwargs)
    assert DashboardSnapshot.objects.filter(key="fx-vol").count() == 1
    assert select_public_fx_vol_snapshot([snapshot]) == snapshot


@pytest.mark.django_db
def test_fx_vol_selector_rejects_payload_metric_and_observation_tamper(
    published_fx_vol,
):
    snapshot, run, _raw = published_fx_vol
    metric = MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).first()
    assert metric is not None
    original_display = metric.display_value
    metric.display_value = "forged"
    metric.save(update_fields=["display_value", "updated_at"])
    assert select_public_fx_vol_snapshot([snapshot]) is None
    metric.display_value = original_display
    metric.save(update_fields=["display_value", "updated_at"])

    observation = Observation.objects.filter(batch_id=run.batch_id).first()
    assert observation is not None
    original_value = observation.value
    observation.value += Decimal("1")
    observation.save(update_fields=["value", "updated_at"])
    assert select_public_fx_vol_snapshot([snapshot]) is None
    observation.value = original_value
    observation.save(update_fields=["value", "updated_at"])

    original_data = deepcopy(snapshot.data)
    tampered = deepcopy(original_data)
    tampered["charts"][0]["data"][-1]["H.10 EUR/USD"] += 1
    # Rehashing the forged payload is still rejected by exact raw replay.
    from research.volatility_contract import _integrity_projection, _semantic_projection, _sha256

    tampered["fingerprint"] = _sha256(_semantic_projection(tampered))
    tampered["payload_integrity_hash"] = _sha256(_integrity_projection(tampered))
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_fx_vol_snapshot([snapshot]) is None


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["title", "summary"])
def test_fx_vol_selector_rejects_public_copy_tamper(published_fx_vol, field):
    snapshot, _run, _raw = published_fx_vol
    setattr(snapshot, field, "forged public copy")
    snapshot.save(update_fields=[field, "updated_at"])

    assert select_public_fx_vol_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_fx_vol_selector_fails_closed_for_malformed_candidate(published_fx_vol):
    snapshot, _run, _raw = published_fx_vol
    snapshot.data = {
        "contract_version": FX_VOL_CONTRACT_VERSION,
        "input_run": ["not", "a", "mapping"],
        "metrics": None,
    }
    snapshot.save(update_fields=["data", "updated_at"])

    assert select_public_fx_vol_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_fx_vol_natural_expiry_is_a_presentation_state(
    published_fx_vol,
    monkeypatch,
):
    snapshot, _run, _raw = published_fx_vol
    fresh_until = datetime.fromisoformat(snapshot.data["fresh_until"])
    monkeypatch.setattr(
        "research.volatility_contract.timezone.now",
        lambda: fresh_until + timedelta(seconds=1),
    )

    selected = select_public_fx_vol_snapshot([snapshot])

    assert selected == snapshot
    assert selected.fx_vol_state == "natural_expiry"
    assert selected.quality_status == Observation.Quality.STALE
    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.ESTIMATED
    assert "refresh_failure" not in snapshot.data


@pytest.mark.django_db
def test_fx_vol_route_labels_natural_expiry_without_fake_failure(
    published_fx_vol,
    client,
    monkeypatch,
):
    snapshot, _run, _raw = published_fx_vol
    fresh_until = datetime.fromisoformat(snapshot.data["fresh_until"])
    monkeypatch.setattr(
        "research.volatility_contract.timezone.now",
        lambda: fresh_until + timedelta(seconds=1),
    )
    monkeypatch.setattr(
        "research.views.timezone.now",
        lambda: fresh_until + timedelta(seconds=1),
    )

    response = client.get("/volatility/fx-vol/")

    assert response.status_code == 200
    assert response.context["snapshot"].quality_status == Observation.Quality.STALE
    assert response.context["refresh_failure"] is None
    assert response.context["stale_notice"]["reason_code"] == "natural-expiry"
    assert all(
        item["quality_status"] == Observation.Quality.STALE
        for item in response.context["metrics"]
    )
    assert response.context["charts"][0]["quality_status"] == Observation.Quality.STALE
    body = response.content.decode()
    assert "自然超过声明的新鲜度窗口" in body
    assert "没有把自然过期伪装成采集失败" in body


@pytest.mark.django_db
def test_fx_vol_same_values_append_and_terminal_failure_retains(published_fx_vol):
    first, old_run, raw = published_fx_vol
    replacement = record_provider_result(
        _h10_result(raw_bytes=raw),
        persist=_store_h10_observations,
    )
    assert replacement.status == IngestionRun.Status.SUCCESS
    dashboards, stale = coordinate_fx_vol_dashboard([replacement])
    assert stale == set()
    assert len(dashboards) == 1
    second = dashboards[0]
    assert second.pk != first.pk
    assert second.data["fingerprint"] == first.data["fingerprint"]
    assert second.data["payload_integrity_hash"] != first.data["payload_integrity_hash"]
    assert Observation.objects.filter(batch_id=old_run.batch_id).count() == old_run.row_count
    assert coordinate_fx_vol_dashboard([replacement]) == ([], set())

    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "upstream timeout"),
        persist=_store_h10_observations,
    )
    assert coordinate_fx_vol_dashboard([failure]) == ([], {"fx-vol"})
    second.refresh_from_db()
    assert second.data["refresh_failure"]["attempt"]["id"] == failure.pk
    selected = select_public_fx_vol_snapshot([second])
    assert selected == second
    assert selected.fx_vol_state == "retained_failure"

    marker = deepcopy(second.data["refresh_failure"])
    marker["attempt"]["id"] = old_run.pk
    second.data = {**second.data, "refresh_failure": marker}
    second.save(update_fields=["data", "updated_at"])
    assert select_public_fx_vol_snapshot([second]) is None


@pytest.mark.django_db
def test_fx_vol_retained_failure_requires_stale_quality_and_current_marker(
    published_fx_vol,
):
    snapshot, _run, _raw = published_fx_vol
    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "upstream timeout"),
        persist=_store_h10_observations,
    )
    assert coordinate_fx_vol_dashboard([failure]) == ([], {"fx-vol"})
    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.STALE
    assert select_public_fx_vol_snapshot([snapshot]) == snapshot

    snapshot.quality_status = Observation.Quality.ESTIMATED
    snapshot.save(update_fields=["quality_status", "updated_at"])
    assert select_public_fx_vol_snapshot([snapshot]) is None

    snapshot.quality_status = Observation.Quality.STALE
    marker = deepcopy(snapshot.data["refresh_failure"])
    marker["checked_at"] = (timezone.now() + timedelta(days=1)).isoformat()
    snapshot.data = {**snapshot.data, "refresh_failure": marker}
    snapshot.save(update_fields=["data", "quality_status", "updated_at"])
    assert select_public_fx_vol_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_fx_vol_transition_pending_hides_prior_failure_marker(
    published_fx_vol,
    client,
):
    snapshot, _run, _raw = published_fx_vol
    failure = record_provider_result(
        ProviderResult.failure("federal-reserve", "h10", "upstream timeout"),
        persist=_store_h10_observations,
    )
    assert coordinate_fx_vol_dashboard([failure]) == ([], {"fx-vol"})
    snapshot.refresh_from_db()
    assert "refresh_failure" in snapshot.data

    running = IngestionRun.objects.create(
        source=ensure_source("federal-reserve"),
        dataset="h10",
        started_at=timezone.now() + timedelta(seconds=1),
        status=IngestionRun.Status.RUNNING,
    )
    selected = select_public_fx_vol_snapshot([snapshot])

    assert running.status == IngestionRun.Status.RUNNING
    assert selected == snapshot
    assert selected.fx_vol_state == "transition_pending"
    assert selected.quality_status == Observation.Quality.STALE
    assert "refresh_failure" not in selected.data
    snapshot.refresh_from_db()
    assert "refresh_failure" in snapshot.data

    response = client.get("/volatility/fx-vol/")
    assert response.status_code == 200
    assert response.context["refresh_failure"] is None
    assert response.context["stale_notice"]["reason_code"] == "transition-pending"
    body = response.content.decode()
    assert "最新 H.10 刷新批次仍在运行" in body
    assert "不表示采集或完整性校验已经失败" in body
    assert "upstream timeout" not in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error",
    (
        OSError("artifact unavailable"),
        zipfile.BadZipFile("archive invalid"),
        ElementTree.ParseError("xml invalid"),
    ),
)
def test_fx_vol_selector_fails_closed_for_artifact_parser_errors(
    published_fx_vol,
    monkeypatch,
    error,
):
    snapshot, _run, _raw = published_fx_vol

    def fail_replay(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(
        "research.volatility_contract._build_fx_vol_payload",
        fail_replay,
    )

    assert select_public_fx_vol_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_five_unsupported_routes_are_structured_prose_only(client):
    source = ensure_source("internal")
    cases = {
        "/volatility/": (
            "volatility",
            (("volatility-coverage-ledger", ("component", "status", "public-output", "next-action")),),
        ),
        "/volatility/dashboard/": (
            "volatility-dashboard",
            (("volatility-dashboard-preconditions", ("layer", "required-contract", "current-state", "failure-policy")),),
        ),
        "/volatility/vix/": (
            "vix",
            (
                ("vix-data-boundary", ("dataset", "what-it-is", "why-not-substitute", "required-licence")),
                ("vix-post-purchase-fields", ("field", "requirement", "reason")),
            ),
        ),
        "/volatility/move/": (
            "volatility-move",
            (
                ("move-data-boundary", ("dataset", "what-it-is", "why-not-substitute", "required-licence")),
                ("move-post-purchase-fields", ("field", "requirement", "reason")),
            ),
        ),
        "/volatility/implied-vs-realized/": (
            "implied-vs-realized",
            (
                ("iv-rv-input-boundary", ("input", "required-contract", "failure-policy", "licence")),
                ("iv-rv-post-purchase-fields", ("field", "requirement", "reason")),
            ),
        ),
    }
    for page_key, _contracts in cases.values():
        DashboardSnapshot.objects.create(
            key=page_key,
            title="Rogue numeric volatility",
            as_of=datetime(2026, 7, 10, tzinfo=UTC),
            batch_id=uuid.uuid4(),
            quality_status=Observation.Quality.FRESH,
            summary="15.8 96 14.9 17.4 11 / 30",
            data={
                "demo": False,
                "metrics": [{"label": "ROGUE VIX", "value": 999}],
                "charts": [{"key": "rogue", "data": [{"date": "2026-07-10", "value": 999}]}],
            },
            source=source,
            is_published=True,
        )
    for path, (_page_key, contracts) in cases.items():
        response = client.get(path)
        body = response.content.decode()
        assert response.status_code == 200
        assert response.context.get("snapshot") is None
        assert response.context["metrics"] == []
        assert response.context["charts"] == []
        assert response.context["sections"]
        assert tuple(section["key"] for section in response.context["sections"]) == tuple(
            key for key, _columns in contracts
        )
        for section, (_key, columns) in zip(
            response.context["sections"], contracts, strict=True
        ):
            assert tuple(column["key"] for column in section["columns"]) == columns
            for row in section["rows"]:
                assert tuple(item["key"] for item in row["cells_list"]) == columns
                for item in row["cells_list"]:
                    value = str(row[item["key"]])
                    if value.startswith("/"):
                        assert item["cell"] == {
                            "kind": "url",
                            "label": value,
                            "href": value,
                        }
                    else:
                        assert item["cell"] == {"kind": "text", "value": value}
        assert "ROGUE VIX" not in body
        assert "999" not in body
        for prototype in ("15.8", "14.9", "17.4", "11 / 30"):
            assert prototype not in body
        assert "dashboard-chart-0" not in body
    overview_body = client.get("/volatility/").content.decode()
    assert '<a href="/volatility/fx-vol/"' in overview_body
    assert ">/volatility/fx-vol/</a>" in overview_body
    assert ">LIVE<" not in overview_body
    assert "CONTRACT_READY" in overview_body


@pytest.mark.django_db
def test_fx_vol_route_controls_and_rendered_contract(published_fx_vol, client):
    response = client.get("/volatility/fx-vol/?period=3m&tab=60d")
    assert response.status_code == 200
    assert response.context["selected_period"] == "3m"
    assert response.context["selected_tab"] == "60d"
    assert len(response.context["metrics"]) == 4
    assert len(response.context["charts"]) == 1
    assert response.context["charts"][0]["key"] == "h10-fx-realized-volatility-60d"
    assert 80 <= len(response.context["charts"][0]["data"]) <= 100
    assert all(
        set(row)
        == {
            "date",
            "H.10 Broad Dollar",
            "H.10 EUR/USD",
            "H.10 USD/CNY",
            "H.10 USD/JPY",
        }
        for row in response.context["charts"][0]["data"]
    )
    assert len(response.context["sections"]) == 3
    body = response.content.decode()
    assert "H.10 外汇实现波动率" in body
    assert "PURCHASE_REQUIRED" in body
    assert "sample_std" in body
    assert "cells_list" not in body
    assert "ATM implied volatility" in body
    assert "MOVE 96" not in body

    normalized = client.get("/volatility/fx-vol/?period=all&tab=iv")
    assert normalized.context["selected_period"] == "1y"
    assert normalized.context["selected_tab"] == "20d"
    assert normalized.context["charts"][0]["key"] == "h10-fx-realized-volatility-20d"


def test_volatility_catalog_separates_live_proxy_and_purchase_boundaries():
    requirements = {item["key"]: item for item in DATA_REQUIREMENTS}
    assert requirements["fx-vol-h10-realized"]["status"] == "proxy"
    assert requirements["volatility-treasury-rv-precondition"]["status"] == "live"
    assert requirements["volatility-cross-asset-parent"]["status"] == "needs_source"
    assert requirements["move-index"]["page_key"] == "volatility-move"
    assert "move-page-data" not in requirements
    assert requirements["vix-history"]["status"] == "purchase_required"
    assert requirements["fx-vol-surface"]["status"] == "purchase_required"
    assert requirements["iv-rv-cross-asset"]["status"] == "purchase_required"
