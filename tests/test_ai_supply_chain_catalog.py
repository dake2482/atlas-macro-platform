from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.management import call_command

from research.ai_supply_chain_catalog import (
    AI_SUPPLY_CHAIN_LAYERS,
    AI_SUPPLY_CHAIN_NODE_CATALOG,
)
from research.models import Company, SupplyChainNode


def _manifest_node_slugs() -> set[str]:
    manifest = json.loads(
        (Path(__file__).parents[1] / "assets" / "timsun_public_ai_contract.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        route["path"].rstrip("/").rsplit("/", 1)[-1]
        for route in manifest["routes"]
        if route["family"] == "supply_chain_node"
    }


def test_ai_supply_chain_catalog_matches_public_route_contract_and_has_primary_sources():
    slugs = {item.slug for item in AI_SUPPLY_CHAIN_NODE_CATALOG}
    assert len(slugs) == 45
    assert slugs == _manifest_node_slugs()
    assert Counter(item.layer for item in AI_SUPPLY_CHAIN_NODE_CATALOG) == {
        layer: 5 for layer in AI_SUPPLY_CHAIN_LAYERS
    }
    assert all(item.source_note.startswith("https://") for item in AI_SUPPLY_CHAIN_NODE_CATALOG)
    assert all("example.com" not in item.source_note for item in AI_SUPPLY_CHAIN_NODE_CATALOG)
    assert all("timsun.net" not in item.source_note for item in AI_SUPPLY_CHAIN_NODE_CATALOG)


@pytest.mark.django_db
def test_ai_supply_chain_sync_is_idempotent_clears_demo_numbers_and_creates_no_companies():
    SupplyChainNode.objects.create(
        slug="advanced-nodes",
        name="old demo",
        layer="old",
        description="old demo",
        thesis="unsupported thesis",
        quadrant="核心",
        narrative_score=Decimal("99"),
        revenue_growth=Decimal("88"),
        gross_margin=Decimal("77"),
        median_pe=Decimal("66"),
        median_ps=Decimal("55"),
        market_cap_usd_m=Decimal("4444"),
        source_note="合成演示数据",
    )

    call_command("sync_ai_supply_chain_catalog", verbosity=0)
    call_command("sync_ai_supply_chain_catalog", verbosity=0)

    catalog_slugs = [item.slug for item in AI_SUPPLY_CHAIN_NODE_CATALOG]
    nodes = SupplyChainNode.objects.filter(slug__in=catalog_slugs)
    assert nodes.count() == 45
    assert nodes.values("layer").distinct().count() == 9
    assert Company.objects.count() == 0
    assert not nodes.exclude(narrative_score=0).exists()
    for field in (
        "revenue_growth",
        "gross_margin",
        "median_pe",
        "median_ps",
        "market_cap_usd_m",
    ):
        assert not nodes.exclude(**{f"{field}__isnull": True}).exists()
    assert not nodes.exclude(thesis="").exists()
    assert not nodes.exclude(quadrant="资料目录").exists()
    assert all(node.source_note.startswith("https://") for node in nodes)


@pytest.mark.django_db
def test_ai_supply_chain_node_routes_market_map_and_sitemap_are_public(client):
    call_command("sync_ai_supply_chain_catalog", verbosity=0)

    market_map = client.get("/ai-industry/market-map/")
    assert market_map.status_code == 200
    market_body = market_map.content.decode()
    assert len(market_map.context["nodes"]) == 45
    assert AI_SUPPLY_CHAIN_NODE_CATALOG[0].name in market_body
    assert "资料目录" in market_body

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    sitemap_body = sitemap.content.decode()
    for item in AI_SUPPLY_CHAIN_NODE_CATALOG:
        path = f"/ai-industry/chain/semiconductor-manufacturing/{item.slug}/"
        detail = client.get(path)
        assert detail.status_code == 200
        detail_body = detail.content.decode()
        assert item.name in detail_body
        assert item.source_note in detail_body
        assert path in sitemap_body
