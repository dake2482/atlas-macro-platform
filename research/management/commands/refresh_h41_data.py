from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_h41_data


class Command(BaseCommand):
    help = "Refresh the Federal Reserve H.4.1 balance-sheet package and dashboards."

    def handle(self, *args, **options):
        summary = refresh_h41_data()
        run = summary["runs"][0]
        self.stdout.write(
            str(
                {
                    "source": run["source"],
                    "dataset": run["dataset"],
                    "status": run["status"],
                    "row_count": run["row_count"],
                    "dashboard_keys": summary["dashboard_keys"],
                }
            )
        )
        if run["status"] != "success":
            raise CommandError(run["error"] or "H.4.1 refresh incomplete; dashboards retained")
        else:
            self.stdout.write(self.style.SUCCESS("H.4.1 refresh completed"))
