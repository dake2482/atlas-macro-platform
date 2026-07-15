from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from . import models
from .thesis_publication import publish_theses, unpublish_theses


class ImmutableSnapshotAdmin(admin.ModelAdmin):
    """Snapshots are written by coordinators and inspected read-only in Admin."""

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):  # pragma: no cover - defence in depth
        raise PermissionDenied("快照只能由经过验证的数据发布器写入。")


@admin.register(models.Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "license_status", "redistribution_allowed")
    search_fields = ("name", "key")


@admin.register(models.SourceLicense)
class SourceLicenseAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "status",
        "is_current",
        "public_display_allowed",
        "valid_from",
        "valid_until",
        "reviewed_at",
    )
    list_filter = ("is_current", "status", "public_display_allowed")
    search_fields = ("source__name", "source__key", "scope")


@admin.register(models.IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = ("dataset", "source", "status", "row_count", "started_at", "completed_at")
    list_filter = ("status", "source")
    readonly_fields = ("batch_id",)


@admin.register(models.Observation)
class ObservationAdmin(admin.ModelAdmin):
    list_display = ("series", "instrument", "value", "value_date", "quality_status", "source")
    list_filter = ("quality_status", "source")
    date_hierarchy = "value_date"


@admin.register(models.ReleaseVintageObservation)
class ReleaseVintageObservationAdmin(admin.ModelAdmin):
    list_display = (
        "series",
        "value_date",
        "release_date",
        "estimate_round",
        "value",
        "quality_status",
        "source",
    )
    list_filter = ("estimate_round", "quality_status", "source")
    search_fields = ("series__name", "series__key", "vintage_label")
    date_hierarchy = "release_date"
    readonly_fields = ("batch_id",)


@admin.register(models.Thesis)
class ThesisAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "regime",
        "confidence",
        "status",
        "review_status",
        "is_published",
        "published_at",
        "hit_rate",
        "simulated_return",
    )
    list_filter = ("is_published", "review_status", "status", "confidence", "regime")
    search_fields = ("summary", "regime")
    readonly_fields = (
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "publication_fingerprint",
        "is_published",
        "published_at",
    )
    actions = ("publish_selected", "unpublish_selected")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_published:
            fields.extend(field.name for field in self.model._meta.fields)
        return tuple(dict.fromkeys(fields))

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change and models.Thesis.objects.filter(pk=obj.pk, is_published=True).exists():
            raise PermissionDenied("已发布日报必须先撤回，才能编辑。")
        super().save_model(request, obj, form, change)

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.has_perm("research.publish_thesis"):
            actions.pop("publish_selected", None)
        if not request.user.has_perm("research.withdraw_thesis"):
            actions.pop("unpublish_selected", None)
        return actions

    @admin.action(description="审核并发布所选日报")
    def publish_selected(self, request, queryset):
        if not request.user.has_perm("research.publish_thesis"):
            raise PermissionDenied("缺少日报审核发布权限。")
        with transaction.atomic():
            outcome = publish_theses(queryset, reviewer=request.user.get_username())
            if outcome.ok:
                for thesis in models.Thesis.objects.filter(pk__in=outcome.published_ids):
                    self.log_change(
                        request,
                        thesis,
                        "通过版本化 daily-evidence 安全门；相同版本保持原发布时间",
                    )
        if not outcome.ok:
            details = "; ".join(
                f"Thesis {pk}: {', '.join(reasons)}"
                for pk, reasons in sorted(outcome.errors.items())
            )
            self.message_user(
                request,
                f"发布被安全门拒绝，所选日报均未更改。{details}",
                level=messages.ERROR,
            )
            return
        self.message_user(
            request,
            f"已原子审核并发布 {len(outcome.published_ids)} 篇日报。",
            level=messages.SUCCESS,
        )

    @admin.action(description="撤回所选日报")
    def unpublish_selected(self, request, queryset):
        if not request.user.has_perm("research.withdraw_thesis"):
            raise PermissionDenied("缺少日报撤回权限。")
        with transaction.atomic():
            withdrawn_ids = unpublish_theses(queryset)
            for thesis in models.Thesis.objects.filter(pk__in=withdrawn_ids):
                self.log_change(request, thesis, "撤回公开日报，保留审核记录")
        self.message_user(
            request,
            f"已撤回 {len(withdrawn_ids)} 篇日报。",
            level=messages.SUCCESS,
        )


@admin.register(models.NewsItem)
class NewsItemAdmin(admin.ModelAdmin):
    list_display = ("title", "source_name", "category", "published_at", "relevance")
    list_filter = ("source_name", "category", "sentiment")
    search_fields = ("title", "summary")


@admin.register(models.FedDocument)
class FedDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "document_type",
        "published_at",
        "analysis_status",
        "hawkish_score",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = ("document_type", "analysis_status", "published_at")
    search_fields = (
        "title",
        "speaker",
        "official_description",
        "summary",
    )
    readonly_fields = (
        "document_type",
        "slug",
        "title",
        "speaker",
        "official_description",
        "published_at",
        "original_url",
        "analysis_status",
        "reviewed_by",
        "reviewed_at",
        "created_at",
        "updated_at",
    )
    actions = ("mark_ai_generated", "review_selected", "reject_selected")

    def has_add_permission(self, request):
        return False

    @admin.action(description="标记为 AI 已生成（未人工审核）")
    def mark_ai_generated(self, request, queryset):
        changed = 0
        with transaction.atomic():
            for document in queryset.select_for_update():
                document.analysis_status = models.FedDocument.AnalysisStatus.AI_GENERATED
                document.reviewed_by = ""
                document.reviewed_at = None
                document.full_clean()
                document.save(
                    update_fields=[
                        "analysis_status",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
                changed += 1
        self.message_user(request, f"已标记 {changed} 篇完整分析为 AI 未审核。")

    @admin.action(description="人工审核通过所选分析")
    def review_selected(self, request, queryset):
        changed = 0
        with transaction.atomic():
            for document in queryset.select_for_update():
                document.analysis_status = models.FedDocument.AnalysisStatus.REVIEWED
                document.reviewed_by = request.user.get_username()
                document.reviewed_at = timezone.now()
                document.full_clean()
                document.save(
                    update_fields=[
                        "analysis_status",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
                changed += 1
        self.message_user(request, f"已人工审核 {changed} 篇分析。")

    @admin.action(description="拒绝所选分析")
    def reject_selected(self, request, queryset):
        changed = 0
        with transaction.atomic():
            for document in queryset.select_for_update():
                document.analysis_status = models.FedDocument.AnalysisStatus.REJECTED
                document.reviewed_by = request.user.get_username()
                document.reviewed_at = timezone.now()
                document.full_clean()
                document.save(
                    update_fields=[
                        "analysis_status",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
                changed += 1
        self.message_user(request, f"已拒绝 {changed} 篇分析；公开页不会展示其摘要或评分。")


@admin.register(models.Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "ticker", "sec_cik", "is_published", "quality_status", "data_as_of")
    list_filter = ("is_published", "quality_status", "primary_node__layer", "rating", "quality_grade")
    search_fields = ("name", "name_en", "ticker", "sec_cik")
    prepopulated_fields = {"slug": ("name_en",)}

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_published:
            return tuple(field.name for field in self.model._meta.fields)
        return ()

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.is_published:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_published:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(models.SECCompanyFact)
class SECCompanyFactAdmin(ImmutableSnapshotAdmin):
    list_display = ("company", "concept", "fiscal_year", "value", "filed_at", "accession_number")
    list_filter = ("taxonomy", "form", "quality_status", "source")
    search_fields = ("company__name", "concept", "accession_number", "identity_hash")


@admin.register(models.FinancialFact)
class FinancialFactAdmin(ImmutableSnapshotAdmin):
    list_display = ("company", "fiscal_year", "capital_expenditures_usd_m", "publication_batch_id", "quality_status")
    list_filter = ("quality_status", "form", "capex_definition")


@admin.register(models.SupplyChainNode)
class SupplyChainNodeAdmin(admin.ModelAdmin):
    list_display = ("name", "layer", "quadrant", "narrative_score", "revenue_growth")
    list_filter = ("layer", "quadrant")
    search_fields = ("name", "description")


@admin.register(models.MetricSnapshot)
class MetricSnapshotAdmin(ImmutableSnapshotAdmin):
    list_display = ("key", "display_value", "value_date", "quality_status", "source")
    list_filter = ("quality_status", "source")
    search_fields = ("key", "label")


@admin.register(models.DashboardSnapshot)
class DashboardSnapshotAdmin(ImmutableSnapshotAdmin):
    list_display = ("key", "title", "as_of", "quality_status", "is_published")
    list_filter = ("key", "quality_status", "is_published")
    search_fields = ("key", "title", "summary")


class ThesisRelationAdmin(admin.ModelAdmin):
    """Require withdrawal before changing any part of a published report graph."""

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.thesis_id and obj.thesis.is_published:
            return tuple(field.name for field in self.model._meta.fields)
        return super().get_readonly_fields(request, obj)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "thesis":
            kwargs["queryset"] = models.Thesis.objects.filter(is_published=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        existing_published = bool(
            change
            and self.model.objects.filter(pk=obj.pk, thesis__is_published=True).exists()
        )
        target_published = bool(obj.thesis_id and obj.thesis.is_published)
        if existing_published or target_published:
            raise PermissionDenied("已发布日报必须先撤回，才能修改关联内容。")
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.thesis_id and obj.thesis.is_published:
            return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        if queryset.filter(thesis__is_published=True).exists():
            raise PermissionDenied("已发布日报必须先撤回，才能删除关联内容。")
        super().delete_queryset(request, queryset)


@admin.register(models.EvidenceItem)
class EvidenceItemAdmin(ThesisRelationAdmin):
    list_display = ("label", "thesis", "analysis", "source", "value_date")
    search_fields = ("label", "body", "thesis__regime")


@admin.register(models.Trigger)
class TriggerAdmin(ThesisRelationAdmin):
    list_display = ("name", "thesis", "status", "triggered_at")
    list_filter = ("status",)
    search_fields = ("name", "condition", "thesis__regime")


@admin.register(models.Invalidation)
class InvalidationAdmin(ThesisRelationAdmin):
    list_display = ("thesis", "is_triggered", "observed_at")
    list_filter = ("is_triggered",)
    search_fields = ("condition", "thesis__regime")


for model in [
    models.DataRequirement,
    models.RawArtifact,
    models.Instrument,
    models.SeriesDefinition,
    models.MarketBar,
    models.QualityCheck,
    models.FallbackEvent,
    models.GeneratedAnalysis,
    models.Outcome,
    models.ResearchMention,
    models.FundLetter,
    models.SupplyChainEdge,
    models.ModelProfile,
    models.CodingAgentProfile,
    models.GitHubProject,
    models.GitHubProjectSnapshot,
    models.GlossaryTerm,
    models.OptionContract,
    models.CFTCPosition,
    models.TreasuryAuction,
]:
    admin.site.register(model)

admin.site.site_header = "Atlas Macro 数据与内容后台"
admin.site.site_title = "Atlas Macro Admin"
admin.site.index_title = "研究平台运维"
