from __future__ import annotations

import hashlib
import html
import json
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest
from django.db import connection
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext

from research.data_catalog import DATA_REQUIREMENTS
from research.economy_contract import (
    _component_payload as _strict_economy_component_payload,
)
from research.employment_contract import EMPLOYMENT_BLS_REQUEST_SERIES
from research.inflation_contract import (
    INFLATION_BLS_DATASET,
    INFLATION_CONTRACT_VERSION,
    INFLATION_FORMULA_VERSION,
    INFLATION_REQUIRED_CHART_KEYS,
    INFLATION_REQUIRED_METRIC_KEYS,
    INFLATION_REQUIRED_SECTION_KEYS,
    _is_inflation_bls_dataset,
    coordinate_inflation_dashboard,
    publish_inflation_revision,
    select_public_inflation_snapshot,
)
from research.inflation_contract import (
    _validate_bls_run as _validate_inflation_bls_run,
)
from research.macro_releases import BEAPIOReleaseProvider
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    SourceLicense,
)
from research.official_data import (
    APPEND_ONLY_PUBLICATION_KEYS,
    BLS_SERIES,
    INDEPENDENT_PUBLICATION_KEYS,
    INFLATION_PUBLICATION_GROUPS,
    MACRO_REQUIRED_SERIES,
    TREASURY_CURVE_FORMULA_VERSION,
    _inflation_market_expectations_from_real_rates,
    _inflation_page_data,
    _inflation_page_is_buildable,
    _publish_dashboard,
    _publish_dashboard_core,
    _store_bea_release_observations_v2,
    _store_bls_observations_v2,
    publish_official_dashboards,
)
from research.page_registry import PAGE_CONFIGS
from research.providers import BLSProvider, ProviderResult
from research.services import ensure_source, record_provider_result
from tests.test_macro_releases import (
    _bea_pio_client,
    _bea_pio_section2_workbook,
    _bea_pio_summary_workbook,
)

INFLATION_SERIES = (
    "CUSR0000SA0",
    "CUUR0000SA0",
    "CUSR0000SA0L1E",
    "CUUR0000SA0L1E",
    "CUSR0000SAH1",
    "CUUR0000SAH1",
    "CUSR0000SACL1E",
    "CUUR0000SACL1E",
    "CUSR0000SASLE",
    "CUUR0000SASLE",
    "WPSFD4",
    "WPUFD4",
)
BLS_LATEST_PERIOD = date(2026, 6, 1)
PIO_LATEST_PERIOD = date(2026, 5, 1)
FIXED_NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)
BLS_FETCHED_AT = datetime(2026, 6, 10, 12, 35, tzinfo=UTC)
PIO_FETCHED_AT = datetime(2026, 6, 25, 14, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_contract_clocks(monkeypatch):
    for target in (
        "research.official_data.timezone.now",
        "research.inflation_contract.timezone.now",
        "research.services.timezone.now",
    ):
        monkeypatch.setattr(target, lambda: FIXED_NOW)


def _month(start: date, offset: int) -> date:
    month_index = start.year * 12 + start.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _bls_value(series_id: str, period: date, index: int) -> Decimal:
    anchors = {
        ("CUSR0000SA0", BLS_LATEST_PERIOD): Decimal("121"),
        ("CUSR0000SA0", date(2026, 5, 1)): Decimal("110"),
        ("CUSR0000SA0", date(2026, 3, 1)): Decimal("100"),
        ("CUSR0000SA0", date(2025, 12, 1)): Decimal("100"),
        ("CUUR0000SA0", BLS_LATEST_PERIOD): Decimal("120"),
        ("CUUR0000SA0", date(2025, 6, 1)): Decimal("100"),
    }
    if (series_id, period) in anchors:
        return anchors[(series_id, period)]
    bases = {
        "CUSR0000SA0": Decimal("100"),
        "CUUR0000SA0": Decimal("101"),
        "CUSR0000SA0L1E": Decimal("200"),
        "CUUR0000SA0L1E": Decimal("201"),
        "CUSR0000SAH1": Decimal("300"),
        "CUUR0000SAH1": Decimal("301"),
        "CUSR0000SACL1E": Decimal("400"),
        "CUUR0000SACL1E": Decimal("401"),
        "CUSR0000SASLE": Decimal("500"),
        "CUUR0000SASLE": Decimal("501"),
        "WPSFD4": Decimal("150"),
        "WPUFD4": Decimal("151"),
    }
    return bases.get(series_id, Decimal("1000")) + Decimal(index) / Decimal("10")


@lru_cache(maxsize=1)
def _bls_template() -> tuple[bytes, tuple[dict, ...], dict]:
    start = date(2021, 5, 1)
    periods = [_month(start, index) for index in range(62)]
    response_series = []
    for series_id in EMPLOYMENT_BLS_REQUEST_SERIES:
        points = []
        for index, period in enumerate(periods):
            preliminary = series_id in {"WPSFD4", "WPUFD4"} and index >= 58
            points.append(
                {
                    "year": str(period.year),
                    "period": f"M{period.month:02d}",
                    "periodName": str(period.month),
                    "value": str(_bls_value(series_id, period, index)),
                    "latest": "true" if period == BLS_LATEST_PERIOD else "false",
                    "footnotes": (
                        [{"code": "P", "text": "preliminary"}]
                        if preliminary
                        else [{}]
                    ),
                }
            )
        response_series.append({"seriesID": series_id, "data": points})
    raw_json = json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "message": [],
            "Results": {"series": response_series},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    records, replay = BLSProvider.parse_series_json_bytes(
        raw_json,
        series_ids=EMPLOYMENT_BLS_REQUEST_SERIES,
        start_year=2021,
        end_year=2026,
        fetched_at=BLS_FETCHED_AT,
    )
    return raw_json, tuple(records), replay


@lru_cache(maxsize=1)
def _pio_template() -> tuple[bytes, tuple[dict, ...], dict]:
    provider = BEAPIOReleaseProvider(
        client=_bea_pio_client(
            _bea_pio_summary_workbook(),
            _bea_pio_section2_workbook(),
        )
    )
    try:
        result = provider.personal_income_outlays()
    finally:
        provider.close()
    assert result.ok
    return bytes(result.raw_bytes), tuple(result.records), dict(result.metadata)


def _strict_bls_run(settings, tmp_path, *, cycle: str = "bls-independent"):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    raw_json, records, replay = _bls_template()
    result = ProviderResult(
        provider="bls",
        dataset=INFLATION_BLS_DATASET,
        fetched_at=BLS_FETCHED_AT,
        records=deepcopy(list(records)),
        raw_bytes=raw_json,
        metadata={
            **deepcopy(replay),
            "endpoint": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            "content_type": "application/json",
            "byte_length": len(raw_json),
            "sha256": hashlib.sha256(raw_json).hexdigest(),
            "request_witness": {
                "series_ids": list(EMPLOYMENT_BLS_REQUEST_SERIES),
                "start_year": 2021,
                "end_year": 2026,
            },
            "refresh_cycle_id": cycle,
        },
    )
    run = record_provider_result(result, persist=_store_bls_observations_v2)
    assert run.status == IngestionRun.Status.SUCCESS
    return run


def _strict_pio_run(settings, tmp_path, *, cycle: str = "pio-independent"):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    raw_bundle, records, metadata = _pio_template()
    result = ProviderResult(
        provider="bea-pio-release",
        dataset="personal-income-outlays-release",
        fetched_at=PIO_FETCHED_AT,
        records=deepcopy(list(records)),
        raw_bytes=raw_bundle,
        metadata={**deepcopy(metadata), "refresh_cycle_id": cycle},
    )
    run = record_provider_result(result, persist=_store_bea_release_observations_v2)
    assert run.status == IngestionRun.Status.SUCCESS
    return run


def _strict_inflation_runs(settings, tmp_path):
    return _strict_bls_run(settings, tmp_path), _strict_pio_run(settings, tmp_path)


@pytest.mark.django_db
def test_inflation_bls_gate_uses_type_strict_required_metadata(
    settings,
    tmp_path,
):
    bls_run = _strict_bls_run(settings, tmp_path)
    baseline = deepcopy(bls_run.metadata)
    for mutation in ("bool-for-int", "float-for-int", "missing-key"):
        metadata = deepcopy(baseline)
        if mutation == "bool-for-int":
            metadata["official_missing_observation_count"] = False
        elif mutation == "float-for-int":
            metadata["start_year"] = 2021.0
        else:
            metadata.pop("latest_missing_series")
        IngestionRun.objects.filter(pk=bls_run.pk).update(metadata=metadata)
        bls_run.refresh_from_db()

        with pytest.raises(ValueError, match="metadata does not replay"):
            _validate_inflation_bls_run(bls_run)


def _real_rates_snapshot() -> DashboardSnapshot:
    internal = ensure_source("internal")
    ensure_source("us-treasury-rates")
    publication_batch = uuid.uuid4()
    nominal_batch = uuid.uuid4()
    real_batch = uuid.uuid4()
    common = {
        "unit": "%",
        "source": "Atlas Macro 计算（U.S. Treasury 输入）",
        "source_key": "internal",
        "source_keys": ["internal", "us-treasury-rates"],
        "quality_status": "estimated",
        "as_of": "2026-07-10T00:00:00+00:00",
        "value_date": "2026-07-10T00:00:00+00:00",
        "fetched_at": "2026-07-13T06:52:50+00:00",
        "fresh_until": "2026-07-20T14:00:00+00:00",
        "fallback_source": None,
        "batch_id": f"{nominal_batch},{real_batch}",
    }
    metrics = [
        {
            **common,
            "key": f"{tenor}y-bei",
            "label": f"{tenor}Y 盈亏平衡通胀",
            "value": value,
            "display_value": f"{value:.2f}%",
            "change": 0.0,
            "metadata": {"formula": f"UST-{tenor}Y - TIPS-{tenor}Y"},
        }
        for tenor, value in ((5, 2.28), (10, 2.24))
    ]
    chart = {
        "key": "nominal-real-breakeven-history",
        "title": "名义、实际与盈亏平衡通胀",
        "description": "Treasury par curve proxy",
        "kind": "line",
        "source_keys": ["internal", "us-treasury-rates"],
        "batch_ids": [str(nominal_batch), str(real_batch)],
        "quality_status": "estimated",
        "as_of": common["as_of"],
        "fetched_at": common["fetched_at"],
        "fresh_until": common["fresh_until"],
        "data": [
            {
                "date": "2026-07-10",
                "5Y BEI": 2.28,
                "10Y BEI": 2.24,
                "_source_keys": ["internal", "us-treasury-rates"],
                "_batch_ids": [str(nominal_batch), str(real_batch)],
            }
        ],
    }
    snapshot = DashboardSnapshot.objects.create(
        key="real-rates",
        title="实际利率",
        summary="strict real-rates fixture",
        as_of=datetime(2026, 7, 10, tzinfo=UTC),
        batch_id=publication_batch,
        quality_status="estimated",
        source=internal,
        is_published=True,
        data={
            "demo": False,
            "contract_version": 2,
            "formula_version": TREASURY_CURVE_FORMULA_VERSION,
            "metrics": metrics,
            "charts": [chart],
            "sections": [],
            "component_batches": [str(nominal_batch), str(real_batch)],
            "source_keys": ["internal", "us-treasury-rates"],
            "fresh_until": common["fresh_until"],
            "fingerprint": "a" * 64,
            "payload_integrity_hash": "b" * 64,
            "publication_batch_id": str(publication_batch),
            "annual_runs": [
                {
                    "component": component,
                    "year": 2026,
                    "source_key": "us-treasury-rates",
                    "batch_id": str(batch_id),
                }
                for component, batch_id in (
                    ("nominal", nominal_batch),
                    ("real", real_batch),
                )
            ],
        },
    )
    snapshot.treasury_publication_state = "current_candidate"
    return snapshot


def test_inflation_contract_freezes_exact_catalog_and_generic_writers_reject_it():
    assert BLS_SERIES == EMPLOYMENT_BLS_REQUEST_SERIES
    assert len(BLS_SERIES) == 24
    assert set(MACRO_REQUIRED_SERIES["inflation"]["bls"]) == set(INFLATION_SERIES)
    assert INFLATION_PUBLICATION_GROUPS == {
        "inflation": frozenset({"bls", "bea-pio-release"})
    }
    assert "inflation" in INDEPENDENT_PUBLICATION_KEYS
    assert "inflation" in APPEND_ONLY_PUBLICATION_KEYS
    assert _is_inflation_bls_dataset(INFLATION_BLS_DATASET)
    assert not _is_inflation_bls_dataset(
        "series:" + ",".join(reversed(EMPLOYMENT_BLS_REQUEST_SERIES))
    )
    assert not _is_inflation_bls_dataset(
        "series:" + ",".join(EMPLOYMENT_BLS_REQUEST_SERIES[:-1])
    )
    assert not _is_inflation_bls_dataset(INFLATION_BLS_DATASET + ",EXTRA")
    assert PAGE_CONFIGS["inflation"]["snapshot_contract_version"] == 2
    assert len(PAGE_CONFIGS["inflation"]["metrics"]) == 32
    assert all(
        metric.get("value") is None and "demo" not in metric
        for metric in PAGE_CONFIGS["inflation"]["metrics"]
    )

    kwargs = {
        "key": "inflation",
        "title": "rogue",
        "summary": "rogue",
        "metrics": [],
        "batch_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError, match="dedicated macro v2"):
        _publish_dashboard(**kwargs)
    with pytest.raises(ValueError, match="dedicated macro v2"):
        _publish_dashboard_core(**kwargs)


@pytest.mark.django_db
def test_inflation_v2_replays_official_bytes_and_publishes_exact_formula_contract(
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    assert bls_run.dataset == INFLATION_BLS_DATASET
    assert pio_run.dataset == "personal-income-outlays-release"
    assert bls_run.metadata["refresh_cycle_id"] != pio_run.metadata["refresh_cycle_id"]
    assert RawArtifact.objects.filter(run__in=[bls_run, pio_run]).count() == 2
    assert Observation.objects.filter(batch_id=bls_run.batch_id).count() == 24 * 62
    assert {
        item.series.key
        for item in Observation.objects.filter(batch_id=pio_run.batch_id).select_related("series")
    } == {key.lower() for key in BEAPIOReleaseProvider.SERIES}

    snapshot = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert snapshot is not None
    assert publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run) is None
    assert publish_official_dashboards(keys={"inflation"}) == []
    assert snapshot.data["contract_version"] == INFLATION_CONTRACT_VERSION
    assert snapshot.data["formula_version"] == INFLATION_FORMULA_VERSION
    assert {item["key"] for item in snapshot.data["metrics"]} == set(
        INFLATION_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in snapshot.data["charts"]} == set(
        INFLATION_REQUIRED_CHART_KEYS
    )
    assert {item["key"] for item in snapshot.data["sections"]} == set(
        INFLATION_REQUIRED_SECTION_KEYS
    )
    assert len(snapshot.data["metrics"]) == len(
        {item["key"] for item in snapshot.data["metrics"]}
    ) == 32
    assert len(snapshot.data["charts"]) == len(
        {item["key"] for item in snapshot.data["charts"]}
    ) == 8
    assert len(snapshot.data["sections"]) == len(
        {item["key"] for item in snapshot.data["sections"]}
    ) == 2
    assert MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).count() == 32
    assert {item["role"] for item in snapshot.data["input_runs"]} == {
        "bls",
        "bea-pio",
    }
    assert "refresh_cycle_id" not in snapshot.data
    assert "market-breakeven-inflation" not in {
        item["key"] for item in snapshot.data["charts"]
    }

    metrics = {item["key"]: item for item in snapshot.data["metrics"]}
    assert metrics["headline-cpi-mom"]["value"] == pytest.approx(10.0)
    assert metrics["headline-cpi-yoy"]["value"] == pytest.approx(20.0)
    assert metrics["headline-cpi-3m-annualized"]["value"] == pytest.approx(
        114.358881
    )
    assert metrics["headline-cpi-6m-annualized"]["value"] == pytest.approx(46.41)
    assert metrics["pce-price-index-mom"]["value"] == pytest.approx(0.7751938)
    assert metrics["pce-price-index-yoy"]["value"] == pytest.approx(8.3333333)
    assert metrics["core-pce-price-index-yoy"]["value"] == pytest.approx(4.0)
    assert metrics["headline-cpi-mom"]["metadata"]["seasonal_basis"] == (
        "seasonally_adjusted"
    )
    assert metrics["headline-cpi-yoy"]["metadata"]["seasonal_basis"] == (
        "not_seasonally_adjusted"
    )
    assert metrics["pce-price-index-yoy"]["metadata"]["seasonal_basis"] == (
        "seasonally_adjusted"
    )
    assert metrics["core-pce-price-index-yoy"]["metadata"]["seasonal_basis"] == (
        "seasonally_adjusted"
    )
    assert metrics["final-demand-ppi-6m-annualized"]["quality_status"] == "estimated"
    assert metrics["final-demand-ppi-6m-annualized"]["metadata"]["preliminary"] is True
    for metric in metrics.values():
        assert metric["source_key"] == "internal"
        assert metric["license_scope"]
        assert metric["metadata"]["input_lineage"]
        assert all(
            lineage["fallback_source"] is None
            and lineage["license_scope"]
            and lineage["value_date"]
            and lineage["fetched_at"]
            and lineage["batch_id"]
            for lineage in metric["metadata"]["input_lineage"]
        )

    selected = select_public_inflation_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.inflation_publication_state == "current_candidate"
    metric, chart, reference, metric_row = _strict_economy_component_payload(
        "inflation",
        snapshot,
    )
    assert metric["key"] == "core-cpi-yoy"
    assert chart["key"] == "core-cpi-rates"
    assert reference["selected_metric_snapshot_id"] == metric_row.pk
    assert reference["payload_integrity_hash"] == snapshot.data["payload_integrity_hash"]
    assert reference["contract_version"] == INFLATION_CONTRACT_VERSION
    assert reference["formula_version"] == INFLATION_FORMULA_VERSION
    assert set(reference["component_roles"]) == set(snapshot.data["component_roles"])
    assert reference["source_roles"] == {"bls": "bls", "bea-pio": "bea-pio-release"}
    assert reference["root_source_keys"] == sorted(snapshot.data["source_keys"])
    assert reference["root_component_batches"] == sorted(
        snapshot.data["component_batches"]
    )
    assert reference["root_fresh_until"] == snapshot.data["fresh_until"]


@pytest.mark.django_db
def test_inflation_pair_idempotence_and_same_values_new_pio_run_append_revision(
    settings,
    tmp_path,
):
    bls_run, first_pio = _strict_inflation_runs(settings, tmp_path)
    first = publish_inflation_revision(bls_run=bls_run, bea_pio_run=first_pio)
    assert first is not None
    first_data = deepcopy(first.data)
    first_metric_ids = set(
        MetricSnapshot.objects.filter(batch_id=first.batch_id).values_list("id", flat=True)
    )

    second_pio = _strict_pio_run(settings, tmp_path, cycle="pio-new-independent-cycle")
    second = publish_inflation_revision(bls_run=bls_run, bea_pio_run=second_pio)
    assert second is not None
    assert second.pk != first.pk
    assert second.data["fingerprint"] != first.data["fingerprint"]
    assert DashboardSnapshot.objects.filter(key="inflation").count() == 2
    first.refresh_from_db()
    assert first.data == first_data
    assert set(
        MetricSnapshot.objects.filter(batch_id=first.batch_id).values_list("id", flat=True)
    ) == first_metric_ids
    assert select_public_inflation_snapshot().pk == second.pk


@pytest.mark.parametrize(
    "tamper",
    [
        "raw-bytes",
        "artifact-row",
        "input-role",
        "run",
        "observation",
        "metric-row",
        "snapshot-json",
        "licence",
    ],
)
@pytest.mark.django_db
def test_inflation_static_replay_fails_closed_on_any_lineage_tamper(
    tamper,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    snapshot = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert snapshot is not None
    if tamper == "raw-bytes":
        artifact = RawArtifact.objects.get(run=bls_run)
        path = (
            Path(settings.RAW_ARTIFACT_ROOT)
            / artifact.sha256[:2]
            / f"{artifact.sha256}.bin"
        )
        path.write_bytes(b"tampered")
    elif tamper == "artifact-row":
        RawArtifact.objects.filter(run=pio_run).update(size_bytes=1)
    elif tamper == "input-role":
        data = deepcopy(snapshot.data)
        data["input_runs"][0]["role"] = "rogue"
        DashboardSnapshot.objects.filter(pk=snapshot.pk).update(data=data)
    elif tamper == "run":
        IngestionRun.objects.filter(pk=bls_run.pk).update(row_count=bls_run.row_count + 1)
    elif tamper == "observation":
        Observation.objects.filter(batch_id=pio_run.batch_id).order_by("id").update(
            quality_status="estimated"
        )
    elif tamper == "metric-row":
        MetricSnapshot.objects.filter(
            batch_id=snapshot.batch_id,
            key="inflation-headline-cpi-yoy",
        ).update(value=Decimal("99"))
    elif tamper == "snapshot-json":
        data = deepcopy(snapshot.data)
        data["metrics"][0]["value"] = 99
        DashboardSnapshot.objects.filter(pk=snapshot.pk).update(data=data)
    else:
        SourceLicense.objects.filter(source=bls_run.source, is_current=True).update(
            public_display_allowed=False
        )
    assert select_public_inflation_snapshot() is None


@pytest.mark.parametrize(
    ("series_key", "period", "mutation"),
    [
        ("cusr0000sa0", date(2026, 5, 1), "delete"),
        ("cusr0000sa0", date(2026, 3, 1), Decimal("0")),
        ("cuur0000sa0", date(2025, 6, 1), Decimal("-1")),
    ],
)
@pytest.mark.django_db
def test_inflation_builder_rejects_missing_zero_or_negative_exact_month(
    series_key,
    period,
    mutation,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    row = Observation.objects.get(
        batch_id=bls_run.batch_id,
        series__key=series_key,
        value_date__date=period,
    )
    if mutation == "delete":
        row.delete()
    else:
        row.value = mutation
        row.save(update_fields=["value", "updated_at"])
    assert not _inflation_page_is_buildable(
        batch_id=bls_run.batch_id,
        bea_pio_batch_id=pio_run.batch_id,
    )


@pytest.mark.django_db
def test_inflation_selector_skips_rogue_legacy_demo_and_unrelated_attempts(
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    published = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert published is not None
    internal = ensure_source("internal")
    demo = ensure_source("demo-market")
    for index in range(60):
        DashboardSnapshot.objects.create(
            key="inflation",
            title=published.title,
            summary=published.summary,
            as_of=published.as_of,
            batch_id=uuid.uuid4(),
            quality_status=published.quality_status,
            source=demo if index % 3 == 0 else internal,
            is_published=True,
            data=(
                {"demo": False, "contract_version": 1}
                if index % 3 == 1
                else {**deepcopy(published.data), "unexpected": True}
            ),
        )
    IngestionRun.objects.bulk_create(
        [
            IngestionRun(
                source=bls_run.source,
                dataset=f"series:UNRELATED-{index:03d}",
                started_at=FIXED_NOW + timedelta(microseconds=index + 1),
                completed_at=FIXED_NOW + timedelta(microseconds=index + 1),
                status=IngestionRun.Status.FAILED,
                error="unrelated",
            )
            for index in range(101)
        ]
    )
    selected = select_public_inflation_snapshot()
    assert selected is not None
    assert selected.pk == published.pk
    assert selected.inflation_publication_state == "current_candidate"


@pytest.mark.parametrize(
    "malformed_container",
    ["metrics", "charts", "sections", "component_roles"],
)
@pytest.mark.django_db
def test_inflation_selector_skips_malformed_strict_looking_containers(
    client,
    malformed_container,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    published = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert published is not None
    rogue_batch = uuid.uuid4()
    rogue_data = deepcopy(published.data)
    rogue_data["publication_batch_id"] = str(rogue_batch)
    if malformed_container == "component_roles":
        rogue_data[malformed_container] = {
            key: None for key in published.data[malformed_container]
        }
    else:
        rogue_data[malformed_container] = [
            None for _item in published.data[malformed_container]
        ]
    DashboardSnapshot.objects.create(
        key="inflation",
        title=published.title,
        summary=published.summary,
        as_of=published.as_of,
        batch_id=rogue_batch,
        quality_status=published.quality_status,
        source=published.source,
        is_published=True,
        data=rogue_data,
    )

    selected = select_public_inflation_snapshot()
    assert selected is not None and selected.pk == published.pk
    assert client.get("/economy/inflation/").status_code == 200


@pytest.mark.django_db
def test_inflation_publisher_locks_sources_before_runs_and_base_observations(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    original = QuerySet.select_for_update
    lock_models = []
    joined_lock_calls = []
    observation_lock_shapes = []

    def recording_select_for_update(queryset, *args, **kwargs):
        lock_models.append(queryset.model)
        if queryset.model in {IngestionRun, DashboardSnapshot}:
            joined_lock_calls.append((queryset.model, kwargs.get("of")))
        if queryset.model is Observation:
            observation_lock_shapes.append(queryset.query.select_related)
        return original(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
    published = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert published is not None
    IngestionRun.objects.create(
        source=bls_run.source,
        dataset=INFLATION_BLS_DATASET,
        started_at=FIXED_NOW + timedelta(microseconds=1),
        completed_at=FIXED_NOW + timedelta(microseconds=1),
        status=IngestionRun.Status.FAILED,
        error="lock-path fixture",
    )
    coordinate_inflation_dashboard()

    assert lock_models.index(bls_run.source.__class__) < lock_models.index(IngestionRun)
    assert joined_lock_calls
    assert all(of == ("self",) for _model, of in joined_lock_calls)
    assert observation_lock_shapes == [False, False]


@pytest.mark.django_db
def test_inflation_expired_success_rolls_back_and_retains_then_recovers(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, first_pio = _strict_inflation_runs(settings, tmp_path)
    baseline = publish_inflation_revision(bls_run=bls_run, bea_pio_run=first_pio)
    assert baseline is not None
    second_pio = _strict_pio_run(settings, tmp_path, cycle="pio-expired-success")
    snapshot_count = DashboardSnapshot.objects.filter(key="inflation").count()
    metric_count = MetricSnapshot.objects.filter(key__startswith="inflation-").count()
    expired_at = datetime.fromisoformat(baseline.data["fresh_until"]) + timedelta(seconds=1)
    monkeypatch.setattr("research.inflation_contract.timezone.now", lambda: expired_at)

    dashboards, stale = coordinate_inflation_dashboard([second_pio])

    assert dashboards == []
    assert stale == {"inflation"}
    assert DashboardSnapshot.objects.filter(key="inflation").count() == snapshot_count
    assert MetricSnapshot.objects.filter(key__startswith="inflation-").count() == metric_count
    retained = select_public_inflation_snapshot()
    assert retained is not None and retained.pk == baseline.pk
    assert retained.inflation_publication_state == "retained_failure"
    marker = retained.data["refresh_failure"]
    assert marker["reason_code"] == "publication-postcondition"
    assert marker["attempts"]["bls"]["ingestion_run_id"] == bls_run.pk
    assert marker["attempts"]["bea-pio"]["ingestion_run_id"] == second_pio.pk

    monkeypatch.setattr("research.inflation_contract.timezone.now", lambda: FIXED_NOW)
    recovered_pio = _strict_pio_run(settings, tmp_path, cycle="pio-success-recovery")
    transitioning = select_public_inflation_snapshot()
    assert transitioning is not None
    assert transitioning.inflation_publication_state == "transition_pending"
    assert "refresh_failure" not in transitioning.data
    recovered, stale = coordinate_inflation_dashboard([recovered_pio])
    assert len(recovered) == 1 and stale == set()
    assert select_public_inflation_snapshot().pk == recovered[0].pk


@pytest.mark.django_db
def test_inflation_same_pair_natural_expiry_is_idempotent_and_write_free(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    baseline = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert baseline is not None
    stored_data = deepcopy(baseline.data)
    stored_updated_at = baseline.updated_at
    snapshot_count = DashboardSnapshot.objects.filter(key="inflation").count()
    metric_count = MetricSnapshot.objects.filter(key__startswith="inflation-").count()
    expired_at = datetime.fromisoformat(baseline.data["fresh_until"]) + timedelta(seconds=1)
    monkeypatch.setattr("research.inflation_contract.timezone.now", lambda: expired_at)

    with CaptureQueriesContext(connection) as captured:
        dashboards, stale = coordinate_inflation_dashboard([bls_run, pio_run])

    assert dashboards == [] and stale == {"inflation"}
    write_queries = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert write_queries == []
    assert DashboardSnapshot.objects.filter(key="inflation").count() == snapshot_count
    assert MetricSnapshot.objects.filter(key__startswith="inflation-").count() == metric_count
    baseline.refresh_from_db()
    assert baseline.data == stored_data
    assert baseline.updated_at == stored_updated_at
    selected = select_public_inflation_snapshot()
    assert selected is not None
    assert selected.inflation_publication_state == "natural_expiry"
    assert "refresh_failure" not in selected.data


@pytest.mark.django_db
def test_inflation_builder_runtime_rolls_back_retains_and_recovers(
    monkeypatch,
    settings,
    tmp_path,
):
    from research import inflation_contract

    bls_run, first_pio = _strict_inflation_runs(settings, tmp_path)
    baseline = publish_inflation_revision(bls_run=bls_run, bea_pio_run=first_pio)
    assert baseline is not None
    second_pio = _strict_pio_run(settings, tmp_path, cycle="pio-runtime-failure")
    original_builder = inflation_contract._build_inflation_payload

    def fail_new_pair(evidence, **kwargs):
        if evidence.bea_pio.run.pk == second_pio.pk:
            raise RuntimeError("inflation builder runtime fixture")
        return original_builder(evidence, **kwargs)

    monkeypatch.setattr(inflation_contract, "_build_inflation_payload", fail_new_pair)
    snapshot_count = DashboardSnapshot.objects.filter(key="inflation").count()
    metric_count = MetricSnapshot.objects.filter(key__startswith="inflation-").count()

    dashboards, stale = coordinate_inflation_dashboard([second_pio])

    assert dashboards == [] and stale == {"inflation"}
    assert DashboardSnapshot.objects.filter(key="inflation").count() == snapshot_count
    assert MetricSnapshot.objects.filter(key__startswith="inflation-").count() == metric_count
    retained = select_public_inflation_snapshot()
    assert retained is not None and retained.pk == baseline.pk
    assert retained.inflation_publication_state == "retained_failure"
    assert retained.data["refresh_failure"]["reason_code"] == (
        "publication-postcondition"
    )
    assert "RuntimeError" not in retained.data["refresh_failure"]["reason"]
    assert "inflation builder runtime fixture" in retained.data["refresh_failure"][
        "reason"
    ]

    monkeypatch.setattr(
        inflation_contract, "_build_inflation_payload", original_builder
    )
    recovered_pio = _strict_pio_run(settings, tmp_path, cycle="pio-runtime-recovery")
    transitioning = select_public_inflation_snapshot()
    assert transitioning is not None
    assert transitioning.inflation_publication_state == "transition_pending"
    recovered, stale = coordinate_inflation_dashboard([recovered_pio])
    assert len(recovered) == 1 and stale == set()
    assert select_public_inflation_snapshot().pk == recovered[0].pk


@pytest.mark.django_db
def test_inflation_states_cover_expiry_transition_timeout_failure_and_recovery(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    published = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert published is not None

    expired_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    monkeypatch.setattr("research.inflation_contract.timezone.now", lambda: expired_at)
    expired = select_public_inflation_snapshot()
    assert expired is not None
    assert expired.inflation_publication_state == "natural_expiry"

    running = IngestionRun.objects.create(
        source=bls_run.source,
        dataset=INFLATION_BLS_DATASET,
        started_at=expired_at - timedelta(minutes=10),
        status=IngestionRun.Status.RUNNING,
        metadata={"refresh_cycle_id": "bls-running"},
    )
    transition = select_public_inflation_snapshot()
    assert transition is not None
    assert transition.inflation_publication_state == "transition_pending"
    running.started_at = expired_at - timedelta(hours=3)
    running.save(update_fields=["started_at", "updated_at"])
    assert select_public_inflation_snapshot() is None

    running.delete()
    monkeypatch.setattr("research.inflation_contract.timezone.now", lambda: FIXED_NOW)
    failed = record_provider_result(
        ProviderResult.failure("bls", INFLATION_BLS_DATASET, "upstream failure")
    )
    before_marker = select_public_inflation_snapshot()
    assert before_marker is not None
    assert before_marker.inflation_publication_state == "transition_pending"
    dashboards, stale = coordinate_inflation_dashboard([failed])
    assert dashboards == []
    assert stale == {"inflation"}
    retained = select_public_inflation_snapshot()
    assert retained is not None
    assert retained.pk == published.pk
    assert retained.inflation_publication_state == "retained_failure"
    attempts = retained.data["refresh_failure"]["attempts"]
    assert attempts["bls"]["ingestion_run_id"] == failed.pk
    assert attempts["bea-pio"]["ingestion_run_id"] == pio_run.pk
    retained_response = client.get("/economy/inflation/")
    assert retained_response.context["inflation_state"] == "retained_failure"
    assert all(
        metric["quality_status"] == "stale"
        for metric in retained_response.context["metrics"]
    )
    assert all(
        chart["quality_status"] == "stale"
        for chart in retained_response.context["charts"]
    )
    assert all(
        section["quality_status"] == "stale"
        for section in retained_response.context["sections"]
    )
    published.refresh_from_db()
    assert all(
        metric["quality_status"] != "stale"
        for metric in published.data["metrics"]
    )

    recovered_bls = _strict_bls_run(settings, tmp_path, cycle="bls-recovered")
    recovered, stale = coordinate_inflation_dashboard([recovered_bls])
    assert stale == set()
    assert len(recovered) == 1
    selected = select_public_inflation_snapshot()
    assert selected is not None
    assert selected.pk == recovered[0].pk
    assert selected.inflation_publication_state == "current_candidate"


@pytest.mark.parametrize(
    "treasury_state",
    ["transition_pending", "natural_expiry", "retained_failure"],
)
@pytest.mark.django_db
def test_inflation_overlay_requires_current_real_rates_state(
    monkeypatch,
    treasury_state,
):
    real_rates = _real_rates_snapshot()
    real_rates.treasury_publication_state = treasury_state
    monkeypatch.setattr(
        "research.official_data.select_public_treasury_curve_snapshot",
        lambda page_key: real_rates if page_key == "real-rates" else None,
    )

    assert _inflation_market_expectations_from_real_rates() == ([], [], [])


@pytest.mark.django_db
def test_inflation_overlay_rejects_expired_component_even_when_selector_is_current(
    monkeypatch,
):
    real_rates = _real_rates_snapshot()
    data = deepcopy(real_rates.data)
    data["metrics"][0]["fresh_until"] = "2026-07-14T00:00:00+00:00"
    real_rates.data = data
    monkeypatch.setattr(
        "research.official_data.select_public_treasury_curve_snapshot",
        lambda page_key: real_rates if page_key == "real-rates" else None,
    )

    assert _inflation_market_expectations_from_real_rates() == ([], [], [])


@pytest.mark.django_db
def test_inflation_stale_base_preserves_current_overlay_quality(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    baseline = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert baseline is not None
    failed = record_provider_result(
        ProviderResult.failure("bls", INFLATION_BLS_DATASET, "fixture failure")
    )
    coordinate_inflation_dashboard([failed])
    real_rates = _real_rates_snapshot()
    monkeypatch.setattr(
        "research.official_data.select_public_treasury_curve_snapshot",
        lambda page_key: real_rates if page_key == "real-rates" else None,
    )

    response = client.get("/economy/inflation/", {"tab": "expectations"})

    assert response.status_code == 200
    metrics = {item["key"]: item for item in response.context["metrics"]}
    assert all(
        metrics[key]["quality_status"] == Observation.Quality.STALE
        for key in INFLATION_REQUIRED_METRIC_KEYS
    )
    assert metrics["market-5y-bei"]["quality_status"] == Observation.Quality.ESTIMATED
    assert metrics["market-10y-bei"]["quality_status"] == Observation.Quality.ESTIMATED
    assert metrics["market-5y-bei"]["component_reference"][
        "component_snapshot_id"
    ] == real_rates.pk
    assert response.context["charts"][0]["quality_status"] == Observation.Quality.ESTIMATED
    sections = {item["key"]: item for item in response.context["sections"]}
    assert all(
        sections[key]["quality_status"] == Observation.Quality.STALE
        for key in INFLATION_REQUIRED_SECTION_KEYS
    )
    assert sections["market-breakeven-methodology"]["status"] == (
        Observation.Quality.ESTIMATED
    )
    assert sections["market-breakeven-methodology"]["quality_status"] == (
        Observation.Quality.ESTIMATED
    )


@pytest.mark.django_db
def test_inflation_route_keeps_base_snapshot_immutable_and_adds_audited_overlay(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    snapshot = publish_inflation_revision(bls_run=bls_run, bea_pio_run=pio_run)
    assert snapshot is not None
    base_data = deepcopy(snapshot.data)
    real_rates = _real_rates_snapshot()
    monkeypatch.setattr(
        "research.official_data.select_public_treasury_curve_snapshot",
        lambda page_key: real_rates if page_key == "real-rates" else None,
    )

    with CaptureQueriesContext(connection) as captured:
        response = client.get(
            "/economy/inflation/", {"period": "1y", "tab": "expectations"}
        )
    writes = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes == []
    assert response.status_code == 200
    assert response.context["selected_period"] == "1y"
    assert response.context["selected_tab"] == "expectations"
    assert [item["key"] for item in response.context["charts"]] == [
        "market-breakeven-inflation"
    ]
    overlay = response.context["charts"][0]["component_reference"]
    assert overlay["component_snapshot_id"] == real_rates.pk
    assert overlay["component_publication_batch_id"] == str(real_rates.batch_id)
    assert overlay["component_fingerprint"] == "a" * 64
    assert overlay["component_payload_integrity_hash"] == "b" * 64
    assert overlay["component_contract_version"] == 2
    assert overlay["component_formula_version"] == TREASURY_CURVE_FORMULA_VERSION
    assert set(overlay["component_roles"]) == {"nominal", "real"}
    assert len(overlay["source_roles"]) == 2
    snapshot.refresh_from_db()
    assert snapshot.data == base_data
    assert len(snapshot.data["metrics"]) == 32
    assert len(snapshot.data["charts"]) == 8
    assert len(snapshot.data["sections"]) == 2

    producer = client.get("/economy/inflation/", {"period": "1y", "tab": "producer"})
    assert [item["key"] for item in producer.context["charts"]] == [
        "final-demand-ppi-rates"
    ]
    body = html.unescape(producer.content.decode())
    assert "period=3y&tab=producer" in body
    assert "period=1y&tab=pce" in body
    assert "U.S. Bureau of Labor Statistics" in body

    expected_tabs = {
        "overview": set(INFLATION_REQUIRED_CHART_KEYS) | {"market-breakeven-inflation"},
        "headline": {"headline-cpi-rates"},
        "core": {"core-cpi-rates"},
        "components": {
            "shelter-cpi-rates",
            "core-goods-cpi-rates",
            "services-less-energy-cpi-rates",
        },
        "producer": {"final-demand-ppi-rates"},
        "pce": {"pce-price-rates", "core-pce-price-rates"},
        "expectations": {"market-breakeven-inflation"},
    }
    for tab, expected_keys in expected_tabs.items():
        tab_response = client.get(
            "/economy/inflation/", {"period": "1y", "tab": tab}
        )
        assert tab_response.context["selected_tab"] == tab
        assert {item["key"] for item in tab_response.context["charts"]} == expected_keys
    headline = client.get(
        "/economy/inflation/", {"period": "1y", "tab": "headline"}
    ).context["charts"][0]
    headline_dates = [date.fromisoformat(row["date"]) for row in headline["data"]]
    assert headline_dates[0] == date(2025, 6, 1)
    assert headline_dates[-1] == date(2026, 6, 1)
    invalid = client.get(
        "/economy/inflation/",
        {"period": "<script>alert(1)</script>", "tab": "missing"},
    )
    assert invalid.context["selected_period"] == "3y"
    assert invalid.context["selected_tab"] == "overview"
    assert "<script>alert(1)</script>" not in invalid.content.decode()

    # Missing overlay provenance fails closed instead of fabricating fields.
    broken = deepcopy(real_rates.data)
    broken.pop("payload_integrity_hash")
    real_rates.data = broken
    real_rates.save(update_fields=["data", "updated_at"])
    unavailable = client.get("/economy/inflation/", {"tab": "expectations"})
    assert "market-breakeven-inflation" not in {
        item.get("key") for item in unavailable.context["charts"]
    }


@pytest.mark.django_db
def test_inflation_october_gap_never_backfills_exact_six_month_denominator(
    settings,
    tmp_path,
):
    bls_run, pio_run = _strict_inflation_runs(settings, tmp_path)
    Observation.objects.get(
        batch_id=bls_run.batch_id,
        series__key="cusr0000sa0",
        value_date__date=date(2025, 10, 1),
    ).delete()

    _metrics, charts, _sections = _inflation_page_data(
        batch_id=bls_run.batch_id,
        bea_pio_batch_id=pio_run.batch_id,
        apply_freshness=False,
        include_market_overlay=False,
    )
    headline = next(item for item in charts if item["key"] == "headline-cpi-rates")
    april = next(row for row in headline["data"] if row["date"] == "2026-04-01")
    assert "CPI 3M 年化" in april
    assert "CPI 6M 年化" not in april


def test_inflation_catalog_keeps_release_vintage_gap_explicit():
    requirements = {
        item["key"]: item
        for item in DATA_REQUIREMENTS
        if item["page_key"] == "inflation"
    }
    assert requirements["bls-inflation-official"]["status"] == "live"
    assert requirements["bea-pce-inflation"]["status"] == "live"
    assert requirements["bls-inflation-components"]["status"] == "live"
    assert requirements["inflation-market-expectations"]["status"] == "live"
    assert requirements["inflation-vintage-trail"]["status"] == "needs_source"
