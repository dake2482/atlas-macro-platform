from __future__ import annotations

import hashlib
import io
import json
import warnings
import zipfile
from copy import deepcopy
from datetime import UTC, datetime

import httpx
import pytest
from django.conf import settings

from research.fed_h10 import (
    H10_DATA_MEMBER,
    H10_SOURCE_ATTRIBUTES,
    H10_TARGET_SERIES,
    FederalReserveH10Provider,
)
from research.models import (
    DashboardSnapshot,
    MetricSnapshot,
    Observation,
    RawArtifact,
    SourceLicense,
)
from research.official_data import (
    GLOBAL_DOLLAR_CONTRACT_VERSION,
    GLOBAL_DOLLAR_REQUIRED_CHART_KEYS,
    GLOBAL_DOLLAR_REQUIRED_METRIC_KEYS,
    GLOBAL_DOLLAR_REQUIRED_SECTION_KEYS,
    INDEPENDENT_PUBLICATION_KEYS,
    _coordinate_global_dollar_dashboard,
    _dashboard_content_fingerprint,
    _global_dollar_content_fingerprint,
    _global_dollar_page_data,
    _global_dollar_payload_integrity_hash,
    _store_global_dollar_swap_observations,
    _store_h10_observations,
    global_dollar_snapshot_is_publicly_displayable,
    publish_official_dashboards,
    select_public_global_dollar_snapshot,
)
from research.providers import NYFedMarketsProvider, ProviderResult
from research.services import record_provider_result

H10_DATES = (
    "2026-07-02",
    "2026-07-03",
    "2026-07-06",
    "2026-07-07",
    "2026-07-08",
    "2026-07-09",
    "2026-07-10",
)
SWAP_FIXTURE_FETCHED_AT = datetime(2026, 7, 14, 18, 0, tzinfo=UTC)


def _h10_archive(
    *,
    duplicate_member: bool = False,
    duplicate_series: bool = False,
    duplicate_prepared: bool = False,
    member_name: str = H10_DATA_MEMBER,
    extra_nested_member: bool = False,
    dates: tuple[str, ...] = H10_DATES,
    prepared_at: str = "2026-07-13T17:45:44",
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    series_blocks = []
    for series_index, (board_id, target) in enumerate(H10_TARGET_SERIES.items()):
        attrs = H10_SOURCE_ATTRIBUTES[board_id]
        observations = "".join(
            f'<frb:Obs OBS_STATUS="A" OBS_VALUE="{100 + series_index + index / 10}" '
            f'TIME_PERIOD="{period}" />'
            for index, period in enumerate(dates)
        )
        block = f"""
        <kf:Series SERIES_NAME="{board_id}" FREQ="{attrs['FREQ']}"
            CURRENCY="{attrs['CURRENCY']}" FX="{attrs['FX']}"
            UNIT="{attrs['UNIT']}" UNIT_MULT="{attrs['UNIT_MULT']}">
          <frb:Annotations><common:Annotation>
            <common:AnnotationText>{target['name']}</common:AnnotationText>
          </common:Annotation></frb:Annotations>
          {observations}
        </kf:Series>
        """
        series_blocks.append(block)
        if duplicate_series and series_index == 0:
            series_blocks.append(block)
    second_prepared = (
        "<message:Prepared>2026-07-14T17:45:44</message:Prepared>"
        if duplicate_prepared
        else ""
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <message:MessageGroup
      xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
      xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
      xmlns:frb="http://www.federalreserve.gov/structure/compact/common"
      xmlns:kf="http://www.federalreserve.gov/structure/compact/H10_H10">
      <message:Header><message:Prepared>{prepared_at}</message:Prepared>{second_prepared}</message:Header>
      <frb:DataSet>{''.join(series_blocks)}</frb:DataSet>
    </message:MessageGroup>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr(member_name, xml)
        if duplicate_member:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(member_name, xml)
        if extra_nested_member:
            archive.writestr(f"nested/{H10_DATA_MEMBER}", xml)
    return buffer.getvalue()


def _h10_result(*, provider_kwargs=None, raw_bytes=None, **archive_kwargs) -> ProviderResult:
    raw = bytes(raw_bytes) if raw_bytes is not None else _h10_archive(**archive_kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/datadownload/Output.aspx"
        return httpx.Response(200, content=raw)

    client = httpx.Client(
        base_url="https://www.federalreserve.example.test",
        transport=httpx.MockTransport(handler),
    )
    return FederalReserveH10Provider(
        client=client,
        **dict(provider_kwargs or {}),
    ).h10()


def _encrypted_h10_archive() -> bytes:
    payload = bytearray(_h10_archive())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        cursor = 0
        while (position := payload.find(signature, cursor)) >= 0:
            offset = position + flag_offset
            flags = int.from_bytes(payload[offset : offset + 2], "little") | 0x1
            payload[offset : offset + 2] = flags.to_bytes(2, "little")
            cursor = offset + 2
    return bytes(payload)


def _recent_swap_operations() -> list[dict[str, object]]:
    return [
        {
            "operationId": "ECB-REGULAR",
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "European Central Bank",
            "currency": "USD",
            "tradeDate": "2026-07-09",
            "settlementDate": "2026-07-10",
            "maturityDate": "2026-07-17",
            "termInDays": 7,
            "amount": 128000000,
            "interestRate": 3.88,
            "isSmallValue": "",
            "lastUpdated": "2026-07-13T12:00:00Z",
        },
        {
            "operationId": "SNB-TECHNICAL",
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "Swiss National Bank",
            "currency": "USD",
            "tradeDate": "2026-07-09",
            "settlementDate": "2026-07-10",
            "maturityDate": "2026-07-17",
            "termInDays": 7,
            "amount": 50000,
            "interestRate": 3.88,
            "isSmallValue": "Y",
            "lastUpdated": "2026-07-13T12:00:00Z",
        },
        {
            "operationId": "ECB-EXPIRED",
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "European Central Bank",
            "currency": "USD",
            "tradeDate": "2026-07-01",
            "settlementDate": "2026-07-02",
            "maturityDate": "2026-07-09",
            "termInDays": 7,
            "amount": 170000000,
            "interestRate": 3.88,
            "isSmallValue": "",
            "lastUpdated": "2026-07-13T12:00:00Z",
        },
    ]


def _swap_operations() -> list[dict[str, object]]:
    witnesses = [
        {
            "operationId": "HISTORY-BOJ-20100518",
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "Bank of Japan",
            "currency": "USD",
            "tradeDate": "2010-05-18",
            "settlementDate": "2010-05-20",
            "maturityDate": "2010-08-12",
            "termInDays": 84,
            "amount": 210000000,
            "interestRate": 1.24,
            "isSmallValue": "",
            "lastUpdated": "2010-05-18T15:00:00",
        },
        {
            "operationId": "HISTORY-ECB-20100518",
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "European Central Bank",
            "currency": "USD",
            "tradeDate": "2010-05-18",
            "settlementDate": "2010-05-20",
            "maturityDate": "2010-08-12",
            "termInDays": 84,
            "amount": 1032000000,
            "interestRate": 1.24,
            "isSmallValue": "",
            "lastUpdated": "2010-05-18T15:00:00",
        },
    ]
    fillers = [
        {
            "operationId": f"HISTORY-FILLER-{index:04d}",
            "operationType": "U.S. Dollar Liquidity Swap",
            "counterparty": "Bank of England",
            "currency": "USD",
            "tradeDate": "2011-01-03",
            "settlementDate": "2011-01-04",
            "maturityDate": "2011-01-11",
            "termInDays": 7,
            "amount": 1000000,
            "interestRate": 1.1,
            "isSmallValue": "",
            "lastUpdated": "2011-01-03T15:00:00",
        }
        for index in range(1535)
    ]
    return witnesses + fillers + _recent_swap_operations()


def _swap_result(*, operations=None) -> ProviderResult:
    source_rows = _swap_operations() if operations is None else operations
    raw = json.dumps(
        {"fxSwaps": {"operations": source_rows}}, separators=(",", ":")
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/fxs/usdollar/search.json"
        assert dict(request.url.params) == {
            "startDate": "2007-01-01",
            "endDate": "2026-07-14",
            "dateType": "trade",
        }
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
        )

    client = httpx.Client(
        base_url="https://markets.newyorkfed.org",
        transport=httpx.MockTransport(handler),
    )
    result = NYFedMarketsProvider(client=client).usd_fx_swaps(
        start_date="2007-01-01",
        end_date="2026-07-14",
        date_type="trade",
    )
    result.fetched_at = SWAP_FIXTURE_FETCHED_AT
    return result


@pytest.fixture
def published_global_dollar(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    h10_result = _h10_result()
    swap_result = _swap_result()
    assert h10_result.ok and swap_result.ok
    h10_run = record_provider_result(h10_result, persist=_store_h10_observations)
    swap_run = record_provider_result(
        swap_result, persist=_store_global_dollar_swap_observations
    )
    dashboards, stale = _coordinate_global_dollar_dashboard([h10_run, swap_run])
    assert not stale, (
        h10_run.status,
        h10_run.error,
        swap_run.status,
        swap_run.error,
        _global_dollar_page_data({"h10": h10_run, "swaps": swap_run})[1],
    )
    snapshot = dashboards[0] if dashboards else DashboardSnapshot.objects.get(
        key="global-dollar"
    )
    return snapshot, h10_run, swap_run, tmp_path


def test_search_provider_binds_explicit_coverage_and_rejects_bad_rows():
    result = _swap_result()
    assert result.ok
    assert result.metadata["coverage_complete"] is True
    assert result.metadata["coverage_start"] == "2007-01-01"
    assert result.metadata["coverage_end"] == "2026-07-14"
    assert result.metadata["date_type"] == "trade"
    assert result.metadata["returned_count"] == 1540
    assert result.raw_bytes

    bad_rows = _swap_operations()
    bad_rows[-1] = {**bad_rows[-1], "settlementDate": "2026-07-18"}
    failed = _swap_result(operations=bad_rows)
    assert not failed.ok
    assert "trade date exceeds settlement" not in failed.error
    assert "settlement must precede maturity" in failed.error


def test_strict_swap_identity_and_naive_new_york_update_clock_fail_closed():
    duplicate_id = _swap_operations()
    duplicate_id.append({**duplicate_id[0], "amount": 129000000})
    failed_id = _swap_result(operations=duplicate_id)
    assert not failed_id.ok
    assert "duplicate or conflicting business identity" in failed_id.error

    no_id = {key: value for key, value in _swap_operations()[0].items() if key != "operationId"}
    conflicting_fallback = [no_id, {**no_id, "interestRate": 4.01}]
    failed_fallback = _swap_result(operations=conflicting_fallback)
    assert not failed_fallback.ok
    assert "duplicate or conflicting business identity" in failed_fallback.error

    future_update = deepcopy(_swap_operations()[0])
    future_update["lastUpdated"] = "2026-07-14T00:30:00"
    with pytest.raises(ValueError, match="future-dated"):
        NYFedMarketsProvider.validate_usd_fx_swap_operations(
            {"fxSwaps": {"operations": [future_update]}},
            strict=True,
            fetched_at=datetime(2026, 7, 14, 3, 0, tzinfo=UTC),
        )


@pytest.mark.django_db
def test_last_endpoint_cannot_forge_search_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    raw = json.dumps(
        {"fxSwaps": {"operations": _recent_swap_operations()}}, separators=(",", ":")
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/fxs/usdollar/last/3.json"
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    result = NYFedMarketsProvider(
        client=httpx.Client(
            base_url="https://markets.newyorkfed.org",
            transport=httpx.MockTransport(handler),
        )
    ).usd_fx_swaps(limit=3, as_of="2026-07-14")
    result.fetched_at = SWAP_FIXTURE_FETCHED_AT
    result.metadata.update(
        {
            "endpoint": (
                "https://markets.newyorkfed.org/api/fxs/usdollar/search.json?"
                "startDate=2007-01-01&endDate=2026-07-14&dateType=trade"
            ),
            "coverage_mode": "explicit-search",
            "coverage_complete": True,
            "coverage_start": "2007-01-01",
            "coverage_end": "2026-07-14",
            "outstanding_as_of": "2026-07-14",
            "date_type": "trade",
            "returned_count": 3,
        }
    )
    run = record_provider_result(
        result,
        persist=_store_global_dollar_swap_observations,
    )
    assert run.status == "failed"
    assert "reviewed row floor" in run.error
    assert not RawArtifact.objects.filter(run=run).exists()


@pytest.mark.django_db
def test_search_response_missing_one_reviewed_row_cannot_persist(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    result = _swap_result(operations=_swap_operations()[:-1])
    assert result.ok
    assert result.metadata["returned_count"] == 1539
    run = record_provider_result(
        result,
        persist=_store_global_dollar_swap_observations,
    )
    assert run.status == "failed"
    assert "reviewed row floor" in run.error
    assert not RawArtifact.objects.filter(run=run).exists()


@pytest.mark.django_db
def test_reviewed_row_count_without_recent_trade_witness_cannot_persist(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    old_history = deepcopy(_swap_operations())
    for index, operation in enumerate(old_history[-3:]):
        old_history[-3 + index] = {
            **operation,
            "tradeDate": "2012-01-03",
            "settlementDate": "2012-01-04",
            "maturityDate": "2012-01-11",
            "termInDays": 7,
            "lastUpdated": "2012-01-03T15:00:00",
        }
    result = _swap_result(operations=old_history)
    assert result.ok
    assert result.metadata["returned_count"] == 1540
    run = record_provider_result(
        result,
        persist=_store_global_dollar_swap_observations,
    )
    assert run.status == "failed"
    assert "recent-trade witness is stale" in run.error
    assert not RawArtifact.objects.filter(run=run).exists()


def test_h10_rejects_duplicate_member_series_prepared_and_nested_only():
    duplicate_member = _h10_result(duplicate_member=True)
    duplicate_series = _h10_result(duplicate_series=True)
    duplicate_prepared = _h10_result(duplicate_prepared=True)
    nested_only = _h10_result(member_name=f"nested/{H10_DATA_MEMBER}")
    extra_nested = _h10_result(extra_nested_member=True)
    assert not duplicate_member.ok
    assert "requires exactly one H10_data.xml" in duplicate_member.error
    assert not duplicate_series.ok
    assert "duplicate H.10 series block" in duplicate_series.error
    assert not duplicate_prepared.ok
    assert "exactly one Prepared timestamp" in duplicate_prepared.error
    assert not nested_only.ok
    assert "requires H10_data.xml at the ZIP root" in nested_only.error
    assert not extra_nested.ok
    assert "requires exactly one H10_data.xml" in extra_nested.error


def test_h10_enforces_compressed_and_expanded_size_limits():
    compressed = _h10_result(provider_kwargs={"max_archive_bytes": 1})
    expanded = _h10_result(provider_kwargs={"max_xml_bytes": 100})
    assert not compressed.ok
    assert "compressed-size limit" in compressed.error
    assert not expanded.ok
    assert "expanded-size limit" in expanded.error


def test_h10_encrypted_or_unsupported_compression_fails_as_provider_result():
    encrypted = _h10_result(raw_bytes=_encrypted_h10_archive())
    unsupported = _h10_result(compression=zipfile.ZIP_BZIP2)
    assert not encrypted.ok
    assert "must not be encrypted" in encrypted.error
    assert not unsupported.ok
    assert "unsupported compression" in unsupported.error


@pytest.mark.django_db
def test_h10_upgrade_uses_legacy_prepared_and_observation_watermarks(
    published_global_dollar,
):
    _snapshot, old_h10_run, _swap_run, _root = published_global_dollar
    old_metadata = dict(old_h10_run.metadata)
    old_metadata.pop("source_prepared_at", None)
    old_metadata.pop("latest_valid_dates", None)
    old_metadata["prepared_at"] = "2026-07-13T17:45:44"
    old_h10_run.metadata = old_metadata
    old_h10_run.save(update_fields=["metadata", "updated_at"])

    prepared_regression = record_provider_result(
        _h10_result(prepared_at="2026-07-12T17:45:44"),
        persist=_store_h10_observations,
    )
    assert prepared_regression.status == "failed"
    assert "Prepared timestamp regressed" in prepared_regression.error

    observation_regression = record_provider_result(
        _h10_result(dates=H10_DATES[:-1]),
        persist=_store_h10_observations,
    )
    assert observation_regression.status == "failed"
    assert "latest valid observation regressed" in observation_regression.error


@pytest.mark.django_db
def test_global_dollar_v1_publishes_exact_9_3_3_contract(published_global_dollar):
    snapshot, h10_run, swap_run, _root = published_global_dollar
    data = snapshot.data
    assert data["contract_version"] == GLOBAL_DOLLAR_CONTRACT_VERSION
    assert {item["key"] for item in data["metrics"]} == set(
        GLOBAL_DOLLAR_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in data["charts"]} == set(
        GLOBAL_DOLLAR_REQUIRED_CHART_KEYS
    )
    assert {item["key"] for item in data["sections"]} == set(
        GLOBAL_DOLLAR_REQUIRED_SECTION_KEYS
    )
    metrics = {item["key"]: item for item in data["metrics"]}
    assert metrics["fxswap-usd-outstanding"]["value"] == 128.05
    assert metrics["fxswap-usd-outstanding-non-small-value"]["value"] == 128.0
    assert metrics["fxswap-usd-outstanding-small-value"]["display_value"] == "0.05"
    assert metrics["fxswap-active-counterparties"]["value"] == 1.0
    assert set(data["component_batches"]) == {
        str(h10_run.batch_id),
        str(swap_run.batch_id),
    }
    assert len(data["acquisition_artifacts"]) == 2
    assert MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).count() == 9
    assert RawArtifact.objects.filter(run__in=[h10_run, swap_run]).count() == 2
    assert global_dollar_snapshot_is_publicly_displayable(snapshot)
    assert select_public_global_dollar_snapshot([snapshot]) == snapshot


@pytest.mark.django_db
def test_generic_publisher_cannot_write_global_dollar(published_global_dollar):
    snapshot, _h10_run, _swap_run, _root = published_global_dollar
    assert "global-dollar" in INDEPENDENT_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"global-dollar"}) == []
    assert DashboardSnapshot.objects.filter(key="global-dollar").count() == 1
    assert select_public_global_dollar_snapshot([snapshot]) == snapshot


def test_legacy_non_global_dashboard_fingerprint_semantics_are_unchanged():
    snapshot_data = {
        "demo": False,
        "title": "legacy-payload-title",
        "custom": {"value": 7},
    }
    legacy_payload = {
        "title": "Legacy Dashboard Title",
        "summary": "Legacy dashboard summary",
        **snapshot_data,
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    assert _dashboard_content_fingerprint(
        title="Legacy Dashboard Title",
        summary="Legacy dashboard summary",
        snapshot_data=snapshot_data,
    ) == expected


@pytest.mark.django_db
def test_selector_fails_closed_when_required_license_is_revoked(
    published_global_dollar,
):
    snapshot, _h10_run, _swap_run, _root = published_global_dollar
    licence = SourceLicense.objects.get(
        source__key="ny-fed-markets",
        is_current=True,
    )
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)
    assert select_public_global_dollar_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_superseded_component_is_stale_until_new_lineage_is_published(
    published_global_dollar,
):
    snapshot, old_h10_run, _swap_run, _root = published_global_dollar
    replacement_h10 = record_provider_result(
        _h10_result(),
        persist=_store_h10_observations,
    )
    assert replacement_h10.status == "success"
    assert replacement_h10.pk != old_h10_run.pk

    transition = select_public_global_dollar_snapshot([snapshot])
    assert transition == snapshot
    assert transition.quality_status == Observation.Quality.STALE

    dashboards, stale = _coordinate_global_dollar_dashboard([replacement_h10])
    assert stale == set()
    assert len(dashboards) == 1
    replacement_snapshot = dashboards[0]
    component_runs = {
        item["component"]: item
        for item in replacement_snapshot.data["component_runs"]
    }
    assert component_runs["h10"]["ingestion_run_id"] == replacement_h10.pk
    assert select_public_global_dollar_snapshot([replacement_snapshot]) == (
        replacement_snapshot
    )


@pytest.mark.django_db
def test_selector_rejects_title_summary_shadow_and_snapshot_tampering(
    published_global_dollar,
):
    snapshot, _h10_run, _swap_run, _root = published_global_dollar
    original_data = deepcopy(snapshot.data)
    original_title = snapshot.title
    original_summary = snapshot.summary
    normalized_rows = list(
        MetricSnapshot.objects.filter(batch_id=snapshot.batch_id)
    )
    original_metadata = {
        row.pk: deepcopy(row.metadata) for row in normalized_rows
    }

    def publish_tampered_state(*, title, summary, data):
        data["fingerprint"] = _global_dollar_content_fingerprint(
            title=title,
            summary=summary,
            snapshot_data=data,
        )
        data["payload_integrity_hash"] = _global_dollar_payload_integrity_hash(
            title=title,
            summary=summary,
            snapshot_data=data,
        )
        snapshot.title = title
        snapshot.summary = summary
        snapshot.data = data
        snapshot.save(update_fields=["title", "summary", "data", "updated_at"])
        for row in normalized_rows:
            metadata = deepcopy(original_metadata[row.pk])
            metadata["publication_fingerprint"] = data["fingerprint"]
            metadata["payload_integrity_hash"] = data["payload_integrity_hash"]
            row.metadata = metadata
            row.save(update_fields=["metadata", "updated_at"])

    shadowed = deepcopy(original_data)
    shadowed["title"] = original_title
    shadowed["summary"] = original_summary
    publish_tampered_state(
        title=original_title,
        summary=original_summary,
        data=shadowed,
    )
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)

    forged_title_data = deepcopy(original_data)
    publish_tampered_state(
        title="FORGED Global Dollar Trading Signal",
        summary=original_summary,
        data=forged_title_data,
    )
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)

    forged_summary_data = deepcopy(original_data)
    publish_tampered_state(
        title=original_title,
        summary="FORGED basis value and trading action",
        data=forged_summary_data,
    )
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)


@pytest.mark.django_db
def test_global_dollar_route_uses_dedicated_selector_and_renders_tables(
    client,
    published_global_dollar,
):
    _snapshot, _h10_run, _swap_run, _root = published_global_dollar
    response = client.get("/liquidity/global-dollar/")
    assert response.status_code == 200
    content = response.content.decode()
    assert "not ICE DXY" in content
    assert "Active USD Liquidity Swap Operations" in content
    assert "PURCHASE_REQUIRED" in content
    assert "European Central Bank" in content


@pytest.mark.django_db
def test_selector_rejects_payload_metric_and_artifact_tampering(published_global_dollar):
    snapshot, _h10_run, swap_run, root = published_global_dollar

    original = deepcopy(snapshot.data)
    tampered = deepcopy(original)
    tampered["sections"][2]["rows"][0]["payload_integrity_hash"] = "nested-tamper"
    tampered["payload_integrity_hash"] = _global_dollar_payload_integrity_hash(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=tampered,
    )
    snapshot.data = tampered
    snapshot.save(update_fields=["data", "updated_at"])
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)

    snapshot.data = original
    snapshot.save(update_fields=["data", "updated_at"])
    normalized = MetricSnapshot.objects.get(
        batch_id=snapshot.batch_id,
        key="global-dollar-fxswap-usd-outstanding-small-value",
    )
    normalized.display_value = "0"
    normalized.save(update_fields=["display_value", "updated_at"])
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)

    normalized.display_value = "0.05"
    normalized.save(update_fields=["display_value", "updated_at"])
    artifact = RawArtifact.objects.get(run=swap_run)
    path = root / artifact.sha256[:2] / f"{artifact.sha256}.bin"
    path.write_bytes(b"tampered")
    assert not global_dollar_snapshot_is_publicly_displayable(snapshot)


@pytest.mark.django_db
def test_selector_requires_exact_normalized_metric_metadata(
    published_global_dollar,
):
    snapshot, _h10_run, _swap_run, _root = published_global_dollar
    normalized = MetricSnapshot.objects.get(
        batch_id=snapshot.batch_id,
        key="global-dollar-fxswap-usd-outstanding",
    )
    original = deepcopy(normalized.metadata)
    mutations = []

    wrong_source_keys = deepcopy(original)
    wrong_source_keys["source_keys"] = ["demo-market"]
    mutations.append(wrong_source_keys)

    wrong_component_date = deepcopy(original)
    wrong_component_date["swap_as_of"] = "1900-01-01"
    mutations.append(wrong_component_date)

    extra_metadata = deepcopy(original)
    extra_metadata["unreviewed_extra"] = "accepted-before-exact-contract"
    mutations.append(extra_metadata)

    missing_metadata = deepcopy(original)
    missing_metadata.pop("public_snapshot")
    mutations.append(missing_metadata)

    for tampered in mutations:
        normalized.metadata = tampered
        normalized.save(update_fields=["metadata", "updated_at"])
        assert not global_dollar_snapshot_is_publicly_displayable(snapshot)

    normalized.metadata = original
    normalized.save(update_fields=["metadata", "updated_at"])
    assert global_dollar_snapshot_is_publicly_displayable(snapshot)


@pytest.mark.django_db
def test_latest_failure_retains_only_audited_stale_snapshot(published_global_dollar):
    snapshot, _h10_run, _swap_run, _root = published_global_dollar
    failed = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets", "fx-swaps:usdollar", "upstream timeout"
        ),
        persist=_store_global_dollar_swap_observations,
    )
    dashboards, stale = _coordinate_global_dollar_dashboard([failed])
    assert dashboards == []
    assert stale == {"global-dollar"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "stale"
    assert snapshot.data["refresh_failure"]["components"][1]["status"] == "failed"
    selected = select_public_global_dollar_snapshot([snapshot])
    assert selected == snapshot
    assert selected.quality_status == "stale"


@pytest.mark.django_db
def test_stale_failure_advances_and_rejects_future_or_forged_attempt_state(
    published_global_dollar,
):
    snapshot, _h10_run, _swap_run, _root = published_global_dollar
    first_failure = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets", "fx-swaps:usdollar", "first upstream timeout"
        ),
        persist=_store_global_dollar_swap_observations,
    )
    _dashboards, stale = _coordinate_global_dollar_dashboard([first_failure])
    assert stale == {"global-dollar"}
    snapshot.refresh_from_db()
    first_audit = deepcopy(snapshot.data["refresh_failure"])
    assert first_audit["components"][1]["ingestion_run_id"] == first_failure.pk

    second_failure = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets", "fx-swaps:usdollar", "second schema drift"
        ),
        persist=_store_global_dollar_swap_observations,
    )
    _dashboards, stale = _coordinate_global_dollar_dashboard([second_failure])
    assert stale == {"global-dollar"}
    snapshot.refresh_from_db()
    current_audit = deepcopy(snapshot.data["refresh_failure"])
    assert current_audit != first_audit
    assert current_audit["reason_code"] == "latest-attempt-incomplete"
    assert current_audit["components"][1]["ingestion_run_id"] == second_failure.pk
    assert current_audit["components"][1]["batch_id"] == str(
        second_failure.batch_id
    )
    assert current_audit["components"][1]["status"] == "failed"
    assert select_public_global_dollar_snapshot([snapshot]) == snapshot

    original = deepcopy(snapshot.data)
    future = deepcopy(original)
    future["refresh_failure"]["checked_at"] = "2099-01-01T00:00:00+00:00"
    snapshot.data = future
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_global_dollar_snapshot([snapshot]) is None

    fake_success = deepcopy(original)
    fake_success["refresh_failure"]["components"][1]["status"] = "success"
    snapshot.data = fake_success
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_global_dollar_snapshot([snapshot]) is None

    fake_reason = deepcopy(original)
    fake_reason["refresh_failure"]["reason"] = "forged success claim"
    snapshot.data = fake_reason
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_global_dollar_snapshot([snapshot]) is None

    fake_batch = deepcopy(original)
    fake_batch["refresh_failure"]["components"][1]["batch_id"] = (
        "00000000-0000-0000-0000-000000000000"
    )
    snapshot.data = fake_batch
    snapshot.save(update_fields=["data", "updated_at"])
    assert select_public_global_dollar_snapshot([snapshot]) is None


@pytest.mark.django_db
def test_retained_stale_snapshot_survives_success_before_coordinator(
    published_global_dollar,
):
    snapshot, _h10_run, _old_swap_run, _root = published_global_dollar
    failed_swap = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets", "fx-swaps:usdollar", "temporary upstream timeout"
        ),
        persist=_store_global_dollar_swap_observations,
    )
    _dashboards, stale = _coordinate_global_dollar_dashboard([failed_swap])
    assert stale == {"global-dollar"}
    snapshot.refresh_from_db()
    audited_failure = deepcopy(snapshot.data["refresh_failure"])
    assert audited_failure["components"][1]["ingestion_run_id"] == failed_swap.pk

    recovered_swap = record_provider_result(
        _swap_result(),
        persist=_store_global_dollar_swap_observations,
    )
    assert recovered_swap.status == "success"
    assert recovered_swap.pk != failed_swap.pk
    snapshot.refresh_from_db()
    assert snapshot.data["refresh_failure"] == audited_failure

    selected = select_public_global_dollar_snapshot([snapshot])
    assert selected == snapshot
    assert selected.quality_status == Observation.Quality.STALE


@pytest.mark.django_db
def test_old_snapshot_survives_append_only_h10_batch_then_other_source_failure(
    published_global_dollar,
):
    snapshot, old_h10_run, _old_swap_run, _root = published_global_dollar
    replacement_h10 = record_provider_result(
        _h10_result(),
        persist=_store_h10_observations,
    )
    assert replacement_h10.status == "success"
    assert replacement_h10.batch_id != old_h10_run.batch_id
    assert Observation.objects.filter(
        source=old_h10_run.source,
        batch_id=old_h10_run.batch_id,
    ).count() == old_h10_run.row_count
    assert Observation.objects.filter(
        source=replacement_h10.source,
        batch_id=replacement_h10.batch_id,
    ).count() == replacement_h10.row_count

    failed_swap = record_provider_result(
        ProviderResult.failure(
            "ny-fed-markets", "fx-swaps:usdollar", "schema drift"
        ),
        persist=_store_global_dollar_swap_observations,
    )
    dashboards, stale = _coordinate_global_dollar_dashboard([failed_swap])
    assert dashboards == []
    assert stale == {"global-dollar"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == "stale"
    assert snapshot.data["refresh_failure"]["reason"]
    assert select_public_global_dollar_snapshot([snapshot]) == snapshot


@pytest.mark.django_db
def test_private_artifact_rows_match_exact_acquisition_hashes(
    published_global_dollar,
):
    _snapshot, h10_run, swap_run, root = published_global_dollar
    for run in (h10_run, swap_run):
        artifact = RawArtifact.objects.get(run=run)
        payload = (root / artifact.sha256[:2] / f"{artifact.sha256}.bin").read_bytes()
        assert hashlib.sha256(payload).hexdigest() == artifact.sha256
        assert len(payload) == artifact.size_bytes
        assert artifact.uri.startswith(f"private://{run.source.key}/")
