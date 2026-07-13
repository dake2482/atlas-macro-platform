from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.tasks import refresh_github_sources


class Command(BaseCommand):
    help = "Refresh the reviewed GitHub application radar and daily snapshots."

    def handle(self, *args, **options):
        summary = refresh_github_sources()
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
        if summary.get("failed") or summary.get("partial"):
            raise CommandError("One or more GitHub repositories failed or were incomplete")
        else:
            self.stdout.write(self.style.SUCCESS("GitHub radar refresh completed"))
