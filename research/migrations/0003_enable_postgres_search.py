from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("research", "0002_evidenceitem_fallbackevent_invalidation_outcome_and_more")]

    operations = [TrigramExtension()]
