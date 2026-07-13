from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from research.admin import ThesisAdmin
from research.models import (
    DashboardSnapshot,
    EvidenceItem,
    GeneratedAnalysis,
    IngestionRun,
    Instrument,
    Invalidation,
    MetricSnapshot,
    Observation,
    Source,
    SourceLicense,
    Thesis,
    Trigger,
)
from research.services import ensure_source
from research.tasks import generate_daily_research, publish_daily_evidence
from research.thesis_publication import (
    component_data_fingerprint,
    component_reference_fingerprint,
    daily_evidence_component_set_fingerprint,
    daily_evidence_payload_fingerprint,
    public_theses,
    publish_daily_evidence_snapshot,
    publish_theses,
    validate_daily_evidence_snapshot,
    validate_public_thesis,
    validate_thesis_readiness,
)
from research.views import _thesis_evidence_rows, _thesis_snapshot_metadata
from tests.thesis_factories import (
    build_complete_thesis,
    build_daily_components,
    build_daily_evidence,
)


@pytest.mark.django_db
def test_complete_public_thesis_is_shared_by_every_public_surface(client):
    thesis = build_complete_thesis(
        "VALID-PUBLIC-THESIS",
        report_date=date(1900, 1, 10),
    )

    home = client.get("/").content.decode()
    listing = client.get("/daily-report/").content.decode()
    detail = client.get(thesis.get_absolute_url()).content.decode()
    ledger = client.get("/regime-log/").content.decode()
    sitemap = client.get("/sitemap.xml").content.decode()
    llms = client.get("/llms.txt").content.decode()
    evidence_search = client.get("/daily-report/?q=evidence+body+1").content.decode()
    pending_filter = client.get("/daily-report/?status=pending").content.decode()
    hit_filter = client.get("/daily-report/?status=hit").content.decode()

    for body in (home, listing, detail, ledger):
        assert "VALID-PUBLIC-THESIS" in body
    assert thesis.get_absolute_url() in sitemap
    assert thesis.get_absolute_url() in llms
    assert "VALID-PUBLIC-THESIS" in evidence_search
    assert "VALID-PUBLIC-THESIS" in pending_filter
    assert "VALID-PUBLIC-THESIS" not in hit_filter
    assert "VALID-PUBLIC-THESIS evidence body 1" in home
    assert "VALID-PUBLIC-THESIS trigger" in home
    assert "VALID-PUBLIC-THESIS invalidation condition" in detail
    assert "daily-evidence v1" in detail
    assert str(thesis.source_snapshot.batch_id) in detail
    assert "LEGACY-EVIDENCE-MUST-NOT-RENDER" not in home + detail
    assert "LEGACY-TRIGGER-MUST-NOT-RENDER" not in home + detail
    assert "LEGACY-INVALIDATION-MUST-NOT-RENDER" not in home + detail
    assert "最新官方动态" in home
    assert ">未来事件</h2>" not in home
    assert "市场主线图等待完整批次" in home
    assert 'id="home-market-chart"' not in home
    assert '<option value="pending"' in listing
    assert '<option value="hit"' in listing
    assert '<option value="partial"' in listing
    assert '<option value="missed"' in listing


def _invalidate_thesis(thesis: Thesis, case: str) -> None:
    snapshot = thesis.source_snapshot
    if case == "future-date":
        Thesis.objects.filter(pk=thesis.pk).update(date=timezone.localdate() + timedelta(days=1))
    elif case == "future-published-at":
        Thesis.objects.filter(pk=thesis.pk).update(
            published_at=timezone.now() + timedelta(days=1)
        )
    elif case == "wrong-snapshot-key":
        snapshot.key = "not-daily-evidence"
        snapshot.save(update_fields=["key", "updated_at"])
    elif case == "missing-contract":
        data = dict(snapshot.data)
        data.pop("contract_version")
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "demo-snapshot":
        data = dict(snapshot.data)
        data["demo"] = True
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "refresh-failure":
        data = dict(snapshot.data)
        data["refresh_failure"] = {"reason": "fixture failure"}
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "stale-quality":
        snapshot.quality_status = Observation.Quality.STALE
        snapshot.save(update_fields=["quality_status", "updated_at"])
    elif case == "fallback-lineage":
        data = dict(snapshot.data)
        data["fallback_source_key"] = "internal"
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "malformed-evidence-ids":
        data = dict(snapshot.data)
        data["evidence_metric_ids"] = [{"invalid": True}, 1, 2]
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "oversized-evidence-id":
        data = dict(snapshot.data)
        data["evidence_metric_ids"] = ["9" * 5000, 1, 2]
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "malformed-source-list":
        data = dict(snapshot.data)
        data["source_keys"] = "internal"
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "deeply-nested-payload":
        data = dict(snapshot.data)
        nested: dict = {}
        cursor = nested
        for _index in range(40):
            cursor["child"] = {}
            cursor = cursor["child"]
        data["unexpected"] = nested
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "malformed-component-id":
        data = dict(snapshot.data)
        references = [dict(item) for item in data["component_snapshots"]]
        references[0]["snapshot_id"] = {"invalid": True}
        data["component_snapshots"] = references
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "nonobject-payload":
        snapshot.data = ["invalid"]
        snapshot.save(update_fields=["data", "updated_at"])
    elif case == "too-few-evidence":
        evidence_ids = list(
            thesis.evidence_items.order_by("pk").values_list("pk", flat=True)[:2]
        )
        thesis.evidence_items.filter(pk__in=evidence_ids).delete()
    elif case == "missing-trigger":
        thesis.trigger_items.all().delete()
    elif case == "missing-invalidation":
        thesis.invalidation_record.delete()
    elif case == "nested-unlicensed-source":
        blocked = Source.objects.create(
            key=f"blocked-{thesis.pk}",
            name="Blocked fixture",
            license_status=Source.LicenseStatus.RESTRICTED,
        )
        SourceLicense.objects.create(
            source=blocked,
            is_current=True,
            status=Source.LicenseStatus.RESTRICTED,
            scope="No public display",
            public_display_allowed=False,
        )
        reference = snapshot.data["component_snapshots"][0]
        component = DashboardSnapshot.objects.get(pk=reference["snapshot_id"])
        component_data = dict(component.data)
        component_data["source_keys"] = ["internal", blocked.key]
        component.data = component_data
        component.save(update_fields=["data", "updated_at"])
        parent_data = dict(snapshot.data)
        parent_data["source_keys"] = ["internal", blocked.key]
        snapshot.data = parent_data
        snapshot.save(update_fields=["data", "updated_at"])
    else:  # pragma: no cover - protects the test matrix itself
        raise AssertionError(case)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    [
        "future-date",
        "future-published-at",
        "wrong-snapshot-key",
        "missing-contract",
        "demo-snapshot",
        "refresh-failure",
        "stale-quality",
        "fallback-lineage",
        "malformed-evidence-ids",
        "oversized-evidence-id",
        "malformed-source-list",
        "deeply-nested-payload",
        "malformed-component-id",
        "nonobject-payload",
        "too-few-evidence",
        "missing-trigger",
        "missing-invalidation",
        "nested-unlicensed-source",
    ],
)
def test_invalid_thesis_is_excluded_from_all_public_surfaces(client, case):
    thesis = build_complete_thesis(
        f"INVALID-{case}",
        report_date=date(1900, 2, 1),
    )
    _invalidate_thesis(thesis, case)
    thesis.refresh_from_db()

    assert validate_public_thesis(thesis)
    assert not any(item.pk == thesis.pk for item in public_theses())
    assert client.get(thesis.get_absolute_url()).status_code == 404
    for path in ("/", "/daily-report/", "/regime-log/", "/sitemap.xml", "/llms.txt"):
        assert f"INVALID-{case}" not in client.get(path).content.decode(), path


@pytest.mark.django_db
def test_public_thesis_rechecks_and_recovers_after_current_licence_change():
    thesis = build_complete_thesis("LICENCE-RECOVERY", report_date=date(1900, 3, 1))
    source = Source.objects.get(key="internal")
    licence = source.licenses.get(is_current=True)

    assert any(item.pk == thesis.pk for item in public_theses())
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])
    assert not any(item.pk == thesis.pk for item in public_theses())
    licence.public_display_allowed = True
    licence.save(update_fields=["public_display_allowed", "updated_at"])
    assert any(item.pk == thesis.pk for item in public_theses())


@pytest.mark.django_db
def test_historical_report_renders_frozen_evidence_after_live_metric_changes(client):
    thesis = build_complete_thesis("FROZEN-EVIDENCE", report_date=date(1900, 3, 2))
    metric = thesis.evidence_items.order_by("pk").first().snapshot
    original_display = metric.display_value

    metric.value = 999
    metric.display_value = "999.00-LIVE-MUTATION"
    metric.batch_id = uuid.uuid4()
    metric.quality_status = Observation.Quality.FALLBACK
    metric.fallback_source = ensure_source("internal")
    metric.save(
        update_fields=[
            "value",
            "display_value",
            "batch_id",
            "quality_status",
            "fallback_source",
            "updated_at",
        ]
    )

    thesis.refresh_from_db()
    assert not validate_public_thesis(thesis)
    assert any(item.pk == thesis.pk for item in public_theses())
    detail = client.get(thesis.get_absolute_url()).content.decode()
    assert original_display in detail
    assert "999.00-LIVE-MUTATION" not in detail


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutation",
    ["summary", "evidence", "trigger", "invalidation", "reviewer"],
)
def test_post_review_content_change_hides_until_explicit_rereview(mutation):
    thesis = build_complete_thesis(
        f"REREVIEW-{mutation}",
        report_date={
            "summary": date(1900, 3, 3),
            "evidence": date(1900, 3, 4),
            "trigger": date(1900, 3, 5),
            "invalidation": date(1900, 3, 6),
            "reviewer": date(1900, 3, 7),
        }[mutation],
    )
    if mutation == "summary":
        Thesis.objects.filter(pk=thesis.pk).update(summary="Changed after review")
    elif mutation == "evidence":
        EvidenceItem.objects.filter(thesis=thesis).order_by("pk").update(
            body="Changed evidence after review"
        )
    elif mutation == "trigger":
        Trigger.objects.filter(thesis=thesis).update(condition="Changed trigger after review")
    elif mutation == "invalidation":
        Invalidation.objects.filter(thesis=thesis).update(
            condition="Changed invalidation after review"
        )
    else:
        Thesis.objects.filter(pk=thesis.pk).update(reviewed_by="tampered-reviewer")

    thesis.refresh_from_db()
    assert validate_public_thesis(thesis)
    assert not any(item.pk == thesis.pk for item in public_theses())

    outcome = publish_theses(
        Thesis.objects.filter(pk=thesis.pk),
        reviewer="explicit-rereviewer",
    )
    assert outcome.ok
    thesis.refresh_from_db()
    assert thesis.reviewed_by == "explicit-rereviewer"
    assert not validate_public_thesis(thesis)
    assert any(item.pk == thesis.pk for item in public_theses())


@pytest.mark.django_db
def test_admin_publish_action_is_atomic_and_publication_fields_are_readonly(client):
    valid = build_complete_thesis("ADMIN-VALID", report_date=date(1900, 4, 1), publish=False)
    invalid = build_complete_thesis(
        "ADMIN-INVALID",
        report_date=date(1900, 4, 2),
        publish=False,
    )
    invalid.trigger_items.all().delete()
    user = get_user_model().objects.create_superuser(
        username="publication-admin",
        email="admin@example.org",
        password="test-password",
    )
    client.force_login(user)
    changelist = reverse("admin:research_thesis_changelist")
    payload = {
        "action": "publish_selected",
        "_selected_action": [valid.pk, invalid.pk],
        "select_across": "0",
        "index": "0",
    }

    response = client.post(changelist, payload, follow=True)
    assert response.status_code == 200
    valid.refresh_from_db()
    invalid.refresh_from_db()
    assert not valid.is_published
    assert not invalid.is_published

    Trigger.objects.create(
        thesis=invalid,
        name="Repaired trigger",
        condition="Verified repair condition",
    )
    response = client.post(changelist, payload, follow=True)
    assert response.status_code == 200
    valid.refresh_from_db()
    invalid.refresh_from_db()
    assert valid.is_published and invalid.is_published
    assert valid.published_at == invalid.published_at
    assert valid.reviewed_at == invalid.reviewed_at
    assert valid.reviewed_by == invalid.reviewed_by == user.get_username()

    published_at = valid.published_at
    client.post(changelist, payload, follow=True)
    valid.refresh_from_db()
    assert valid.published_at == published_at
    model_admin = ThesisAdmin(Thesis, admin.site)
    assert {
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "is_published",
        "published_at",
    } <= set(model_admin.get_readonly_fields(None))
    assert {field.name for field in Thesis._meta.fields} <= set(
        model_admin.get_readonly_fields(None, valid)
    )
    assert not model_admin.has_delete_permission(None, valid)


@pytest.mark.django_db
def test_admin_blocks_published_graph_and_snapshot_mutation(client):
    thesis = build_complete_thesis("ADMIN-IMMUTABLE", report_date=date(1900, 4, 3))
    evidence = thesis.evidence_items.order_by("pk").first()
    user = get_user_model().objects.create_superuser(
        username="immutable-admin",
        email="immutable@example.org",
        password="test-password",
    )
    client.force_login(user)

    thesis_response = client.post(
        reverse("admin:research_thesis_change", args=[thesis.pk]),
        {"_save": "Save"},
    )
    evidence_response = client.post(
        reverse("admin:research_evidenceitem_change", args=[evidence.pk]),
        {"_save": "Save"},
    )
    snapshot_response = client.post(
        reverse(
            "admin:research_dashboardsnapshot_change",
            args=[thesis.source_snapshot_id],
        ),
        {"_save": "Save"},
    )

    assert thesis_response.status_code == 403
    assert evidence_response.status_code == 403
    assert snapshot_response.status_code == 403
    thesis.refresh_from_db()
    assert thesis.is_published
    assert not validate_public_thesis(thesis)


@pytest.mark.django_db
def test_database_constraint_rejects_inconsistent_publication_state():
    snapshot, _metrics = build_daily_evidence("constraint-fixture")
    with pytest.raises(IntegrityError), transaction.atomic():
        Thesis.objects.create(
            date=date(1900, 5, 1),
            regime="INVALID-CONSTRAINT",
            summary="Missing review state",
            evidence=[],
            triggers=[],
            invalidation="legacy",
            source_snapshot=snapshot,
            is_published=True,
            published_at=timezone.now(),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Thesis.objects.create(
            date=date(1900, 5, 2),
            regime="INVALID-UNPUBLISHED-TIMESTAMP",
            summary="Unpublished row with publication timestamp",
            evidence=[],
            triggers=[],
            invalidation="legacy",
            published_at=timezone.now(),
        )


@pytest.mark.django_db
def test_daily_evidence_v1_rejects_observation_lineage():
    thesis = build_complete_thesis(
        "OBSERVATION-LINEAGE",
        report_date=date(1900, 5, 3),
        publish=False,
    )
    source = ensure_source("internal")
    instrument = Instrument.objects.create(
        symbol="OBS-LINEAGE",
        name="Observation lineage fixture",
        asset_class="test",
    )
    observed_at = timezone.now() - timedelta(hours=1)
    observation = Observation.objects.create(
        instrument=instrument,
        value=1,
        value_date=observed_at,
        as_of=observed_at,
        fetched_at=observed_at,
        source=source,
    )
    evidence = thesis.evidence_items.order_by("pk").first()
    evidence.snapshot = None
    evidence.observation = observation
    evidence.save(update_fields=["snapshot", "observation", "updated_at"])

    outcome = publish_theses(
        Thesis.objects.filter(pk=thesis.pk),
        reviewer="observation-rejector",
    )

    assert not outcome.ok
    assert any("requires MetricSnapshot" in reason for reason in outcome.errors[thesis.pk])
    thesis.refresh_from_db()
    assert not thesis.is_published


@pytest.mark.django_db
def test_daily_evidence_rejects_null_frozen_metric_value_without_query_crash():
    snapshot, _metrics = build_daily_evidence("null-frozen-value")
    data = deepcopy(snapshot.data)
    evidence = data["evidence_items"][0]
    component = next(
        item
        for item in data["component_snapshots"]
        if item["page_key"] == evidence["component"]
    )
    component_metric = next(
        item
        for item in component["metrics"]
        if item["key"] == evidence["component_metric_key"]
    )
    evidence["value"] = None
    component_metric["value"] = None
    component["component_payload_sha256"] = component_reference_fingerprint(component)
    data["fingerprint"] = daily_evidence_payload_fingerprint(data)
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])

    errors = validate_daily_evidence_snapshot(snapshot)

    assert any("value is null or non-finite" in error for error in errors)


@pytest.mark.django_db
def test_daily_evidence_current_gate_uses_database_decimal_precision():
    snapshot, _metrics = build_daily_evidence("decimal-tail")
    data = deepcopy(snapshot.data)
    evidence = data["evidence_items"][0]
    component = next(
        item
        for item in data["component_snapshots"]
        if item["page_key"] == evidence["component"]
    )
    component_metric = next(
        item
        for item in component["metrics"]
        if item["key"] == evidence["component_metric_key"]
    )
    live_component = DashboardSnapshot.objects.get(pk=component["snapshot_id"])
    live_data = deepcopy(live_component.data)
    live_data["metrics"][0]["value"] = "1.0000000000000002"
    live_component.data = live_data
    live_component.save(update_fields=["data", "updated_at"])
    component_metric["value"] = "1.0000000000000002"
    component["component_data_sha256"] = component_data_fingerprint(live_data)
    component["component_payload_sha256"] = component_reference_fingerprint(component)
    data["component_set_sha256"] = daily_evidence_component_set_fingerprint(data)
    data["fingerprint"] = daily_evidence_payload_fingerprint(data)
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])

    assert not validate_daily_evidence_snapshot(
        snapshot,
        require_current_components=True,
        require_latest_snapshot=True,
    )


@pytest.mark.django_db
def test_daily_research_ignores_arbitrary_dashboard_and_never_creates_thesis():
    thesis_count = Thesis.objects.count()
    slug = f"daily-system-summary-{timezone.localdate().isoformat()}"
    source = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="rates",
        title="Arbitrary latest dashboard",
        as_of=timezone.now(),
        source=source,
        is_published=True,
        data={"demo": False, "contract_version": 1},
    )

    result = generate_daily_research()

    assert result["partial"] == 1
    assert not GeneratedAnalysis.objects.filter(slug=slug).exists()
    assert Thesis.objects.count() == thesis_count


@pytest.mark.django_db
def test_daily_research_uses_only_latest_ready_v1_and_is_idempotent():
    thesis_count = Thesis.objects.count()
    slug = f"daily-system-summary-{timezone.localdate().isoformat()}"
    snapshot, _metrics = build_daily_evidence("daily-task")

    first = generate_daily_research()
    first_analysis = GeneratedAnalysis.objects.get(slug=slug)
    first_generated_at = first_analysis.generated_at
    first_updated_at = first_analysis.updated_at
    first_body = first_analysis.body
    second = generate_daily_research()

    assert first["failed"] == second["failed"] == 0
    assert first["partial"] == second["partial"] == 0
    assert first["row_count"] == second["row_count"] == 1
    assert GeneratedAnalysis.objects.filter(slug=slug).count() == 1
    analysis = GeneratedAnalysis.objects.get(slug=slug)
    assert analysis.review_status == GeneratedAnalysis.ReviewStatus.DRAFT
    assert analysis.prompt_version == "daily-evidence-v1"
    assert analysis.evidence[0]["id"] == snapshot.pk
    assert analysis.evidence[0]["batch_id"] == str(snapshot.batch_id)
    assert analysis.evidence[0]["metric_ids"] == snapshot.data["evidence_metric_ids"]
    assert analysis.generated_at == first_generated_at
    assert analysis.updated_at == first_updated_at
    assert analysis.body == first_body
    assert Thesis.objects.count() == thesis_count


@pytest.mark.django_db
def test_daily_research_does_not_fall_back_around_new_invalid_candidate():
    slug = f"daily-system-summary-{timezone.localdate().isoformat()}"
    build_daily_evidence("older-valid")
    source = ensure_source("internal")
    DashboardSnapshot.objects.create(
        key="daily-evidence",
        title="New invalid attempt",
        as_of=timezone.now(),
        source=source,
        is_published=False,
        data={"demo": False, "contract_version": 1},
    )

    result = generate_daily_research()

    assert result["partial"] == 1
    assert not GeneratedAnalysis.objects.filter(slug=slug).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "review_status",
    [
        GeneratedAnalysis.ReviewStatus.AI,
        GeneratedAnalysis.ReviewStatus.REVIEWED,
        GeneratedAnalysis.ReviewStatus.REJECTED,
    ],
)
def test_daily_research_never_overwrites_a_non_draft_analysis(review_status):
    build_daily_evidence("reviewed-analysis")
    today = timezone.localdate()
    slug = f"daily-system-summary-{today.isoformat()}"
    existing = GeneratedAnalysis.objects.create(
        slug=slug,
        title="Human reviewed title",
        body="Human reviewed body",
        generated_at=timezone.now(),
        review_status=review_status,
    )

    result = generate_daily_research()

    existing.refresh_from_db()
    assert result["failed"] == 0
    assert result["partial"] == 1
    assert existing.title == "Human reviewed title"
    assert existing.body == "Human reviewed body"
    assert existing.review_status == review_status
    assert result["runs"][0]["metadata"]["existing_review_status"] == review_status


@pytest.mark.django_db
def test_daily_research_revalidates_inside_persistence_transaction(monkeypatch):
    build_daily_evidence("race-revalidation")
    slug = f"daily-system-summary-{timezone.localdate().isoformat()}"
    monkeypatch.setattr(
        "research.tasks.validate_daily_evidence_snapshot",
        lambda *args, **kwargs: ("licence revoked during persistence",),
    )

    result = generate_daily_research()

    assert result["failed"] == 1
    assert result["row_count"] == 0
    assert not GeneratedAnalysis.objects.filter(slug=slug).exists()


@pytest.mark.django_db
def test_daily_research_parent_lock_targets_only_snapshot(monkeypatch):
    build_daily_evidence("daily-research-lock-target")
    dashboard_lock_targets: list[tuple[str, ...]] = []
    original_select_for_update = QuerySet.select_for_update

    def tracked_select_for_update(queryset, *args, **kwargs):
        if queryset.model is DashboardSnapshot:
            dashboard_lock_targets.append(kwargs.get("of", ()))
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)

    result = generate_daily_research()

    assert result["failed"] == 0
    assert dashboard_lock_targets
    assert dashboard_lock_targets[0] == ("self",)


@pytest.mark.django_db
def test_daily_evidence_coordinator_freezes_exact_lineage_and_is_idempotent():
    current_time = timezone.now()
    components, metrics = build_daily_components(
        "coordinator-real-components",
        now=current_time,
    )

    first = publish_daily_evidence_snapshot(now=current_time)
    second = publish_daily_evidence_snapshot(
        now=current_time + timedelta(minutes=1)
    )

    assert first.ok and first.created
    assert second.ok and not second.created
    assert second.snapshot.pk == first.snapshot.pk
    assert DashboardSnapshot.objects.filter(key="daily-evidence").count() == 1
    data = first.snapshot.data
    assert data["research_date"] == timezone.localdate(current_time).isoformat()
    assert data["component_set_sha256"] == daily_evidence_component_set_fingerprint(
        data
    )
    assert data["fingerprint"] == daily_evidence_payload_fingerprint(data)
    assert set(data["evidence_metric_ids"]) == {item.pk for item in metrics}
    assert {item["snapshot_id"] for item in data["component_snapshots"]} == {
        item.pk for item in components
    }
    for reference in data["component_snapshots"]:
        component = next(item for item in components if item.pk == reference["snapshot_id"])
        assert reference["component_data_sha256"] == component_data_fingerprint(
            component.data
        )
        assert reference["component_payload_sha256"] == component_reference_fingerprint(
            reference
        )
    assert not validate_daily_evidence_snapshot(
        first.snapshot,
        now=current_time,
        require_current_components=True,
        require_latest_snapshot=True,
    )


@pytest.mark.django_db
def test_daily_evidence_coordinator_locks_only_primary_rows(monkeypatch):
    current_time = timezone.now()
    build_daily_components("coordinator-lock-targets", now=current_time)
    lock_calls: list[tuple[type, tuple[str, ...]]] = []
    original_select_for_update = QuerySet.select_for_update

    def tracked_select_for_update(queryset, *args, **kwargs):
        if queryset.model in {DashboardSnapshot, MetricSnapshot}:
            lock_calls.append((queryset.model, kwargs.get("of", ())))
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", tracked_select_for_update)

    outcome = publish_daily_evidence_snapshot(now=current_time)

    assert outcome.ok
    assert {model for model, _of in lock_calls} == {
        DashboardSnapshot,
        MetricSnapshot,
    }
    assert all(of == ("self",) for _model, of in lock_calls)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "refresh-failure",
        "missing-metric",
        "unlicensed-derived",
        "json-normalized-mismatch",
    ],
)
def test_daily_evidence_coordinator_rejects_incomplete_component_sets(failure):
    current_time = timezone.now()
    components, _metrics = build_daily_components(
        f"coordinator-{failure}",
        now=current_time,
    )
    component = components[0]
    if failure == "stale":
        component.quality_status = Observation.Quality.STALE
        component.save(update_fields=["quality_status", "updated_at"])
    elif failure == "refresh-failure":
        data = deepcopy(component.data)
        data["refresh_failure"] = {"reason": "fixture upstream outage"}
        component.data = data
        component.save(update_fields=["data", "updated_at"])
    elif failure == "missing-metric":
        data = deepcopy(component.data)
        data["metrics"] = []
        component.data = data
        component.save(update_fields=["data", "updated_at"])
    elif failure == "unlicensed-derived":
        licence = Source.objects.get(key="internal").licenses.get(is_current=True)
        licence.derived_display_allowed = False
        licence.reviewed_by = "fixture-reviewer"
        licence.reviewed_at = current_time
        licence.save(
            update_fields=[
                "derived_display_allowed",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
    else:
        data = deepcopy(component.data)
        data["metrics"][0]["value"] = "999"
        component.data = data
        component.save(update_fields=["data", "updated_at"])

    outcome = publish_daily_evidence_snapshot(now=current_time)

    assert not outcome.ok
    assert outcome.errors
    assert not DashboardSnapshot.objects.filter(key="daily-evidence").exists()


@pytest.mark.django_db
def test_daily_evidence_task_records_missing_components_as_partial():
    result = publish_daily_evidence()

    run = IngestionRun.objects.get(dataset="daily-evidence-v1")
    assert result["failed"] == 0
    assert result["partial"] == 1
    assert result["row_count"] == 0
    assert run.status == IngestionRun.Status.PARTIAL
    assert run.metadata["reasons"]
    assert "missing" in run.metadata["reason"]
    assert not DashboardSnapshot.objects.filter(key="daily-evidence").exists()


def _save_rehashed_parent(snapshot: DashboardSnapshot, data: dict) -> None:
    for reference in data.get("component_snapshots", []):
        reference["component_payload_sha256"] = component_reference_fingerprint(
            reference
        )
    data["component_set_sha256"] = daily_evidence_component_set_fingerprint(data)
    data["fingerprint"] = daily_evidence_payload_fingerprint(data)
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])


@pytest.mark.django_db
def test_temporal_contract_rejects_backdated_parent_and_thesis_date_mismatch():
    current_time = timezone.now()
    snapshot, _metrics = build_daily_evidence("temporal-parent", now=current_time)
    data = deepcopy(snapshot.data)
    data["research_date"] = (
        timezone.localdate(current_time) - timedelta(days=1)
    ).isoformat()
    _save_rehashed_parent(snapshot, data)

    snapshot.refresh_from_db()
    snapshot_errors = validate_daily_evidence_snapshot(snapshot, now=current_time)
    assert any("creation date" in error for error in snapshot_errors)

    thesis = build_complete_thesis(
        "temporal-thesis",
        report_date=date(1900, 7, 1),
        publish=False,
    )
    Thesis.objects.filter(pk=thesis.pk).update(date=date(1900, 7, 2))
    thesis.refresh_from_db()
    assert any(
        "does not match daily-evidence research_date" in error
        for error in validate_thesis_readiness(thesis)
    )


@pytest.mark.django_db
def test_temporal_contract_rejects_post_cutoff_frozen_evidence():
    current_time = timezone.now()
    snapshot, _metrics = build_daily_evidence("post-cutoff", now=current_time)
    data = deepcopy(snapshot.data)
    evidence = data["evidence_items"][0]
    reference = next(
        item
        for item in data["component_snapshots"]
        if item["page_key"] == evidence["component"]
    )
    component_metric = next(
        item
        for item in reference["metrics"]
        if item["key"] == evidence["component_metric_key"]
    )
    after_cutoff = (snapshot.created_at + timedelta(minutes=1)).isoformat()
    evidence["fetched_at"] = after_cutoff
    component_metric["fetched_at"] = after_cutoff
    _save_rehashed_parent(snapshot, data)

    snapshot.refresh_from_db()
    assert any(
        "publication cutoff" in error
        for error in validate_daily_evidence_snapshot(snapshot, now=current_time)
    )


@pytest.mark.django_db
def test_temporal_contract_rejects_future_same_day_parent_cutoff():
    local_date = timezone.localdate()
    validation_time = timezone.make_aware(
        datetime.combine(local_date, time(hour=12)),
        timezone.get_current_timezone(),
    )
    snapshot, _metrics = build_daily_evidence(
        "future-same-day-cutoff",
        now=validation_time,
    )
    DashboardSnapshot.objects.filter(pk=snapshot.pk).update(
        created_at=validation_time + timedelta(minutes=1)
    )
    snapshot.refresh_from_db()

    assert timezone.localdate(snapshot.created_at) == timezone.localdate(
        validation_time
    )
    assert any(
        "creation cutoff is in the future" in error
        for error in validate_daily_evidence_snapshot(
            snapshot,
            now=validation_time,
        )
    )


@pytest.mark.django_db
@pytest.mark.parametrize("late_child", ["component", "metric"])
def test_live_gate_rejects_database_child_created_after_parent(late_child):
    current_time = timezone.now()
    snapshot, metrics = build_daily_evidence(
        f"late-live-{late_child}",
        now=current_time,
    )
    if late_child == "component":
        component_id = snapshot.data["component_snapshots"][0]["snapshot_id"]
        DashboardSnapshot.objects.filter(pk=component_id).update(
            created_at=snapshot.created_at + timedelta(minutes=1)
        )
        expected = "live component was created after daily-evidence"
    else:
        MetricSnapshot.objects.filter(pk=metrics[0].pk).update(
            created_at=snapshot.created_at + timedelta(minutes=1)
        )
        expected = "was created after daily-evidence"

    errors = validate_daily_evidence_snapshot(
        snapshot,
        now=current_time,
        require_live_components=True,
    )

    assert any(expected in error for error in errors)


@pytest.mark.django_db
def test_old_but_fresh_parent_cannot_generate_a_new_days_draft():
    current_time = timezone.now()
    old_time = current_time - timedelta(days=1)
    snapshot, _metrics = build_daily_evidence("old-but-fresh", now=old_time)
    data = deepcopy(snapshot.data)
    future_deadline = (current_time + timedelta(days=1)).isoformat()
    for reference in data["component_snapshots"]:
        component = DashboardSnapshot.objects.get(pk=reference["snapshot_id"])
        component_data = deepcopy(component.data)
        component_data["fresh_until"] = future_deadline
        for metric_payload in component_data["metrics"]:
            metric_payload["fresh_until"] = future_deadline
        component.data = component_data
        component.save(update_fields=["data", "updated_at"])
        reference["fresh_until"] = future_deadline
        reference["metrics"] = deepcopy(component_data["metrics"])
        reference["component_data_sha256"] = component_data_fingerprint(
            component_data
        )
    for evidence in data["evidence_items"]:
        evidence["fresh_until"] = future_deadline
    _save_rehashed_parent(snapshot, data)

    result = generate_daily_research()

    assert result["failed"] == 0
    assert result["partial"] == 1
    target_slug = f"daily-system-summary-{snapshot.data['research_date']}"
    assert not GeneratedAnalysis.objects.filter(slug=target_slug).exists()
    assert "current local date" in result["runs"][0]["metadata"]["reason"]


@pytest.mark.django_db
def test_derived_permission_revocation_hides_and_recovers_estimated_report():
    thesis = build_complete_thesis(
        "DERIVED-LICENCE-RECOVERY",
        report_date=date(1900, 7, 3),
    )
    licence = Source.objects.get(key="internal").licenses.get(is_current=True)
    assert licence.public_display_allowed and licence.derived_display_allowed
    assert any(item.pk == thesis.pk for item in public_theses())

    licence.derived_display_allowed = False
    licence.save(update_fields=["derived_display_allowed", "updated_at"])
    assert not any(item.pk == thesis.pk for item in public_theses())

    licence.derived_display_allowed = True
    licence.save(update_fields=["derived_display_allowed", "updated_at"])
    assert any(item.pk == thesis.pk for item in public_theses())


@pytest.mark.django_db
def test_first_publication_detects_live_metric_tamper():
    thesis = build_complete_thesis(
        "FIRST-PUBLISH-LIVE-CHECK",
        report_date=date(1900, 7, 4),
        publish=False,
    )
    metric = thesis.evidence_items.order_by("pk").first().snapshot
    metric.value = Decimal("999")
    metric.save(update_fields=["value", "updated_at"])

    outcome = publish_theses(
        Thesis.objects.filter(pk=thesis.pk),
        reviewer="live-tamper-reviewer",
    )

    assert not outcome.ok
    assert any("does not match MetricSnapshot" in error for error in outcome.errors[thesis.pk])
    thesis.refresh_from_db()
    assert not thesis.is_published


@pytest.mark.django_db
def test_frozen_zero_value_and_component_deadline_are_rendered_exactly():
    thesis = build_complete_thesis(
        "ZERO-AND-DEADLINE",
        report_date=date(1900, 7, 5),
    )
    snapshot = thesis.source_snapshot
    data = deepcopy(snapshot.data)
    data["evidence_items"][0]["display_value"] = ""
    data["evidence_items"][0]["value"] = 0
    for evidence in data["evidence_items"]:
        evidence["fresh_until"] = (timezone.now() + timedelta(days=1)).isoformat()
    data["component_snapshots"][0]["fresh_until"] = (
        timezone.now() - timedelta(days=1)
    ).isoformat()
    snapshot.data = data
    snapshot.save(update_fields=["data", "updated_at"])
    thesis.refresh_from_db()

    assert _thesis_evidence_rows(thesis)[0]["value"] == "0"
    assert _thesis_snapshot_metadata(thesis)["stale"] is True


@pytest.mark.django_db
def test_public_theses_batches_licence_lookup_for_twenty_reports():
    for day in range(1, 21):
        build_complete_thesis(
            f"QUERY-BOUND-{day}",
            report_date=date(1900, 8, day),
        )

    with CaptureQueriesContext(connection) as captured:
        reports = public_theses()

    licence_queries = [
        query["sql"]
        for query in captured.captured_queries
        if "research_sourcelicense" in query["sql"].lower()
    ]
    assert len(reports) == 20
    assert len(licence_queries) == 1
    assert len(captured) < 20


@pytest.mark.django_db
def test_limited_public_selector_scans_past_invalid_newest_candidate():
    older = build_complete_thesis(
        "LIMITED-OLDER-VALID",
        report_date=date(1900, 9, 1),
    )
    newer = build_complete_thesis(
        "LIMITED-NEWER-INVALID",
        report_date=date(1900, 9, 2),
    )
    Thesis.objects.filter(pk=newer.pk).update(summary="tampered after review")

    selected = public_theses(limit=1)

    assert [item.pk for item in selected] == [older.pk]


@pytest.mark.django_db
def test_home_requests_only_one_validated_thesis(client, monkeypatch):
    requested_limits: list[int | None] = []

    def fake_public_theses(*, limit=None, **_kwargs):
        requested_limits.append(limit)
        return ()

    monkeypatch.setattr("research.views.public_theses", fake_public_theses)

    assert client.get("/").status_code == 200
    assert requested_limits == [1]


def test_daily_evidence_and_research_schedules_follow_official_refresh():
    evidence_schedule = settings.CELERY_BEAT_SCHEDULE[
        "publish-daily-evidence-every-2h"
    ]["schedule"]
    research_schedule = settings.CELERY_BEAT_SCHEDULE[
        "generate-daily-research-every-2h"
    ]["schedule"]

    assert "40 */2" in str(evidence_schedule)
    assert "45 */2" in str(research_schedule)
