from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.conf import settings

from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
    SourceLicense,
)
from research.official_data import (
    INDEPENDENT_PUBLICATION_KEYS,
    OPERATIONS_ALLOWED_SERIES,
    OPERATIONS_REQUIRED_CHART_KEYS,
    OPERATIONS_REQUIRED_METRIC_KEYS,
    OPERATIONS_REQUIRED_SECTION_KEYS,
    _coordinate_operations_dashboard,
    _dashboard_content_fingerprint,
    _dashboard_payload_integrity_hash,
    _store_operations_ny_fed_observations,
    operations_snapshot_is_publicly_displayable,
    publish_official_dashboards,
    select_public_operations_snapshot,
)
from research.providers import NYFedMarketsProvider, ProviderResult
from research.services import SERIES_CATALOG, begin_ingestion, record_provider_result

FIXED_NOW = datetime(2026, 7, 14, 10, tzinfo=UTC)
FETCHED_AT = datetime(2026, 7, 14, 6, tzinfo=UTC)
LATEST_DAILY = date(2026, 7, 13)


def _dates(*, end: date, count: int, business_only: bool = False) -> list[date]:
    periods: list[date] = []
    cursor = end
    while len(periods) < count:
        if not business_only or cursor.weekday() < 5:
            periods.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(periods))


def _raw_result(
    *, dataset: str, records: list[dict], cycle: str, marker: str = "v1"
) -> ProviderResult:
    raw = json.dumps(
        {"dataset": dataset, "marker": marker, "rows": len(records)},
        sort_keys=True,
    ).encode()
    return ProviderResult(
        provider="ny-fed-markets",
        dataset=dataset,
        fetched_at=FETCHED_AT,
        records=records,
        raw_bytes=raw,
        metadata={
            "refresh_cycle_id": cycle,
            "endpoint": f"https://markets.example/{dataset}.json",
            "content_type": "application/json",
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )


def _treasury_records() -> list[dict]:
    records: list[dict] = []
    for index, period in enumerate(_dates(end=date(2026, 7, 9), count=35)):
        accepted = Decimal("25") if index == 34 else Decimal("10") + index
        operation_id = "BLANK-NOTE-25M" if index == 34 else f"TSY-{period}"
        operation = {
            "operationId": operation_id,
            "auctionStatus": "Results",
            "operationDirection": "P",
            "operationDate": period.isoformat(),
            "settlementDate": (period + timedelta(days=1)).isoformat(),
            "maturityRangeStart": "2027-01-01",
            "maturityRangeEnd": "2030-12-31",
            "operationType": "Treasury Coupon Purchase",
            "auctionMethod": "Multiple Price",
            "totalParAmtSubmitted": str((accepted + Decimal("5")) * 1_000_000),
            "totalParAmtAccepted": str(accepted * 1_000_000),
            "note": "",
        }
        records.append(
            {
                "series_id": "TREASURY-PURCHASES",
                "date": period.isoformat(),
                "value": accepted,
                "metadata": {
                    "unit": "USD millions",
                    "submitted": str(accepted + Decimal("5")),
                    "operation_count": 1,
                    "operations": [operation],
                    "coverage": "all official purchase results",
                    "small_value_classification": "unavailable",
                },
            }
        )
    return records


def _reverse_repo_records() -> list[dict]:
    records: list[dict] = []
    for index, period in enumerate(
        _dates(end=LATEST_DAILY, count=35, business_only=True)
    ):
        regular_value = Decimal("80") + index
        regular = {
            "operationId": f"RRP-{period}",
            "auctionStatus": "Results",
            "operationType": "Reverse Repo",
            "operationMethod": "Fixed Rate",
            "operationDate": period.isoformat(),
            "totalAmtSubmitted": str((regular_value + Decimal("20")) * 1_000_000),
            "totalAmtAccepted": str(regular_value * 1_000_000),
            "acceptedCpty": "42",
            "note": "",
            "details": [
                {
                    "percentAwardRate": "3.50",
                    "percentOfferingRate": "3.50",
                    "amtAccepted": str(regular_value * 1_000_000),
                }
            ],
        }
        small = {
            "operationId": f"RRP-SVE-{period}",
            "auctionStatus": "Results",
            "operationType": "Reverse Repo",
            "operationMethod": "Fixed Rate",
            "operationDate": period.isoformat(),
            "totalAmtSubmitted": "50000",
            "totalAmtAccepted": "50000",
            "acceptedCpty": "1",
            "note": "Small Value Exercise (SVE)",
            "details": [{"percentOfferingRate": "0.01", "amtAccepted": "50000"}],
        }
        common_regular = {
            "small_value_excluded": True,
            "classification": "non-small-value",
            "operations": [regular],
        }
        records.extend(
            [
                {
                    "series_id": "ONRRP",
                    "date": period.isoformat(),
                    "value": regular_value + Decimal("0.05"),
                    "metadata": {"operations": [regular, small]},
                },
                {
                    "series_id": "ONRRP-NON-SMALL-VALUE-TOTAL",
                    "date": period.isoformat(),
                    "value": regular_value,
                    "metadata": common_regular,
                },
                {
                    "series_id": "ONRRP-SMALL-VALUE-TOTAL",
                    "date": period.isoformat(),
                    "value": Decimal("0.05"),
                    "metadata": {
                        "small_value_only": True,
                        "classification": "small-value-technical-exercise",
                        "operations": [small],
                    },
                },
                {
                    "series_id": "ONRRP-RATE",
                    "date": period.isoformat(),
                    "value": Decimal("3.50"),
                    "metadata": {**common_regular, "reported_rates": ["3.50"]},
                },
                {
                    "series_id": "ONRRP-PARTICIPANTS",
                    "date": period.isoformat(),
                    "value": Decimal("42"),
                    "metadata": common_regular,
                },
            ]
        )
        if index == 34:
            records.append(
                {
                    "series_id": "ONRRP-BANK",
                    "date": period.isoformat(),
                    "value": Decimal("10"),
                    "metadata": {
                        "unit": "USD millions",
                        "counterparty_type": "BANK",
                        "operations": [regular],
                    },
                }
            )
    return records


def _standing_repo_records() -> list[dict]:
    records: list[dict] = []
    for index, period in enumerate(
        _dates(end=LATEST_DAILY, count=35, business_only=True)
    ):
        regular_value = Decimal(index % 4)
        regular = {
            "operationId": f"SRF-{period}",
            "auctionStatus": "Results",
            "operationType": "Repo",
            "operationMethod": "Full Allotment",
            "operationDate": period.isoformat(),
            "totalAmtSubmitted": str((regular_value + Decimal("10")) * 1_000_000),
            "totalAmtAccepted": str(regular_value * 1_000_000),
            "note": "",
            "details": [
                {
                    "securityType": "Treasury",
                    "amtAccepted": str(regular_value * 1_000_000),
                    "percentOfferingRate": None,
                    "minimumBidRate": "3.75",
                }
            ],
        }
        small = {
            "operationId": f"SRF-SVE-{period}",
            "auctionStatus": "Results",
            "operationType": "Repo",
            "operationMethod": "Full Allotment",
            "operationDate": period.isoformat(),
            "totalAmtSubmitted": "50000",
            "totalAmtAccepted": "50000",
            "note": "Small Value Exercise (SVE)",
            "details": [
                {
                    "securityType": "Treasury",
                    "amtAccepted": "50000",
                    "percentOfferingRate": "0.01",
                }
            ],
        }
        regular_metadata = {
            "small_value_excluded": True,
            "classification": "non-small-value",
            "operations": [regular],
        }
        values = {
            "SRP": (regular_value + Decimal("0.05"), {"operations": [regular, small]}),
            "SRP-NON-SMALL-VALUE-TOTAL": (regular_value, regular_metadata),
            "SRP-SMALL-VALUE-TOTAL": (
                Decimal("0.05"),
                {
                    "small_value_only": True,
                    "classification": "small-value-technical-exercise",
                    "operations": [small],
                },
            ),
            "SRP-NON-SMALL-VALUE-TREASURY": (regular_value, regular_metadata),
            "SRP-NON-SMALL-VALUE-AGENCY": (Decimal("0"), regular_metadata),
            "SRP-NON-SMALL-VALUE-MBS": (Decimal("0"), regular_metadata),
            "SRP-NON-SMALL-VALUE-RATE": (
                Decimal("3.75"),
                {**regular_metadata, "reported_rates": ["3.75"]},
            ),
        }
        for series_id, (value, metadata) in values.items():
            records.append(
                {
                    "series_id": series_id,
                    "date": period.isoformat(),
                    "value": value,
                    "metadata": metadata,
                }
            )
    return records


def _soma_records() -> list[dict]:
    fields = {
        "SOMA-TOTAL": "total",
        "SOMA-BILLS": "bills",
        "SOMA-NOTES-BONDS": "notesbonds",
        "SOMA-TIPS": "tips",
        "SOMA-FRN": "frn",
        "SOMA-TIPS-INFLATION-COMPENSATION": "tipsInflationCompensation",
        "SOMA-MBS": "mbs",
        "SOMA-CMBS": "cmbs",
        "SOMA-AGENCIES": "agencies",
    }
    records: list[dict] = []
    for index, period in enumerate((date(2026, 7, 1), date(2026, 7, 8))):
        base = Decimal("6000000") + index * Decimal("1000")
        for offset, (series_id, source_field) in enumerate(fields.items()):
            records.append(
                {
                    "series_id": series_id,
                    "date": period.isoformat(),
                    "value": base if series_id == "SOMA-TOTAL" else Decimal(offset * 100),
                    "metadata": {"source_field": source_field},
                }
            )
    return records


def _component_results(cycle: str, *, marker: str = "v1") -> dict[str, ProviderResult]:
    return {
        "treasury": _raw_result(
            dataset="treasury:purchases",
            records=_treasury_records(),
            cycle=cycle,
            marker=marker,
        ),
        "onrrp": _raw_result(
            dataset="repo:reverse-repo-fixed-results",
            records=_reverse_repo_records(),
            cycle=cycle,
            marker=marker,
        ),
        "srf": _raw_result(
            dataset="repo:standing-repo-full-allotment-results",
            records=_standing_repo_records(),
            cycle=cycle,
            marker=marker,
        ),
        "soma": _raw_result(
            dataset="soma:summary",
            records=_soma_records(),
            cycle=cycle,
            marker=marker,
        ),
    }


def _record_components(monkeypatch, tmp_path, *, cycle: str = "operations-cycle"):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    return {
        identity: record_provider_result(
            result, persist=_store_operations_ny_fed_observations
        )
        for identity, result in _component_results(cycle).items()
    }


def test_every_allowed_operations_series_has_an_explicit_catalog_unit():
    for series_keys in OPERATIONS_ALLOWED_SERIES.values():
        for series_key in series_keys:
            catalog = SERIES_CATALOG[series_key.upper()]
            expected_unit = (
                "%"
                if series_key.endswith("-rate")
                else "count"
                if series_key == "onrrp-participants"
                else "USD millions"
            )
            assert catalog[1] == expected_unit
            assert catalog[2] in {"daily", "weekly"}


@pytest.mark.django_db
def test_operations_v1_exact_same_cycle_contract_is_public_and_rendered(
    client, monkeypatch, tmp_path
):
    runs = _record_components(monkeypatch, tmp_path)
    assert all(run.status == IngestionRun.Status.SUCCESS for run in runs.values())

    dashboards, stale = _coordinate_operations_dashboard(runs.values())

    assert stale == set()
    assert len(dashboards) == 1
    snapshot = dashboards[0]
    assert {item["key"] for item in snapshot.data["metrics"]} == set(
        OPERATIONS_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in snapshot.data["charts"]} == set(
        OPERATIONS_REQUIRED_CHART_KEYS
    )
    assert {item["key"] for item in snapshot.data["sections"]} == set(
        OPERATIONS_REQUIRED_SECTION_KEYS
    )
    assert len(snapshot.data["component_batches"]) == 4
    optional_counterparty = Observation.objects.get(
        batch_id=runs["onrrp"].batch_id,
        series__key="onrrp-bank",
    )
    assert optional_counterparty.series.unit == "USD millions"
    assert all(RawArtifact.objects.filter(run=run).count() == 1 for run in runs.values())
    assert operations_snapshot_is_publicly_displayable(snapshot)
    assert select_public_operations_snapshot().pk == snapshot.pk
    ny_license = SourceLicense.objects.get(
        source__key="ny-fed-markets", is_current=True
    )
    original_notice = ny_license.required_notice
    ny_license.required_notice = "Updated attribution notice"
    ny_license.save(update_fields=["required_notice"])
    assert operations_snapshot_is_publicly_displayable(snapshot) is False
    ny_license.required_notice = original_notice
    ny_license.save(update_fields=["required_notice"])
    assert operations_snapshot_is_publicly_displayable(snapshot)

    metrics = {item["key"]: item for item in snapshot.data["metrics"]}
    assert Decimal(str(metrics["treasury-purchase-latest"]["value"])) == Decimal("25")
    assert Decimal(str(metrics["soma-weekly-change"]["value"])) == Decimal("1000")
    treasury_inputs = metrics["treasury-purchases-30d"]["metadata"]["input_lineage"]
    assert Decimal(str(metrics["treasury-purchases-30d"]["value"])) == sum(
        (Decimal(item["raw_value"]) for item in treasury_inputs), Decimal("0")
    )
    assert Decimal(str(metrics["onrrp-non-small-value-total"]["value"])) == (
        Decimal(
            metrics["onrrp-non-small-value-total"]["metadata"]["input_lineage"][0][
                "raw_value"
            ]
        )
        - Decimal(
            metrics["onrrp-non-small-value-total"]["metadata"]["input_lineage"][1][
                "raw_value"
            ]
        )
    )
    assert metrics["srf-active-days-30d"]["as_of"] == max(
        item["as_of"]
        for item in metrics["srf-active-days-30d"]["metadata"]["input_lineage"]
    )
    treasury_rows = next(
        item["rows"]
        for item in snapshot.data["sections"]
        if item["key"] == "recent-treasury-purchase-operations"
    )
    repo_rows = next(
        item["rows"]
        for item in snapshot.data["sections"]
        if item["key"] == "recent-repo-reverse-repo-operations"
    )
    assert len(treasury_rows) == 20
    assert len(repo_rows) == 20
    blank_note = next(row for row in treasury_rows if row["operation_id"] == "BLANK-NOTE-25M")
    assert blank_note["small_value_status"] == "unavailable"
    assert [item["key"] for item in blank_note["cells_list"]][-1] == "small-value"

    normalized = list(MetricSnapshot.objects.filter(batch_id=snapshot.batch_id))
    assert len(normalized) == 10
    assert all(
        item.metadata["publication_fingerprint"] == snapshot.data["fingerprint"]
        and item.metadata["payload_integrity_hash"]
        == snapshot.data["payload_integrity_hash"]
        for item in normalized
    )

    duplicate, duplicate_stale = _coordinate_operations_dashboard(runs.values())
    assert duplicate == []
    assert duplicate_stale == set()
    assert DashboardSnapshot.objects.filter(key="operations").count() == 1

    response = client.get("/liquidity/operations/")
    assert response.status_code == 200
    assert response.context["snapshot"].pk == snapshot.pk
    rendered = response.content.decode()
    assert "BLANK-NOTE-25M" in rendered
    assert "官方 feed 未披露" in rendered
    assert '组件数据血缘' in rendered
    assert "数值截至" in rendered
    assert "fallback 无" in rendered
    assert "<tr></tr>" not in rendered


@pytest.mark.django_db
def test_operations_partial_new_cycle_migrates_rows_but_retains_audited_stale_snapshot(
    monkeypatch, tmp_path
):
    runs = _record_components(monkeypatch, tmp_path, cycle="cycle-one")
    snapshot = _coordinate_operations_dashboard(runs.values())[0][0]
    old_treasury_batch = runs["treasury"].batch_id

    new_treasury = record_provider_result(
        _component_results("cycle-two", marker="new-treasury")["treasury"],
        persist=_store_operations_ny_fed_observations,
    )
    failed_onrrp = record_provider_result(
        ProviderResult(
            provider="ny-fed-markets",
            dataset="repo:reverse-repo-fixed-results",
            fetched_at=FETCHED_AT,
            error="upstream partial cycle failure",
            metadata={"refresh_cycle_id": "cycle-two"},
        )
    )
    assert new_treasury.status == IngestionRun.Status.SUCCESS
    assert not Observation.objects.filter(batch_id=old_treasury_batch).exists()

    dashboards, stale = _coordinate_operations_dashboard(
        [new_treasury, failed_onrrp]
    )

    assert dashboards == []
    assert stale == {"operations"}
    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.STALE
    assert "refresh_failure" in snapshot.data
    assert operations_snapshot_is_publicly_displayable(snapshot)
    assert select_public_operations_snapshot().pk == snapshot.pk

    ny_license = SourceLicense.objects.get(
        source__key="ny-fed-markets", is_current=True
    )
    original_notice = ny_license.required_notice
    ny_license.required_notice = "Updated stale attribution notice"
    ny_license.save(update_fields=["required_notice"])
    assert operations_snapshot_is_publicly_displayable(snapshot) is False
    ny_license.required_notice = original_notice
    ny_license.save(update_fields=["required_notice"])
    assert operations_snapshot_is_publicly_displayable(snapshot)

    tampered = deepcopy(snapshot.data)
    tampered["charts"][0]["data"][0]["refresh_failure"] = {
        "forged": "nested reserved key"
    }
    tampered["fingerprint"] = _dashboard_content_fingerprint(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=tampered,
    )
    tampered["payload_integrity_hash"] = _dashboard_payload_integrity_hash(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=tampered,
    )
    snapshot.data = tampered
    snapshot.save(update_fields=["data"])
    assert operations_snapshot_is_publicly_displayable(snapshot) is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tamper",
    [
        "chart-value",
        "lineage-batch",
        "lineage-time",
        "non-dict-row",
        "non-dict-cell",
        "string-metadata",
        "invalid-run-id",
    ],
)
def test_operations_strict_selector_rejects_tamper_and_malformed_json(
    monkeypatch, tmp_path, tamper
):
    runs = _record_components(monkeypatch, tmp_path)
    snapshot = _coordinate_operations_dashboard(runs.values())[0][0]
    data = deepcopy(snapshot.data)
    if tamper == "chart-value":
        data["charts"][0]["data"][0]["Purchases"] += 1
    elif tamper == "lineage-batch":
        data["charts"][0]["data"][0]["_lineage"]["Purchases"]["batch_id"] = str(
            runs["onrrp"].batch_id
        )
    elif tamper == "lineage-time":
        data["sections"][0]["rows"][0]["fetched_at"] = FIXED_NOW.isoformat()
    elif tamper == "non-dict-row":
        data["sections"][0]["rows"][0] = "malformed"
    elif tamper == "non-dict-cell":
        data["sections"][0]["rows"][0]["cells_list"][0] = "malformed"
    elif tamper == "string-metadata":
        data["metrics"][0]["metadata"] = "malformed"
    elif tamper == "invalid-run-id":
        data["component_runs"][0]["ingestion_run_id"] = "not-an-integer"
    data["fingerprint"] = _dashboard_content_fingerprint(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=data,
    )
    data["payload_integrity_hash"] = _dashboard_payload_integrity_hash(
        title=snapshot.title,
        summary=snapshot.summary,
        snapshot_data=data,
    )
    snapshot.data = data
    snapshot.save(update_fields=["data"])

    assert operations_snapshot_is_publicly_displayable(snapshot) is False


@pytest.mark.django_db
def test_operations_selector_rejects_artifact_and_normalized_metric_tamper(
    monkeypatch, tmp_path
):
    runs = _record_components(monkeypatch, tmp_path)
    snapshot = _coordinate_operations_dashboard(runs.values())[0][0]
    artifact = RawArtifact.objects.get(run=runs["soma"])
    artifact.sha256 = "0" * 64
    artifact.save(update_fields=["sha256"])
    assert operations_snapshot_is_publicly_displayable(snapshot) is False

    artifact.sha256 = str((runs["soma"].metadata or {})["sha256"])
    artifact.save(update_fields=["sha256"])
    digest = artifact.sha256
    artifact_path = tmp_path / digest[:2] / f"{digest}.bin"
    original_bytes = artifact_path.read_bytes()
    artifact_path.write_bytes(b"tampered raw bytes")
    assert operations_snapshot_is_publicly_displayable(snapshot) is False
    artifact_path.write_bytes(original_bytes)

    normalized = MetricSnapshot.objects.filter(batch_id=snapshot.batch_id).first()
    MetricSnapshot.objects.create(
        key="operations-unexpected-extra",
        label="Unexpected",
        value=Decimal("1"),
        value_date=normalized.value_date,
        as_of=normalized.as_of,
        fetched_at=normalized.fetched_at,
        batch_id=snapshot.batch_id,
        source=normalized.source,
        quality_status=normalized.quality_status,
        license_scope=normalized.license_scope,
        metadata=normalized.metadata,
    )
    assert operations_snapshot_is_publicly_displayable(snapshot) is False
    MetricSnapshot.objects.filter(
        batch_id=snapshot.batch_id, key="operations-unexpected-extra"
    ).delete()

    original_unit = normalized.unit
    normalized.unit = "tampered-unit"
    normalized.save(update_fields=["unit"])
    assert operations_snapshot_is_publicly_displayable(snapshot) is False
    normalized.unit = original_unit
    normalized.save(update_fields=["unit"])
    assert operations_snapshot_is_publicly_displayable(snapshot)

    normalized.metadata = {**normalized.metadata, "payload_integrity_hash": "0" * 64}
    normalized.save(update_fields=["metadata"])
    assert operations_snapshot_is_publicly_displayable(snapshot) is False


@pytest.mark.django_db
def test_operations_generic_publisher_cannot_bypass_v1(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    assert "operations" in INDEPENDENT_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"operations"}) == []
    publish_official_dashboards()
    assert not DashboardSnapshot.objects.filter(key="operations").exists()


@pytest.mark.django_db
def test_operations_same_cycle_and_latest_attempt_are_mandatory(monkeypatch, tmp_path):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    runs = {
        identity: record_provider_result(
            result, persist=_store_operations_ny_fed_observations
        )
        for identity, result in {
            identity: _component_results(f"cycle-{identity}")[identity]
            for identity in ("treasury", "onrrp", "srf", "soma")
        }.items()
    }
    dashboards, stale = _coordinate_operations_dashboard(runs.values())
    assert dashboards == []
    assert stale == {"operations"}
    assert not DashboardSnapshot.objects.filter(key="operations").exists()


@pytest.mark.django_db
def test_operations_persistence_rejects_regression_and_superseded_writer(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    monkeypatch.setattr(settings, "RAW_ARTIFACT_ROOT", tmp_path)
    first = record_provider_result(
        _component_results("cycle-one")["treasury"],
        persist=_store_operations_ny_fed_observations,
    )
    assert first.status == IngestionRun.Status.SUCCESS

    older_records = deepcopy(_treasury_records())
    for record in older_records:
        old_date = date.fromisoformat(record["date"]) - timedelta(days=60)
        record["date"] = old_date.isoformat()
        operation = record["metadata"]["operations"][0]
        operation["operationDate"] = old_date.isoformat()
        operation["settlementDate"] = (old_date + timedelta(days=1)).isoformat()
    regressed = record_provider_result(
        _raw_result(
            dataset="treasury:purchases",
            records=older_records,
            cycle="cycle-two",
            marker="regressed",
        ),
        persist=_store_operations_ny_fed_observations,
    )
    assert regressed.status == IngestionRun.Status.FAILED
    assert "regressed" in regressed.error

    stale_writer = begin_ingestion("ny-fed-markets", "treasury:purchases")
    begin_ingestion("ny-fed-markets", "treasury:purchases")
    with pytest.raises(ValueError, match="superseded"):
        _store_operations_ny_fed_observations(
            _component_results("cycle-three")["treasury"],
            stale_writer.source,
            stale_writer,
        )


def _provider_response(payload: dict) -> tuple[dict, bytes, dict, None]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return (
        payload,
        raw,
        {
            "content_type": "application/json",
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        None,
    )


def test_operations_providers_preserve_raw_bytes_and_fail_closed_on_schema(monkeypatch):
    provider = NYFedMarketsProvider()
    treasury = {
        "treasury": {
            "auctions": [
                {
                    "operationId": "T-1",
                    "auctionStatus": "Results",
                    "operationDirection": "P",
                    "operationDate": "2026-07-09",
                    "settlementDate": "2026-07-10",
                    "maturityRangeStart": "2027-01-01",
                    "maturityRangeEnd": "2030-12-31",
                    "operationType": "Treasury Coupon Purchase",
                    "totalParAmtSubmitted": "30000000",
                    "totalParAmtAccepted": "25000000",
                    "note": "",
                }
            ]
        }
    }
    response = _provider_response(treasury)
    monkeypatch.setattr(provider, "_get_json_with_raw", lambda *_args: response)
    result = provider.treasury_purchases(limit=100)
    assert result.ok
    assert result.raw_bytes == response[1]
    assert result.records[0]["value"] == Decimal("25")
    assert "small_value_classification" in result.records[0]["metadata"]

    duplicate = deepcopy(treasury)
    duplicate["treasury"]["auctions"].append(
        deepcopy(duplicate["treasury"]["auctions"][0])
    )
    monkeypatch.setattr(
        provider, "_get_json_with_raw", lambda *_args: _provider_response(duplicate)
    )
    assert provider.treasury_purchases().error


def test_reverse_repo_null_award_rate_falls_back_to_offering_rate(monkeypatch):
    provider = NYFedMarketsProvider()
    operation = {
        "operationId": "RRP-1",
        "auctionStatus": "Results",
        "operationType": "Reverse Repo",
        "operationMethod": "Fixed Rate",
        "operationDate": "2026-07-13",
        "totalAmtSubmitted": "1000000",
        "totalAmtAccepted": "1000000",
        "acceptedCpty": "2",
        "details": [
            {
                "amtAccepted": "1000000",
                "percentAwardRate": None,
                "percentOfferingRate": "3.50",
            }
        ],
    }
    payload = {"repo": {"operations": [operation]}}
    monkeypatch.setattr(
        provider, "_get_json_with_raw", lambda *_args: _provider_response(payload)
    )
    result = provider.reverse_repo_results()
    assert result.ok
    rate = next(item for item in result.records if item["series_id"] == "ONRRP-RATE")
    assert rate["value"] == Decimal("3.50")


def test_soma_validates_full_unsorted_envelope_before_sort_and_limit(monkeypatch):
    provider = NYFedMarketsProvider()
    payload = {
        "soma": {
            "summary": [
                {"asOfDate": "2026-07-08", "total": "2000000"},
                {"asOfDate": "2026-07-01", "total": "1000000"},
            ]
        }
    }
    monkeypatch.setattr(
        provider, "_get_json_with_raw", lambda *_args: _provider_response(payload)
    )
    result = provider.soma_summary(limit=1)
    assert result.ok
    assert {item["date"] for item in result.records} == {"2026-07-08"}

    malformed_old_row = deepcopy(payload)
    malformed_old_row["soma"]["summary"][1]["total"] = "not-a-number"
    monkeypatch.setattr(
        provider,
        "_get_json_with_raw",
        lambda *_args: _provider_response(malformed_old_row),
    )
    assert provider.soma_summary(limit=1).error

    malformed = {"treasury": {"auctions": [{"operationId": "T-2"}]}}
    monkeypatch.setattr(
        provider, "_get_json_with_raw", lambda *_args: _provider_response(malformed)
    )
    assert provider.treasury_purchases().error


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("totalAmtAccepted", ""),
        ("auctionStatus", "Pending"),
        ("operationType", "Reverse Repo"),
        ("operationMethod", "Multiple Price"),
        ("detail_amount", "500000"),
    ],
)
def test_strict_standing_repo_rejects_schema_and_reconciliation_drift(
    monkeypatch, mutation, value
):
    provider = NYFedMarketsProvider()
    operation = {
        "operationId": "SRF-1",
        "auctionStatus": "Results",
        "operationType": "Repo",
        "operationMethod": "Full Allotment",
        "operationDate": "2026-07-13",
        "totalAmtSubmitted": "1000000",
        "totalAmtAccepted": "1000000",
        "details": [
            {
                "securityType": "Treasury",
                "amtAccepted": "1000000",
                "percentOfferingRate": None,
                "minimumBidRate": "3.75",
            }
        ],
    }
    payload = {"repo": {"operations": [operation]}}
    monkeypatch.setattr(
        provider, "_get_json_with_raw", lambda *_args: _provider_response(payload)
    )
    result = provider.standing_repo_results(strict=True)
    assert result.ok
    rate = next(
        item for item in result.records if item["series_id"] == "SRP-NON-SMALL-VALUE-RATE"
    )
    assert rate["value"] == Decimal("3.75")

    bad = deepcopy(payload)
    if mutation == "detail_amount":
        bad["repo"]["operations"][0]["details"][0]["amtAccepted"] = value
    else:
        bad["repo"]["operations"][0][mutation] = value
    monkeypatch.setattr(
        provider, "_get_json_with_raw", lambda *_args: _provider_response(bad)
    )
    assert provider.standing_repo_results(strict=True).error
