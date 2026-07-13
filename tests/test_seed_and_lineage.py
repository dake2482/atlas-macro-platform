from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from research.models import (
    CodingAgentProfile,
    Company,
    DashboardSnapshot,
    FinancialFact,
    FundLetter,
    GitHubProject,
    GlossaryTerm,
    IngestionRun,
    MetricSnapshot,
    ModelProfile,
    Observation,
    RawArtifact,
    SeriesDefinition,
    Source,
    SupplyChainNode,
    Thesis,
)

BASELINE_COUNTS = {
    Thesis: 86,
    FundLetter: 267,
    SupplyChainNode: 45,
    Company: 219,
    FinancialFact: 657,
    ModelProfile: 12,
    CodingAgentProfile: 11,
    GitHubProject: 45,
    GlossaryTerm: 32,
}


@pytest.mark.django_db
def test_seed_platform_creates_full_product_shape(seeded_platform):
    for model, expected in BASELINE_COUNTS.items():
        assert model.objects.count() == expected, model.__name__

    assert SupplyChainNode.objects.values("layer").distinct().count() == 9
    assert not Company.objects.filter(primary_node__isnull=True).exists()
    assert not Thesis.objects.filter(is_published=True).exists()
    assert not Thesis.objects.exclude(review_status=Thesis.ReviewStatus.DRAFT).exists()
    assert not Thesis.objects.filter(published_at__isnull=False).exists()
    assert not Thesis.objects.filter(reviewed_at__isnull=False).exists()
    assert not Thesis.objects.exclude(reviewed_by="").exists()
    assert not Thesis.objects.exclude(publication_fingerprint="").exists()
    assert not Thesis.objects.filter(source_snapshot__isnull=False).exists()


@pytest.mark.django_db
def test_seed_platform_is_idempotent(seeded_platform):
    before = {model: model.objects.count() for model in BASELINE_COUNTS}

    call_command("seed_platform", allow_demo_data=True, verbosity=0)

    assert {model: model.objects.count() for model in BASELINE_COUNTS} == before
    assert not Thesis.objects.filter(is_published=True).exists()
    assert not Thesis.objects.exclude(review_status=Thesis.ReviewStatus.DRAFT).exists()
    assert not Thesis.objects.exclude(publication_fingerprint="").exists()
    assert not Thesis.objects.filter(source_snapshot__isnull=False).exists()


@pytest.mark.django_db
def test_observation_retains_complete_lineage():
    primary = Source.objects.create(
        key="lineage-primary",
        name="Lineage primary",
        homepage="https://example.com/primary",
        license_status=Source.LicenseStatus.OPEN,
        license_scope="public time series",
        redistribution_allowed=True,
    )
    fallback = Source.objects.create(
        key="lineage-fallback",
        name="Lineage fallback",
        homepage="https://example.com/fallback",
        license_status=Source.LicenseStatus.REVIEW,
        license_scope="internal evaluation only",
    )
    series = SeriesDefinition.objects.create(
        key="lineage-test-series",
        name="Lineage test series",
        unit="index",
        source=primary,
    )
    value_date = timezone.now() - timedelta(days=1)
    fetched_at = timezone.now()
    batch_id = uuid.uuid4()

    observation = Observation.objects.create(
        series=series,
        value=Decimal("123.456"),
        value_date=value_date,
        as_of=value_date,
        fetched_at=fetched_at,
        batch_id=batch_id,
        source=primary,
        fallback_source=fallback,
        quality_status=Observation.Quality.FALLBACK,
        metadata={"provider_symbol": "TEST", "revision": 2},
    )

    observation.refresh_from_db()
    assert observation.source == primary
    assert observation.fallback_source == fallback
    assert observation.batch_id == batch_id
    assert observation.value_date == value_date
    assert observation.as_of == value_date
    assert observation.fetched_at == fetched_at
    assert observation.quality_status == Observation.Quality.FALLBACK
    assert observation.metadata["revision"] == 2
    assert observation.source.license_scope == "public time series"


@pytest.mark.django_db
def test_raw_artifact_and_snapshot_share_auditable_batch():
    source = Source.objects.create(
        key="batch-lineage",
        name="Batch lineage source",
        license_status=Source.LicenseStatus.OPEN,
        license_scope="test",
        redistribution_allowed=True,
    )
    now = timezone.now()
    run = IngestionRun.objects.create(
        source=source,
        dataset="test-dataset",
        started_at=now,
        completed_at=now,
        status=IngestionRun.Status.SUCCESS,
        row_count=1,
    )
    payload = b'{"value": 42}'
    artifact = RawArtifact.objects.create(
        run=run,
        uri="memory://test-dataset/payload.json",
        sha256=hashlib.sha256(payload).hexdigest(),
        content_type="application/json",
        size_bytes=len(payload),
    )
    metric = MetricSnapshot.objects.create(
        key="batch-lineage-value",
        label="Batch lineage value",
        value=Decimal("42"),
        display_value="42",
        value_date=now,
        as_of=now,
        fetched_at=now,
        batch_id=run.batch_id,
        source=source,
        quality_status=Observation.Quality.FRESH,
        license_scope=source.license_scope,
        metadata={"artifact_sha256": artifact.sha256},
    )
    dashboard = DashboardSnapshot.objects.create(
        key="batch-lineage-dashboard",
        title="Batch lineage dashboard",
        as_of=now,
        batch_id=run.batch_id,
        quality_status=Observation.Quality.FRESH,
        source=source,
        is_published=True,
        data={"metric_keys": [metric.key]},
    )

    assert len(artifact.sha256) == 64
    assert metric.batch_id == run.batch_id == dashboard.batch_id
    assert metric.value_date == now
    assert metric.fallback_source is None
    assert metric.metadata["artifact_sha256"] == artifact.sha256
    assert dashboard.is_published is True
