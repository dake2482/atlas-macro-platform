from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_h10_data


class Command(BaseCommand):
    help = "Refresh Federal Reserve H.10 FX reference data and dashboards."

    def handle(self, *args, **options):
        summary = refresh_h10_data()
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
            raise CommandError(run["error"] or "H.10 refresh incomplete; dashboards retained")
        self.stdout.write(self.style.SUCCESS("H.10 reference-data refresh completed"))
