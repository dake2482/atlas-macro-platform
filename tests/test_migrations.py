from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from research.models import FedDocument, Source, SourceLicense, Thesis
from tests.thesis_factories import build_complete_thesis


@pytest.mark.django_db
def test_thesis_review_migration_fails_closed_for_legacy_publications():
    migration = import_module(
        "research.migrations.0015_thesis_review_publication_contract"
    )
    legitimate = build_complete_thesis("migration-reviewed", report_date=date(1901, 1, 1))
    demo = Thesis.objects.create(
        date=date(1901, 1, 2),
        regime="demo",
        summary="演示日报 1901-01-02：仅用于测试",
        evidence=[],
        triggers=[],
        invalidation="test",
    )

    migration.unpublish_legacy_theses(apps, schema_editor=None)

    legitimate.refresh_from_db()
    demo.refresh_from_db()
    assert legitimate.is_published is False
    assert legitimate.published_at is None
    assert legitimate.review_status == Thesis.ReviewStatus.DRAFT
    assert legitimate.reviewed_by == ""
    assert legitimate.reviewed_at is None
    assert legitimate.publication_fingerprint == ""
    assert demo.is_published is False
    assert demo.published_at is None


def test_thesis_review_migration_orders_cleanup_before_constraint():
    migration = import_module(
        "research.migrations.0015_thesis_review_publication_contract"
    )
    operations = migration.Migration.operations
    cleanup_index = next(
        index for index, operation in enumerate(operations) if isinstance(operation, migrations.RunPython)
    )
    constraint_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AddConstraint)
    )
    review_fields = {
        operation.name: index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AddField)
        and operation.name
        in {"review_status", "reviewed_at", "reviewed_by", "publication_fingerprint"}
    }

    assert set(review_fields) == {
        "review_status",
        "reviewed_at",
        "reviewed_by",
        "publication_fingerprint",
    }
    assert all(index < cleanup_index for index in review_fields.values())
    assert cleanup_index < constraint_index
    assert migration.Migration.dependencies == [
        ("research", "0014_releasevintageobservation")
    ]


@pytest.mark.django_db(transaction=True)
def test_real_0014_to_0015_migration_fails_closed():
    executor = MigrationExecutor(connection)
    old_target = [("research", "0014_releasevintageobservation")]
    new_target = [("research", "0015_thesis_review_publication_contract")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps
    OldSource = old_apps.get_model("research", "Source")
    OldDashboardSnapshot = old_apps.get_model("research", "DashboardSnapshot")
    OldThesis = old_apps.get_model("research", "Thesis")
    source = OldSource.objects.create(
        key="migration-executor-internal",
        name="Migration executor source",
    )
    snapshot = OldDashboardSnapshot.objects.create(
        key="daily-evidence",
        title="Legacy daily evidence",
        as_of=timezone.now(),
        data={},
        source=source,
        is_published=True,
    )
    legacy = OldThesis.objects.create(
        date=date(1901, 1, 3),
        regime="legacy-publication",
        summary="Legacy public row",
        evidence=[],
        triggers=[],
        invalidation="legacy",
        source_snapshot=snapshot,
        is_published=True,
        published_at=timezone.now(),
    )

    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    new_apps = executor.loader.project_state(new_target).apps
    NewThesis = new_apps.get_model("research", "Thesis")
    migrated = NewThesis.objects.get(pk=legacy.pk)

    assert migrated.is_published is False
    assert migrated.published_at is None
    assert migrated.review_status == "draft"
    assert migrated.reviewed_by == ""
    assert migrated.reviewed_at is None
    assert migrated.publication_fingerprint == ""


@pytest.mark.django_db
def test_source_license_migration_keeps_only_latest_created_decision_current():
    migration = import_module("research.migrations.0009_sourcelicense_is_current_and_more")
    source = Source.objects.create(key="migration-licence", name="Migration licence")
    older = SourceLicense.objects.create(
        source=source,
        status=Source.LicenseStatus.OPEN,
        scope="older",
        is_current=True,
        reviewed_at=timezone.now(),
    )
    newer = SourceLicense.objects.create(
        source=source,
        status=Source.LicenseStatus.RESTRICTED,
        scope="newer",
        is_current=False,
        reviewed_at=None,
    )
    now = timezone.now()
    SourceLicense.objects.filter(pk=older.pk).update(created_at=now - timedelta(days=1))
    SourceLicense.objects.filter(pk=newer.pk).update(created_at=now)

    migration.keep_latest_license_current(apps, schema_editor=None)

    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.is_current is False
    assert newer.is_current is True
    assert SourceLicense.objects.filter(source=source, is_current=True).count() == 1
    # A nullable ``reviewed_at`` must not put the older reviewed row first.
    assert SourceLicense.objects.filter(source=source).first() == newer


def test_source_license_schema_operations_are_ordered_safely():
    data_migration = import_module("research.migrations.0009_sourcelicense_is_current_and_more")
    constraint_migration = import_module(
        "research.migrations.0010_sourcelicense_current_constraint"
    )
    operations = data_migration.Migration.operations

    is_current_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AddField) and operation.name == "is_current"
    )
    required_notice_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AddField) and operation.name == "required_notice"
    )
    cleanup_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.RunPython)
    )
    constraint = next(
        operation
        for operation in constraint_migration.Migration.operations
        if isinstance(operation, migrations.AddConstraint)
        and operation.constraint.name == "one_current_license_per_source"
    )
    index_operation = next(
        operation
        for operation in constraint_migration.Migration.operations
        if isinstance(operation, migrations.AlterField) and operation.name == "is_current"
    )

    assert is_current_index < cleanup_index
    assert required_notice_index < cleanup_index
    assert index_operation.field.db_index is True
    assert constraint.constraint.condition == models.Q(is_current=True)
    assert constraint_migration.Migration.dependencies == [
        ("research", "0009_sourcelicense_is_current_and_more")
    ]
    assert SourceLicense._meta.ordering == ["-created_at", "-pk"]


@pytest.mark.django_db
def test_fed_provenance_migration_splits_typical_rss_and_retains_legacy_analysis():
    migration = import_module(
        "research.migrations.0017_fed_document_analysis_provenance"
    )
    typical = FedDocument.objects.create(
        document_type=FedDocument.DocumentType.NEWS,
        slug="migration-typical-rss",
        title="Typical RSS row",
        summary="Official RSS description stored in the old summary field",
        key_points=[],
        published_at=timezone.now(),
        hawkish_score=0,
        original_url=(
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "migration-typical-rss.htm"
        ),
    )
    enriched = FedDocument.objects.create(
        document_type=FedDocument.DocumentType.SPEECH,
        slug="migration-legacy-enrichment",
        title="Legacy enrichment",
        speaker="Governor Fixture",
        summary="Legacy analysis must be retained but not published",
        key_points=["Legacy point"],
        published_at=timezone.now(),
        hawkish_score=3,
        original_url=(
            "https://www.federalreserve.gov/newsevents/speech/"
            "migration-legacy-enrichment.htm"
        ),
    )

    migration.split_official_description_from_legacy_analysis(
        apps,
        schema_editor=None,
    )

    typical.refresh_from_db()
    assert typical.official_description == (
        "Official RSS description stored in the old summary field"
    )
    assert typical.summary == ""
    assert typical.hawkish_score is None
    assert typical.analysis_status == FedDocument.AnalysisStatus.DRAFT
    assert typical.analysis_evidence == []

    enriched.refresh_from_db()
    assert enriched.official_description == ""
    assert enriched.summary == "Legacy analysis must be retained but not published"
    assert enriched.key_points == ["Legacy point"]
    assert enriched.hawkish_score == 3
    assert enriched.analysis_status == FedDocument.AnalysisStatus.DRAFT
    assert enriched.analysis_evidence[0]["kind"] == "legacy_unverified"
    assert enriched.has_public_analysis is False


def test_fed_provenance_migration_orders_split_before_score_constraint():
    migration = import_module(
        "research.migrations.0017_fed_document_analysis_provenance"
    )
    operations = migration.Migration.operations
    split_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.RunPython)
    )
    score_alter_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AlterField)
        and operation.name == "hawkish_score"
    )
    constraint_index = next(
        index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AddConstraint)
        and operation.constraint.name == "fed_hawkish_score_range"
    )
    provenance_fields = {
        operation.name: index
        for index, operation in enumerate(operations)
        if isinstance(operation, migrations.AddField)
        and operation.name
        in {
            "official_description",
            "analysis_status",
            "analysis_model",
            "analysis_prompt_version",
            "analysis_generated_at",
            "analysis_evidence",
            "reviewed_by",
            "reviewed_at",
        }
    }

    assert len(provenance_fields) == 8
    assert score_alter_index < split_index < constraint_index
    assert all(index < split_index for index in provenance_fields.values())
    assert migration.Migration.dependencies == [
        ("research", "0016_sec_company_facts_and_capex_projection")
    ]


@pytest.mark.django_db(transaction=True)
def test_real_0016_to_0017_fed_migration_fails_closed_and_preserves_legacy_values():
    executor = MigrationExecutor(connection)
    old_target = [("research", "0016_sec_company_facts_and_capex_projection")]
    new_target = [("research", "0017_fed_document_analysis_provenance")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps
    OldFedDocument = old_apps.get_model("research", "FedDocument")
    typical = OldFedDocument.objects.create(
        document_type="news",
        slug="executor-typical-fed-rss",
        title="Executor typical RSS",
        summary="Executor official description",
        key_points=[],
        published_at=timezone.now(),
        hawkish_score=0,
        original_url=(
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "executor-typical-fed-rss.htm"
        ),
    )
    enriched = OldFedDocument.objects.create(
        document_type="speech",
        slug="executor-enriched-fed-row",
        title="Executor enriched row",
        speaker="Governor Executor",
        summary="Executor legacy enrichment",
        key_points=["Executor legacy point"],
        published_at=timezone.now(),
        hawkish_score=7,
        original_url=(
            "https://www.federalreserve.gov/newsevents/speech/"
            "executor-enriched-fed-row.htm"
        ),
    )

    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    new_apps = executor.loader.project_state(new_target).apps
    NewFedDocument = new_apps.get_model("research", "FedDocument")

    migrated_typical = NewFedDocument.objects.get(pk=typical.pk)
    assert migrated_typical.official_description == "Executor official description"
    assert migrated_typical.summary == ""
    assert migrated_typical.hawkish_score is None
    assert migrated_typical.analysis_status == "draft"
    assert migrated_typical.analysis_evidence == []

    migrated_enriched = NewFedDocument.objects.get(pk=enriched.pk)
    assert migrated_enriched.summary == "Executor legacy enrichment"
    assert migrated_enriched.key_points == ["Executor legacy point"]
    assert migrated_enriched.hawkish_score is None
    assert migrated_enriched.analysis_status == "draft"
    assert migrated_enriched.analysis_evidence[0]["kind"] == "legacy_unverified"
    assert migrated_enriched.analysis_evidence[0]["legacy_hawkish_score"] == 7


@pytest.mark.django_db(transaction=True)
def test_real_0017_to_0018_preserves_vintages_and_allows_new_acquisition_batch():
    executor = MigrationExecutor(connection)
    old_target = [("research", "0017_fed_document_analysis_provenance")]
    new_target = [("research", "0018_release_vintage_batch_identity")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps
    OldSource = old_apps.get_model("research", "Source")
    OldSeries = old_apps.get_model("research", "SeriesDefinition")
    OldVintage = old_apps.get_model("research", "ReleaseVintageObservation")
    source = OldSource.objects.create(
        key="migration-bea-release",
        name="Migration BEA release",
        license_scope="public-domain official data",
    )
    series = OldSeries.objects.create(
        key="migration-bea-a191rl",
        name="Migration real GDP",
        unit="percent",
        frequency="quarterly",
        source=source,
    )
    fetched_at = timezone.now()
    original_batch = uuid.uuid4()
    first = OldVintage.objects.create(
        series=series,
        value="2.1",
        value_date=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 6, 25, tzinfo=UTC),
        release_date=date(2026, 6, 25),
        estimate_round="Third",
        vintage_label="Third",
        fetched_at=fetched_at,
        batch_id=original_batch,
        source=source,
        quality_status="fresh",
        license_scope=source.license_scope,
        metadata={"fixture": "first"},
    )
    second = OldVintage.objects.create(
        series=series,
        value="1.6",
        value_date=datetime(2026, 1, 1, tzinfo=UTC),
        as_of=datetime(2026, 5, 28, tzinfo=UTC),
        release_date=date(2026, 5, 28),
        estimate_round="Second",
        vintage_label="Second",
        fetched_at=fetched_at,
        batch_id=original_batch,
        source=source,
        quality_status="fresh",
        license_scope=source.license_scope,
        metadata={"fixture": "second"},
    )

    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    new_apps = executor.loader.project_state(new_target).apps
    NewVintage = new_apps.get_model("research", "ReleaseVintageObservation")
    assert list(
        NewVintage.objects.filter(pk__in=(first.pk, second.pk))
        .order_by("pk")
        .values_list("value", "batch_id", "metadata")
    ) == [
        (Decimal("2.1"), original_batch, {"fixture": "first"}),
        (Decimal("1.6"), original_batch, {"fixture": "second"}),
    ]

    new_batch = uuid.uuid4()
    retained = NewVintage.objects.get(pk=first.pk)
    duplicate_release_new_acquisition = NewVintage.objects.create(
        series_id=retained.series_id,
        value=retained.value,
        value_date=retained.value_date,
        as_of=retained.as_of,
        release_date=retained.release_date,
        estimate_round=retained.estimate_round,
        vintage_label=retained.vintage_label,
        fetched_at=retained.fetched_at,
        batch_id=new_batch,
        source_id=retained.source_id,
        quality_status=retained.quality_status,
        license_scope=retained.license_scope,
        metadata=retained.metadata,
    )
    assert duplicate_release_new_acquisition.batch_id == new_batch
    assert NewVintage.objects.filter(
        series_id=retained.series_id,
        value_date=retained.value_date,
        release_date=retained.release_date,
        estimate_round=retained.estimate_round,
        source_id=retained.source_id,
    ).count() == 2


@pytest.mark.django_db(transaction=True)
def test_real_0018_to_0019_preserves_runs_and_accepts_full_bls_dataset_identity():
    executor = MigrationExecutor(connection)
    old_target = [("research", "0018_release_vintage_batch_identity")]
    new_target = [("research", "0019_expand_ingestion_run_dataset")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps
    OldSource = old_apps.get_model("research", "Source")
    OldRun = old_apps.get_model("research", "IngestionRun")
    source = OldSource.objects.create(
        key="migration-bls",
        name="Migration BLS",
        license_scope="public official data",
    )
    original_dataset = "series:" + "X" * 113
    original = OldRun.objects.create(
        source=source,
        dataset=original_dataset,
        started_at=timezone.now(),
        status="success",
        row_count=1,
    )

    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    new_apps = executor.loader.project_state(new_target).apps
    NewRun = new_apps.get_model("research", "IngestionRun")
    assert NewRun._meta.get_field("dataset").max_length == 512
    assert NewRun.objects.get(pk=original.pk).dataset == original_dataset

    full_bls_dataset = "series:" + ",".join(
        f"SERIES{index:03d}" for index in range(32)
    )
    assert 120 < len(full_bls_dataset) <= 512
    created = NewRun.objects.create(
        source_id=source.pk,
        dataset=full_bls_dataset,
        started_at=timezone.now(),
    )
    assert NewRun.objects.get(pk=created.pk).dataset == full_bls_dataset
