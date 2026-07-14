from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

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
    SUBSURFACE_REQUIRED_CHART_KEYS,
    SUBSURFACE_REQUIRED_METRIC_KEYS,
    SUBSURFACE_REQUIRED_SECTION_KEYS,
    _coordinate_subsurface_dashboard,
    _dashboard_content_fingerprint,
    _store_prates_observations,
    _store_subsurface_ny_fed_observations,
    publish_official_dashboards,
    select_public_subsurface_snapshot,
    subsurface_snapshot_is_publicly_displayable,
)
from research.page_registry import get_page_config
from research.providers import ProviderResult
from research.services import begin_ingestion, record_provider_result

FIXED_NOW = datetime(2026, 7, 14, 10, tzinfo=UTC)
CURRENT_DATE = date(2026, 7, 13)


def _business_dates(count: int = 65) -> list[date]:
    periods: list[date] = []
    cursor = CURRENT_DATE
    while len(periods) < count:
        if cursor.weekday() < 5:
            periods.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(periods))


def _raw_result(
    *,
    dataset: str,
    records: list[dict],
    cycle: str,
) -> ProviderResult:
    raw = f'{{"dataset":"{dataset}","rows":{len(records)}}}'.encode()
    return ProviderResult(
        provider="ny-fed-markets",
        dataset=dataset,
        fetched_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
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


def _record_components(
    monkeypatch,
    *,
    cycle: str = "subsurface-cycle",
    history_count: int = 65,
    constant_volume: bool = False,
    missing_sofr_metadata: bool = False,
    swap_small_marker: str = "",
    swap_small_note: str = "",
    omit_swap_series: str | None = None,
) -> dict[str, IngestionRun]:
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    periods = _business_dates(history_count)
    sofr_records = []
    for index, period in enumerate(periods):
        metadata = {
            "percentPercentile99": str(Decimal("3.60") + Decimal(index) / 1000),
            "volumeInBillions": str(
                Decimal("1800")
                if constant_volume
                else Decimal("1800") + index * index
            ),
        }
        if missing_sofr_metadata and period == CURRENT_DATE:
            metadata.pop("percentPercentile99")
        sofr_records.append(
            {
                "series_id": "SOFR",
                "date": period.isoformat(),
                "value": Decimal("3.50") + Decimal(index) / 1000,
                "metadata": metadata,
            }
        )
    iorb_records = [
        {
            "series_id": "IORB",
            "date": period.isoformat(),
            "value": Decimal("3.65"),
            "metadata": {"unit": "Percent"},
        }
        for period in periods
    ]
    srf_periods = [period for period in periods if period >= CURRENT_DATE - timedelta(days=29)]
    srf_records: list[dict] = []
    for index, period in enumerate(srf_periods):
        accepted = Decimal(index % 3)
        regular_operation = {
            "operationId": f"REGULAR-{period}",
            "operationDate": period.isoformat(),
            "totalAmtAccepted": str(accepted * Decimal("1000000")),
            "isSmallValue": "",
            "details": [
                {
                    "securityType": "Treasury",
                    "amtAccepted": str(accepted * Decimal("1000000")),
                    "percentOfferingRate": "3.75",
                }
            ],
        }
        small_operation = {
            "operationId": f"SMALL-{period}",
            "operationDate": period.isoformat(),
            "totalAmtAccepted": "50000",
            "isSmallValue": "Y",
            "note": "Small-Value Technical Exercise",
            "details": [
                {
                    "securityType": "Treasury",
                    "amtAccepted": "50000",
                    "percentOfferingRate": "0.01",
                }
            ],
        }
        values = {
            "SRP-NON-SMALL-VALUE-TOTAL": accepted,
            "SRP-SMALL-VALUE-TOTAL": Decimal("0.05"),
            "SRP-NON-SMALL-VALUE-TREASURY": accepted,
            "SRP-NON-SMALL-VALUE-AGENCY": Decimal("0"),
            "SRP-NON-SMALL-VALUE-MBS": Decimal("0"),
            "SRP-NON-SMALL-VALUE-RATE": Decimal("3.75"),
        }
        for series_id, value in values.items():
            if series_id == "SRP-SMALL-VALUE-TOTAL":
                metadata = {
                    "small_value_only": True,
                    "classification": "small-value-technical-exercise",
                    "operations": [small_operation],
                }
            else:
                metadata = {
                    "small_value_excluded": True,
                    "classification": "non-small-value",
                    "operations": [regular_operation],
                }
                if series_id == "SRP-NON-SMALL-VALUE-RATE":
                    metadata["reported_rates"] = ["3.75"]
            srf_records.append(
                {
                    "series_id": series_id,
                    "date": period.isoformat(),
                    "value": value,
                    "metadata": metadata,
                }
            )
    active_operation = {
        "counterparty": "European Central Bank",
        "settlementDate": "2026-07-09",
        "maturityDate": "2026-07-16",
        "amount": "128000000",
        "isSmallValue": swap_small_marker,
        "note": swap_small_note,
    }
    small_active_operation = {
        "counterparty": "Swiss National Bank",
        "settlementDate": "2026-07-09",
        "maturityDate": "2026-07-16",
        "amount": "50000",
        "isSmallValue": "Y",
        "note": "Small-Value Technical Exercise",
    }
    swap_records = [
        {
            "series_id": "FXSWAP-USD-DRAWDOWN-NON-SMALL-VALUE",
            "date": "2026-07-09",
            "value": Decimal("128"),
            "metadata": {
                "operations": [active_operation],
                "small_value_excluded": True,
                "classification": "non-small-value",
            },
        },
        {
            "series_id": "FXSWAP-USD-DRAWDOWN-SMALL-VALUE",
            "date": "2026-07-09",
            "value": Decimal("0.05"),
            "metadata": {
                "operations": [small_active_operation],
                "small_value_only": True,
                "classification": "small-value-technical-exercise",
            },
        },
        {
            "series_id": "FXSWAP-USD-OUTSTANDING",
            "date": CURRENT_DATE.isoformat(),
            "value": Decimal("128.05"),
            "metadata": {
                "as_of": CURRENT_DATE.isoformat(),
                "active_operations": [active_operation, small_active_operation],
            },
        },
        {
            "series_id": "FXSWAP-USD-OUTSTANDING-SMALL-VALUE",
            "date": CURRENT_DATE.isoformat(),
            "value": Decimal("0.05"),
            "metadata": {
                "as_of": CURRENT_DATE.isoformat(),
                "active_operations": [small_active_operation],
                "small_value_only": True,
            },
        },
        {
            "series_id": "FXSWAP-USD-OUTSTANDING-NON-SMALL-VALUE",
            "date": CURRENT_DATE.isoformat(),
            "value": Decimal("128"),
            "metadata": {
                "as_of": CURRENT_DATE.isoformat(),
                "active_operations": [active_operation],
                "small_value_excluded": True,
            },
        },
    ]
    if omit_swap_series:
        swap_records = [
            record
            for record in swap_records
            if record["series_id"] != omit_swap_series
        ]
    sofr = record_provider_result(
        _raw_result(
            dataset="reference-rate:sofr", records=sofr_records, cycle=cycle
        ),
        persist=_store_subsurface_ny_fed_observations,
    )
    archive_hash = "a" * 64
    iorb = record_provider_result(
        ProviderResult(
            provider="federal-reserve",
            dataset="prates:iorb",
            fetched_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
            records=iorb_records,
            metadata={
                "archive_sha256": archive_hash,
                "archive_size": 1024,
                "source_url": "https://www.federalreserve.gov/prates.zip",
            },
        ),
        persist=_store_prates_observations,
    )
    srf = record_provider_result(
        _raw_result(
            dataset="repo:standing-repo-full-allotment-results",
            records=srf_records,
            cycle=cycle,
        ),
        persist=_store_subsurface_ny_fed_observations,
    )
    swaps = record_provider_result(
        _raw_result(
            dataset="fx-swaps:usdollar", records=swap_records, cycle=cycle
        ),
        persist=_store_subsurface_ny_fed_observations,
    )
    return {"sofr": sofr, "iorb": iorb, "srf": srf, "swaps": swaps}


@pytest.mark.django_db
def test_subsurface_registry_and_generic_publisher_cannot_bypass_contract():
    config = get_page_config("subsurface")
    assert config["snapshot_contract_version"] == 1
    assert len(config["metrics"]) == 11
    assert all(item["value"] is None for item in config["metrics"])
    assert "subsurface" in INDEPENDENT_PUBLICATION_KEYS
    assert publish_official_dashboards(keys={"subsurface"}) == []


@pytest.mark.django_db
def test_subsurface_v1_happy_path_is_exact_and_public(client, monkeypatch):
    runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert stale == set()
    assert len(dashboards) == 1
    snapshot = dashboards[0]
    assert {item["key"] for item in snapshot.data["metrics"]} == set(
        SUBSURFACE_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in snapshot.data["charts"]} == set(
        SUBSURFACE_REQUIRED_CHART_KEYS
    )
    assert {item["key"] for item in snapshot.data["sections"]} == set(
        SUBSURFACE_REQUIRED_SECTION_KEYS
    )
    assert snapshot.data["common_effective_date"] == CURRENT_DATE.isoformat()
    assert snapshot.data["swap_as_of"] == CURRENT_DATE.isoformat()
    assert len(snapshot.data["component_batches"]) == 4
    sections = {item["key"]: item for item in snapshot.data["sections"]}
    recent = sections["recent-subsurface-observations"]["rows"][-1]
    assert [item["key"] for item in recent["cells_list"]] == [
        "date",
        "sofr",
        "99p",
        "iorb",
        "99p-iorb",
        "volume",
        "z60",
    ]
    operation_rows = sections["recent-srf-operations"]["rows"]
    assert any(
        row["session"] == f"REGULAR-{CURRENT_DATE}"
        and [item["key"] for item in row["cells_list"]]
        == ["date", "session", "accepted", "rate", "is-small-value"]
        for row in operation_rows
    )
    assert all(RawArtifact.objects.filter(run=run).count() == 1 for run in runs.values())
    assert subsurface_snapshot_is_publicly_displayable(snapshot)
    assert select_public_subsurface_snapshot().pk == snapshot.pk
    duplicate_dashboards, duplicate_stale = _coordinate_subsurface_dashboard(
        runs.values()
    )
    assert duplicate_dashboards == []
    assert duplicate_stale == set()
    assert DashboardSnapshot.objects.filter(key="subsurface").count() == 1
    response = client.get("/liquidity/subsurface/")
    assert response.status_code == 200
    assert response.context["snapshot"].pk == snapshot.pk
    rendered = response.content.decode()
    assert f"<td>{CURRENT_DATE.isoformat()}</td>" in rendered
    assert f"REGULAR-{CURRENT_DATE}" in rendered
    assert "<tr></tr>" not in rendered
    normalized_swap = MetricSnapshot.objects.get(
        batch_id=snapshot.batch_id,
        key="subsurface-fxswap-outstanding-non-small-value",
    )
    assert normalized_swap.metadata["swap_as_of"] == CURRENT_DATE.isoformat()
    assert normalized_swap.metadata["active_operations"]
    assert normalized_swap.metadata["small_value_active_operations"]


@pytest.mark.django_db
def test_subsurface_missing_latest_sofr_metadata_fails_closed(monkeypatch):
    runs = _record_components(monkeypatch, missing_sofr_metadata=True)
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert dashboards == []
    assert stale == {"subsurface"}
    assert not DashboardSnapshot.objects.filter(key="subsurface").exists()


@pytest.mark.parametrize(
    ("marker", "note"),
    [
        ("TRUE", ""),
        ("", "Small-Value Technical Exercise"),
    ],
)
@pytest.mark.django_db
def test_subsurface_swap_small_value_operation_cannot_enter_non_test_outstanding(
    monkeypatch, marker, note
):
    runs = _record_components(
        monkeypatch,
        swap_small_marker=marker,
        swap_small_note=note,
    )
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert dashboards == []
    assert stale == {"subsurface"}
    assert not DashboardSnapshot.objects.filter(key="subsurface").exists()


@pytest.mark.django_db
def test_subsurface_main_components_require_one_refresh_cycle(monkeypatch):
    runs = _record_components(monkeypatch)
    runs["srf"].metadata = {
        **runs["srf"].metadata,
        "refresh_cycle_id": "different-cycle",
    }
    runs["srf"].save(update_fields=["metadata", "updated_at"])

    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert dashboards == []
    assert stale == {"subsurface"}
    assert not DashboardSnapshot.objects.filter(key="subsurface").exists()


@pytest.mark.parametrize(
    ("history_count", "constant_volume"),
    [(59, False), (65, True)],
)
@pytest.mark.django_db
def test_subsurface_z60_requires_60_rows_and_nonzero_population_std(
    monkeypatch, history_count, constant_volume
):
    runs = _record_components(
        monkeypatch,
        history_count=history_count,
        constant_volume=constant_volume,
    )

    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert dashboards == []
    assert stale == {"subsurface"}
    assert not DashboardSnapshot.objects.filter(key="subsurface").exists()


@pytest.mark.django_db
def test_subsurface_never_promotes_older_day_when_current_iorb_is_missing(
    monkeypatch,
):
    runs = _record_components(monkeypatch)
    deleted, _ = Observation.objects.filter(
        batch_id=runs["iorb"].batch_id,
        series__key="iorb",
        value_date__date=CURRENT_DATE,
    ).delete()
    assert deleted == 1
    runs["iorb"].row_count -= 1
    runs["iorb"].save(update_fields=["row_count", "updated_at"])

    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert dashboards == []
    assert stale == {"subsurface"}
    assert not DashboardSnapshot.objects.filter(key="subsurface").exists()


@pytest.mark.parametrize(
    "series_id",
    ["FXSWAP-USD-OUTSTANDING", "FXSWAP-USD-OUTSTANDING-SMALL-VALUE"],
)
@pytest.mark.django_db
def test_subsurface_swap_required_outstanding_series_cannot_be_omitted(
    monkeypatch, series_id
):
    runs = _record_components(monkeypatch, omit_swap_series=series_id)

    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert runs["swaps"].status == IngestionRun.Status.FAILED
    assert dashboards == []
    assert stale == {"subsurface"}


@pytest.mark.parametrize("tamper", ["equation", "small-operations", "drawdown"])
@pytest.mark.django_db
def test_subsurface_swap_values_and_operation_partitions_are_recomputed(
    monkeypatch, tamper
):
    runs = _record_components(monkeypatch)
    if tamper == "equation":
        observation = Observation.objects.get(
            batch_id=runs["swaps"].batch_id,
            series__key="fxswap-usd-outstanding",
        )
        observation.value += Decimal("1")
        observation.save(update_fields=["value", "updated_at"])
    elif tamper == "small-operations":
        observation = Observation.objects.get(
            batch_id=runs["swaps"].batch_id,
            series__key="fxswap-usd-outstanding-small-value",
        )
        metadata = deepcopy(observation.metadata)
        metadata["active_operations"][0]["isSmallValue"] = ""
        metadata["active_operations"][0]["note"] = ""
        observation.metadata = metadata
        observation.save(update_fields=["metadata", "updated_at"])
    else:
        observation = Observation.objects.get(
            batch_id=runs["swaps"].batch_id,
            series__key="fxswap-usd-drawdown-non-small-value",
        )
        observation.value += Decimal("1")
        observation.save(update_fields=["value", "updated_at"])

    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert dashboards == []
    assert stale == {"subsurface"}


@pytest.mark.parametrize("tamper", ["multiple-rates", "classification", "collateral"])
@pytest.mark.django_db
def test_subsurface_srf_operation_contract_rejects_inconsistent_rows(
    monkeypatch, tamper
):
    runs = _record_components(monkeypatch)
    if tamper == "multiple-rates":
        observation = Observation.objects.get(
            batch_id=runs["srf"].batch_id,
            series__key="srp-non-small-value-rate",
            value_date__date=CURRENT_DATE,
        )
        metadata = deepcopy(observation.metadata)
        metadata["reported_rates"] = ["3.75", "3.80"]
        observation.metadata = metadata
        observation.save(update_fields=["metadata", "updated_at"])
    elif tamper == "classification":
        observation = Observation.objects.get(
            batch_id=runs["srf"].batch_id,
            series__key="srp-non-small-value-total",
            value_date__date=CURRENT_DATE,
        )
        metadata = deepcopy(observation.metadata)
        metadata["operations"][0]["isSmallValue"] = "Y"
        observation.metadata = metadata
        observation.save(update_fields=["metadata", "updated_at"])
    else:
        observation = Observation.objects.get(
            batch_id=runs["srf"].batch_id,
            series__key="srp-non-small-value-treasury",
            value_date__date=CURRENT_DATE,
        )
        observation.value += Decimal("1")
        observation.save(update_fields=["value", "updated_at"])

    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())

    assert dashboards == []
    assert stale == {"subsurface"}


@pytest.mark.django_db
def test_subsurface_retained_stale_revalidates_stored_component_cycle(monkeypatch):
    runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    failed = record_provider_result(
        ProviderResult.failure("federal-reserve", "prates:iorb", "outage")
    )
    assert _coordinate_subsurface_dashboard([failed])[1] == {"subsurface"}
    runs["srf"].metadata = {
        **runs["srf"].metadata,
        "refresh_cycle_id": "tampered-cycle",
    }
    runs["srf"].save(update_fields=["metadata", "updated_at"])
    snapshot.refresh_from_db()

    assert not subsurface_snapshot_is_publicly_displayable(snapshot)
    assert select_public_subsurface_snapshot() is None


@pytest.mark.parametrize("artifact_field", ["sha256", "size_bytes", "uri"])
@pytest.mark.django_db
def test_subsurface_selector_rejects_artifact_binding_tamper(
    monkeypatch, artifact_field
):
    runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    artifact = RawArtifact.objects.get(run=runs["sofr"])
    if artifact_field == "sha256":
        artifact.sha256 = "0" * 64
    elif artifact_field == "size_bytes":
        artifact.size_bytes += 1
    else:
        artifact.uri = "private://ny-fed-markets/tampered.bin"
    artifact.save(update_fields=[artifact_field, "updated_at"])

    assert not subsurface_snapshot_is_publicly_displayable(snapshot)
    assert select_public_subsurface_snapshot() is None


@pytest.mark.parametrize("tamper", ["fingerprint", "formula", "normalized"])
@pytest.mark.django_db
def test_subsurface_selector_rejects_snapshot_and_normalized_metric_tamper(
    monkeypatch, tamper
):
    runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    if tamper == "normalized":
        normalized = MetricSnapshot.objects.get(
            batch_id=snapshot.batch_id,
            key="subsurface-sofr-p99-minus-rate",
        )
        normalized.value = Decimal("999")
        normalized.save(update_fields=["value", "updated_at"])
    else:
        data = deepcopy(snapshot.data)
        if tamper == "formula":
            data["formulas"]["sofr-p99-minus-rate"] = "opaque formula"
            data["fingerprint"] = _dashboard_content_fingerprint(
                title=snapshot.title,
                summary=snapshot.summary,
                snapshot_data=data,
            )
        else:
            data["fingerprint"] = "0" * 64
        snapshot.data = data
        snapshot.save(update_fields=["data", "updated_at"])

    assert not subsurface_snapshot_is_publicly_displayable(snapshot)
    assert select_public_subsurface_snapshot() is None


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("active_operations", []),
        ("all_active_operations", []),
        ("small_value_active_operations", []),
        ("swap_as_of", "1999-01-01"),
        ("swap_total", "999"),
        ("swap_small_value", "999"),
    ],
)
@pytest.mark.django_db
def test_subsurface_selector_rejects_normalized_swap_evidence_tamper(
    monkeypatch, field, tampered_value
):
    runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    normalized = MetricSnapshot.objects.get(
        batch_id=snapshot.batch_id,
        key="subsurface-fxswap-outstanding-non-small-value",
    )
    metadata = deepcopy(normalized.metadata)
    metadata[field] = tampered_value
    normalized.metadata = metadata
    normalized.save(update_fields=["metadata", "updated_at"])

    assert not subsurface_snapshot_is_publicly_displayable(snapshot)
    assert select_public_subsurface_snapshot() is None


@pytest.mark.django_db
def test_subsurface_latest_failed_attempt_retains_stale_then_same_value_recovers(
    monkeypatch,
):
    first_runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(first_runs.values())
    assert stale == set()
    first = dashboards[0]
    failed = record_provider_result(
        ProviderResult.failure(
            "federal-reserve", "prates:iorb", "upstream archive unavailable"
        )
    )

    failed_dashboards, failed_stale = _coordinate_subsurface_dashboard([failed])

    assert failed_dashboards == []
    assert failed_stale == {"subsurface"}
    first.refresh_from_db()
    assert first.quality_status == Observation.Quality.STALE
    assert first.data["refresh_failure"]["components"]
    assert select_public_subsurface_snapshot().pk == first.pk

    recovered_runs = _record_components(monkeypatch)
    recovered_dashboards, recovered_stale = _coordinate_subsurface_dashboard(
        recovered_runs.values()
    )

    assert recovered_stale == set()
    assert recovered_dashboards == []
    first.refresh_from_db()
    assert first.quality_status == Observation.Quality.ESTIMATED
    assert "refresh_failure" not in first.data
    assert set(first.data["component_batches"]) == {
        str(run.batch_id) for run in recovered_runs.values()
    }
    assert select_public_subsurface_snapshot().pk == first.pk


@pytest.mark.django_db
def test_subsurface_selector_rejects_fallback_and_revoked_licence(monkeypatch):
    runs = _record_components(monkeypatch)
    Observation.objects.filter(batch_id=runs["sofr"].batch_id).update(
        fallback_source=runs["iorb"].source
    )
    dashboards, stale = _coordinate_subsurface_dashboard(runs.values())
    assert dashboards == []
    assert stale == {"subsurface"}

    fresh_runs = _record_components(monkeypatch)
    dashboards, stale = _coordinate_subsurface_dashboard(fresh_runs.values())
    assert stale == set()
    snapshot = dashboards[0]
    licence = SourceLicense.objects.get(
        source__key="ny-fed-markets", is_current=True
    )
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed", "updated_at"])
    assert not subsurface_snapshot_is_publicly_displayable(snapshot)
    assert select_public_subsurface_snapshot() is None


@pytest.mark.django_db
def test_subsurface_superseded_writer_cannot_persist(monkeypatch):
    monkeypatch.setattr("research.official_data.timezone.now", lambda: FIXED_NOW)
    result = _raw_result(
        dataset="reference-rate:sofr",
        cycle="old-writer",
        records=[
            {
                "series_id": "SOFR",
                "date": CURRENT_DATE.isoformat(),
                "value": Decimal("3.50"),
                "metadata": {
                    "percentPercentile99": "3.60",
                    "volumeInBillions": "1800",
                },
            }
        ],
    )
    old = begin_ingestion("ny-fed-markets", result.dataset)
    begin_ingestion("ny-fed-markets", result.dataset)

    with pytest.raises(ValueError, match="superseded"):
        _store_subsurface_ny_fed_observations(result, old.source, old)

    assert not Observation.objects.filter(batch_id=old.batch_id).exists()
    assert not RawArtifact.objects.filter(run=old).exists()
