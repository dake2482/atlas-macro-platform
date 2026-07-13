from django.core.management.base import BaseCommand

from research.ai_supply_chain_catalog import sync_ai_supply_chain_catalog


class Command(BaseCommand):
    help = "Synchronize the reviewed 45-node AI supply-chain route catalogue."

    def handle(self, *args, **options):
        result = sync_ai_supply_chain_catalog()
        self.stdout.write(
            self.style.SUCCESS(
                "AI supply-chain catalogue synchronized: "
                f"created={result['created']} updated={result['updated']} total={result['total']}"
            )
        )
