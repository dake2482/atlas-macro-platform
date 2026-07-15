from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_credit_official_data


class Command(BaseCommand):
    help = "Refresh public Treasury HQM and Federal Reserve SLOOS credit proxies."

    def handle(self, *args, **options):
        summary = refresh_credit_official_data()
        compact = {
            "runs": len(summary["runs"]),
            "rows": sum(run["row_count"] for run in summary["runs"]),
            "failed": sum(run["status"] == "failed" for run in summary["runs"]),
            "partial": sum(run["status"] == "partial" for run in summary["runs"]),
            "dashboard_keys": summary["dashboard_keys"],
            "stale_dashboard_keys": summary.get("stale_dashboard_keys", []),
            "credit_refresh_id": summary.get("credit_refresh_id"),
        }
        self.stdout.write(str(compact))
        stale = compact["stale_dashboard_keys"]
        legacy_incomplete = (
            "stale_dashboard_keys" not in summary
            and (compact["failed"] or compact["partial"])
        )
        if stale or legacy_incomplete:
            raise CommandError("One or more official credit sources failed or were incomplete")
        else:
            self.stdout.write(self.style.SUCCESS("Official credit refresh completed"))
