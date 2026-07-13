from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from research.sec_company_facts import REVIEWED_COMPANIES, refresh_sec_company_data


class Command(BaseCommand):
    help = "Refresh the reviewed SEC annual-financials batch."

    def add_arguments(self, parser):
        parser.add_argument("--company", action="append", dest="companies", help="Reviewed company slug; repeatable.")
        parser.add_argument("--no-publish", action="store_true", help="Ingest diagnostics without publishing.")

    def handle(self, *args, **options):
        companies = options.get("companies")
        if companies:
            known = {item.slug for item in REVIEWED_COMPANIES}
            unknown = sorted(set(companies) - known)
            if unknown:
                raise CommandError(f"Unknown reviewed SEC company: {', '.join(unknown)}")
        result = refresh_sec_company_data(
            company_slugs=companies,
            publish=not options.get("no_publish", False) and not companies,
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("failure"):
            raise CommandError(result["failure"].get("reason", "SEC refresh failed"))
