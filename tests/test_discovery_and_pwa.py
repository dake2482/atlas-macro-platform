from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from xml.etree import ElementTree

import pytest
from django.contrib.staticfiles import finders
from django.utils import timezone

from research.models import Company, FundLetter, Source, SourceLicense, SupplyChainNode


@pytest.mark.django_db
def test_sitemap_is_xml_and_contains_static_and_dynamic_urls(client, seeded_platform, settings):
    settings.SITE_URL = "http://public.example.test:3080"
    settings.ALLOWED_HOSTS = ["internal.example.test"]
    node = SupplyChainNode.objects.create(
        slug="sitemap-verified-node",
        name="Sitemap Verified Node",
        layer="fixture",
        description="Reviewed fixture",
        source_note="Company IR",
    )
    source = Source.objects.create(key="fixture-sitemap-sec", name="SEC fixture", license_status="open")
    SourceLicense.objects.create(source=source, status="open", scope="Fixture", public_display_allowed=True)
    company = Company.objects.create(
        slug="sitemap-verified-company",
        name="Sitemap Verified Company",
        ticker="SMAP",
        primary_node=node,
        description="Reviewed fixture",
        data_source_note="SEC EDGAR",
        source=source,
        sec_cik="0000000002",
        publication_batch_id=uuid.uuid4(),
        fetched_at=timezone.now(),
        license_scope="Fixture",
        is_published=True,
        quality_status="fresh",
    )
    letter = FundLetter.objects.create(
        fund_name="Sitemap Verified Fund",
        quarter="2030Q1",
        strategy="macro",
        stance="neutral",
        summary="Reviewed fixture",
        key_points=[],
        original_url="https://example.org/sitemap-letter",
        published_at="2030-01-01",
    )

    response = client.get(
        "/sitemap.xml", HTTP_HOST="internal.example.test:9000"
    )

    assert response.status_code == 200
    assert response["Content-Type"].startswith(("application/xml", "text/xml"))
    document = ElementTree.fromstring(response.content)
    locations = {
        element.text
        for element in document.iter()
        if element.tag.rsplit("}", 1)[-1] == "loc" and element.text
    }
    expected_paths = {
        "/",
        "/assets/",
        "/rates/",
        "/liquidity/",
        company.get_absolute_url(),
        node.get_absolute_url(),
        letter.get_absolute_url(),
    }
    for path in expected_paths:
        assert f"{settings.SITE_URL}{path}" in locations, path
    assert all(
        location.startswith(f"{settings.SITE_URL}/") for location in locations
    )
    demo_letter = FundLetter.objects.filter(original_url__contains="example.com/clean-room").first()
    assert demo_letter
    assert not any(location.endswith(demo_letter.get_absolute_url()) for location in locations)


@pytest.mark.django_db
def test_robots_policy_protects_internal_surfaces(client, settings):
    settings.SITE_URL = "https://atlas.example.test"

    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert "User-agent: *" in body
    assert "Disallow: /admin/" in body
    assert "Disallow: /api/" in body
    assert "Disallow: /search/" in body
    assert "Sitemap: https://atlas.example.test/sitemap.xml" in body


def test_pwa_manifest_is_discoverable_and_valid_json():
    path = finders.find("research/manifest.webmanifest")
    assert path, "manifest.webmanifest must be collected by Django staticfiles"

    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    assert manifest["name"]
    assert manifest["short_name"]
    assert manifest["start_url"] == "/"
    assert manifest["display"] in {"standalone", "minimal-ui"}
    assert manifest["theme_color"].startswith("#")
    assert manifest["background_color"].startswith("#")
    assert manifest.get("icons"), "installable PWAs need at least one icon"


@pytest.mark.django_db
def test_pwa_manifest_endpoint_uses_the_static_manifest(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/manifest+json")
    assert response.json()["start_url"] == "/"
    assert response.json()["icons"]


def test_service_worker_caches_offline_fallback():
    path = finders.find("research/sw.js")
    assert path, "sw.js must be collected by Django staticfiles"

    source = Path(path).read_text(encoding="utf-8")
    assert "/offline/" in source
    assert "fetch" in source
    assert "caches" in source
    for asset in ("research/css/app.css", "research/js/app.js", "research/icon.svg"):
        assert finders.find(asset), f"service-worker shell asset is missing: {asset}"

    app_source = Path(finders.find("research/js/app.js")).read_text(encoding="utf-8")
    root_worker = re.search(r"register\([\"']/sw\.js", app_source)
    explicit_root_scope = re.search(r"scope\s*:\s*[\"']/[\"']", app_source)
    assert root_worker or explicit_root_scope, "service worker must control root-page navigation"


def test_sparse_chart_rows_render_as_gaps_instead_of_fabricated_zeroes():
    source = (
        Path(__file__).resolve().parents[1] / "assets" / "js" / "app.js"
    ).read_text(encoding="utf-8")

    assert 'value === null || value === undefined || value === ""' in source
    assert "Number.isFinite(numeric) ? numeric : null" in source
    assert "Number(row[key]) || 0" not in source


@pytest.mark.django_db
def test_root_service_worker_endpoint_can_control_navigation(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response["Content-Type"]
    assert response["Service-Worker-Allowed"] == "/"
    assert "/offline/" in response.content.decode()


@pytest.mark.django_db
def test_offline_fallback_page_is_self_contained(client):
    response = client.get("/offline/")
    assert response.status_code == 200
    body = response.content.decode().lower()
    assert "offline" in body or "离线" in body
