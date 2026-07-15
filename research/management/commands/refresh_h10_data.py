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
        if "assets-fx" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "H.10 ingestion completed but assets-fx v1 could not publish "
                "or retain an audited snapshot"
            )
        if "global-dollar" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "H.10 ingestion completed but global-dollar v1 could not publish "
                "or retain an audited snapshot"
            )
        if "transmission-chain" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "H.10 ingestion completed but transmission-chain v1 remained stale"
            )
        self.stdout.write(self.style.SUCCESS("H.10 reference-data refresh completed"))
