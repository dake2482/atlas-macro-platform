from django.core.management.base import BaseCommand

from research.ai_reference_catalog import sync_ai_reference_catalog


class Command(BaseCommand):
    help = "Synchronize reviewed official AI model and coding-agent product metadata."

    def handle(self, *args, **options):
        result = sync_ai_reference_catalog()
        self.stdout.write(self.style.SUCCESS(f"AI reference catalogue synchronized: {result}"))
