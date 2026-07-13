from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models

LEGACY_NOTE = "Pre-0017 enrichment retained for manual provenance review."


def split_official_description_from_legacy_analysis(apps, schema_editor):
    FedDocument = apps.get_model("research", "FedDocument")
    for document in FedDocument.objects.all().iterator():
        summary = document.summary or ""
        key_points = document.key_points or []
        score = document.hawkish_score
        typical_rss_row = not (document.speaker or "").strip() and not key_points and score == 0

        document.analysis_status = "draft"
        document.analysis_model = ""
        document.analysis_prompt_version = ""
        document.analysis_generated_at = None
        document.reviewed_by = ""
        document.reviewed_at = None

        if typical_rss_row:
            document.official_description = summary
            document.summary = ""
            document.hawkish_score = None
            document.analysis_evidence = []
        else:
            legacy_evidence = {"kind": "legacy_unverified", "note": LEGACY_NOTE}
            if score is not None and not -5 <= score <= 5:
                legacy_evidence["legacy_hawkish_score"] = score
                document.hawkish_score = None
            document.analysis_evidence = [legacy_evidence]

        document.save(
            update_fields=[
                "official_description",
                "summary",
                "hawkish_score",
                "analysis_status",
                "analysis_model",
                "analysis_prompt_version",
                "analysis_generated_at",
                "analysis_evidence",
                "reviewed_by",
                "reviewed_at",
            ]
        )


class Migration(migrations.Migration):
    dependencies = [("research", "0016_sec_company_facts_and_capex_projection")]

    operations = [
        migrations.AddField(
            model_name="feddocument",
            name="official_description",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="feddocument",
            name="summary",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="feddocument",
            name="hawkish_score",
            field=models.SmallIntegerField(
                blank=True,
                null=True,
                validators=[MinValueValidator(-5), MaxValueValidator(5)],
            ),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="analysis_status",
            field=models.CharField(
                choices=[
                    ("draft", "待分析 / 待审核"),
                    ("ai_generated", "AI 已生成（未人工审核）"),
                    ("reviewed", "已人工审核"),
                    ("rejected", "已拒绝"),
                ],
                db_index=True,
                default="draft",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="analysis_model",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="analysis_prompt_version",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="analysis_generated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="analysis_evidence",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="reviewed_by",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="feddocument",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            split_official_description_from_legacy_analysis,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="feddocument",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(hawkish_score__isnull=True)
                    | models.Q(hawkish_score__gte=-5, hawkish_score__lte=5)
                ),
                name="fed_hawkish_score_range",
            ),
        ),
    ]
