"""Application services for lineage-aware ingestion and data access."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import (
    CFTCPosition,
    FedDocument,
    GitHubProject,
    GitHubProjectSnapshot,
    IngestionRun,
    Instrument,
    Observation,
    QualityCheck,
    RawArtifact,
    ReleaseVintageObservation,
    SeriesDefinition,
    Source,
    SourceLicense,
    TreasuryAuction,
)
from .providers import ProviderResult

SOURCE_CATALOG: dict[str, dict[str, Any]] = {
    "fred": {
        "name": "Federal Reserve Economic Data",
        "homepage": "https://fred.stlouisfed.org/",
        "kind": "aggregator",
        "license_status": Source.LicenseStatus.REVIEW,
        "license_scope": "Each series retains its upstream owner's rights; no blanket public redistribution",
        "redistribution_allowed": False,
        "public_display_allowed": False,
        "derived_display_allowed": False,
        "historical_storage_allowed": False,
        "ai_use_allowed": False,
        "terms_url": "https://fred.stlouisfed.org/docs/api/terms_of_use.html",
        "attribution": "Federal Reserve Bank of St. Louis",
    },
    "ny-fed-markets": {
        "name": "Federal Reserve Bank of New York Markets Data",
        "homepage": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Attributed official reference-rate display; NY Fed disclaimer applies",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.newyorkfed.org/privacy/termsofuse",
        "attribution": "Federal Reserve Bank of New York",
        "required_notice": (
            f"© {date.today().year} Federal Reserve Bank of New York. Content from the New "
            "York Fed subject to the Terms of Use at newyorkfed.org. The SOFR and EFFR data "
            "are subject to those Terms. The New York Fed is not responsible for publication "
            "of these data by Atlas Macro, does not sanction or endorse this republication, "
            "and has no liability for its use. Atlas Macro is not affiliated with the New York "
            "Fed. SOFR data use transaction data supplied under licence by DTCC Solutions LLC; "
            "DTCC Solutions, its affiliates and upstream providers have no liability for this material."
        ),
    },
    "us-treasury-rates": {
        "name": "U.S. Treasury Daily Interest Rates",
        "homepage": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Attributed U.S. government data",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "attribution": "U.S. Department of the Treasury",
    },
    "treasury-fiscal-data": {
        "name": "U.S. Treasury FiscalData",
        "homepage": "https://fiscaldata.treasury.gov/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed U.S. government FiscalData API output; Treasury seals, "
            "marks, site layout and third-party intellectual property excluded"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": (
            "https://www.treasurydirect.gov/legal-information/developers/"
            "web-api-terms/"
        ),
        "attribution": "U.S. Department of the Treasury, FiscalData",
        "required_notice": (
            "Source: U.S. Department of the Treasury, FiscalData API. Atlas Macro "
            "is not affiliated with or endorsed by the U.S. Department of the Treasury. "
            "Treasury seals, marks, site layout and third-party material are not reproduced."
        ),
    },
    "bls": {
        "name": "U.S. Bureau of Labor Statistics",
        "homepage": "https://www.bls.gov/developers/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Public BLS data; access date and non-endorsement disclaimer required",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.bls.gov/developers/termsOfService.htm",
        "attribution": "U.S. Bureau of Labor Statistics",
        "required_notice": (
            "Source: U.S. Bureau of Labor Statistics. Access time is shown on each component. "
            "Atlas Macro is not endorsed or certified by BLS."
        ),
    },
    "dol-eta-ui": {
        "name": "U.S. Department of Labor Weekly UI Claims",
        "homepage": "https://oui.doleta.gov/unemploy/claims.asp",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Federal-government public-domain unemployment-insurance data; "
            "DOL seals, logos and third-party material excluded"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.dol.gov/general/aboutdol/copyright",
        "attribution": (
            "U.S. Department of Labor, Employment and Training Administration, "
            "Office of Unemployment Insurance"
        ),
        "required_notice": (
            "Source: U.S. Department of Labor, Employment and Training Administration. "
            "Seasonally adjusted weekly unemployment-insurance claims; the latest week is "
            "an advance estimate and may be revised. Continued claims are continued weeks "
            "claimed, not a count of unique recipients. Atlas Macro is not endorsed by DOL; "
            "DOL seals and logos are not used."
        ),
    },
    "bea": {
        "name": "U.S. Bureau of Economic Analysis Data API",
        "homepage": "https://apps.bea.gov/api/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Attributed official BEA data; non-endorsement notice required",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://apps.bea.gov/API/_pdf/bea_api_tos.pdf",
        "attribution": "U.S. Bureau of Economic Analysis",
        "required_notice": (
            "This product uses the Bureau of Economic Analysis (BEA) Data API but is not "
            "endorsed or certified by BEA."
        ),
    },
    "bea-release": {
        "name": "U.S. Bureau of Economic Analysis GDP Releases",
        "homepage": "https://www.bea.gov/data/gdp/gross-domestic-product",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed U.S. government GDP release tables and workbooks; "
            "BEA logos, seals and third-party material excluded"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.bea.gov/about/policies-and-information/data-dissemination",
        "attribution": "U.S. Bureau of Economic Analysis",
        "required_notice": (
            "Source: U.S. Bureau of Economic Analysis GDP release workbooks. "
            "Estimate and revision labels are retained; Atlas Macro is not affiliated with BEA."
        ),
    },
    "bea-pio-release": {
        "name": "U.S. Bureau of Economic Analysis Personal Income and Outlays Releases",
        "homepage": "https://www.bea.gov/data/income-saving/personal-income",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed U.S. government Personal Income and Outlays release data; "
            "BEA logos, seals and third-party material excluded"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.bea.gov/about/policies-and-information/data-dissemination",
        "attribution": "U.S. Bureau of Economic Analysis",
        "required_notice": (
            "Source: U.S. Bureau of Economic Analysis Personal Income and Outlays "
            "release workbooks. Atlas Macro is not affiliated with BEA."
        ),
    },
    "census": {
        "name": "U.S. Census Bureau Data API",
        "homepage": "https://www.census.gov/data/developers.html",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Official Census API data under CC0 catalogue metadata",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.census.gov/data/developers/about/terms-of-service.html",
        "attribution": "U.S. Census Bureau",
        "required_notice": (
            "This product uses the Census Bureau Data API but is not endorsed or certified "
            "by the Census Bureau."
        ),
    },
    "census-release": {
        "name": "U.S. Census Bureau Monthly Retail Trade Releases",
        "homepage": "https://www2.census.gov/retail/releases/historical/marts/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed U.S. government retail release workbooks; estimate and revision "
            "labels retained"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.census.gov/about/policies/open-gov/open-data.html",
        "attribution": "U.S. Census Bureau",
        "required_notice": (
            "Source: U.S. Census Bureau Monthly Retail Trade release workbooks. "
            "Advance, preliminary and revised estimates remain explicitly labelled."
        ),
    },
    "cftc": {
        "name": "CFTC Public Reporting Environment",
        "homepage": "https://publicreporting.cftc.gov/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Attributed CFTC-authored public-domain COT reporting data",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.cftc.gov/WebPolicy/index.htm",
        "attribution": "U.S. Commodity Futures Trading Commission",
        "required_notice": (
            "Source: U.S. Commodity Futures Trading Commission, Commitments of Traders. "
            "CFTC government information is public domain with acknowledgement requested; "
            "CFTC does not endorse Atlas Macro or guarantee this republication."
        ),
    },
    "federal-reserve": {
        "name": "Federal Reserve Board",
        "homepage": "https://www.federalreserve.gov/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed Board-authored public-domain releases, document metadata and DDP data; "
            "Federal Reserve seals and third-party material excluded"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.federalreserve.gov/disclaimer.htm",
        "attribution": "Board of Governors of the Federal Reserve System",
    },
    "federal-reserve-g19": {
        "name": "Federal Reserve Consumer Credit G.19",
        "homepage": "https://www.federalreserve.gov/releases/g19/current/",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed Board-authored public-domain G.19 DDP data; "
            "Federal Reserve seals and third-party material excluded"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.federalreserve.gov/disclaimer.htm",
        "attribution": "Board of Governors of the Federal Reserve System",
        "required_notice": (
            "Source: Board of Governors of the Federal Reserve System, Consumer Credit G.19. "
            "Atlas Macro is not affiliated with or endorsed by the Federal Reserve Board."
        ),
    },
    "ny-fed-household-credit": {
        "name": "New York Fed Household Debt and Credit",
        "homepage": "https://www.newyorkfed.org/microeconomics/hhdc",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Attributed Household Debt and Credit data under the New York Fed Terms of Use; "
            "source attribution to New York Fed Consumer Credit Panel / Equifax required"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.newyorkfed.org/privacy/termsofuse",
        "attribution": "New York Fed Consumer Credit Panel / Equifax",
        "required_notice": (
            f"© {date.today().year} Federal Reserve Bank of New York. Content from the New "
            "York Fed subject to the Terms of Use at newyorkfed.org. Source: New York Fed "
            "Consumer Credit Panel / Equifax. Atlas Macro is responsible for its analysis and "
            "is not affiliated with or endorsed by the New York Fed."
        ),
    },
    "federal-reserve-sloos": {
        "name": "Federal Reserve Senior Loan Officer Opinion Survey",
        "homepage": "https://www.federalreserve.gov/data/sloos.htm",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Attributed Board-authored public-domain DDP survey data",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.federalreserve.gov/disclaimer.htm",
        "attribution": "Board of Governors of the Federal Reserve System",
    },
    "us-treasury-hqm": {
        "name": "U.S. Treasury HQM Corporate Bond Yield Curve",
        "homepage": (
            "https://home.treasury.gov/data/treasury-coupon-issues-and-corporate-bond-"
            "yield-curve/corporate-bond-yield-curve"
        ),
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Attributed U.S. government HQM curve data; not an OAS or CDS quote",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": (
            "https://www.govinfo.gov/content/pkg/USCODE-2024-title17/html/"
            "USCODE-2024-title17-chap1-sec105.htm"
        ),
        "attribution": "U.S. Department of the Treasury",
    },
    "chicago-fed-nfci": {
        "name": "Chicago Fed National Financial Conditions Index",
        "homepage": "https://www.chicagofed.org/research/data/nfci/current-data",
        "kind": "official",
        "license_status": Source.LicenseStatus.REVIEW,
        "license_scope": "Internal licence review only; commercial republication not confirmed",
        "redistribution_allowed": False,
        "public_display_allowed": False,
        "derived_display_allowed": False,
        "historical_storage_allowed": False,
        "ai_use_allowed": False,
        "terms_url": "https://www.chicagofed.org/utilities/legal-notices",
        "attribution": "Federal Reserve Bank of Chicago",
    },
    "sec": {
        "name": "U.S. Securities and Exchange Commission EDGAR",
        "homepage": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Public filings and SEC-authored website metadata with attribution; issuer and "
            "third-party material retains its own rights"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://www.sec.gov/os/accessing-edgar-data",
        "attribution": "U.S. Securities and Exchange Commission",
        "required_notice": "Source: U.S. Securities and Exchange Commission EDGAR. Atlas Macro is not affiliated with, sponsored by, or endorsed by the SEC; SEC data is shown with the original filing period and retrieval time.",
    },
    "us-treasury-news": {
        "name": "U.S. Treasury Official Press Releases",
        "homepage": "https://home.treasury.gov/news/press-releases",
        "kind": "official",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "U.S. government release metadata and canonical links only",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "terms_url": "https://home.treasury.gov/footer/privacy-act/privacy-policy",
        "attribution": "U.S. Department of the Treasury",
    },
    "berkshire-hathaway": {
        "name": "Berkshire Hathaway Shareholder Letters",
        "homepage": "https://www.berkshirehathaway.com/letters/letters.html",
        "kind": "first-party-content-index",
        "license_status": Source.LicenseStatus.REVIEW,
        "license_scope": (
            "First-party year, title and outbound-link metadata only; no letter text, "
            "PDF, excerpt or image is copied or hosted"
        ),
        "redistribution_allowed": False,
        "public_display_allowed": True,
        "derived_display_allowed": False,
        "historical_storage_allowed": True,
        "ai_use_allowed": False,
        "terms_url": "https://www.berkshirehathaway.com/letters/letters.html",
        "attribution": "Berkshire Hathaway Inc.",
        "required_notice": (
            "Atlas Macro stores link metadata only. Shareholder letters remain on and are "
            "controlled by Berkshire Hathaway; no document text or PDF is republished here."
        ),
    },
    "github": {
        "name": "GitHub REST API",
        "homepage": "https://docs.github.com/en/rest",
        "kind": "public-api",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": (
            "Low-throughput, attributed REST API repository metadata for a research display; "
            "no resale, spam, personal-data sale or repository-content republication"
        ),
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": False,
        "terms_url": "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
        "attribution": "GitHub",
        "required_notice": (
            "Repository metadata supplied by the GitHub REST API under GitHub's Terms of "
            "Service; descriptions and repository content remain subject to their owners' licences."
        ),
    },
    "okx": {
        "name": "OKX Public Market Data",
        "homepage": "https://www.okx.com/docs-v5/",
        "kind": "public-api",
        "license_status": Source.LicenseStatus.REVIEW,
        "license_scope": "Internal testing only unless OKX grants written public-display permission",
        "redistribution_allowed": False,
        "public_display_allowed": False,
        "derived_display_allowed": False,
        "historical_storage_allowed": False,
        "ai_use_allowed": False,
        "terms_url": "https://www.okx.com/en-us/help/okx-api-agreement",
        "attribution": "OKX",
    },
    "deribit": {
        "name": "Deribit Public API",
        "homepage": "https://docs.deribit.com/",
        "kind": "public-api",
        "license_status": Source.LicenseStatus.REVIEW,
        "license_scope": "Personal/internal testing only unless Deribit grants written permission",
        "redistribution_allowed": False,
        "public_display_allowed": False,
        "derived_display_allowed": False,
        "historical_storage_allowed": False,
        "ai_use_allowed": False,
        "terms_url": "https://statics.deribit.com/files/TermsofServiceDeribit.pdf",
        "attribution": "Deribit",
    },
    "internal": {
        "name": "Atlas Macro Derived Data",
        "homepage": "",
        "kind": "derived",
        "license_status": Source.LicenseStatus.OPEN,
        "license_scope": "Original calculations derived from attributed inputs",
        "redistribution_allowed": True,
        "public_display_allowed": True,
        "derived_display_allowed": True,
        "historical_storage_allowed": True,
        "ai_use_allowed": True,
        "attribution": "Atlas Macro",
    },
}


def ensure_source(key: str, **overrides: Any) -> Source:
    """Get or create a catalogued data source without replacing admin edits."""

    defaults = {**SOURCE_CATALOG.get(key, {"name": key.replace("-", " ").title()}), **overrides}
    source_field_names = {
        "name",
        "homepage",
        "kind",
        "license_status",
        "license_scope",
        "redistribution_allowed",
        "attribution",
    }
    source_defaults = {
        field: value for field, value in defaults.items() if field in source_field_names
    }
    source, created = Source.objects.get_or_create(key=key, defaults=source_defaults)
    current_decision = None if created else source.licenses.filter(is_current=True).first()
    if current_decision and (
        current_decision.status in {Source.LicenseStatus.LICENSED, Source.LicenseStatus.RESTRICTED}
        or current_decision.reviewed_at is not None
        or (
            bool(current_decision.reviewed_by)
            and current_decision.reviewed_by != "clean-room seed policy"
        )
    ):
        # A reviewed admin decision is authoritative. In particular, a later
        # ingestion must never reactivate a source whose publication rights
        # were revoked or restricted in the licence ledger.
        return source
    # Correct unsafe seed defaults while preserving a later explicit licensed contract.
    if not created and source.license_status != Source.LicenseStatus.LICENSED:
        for field in (
            "name",
            "homepage",
            "kind",
            "license_status",
            "license_scope",
            "redistribution_allowed",
            "attribution",
        ):
            if field in source_defaults:
                setattr(source, field, source_defaults[field])
        source.save()
    if (
        source.license_status == Source.LicenseStatus.LICENSED
        and source.licenses.filter(
            status=Source.LicenseStatus.LICENSED,
            is_current=True,
        ).exists()
    ):
        return source
    licence_defaults = {
        "status": defaults.get("license_status", Source.LicenseStatus.REVIEW),
        "scope": defaults.get("license_scope", "Terms review required before publication"),
        "required_notice": defaults.get("required_notice", ""),
        "terms_url": defaults.get("terms_url", defaults.get("homepage", "")),
        "redistribution_allowed": defaults.get("redistribution_allowed", False),
        "public_display_allowed": defaults.get("public_display_allowed", False),
        "derived_display_allowed": defaults.get("derived_display_allowed", False),
        "historical_storage_allowed": defaults.get("historical_storage_allowed", False),
        "ai_use_allowed": defaults.get("ai_use_allowed", False),
        "territories": defaults.get("territories", "Worldwide public web"),
    }
    matching = source.licenses.filter(**licence_defaults).order_by("-created_at").first()
    if matching is None or not matching.is_current:
        with transaction.atomic():
            Source.objects.select_for_update().get(pk=source.pk)
            source.licenses.filter(is_current=True).update(is_current=False)
            if matching is None:
                SourceLicense.objects.create(
                    source=source,
                    is_current=True,
                    **licence_defaults,
                    notes="Created automatically on first ingestion; review in Django Admin.",
                )
            else:
                matching.is_current = True
                matching.save(update_fields=["is_current", "updated_at"])
    return source


def public_display_license_q(prefix: str = "source__licenses") -> Q:
    """Return the current, effective public-display licence predicate."""

    today = timezone.localdate()
    return (
        Q(**{f"{prefix}__is_current": True})
        & Q(
            **{
                f"{prefix}__status__in": (
                    Source.LicenseStatus.OPEN,
                    Source.LicenseStatus.LICENSED,
                )
            }
        )
        & Q(**{f"{prefix}__public_display_allowed": True})
        & (
            Q(**{f"{prefix}__valid_from__isnull": True})
            | Q(**{f"{prefix}__valid_from__lte": today})
        )
        & (
            Q(**{f"{prefix}__valid_until__isnull": True})
            | Q(**{f"{prefix}__valid_until__gte": today})
        )
    )


def derived_display_license_q(prefix: str = "source__licenses") -> Q:
    """Return the current public-and-derived display licence predicate."""

    return public_display_license_q(prefix) & Q(
        **{f"{prefix}__derived_display_allowed": True}
    )


def current_display_source_key_sets(
    keys: Iterable[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Load effective public and derived source decisions in one query.

    The returned derived set is deliberately a subset of the public set: a
    source cannot authorize a public derived display while denying the public
    display that carries its attribution and provenance.
    """

    requested = {str(key) for key in keys or () if key}
    today = timezone.localdate()
    decisions = SourceLicense.objects.filter(
        is_current=True,
        status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
    ).filter(
        Q(valid_from__isnull=True) | Q(valid_from__lte=today),
        Q(valid_until__isnull=True) | Q(valid_until__gte=today),
    )
    if requested:
        decisions = decisions.filter(source__key__in=requested)
    public: set[str] = set()
    derived: set[str] = set()
    for source_key, public_allowed, derived_allowed in decisions.values_list(
        "source__key",
        "public_display_allowed",
        "derived_display_allowed",
    ):
        if not public_allowed:
            continue
        public.add(source_key)
        if derived_allowed:
            derived.add(source_key)
    return public, derived


def publicly_displayable_source_keys(keys: Iterable[str]) -> bool:
    """Return True only when every source has one current effective licence."""

    required = {str(key) for key in keys if key}
    if not required:
        return False
    allowed, _derived = current_display_source_key_sets(required)
    return allowed == required


def public_source_notices(keys: Iterable[str]) -> list[str]:
    """Return required notices for the current effective source licences."""

    required = {str(key) for key in keys if key}
    if not required:
        return []
    notices = (
        SourceLicense.objects.filter(
            source__key__in=required,
            is_current=True,
            status__in=(Source.LicenseStatus.OPEN, Source.LicenseStatus.LICENSED),
            public_display_allowed=True,
        )
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=timezone.localdate()))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=timezone.localdate()))
        .exclude(required_notice="")
        .order_by("source__key")
        .values_list("required_notice", flat=True)
    )
    return list(dict.fromkeys(notices))


def begin_ingestion(
    source: Source | str, dataset: str, *, metadata: Mapping[str, Any] | None = None
) -> IngestionRun:
    source_obj = ensure_source(source) if isinstance(source, str) else source
    return IngestionRun.objects.create(
        source=source_obj,
        dataset=dataset[:120],
        started_at=timezone.now(),
        metadata=dict(metadata or {}),
    )


def finish_ingestion(
    run: IngestionRun,
    *,
    status: str,
    row_count: int = 0,
    error: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> IngestionRun:
    run.status = status
    run.row_count = max(0, int(row_count))
    run.error = error[:8000]
    run.completed_at = timezone.now()
    if metadata:
        run.metadata = {**run.metadata, **dict(metadata)}
    run.save(
        update_fields=["status", "row_count", "error", "completed_at", "metadata", "updated_at"]
    )
    check_status = {
        IngestionRun.Status.SUCCESS: QualityCheck.Status.PASS,
        IngestionRun.Status.PARTIAL: QualityCheck.Status.WARN,
        IngestionRun.Status.FAILED: QualityCheck.Status.FAIL,
    }.get(status, QualityCheck.Status.WARN)
    QualityCheck.objects.update_or_create(
        run=run,
        batch_id=run.batch_id,
        scope_key=f"{run.source.key}:{run.dataset}"[:160],
        check_name="provider_result",
        defaults={
            "status": check_status,
            "observed_at": run.completed_at,
            "details": {"row_count": run.row_count, "error": run.error},
        },
    )
    return run


def record_provider_result(
    result: ProviderResult,
    *,
    source_key: str | None = None,
    persist: Callable[[ProviderResult, Source, IngestionRun], int] | None = None,
) -> IngestionRun:
    """Persist a provider outcome and optionally normalize its records atomically."""

    key = source_key or result.provider
    run = begin_ingestion(
        key,
        result.dataset,
        metadata={"provider": result.provider, "fetched_at": result.fetched_at.isoformat()},
    )
    if result.skipped:
        return finish_ingestion(
            run,
            status=IngestionRun.Status.PARTIAL,
            metadata={**result.metadata, "skipped": True},
        )
    if result.error:
        return finish_ingestion(run, status=IngestionRun.Status.FAILED, error=result.error)

    try:
        with transaction.atomic():
            row_count = persist(result, run.source, run) if persist else result.row_count
    except Exception as exc:
        return finish_ingestion(
            run,
            status=IngestionRun.Status.FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )
    partial_quality = bool(result.metadata.get("missing_series")) or (
        result.metadata.get("quality_status") == "partial"
    )
    allow_empty_success = bool(
        result.provider == "treasury-fiscal-data"
        and result.dataset == "treasury-securities-auctions"
        and result.metadata.get("coverage_complete")
        and result.metadata.get("allow_empty_success")
    )
    status = (
        IngestionRun.Status.PARTIAL
        if (row_count == 0 and not allow_empty_success) or partial_quality
        else IngestionRun.Status.SUCCESS
    )
    metadata = dict(result.metadata)
    if row_count == 0 and not allow_empty_success:
        metadata.setdefault("quality_reason", "provider returned no persistable rows")
    return finish_ingestion(
        run,
        status=status,
        row_count=row_count,
        metadata=metadata,
    )


def _aware_midnight(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        dt = parse_datetime(value) or datetime.combine(parse_date(value) or date.min, time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, UTC)
    return dt


SERIES_CATALOG = {
    "SOFR": ("Secured Overnight Financing Rate", "%", "daily"),
    "EFFR": ("Effective Federal Funds Rate", "%", "daily"),
    "IORB": ("Interest Rate on Reserve Balances", "%", "daily"),
    "UST-BILL-13W-COUPON-EQUIVALENT": (
        "U.S. Treasury 13-week Bill Coupon Equivalent",
        "%",
        "daily",
    ),
    "H10-BROAD-DOLLAR": (
        "Federal Reserve H.10 Nominal Broad Dollar Index",
        "index",
        "daily",
    ),
    "H10-EURUSD": ("Federal Reserve H.10 U.S. Dollars per Euro", "USD/EUR", "daily"),
    "H10-USDCNY": ("Federal Reserve H.10 Chinese Yuan per U.S. Dollar", "CNY/USD", "daily"),
    "H10-USDJPY": ("Federal Reserve H.10 Japanese Yen per U.S. Dollar", "JPY/USD", "daily"),
    "TGA": ("Treasury General Account Closing Balance", "USD millions", "daily"),
    "CES0000000001": ("Total Nonfarm Payroll Employment", "thousands", "monthly"),
    "LNS14000000": ("Unemployment Rate", "%", "monthly"),
    "LNS11300000": ("Labor Force Participation Rate", "%", "monthly"),
    "CES0500000003": ("Average Hourly Earnings, Total Private", "USD/hour", "monthly"),
    "JTS000000000000000JOL": ("Job Openings", "thousands", "monthly"),
    "JTS000000000000000JOR": ("Job Openings Rate", "%", "monthly"),
    "JTS000000000000000HIL": ("Hires", "thousands", "monthly"),
    "JTS000000000000000HIR": ("Hires Rate", "%", "monthly"),
    "JTS000000000000000QUL": ("Quits", "thousands", "monthly"),
    "JTS000000000000000QUR": ("Quits Rate", "%", "monthly"),
    "JTS000000000000000LDL": ("Layoffs and Discharges", "thousands", "monthly"),
    "JTS000000000000000LDR": ("Layoffs and Discharges Rate", "%", "monthly"),
    "DOL-UI-INITIAL-CLAIMS-SA": (
        "Seasonally Adjusted Initial UI Claims",
        "claims",
        "weekly",
    ),
    "DOL-UI-INITIAL-CLAIMS-SA-4WK": (
        "Seasonally Adjusted Initial UI Claims, 4-Week Average",
        "claims",
        "weekly",
    ),
    "DOL-UI-CONTINUED-CLAIMS-SA": (
        "Seasonally Adjusted Continued Weeks Claimed",
        "claims",
        "weekly",
    ),
    "DOL-UI-CONTINUED-CLAIMS-SA-4WK": (
        "Seasonally Adjusted Continued Weeks Claimed, 4-Week Average",
        "claims",
        "weekly",
    ),
    "DOL-UI-IUR-SA": (
        "Seasonally Adjusted Insured Unemployment Rate",
        "%",
        "weekly",
    ),
    "CUSR0000SA0": ("Consumer Price Index for All Urban Consumers", "index", "monthly"),
    "CUUR0000SA0": (
        "Consumer Price Index for All Urban Consumers, Not Seasonally Adjusted",
        "index",
        "monthly",
    ),
    "CUSR0000SA0L1E": ("Core CPI, All Items Less Food and Energy", "index", "monthly"),
    "CUUR0000SA0L1E": (
        "Core CPI, All Items Less Food and Energy, Not Seasonally Adjusted",
        "index",
        "monthly",
    ),
    "CUSR0000SAH1": ("CPI Shelter", "index", "monthly"),
    "CUUR0000SAH1": (
        "CPI Shelter, Not Seasonally Adjusted",
        "index",
        "monthly",
    ),
    "CUSR0000SACL1E": (
        "CPI Commodities Less Food and Energy Commodities",
        "index",
        "monthly",
    ),
    "CUUR0000SACL1E": (
        "CPI Commodities Less Food and Energy Commodities, Not Seasonally Adjusted",
        "index",
        "monthly",
    ),
    "CUSR0000SASLE": (
        "CPI Services Less Energy Services",
        "index",
        "monthly",
    ),
    "CUUR0000SASLE": (
        "CPI Services Less Energy Services, Not Seasonally Adjusted",
        "index",
        "monthly",
    ),
    "WPSFD4": ("Producer Price Index: Final Demand", "index", "monthly"),
    "WPUFD4": (
        "Producer Price Index: Final Demand, Not Seasonally Adjusted",
        "index",
        "monthly",
    ),
    "BEA-A191RL": ("Real GDP Growth, SAAR", "%", "quarterly"),
    "BEA-DPCERL": ("Real Personal Consumption Expenditures Growth, SAAR", "%", "quarterly"),
    "BEA-GDP-NOMINAL-SAAR": ("Nominal GDP, SAAR", "USD billions", "quarterly"),
    "BEA-GDI-NOMINAL-SAAR": ("Nominal GDI, SAAR", "USD billions", "quarterly"),
    "BEA-GDI-REAL-GROWTH-SAAR": ("Real GDI Growth, SAAR", "%", "quarterly"),
    "BEA-PCE-GOODS-GROWTH": ("Real PCE Goods Growth, SAAR", "%", "quarterly"),
    "BEA-PCE-SERVICES-GROWTH": ("Real PCE Services Growth, SAAR", "%", "quarterly"),
    "BEA-GPDI-GROWTH": ("Real Gross Private Domestic Investment Growth, SAAR", "%", "quarterly"),
    "BEA-FIXED-INVESTMENT-GROWTH": ("Real Fixed Investment Growth, SAAR", "%", "quarterly"),
    "BEA-EXPORTS-GROWTH": ("Real Exports Growth, SAAR", "%", "quarterly"),
    "BEA-IMPORTS-GROWTH": ("Real Imports Growth, SAAR", "%", "quarterly"),
    "BEA-GOVERNMENT-GROWTH": ("Real Government Spending and Investment Growth, SAAR", "%", "quarterly"),
    "BEA-PCE-CONTRIBUTION": (
        "PCE Contribution to Real GDP Growth",
        "percentage points",
        "quarterly",
    ),
    "BEA-GPDI-CONTRIBUTION": (
        "Gross Private Domestic Investment Contribution to Real GDP Growth",
        "percentage points",
        "quarterly",
    ),
    "BEA-NET-EXPORTS-CONTRIBUTION": (
        "Net Exports Contribution to Real GDP Growth",
        "percentage points",
        "quarterly",
    ),
    "BEA-GOVERNMENT-CONTRIBUTION": (
        "Government Contribution to Real GDP Growth",
        "percentage points",
        "quarterly",
    ),
    "BEA-REAL-PCE-MOM": (
        "Real Personal Consumption Expenditures, Month-over-Month",
        "%",
        "monthly",
    ),
    "BEA-REAL-DPI-MOM": (
        "Real Disposable Personal Income, Month-over-Month",
        "%",
        "monthly",
    ),
    "BEA-PERSONAL-SAVING-RATE": (
        "Personal Saving Rate",
        "%",
        "monthly",
    ),
    "BEA-DPI-NOMINAL-SAAR": (
        "Disposable Personal Income, SAAR",
        "USD millions",
        "monthly",
    ),
    "BEA-DPI-REAL-SAAR": (
        "Real Disposable Personal Income, SAAR",
        "millions of chained dollars",
        "monthly",
    ),
    "BEA-DPI-NOMINAL-MOM": (
        "Disposable Personal Income, Month-over-Month",
        "%",
        "monthly",
    ),
    "BEA-REAL-PCE-SAAR": (
        "Real Personal Consumption Expenditures, SAAR",
        "millions of chained dollars",
        "monthly",
    ),
    "CENSUS-MRTS-44X72-SM-SA": (
        "Retail Trade and Food Services Sales, Seasonally Adjusted",
        "USD millions",
        "monthly",
    ),
    "CENSUS-MRTS-44X72-SM-SA-MOM": (
        "Retail Trade and Food Services Sales, Month-over-Month",
        "%",
        "monthly",
    ),
    "CENSUS-MRTS-44X72-SM-SA-YOY": (
        "Retail Trade and Food Services Sales, Year-over-Year",
        "%",
        "monthly",
    ),
    "G19-CONSUMER-CREDIT-GROWTH-SAAR": (
        "Total Consumer Credit Growth, SAAR",
        "% annual rate",
        "monthly",
    ),
    "G19-REVOLVING-CREDIT-GROWTH-SAAR": (
        "Revolving Consumer Credit Growth, SAAR",
        "% annual rate",
        "monthly",
    ),
    "G19-NONREVOLVING-CREDIT-GROWTH-SAAR": (
        "Nonrevolving Consumer Credit Growth, SAAR",
        "% annual rate",
        "monthly",
    ),
    "G19-CONSUMER-CREDIT-OUTSTANDING-SA": (
        "Total Consumer Credit Outstanding, Seasonally Adjusted",
        "USD millions",
        "monthly",
    ),
    "G19-REVOLVING-CREDIT-OUTSTANDING-SA": (
        "Revolving Consumer Credit Outstanding, Seasonally Adjusted",
        "USD millions",
        "monthly",
    ),
    "G19-NONREVOLVING-CREDIT-OUTSTANDING-SA": (
        "Nonrevolving Consumer Credit Outstanding, Seasonally Adjusted",
        "USD millions",
        "monthly",
    ),
    "G19-CONSUMER-CREDIT-FLOW-SA": (
        "Total Consumer Credit Monthly Flow, Seasonally Adjusted",
        "USD millions per month",
        "monthly",
    ),
    "G19-REVOLVING-CREDIT-FLOW-SA": (
        "Revolving Consumer Credit Monthly Flow, Seasonally Adjusted",
        "USD millions per month",
        "monthly",
    ),
    "G19-NONREVOLVING-CREDIT-FLOW-SA": (
        "Nonrevolving Consumer Credit Monthly Flow, Seasonally Adjusted",
        "USD millions per month",
        "monthly",
    ),
    "HHDC-MORTGAGE-BALANCE": ("Household Mortgage Balance", "USD trillions", "quarterly"),
    "HHDC-HELOC-BALANCE": ("Household HELOC Balance", "USD trillions", "quarterly"),
    "HHDC-AUTO-LOAN-BALANCE": ("Household Auto Loan Balance", "USD trillions", "quarterly"),
    "HHDC-CREDIT-CARD-BALANCE": (
        "Household Credit Card Balance",
        "USD trillions",
        "quarterly",
    ),
    "HHDC-STUDENT-LOAN-BALANCE": (
        "Household Student Loan Balance",
        "USD trillions",
        "quarterly",
    ),
    "HHDC-OTHER-BALANCE": ("Other Household Debt Balance", "USD trillions", "quarterly"),
    "HHDC-TOTAL-DEBT-BALANCE": ("Total Household Debt Balance", "USD trillions", "quarterly"),
    "HHDC-MORTGAGE-90D-DELINQUENT": (
        "Mortgage Balance 90+ Days Delinquent",
        "%",
        "quarterly",
    ),
    "HHDC-HELOC-90D-DELINQUENT": ("HELOC Balance 90+ Days Delinquent", "%", "quarterly"),
    "HHDC-AUTO-90D-DELINQUENT": (
        "Auto Loan Balance 90+ Days Delinquent",
        "%",
        "quarterly",
    ),
    "HHDC-CREDIT-CARD-90D-DELINQUENT": (
        "Credit Card Balance 90+ Days Delinquent",
        "%",
        "quarterly",
    ),
    "HHDC-STUDENT-LOAN-90D-DELINQUENT": (
        "Student Loan Balance 90+ Days Delinquent",
        "%",
        "quarterly",
    ),
    "HHDC-OTHER-90D-DELINQUENT": ("Other Balance 90+ Days Delinquent", "%", "quarterly"),
    "HHDC-ALL-90D-DELINQUENT": ("All Debt Balance 90+ Days Delinquent", "%", "quarterly"),
    "SUBLPDMBS_XWB_N.Q": (
        "Business-loan Lending Standards",
        "net percentage",
        "quarterly",
    ),
    "SUBLPDMBD_XWB_N.Q": (
        "Business-loan Demand",
        "net percentage",
        "quarterly",
    ),
    "SUBLPDMHS_XWB_N.Q": (
        "Household-loan Lending Standards",
        "net percentage",
        "quarterly",
    ),
    "SUBLPDMHD_XWB_N.Q": (
        "Household-loan Demand",
        "net percentage",
        "quarterly",
    ),
    "SUBLPDCILS_N.Q": (
        "C&I Lending Standards, Large and Middle-market Firms",
        "net percentage",
        "quarterly",
    ),
    "SUBLPDCISS_N.Q": (
        "C&I Lending Standards, Small Firms",
        "net percentage",
        "quarterly",
    ),
    "HQM-PAR-2Y": ("Treasury HQM 2-year Par Yield", "%", "monthly"),
    "HQM-PAR-5Y": ("Treasury HQM 5-year Par Yield", "%", "monthly"),
    "HQM-PAR-10Y": ("Treasury HQM 10-year Par Yield", "%", "monthly"),
    "HQM-PAR-30Y": ("Treasury HQM 30-year Par Yield", "%", "monthly"),
    "ONRRP": ("Overnight Reverse Repo Accepted Amount", "USD millions", "daily"),
    "ONRRP-RATE": ("Overnight Reverse Repo Offering Rate", "%", "daily"),
    "ONRRP-PARTICIPANTS": ("Overnight Reverse Repo Counterparties", "count", "daily"),
    "SRP": ("Standing Repo Accepted Amount", "USD millions", "daily"),
    "SRP-TREASURY": ("Standing Repo Treasury Collateral", "USD millions", "daily"),
    "SRP-AGENCY": ("Standing Repo Agency Collateral", "USD millions", "daily"),
    "SRP-MBS": ("Standing Repo MBS Collateral", "USD millions", "daily"),
    "SRP-RATE": ("Standing Repo Offering Rate", "%", "daily"),
    "SOMA-TOTAL": ("SOMA Domestic Securities Total", "USD millions", "weekly"),
    "SOMA-BILLS": ("SOMA Treasury Bills", "USD millions", "weekly"),
    "SOMA-NOTES-BONDS": ("SOMA Treasury Notes and Bonds", "USD millions", "weekly"),
    "SOMA-TIPS": ("SOMA TIPS", "USD millions", "weekly"),
    "SOMA-FRN": ("SOMA Floating Rate Notes", "USD millions", "weekly"),
    "SOMA-TIPS-INFLATION-COMPENSATION": (
        "SOMA TIPS Inflation Compensation",
        "USD millions",
        "weekly",
    ),
    "SOMA-MBS": ("SOMA Agency MBS", "USD millions", "weekly"),
    "SOMA-CMBS": ("SOMA Agency CMBS", "USD millions", "weekly"),
    "SOMA-AGENCIES": ("SOMA Agency Debt", "USD millions", "weekly"),
    "FXSWAP-USD-DRAWDOWN": ("USD Liquidity Swap Drawdowns", "USD millions", "daily"),
    "FXSWAP-USD-OUTSTANDING": (
        "USD Liquidity Swaps Outstanding",
        "USD millions",
        "daily",
    ),
    "FXSWAP-USD-OUTSTANDING-SMALL-VALUE": (
        "USD Liquidity Swap Small Value Exercises",
        "USD millions",
        "daily",
    ),
    "WALCL": ("Federal Reserve Total Assets", "USD millions", "weekly"),
    "WSHOTSL": ("Federal Reserve Treasury Securities Held Outright", "USD millions", "weekly"),
    "WSHOMCB": ("Federal Reserve Mortgage-Backed Securities", "USD millions", "weekly"),
    "WRBWFRBL": ("Reserve Balances with Federal Reserve Banks", "USD millions", "weekly"),
    "WDTGAL": ("Treasury General Account at Federal Reserve Banks", "USD millions", "weekly"),
    "SWPT": ("Central Bank Liquidity Swaps on H.4.1", "USD millions", "weekly"),
}


def store_series_observations(result: ProviderResult, source: Source, run: IngestionRun) -> int:
    """Upsert normalized observations from any official time-series provider."""

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in result.records:
        if record.get("series_id") and record.get("date") and record.get("value") is not None:
            grouped[str(record["series_id"])].append(record)
    fetched_at = result.fetched_at
    if timezone.is_naive(fetched_at):
        fetched_at = timezone.make_aware(fetched_at, UTC)
    count = 0
    for series_id, records in grouped.items():
        name, unit, frequency = SERIES_CATALOG.get(
            series_id,
            (
                series_id.replace("UST-", "U.S. Treasury ").replace("TIPS-", "Treasury Real "),
                "%" if series_id.startswith(("UST-", "TIPS-")) else "",
                "daily",
            ),
        )
        series, _ = SeriesDefinition.objects.get_or_create(
            key=series_id.lower(),
            defaults={
                "name": name,
                "unit": unit,
                "source": source,
                "frequency": frequency,
                "description": f"Imported directly from {source.name}.",
            },
        )
        for record in records:
            value_date = _aware_midnight(record["date"])
            metadata = dict(record.get("metadata") or {})
            for key in ("realtime_start", "realtime_end"):
                if record.get(key) is not None:
                    metadata[key] = record[key]
            Observation.objects.update_or_create(
                series=series,
                instrument=None,
                value_date=value_date,
                source=source,
                defaults={
                    "value": record["value"],
                    "as_of": value_date,
                    "fetched_at": fetched_at,
                    "batch_id": run.batch_id,
                    "quality_status": (
                        record.get("quality_status")
                        if record.get("quality_status")
                        in Observation.Quality.values
                        else Observation.Quality.FRESH
                    ),
                    "metadata": metadata,
                },
            )
            count += 1
    return count


def store_release_vintage_observations(
    result: ProviderResult,
    source: Source,
    run: IngestionRun,
    *,
    record_group: str = "release_vintages",
) -> int:
    """Upsert release-vintage rows without collapsing values by economic period."""

    records = result.supplemental_records.get(record_group, [])
    if not records:
        return 0
    fetched_at = result.fetched_at
    if timezone.is_naive(fetched_at):
        fetched_at = timezone.make_aware(fetched_at, UTC)
    count = 0
    for record in records:
        series_id = str(record.get("series_id") or "")
        release_date = parse_date(str(record.get("release_date") or ""))
        estimate_round = str(record.get("estimate_round") or "").strip()
        vintage_label = str(record.get("vintage_label") or estimate_round).strip()
        if not all(
            (
                series_id,
                record.get("date"),
                record.get("value") is not None,
                release_date,
                estimate_round,
                vintage_label,
            )
        ):
            raise ValueError("release vintage record is missing an identity or value field")
        name, unit, frequency = SERIES_CATALOG.get(
            series_id,
            (series_id, "", "quarterly"),
        )
        series, _ = SeriesDefinition.objects.get_or_create(
            key=series_id.lower(),
            defaults={
                "name": name,
                "unit": unit,
                "source": source,
                "frequency": frequency,
                "description": f"Imported directly from {source.name}.",
            },
        )
        value_date = _aware_midnight(record["date"])
        as_of = _aware_midnight(release_date)
        ReleaseVintageObservation.objects.update_or_create(
            series=series,
            value_date=value_date,
            release_date=release_date,
            estimate_round=estimate_round,
            source=source,
            defaults={
                "value": record["value"],
                "as_of": as_of,
                "vintage_label": vintage_label,
                "fetched_at": fetched_at,
                "batch_id": run.batch_id,
                "fallback_source": None,
                "quality_status": Observation.Quality.FRESH,
                "license_scope": source.license_scope[:240],
                "metadata": dict(record.get("metadata") or {}),
            },
        )
        count += 1
    return count


def store_fred_observations(result: ProviderResult, source: Source, run: IngestionRun) -> int:
    """Backward-compatible FRED normalizer."""

    return store_series_observations(result, source, run)


def store_market_observation(
    *,
    symbol: str,
    name: str,
    asset_class: str,
    value: Decimal | float | str,
    value_date: datetime,
    source: Source,
    run: IngestionRun,
    metadata: Mapping[str, Any] | None = None,
) -> Observation:
    instrument, _ = Instrument.objects.get_or_create(
        symbol=symbol,
        defaults={"name": name, "asset_class": asset_class},
    )
    if timezone.is_naive(value_date):
        value_date = timezone.make_aware(value_date, UTC)
    observation, _ = Observation.objects.update_or_create(
        instrument=instrument,
        series=None,
        value_date=value_date,
        source=source,
        defaults={
            "value": value,
            "as_of": value_date,
            "fetched_at": timezone.now(),
            "batch_id": run.batch_id,
            "quality_status": Observation.Quality.FRESH,
            "metadata": dict(metadata or {}),
        },
    )
    return observation


def store_okx_ticker(result: ProviderResult, source: Source, run: IngestionRun) -> int:
    count = 0
    for record in result.records:
        last = record.get("last")
        symbol = record.get("instId")
        if not symbol or _safe_decimal(last) is None:
            continue
        timestamp_ms = int(record.get("ts") or 0)
        value_date = (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
            if timestamp_ms
            else result.fetched_at
        )
        store_market_observation(
            symbol=symbol,
            name=symbol.replace("-", " / "),
            asset_class="crypto",
            value=last,
            value_date=value_date,
            source=source,
            run=run,
            metadata={"bid": record.get("bidPx"), "ask": record.get("askPx")},
        )
        count += 1
    return count


def _safe_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:
        return None


def store_github_repository(result: ProviderResult, source: Source, run: IngestionRun) -> int:
    count = 0
    for record in result.records:
        pushed_at = parse_datetime(record.get("pushed_at") or "")
        project, _ = GitHubProject.objects.update_or_create(
            repo=record["repo"],
            defaults={
                "category": record.get("category") or (record.get("topics") or ["AI 应用"])[0],
                "description": record.get("description", ""),
                "stars": record.get("stars", 0),
                "forks": record.get("forks", 0),
                "open_issues": record.get("open_issues", 0),
                "pushed_at": pushed_at,
                "homepage": record.get("homepage") or f"https://github.com/{record['repo']}",
                "source": source,
                "data_as_of": result.fetched_at,
                "quality_status": Observation.Quality.FRESH,
                "archived": bool(record.get("archived", False)),
                "is_fork": bool(record.get("is_fork", False)),
                "license_spdx": record.get("license", "") or "",
            },
        )
        snapshot_date = result.fetched_at.date()
        GitHubProjectSnapshot.objects.update_or_create(
            project=project,
            snapshot_date=snapshot_date,
            defaults={
                "stars": record.get("stars", 0),
                "forks": record.get("forks", 0),
                "open_issues": record.get("open_issues", 0),
                "pushed_at": pushed_at,
                "fetched_at": result.fetched_at,
                "batch_id": run.batch_id,
                "source": source,
            },
        )
        baseline = (
            project.snapshots.filter(snapshot_date__lte=snapshot_date - timedelta(days=7))
            .order_by("-snapshot_date")
            .first()
        )
        stars_7d = max(0, project.stars - baseline.stars) if baseline else 0
        project.stars_7d = stars_7d
        project.momentum_score = Decimal(stars_7d)
        project.save(update_fields=["stars_7d", "momentum_score", "updated_at"])
        count += 1
    return count


def store_cftc_positions(result: ProviderResult, source: Source, run: IngestionRun) -> int:
    fetched_at = result.fetched_at
    if timezone.is_naive(fetched_at):
        fetched_at = timezone.make_aware(fetched_at, UTC)
    count = 0
    chunk_size = 5_000
    for offset in range(0, len(result.records), chunk_size):
        objects = []
        for record in result.records[offset : offset + chunk_size]:
            published_at = parse_datetime(record.get("published_at") or "")
            source_updated_at = parse_datetime(record.get("source_updated_at") or "")
            if published_at and timezone.is_naive(published_at):
                published_at = timezone.make_aware(published_at, UTC)
            if source_updated_at and timezone.is_naive(source_updated_at):
                source_updated_at = timezone.make_aware(source_updated_at, UTC)
            objects.append(
                CFTCPosition(
                    report_type=record["report_type"],
                    report_date=parse_date(record["report_date"]),
                    published_at=published_at,
                    source_updated_at=source_updated_at,
                    market_code=record["market_code"],
                    market_name=record["market_name"],
                    trader_group=record["trader_group"],
                    long_positions=record["long_positions"],
                    short_positions=record["short_positions"],
                    open_interest=record.get("open_interest"),
                    fetched_at=fetched_at,
                    batch_id=run.batch_id,
                    source=source,
                    quality_status=(
                        Observation.Quality.FRESH if published_at else Observation.Quality.ERROR
                    ),
                )
            )
        CFTCPosition.objects.bulk_create(
            objects,
            batch_size=1_000,
            update_conflicts=True,
            update_fields=[
                "published_at",
                "source_updated_at",
                "market_name",
                "long_positions",
                "short_positions",
                "open_interest",
                "fetched_at",
                "batch_id",
                "source",
                "quality_status",
                "updated_at",
            ],
            unique_fields=["report_type", "report_date", "market_code", "trader_group"],
        )
        count += len(objects)
    return count


def store_fed_documents(result: ProviderResult, _: Source, __: IngestionRun) -> int:
    count = 0
    for record in result.records:
        published_at = parse_datetime(record.get("published_at") or "")
        if not published_at:
            continue
        create_defaults = {
            "document_type": record["document_type"],
            "title": record["title"],
            "speaker": "",
            "official_description": record.get("official_description", ""),
            "summary": "",
            "key_points": [],
            "published_at": published_at,
            "hawkish_score": None,
            "original_url": record["original_url"],
            "analysis_status": FedDocument.AnalysisStatus.DRAFT,
        }
        document, created = FedDocument.objects.get_or_create(
            slug=record["slug"],
            defaults=create_defaults,
        )
        if not created:
            upstream_fields = {
                "document_type": record["document_type"],
                "title": record["title"],
                "official_description": record.get("official_description", ""),
                "published_at": published_at,
                "original_url": record["original_url"],
            }
            for field, value in upstream_fields.items():
                setattr(document, field, value)
            document.save(update_fields=[*upstream_fields, "updated_at"])
        count += 1
    return count


def _validated_auction_window_contract(
    result: ProviderResult,
) -> tuple[date, date, date, date, set[tuple[str, date]], datetime]:
    if (
        result.provider != "treasury-fiscal-data"
        or result.dataset != "treasury-securities-auctions"
    ):
        raise ValueError("auction persistence received the wrong provider or dataset")
    metadata = dict(result.metadata or {})
    if metadata.get("coverage_complete") is not True:
        raise ValueError("auction result is not complete")
    if metadata.get("timezone") != "America/New_York":
        raise ValueError("auction result has invalid timezone semantics")
    raw_as_of = str(metadata.get("as_of_date_et") or "")
    as_of_date = parse_date(raw_as_of)
    if as_of_date is None or raw_as_of != as_of_date.isoformat():
        raise ValueError("auction result has invalid ET as_of_date")
    fetched_at = result.fetched_at
    if timezone.is_naive(fetched_at):
        fetched_at = timezone.make_aware(fetched_at, UTC)
    if fetched_at > timezone.now() + timedelta(minutes=5):
        raise ValueError("auction result fetched_at is in the future")
    if (
        fetched_at.astimezone(ZoneInfo("America/New_York")).date()
        != as_of_date
    ):
        raise ValueError("auction result fetched_at does not match its ET as_of_date")
    expected = {
        "auction_window": {
            "date_field": "auction_date",
            "lower": as_of_date - timedelta(days=90),
            "upper": as_of_date + timedelta(days=14),
        },
        "issue_window": {
            "date_field": "issue_date",
            "lower": as_of_date,
            "upper": as_of_date + timedelta(days=14),
        },
    }
    raw_slices = metadata.get("slices")
    if not isinstance(raw_slices, list) or len(raw_slices) != 2:
        raise ValueError("complete auction result must contain exactly two slices")
    if not all(isinstance(item, Mapping) for item in raw_slices):
        raise ValueError("auction slice metadata must be mappings")
    slices = {str(item.get("name") or ""): item for item in raw_slices}
    if len(slices) != 2 or set(slices) != set(expected):
        raise ValueError("auction result has missing or duplicate bounded slices")
    for name, contract in expected.items():
        state = slices[name]
        if state.get("coverage_complete") is not True:
            raise ValueError(f"{name} is not complete")
        if state.get("date_field") != contract["date_field"]:
            raise ValueError(f"{name} has the wrong date field")
        if state.get("lower") != contract["lower"].isoformat() or state.get(
            "upper_exclusive"
        ) != contract["upper"].isoformat():
            raise ValueError(f"{name} has unsafe or unexpected bounds")
        counts = {
            key: state.get(key)
            for key in (
                "returned_count",
                "normalized_count",
                "rejected_count",
                "total_count",
                "count",
                "total_pages",
                "page_size",
            )
        }
        if not all(type(value) is int for value in counts.values()):
            raise ValueError(f"{name} lacks complete integer pagination metadata")
        if any(value < 0 for value in counts.values()):
            raise ValueError(f"{name} contains negative pagination metadata")
        if counts["rejected_count"] != 0 or not (
            counts["returned_count"]
            == counts["normalized_count"]
            == counts["total_count"]
            == counts["count"]
        ):
            raise ValueError(f"{name} row counts do not reconcile")
        if counts["page_size"] <= 0 or counts["total_count"] > counts["page_size"]:
            raise ValueError(f"{name} page size cannot prove full coverage")
        if counts["total_count"] == 0:
            if counts["total_pages"] not in {0, 1}:
                raise ValueError(f"{name} has invalid empty pagination")
        elif counts["total_pages"] != 1:
            raise ValueError(f"{name} spans more than one fetched page")

    merged_count = metadata.get("merged_record_count")
    deduplicated_count = metadata.get("deduplicated_record_count")
    if type(merged_count) is not int or type(deduplicated_count) is not int:
        raise ValueError("auction result lacks merged/deduplicated row counts")
    returned_total = sum(
        int(slices[name]["returned_count"]) for name in expected
    )
    if (
        merged_count != len(result.records)
        or deduplicated_count != returned_total - merged_count
        or merged_count < 0
        or deduplicated_count < 0
    ):
        raise ValueError("auction result merged/deduplicated counts do not reconcile")

    auction_lower = expected["auction_window"]["lower"]
    auction_upper = expected["auction_window"]["upper"]
    issue_lower = expected["issue_window"]["lower"]
    issue_upper = expected["issue_window"]["upper"]
    incoming_identities: set[tuple[str, date]] = set()
    for record in result.records:
        cusip = str(record.get("cusip") or "")
        auction_date = parse_date(str(record.get("auction_date") or ""))
        if not cusip or auction_date is None:
            raise ValueError("auction record has an invalid identity")
        identity = (cusip, auction_date)
        if identity in incoming_identities:
            raise ValueError("auction result contains a duplicate identity")
        incoming_identities.add(identity)
        raw_issue_date = record.get("issue_date")
        issue_date = parse_date(str(raw_issue_date or ""))
        if raw_issue_date not in (None, "") and issue_date is None:
            raise ValueError("auction record has an invalid issue_date")
        if not (
            auction_lower <= auction_date < auction_upper
            or issue_date is not None and issue_lower <= issue_date < issue_upper
        ):
            raise ValueError("auction record is outside both proven bounded slices")
    return (
        auction_lower,
        auction_upper,
        issue_lower,
        issue_upper,
        incoming_identities,
        fetched_at,
    )


def store_treasury_auctions(result: ProviderResult, source: Source, run: IngestionRun) -> int:
    (
        auction_lower,
        auction_upper,
        issue_lower,
        issue_upper,
        incoming_identities,
        fetched_at,
    ) = _validated_auction_window_contract(result)
    if source.key != "treasury-fiscal-data" or run.dataset != result.dataset:
        raise ValueError("auction persistence source/run identity mismatch")
    count = 0
    for record in result.records:
        auction_date = parse_date(record["auction_date"])
        if auction_date is None:
            raise ValueError("auction record has an invalid auction_date")
        TreasuryAuction.objects.update_or_create(
            cusip=record["cusip"],
            auction_date=auction_date,
            defaults={
                "security_type": record["security_type"],
                "security_term": record["security_term"],
                "announcement_date": parse_date(record.get("announcement_date") or ""),
                "issue_date": parse_date(record.get("issue_date") or ""),
                "maturity_date": parse_date(record.get("maturity_date") or ""),
                "offering_amount": record.get("offering_amt"),
                "total_tendered": record.get("total_tendered"),
                "total_accepted": record.get("total_accepted"),
                "bid_to_cover_ratio": record.get("bid_to_cover_ratio"),
                "high_yield": record.get("high_yield"),
                "indirect_bidder_accepted": record.get("indirect_bidder_accepted"),
                "direct_bidder_accepted": record.get("direct_bidder_accepted"),
                "primary_dealer_accepted": record.get("primary_dealer_accepted"),
                "fetched_at": fetched_at,
                "batch_id": run.batch_id,
                "source": source,
                "quality_status": Observation.Quality.FRESH,
            },
        )
        count += 1

    covered = TreasuryAuction.objects.filter(source=source).filter(
        Q(
            auction_date__gte=auction_lower,
            auction_date__lt=auction_upper,
        )
        | Q(issue_date__gte=issue_lower, issue_date__lt=issue_upper)
    )
    stale_ids = [
        item.pk
        for item in covered.only("pk", "cusip", "auction_date")
        if (item.cusip, item.auction_date) not in incoming_identities
    ]
    if stale_ids:
        TreasuryAuction.objects.filter(pk__in=stale_ids).delete()
    return count


def store_raw_artifact(
    run: IngestionRun,
    *,
    uri: str,
    content: bytes,
    content_type: str = "application/json",
) -> RawArtifact:
    """Record the immutable metadata for an externally stored raw response."""

    return RawArtifact.objects.create(
        run=run,
        uri=uri,
        sha256=hashlib.sha256(content).hexdigest(),
        content_type=content_type,
        size_bytes=len(content),
    )


def latest_observation(
    *, series_key: str | None = None, instrument_symbol: str | None = None
) -> Observation | None:
    if bool(series_key) == bool(instrument_symbol):
        raise ValueError("provide exactly one of series_key or instrument_symbol")
    queryset = Observation.objects.select_related("source", "series", "instrument")
    if series_key:
        queryset = queryset.filter(series__key=series_key)
    else:
        queryset = queryset.filter(instrument__symbol=instrument_symbol)
    return queryset.order_by("-value_date").first()


def serialize_run(run: IngestionRun) -> dict[str, Any]:
    """Return a stable Celery-result representation."""

    return {
        "batch_id": str(run.batch_id),
        "source": run.source.key,
        "dataset": run.dataset,
        "status": run.status,
        "row_count": run.row_count,
        "error": run.error,
        "metadata": json.loads(json.dumps(run.metadata, default=str)),
    }


def summarize_runs(runs: Iterable[IngestionRun]) -> dict[str, Any]:
    rows = [serialize_run(run) for run in runs]
    return {
        "runs": rows,
        "row_count": sum(row["row_count"] for row in rows),
        "failed": sum(row["status"] == IngestionRun.Status.FAILED for row in rows),
        "partial": sum(row["status"] == IngestionRun.Status.PARTIAL for row in rows),
    }
