from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_macro_official_data


class Command(BaseCommand):
    help = (
        "Refresh BEA, credential-gated Census MARTS, G.19 and NY Fed consumer releases with "
        "page-level publication gates."
    )

    def handle(self, *args, **options):
        summary = refresh_macro_official_data()
        compact = {
            "runs": len(summary["runs"]),
            "rows": sum(run["row_count"] for run in summary["runs"]),
            "failed": sum(run["status"] == "failed" for run in summary["runs"]),
            "partial": sum(run["status"] == "partial" for run in summary["runs"]),
            "dashboard_keys": summary["dashboard_keys"],
        }
        self.stdout.write(str(compact))
        if compact["failed"]:
            raise CommandError("One or more official macro sources failed")
        else:
            self.stdout.write(self.style.SUCCESS("Official macro refresh completed"))
