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
        }
        self.stdout.write(str(compact))
        if compact["failed"] or compact["partial"]:
            raise CommandError("One or more official credit sources failed or were incomplete")
        else:
            self.stdout.write(self.style.SUCCESS("Official credit refresh completed"))
