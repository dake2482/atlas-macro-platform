from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_consumer_credit import (
    _g19_client,
    _hhdc_client,
    _hhdc_workbook,
)
from test_macro_official import _census_history_payload, _client
from test_macro_releases import (
    _bea_pio_client,
    _bea_pio_section2_workbook,
    _bea_pio_summary_workbook,
    _census_client,
    _census_current_workbook,
)

import research.consumer_contract as consumer_contract
from research.consumer_contract import (
    CONSUMER_FORMULA_VERSION,
    CONSUMER_REQUIRED_CHART_KEYS,
    CONSUMER_REQUIRED_METRIC_KEYS,
    CONSUMER_REQUIRED_SECTION_KEYS,
    _validate_run,
    coordinate_consumer_dashboard,
    publish_consumer_revision,
    select_public_consumer_snapshot,
)
from research.consumer_credit import (
    FederalReserveG19Provider,
    NYFedHouseholdDebtProvider,
)
from research.data_catalog import DATA_REQUIREMENTS
from research.economy_contract import (
    _component_payload as _strict_economy_component_payload,
)
from research.macro_official import CensusMARTSProvider
from research.macro_releases import (
    CENSUS_MARTS_CURRENT_WORKBOOK,
    CENSUS_MARTS_INDEX,
    XLSX_CONTENT_TYPE,
    BEAPIOReleaseProvider,
    CensusMARTSReleaseProvider,
)
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    SourceLicense,
)
from research.official_data import (
    _store_bea_release_observations_v2,
    _store_census_marts_observations_v2,
    _store_consumer_credit_observations_v2,
    publish_official_dashboards,
)
from research.page_registry import PAGE_CONFIGS
from research.raw_evidence import EvidenceResponse, build_evidence_bundle, parse_evidence_bundle
from research.services import begin_ingestion, finish_ingestion, record_provider_result

FROZEN_NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)


def _retime_evidence_result(result, provider, retrieved_at: datetime):
    evidence = parse_evidence_bundle(
        result.raw_bytes,
        expected_provider=result.provider,
        expected_dataset=result.dataset,
    )
    responses = []
    for entry in evidence.manifest["responses"]:
        response_witness = deepcopy(entry["response_witness"])
        response_witness["retrieved_at"] = retrieved_at.isoformat()
        responses.append(
            EvidenceResponse(
                role=entry["role"],
                url=entry["url"],
                content_type=entry["content_type"],
                raw_bytes=evidence.responses[entry["role"]],
                request_witness=entry["request_witness"],
                response_witness=response_witness,
            )
        )
    raw_bytes, bundle_metadata = build_evidence_bundle(
        provider=result.provider,
        dataset=result.dataset,
        responses=responses,
    )
    replay = provider.replay_evidence_bundle(raw_bytes)
    replay_records, replay_metadata = replay
    result.raw_bytes = raw_bytes
    result.records = replay_records
    result.fetched_at = retrieved_at
    result.metadata = {
        **dict(result.metadata or {}),
        **bundle_metadata,
        **replay_metadata,
    }
    return result


def _census_api_result(*, mismatched_overlap: bool = False):
    payload = deepcopy(_census_history_payload())
    for row in payload[1:]:
        if row[3] == "2025-04":
            row[1] = "722300"
        if mismatched_overlap and row[3] == "2026-05":
            row[1] = "763700"

    def handler(_request):
        return httpx.Response(200, json=payload)

    result = CensusMARTSProvider(
        api_key="consumer-test-key",
        client=_client(handler),
    ).monthly_retail_sales(time="from 1992", require_complete_history=True)
    evidence = parse_evidence_bundle(
        result.raw_bytes,
        expected_provider="census",
        expected_dataset="marts:44X72:SM:yes",
    )
    entry = evidence.manifest["responses"][0]
    raw_bytes, bundle_metadata = build_evidence_bundle(
        provider="census",
        dataset="marts:44X72:SM:yes",
        responses=(
            EvidenceResponse(
                role=entry["role"],
                url=entry["url"],
                content_type=entry["content_type"],
                raw_bytes=evidence.responses[entry["role"]],
                request_witness=entry["request_witness"],
                response_witness={"retrieved_at": FROZEN_NOW.isoformat()},
            ),
        ),
    )
    records, replay_metadata = CensusMARTSProvider.replay_evidence_bundle(
        raw_bytes,
        expected_dataset="marts:44X72:SM:yes",
    )
    result.raw_bytes = raw_bytes
    result.records = records
    result.fetched_at = FROZEN_NOW
    result.metadata = {
        **dict(result.metadata or {}),
        **bundle_metadata,
        **replay_metadata,
    }
    return result


def _recent_release_result():
    workbook = _census_current_workbook()
    anchor = FROZEN_NOW
    candidates = CensusMARTSReleaseProvider._recent_archive_candidates(anchor)
    may_url = next(url for month, url in candidates if month == "2026-05")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == CENSUS_MARTS_CURRENT_WORKBOOK:
            return httpx.Response(403, text="current workbook unavailable")
        if url == may_url:
            return httpx.Response(
                200,
                content=workbook,
                headers={"content-type": XLSX_CONTENT_TYPE},
            )
        if url.startswith(CENSUS_MARTS_INDEX) and url.endswith(".xlsx"):
            return httpx.Response(404, text="not yet published")
        raise AssertionError(f"unexpected URL: {url}")

    return CensusMARTSReleaseProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        )
    ).monthly_retail_sales()


def _persist_inputs(
    monkeypatch,
    settings,
    tmp_path,
    *,
    include_api: bool = False,
    recent_release: bool = False,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    monkeypatch.setattr(consumer_contract.timezone, "now", lambda: FROZEN_NOW)
    monkeypatch.setattr(
        CensusMARTSReleaseProvider,
        "_utc_now",
        staticmethod(lambda: FROZEN_NOW),
    )
    retail_result = (
        _recent_release_result()
        if recent_release
        else CensusMARTSReleaseProvider(
            client=_census_client(_census_current_workbook(), current_status=200)
        ).monthly_retail_sales()
    )
    pio_result = BEAPIOReleaseProvider(
        client=_bea_pio_client(
            _bea_pio_summary_workbook(),
            _bea_pio_section2_workbook(),
        )
    ).personal_income_outlays()
    pio_result.fetched_at = FROZEN_NOW
    g19_result = _retime_evidence_result(
        FederalReserveG19Provider(client=_g19_client()).consumer_credit(),
        FederalReserveG19Provider,
        FROZEN_NOW,
    )
    hhdc_result = _retime_evidence_result(
        NYFedHouseholdDebtProvider(
            client=_hhdc_client(_hhdc_workbook())
        ).household_debt(),
        NYFedHouseholdDebtProvider,
        FROZEN_NOW,
    )
    runs = {
        "retail_release": record_provider_result(
            retail_result,
            persist=_store_census_marts_observations_v2,
        ),
        "pio": record_provider_result(
            pio_result,
            persist=_store_bea_release_observations_v2,
        ),
        "g19": record_provider_result(
            g19_result,
            persist=_store_consumer_credit_observations_v2,
        ),
        "hhdc": record_provider_result(
            hhdc_result,
            persist=_store_consumer_credit_observations_v2,
        ),
    }
    if include_api:
        runs["retail_history_api"] = record_provider_result(
            _census_api_result(),
            persist=_store_census_marts_observations_v2,
        )
    assert all(run.status == IngestionRun.Status.SUCCESS for run in runs.values())
    return runs


def _publish(runs):
    return publish_consumer_revision(
        retail_release_run=runs["retail_release"],
        pio_run=runs["pio"],
        g19_run=runs["g19"],
        hhdc_run=runs["hhdc"],
        retail_history_api_run=runs.get("retail_history_api"),
    )


def _database_signature():
    return (
        list(
            DashboardSnapshot.objects.order_by("pk").values_list(
                "pk",
                "updated_at",
                "quality_status",
                "batch_id",
                "data",
            )
        ),
        list(
            MetricSnapshot.objects.order_by("pk").values_list(
                "pk",
                "updated_at",
                "batch_id",
                "metadata",
            )
        ),
    )


def test_consumer_registry_v2_has_only_empty_cards_and_procurement_boundary():
    config = PAGE_CONFIGS["consumer"]

    assert config["snapshot_contract_version"] == 2
    assert len(config["metrics"]) == 14
    assert all(item["value"] is None for item in config["metrics"])
    assert all(item["display_value"] == "—" for item in config["metrics"])
    assert all("confidence" not in item for item in config["metrics"])
    assert config.get("chart_data", []) == []
    serialized = json.dumps(config, ensure_ascii=False)
    assert "68.2" not in serialized
    assert "+0.4" not in serialized
    assert "3.9" not in serialized
    assert "低收入群体压力" not in serialized

    requirement = next(
        item for item in DATA_REQUIREMENTS if item["key"] == "consumer-confidence"
    )
    assert requirement["status"] == "purchase_required"


@pytest.mark.django_db
def test_consumer_v2_happy_path_exact_contract_selector_route_and_get_zero_writes(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    runs = _persist_inputs(monkeypatch, settings, tmp_path)
    snapshot = _publish(runs)

    assert snapshot is not None
    assert snapshot.data["formula_version"] == CONSUMER_FORMULA_VERSION
    assert {item["key"] for item in snapshot.data["metrics"]} == CONSUMER_REQUIRED_METRIC_KEYS
    assert {item["key"] for item in snapshot.data["charts"]} == CONSUMER_REQUIRED_CHART_KEYS
    assert {item["key"] for item in snapshot.data["sections"]} == CONSUMER_REQUIRED_SECTION_KEYS
    assert snapshot.data["retail_history_coverage"]["status"] == "release_only"
    assert snapshot.data["optional_history_attempt"] is None
    assert len(snapshot.data["input_runs"]) == 4
    assert MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).count() == 14
    selected = select_public_consumer_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.consumer_publication_state == "current_candidate"
    metric, chart, reference, metric_row = _strict_economy_component_payload(
        "consumer",
        snapshot,
    )
    assert metric["key"] == "bea-real-pce-mom"
    assert chart["key"] == "real-consumption-income-momentum"
    assert reference["snapshot_id"] == snapshot.pk
    assert reference["selected_metric_snapshot_id"] == metric_row.pk
    assert reference["root_source_keys"] == sorted(snapshot.data["source_keys"])
    assert reference["root_component_batches"] == sorted(
        snapshot.data["component_batches"]
    )
    assert reference["root_fresh_until"] == snapshot.data["fresh_until"]
    assert publish_official_dashboards(keys={"consumer"}) == []

    dashboard_signature = list(
        DashboardSnapshot.objects.order_by("pk").values_list("pk", "updated_at", "data")
    )
    metric_signature = list(
        MetricSnapshot.objects.order_by("pk").values_list("pk", "updated_at", "metadata")
    )
    response = client.get("/economy/consumer/")
    assert response.status_code == 200
    assert response.content.count(b" data-chart ") == 6
    assert list(
        DashboardSnapshot.objects.order_by("pk").values_list("pk", "updated_at", "data")
    ) == dashboard_signature
    assert list(
        MetricSnapshot.objects.order_by("pk").values_list("pk", "updated_at", "metadata")
    ) == metric_signature


@pytest.mark.django_db
def test_recent_archive_evidence_persists_and_publishes_consumer(
    monkeypatch,
    settings,
    tmp_path,
):
    runs = _persist_inputs(
        monkeypatch,
        settings,
        tmp_path,
        recent_release=True,
    )
    snapshot = _publish(runs)

    assert snapshot is not None
    assert snapshot.data["metrics"][0]["source_key"] == "census-release"
    assert runs["retail_release"].metadata["evidence_roles"] == [
        "current-workbook-failure",
        "recent-probe-01",
        "recent-probe-02",
    ]
    artifact = runs["retail_release"].artifacts.get()
    raw_bytes = (
        settings.RAW_ARTIFACT_ROOT
        / artifact.sha256[:2]
        / f"{artifact.sha256}.bin"
    ).read_bytes()
    evidence = parse_evidence_bundle(
        raw_bytes,
        expected_provider="census-release",
        expected_dataset="marts:retail-food-services",
    )
    final_entry = next(
        item
        for item in evidence.manifest["responses"]
        if item["role"] == "recent-probe-02"
    )
    final_retrieved_at = final_entry["response_witness"]["retrieved_at"]
    assert runs["retail_release"].metadata["fetched_at"] == final_retrieved_at
    assert runs["retail_release"].metadata["retrieved_at"] == final_retrieved_at
    assert set(
        Observation.objects.filter(batch_id=runs["retail_release"].batch_id).values_list(
            "fetched_at", flat=True
        )
    ) == {datetime.fromisoformat(final_retrieved_at)}


@pytest.mark.django_db
def test_optional_full_history_identity_is_exact_and_monotonic(
    monkeypatch,
    settings,
    tmp_path,
):
    runs = _persist_inputs(monkeypatch, settings, tmp_path, include_api=True)
    snapshot = _publish(runs)

    assert snapshot is not None
    identity = snapshot.data["publication_input_identity"]["optional_effective"]
    assert identity["status"] == "complete_history"
    assert identity["api_run_id"] == runs["retail_history_api"].pk
    assert identity["api_batch_id"] == str(runs["retail_history_api"].batch_id)
    assert identity["api_artifact_sha256"] == runs[
        "retail_history_api"
    ].artifacts.get().sha256

    before = (DashboardSnapshot.objects.count(), MetricSnapshot.objects.count())
    failed = finish_ingestion(
        begin_ingestion("census", "marts:44X72:SM:yes"),
        status=IngestionRun.Status.FAILED,
        error="optional source unavailable",
    )
    assert failed.status == IngestionRun.Status.FAILED
    assert coordinate_consumer_dashboard()[0] == []
    assert (DashboardSnapshot.objects.count(), MetricSnapshot.objects.count()) == before
    selected = select_public_consumer_snapshot()
    assert selected is not None
    assert selected.data["publication_input_identity"] == snapshot.data[
        "publication_input_identity"
    ]

    invalid = record_provider_result(
        _census_api_result(mismatched_overlap=True),
        persist=_store_census_marts_observations_v2,
    )
    assert invalid.status == IngestionRun.Status.SUCCESS
    assert coordinate_consumer_dashboard()[0] == []
    assert (DashboardSnapshot.objects.count(), MetricSnapshot.objects.count()) == before


@pytest.mark.django_db
def test_legacy_census_api_metadata_without_retrieved_at_replays_full_history(
    monkeypatch,
    settings,
    tmp_path,
):
    runs = _persist_inputs(monkeypatch, settings, tmp_path, include_api=True)
    api_run = runs["retail_history_api"]
    metadata = deepcopy(api_run.metadata)
    assert metadata.pop("retrieved_at") == FROZEN_NOW.isoformat()
    api_run.metadata = metadata
    api_run.save(update_fields=["metadata", "updated_at"])

    replayed = _validate_run("retail_history_api", api_run)
    assert replayed.run.pk == api_run.pk
    snapshot = _publish(runs)
    assert snapshot is not None
    assert snapshot.data["retail_history_coverage"]["status"] == "complete_history"
    assert snapshot.data["publication_input_identity"]["optional_effective"][
        "api_run_id"
    ] == api_run.pk


@pytest.mark.django_db
def test_legacy_census_api_timestamp_compatibility_remains_fail_closed(
    monkeypatch,
    settings,
    tmp_path,
):
    runs = _persist_inputs(monkeypatch, settings, tmp_path, include_api=True)
    api_run = runs["retail_history_api"]
    metadata = deepcopy(api_run.metadata)
    metadata.pop("retrieved_at")
    metadata["fetched_at"] = (FROZEN_NOW + timedelta(seconds=1)).isoformat()
    api_run.metadata = metadata
    api_run.save(update_fields=["metadata", "updated_at"])

    with pytest.raises(ValueError, match="final retrieval does not match fetched_at"):
        _validate_run("retail_history_api", api_run)

    metadata["fetched_at"] = FROZEN_NOW.isoformat()
    metadata["retrieved_at"] = (FROZEN_NOW + timedelta(seconds=1)).isoformat()
    api_run.metadata = metadata
    api_run.save(update_fields=["metadata", "updated_at"])
    with pytest.raises(ValueError, match="metadata does not replay from exact evidence"):
        _validate_run("retail_history_api", api_run)

    api_run.refresh_from_db()
    original_artifact = api_run.artifacts.get()
    original_payload = (
        settings.RAW_ARTIFACT_ROOT
        / original_artifact.sha256[:2]
        / f"{original_artifact.sha256}.bin"
    ).read_bytes()
    evidence = parse_evidence_bundle(
        original_payload,
        expected_provider="census",
        expected_dataset="marts:44X72:SM:yes",
    )
    entry = evidence.manifest["responses"][0]
    raw_bytes, bundle_metadata = build_evidence_bundle(
        provider="census",
        dataset="marts:44X72:SM:yes",
        responses=(
            EvidenceResponse(
                role=entry["role"],
                url=entry["url"],
                content_type=entry["content_type"],
                raw_bytes=evidence.responses[entry["role"]],
                request_witness=entry["request_witness"],
                response_witness={
                    "retrieved_at": (FROZEN_NOW - timedelta(minutes=1)).isoformat()
                },
            ),
        ),
    )
    target = (
        settings.RAW_ARTIFACT_ROOT
        / bundle_metadata["sha256"][:2]
        / f'{bundle_metadata["sha256"]}.bin'
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw_bytes)
    artifact = api_run.artifacts.get()
    artifact.sha256 = bundle_metadata["sha256"]
    artifact.uri = (
        f'private://census/{bundle_metadata["sha256"][:2]}/'
        f'{bundle_metadata["sha256"]}.bin'
    )
    artifact.size_bytes = bundle_metadata["byte_length"]
    artifact.save(
        update_fields=["sha256", "uri", "size_bytes", "updated_at"]
    )
    metadata = {
        **api_run.metadata,
        **bundle_metadata,
        "fetched_at": FROZEN_NOW.isoformat(),
        "raw_artifact_sha256": bundle_metadata["sha256"],
        "raw_artifact_uri": artifact.uri,
    }
    metadata.pop("retrieved_at", None)
    api_run.metadata = metadata
    api_run.save(update_fields=["metadata", "updated_at"])

    with pytest.raises(ValueError, match="final retrieval does not match fetched_at"):
        _validate_run("retail_history_api", api_run)


@pytest.mark.django_db
def test_consumer_freshness_uses_inclusive_calendar_end_of_day(
    monkeypatch,
    settings,
    tmp_path,
):
    snapshot = _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    freshness = snapshot.data["component_freshness"]

    assert freshness["retail_release"]["fresh_until"] == (
        "2026-07-15T23:59:59.999999+00:00"
    )
    assert freshness["hhdc"]["fresh_until"] == "2026-07-29T23:59:59.999999+00:00"
    assert freshness["pio"]["fresh_until"] == "2026-08-09T00:00:00+00:00"
    assert freshness["g19"]["fresh_until"] == "2026-08-22T00:00:00+00:00"
    role_by_metric = {
        **{
            key: "retail_release"
            for key in (
                "census-mrts-44x72-sm-sa",
                "census-mrts-44x72-sm-sa-mom",
                "census-mrts-44x72-sm-sa-yoy",
            )
        },
        **{
            key: "pio"
            for key in (
                "bea-real-pce-mom",
                "bea-personal-saving-rate",
                "bea-real-dpi-mom",
            )
        },
        **{
            key: "g19"
            for key in (
                "g19-consumer-credit-outstanding-sa",
                "g19-consumer-credit-growth-saar",
                "g19-revolving-credit-growth-saar",
                "g19-nonrevolving-credit-growth-saar",
            )
        },
        **{
            key: "hhdc"
            for key in (
                "hhdc-total-debt-balance",
                "hhdc-credit-card-balance",
                "hhdc-all-90d-delinquent",
                "hhdc-credit-card-90d-delinquent",
            )
        },
    }
    assert {
        metric["key"]: metric["fresh_until"] for metric in snapshot.data["metrics"]
    } == {
        key: freshness[role]["fresh_until"] for key, role in role_by_metric.items()
    }
    chart_roles = {
        "retail-sales": "retail_release",
        "real-consumption-income-momentum": "pio",
        "personal-saving-rate": "pio",
        "consumer-credit-composition": "g19",
        "household-debt-composition": "hhdc",
        "household-debt-delinquency": "hhdc",
    }
    assert {
        chart["key"]: chart["fresh_until"] for chart in snapshot.data["charts"]
    } == {
        key: freshness[role]["fresh_until"] for key, role in chart_roles.items()
    }


@pytest.mark.django_db
def test_release_only_upgrades_once_per_exact_valid_optional_run(
    monkeypatch,
    settings,
    tmp_path,
):
    release_snapshot = _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    assert release_snapshot.data["retail_history_coverage"]["status"] == "release_only"
    before_dashboards = DashboardSnapshot.objects.count()
    before_metrics = MetricSnapshot.objects.count()

    first_api = record_provider_result(
        _census_api_result(),
        persist=_store_census_marts_observations_v2,
    )
    published, missing = coordinate_consumer_dashboard()

    assert missing == set()
    assert len(published) == 1
    first_full = published[0]
    assert first_full.data["retail_history_coverage"]["status"] == "complete_history"
    assert first_full.data["publication_input_identity"]["optional_effective"][
        "api_run_id"
    ] == first_api.pk
    assert DashboardSnapshot.objects.count() == before_dashboards + 1
    assert MetricSnapshot.objects.count() == before_metrics + 14
    assert coordinate_consumer_dashboard() == ([], set())

    second_api = record_provider_result(
        _census_api_result(),
        persist=_store_census_marts_observations_v2,
    )
    republished, missing = coordinate_consumer_dashboard()

    assert missing == set()
    assert len(republished) == 1
    assert republished[0].data["publication_input_identity"]["optional_effective"][
        "api_run_id"
    ] == second_api.pk
    assert second_api.pk != first_api.pk
    assert DashboardSnapshot.objects.count() == before_dashboards + 2
    assert MetricSnapshot.objects.count() == before_metrics + 28


@pytest.mark.django_db
def test_natural_expiry_blocks_valid_and_partial_optional_publication(
    monkeypatch,
    settings,
    tmp_path,
):
    snapshot = _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    expired_now = FROZEN_NOW + timedelta(days=1)
    monkeypatch.setattr(consumer_contract.timezone, "now", lambda: expired_now)

    selected = select_public_consumer_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.consumer_publication_state == "natural_expiry"
    before = _database_signature()

    valid_api = record_provider_result(
        _census_api_result(),
        persist=_store_census_marts_observations_v2,
    )
    assert valid_api.status == IngestionRun.Status.SUCCESS
    assert coordinate_consumer_dashboard() == ([], {"consumer"})
    assert _database_signature() == before

    partial_api = finish_ingestion(
        begin_ingestion("census", "marts:44X72:SM:yes"),
        status=IngestionRun.Status.PARTIAL,
        row_count=1,
        error="optional history incomplete",
    )
    assert partial_api.status == IngestionRun.Status.PARTIAL
    assert coordinate_consumer_dashboard() == ([], {"consumer"})
    assert _database_signature() == before


@pytest.mark.django_db
def test_optional_publication_postcondition_failure_rolls_back_atomically(
    monkeypatch,
    settings,
    tmp_path,
):
    _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    record_provider_result(
        _census_api_result(),
        persist=_store_census_marts_observations_v2,
    )
    before = _database_signature()
    real_selector = consumer_contract.select_public_consumer_snapshot
    calls = 0

    def fail_second_selector(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return None
        return real_selector(*args, **kwargs)

    monkeypatch.setattr(
        consumer_contract,
        "select_public_consumer_snapshot",
        fail_second_selector,
    )

    assert coordinate_consumer_dashboard() == ([], set())
    assert calls == 2
    assert _database_signature() == before
    selected = real_selector()
    assert selected is not None
    assert selected.consumer_publication_state == "current_candidate"
    assert selected.data.get("refresh_failure") is None


@pytest.mark.django_db
def test_running_timeout_mixed_state_marker_mutation_and_recovery(
    monkeypatch,
    settings,
    tmp_path,
):
    initial = _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    running_release = begin_ingestion(
        "census-release",
        "marts:retail-food-services",
    )
    running_pio = begin_ingestion(
        "bea-pio-release",
        "personal-income-outlays-release",
    )
    IngestionRun.objects.filter(pk__in=[running_release.pk, running_pio.pk]).update(
        started_at=FROZEN_NOW
    )
    running_release.refresh_from_db()
    running_pio.refresh_from_db()
    transition_now = FROZEN_NOW + timedelta(hours=1)
    monkeypatch.setattr(consumer_contract.timezone, "now", lambda: transition_now)
    before_counts = (
        DashboardSnapshot.objects.count(),
        MetricSnapshot.objects.count(),
    )

    assert coordinate_consumer_dashboard() == ([], {"consumer"})
    pending = select_public_consumer_snapshot()
    assert pending is not None
    assert pending.pk == initial.pk
    assert pending.consumer_publication_state == "transition_pending"
    assert (
        DashboardSnapshot.objects.count(),
        MetricSnapshot.objects.count(),
    ) == before_counts

    timeout_now = FROZEN_NOW + timedelta(hours=2)
    monkeypatch.setattr(consumer_contract.timezone, "now", lambda: timeout_now)
    assert coordinate_consumer_dashboard() == ([], {"consumer"})
    retained = select_public_consumer_snapshot()
    assert retained is not None
    assert retained.pk == initial.pk
    assert retained.consumer_publication_state == "retained_failure"
    assert retained.quality_status == "stale"
    marker = retained.data["refresh_failure"]
    assert marker["reason_code"] == "latest-attempt-incomplete"
    assert marker["attempts"]["retail_release"]["status"] == "running"
    assert marker["attempts"]["pio"]["ingestion_run_id"] == running_pio.pk
    assert marker["attempts"]["pio"]["status"] == "running"
    assert (
        DashboardSnapshot.objects.count(),
        MetricSnapshot.objects.count(),
    ) == before_counts

    running_release.status = IngestionRun.Status.SUCCESS
    running_release.row_count = 1
    running_release.completed_at = timeout_now
    running_release.save(
        update_fields=["status", "row_count", "completed_at", "updated_at"]
    )
    assert select_public_consumer_snapshot() is None

    _persist_inputs(monkeypatch, settings, tmp_path)
    recovered, missing = coordinate_consumer_dashboard()
    assert missing == set()
    assert len(recovered) == 1
    current = select_public_consumer_snapshot()
    assert current is not None
    assert current.pk == recovered[0].pk
    assert current.consumer_publication_state == "current_candidate"
    assert current.data.get("refresh_failure") is None


@pytest.mark.django_db
def test_terminal_failure_with_fresh_running_attempt_is_retained_immediately(
    monkeypatch,
    settings,
    tmp_path,
):
    initial = _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    failed_release = finish_ingestion(
        begin_ingestion("census-release", "marts:retail-food-services"),
        status=IngestionRun.Status.FAILED,
        error="official workbook rejected",
    )
    fresh_running = begin_ingestion(
        "bea-pio-release",
        "personal-income-outlays-release",
    )
    IngestionRun.objects.filter(pk=failed_release.pk).update(
        started_at=FROZEN_NOW,
        completed_at=FROZEN_NOW,
    )
    IngestionRun.objects.filter(pk=fresh_running.pk).update(started_at=FROZEN_NOW)
    marker_now = FROZEN_NOW + timedelta(minutes=10)
    monkeypatch.setattr(consumer_contract.timezone, "now", lambda: marker_now)
    before_counts = (
        DashboardSnapshot.objects.count(),
        MetricSnapshot.objects.count(),
    )

    assert coordinate_consumer_dashboard() == ([], {"consumer"})
    selected = select_public_consumer_snapshot()
    assert selected is not None
    assert selected.pk == initial.pk
    assert selected.consumer_publication_state == "retained_failure"
    marker = selected.data["refresh_failure"]
    assert marker["attempts"]["retail_release"]["ingestion_run_id"] == failed_release.pk
    assert marker["attempts"]["retail_release"]["status"] == "failed"
    assert marker["attempts"]["pio"]["ingestion_run_id"] == fresh_running.pk
    assert marker["attempts"]["pio"]["status"] == "running"
    assert (
        DashboardSnapshot.objects.count(),
        MetricSnapshot.objects.count(),
    ) == before_counts


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tamper",
    ("payload-value", "payload-lineage", "metric-row", "artifact-hash"),
)
def test_consumer_selector_fails_closed_on_payload_lineage_row_and_artifact_tamper(
    monkeypatch,
    settings,
    tmp_path,
    tamper,
):
    runs = _persist_inputs(monkeypatch, settings, tmp_path)
    snapshot = _publish(runs)

    if tamper == "payload-value":
        data = deepcopy(snapshot.data)
        data["metrics"][0]["value"] += 1
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif tamper == "payload-lineage":
        data = deepcopy(snapshot.data)
        data["metrics"][0]["metadata"]["input_lineage"][0]["batch_id"] = (
            "00000000-0000-0000-0000-000000000000"
        )
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif tamper == "metric-row":
        row = MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).first()
        metadata = deepcopy(row.metadata)
        metadata["input_batch_ids"] = ["00000000-0000-0000-0000-000000000000"]
        row.metadata = metadata
        row.save(update_fields=["metadata", "updated_at"])
    else:
        artifact = runs["g19"].artifacts.get()
        artifact.sha256 = "0" * 64
        artifact.save(update_fields=["sha256", "updated_at"])

    assert select_public_consumer_snapshot() is None


@pytest.mark.django_db
def test_consumer_selector_rejects_revoked_source_license(
    monkeypatch,
    settings,
    tmp_path,
):
    _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    licence = SourceLicense.objects.get(
        source__key="census-release",
        is_current=True,
    )
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])

    assert select_public_consumer_snapshot() is None


@pytest.mark.django_db
def test_retained_consumer_route_keeps_exact_14_metrics_and_6_charts(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    initial = _publish(_persist_inputs(monkeypatch, settings, tmp_path))
    failed = finish_ingestion(
        begin_ingestion("census-release", "marts:retail-food-services"),
        status=IngestionRun.Status.FAILED,
        error="official release validation failed",
    )

    assert coordinate_consumer_dashboard() == ([], {"consumer"})
    selected = select_public_consumer_snapshot()
    assert selected is not None
    assert selected.pk == initial.pk
    assert selected.consumer_publication_state == "retained_failure"
    assert selected.data["refresh_failure"]["attempts"]["retail_release"][
        "ingestion_run_id"
    ] == failed.pk

    response = client.get("/economy/consumer/")
    assert response.status_code == 200
    assert response.content.count(b'class="metric-card"') == 14
    assert response.content.count(b" data-chart ") == 6
    assert "数据已过期" in response.content.decode()
    assert "保留上一版" in response.content.decode()
