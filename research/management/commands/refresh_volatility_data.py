from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from research.volatility_contract import coordinate_fx_vol_dashboard


class Command(BaseCommand):
    help = "Publish strict volatility pages from already-ingested exact inputs."

    def handle(self, *args, **options):
        dashboards, stale_keys = coordinate_fx_vol_dashboard()
        self.stdout.write(
            str(
                {
                    "dashboard_keys": [item.key for item in dashboards],
                    "stale_dashboard_keys": sorted(stale_keys),
                }
            )
        )
        if stale_keys:
            raise CommandError(
                "Strict volatility publication unavailable for: "
                + ", ".join(sorted(stale_keys))
            )
        self.stdout.write(self.style.SUCCESS("Strict volatility publication completed"))
