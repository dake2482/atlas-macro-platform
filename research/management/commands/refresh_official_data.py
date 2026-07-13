from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_official_data


class Command(BaseCommand):
    help = "Refresh public-display-safe official sources and publish dashboard snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, help="Calendar year to refresh.")

    def handle(self, *args, **options):
        result = refresh_official_data(current_year=options.get("year"))
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        incomplete = [run for run in result["runs"] if run["status"] != "success"]
        if incomplete:
            raise CommandError(
                f"{len(incomplete)} official source refreshes were incomplete; dashboards retained"
            )
        self.stdout.write(self.style.SUCCESS("Official source refresh completed"))
