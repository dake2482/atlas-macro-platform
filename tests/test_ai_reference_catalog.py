from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.core.management import call_command

from research.ai_reference_catalog import (
    CODING_AGENT_CATALOG,
    MODEL_CATALOG,
    SUPERSEDED_MODEL_SLUGS,
)
from research.models import CodingAgentProfile, ModelProfile

CONTRACT_PATH = Path(__file__).parents[1] / "assets" / "timsun_public_ai_contract.json"
OFFICIAL_MODEL_SOURCE_HOSTS = {
    "ai.google.dev",
    "ai.meta.com",
    "api-docs.deepseek.com",
    "blog.google",
    "developers.openai.com",
    "mistral.ai",
    "openai.com",
    "qwen.ai",
    "www.anthropic.com",
    "www.deepseek.com",
    "x.ai",
}


@pytest.mark.django_db
def test_ai_reference_catalog_is_idempotent_and_source_linked():
    for slug in SUPERSEDED_MODEL_SLUGS:
        ModelProfile.objects.create(
            slug=slug,
            name=f"Superseded {slug}",
            provider="Superseded fixture",
            release_date="2026-01-01",
            description="This row must be removed by the reviewed catalogue sync.",
            sources=[{"label": "Vendor", "url": "https://example.org/superseded"}],
        )

    call_command("sync_ai_reference_catalog", verbosity=0)
    call_command("sync_ai_reference_catalog", verbosity=0)

    models = ModelProfile.objects.all()
    agents = CodingAgentProfile.objects.filter(
        slug__in=[item["slug"] for item in CODING_AGENT_CATALOG]
    )
    assert models.count() == len(MODEL_CATALOG)
    assert agents.count() == len(CODING_AGENT_CATALOG)
    assert all(model.sources for model in models)
    assert all(
        source["url"].startswith("https://")
        and source["verified_at"] == "2026-07-12"
        for model in models
        for source in model.sources
    )
    assert {
        urlparse(source["url"]).netloc
        for model in models
        for source in model.sources
    } <= OFFICIAL_MODEL_SOURCE_HOSTS
    assert {
        model.slug for model in models if model.capability_score is not None
    } == {"gpt-5-5"}
    assert all(agent.homepage.startswith("https://") for agent in agents)

    contract = json.loads(CONTRACT_PATH.read_text())
    expected_agent_slugs = {
        item["path"].rstrip("/").rsplit("/", 1)[-1]
        for item in contract["routes"]
        if item["family"] == "coding_agent"
    }
    expected_model_slugs = {
        item["path"].rstrip("/").rsplit("/", 1)[-1]
        for item in contract["routes"]
        if item["family"] == "model"
    }
    assert {item["slug"] for item in MODEL_CATALOG} == expected_model_slugs
    assert set(models.values_list("slug", flat=True)) == expected_model_slugs
    assert not ModelProfile.objects.filter(slug__in=SUPERSEDED_MODEL_SLUGS).exists()
    assert {item["slug"] for item in CODING_AGENT_CATALOG} == expected_agent_slugs


@pytest.mark.django_db
def test_ai_reference_catalog_detail_routes_and_sitemap_are_public(client):
    call_command("sync_ai_reference_catalog", verbosity=0)

    for item in MODEL_CATALOG:
        response = client.get(f"/ai-industry/chain/model-evolution/model/{item['slug']}/")
        assert response.status_code == 200
        assert item["name"] in response.content.decode()
    for item in CODING_AGENT_CATALOG:
        response = client.get(f"/ai-industry/vibe-coding/{item['slug']}/")
        assert response.status_code == 200
        assert item["name"] in response.content.decode()

    model_list = client.get("/ai-industry/chain/model-evolution/").content.decode()
    assert '/ai-industry/chain/model-evolution/model/gpt-5-5/' in model_list
    assert '/ai-industry/vibe-coding/claude-code/' in model_list
    agent_list = client.get("/ai-industry/vibe-coding/")
    assert agent_list.status_code == 200
    assert '/ai-industry/vibe-coding/claude-code/' in agent_list.content.decode()
    sitemap = client.get("/sitemap.xml").content.decode()
    assert '/ai-industry/chain/model-evolution/model/gpt-5-5/' in sitemap
    assert '/ai-industry/vibe-coding/claude-code/' in sitemap
    assert '/ai-industry/vibe-coding/' in sitemap
