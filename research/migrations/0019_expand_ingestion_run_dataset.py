from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("research", "0018_release_vintage_batch_identity"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ingestionrun",
            name="dataset",
            field=models.CharField(max_length=512),
        ),
    ]
