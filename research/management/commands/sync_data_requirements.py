from django.core.management.base import BaseCommand
from django.utils import timezone

from research.data_catalog import DATA_REQUIREMENTS
from research.models import DataRequirement
from research.services import SOURCE_CATALOG, ensure_source


class Command(BaseCommand):
    help = "Synchronize the auditable page-level data coverage and procurement catalogue."

    def handle(self, *args, **options):
        for source_key in SOURCE_CATALOG:
            ensure_source(source_key)
        active_keys = set()
        now = timezone.now()
        for item in DATA_REQUIREMENTS:
            payload = item.copy()
            key = payload.pop("key")
            active_keys.add(key)
            DataRequirement.objects.update_or_create(
                key=key,
                defaults={**payload, "last_verified_at": now},
            )
        DataRequirement.objects.exclude(key__in=active_keys).delete()
        counts = {
            status: DataRequirement.objects.filter(status=status).count()
            for status, _ in DataRequirement.Status.choices
        }
        self.stdout.write(self.style.SUCCESS(f"Data requirements synchronized: {counts}"))
