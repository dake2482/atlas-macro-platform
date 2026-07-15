from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal

import pytest
from django.utils import timezone

from research.labor_official import (
    CONTINUED_4WK,
    CONTINUED_SA,
    INITIAL_4WK,
    INITIAL_SA,
    IUR_SA,
    DOLWeeklyClaimsProvider,
)
from research.macro_official import CensusMARTSProvider
from research.macro_releases import BEAPIOReleaseProvider
from research.models import IngestionRun, Observation, RawArtifact
from research.official_data import (
    _store_bea_release_observations_v2,
    _store_bls_observations_v2,
    _store_census_marts_observations_v2,
    _store_dol_claims_observations_v2,
    _validated_bea_release_bundle,
    _validated_bls_raw_contract,
    _validated_census_marts_bundle,
    _validated_dol_claims_bundle,
)
from research.providers import BLSProvider, ProviderResult
from research.raw_evidence import EvidenceResponse, build_evidence_bundle
from research.services import ensure_source, record_provider_result


def _bls_result(
    *,
    start_year: int = 2025,
    end_year: int = 2026,
    observation_year: int = 2026,
    observation_years: tuple[int, ...] | None = None,
    message: str | None = None,
    include_series: bool = True,
    preliminary: bool = False,
) -> ProviderResult:
    series_id = "LNS14000000"
    years = observation_years or (observation_year,)
    payload = {
        "status": "REQUEST_SUCCEEDED",
        "message": [message] if message is not None else [],
        "Results": {
            "series": (
                [
                    {
                        "seriesID": series_id,
                        "data": [
                            {
                                "year": str(year),
                                "period": "M06",
                                "periodName": "June",
                                "value": "4.2",
                                "latest": "true",
                                "footnotes": (
                                    [{"code": "P", "text": "preliminary"}]
                                    if preliminary
                                    else []
                                ),
                            }
                            for year in years
                        ],
                    }
                ]
                if include_series
                else []
            )
        },
    }
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode()
    fetched_at = timezone.now()
    records, replay_metadata = BLSProvider.parse_series_json_bytes(
        raw_bytes,
        series_ids=[series_id],
        start_year=start_year,
        end_year=end_year,
        fetched_at=fetched_at,
    )
    return ProviderResult(
        provider="bls",
        dataset=f"series:{series_id}",
        records=records,
        fetched_at=fetched_at,
        raw_bytes=raw_bytes,
        metadata={
            "endpoint": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            "content_type": "application/json",
            "byte_length": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            **replay_metadata,
            "request_witness": {
                "series_ids": [series_id],
                "start_year": start_year,
                "end_year": end_year,
            },
        },
    )


def _evidence_result(
    *,
    provider: str,
    dataset: str,
    roles: tuple[str, ...],
    records: list[dict],
    replay_metadata: dict,
    nonce: str,
) -> ProviderResult:
    raw_bytes, bundle_metadata = build_evidence_bundle(
        provider=provider,
        dataset=dataset,
        responses=tuple(
            EvidenceResponse(
                role=role,
                url=f"https://example.test/{provider}/{role}",
                content_type=(
                    "application/pdf"
                    if role.endswith("pdf")
                    else "application/octet-stream"
                ),
                raw_bytes=f"{nonce}:{role}".encode(),
            )
            for role in roles
        ),
    )
    return ProviderResult(
        provider=provider,
        dataset=dataset,
        records=deepcopy(records),
        fetched_at=timezone.now(),
        raw_bytes=raw_bytes,
        metadata={**bundle_metadata, **deepcopy(replay_metadata)},
    )


@pytest.mark.django_db
def test_bls_failed_attempt_cannot_pollute_watermark_or_coverage(
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    source = ensure_source("bls")
    first = record_provider_result(
        _bls_result(),
        persist=_store_bls_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS

    IngestionRun.objects.create(
        source=source,
        dataset=first.dataset,
        started_at=timezone.now(),
        completed_at=timezone.now(),
        status=IngestionRun.Status.FAILED,
        metadata={
            "latest_value_dates": {"LNS14000000": "2099-06-01"},
            "start_year": 1900,
            "end_year": 2099,
            "series_date_coverage": {
                "lns14000000": {
                    "start": "1900-01-01",
                    "end": "2099-06-01",
                    "count": 2394,
                }
            },
        },
    )

    repeated = record_provider_result(
        _bls_result(),
        persist=_store_bls_observations_v2,
    )

    assert repeated.status == IngestionRun.Status.SUCCESS
    assert Observation.objects.filter(batch_id=repeated.batch_id).count() == 1
    assert RawArtifact.objects.filter(run=repeated).count() == 1


@pytest.mark.django_db
def test_bls_persisted_partial_rows_cannot_pollute_success_watermark(
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    first = record_provider_result(
        _bls_result(
            start_year=2025,
            end_year=2025,
            observation_year=2025,
        ),
        persist=_store_bls_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS

    partial_result = _bls_result(
        start_year=2025,
        end_year=2026,
        observation_years=(2025, 2026),
    )
    partial = record_provider_result(
        partial_result,
        persist=_store_bls_observations_v2,
    )
    assert partial.status == IngestionRun.Status.SUCCESS, partial.error
    partial.status = IngestionRun.Status.PARTIAL
    partial.metadata = {**partial.metadata, "quality_status": "partial"}
    partial.save(update_fields=["status", "metadata", "updated_at"])
    assert Observation.objects.filter(batch_id=partial.batch_id).count() == 2

    repeated = record_provider_result(
        _bls_result(
            start_year=2025,
            end_year=2025,
            observation_year=2025,
        ),
        persist=_store_bls_observations_v2,
    )
    assert repeated.status == IngestionRun.Status.SUCCESS


@pytest.mark.django_db
def test_bls_rejects_coverage_shrink_and_missing_requested_series(
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    first = record_provider_result(
        _bls_result(
            start_year=2024,
            end_year=2025,
            observation_year=2025,
        ),
        persist=_store_bls_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS

    narrowed = record_provider_result(
        _bls_result(
            start_year=2025,
            end_year=2025,
            observation_year=2025,
        ),
        persist=_store_bls_observations_v2,
    )
    assert narrowed.status == IngestionRun.Status.FAILED
    assert "coverage window shrank" in narrowed.error

    rolled = record_provider_result(
        _bls_result(start_year=2025, end_year=2026),
        persist=_store_bls_observations_v2,
    )
    assert rolled.status == IngestionRun.Status.SUCCESS

    missing = record_provider_result(
        _bls_result(include_series=False),
        persist=_store_bls_observations_v2,
    )
    assert missing.status == IngestionRun.Status.FAILED
    assert "lacks required requested-series coverage" in missing.error


@pytest.mark.django_db
def test_bls_preliminary_row_persists_as_exact_estimated_observation(
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    run = record_provider_result(
        _bls_result(preliminary=True),
        persist=_store_bls_observations_v2,
    )

    assert run.status == IngestionRun.Status.SUCCESS, run.error
    observation = Observation.objects.get(batch_id=run.batch_id)
    assert observation.quality_status == Observation.Quality.ESTIMATED
    assert observation.metadata["preliminary"] is True
    assert RawArtifact.objects.filter(run=run).count() == 1


@pytest.mark.django_db
def test_bls_rejects_future_observation_and_registration_key_echo(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    ensure_source("bls")
    future = _bls_result(observation_year=2026)
    future_payload = json.loads(future.raw_bytes)
    future_payload["Results"]["series"][0]["data"][0]["year"] = "2099"
    future.raw_bytes = json.dumps(future_payload, separators=(",", ":")).encode()
    future.metadata.update(
        {
            "byte_length": len(future.raw_bytes),
            "sha256": hashlib.sha256(future.raw_bytes).hexdigest(),
            "start_year": 2026,
            "end_year": 2099,
            "request_witness": {
                "series_ids": ["LNS14000000"],
                "start_year": 2026,
                "end_year": 2099,
            },
        }
    )
    with pytest.raises(ValueError, match="outside the request contract"):
        _validated_bls_raw_contract(future)

    secret = "fixture-registration-secret"
    monkeypatch.setenv("BLS_REGISTRATION_KEY", secret)
    echoed = _bls_result(message=f"upstream echoed {secret}")
    echoed_run = record_provider_result(
        echoed,
        persist=_store_bls_observations_v2,
    )
    assert echoed_run.status == IngestionRun.Status.FAILED
    assert "registration credential" in echoed_run.error
    assert secret not in json.dumps(echoed_run.metadata)
    assert not RawArtifact.objects.filter(run=echoed_run).exists()


def _install_bea_replay(monkeypatch, results: list[ProviderResult]) -> None:
    registry = {
        bytes(result.raw_bytes): (
            deepcopy(result.records),
            {
                key: deepcopy(value)
                for key, value in result.metadata.items()
                if key
                not in {
                    "content_type",
                    "byte_length",
                    "sha256",
                    "evidence_bundle_schema",
                    "evidence_roles",
                    "response_count",
                    "unique_blob_count",
                }
            },
        )
        for result in results
    }
    monkeypatch.setattr(
        BEAPIOReleaseProvider,
        "replay_evidence_bundle",
        classmethod(lambda _cls, raw_bytes: deepcopy(registry[bytes(raw_bytes)])),
    )


def _bea_result(
    *,
    dates: tuple[str, ...],
    release_date: str,
    nonce: str,
) -> ProviderResult:
    records = [
        {
            "series_id": "BEA-REAL-PCE-MOM",
            "date": period,
            "value": Decimal(index + 1),
            "metadata": {"source_revision_date": release_date},
        }
        for index, period in enumerate(dates)
    ]
    return _evidence_result(
        provider="bea-pio-release",
        dataset="personal-income-outlays-release",
        roles=("release-page", "summary-workbook", "section2-workbook"),
        records=records,
        replay_metadata={
            "latest_value_date": max(dates),
            "source_revision_date": release_date,
        },
        nonce=nonce,
    )


@pytest.mark.django_db
def test_bea_rejects_future_dates_and_successful_coverage_shrink(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    valid = _bea_result(
        dates=("2020-01-01", "2020-02-01"),
        release_date="2020-03-27",
        nonce="bea-valid",
    )
    narrowed = _bea_result(
        dates=("2020-02-01",),
        release_date="2020-04-27",
        nonce="bea-narrowed",
    )
    repeated = _bea_result(
        dates=("2020-01-01", "2020-02-01"),
        release_date="2020-03-27",
        nonce="bea-repeated",
    )
    partial = _bea_result(
        dates=("2020-01-01", "2020-02-01", "2021-01-01"),
        release_date="2021-02-26",
        nonce="bea-partial",
    )
    partial.metadata["quality_status"] = "partial"
    after_partial = _bea_result(
        dates=("2020-01-01", "2020-02-01"),
        release_date="2020-03-27",
        nonce="bea-after-partial",
    )
    future_observation = _bea_result(
        dates=("2099-01-01",),
        release_date="2099-02-01",
        nonce="bea-future-observation",
    )
    future_release = _bea_result(
        dates=("2020-01-01",),
        release_date="2099-02-01",
        nonce="bea-future-release",
    )
    _install_bea_replay(
        monkeypatch,
        [
            valid,
            narrowed,
            repeated,
            partial,
            after_partial,
            future_observation,
            future_release,
        ],
    )

    with pytest.raises(ValueError, match="duplicate or invalid observation"):
        _validated_bea_release_bundle(future_observation)
    with pytest.raises(ValueError, match="future or impossible"):
        _validated_bea_release_bundle(future_release)

    first = record_provider_result(
        valid,
        persist=_store_bea_release_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS
    IngestionRun.objects.create(
        source=first.source,
        dataset=first.dataset,
        started_at=timezone.now(),
        completed_at=timezone.now(),
        status=IngestionRun.Status.FAILED,
        metadata={
            "latest_value_dates": {"bea-real-pce-mom": "2099-01-01"},
            "latest_release_date": "2099-02-01",
            "series_date_coverage": {
                "bea-real-pce-mom": {
                    "start": "1900-01-01",
                    "end": "2099-01-01",
                    "count": 2389,
                }
            },
        },
    )
    after_failed = record_provider_result(
        repeated,
        persist=_store_bea_release_observations_v2,
    )
    assert after_failed.status == IngestionRun.Status.SUCCESS
    partial_run = record_provider_result(
        partial,
        persist=_store_bea_release_observations_v2,
    )
    assert partial_run.status == IngestionRun.Status.PARTIAL
    assert Observation.objects.filter(batch_id=partial_run.batch_id).count() == 3
    after_partial_run = record_provider_result(
        after_partial,
        persist=_store_bea_release_observations_v2,
    )
    assert after_partial_run.status == IngestionRun.Status.SUCCESS
    rejected = record_provider_result(
        narrowed,
        persist=_store_bea_release_observations_v2,
    )
    assert rejected.status == IngestionRun.Status.FAILED
    assert "series coverage shrank" in rejected.error


def _dol_result(
    *,
    start_year: int,
    end_year: int,
    release_date: str = "2020-07-09",
    initial_week: str = "2020-07-04",
    continued_week: str = "2020-06-27",
    nonce: str,
) -> ProviderResult:
    records = [
        {
            "series_id": series_id,
            "date": initial_week if series_id in {INITIAL_SA, INITIAL_4WK} else continued_week,
            "value": Decimal("1"),
            "quality_status": "fresh",
            "metadata": {},
        }
        for series_id in (INITIAL_SA, INITIAL_4WK, CONTINUED_SA, CONTINUED_4WK, IUR_SA)
    ]
    return _evidence_result(
        provider="dol-eta-ui",
        dataset="national-weekly-claims",
        roles=(
            *(f"history-xml-{year}" for year in range(start_year, end_year + 1)),
            "current-release-pdf",
            "archive-release-pdf",
        ),
        records=records,
        replay_metadata={
            "xml_run_date": release_date,
            "release_date": release_date,
            "release_initial_week": initial_week,
            "release_continued_week": continued_week,
            "requested_start_year": start_year,
            "requested_end_year": end_year,
        },
        nonce=nonce,
    )


def _install_dol_replay(monkeypatch, results: list[ProviderResult]) -> None:
    registry = {
        bytes(result.raw_bytes): (
            deepcopy(result.records),
            {
                key: deepcopy(value)
                for key, value in result.metadata.items()
                if key
                not in {
                    "content_type",
                    "byte_length",
                    "sha256",
                    "evidence_bundle_schema",
                    "evidence_roles",
                    "response_count",
                    "unique_blob_count",
                }
            },
        )
        for result in results
    }
    monkeypatch.setattr(
        DOLWeeklyClaimsProvider,
        "replay_evidence_bundle",
        classmethod(lambda _cls, raw_bytes: deepcopy(registry[bytes(raw_bytes)])),
    )


@pytest.mark.django_db
def test_dol_rejects_future_release_and_requested_history_shrink(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    valid = _dol_result(start_year=2019, end_year=2020, nonce="dol-valid")
    narrowed = _dol_result(start_year=2020, end_year=2020, nonce="dol-narrowed")
    future = _dol_result(
        start_year=2099,
        end_year=2099,
        release_date="2099-07-09",
        initial_week="2099-07-04",
        continued_week="2099-06-27",
        nonce="dol-future",
    )
    _install_dol_replay(monkeypatch, [valid, narrowed, future])

    with pytest.raises(ValueError, match="future-dated"):
        _validated_dol_claims_bundle(future)

    first = record_provider_result(
        valid,
        persist=_store_dol_claims_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS
    rejected = record_provider_result(
        narrowed,
        persist=_store_dol_claims_observations_v2,
    )
    assert rejected.status == IngestionRun.Status.FAILED
    assert "coverage window shrank" in rejected.error


def _census_result(
    *,
    latest_date: str,
    require_complete_history: bool,
    nonce: str,
) -> ProviderResult:
    records = [
        {
            "series_id": series_id,
            "date": latest_date,
            "value": Decimal("1"),
            "metadata": {},
        }
        for series_id in (
            "CENSUS-MRTS-44X72-SM-SA",
            "CENSUS-MRTS-44X72-SM-SA-MOM",
            "CENSUS-MRTS-44X72-SM-SA-YOY",
        )
    ]
    return _evidence_result(
        provider="census",
        dataset="marts:44X72:SM:yes",
        roles=("marts-api-response",),
        records=records,
        replay_metadata={
            "latest_value_date": latest_date,
            "history_start": latest_date,
            "requested_time": "from 1992" if require_complete_history else latest_date[:7],
            "require_complete_history": require_complete_history,
        },
        nonce=nonce,
    )


def _install_census_replay(monkeypatch, results: list[ProviderResult]) -> None:
    registry = {
        bytes(result.raw_bytes): (
            deepcopy(result.records),
            {
                key: deepcopy(value)
                for key, value in result.metadata.items()
                if key
                not in {
                    "content_type",
                    "byte_length",
                    "sha256",
                    "evidence_bundle_schema",
                    "evidence_roles",
                    "response_count",
                    "unique_blob_count",
                }
            },
        )
        for result in results
    }
    monkeypatch.setattr(
        CensusMARTSProvider,
        "replay_evidence_bundle",
        classmethod(
            lambda _cls, raw_bytes, *, expected_dataset: deepcopy(
                registry[bytes(raw_bytes)]
            )
        ),
    )


@pytest.mark.django_db
def test_census_rejects_future_month_and_complete_history_policy_shrink(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    valid = _census_result(
        latest_date="2020-05-01",
        require_complete_history=True,
        nonce="census-valid",
    )
    narrowed = _census_result(
        latest_date="2020-05-01",
        require_complete_history=False,
        nonce="census-narrowed",
    )
    future = _census_result(
        latest_date="2099-05-01",
        require_complete_history=True,
        nonce="census-future",
    )
    _install_census_replay(monkeypatch, [valid, narrowed, future])

    with pytest.raises(ValueError, match="duplicate or invalid row"):
        _validated_census_marts_bundle(future)

    first = record_provider_result(
        valid,
        persist=_store_census_marts_observations_v2,
    )
    assert first.status == IngestionRun.Status.SUCCESS
    rejected = record_provider_result(
        narrowed,
        persist=_store_census_marts_observations_v2,
    )
    assert rejected.status == IngestionRun.Status.FAILED
    assert "complete-history policy shrank" in rejected.error
