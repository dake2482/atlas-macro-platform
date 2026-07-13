from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from research.models import (
    CodingAgentProfile,
    Company,
    DashboardSnapshot,
    FedDocument,
    FundLetter,
    GeneratedAnalysis,
    GitHubProject,
    GlossaryTerm,
    IngestionRun,
    Instrument,
    MarketBar,
    MetricSnapshot,
    ModelProfile,
    NewsItem,
    Observation,
    OptionContract,
    ResearchMention,
    SeriesDefinition,
    SupplyChainNode,
    Thesis,
)


class Command(BaseCommand):
    help = "Delete only records carrying deterministic clean-room demo markers."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        querysets = [
            (
                "dashboard snapshots",
                DashboardSnapshot.objects.filter(Q(source__key="demo-market") | Q(data__demo=True)),
            ),
            (
                "metric snapshots",
                MetricSnapshot.objects.filter(
                    Q(source__key="demo-market") | Q(metadata__demo=True)
                ),
            ),
            ("option contracts", OptionContract.objects.filter(source__key="demo-market")),
            ("market bars", MarketBar.objects.filter(source__key="demo-market")),
            (
                "observations",
                Observation.objects.filter(Q(source__key="demo-market") | Q(metadata__demo=True)),
            ),
            ("theses", Thesis.objects.filter(summary__startswith="演示日报 ")),
            (
                "generated analyses",
                GeneratedAnalysis.objects.filter(prompt_version__startswith="demo-"),
            ),
            (
                "news",
                NewsItem.objects.filter(
                    Q(source_url__contains="example.com/clean-room")
                    | Q(original_title__startswith="Clean-room demonstration")
                ),
            ),
            (
                "research mentions",
                ResearchMention.objects.filter(source_url__contains="example.com/clean-room"),
            ),
            (
                "fund letters",
                FundLetter.objects.filter(original_url__contains="example.com/clean-room"),
            ),
            (
                "Fed documents",
                FedDocument.objects.filter(original_url__contains="example.com/clean-room"),
            ),
            (
                "companies",
                Company.objects.filter(
                    Q(slug__startswith="clean-room-company-")
                    | Q(data_source_note__icontains="合成演示")
                ),
            ),
            (
                "supply-chain nodes",
                SupplyChainNode.objects.filter(
                    Q(slug__startswith="clean-room-node-") | Q(source_note__icontains="合成演示")
                ),
            ),
            ("model profiles", ModelProfile.objects.filter(slug__startswith="clean-room-model-")),
            (
                "coding agents",
                CodingAgentProfile.objects.filter(homepage__contains="example.com/clean-room"),
            ),
            ("GitHub projects", GitHubProject.objects.filter(repo__startswith="atlas-clean-room/")),
            ("glossary terms", GlossaryTerm.objects.filter(source_url__contains="example.com/")),
            ("instruments", Instrument.objects.filter(metadata__demo=True)),
            (
                "series",
                SeriesDefinition.objects.filter(
                    Q(source__key="demo-market")
                    | Q(description__icontains="Synthetic demonstration")
                ),
            ),
            ("ingestion runs", IngestionRun.objects.filter(source__key="demo-market")),
        ]
        counts = [(label, queryset.count()) for label, queryset in querysets]
        for label, count in counts:
            self.stdout.write(f"{label}: {count}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run only; no records deleted"))
            return
        with transaction.atomic():
            for _, queryset in querysets:
                queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Purged {sum(count for _, count in counts)} demo roots")
        )
