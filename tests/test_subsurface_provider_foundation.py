from __future__ import annotations

import hashlib
from decimal import Decimal

import httpx
import pytest
from django.test import override_settings
from django.utils import timezone

from research.models import IngestionRun, RawArtifact, SeriesDefinition, Source
from research.providers import NYFedMarketsProvider, ProviderResult
from research.services import persist_private_raw_artifact, store_series_observations


def _client(handler):
    return httpx.Client(
        base_url="https://markets.example.test",
        transport=httpx.MockTransport(handler),
    )


def test_sofr_retains_exact_json_response_bytes_and_fingerprint_metadata():
    raw = (
        b'{\n  "refRates": [{"effectiveDate": "2026-07-13", '
        b'"percentRate": 3.55, "percentPercentile99": 3.66, '
        b'"volumeInBillions": 2100}]\n}\n'
    )

    def handler(request):
        assert request.url.path == "/api/rates/secured/sofr/last/3.json"
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    result = NYFedMarketsProvider(client=_client(handler)).sofr(limit=3)

    assert result.ok
    assert result.raw_bytes == raw
    assert result.metadata == {
        "attribution": "Federal Reserve Bank of New York",
        "terms_url": "https://www.newyorkfed.org/privacy/termsofuse",
        "endpoint": (
            "https://markets.example.test/api/rates/secured/sofr/last/3.json"
        ),
        "content_type": "application/json; charset=utf-8",
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert result.records[0]["series_id"] == "SOFR"
    assert result.records[0]["value"] == Decimal("3.55")


def test_standing_repo_adds_explicit_non_small_value_series_without_changing_legacy():
    raw = b"""{
      "repo": {"operations": [
        {
          "operationId": "SMALL-TEST",
          "operationDate": "2026-07-13",
          "isSmallValue": "Y",
          "note": "Small-Value Technical Exercise",
          "totalAmtAccepted": 50000,
          "details": [
            {"securityType": "Treasury", "amtAccepted": 50000,
             "percentOfferingRate": 0.01}
          ]
        },
        {
          "operationId": "REGULAR-PM",
          "operationDate": "2026-07-13",
          "isSmallValue": "",
          "totalAmtAccepted": 3000000,
          "details": [
            {"securityType": "Treasury", "amtAccepted": 1000000,
             "percentOfferingRate": 3.75},
            {"securityType": "Agency", "amtAccepted": 1000000,
             "percentOfferingRate": 3.75},
            {"securityType": "Mortgage-Backed", "amtAccepted": 1000000,
             "percentOfferingRate": 3.75}
          ]
        }
      ]}
    }"""

    def handler(request):
        assert request.url.path == "/api/rp/repo/allotment/results/last/2.json"
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    result = NYFedMarketsProvider(client=_client(handler)).standing_repo_results(limit=2)
    records = {record["series_id"]: record for record in result.records}

    expected_new_series = {
        "SRP-NON-SMALL-VALUE-TOTAL",
        "SRP-SMALL-VALUE-TOTAL",
        "SRP-NON-SMALL-VALUE-TREASURY",
        "SRP-NON-SMALL-VALUE-AGENCY",
        "SRP-NON-SMALL-VALUE-MBS",
        "SRP-NON-SMALL-VALUE-RATE",
    }
    assert expected_new_series <= records.keys()
    assert result.raw_bytes == raw
    assert result.metadata["byte_length"] == len(raw)
    assert result.metadata["sha256"] == hashlib.sha256(raw).hexdigest()

    # Legacy all-operation series remain byte-for-byte compatible in meaning.
    assert records["SRP"]["value"] == Decimal("3.05")
    assert records["SRP-TREASURY"]["value"] == Decimal("1.05")
    assert records["SRP-AGENCY"]["value"] == Decimal("1")
    assert records["SRP-MBS"]["value"] == Decimal("1")
    assert records["SRP-RATE"]["value"] == Decimal("0.01")

    # The v1 inputs never allow a technical exercise into the regular signal.
    assert records["SRP-NON-SMALL-VALUE-TOTAL"]["value"] == Decimal("3")
    assert records["SRP-SMALL-VALUE-TOTAL"]["value"] == Decimal("0.05")
    assert records["SRP-NON-SMALL-VALUE-TREASURY"]["value"] == Decimal("1")
    assert records["SRP-NON-SMALL-VALUE-AGENCY"]["value"] == Decimal("1")
    assert records["SRP-NON-SMALL-VALUE-MBS"]["value"] == Decimal("1")
    assert records["SRP-NON-SMALL-VALUE-RATE"]["value"] == Decimal("3.75")
    regular_metadata = records["SRP-NON-SMALL-VALUE-TOTAL"]["metadata"]
    assert regular_metadata["small_value_excluded"] is True
    assert [item["operationId"] for item in regular_metadata["operations"]] == [
        "REGULAR-PM"
    ]
    assert regular_metadata["operations"][0]["isSmallValue"] == ""


def test_usd_swaps_add_drawdown_and_outstanding_splits_without_changing_legacy():
    raw = b"""{
      "fxSwaps": {"operations": [
        {
          "counterparty": "European Central Bank",
          "settlementDate": "2026-07-09",
          "maturityDate": "2026-07-16",
          "amount": 128000000,
          "isSmallValue": ""
        },
        {
          "counterparty": "Swiss National Bank",
          "settlementDate": "2026-07-09",
          "maturityDate": "2026-07-16",
          "amount": 50000,
          "isSmallValue": "Y"
        }
      ]}
    }"""

    def handler(request):
        assert request.url.path == "/api/fxs/usdollar/last/2.json"
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    result = NYFedMarketsProvider(client=_client(handler)).usd_fx_swaps(
        limit=2, as_of="2026-07-12"
    )
    records = {record["series_id"]: record for record in result.records}

    assert result.raw_bytes == raw
    assert result.metadata["content_type"] == "application/json"
    assert result.metadata["byte_length"] == len(raw)
    assert result.metadata["sha256"] == hashlib.sha256(raw).hexdigest()
    assert records["FXSWAP-USD-DRAWDOWN"]["value"] == Decimal("128.05")
    assert records["FXSWAP-USD-OUTSTANDING"]["value"] == Decimal("128.05")
    assert records["FXSWAP-USD-OUTSTANDING-SMALL-VALUE"]["value"] == Decimal("0.05")
    assert records["FXSWAP-USD-DRAWDOWN-NON-SMALL-VALUE"]["value"] == Decimal("128")
    assert records["FXSWAP-USD-DRAWDOWN-SMALL-VALUE"]["value"] == Decimal("0.05")
    assert records["FXSWAP-USD-OUTSTANDING-NON-SMALL-VALUE"]["value"] == Decimal(
        "128"
    )
    assert (
        records["FXSWAP-USD-DRAWDOWN-NON-SMALL-VALUE"]["metadata"][
            "small_value_excluded"
        ]
        is True
    )
    assert [
        item["counterparty"]
        for item in records["FXSWAP-USD-OUTSTANDING-SMALL-VALUE"]["metadata"][
            "active_operations"
        ]
    ] == ["Swiss National Bank"]


def _run(key: str) -> IngestionRun:
    source = Source.objects.create(key=key, name=key, license_status="open")
    return IngestionRun.objects.create(
        source=source,
        dataset="raw-json-fixture",
        started_at=timezone.now(),
    )


@pytest.mark.django_db
def test_new_subsurface_series_persist_with_explicit_units_and_daily_frequency():
    expected_units = {
        "SRP-NON-SMALL-VALUE-TOTAL": "USD millions",
        "SRP-SMALL-VALUE-TOTAL": "USD millions",
        "SRP-NON-SMALL-VALUE-TREASURY": "USD millions",
        "SRP-NON-SMALL-VALUE-AGENCY": "USD millions",
        "SRP-NON-SMALL-VALUE-MBS": "USD millions",
        "SRP-NON-SMALL-VALUE-RATE": "%",
        "FXSWAP-USD-DRAWDOWN-NON-SMALL-VALUE": "USD millions",
        "FXSWAP-USD-DRAWDOWN-SMALL-VALUE": "USD millions",
        "FXSWAP-USD-OUTSTANDING-NON-SMALL-VALUE": "USD millions",
    }
    run = _run("subsurface-series-catalog")
    result = ProviderResult(
        provider="ny-fed-markets",
        dataset="subsurface-series-catalog",
        records=[
            {
                "series_id": series_id,
                "date": "2026-07-13",
                "value": Decimal("1"),
            }
            for series_id in expected_units
        ],
    )

    assert store_series_observations(result, run.source, run) == len(expected_units)
    stored = {
        definition.key.upper(): definition
        for definition in SeriesDefinition.objects.filter(
            key__in=[series_id.lower() for series_id in expected_units]
        )
    }
    assert stored.keys() == expected_units.keys()
    for series_id, unit in expected_units.items():
        assert stored[series_id].unit == unit
        assert stored[series_id].frequency == "daily"


@pytest.mark.django_db
def test_private_raw_artifact_persists_exact_content_addressed_bytes_and_row(tmp_path):
    raw = b'{"exact": "NY Fed response bytes"}\n'
    digest = hashlib.sha256(raw).hexdigest()
    result = ProviderResult(
        provider="ny-fed-markets",
        dataset="reference-rate:sofr",
        raw_bytes=raw,
        metadata={
            "endpoint": "https://markets.newyorkfed.org/example.json",
            "content_type": "application/json; charset=utf-8",
            "byte_length": len(raw),
            "sha256": digest,
        },
    )

    with override_settings(RAW_ARTIFACT_ROOT=tmp_path):
        run = _run("raw-artifact-fidelity")
        artifact = persist_private_raw_artifact(run=run, result=result)

    target = tmp_path / digest[:2] / f"{digest}.bin"
    assert target.read_bytes() == raw
    assert artifact == RawArtifact.objects.get(run=run)
    assert artifact.uri == f"private://ny-fed-markets/{digest[:2]}/{digest}.bin"
    assert artifact.sha256 == digest
    assert artifact.size_bytes == len(raw)
    assert artifact.content_type == "application/json; charset=utf-8"
    assert not list(target.parent.glob(f".{digest}.*"))


@pytest.mark.django_db
def test_private_raw_artifact_rejects_existing_content_address_collision(tmp_path):
    raw = b"expected raw response"
    digest = hashlib.sha256(raw).hexdigest()
    target = tmp_path / digest[:2] / f"{digest}.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different bytes at simulated colliding path")
    result = ProviderResult(
        provider="ny-fed-markets",
        dataset="fx-swaps:usdollar",
        raw_bytes=raw,
        metadata={"byte_length": len(raw), "sha256": digest},
    )

    with override_settings(RAW_ARTIFACT_ROOT=tmp_path):
        run = _run("raw-artifact-collision")
        with pytest.raises(ValueError, match="do not match digest"):
            persist_private_raw_artifact(run=run, result=result)

    assert target.read_bytes() == b"different bytes at simulated colliding path"
    assert not RawArtifact.objects.filter(run=run).exists()


@pytest.mark.django_db
def test_private_raw_artifact_validates_declared_length_and_hash(tmp_path):
    raw = b"exact"
    with override_settings(RAW_ARTIFACT_ROOT=tmp_path):
        run = _run("raw-artifact-declarations")
        with pytest.raises(ValueError, match="byte_length"):
            persist_private_raw_artifact(
                run=run,
                result=ProviderResult(
                    provider="ny-fed-markets",
                    dataset="bad-length",
                    raw_bytes=raw,
                    metadata={"byte_length": len(raw) + 1},
                ),
            )
        with pytest.raises(ValueError, match="sha256"):
            persist_private_raw_artifact(
                run=run,
                result=ProviderResult(
                    provider="ny-fed-markets",
                    dataset="bad-hash",
                    raw_bytes=raw,
                    metadata={"sha256": "0" * 64},
                ),
            )

    assert not RawArtifact.objects.filter(run=run).exists()
