from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from django.db import transaction

from research.models import IngestionRun, Observation, RawArtifact
from research.official_data import _store_bls_observations_v2
from research.providers import BLSProvider
from research.services import begin_ingestion, ensure_source, record_provider_result


def _bls_result(
    monkeypatch,
    *,
    secret: str = "fixture-registration-secret",
    value: str = "4.2",
):
    payload = {
        "status": "REQUEST_SUCCEEDED",
        "message": [],
        "Results": {
            "series": [
                {
                    "seriesID": "LNS14000000",
                    "data": [
                        {
                            "year": "2026",
                            "period": "M06",
                            "periodName": "June",
                            "value": value,
                            "latest": "true",
                            "footnotes": [],
                        }
                    ],
                }
            ]
        },
    }
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode()

    def handler(request):
        assert request.method == "POST"
        assert request.url == httpx.URL(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        )
        assert secret.encode() in request.content
        return httpx.Response(
            200,
            content=raw_bytes,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    monkeypatch.setenv("BLS_REGISTRATION_KEY", secret)
    provider = BLSProvider(
        client=httpx.Client(
            base_url="https://api.bls.gov",
            transport=httpx.MockTransport(handler),
        )
    )
    try:
        result = provider.series(
            ["LNS14000000"],
            start_year=2026,
            end_year=2026,
        )
    finally:
        provider.close()
    assert result.ok, result.error
    return result, raw_bytes


@pytest.mark.django_db
def test_bls_v2_same_running_run_retry_reuses_artifact_and_batch_rows(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    source = ensure_source("bls")
    first_result, raw_bytes = _bls_result(monkeypatch)
    run = begin_ingestion("bls", first_result.dataset)

    # Model the crash window after the atomic callback commits but before the
    # coordinator marks its pre-created run SUCCESS.
    with transaction.atomic():
        assert _store_bls_observations_v2(first_result, source, run) == 1
    run.refresh_from_db()
    assert run.status == IngestionRun.Status.RUNNING
    first_artifact = RawArtifact.objects.get(run=run)
    first_row = Observation.objects.get(batch_id=run.batch_id)

    retry_result, retry_raw_bytes = _bls_result(monkeypatch)
    assert retry_raw_bytes == raw_bytes
    retried = record_provider_result(
        retry_result,
        persist=_store_bls_observations_v2,
        run=run,
    )

    assert retried.status == IngestionRun.Status.SUCCESS
    assert retried.row_count == 1
    retained_artifact = RawArtifact.objects.get(run=run)
    retained_row = Observation.objects.get(batch_id=run.batch_id)
    assert retained_artifact.pk == first_artifact.pk
    assert retained_artifact.updated_at == first_artifact.updated_at
    assert retained_row.pk == first_row.pk
    assert retained_row.updated_at == first_row.updated_at
    assert retained_row.fetched_at == first_row.fetched_at
    assert RawArtifact.objects.filter(run=run).count() == 1
    assert Observation.objects.filter(batch_id=run.batch_id).count() == 1


@pytest.mark.django_db
def test_bls_v2_same_running_run_conflicting_bytes_fail_closed(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    source = ensure_source("bls")
    first_result, _raw_bytes = _bls_result(monkeypatch)
    run = begin_ingestion("bls", first_result.dataset)
    with transaction.atomic():
        assert _store_bls_observations_v2(first_result, source, run) == 1
    first_artifact = RawArtifact.objects.get(run=run)
    first_row = Observation.objects.get(batch_id=run.batch_id)

    conflicting_result, conflicting_raw = _bls_result(monkeypatch, value="4.3")
    conflicting_digest = hashlib.sha256(conflicting_raw).hexdigest()
    failed = record_provider_result(
        conflicting_result,
        persist=_store_bls_observations_v2,
        run=run,
    )

    assert failed.status == IngestionRun.Status.FAILED
    assert "conflicting private raw artifact" in failed.error
    assert RawArtifact.objects.get(run=run).pk == first_artifact.pk
    retained = Observation.objects.get(batch_id=run.batch_id)
    assert retained.pk == first_row.pk
    assert retained.value == Decimal("4.2")
    assert RawArtifact.objects.filter(run=run).count() == 1
    assert Observation.objects.filter(batch_id=run.batch_id).count() == 1
    assert not (
        Path(settings.RAW_ARTIFACT_ROOT)
        / conflicting_digest[:2]
        / f"{conflicting_digest}.bin"
    ).exists()


@pytest.mark.django_db
def test_bls_v2_keeps_exact_private_bytes_and_append_only_batches(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    ensure_source("bls")

    first_result, raw_bytes = _bls_result(monkeypatch)
    assert first_result.raw_bytes == raw_bytes
    assert first_result.metadata["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert "registration" not in json.dumps(first_result.metadata).lower()
    assert "fixture-registration-secret" not in json.dumps(first_result.metadata)
    first = record_provider_result(
        first_result,
        persist=_store_bls_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS
    assert first.row_count == 1
    first_artifact = RawArtifact.objects.get(run=first)
    assert first_artifact.uri.startswith("private://bls/")
    artifact_path = (
        Path(settings.RAW_ARTIFACT_ROOT)
        / first_artifact.sha256[:2]
        / f"{first_artifact.sha256}.bin"
    )
    assert artifact_path.read_bytes() == raw_bytes
    assert Observation.objects.filter(batch_id=first.batch_id).count() == 1
    first_value = Observation.objects.get(batch_id=first.batch_id).value
    assert first_value == Decimal("4.2")

    second_result, second_raw_bytes = _bls_result(monkeypatch)
    second = record_provider_result(
        second_result,
        persist=_store_bls_observations_v2,
    )
    assert second.status == IngestionRun.Status.SUCCESS
    assert second.batch_id != first.batch_id
    assert second_raw_bytes == raw_bytes
    assert RawArtifact.objects.filter(
        run__in=(first, second),
    ).count() == 2
    assert Observation.objects.filter(batch_id=first.batch_id).count() == 1
    assert Observation.objects.filter(batch_id=second.batch_id).count() == 1
    assert Observation.objects.filter(source__key="bls").count() == 2


@pytest.mark.django_db
def test_bls_v2_rejects_normalized_or_transport_tamper(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    ensure_source("bls")

    record_tamper, _raw_bytes = _bls_result(monkeypatch)
    record_tamper.records[0]["value"] = Decimal("9.9")
    failed_record = record_provider_result(
        record_tamper,
        persist=_store_bls_observations_v2,
    )
    assert failed_record.status == IngestionRun.Status.FAILED
    assert "do not match the exact JSON" in failed_record.error
    assert not RawArtifact.objects.filter(run=failed_record).exists()
    assert not Observation.objects.filter(batch_id=failed_record.batch_id).exists()

    transport_tamper, _raw_bytes = _bls_result(monkeypatch)
    transport_tamper.metadata["endpoint"] = (
        "https://mirror.invalid/publicAPI/v2/timeseries/data/"
    )
    failed_transport = record_provider_result(
        transport_tamper,
        persist=_store_bls_observations_v2,
    )
    assert failed_transport.status == IngestionRun.Status.FAILED
    assert "endpoint" in failed_transport.error
    assert not RawArtifact.objects.filter(run=failed_transport).exists()
    assert not Observation.objects.filter(batch_id=failed_transport.batch_id).exists()
