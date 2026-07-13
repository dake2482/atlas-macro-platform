from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from research.official_data import refresh_treasury_curve_data


class Command(BaseCommand):
    help = (
        "Backfill explicit annual U.S. Treasury nominal and real curves and publish "
        "the contract-v1 rate dashboards."
    )

    def add_arguments(self, parser):
        parser.add_argument("--start-year", type=int)
        parser.add_argument("--end-year", type=int)
        parser.add_argument(
            "--no-publish",
            action="store_true",
            help="Store the requested annual shard without coordinating public snapshots.",
        )

    def handle(self, *args, **options):
        try:
            result = refresh_treasury_curve_data(
                start_year=options.get("start_year"),
                end_year=options.get("end_year"),
                publish=not options.get("no_publish"),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        incomplete = [run for run in result["runs"] if run["status"] != "success"]
        if incomplete:
            raise CommandError(
                f"{len(incomplete)} Treasury annual curve refreshes were incomplete; "
                "dashboards retained"
            )
        if result.get("publish_requested", True) and {
            "yield-curve",
            "real-rates",
        } & set(result["stale_dashboard_keys"]):
            raise CommandError("Treasury curve contract did not publish a complete v1 snapshot")
        self.stdout.write(self.style.SUCCESS("Treasury curve backfill completed"))
