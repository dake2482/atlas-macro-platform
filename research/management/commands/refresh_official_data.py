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
        if "fed-balance-sheet" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but the required fed-balance-sheet "
                "v1 atomic publication failed"
            )
        if "subsurface" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but the required subsurface v1 "
                "atomic publication failed"
            )
        if "operations" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but the required operations v1 "
                "atomic publication failed"
            )
        if "assets-fx" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but the required assets-fx v1 "
                "snapshot is stale or unavailable"
            )
        if "fx-vol" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but the required fx-vol v1 "
                "snapshot is stale or unavailable"
            )
        if "global-dollar" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but the required global-dollar v1 "
                "atomic publication failed"
            )
        if "transmission-chain" in result.get("stale_dashboard_keys", []):
            raise CommandError(
                "Official ingestion completed but transmission-chain v1 could "
                "not publish or retain a fully audited current snapshot"
            )
        self.stdout.write(self.style.SUCCESS("Official source refresh completed"))
