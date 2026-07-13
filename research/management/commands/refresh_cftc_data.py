from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.tasks import refresh_cftc_sources


class Command(BaseCommand):
    help = "Refresh five years of official CFTC TFF futures-only and combined positions."

    def handle(self, *args, **options):
        summary = refresh_cftc_sources()
        self.stdout.write(
            str(
                {
                    "runs": len(summary["runs"]),
                    "row_count": summary["row_count"],
                    "failed": summary["failed"],
                    "partial": summary["partial"],
                }
            )
        )
        if summary["failed"] or summary["partial"]:
            raise CommandError("One or more CFTC TFF datasets failed or were incomplete")
        self.stdout.write(self.style.SUCCESS("CFTC TFF refresh completed"))
