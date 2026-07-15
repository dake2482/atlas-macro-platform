from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_h8_data


class Command(BaseCommand):
    help = "Refresh Federal Reserve H.8 commercial-bank assets and reserves v1."

    def handle(self, *args, **options):
        summary = refresh_h8_data()
        run = summary["runs"][0]
        self.stdout.write(
            str(
                {
                    "source": run["source"],
                    "dataset": run["dataset"],
                    "status": run["status"],
                    "row_count": run["row_count"],
                    "dashboard_keys": summary["dashboard_keys"],
                    "stale_dashboard_keys": summary.get(
                        "stale_dashboard_keys", []
                    ),
                }
            )
        )
        if run["status"] != "success":
            raise CommandError(
                run["error"] or "H.8 refresh incomplete; reserves v1 retained"
            )
        if "reserves" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "H.8 ingestion succeeded but reserves v1 atomic publication failed"
            )
        if "transmission-chain" in summary.get("stale_dashboard_keys", []):
            raise CommandError(
                "H.8 ingestion succeeded but transmission-chain v1 remained stale"
            )
        self.stdout.write(self.style.SUCCESS("H.8 refresh completed"))
