from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.berkshire_letters import refresh_berkshire_letters


class Command(BaseCommand):
    help = "Refresh metadata-only Berkshire Hathaway official shareholder-letter links."

    def handle(self, *args, **options):
        summary = refresh_berkshire_letters()
        run = summary["runs"][0]
        self.stdout.write(
            str(
                {
                    "source": run["source"],
                    "status": run["status"],
                    "row_count": run["row_count"],
                    "first_year": run["metadata"].get("first_year"),
                    "last_year": run["metadata"].get("last_year"),
                }
            )
        )
        if summary["failed"] or summary["partial"]:
            raise CommandError(run["error"] or "Berkshire letter index is incomplete")
        self.stdout.write(self.style.SUCCESS("Berkshire letter metadata refresh completed"))
