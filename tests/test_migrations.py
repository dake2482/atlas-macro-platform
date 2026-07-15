from __future__ import annotations

import uuid
from copy import deepcopy
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


@pytest.mark.django_db(transaction=True)
def test_real_0019_to_0020_qualifies_census_api_series_and_all_lineage_references():
    executor = MigrationExecutor(connection)
    old_target = [("research", "0019_expand_ingestion_run_dataset")]
    new_target = [("research", "0020_census_api_series_identity")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps
    OldSource = old_apps.get_model("research", "Source")
    OldSeries = old_apps.get_model("research", "SeriesDefinition")
    OldRun = old_apps.get_model("research", "IngestionRun")
    OldObservation = old_apps.get_model("research", "Observation")

    aliases = {
        "census-mrts-44x72-sm-sa": "census-api-mrts-44x72-sm-sa",
        "census-mrts-44x72-sm-sa-mom": "census-api-mrts-44x72-sm-sa-mom",
        "census-mrts-44x72-sm-sa-yoy": "census-api-mrts-44x72-sm-sa-yoy",
    }
    legacy_keys = list(aliases)
    census = OldSource.objects.create(
        key="census",
        name="Migration Census API",
        license_scope="public official data",
    )
    series_by_key = {
        key: OldSeries.objects.create(
            key=key,
            name=f"Legacy Census series {index}",
            unit="USD millions" if index == 0 else "%",
            frequency="monthly",
            source=census,
        )
        for index, key in enumerate(legacy_keys)
    }
    unrelated = OldSeries.objects.create(
        key="census-unrelated-series",
        name="Unrelated Census series",
        unit="index",
        frequency="monthly",
        source=census,
    )
    run = OldRun.objects.create(
        source=census,
        dataset="marts:44X72:SM:yes",
        started_at=timezone.now(),
        status="success",
        row_count=3,
        metadata={
            "latest_value_dates": {
                legacy_keys[0]: "2026-05-01",
                legacy_keys[1]: "2026-05-01",
                legacy_keys[2]: "2026-05-01",
                unrelated.key: "2026-04-01",
            },
            "series_date_coverage": {
                legacy_keys[0]: {"first": "1992-01-01", "latest": "2026-05-01"},
                legacy_keys[1]: {"first": "1992-02-01", "latest": "2026-05-01"},
                legacy_keys[2]: {"first": "1993-01-01", "latest": "2026-05-01"},
                unrelated.key: {"first": "2026-04-01", "latest": "2026-04-01"},
            },
            "preserved": {"series_key": legacy_keys[0]},
        },
    )
    observed_at = datetime(2026, 5, 1, tzinfo=UTC)
    fetched_at = datetime(2026, 6, 13, tzinfo=UTC)
    observation_pks = []
    for index, legacy_key in enumerate(legacy_keys):
        observation = OldObservation.objects.create(
            series=series_by_key[legacy_key],
            value=Decimal(str(100 + index)),
            value_date=observed_at,
            as_of=fetched_at,
            fetched_at=fetched_at,
            batch_id=uuid.uuid4(),
            source=census,
            quality_status="fresh",
            metadata={
                "input_series": [
                    legacy_key,
                    legacy_keys[(index + 1) % len(legacy_keys)],
                    unrelated.key,
                ],
                "input_series_id": legacy_key.upper(),
                "input_lineage": [
                    {"series_key": key, "value_date": "2026-05-01"}
                    for key in legacy_keys
                ]
                + [
                    {"series_key": unrelated.key, "value_date": "2026-04-01"},
                    {"preserved": legacy_key},
                    "opaque-lineage-entry",
                ],
                "preserved": {"series_key": legacy_key},
            },
        )
        observation_pks.append(observation.pk)

    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    new_apps = executor.loader.project_state(new_target).apps
    NewSeries = new_apps.get_model("research", "SeriesDefinition")
    NewRun = new_apps.get_model("research", "IngestionRun")
    NewObservation = new_apps.get_model("research", "Observation")

    migrated_series = {
        row.pk: row.key
        for row in NewSeries.objects.filter(pk__in=[item.pk for item in series_by_key.values()])
    }
    assert migrated_series == {
        series_by_key[legacy_key].pk: qualified_key
        for legacy_key, qualified_key in aliases.items()
    }
    assert NewSeries.objects.filter(key__in=legacy_keys).count() == 0
    assert NewSeries.objects.filter(key__in=aliases.values()).count() == 3
    assert NewSeries.objects.get(pk=unrelated.pk).key == unrelated.key

    migrated_run = NewRun.objects.get(pk=run.pk)
    assert migrated_run.metadata["latest_value_dates"] == {
        aliases.get(key, key): value
        for key, value in run.metadata["latest_value_dates"].items()
    }
    assert migrated_run.metadata["series_date_coverage"] == {
        aliases.get(key, key): value
        for key, value in run.metadata["series_date_coverage"].items()
    }
    assert migrated_run.metadata["preserved"] == {"series_key": legacy_keys[0]}

    for index, observation in enumerate(
        NewObservation.objects.filter(pk__in=observation_pks).order_by("pk")
    ):
        legacy_key = legacy_keys[index]
        assert observation.series.key == aliases[legacy_key]
        assert observation.metadata["input_series"] == [
            aliases[legacy_key],
            aliases[legacy_keys[(index + 1) % len(legacy_keys)]],
            unrelated.key,
        ]
        assert observation.metadata["input_series_id"] == aliases[legacy_key].upper()
        assert [
            entry["series_key"]
            for entry in observation.metadata["input_lineage"][:4]
        ] == [*aliases.values(), unrelated.key]
        assert observation.metadata["input_lineage"][4:] == [
            {"preserved": legacy_key},
            "opaque-lineage-entry",
        ]
        assert observation.metadata["preserved"] == {"series_key": legacy_key}

    migration = import_module("research.migrations.0020_census_api_series_identity")
    state_before_retry = {
        "series": list(
            NewSeries.objects.filter(source_id=census.pk)
            .order_by("pk")
            .values_list("pk", "key", "updated_at")
        ),
        "run": NewRun.objects.get(pk=run.pk).metadata,
        "observations": list(
            NewObservation.objects.filter(pk__in=observation_pks)
            .order_by("pk")
            .values_list("pk", "metadata")
        ),
    }
    migration.qualify_census_api_series(new_apps, schema_editor=None)
    migration.qualify_census_api_series(new_apps, schema_editor=None)
    assert {
        "series": list(
            NewSeries.objects.filter(source_id=census.pk)
            .order_by("pk")
            .values_list("pk", "key", "updated_at")
        ),
        "run": NewRun.objects.get(pk=run.pk).metadata,
        "observations": list(
            NewObservation.objects.filter(pk__in=observation_pks)
            .order_by("pk")
            .values_list("pk", "metadata")
        ),
    } == state_before_retry


@pytest.mark.django_db(transaction=True)
def test_real_0019_to_0020_legacy_census_raw_evidence_replays_full_history(
    monkeypatch,
    settings,
    tmp_path,
):
    from research.consumer_contract import _validate_run
    from research.models import (
        IngestionRun,
        Observation,
        RawArtifact,
        SeriesDefinition,
    )
    from research.official_data import _store_census_marts_observations_v2
    from research.services import record_provider_result
    from tests.test_consumer_official import (
        FROZEN_NOW,
        _census_api_result,
        _persist_inputs,
        _publish,
    )

    old_target = [("research", "0019_expand_ingestion_run_dataset")]
    new_target = [("research", "0020_census_api_series_identity")]
    MigrationExecutor(connection).migrate(old_target)
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    monkeypatch.setattr("research.consumer_contract.timezone.now", lambda: FROZEN_NOW)

    api_run = record_provider_result(
        _census_api_result(),
        persist=_store_census_marts_observations_v2,
    )
    assert api_run.status == IngestionRun.Status.SUCCESS
    aliases = {
        "census-mrts-44x72-sm-sa": "census-api-mrts-44x72-sm-sa",
        "census-mrts-44x72-sm-sa-mom": "census-api-mrts-44x72-sm-sa-mom",
        "census-mrts-44x72-sm-sa-yoy": "census-api-mrts-44x72-sm-sa-yoy",
    }
    legacy_by_qualified = {qualified: legacy for legacy, qualified in aliases.items()}

    # Reconstruct the exact data shape that existed at 0019: raw evidence was
    # already timestamped, while normalized API series and their lineage used
    # the unqualified Census release identities and run metadata had no
    # provider-specific ``retrieved_at`` replay key.
    for series in SeriesDefinition.objects.filter(source=api_run.source):
        if series.key in legacy_by_qualified:
            series.key = legacy_by_qualified[series.key]
            series.save(update_fields=["key", "updated_at"])
    run_metadata = deepcopy(api_run.metadata)
    assert run_metadata.pop("retrieved_at") == FROZEN_NOW.isoformat()
    for field in ("latest_value_dates", "series_date_coverage"):
        run_metadata[field] = {
            legacy_by_qualified.get(str(key).lower(), str(key)): value
            for key, value in run_metadata[field].items()
        }
    api_run.metadata = run_metadata
    api_run.save(update_fields=["metadata", "updated_at"])

    def legacy_key(value):
        raw = str(value)
        legacy = legacy_by_qualified.get(raw.lower(), raw)
        return legacy.upper() if raw.isupper() else legacy

    observation_ids = []
    for observation in Observation.objects.filter(
        source=api_run.source,
        batch_id=api_run.batch_id,
    ):
        metadata = deepcopy(observation.metadata)
        if isinstance(metadata.get("input_series"), list):
            metadata["input_series"] = [
                legacy_key(value) for value in metadata["input_series"]
            ]
        if metadata.get("input_series_id") is not None:
            metadata["input_series_id"] = legacy_key(metadata["input_series_id"])
        if isinstance(metadata.get("input_lineage"), list):
            for entry in metadata["input_lineage"]:
                if isinstance(entry, dict) and entry.get("series_key") is not None:
                    entry["series_key"] = legacy_key(entry["series_key"])
        observation.metadata = metadata
        observation.save(update_fields=["metadata", "updated_at"])
        observation_ids.append(observation.pk)

    artifact = RawArtifact.objects.get(run=api_run)
    evidence_bytes = (
        settings.RAW_ARTIFACT_ROOT
        / artifact.sha256[:2]
        / f"{artifact.sha256}.bin"
    ).read_bytes()
    immutable_witness = {
        "run_pk": api_run.pk,
        "run_batch": api_run.batch_id,
        "artifact_pk": artifact.pk,
        "artifact_sha256": artifact.sha256,
        "artifact_uri": artifact.uri,
        "evidence_bytes": evidence_bytes,
        "observation_ids": sorted(observation_ids),
    }

    MigrationExecutor(connection).migrate(new_target)
    api_run = IngestionRun.objects.get(pk=immutable_witness["run_pk"])
    artifact = RawArtifact.objects.get(pk=immutable_witness["artifact_pk"])
    migrated_observations = Observation.objects.filter(
        pk__in=immutable_witness["observation_ids"]
    ).select_related("series")

    assert api_run.batch_id == immutable_witness["run_batch"]
    assert "retrieved_at" not in api_run.metadata
    assert artifact.sha256 == immutable_witness["artifact_sha256"]
    assert artifact.uri == immutable_witness["artifact_uri"]
    assert (
        settings.RAW_ARTIFACT_ROOT
        / artifact.sha256[:2]
        / f"{artifact.sha256}.bin"
    ).read_bytes() == immutable_witness["evidence_bytes"]
    assert sorted(migrated_observations.values_list("pk", flat=True)) == immutable_witness[
        "observation_ids"
    ]
    assert {item.series.key for item in migrated_observations} == set(aliases.values())

    replayed = _validate_run("retail_history_api", api_run)
    assert replayed.run.pk == api_run.pk
    assert len(replayed.records) == api_run.row_count

    mandatory = _persist_inputs(monkeypatch, settings, tmp_path)
    mandatory["retail_history_api"] = api_run
    snapshot = _publish(mandatory)
    assert snapshot is not None
    assert snapshot.data["retail_history_coverage"]["status"] == "complete_history"
    assert snapshot.data["publication_input_identity"]["optional_effective"][
        "api_run_id"
    ] == api_run.pk


@pytest.mark.django_db(transaction=True)
def test_real_0019_to_0020_target_collision_rolls_back_without_partial_rename():
    executor = MigrationExecutor(connection)
    old_target = [("research", "0019_expand_ingestion_run_dataset")]
    new_target = [("research", "0020_census_api_series_identity")]
    executor.migrate(old_target)
    old_apps = executor.loader.project_state(old_target).apps
    OldSource = old_apps.get_model("research", "Source")
    OldSeries = old_apps.get_model("research", "SeriesDefinition")
    census = OldSource.objects.create(
        key="census",
        name="Collision Census API",
        license_scope="public official data",
    )
    legacy_keys = [
        "census-mrts-44x72-sm-sa",
        "census-mrts-44x72-sm-sa-mom",
        "census-mrts-44x72-sm-sa-yoy",
    ]
    legacy_pks = [
        OldSeries.objects.create(
            key=key,
            name=f"Collision legacy {index}",
            unit="%",
            frequency="monthly",
            source=census,
        ).pk
        for index, key in enumerate(legacy_keys)
    ]
    collision = OldSeries.objects.create(
        key="census-api-mrts-44x72-sm-sa-mom",
        name="Pre-existing qualified collision",
        unit="%",
        frequency="monthly",
        source=census,
    )

    executor = MigrationExecutor(connection)
    with pytest.raises(RuntimeError, match="cannot qualify Census API series"):
        executor.migrate(new_target)

    assert list(
        OldSeries.objects.filter(pk__in=legacy_pks).order_by("pk").values_list("key", flat=True)
    ) == legacy_keys
    assert OldSeries.objects.get(pk=collision.pk).key == (
        "census-api-mrts-44x72-sm-sa-mom"
    )

    OldSeries.objects.filter(pk=collision.pk).delete()
    executor = MigrationExecutor(connection)
    executor.migrate(new_target)
    new_apps = executor.loader.project_state(new_target).apps
    NewSeries = new_apps.get_model("research", "SeriesDefinition")
    assert list(
        NewSeries.objects.filter(pk__in=legacy_pks).order_by("pk").values_list("key", flat=True)
    ) == [
        "census-api-mrts-44x72-sm-sa",
        "census-api-mrts-44x72-sm-sa-mom",
        "census-api-mrts-44x72-sm-sa-yoy",
    ]
