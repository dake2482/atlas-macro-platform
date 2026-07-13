from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.core.management import call_command

from research.ai_glossary_catalog import AI_GLOSSARY_TERM_SLUGS, AI_GLOSSARY_TERMS
from research.models import GlossaryTerm


def _manifest_ai_glossary_slugs() -> set[str]:
    manifest_path = Path(__file__).parents[1] / "assets" / "timsun_public_ai_contract.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prefix = "/ai-industry/chain/glossary/"
    return {
        item["path"].removeprefix(prefix).strip("/")
        for item in manifest["routes"]
        if item["family"] == "ai_glossary_term"
    }


def test_ai_glossary_catalog_exactly_matches_public_contract():
    expected = _manifest_ai_glossary_slugs()

    assert len(expected) == 32
    assert set(AI_GLOSSARY_TERM_SLUGS) == expected
    assert len(AI_GLOSSARY_TERMS) == len(expected)


@pytest.mark.django_db
def test_ai_glossary_sync_is_source_linked_original_and_idempotent():
    first_output = StringIO()
    call_command("sync_ai_glossary_catalog", stdout=first_output)

    terms = GlossaryTerm.objects.filter(slug__in=AI_GLOSSARY_TERM_SLUGS)
    assert terms.count() == 32
    assert "32 created, 0 updated" in first_output.getvalue()
    assert not terms.exclude(formula="").exists()
    assert not terms.filter(definition__icontains="演示").exists()
    assert not terms.filter(source_url__icontains="example.com").exists()
    assert all(urlparse(term.source_url).scheme == "https" for term in terms)
    assert all(urlparse(term.source_url).hostname for term in terms)

    second_output = StringIO()
    call_command("sync_ai_glossary_catalog", stdout=second_output)

    assert GlossaryTerm.objects.filter(slug__in=AI_GLOSSARY_TERM_SLUGS).count() == 32
    assert "0 created, 32 updated" in second_output.getvalue()


@pytest.mark.django_db
def test_ai_glossary_collection_is_scoped_and_every_detail_is_public(client):
    call_command("sync_official_glossary", verbosity=0)
    call_command("sync_ai_glossary_catalog", verbosity=0)

    collection = client.get("/ai-industry/chain/glossary/")
    assert collection.status_code == 200
    collection_body = collection.content.decode()
    assert "注意力机制" in collection_body
    assert "隔夜逆回购工具" not in collection_body
    assert collection_body.count('id="') >= 32

    for payload in AI_GLOSSARY_TERMS:
        path = f"/ai-industry/chain/glossary/{payload['slug']}/"
        response = client.get(path)
        body = response.content.decode()
        assert response.status_code == 200, path
        assert payload["term"] in body
        assert payload["definition"] in body
        assert payload["source_url"] in body
        assert f'id="{payload["slug"]}"' in body
        assert "open" in body

    assert client.get("/ai-industry/chain/glossary/not-in-contract/").status_code == 404


@pytest.mark.django_db
def test_ai_glossary_details_are_in_sitemap_and_llms_inventory(
    client, settings
):
    settings.SITE_URL = "http://public.example.test:3080"
    call_command("sync_official_glossary", verbosity=0)
    call_command("sync_ai_glossary_catalog", verbosity=0)

    sitemap = client.get("/sitemap.xml").content.decode()
    llms = client.get("/llms.txt").content.decode()

    for slug in AI_GLOSSARY_TERM_SLUGS:
        route = f"/ai-industry/chain/glossary/{slug}/"
        assert f"{settings.SITE_URL}{route}" in sitemap
        assert f"http://testserver{route}" in llms

    assert "http://testserver/glossary/#rrp" in llms
    assert "http://testserver/glossary/#attention" not in llms
