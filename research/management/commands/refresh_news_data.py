from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.tasks import refresh_news_sources


class Command(BaseCommand):
    help = "Refresh metadata-only SEC, Treasury and BLS official news feeds."

    def handle(self, *args, **options):
        summary = refresh_news_sources()
        compact = {
            "runs": len(summary["runs"]),
            "rows": summary["row_count"],
            "failed": summary["failed"],
            "partial": summary["partial"],
        }
        self.stdout.write(str(compact))
        if compact["failed"]:
            raise CommandError("One or more official news feeds failed")
        else:
            self.stdout.write(self.style.SUCCESS("Official news refresh completed"))
