from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_prates_data


class Command(BaseCommand):
    help = "Refresh Federal Reserve PRATES IORB data and dependent dashboards."

    def handle(self, *args, **options):
        summary = refresh_prates_data()
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
            raise CommandError(run["error"] or "PRATES refresh incomplete; dashboards retained")
        if "subsurface" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "PRATES ingestion completed but the required subsurface v1 "
                "atomic publication failed"
            )
        if "transmission-chain" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "PRATES ingestion completed but transmission-chain v1 remained stale"
            )
        self.stdout.write(self.style.SUCCESS("PRATES IORB refresh completed"))
