from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("research", "0017_fed_document_analysis_provenance")]

    operations = [
        migrations.RemoveConstraint(
            model_name="releasevintageobservation",
            name="release_vintage_series_period_round_source",
        ),
        migrations.AddConstraint(
            model_name="releasevintageobservation",
            constraint=models.UniqueConstraint(
                fields=(
                    "series",
                    "value_date",
                    "release_date",
                    "estimate_round",
                    "source",
                    "batch_id",
                ),
                name="release_vintage_series_period_round_source_batch",
            ),
        ),
    ]
