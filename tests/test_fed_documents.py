from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils import timezone

from research.context_processors import ROUTE_REQUIREMENT_KEYS
from research.data_catalog import DATA_REQUIREMENTS
from research.models import DataRequirement, FedDocument
from research.providers import FederalReserveRSSProvider, ProviderResult
from research.services import store_fed_documents


def _fed_document(slug: str, **overrides) -> FedDocument:
    fields = {
        "document_type": FedDocument.DocumentType.NEWS,
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "speaker": "",
        "official_description": f"Official description for {slug}",
        "summary": "",
        "key_points": [],
        "published_at": datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        "hawkish_score": None,
        "original_url": (
            f"https://www.federalreserve.gov/newsevents/pressreleases/{slug}.htm"
        ),
    }
    fields.update(overrides)
    return FedDocument.objects.create(**fields)


def _analysis_fields(*, status: str, score: int | None) -> dict:
    fields = {
        "summary": f"Atlas {status} analysis",
        "hawkish_score": score,
        "analysis_status": status,
        "analysis_model": "fixture-analysis-model",
        "analysis_prompt_version": "fed-language-v1",
        "analysis_generated_at": datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        "analysis_evidence": [{"id": "official-document", "label": "Official document"}],
    }
    if status == FedDocument.AnalysisStatus.REVIEWED:
        fields.update(
            reviewed_by="Fixture Reviewer",
            reviewed_at=datetime(2026, 7, 13, 14, 0, tzinfo=UTC),
        )
    return fields


def test_federal_reserve_rss_provider_names_description_as_official_metadata():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item>
      <title>Policy update</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260713a.htm</link>
      <description>Official RSS description</description>
      <pubDate>Mon, 13 Jul 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/feeds/press_monetary.xml"
        return httpx.Response(200, content=xml, request=request)

    with httpx.Client(
        base_url="https://www.federalreserve.gov",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = FederalReserveRSSProvider(client=client).feed(
            "press-monetary",
            document_type=FedDocument.DocumentType.STATEMENT,
        )

    assert result.ok
    assert result.records[0]["official_description"] == "Official RSS description"
    assert "summary" not in result.records[0]


@pytest.mark.django_db
def test_new_rss_document_starts_as_unanalysed_draft():
    result = ProviderResult(
        provider="federal-reserve",
        dataset="rss:press-all",
        records=[
            {
                "slug": "new-official-rss-row",
                "document_type": FedDocument.DocumentType.NEWS,
                "title": "New official RSS row",
                "official_description": "Official description, not Atlas analysis",
                "published_at": "2026-07-13T12:00:00+00:00",
                "original_url": (
                    "https://www.federalreserve.gov/newsevents/pressreleases/"
                    "new-official-rss-row.htm"
                ),
            }
        ],
    )

    assert store_fed_documents(result, None, None) == 1

    document = FedDocument.objects.get(slug="new-official-rss-row")
    assert document.official_description == "Official description, not Atlas analysis"
    assert document.summary == ""
    assert document.hawkish_score is None
    assert document.analysis_status == FedDocument.AnalysisStatus.DRAFT
    assert document.has_public_analysis is False
    assert document.has_public_score is False


@pytest.mark.django_db
def test_draft_legacy_enrichment_is_not_exposed_as_ai_or_score(client):
    document = _fed_document(
        "legacy-draft-hidden",
        summary="LEGACY SUMMARY MUST STAY PRIVATE",
        hawkish_score=0,
        analysis_evidence=[{"kind": "legacy_unverified"}],
    )

    detail = client.get(f"/fed/news/{document.slug}/").content.decode()
    listing = client.get("/fed/news/").content.decode()

    assert "Official description for legacy-draft-hidden" in detail
    assert "LEGACY SUMMARY MUST STAY PRIVATE" not in detail
    assert "LEGACY SUMMARY MUST STAY PRIVATE" not in listing
    assert "AI generated" not in detail
    assert 'class="mono text-5xl muted">—<' in detail


@pytest.mark.django_db
def test_complete_ai_generated_analysis_is_labelled_unreviewed_and_scored(client):
    document = _fed_document(
        "complete-ai-generated",
        **_analysis_fields(status=FedDocument.AnalysisStatus.AI_GENERATED, score=2),
    )

    detail = client.get(f"/fed/news/{document.slug}/").content.decode()

    assert "Atlas ai_generated analysis" in detail
    assert "AI generated · 未人工审核" in detail
    assert "fixture-analysis-model" in detail
    assert "fed-language-v1" in detail
    assert "Official document" in detail
    assert 'class="mono text-5xl negative">+2<' in detail


@pytest.mark.django_db
def test_reviewed_average_excludes_unreviewed_and_draft_scores(client):
    _fed_document(
        "reviewed-score-minus-two",
        **_analysis_fields(status=FedDocument.AnalysisStatus.REVIEWED, score=-2),
    )
    _fed_document(
        "unreviewed-score-five",
        **_analysis_fields(status=FedDocument.AnalysisStatus.AI_GENERATED, score=5),
    )
    _fed_document("draft-score-four", summary="Legacy", hawkish_score=4)

    hub = client.get("/fed/")
    hawkish = client.get("/fed/hawkish-dovish/")

    assert hub.status_code == 200
    assert hub.context["average_score"] == -2
    assert hawkish.status_code == 200
    assert hawkish.context["average_score"] == -2
    hawkish_body = hawkish.content.decode()
    assert "reviewed-score-minus-two" in hawkish_body
    assert "unreviewed-score-five" in hawkish_body
    assert "draft-score-four" not in hawkish_body
    assert "+0.4" not in hawkish_body


@pytest.mark.django_db
def test_fed_query_and_type_filters_cover_official_and_analysis_fields(client):
    _fed_document(
        "statement-title-needle",
        document_type=FedDocument.DocumentType.STATEMENT,
        title="TitleNeedle policy statement",
    )
    _fed_document(
        "speech-speaker-needle",
        document_type=FedDocument.DocumentType.SPEECH,
        speaker="SpeakerNeedle Governor",
        original_url=(
            "https://www.federalreserve.gov/newsevents/speech/"
            "speech-speaker-needle.htm"
        ),
    )
    _fed_document(
        "news-official-needle",
        official_description="OfficialNeedle in RSS metadata",
    )
    _fed_document(
        "news-analysis-needle",
        **{
            **_analysis_fields(status=FedDocument.AnalysisStatus.REVIEWED, score=1),
            "summary": "AtlasNeedle in reviewed analysis",
        },
    )

    speech_only = client.get("/fed/?q=Needle&type=speech")
    assert [item.slug for item in speech_only.context["documents"]] == [
        "speech-speaker-needle"
    ]

    fixed_news = client.get("/fed/news/?q=OfficialNeedle&type=speech")
    assert [item.slug for item in fixed_news.context["documents"]] == [
        "news-official-needle"
    ]
    assert 'name="type"' not in fixed_news.content.decode()

    analysis_search = client.get("/fed/news/?q=AtlasNeedle")
    assert [item.slug for item in analysis_search.context["documents"]] == [
        "news-analysis-needle"
    ]


@pytest.mark.django_db
def test_fed_summary_search_only_uses_analysis_that_is_actually_public(client):
    _fed_document(
        "summary-search-draft",
        summary="HiddenOnlyNeedle SummaryVisibilityNeedle",
        analysis_model="fixture-analysis-model",
        analysis_prompt_version="fed-language-v1",
        analysis_generated_at=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        analysis_evidence=[{"id": "draft-evidence"}],
    )
    _fed_document(
        "summary-search-rejected",
        **{
            **_analysis_fields(status=FedDocument.AnalysisStatus.REJECTED, score=1),
            "summary": "SummaryVisibilityNeedle rejected analysis",
            "reviewed_by": "Fixture Reviewer",
            "reviewed_at": datetime(2026, 7, 13, 14, 0, tzinfo=UTC),
        },
    )
    _fed_document(
        "summary-search-malformed",
        **{
            **_analysis_fields(status=FedDocument.AnalysisStatus.AI_GENERATED, score=2),
            "summary": "SummaryVisibilityNeedle malformed analysis",
            "analysis_evidence": [{"label": "Missing stable identifier"}],
        },
    )
    generated = _fed_document(
        "summary-search-generated",
        **{
            **_analysis_fields(status=FedDocument.AnalysisStatus.AI_GENERATED, score=3),
            "summary": "SummaryVisibilityNeedle generated analysis",
        },
    )
    reviewed = _fed_document(
        "summary-search-reviewed",
        **{
            **_analysis_fields(status=FedDocument.AnalysisStatus.REVIEWED, score=-1),
            "summary": "SummaryVisibilityNeedle reviewed analysis",
        },
    )

    hidden_only = client.get("/fed/news/?q=HiddenOnlyNeedle")
    assert list(hidden_only.context["documents"]) == []

    visible = client.get("/fed/news/?q=SummaryVisibilityNeedle")
    assert {item.pk for item in visible.context["documents"]} == {
        generated.pk,
        reviewed.pk,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "evidence",
    [
        [{}],
        [""],
        [{"label": "Display label only"}],
        [{"id": ""}],
        [{"source_id": "   "}],
        [{"id": " evidence-with-padding "}],
        [{"id": "evidence-1", "url": ""}],
        [{"id": "evidence-1", "url": "http://www.federalreserve.gov/test"}],
        [{"id": "evidence-1", "url": "/relative/path"}],
        [{"id": "evidence-1", "url": "javascript:alert(1)"}],
        [{"id": "evidence-1", "url": "https://user:pass@example.com/test"}],
        [{"id": "evidence-1", "url": "https://[invalid/test"}],
    ],
)
def test_fed_model_rejects_structurally_invalid_or_unsafe_evidence(evidence):
    document = FedDocument(
        document_type=FedDocument.DocumentType.NEWS,
        slug="invalid-evidence-fixture",
        title="Invalid evidence fixture",
        summary="Analysis with invalid evidence",
        published_at=timezone.now(),
        hawkish_score=1,
        original_url=(
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "invalid-evidence-fixture.htm"
        ),
        analysis_status=FedDocument.AnalysisStatus.AI_GENERATED,
        analysis_model="fixture-analysis-model",
        analysis_prompt_version="fed-language-v1",
        analysis_generated_at=timezone.now(),
        analysis_evidence=evidence,
    )

    with pytest.raises(ValidationError, match="id/source_id"):
        document.full_clean()


@pytest.mark.django_db
def test_bypassed_invalid_evidence_stays_hidden_from_pages_links_and_search(client):
    document = _fed_document("bypassed-invalid-evidence")
    FedDocument.objects.filter(pk=document.pk).update(
        summary="BYPASSED-PRIVATE-ANALYSIS-NEEDLE",
        hawkish_score=3,
        analysis_status=FedDocument.AnalysisStatus.AI_GENERATED,
        analysis_model="bypassed-model",
        analysis_prompt_version="bypassed-v1",
        analysis_generated_at=timezone.now(),
        analysis_evidence=[
            {
                "id": "",
                "label": "Unsafe evidence",
                "url": "javascript:alert(1)",
            }
        ],
    )
    document.refresh_from_db()

    assert document.has_public_analysis is False
    assert document.public_analysis_evidence == []

    detail = client.get(f"/fed/news/{document.slug}/").content.decode()
    hawkish = client.get("/fed/hawkish-dovish/").content.decode()
    search = client.get("/fed/news/?q=BYPASSED-PRIVATE-ANALYSIS-NEEDLE")
    assert "BYPASSED-PRIVATE-ANALYSIS-NEEDLE" not in detail
    assert "javascript:alert(1)" not in detail
    assert document.title not in hawkish
    assert list(search.context["documents"]) == []


@pytest.mark.django_db
def test_valid_source_id_and_https_evidence_remain_public(client):
    document = _fed_document(
        "valid-source-id-evidence",
        **{
            **_analysis_fields(status=FedDocument.AnalysisStatus.AI_GENERATED, score=1),
            "key_points": ["Valid evidence fixture"],
            "analysis_evidence": [
                {
                    "source_id": "federal-reserve:valid-source-id-evidence",
                    "label": "Federal Reserve official document",
                    "url": (
                        "https://www.federalreserve.gov/newsevents/pressreleases/"
                        "valid-source-id-evidence.htm"
                    ),
                }
            ],
        },
    )

    document.full_clean()
    assert document.has_public_analysis is True
    detail = client.get(f"/fed/news/{document.slug}/").content.decode()
    assert "Atlas ai_generated analysis" in detail
    assert "Federal Reserve official document" in detail
    assert (
        'href="https://www.federalreserve.gov/newsevents/pressreleases/'
        'valid-source-id-evidence.htm"' in detail
    )


@pytest.mark.django_db
def test_fed_model_rejects_incomplete_public_states_and_out_of_range_scores():
    incomplete = FedDocument(
        document_type=FedDocument.DocumentType.NEWS,
        slug="incomplete-public-analysis",
        title="Incomplete public analysis",
        summary="Looks complete but has no provenance",
        published_at=timezone.now(),
        original_url=(
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "incomplete-public-analysis.htm"
        ),
        analysis_status=FedDocument.AnalysisStatus.AI_GENERATED,
    )
    with pytest.raises(ValidationError):
        incomplete.full_clean()

    out_of_range = FedDocument(
        document_type=FedDocument.DocumentType.NEWS,
        slug="out-of-range-fed-score",
        title="Out of range score",
        published_at=timezone.now(),
        hawkish_score=6,
        original_url=(
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "out-of-range-fed-score.htm"
        ),
    )
    with pytest.raises(ValidationError):
        out_of_range.full_clean()


def test_fed_admin_and_requirement_routes_expose_separate_review_contracts():
    model_admin = admin.site._registry[FedDocument]
    assert {
        "official_description",
        "title",
        "published_at",
        "original_url",
    } <= set(model_admin.readonly_fields)
    assert "summary" not in model_admin.readonly_fields
    assert "analysis_evidence" not in model_admin.readonly_fields

    requirement = next(
        item for item in DATA_REQUIREMENTS if item["key"] == "fed-hawkish-dovish"
    )
    assert requirement["status"] == DataRequirement.Status.NEEDS_SOURCE
    for route_name in (
        "fed-hub",
        "fed-detail",
        "fed-speech-detail",
        "fed-news-detail",
        "hawkish-dovish",
    ):
        assert ROUTE_REQUIREMENT_KEYS[route_name] == ("fed", "fed-hawkish-dovish")
