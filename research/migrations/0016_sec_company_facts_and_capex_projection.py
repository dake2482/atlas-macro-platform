from django.db import migrations, models
import django.db.models.deletion
import uuid


def fail_closed_legacy_companies(apps, schema_editor):
    Company = apps.get_model("research", "Company")
    Company.objects.all().update(
        sec_cik="",
        publication_batch_id=None,
        fetched_at=None,
        license_scope="",
        is_published=False,
    )


class Migration(migrations.Migration):
    dependencies = [("research", "0015_thesis_review_publication_contract")]

    operations = [
        migrations.AddField(
            model_name="company", name="sec_cik", field=models.CharField(blank=True, db_index=True, max_length=10)
        ),
        migrations.AddField(
            model_name="company", name="source",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="companies", to="research.source"),
        ),
        migrations.AddField(
            model_name="company", name="fallback_source",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fallback_companies", to="research.source"),
        ),
        migrations.AddField(
            model_name="company", name="publication_batch_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="company", name="fetched_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="company", name="quality_status",
            field=models.CharField(choices=[("fresh", "正常"), ("stale", "过期"), ("fallback", "备用源"), ("estimated", "估算"), ("error", "异常")], default="error", max_length=20),
        ),
        migrations.AddField(
            model_name="company", name="license_scope", field=models.CharField(blank=True, max_length=240)
        ),
        migrations.AddField(
            model_name="company", name="is_published", field=models.BooleanField(db_index=True, default=False)
        ),
        migrations.AlterField(
            model_name="company",
            name="rating",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AlterField(
            model_name="company",
            name="quality_grade",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
        migrations.RunPython(fail_closed_legacy_companies, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="company",
            constraint=models.UniqueConstraint(condition=~models.Q(sec_cik=""), fields=("sec_cik",), name="company_nonblank_sec_cik_unique"),
        ),
        migrations.AddConstraint(
            model_name="company",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(is_published=False)
                    | (
                        models.Q(source__isnull=False)
                        & models.Q(publication_batch_id__isnull=False)
                        & models.Q(fetched_at__isnull=False)
                        & ~models.Q(license_scope="")
                        & models.Q(fallback_source__isnull=True)
                        & ~models.Q(quality_status="error")
                    )
                ),
                name="published_company_publication_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="company",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(is_published=False)
                    | (
                        ~models.Q(slug__in=("microsoft", "alphabet", "amazon", "meta"))
                        & ~models.Q(sec_cik__in=("0000789019", "0001652044", "0001018724", "0001326801"))
                    )
                    | models.Q(slug="microsoft", sec_cik="0000789019")
                    | models.Q(slug="alphabet", sec_cik="0001652044")
                    | models.Q(slug="amazon", sec_cik="0001018724")
                    | models.Q(slug="meta", sec_cik="0001326801")
                ),
                name="published_reviewed_company_identity",
            ),
        ),
        migrations.AddField(
            model_name="financialfact", name="period_start", field=models.DateField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="period_end", field=models.DateField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="fiscal_period", field=models.CharField(blank=True, max_length=12)
        ),
        migrations.AddField(
            model_name="financialfact", name="gross_profit_usd_m", field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="capital_expenditures_usd_m", field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="capex_intensity", field=models.DecimalField(blank=True, decimal_places=4, max_digits=8, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="capex_definition", field=models.CharField(blank=True, max_length=80)
        ),
        migrations.AddField(
            model_name="financialfact", name="capex_source_url", field=models.URLField(blank=True, max_length=800)
        ),
        migrations.AddField(
            model_name="financialfact", name="accession_number", field=models.CharField(blank=True, max_length=40)
        ),
        migrations.AddField(
            model_name="financialfact", name="form", field=models.CharField(blank=True, max_length=20)
        ),
        migrations.AddField(
            model_name="financialfact", name="source_url", field=models.URLField(blank=True, max_length=800)
        ),
        migrations.AddField(
            model_name="financialfact", name="publication_batch_id", field=models.UUIDField(blank=True, db_index=True, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="fetched_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="financialfact", name="quality_status",
            field=models.CharField(choices=[("fresh", "正常"), ("stale", "过期"), ("fallback", "备用源"), ("estimated", "估算"), ("error", "异常")], default="error", max_length=20),
        ),
        migrations.AddField(
            model_name="financialfact", name="license_scope", field=models.CharField(blank=True, max_length=240)
        ),
        migrations.AddField(
            model_name="financialfact", name="fallback_source",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fallback_financial_facts", to="research.source"),
        ),
        migrations.AddField(
            model_name="financialfact", name="metadata", field=models.JSONField(blank=True, default=dict)
        ),
        migrations.RemoveConstraint(model_name="financialfact", name="company_fiscal_year"),
        migrations.CreateModel(
            name="SECCompanyFact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("taxonomy", models.CharField(max_length=40)),
                ("concept", models.CharField(max_length=180)),
                ("unit", models.CharField(max_length=40)),
                ("value", models.DecimalField(decimal_places=4, max_digits=28)),
                ("period_start", models.DateField()),
                ("period_end", models.DateField()),
                ("fiscal_year", models.PositiveSmallIntegerField()),
                ("fiscal_period", models.CharField(default="FY", max_length=12)),
                ("form", models.CharField(max_length=20)),
                ("filed_at", models.DateField()),
                ("accession_number", models.CharField(max_length=40)),
                ("frame", models.CharField(blank=True, max_length=40)),
                ("fetched_at", models.DateTimeField()),
                ("quality_status", models.CharField(choices=[("fresh", "正常"), ("stale", "过期"), ("fallback", "备用源"), ("estimated", "估算"), ("error", "异常")], default="fresh", max_length=20)),
                ("license_scope", models.CharField(max_length=240)),
                ("identity_hash", models.CharField(max_length=64, unique=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sec_facts", to="research.company")),
                ("fallback_source", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="fallback_sec_company_facts", to="research.source")),
                ("ingestion_run", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sec_company_facts", to="research.ingestionrun")),
                ("raw_artifact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sec_company_facts", to="research.rawartifact")),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sec_company_facts", to="research.source")),
                ("source_license", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sec_company_facts", to="research.sourcelicense")),
            ],
            options={
                "ordering": ["company", "fiscal_year", "concept", "filed_at", "accession_number"],
                "indexes": [
                    models.Index(fields=["company", "concept", "period_end"], name="research_se_company_cdbe95_idx"),
                    models.Index(fields=["ingestion_run", "concept"], name="research_se_ingesti_2963dd_idx"),
                    models.Index(fields=["accession_number"], name="research_se_accessi_a2e1c7_idx"),
                ],
            },
        ),
        migrations.AddField(
            model_name="financialfact", name="capex_source_fact",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="financial_projections", to="research.seccompanyfact"),
        ),
        migrations.AddConstraint(
            model_name="financialfact",
            constraint=models.UniqueConstraint(condition=models.Q(publication_batch_id__isnull=True), fields=("company", "fiscal_year"), name="company_fiscal_year_unbatched_unique"),
        ),
        migrations.AddConstraint(
            model_name="financialfact",
            constraint=models.UniqueConstraint(fields=("company", "fiscal_year", "publication_batch_id"), name="company_fiscal_year_publication_batch"),
        ),
        migrations.AddConstraint(
            model_name="financialfact",
            constraint=models.CheckConstraint(
                condition=(models.Q(publication_batch_id__isnull=True) | (models.Q(period_start__isnull=False) & models.Q(period_end__isnull=False) & models.Q(capital_expenditures_usd_m__isnull=False) & models.Q(capex_source_fact__isnull=False) & models.Q(fetched_at__isnull=False) & ~models.Q(license_scope="") & models.Q(fallback_source__isnull=True) & models.Q(capital_expenditures_usd_m__gte=0))),
                name="published_financial_fact_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="rawartifact",
            constraint=models.UniqueConstraint(fields=("run", "sha256"), name="raw_artifact_run_sha256"),
        ),
        migrations.AddIndex(model_name="rawartifact", index=models.Index(fields=["sha256"], name="research_ra_sha256_18b497_idx")),
    ]
