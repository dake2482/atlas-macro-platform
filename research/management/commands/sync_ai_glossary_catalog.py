from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from research.ai_glossary_catalog import AI_GLOSSARY_TERMS
from research.models import GlossaryTerm


class Command(BaseCommand):
    help = "Idempotently install the reviewed, source-linked AI glossary public contract."

    def handle(self, *args, **options):
        created = 0
        updated = 0
        with transaction.atomic():
            for payload in AI_GLOSSARY_TERMS:
                slug = payload["slug"]
                _, was_created = GlossaryTerm.objects.update_or_create(
                    slug=slug,
                    defaults={key: value for key, value in payload.items() if key != "slug"},
                )
                created += int(was_created)
                updated += int(not was_created)

        self.stdout.write(
            self.style.SUCCESS(
                f"AI glossary synchronized: {created} created, {updated} updated."
            )
        )
