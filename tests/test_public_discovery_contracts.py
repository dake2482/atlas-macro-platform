from __future__ import annotations

from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command

from research.models import FundLetter, GlossaryTerm

OFFICIAL_TERM_SLUGS = {
    "rrp",
    "tga",
    "sofr-iorb",
    "cross-currency-basis",
    "aoci",
    "net-liquidity",
    "vix-term-structure",
    "transmission-chain",
}


@pytest.mark.django_db
def test_official_glossary_sync_is_complete_source_linked_and_idempotent():
    first_output = StringIO()
    call_command("sync_official_glossary", stdout=first_output)

    terms = GlossaryTerm.objects.filter(slug__in=OFFICIAL_TERM_SLUGS)
    assert set(terms.values_list("slug", flat=True)) == OFFICIAL_TERM_SLUGS
    assert terms.count() == 8
    assert all(term.source_url.startswith("https://") for term in terms)
    assert not terms.filter(source_url__icontains="example.com").exists()
    assert "8 created" in first_output.getvalue()

    second_output = StringIO()
    call_command("sync_official_glossary", stdout=second_output)

    assert GlossaryTerm.objects.filter(slug__in=OFFICIAL_TERM_SLUGS).count() == 8
    assert "0 created, 8 updated" in second_output.getvalue()
    net_liquidity = GlossaryTerm.objects.get(slug="net-liquidity")
    assert "不是" in net_liquidity.definition
    assert "代理" in net_liquidity.term


@pytest.mark.django_db
def test_glossary_fragments_have_stable_ids_and_auto_open(client):
    call_command("sync_official_glossary", verbosity=0)

    response = client.get("/glossary/")

    assert response.status_code == 200
    body = response.content.decode()
    for slug in OFFICIAL_TERM_SLUGS:
        assert f'id="{slug}"' in body
    assert "HTMLDetailsElement" in body
    assert 'window.addEventListener("hashchange", openFragment)' in body


@pytest.mark.django_db
def test_llms_txt_lists_only_resolved_public_instances(client):
    call_command("sync_official_glossary", verbosity=0)
    published = FundLetter.objects.create(
        fund_name="Source-linked Fund",
        quarter="2026Q1",
        strategy="macro",
        stance="neutral",
        summary="Reviewed source-linked test record.",
        key_points=[],
        original_url="https://www.sec.gov/Archives/edgar/data/000000/test.htm",
        published_at=date(2026, 4, 1),
    )
    excluded = FundLetter.objects.create(
        fund_name="Synthetic Fund",
        quarter="2026Q1",
        strategy="macro",
        stance="neutral",
        summary="Synthetic test record.",
        key_points=[],
        original_url="https://example.com/clean-room/fund-letter",
        license_status="synthetic",
        published_at=date(2026, 4, 1),
    )

    response = client.get("/llms.txt")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert "# Atlas Macro" in body
    assert "http://testserver/glossary/" in body
    assert f"http://testserver{published.get_absolute_url()}" in body
    assert f"http://testserver{excluded.get_absolute_url()}" not in body
    assert "http://testserver/glossary/#rrp" in body
    assert "/credit/issuance/" not in body
    assert "/credit/events/" not in body
    assert "/admin/" not in body
    assert "/search/" not in body
    assert "/research/fund-letters/999999/" not in body
    assert "<slug:" not in body
    assert "<int:" not in body


@pytest.mark.django_db
def test_robots_blocks_auth_and_research_pdf_surfaces(client, settings):
    settings.SITE_URL = "https://atlas.example.test"

    response = client.get("/robots.txt")

    body = response.content.decode()
    assert response.status_code == 200
    assert "Disallow: /login/" in body
    assert "Disallow: /logout/" in body
    assert "Disallow: /static/research_pdfs/" in body
