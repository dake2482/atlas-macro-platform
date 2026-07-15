from __future__ import annotations

import calendar
import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from research import credit_contract, credit_official
from research.credit_contract import (
    CREDIT_CONTRACT_VERSION,
    CREDIT_FORMULA_VERSION,
    CREDIT_OVERVIEW_CHARTS,
    CREDIT_OVERVIEW_METRICS,
    CREDIT_OVERVIEW_SECTION_COLUMNS,
    CREDIT_OVERVIEW_SECTIONS,
    CREDIT_SECTION_COLUMNS,
    CREDIT_SPREAD_CHARTS,
    CREDIT_SPREAD_METRICS,
    CREDIT_SPREAD_SECTION_COLUMNS,
    CREDIT_SPREAD_SECTIONS,
    CREDIT_STRESS_CHARTS,
    CREDIT_STRESS_METRICS,
    CREDIT_STRESS_SECTION_COLUMNS,
    CREDIT_STRESS_SECTIONS,
    HQM_DATASET,
    HQM_SOURCE,
    FederalReserveSLOOSProvider,
    TreasuryHQMProvider,
    coordinate_credit_dashboards,
    credit_child_snapshot_base_is_valid,
    credit_snapshot_base_is_valid,
    persist_credit_official_result,
    select_public_credit_snapshot,
    select_public_credit_spreads_snapshot,
    select_public_credit_stress_snapshot,
)
from research.models import (
    DashboardSnapshot,
    MetricSnapshot,
    Observation,
    RawArtifact,
    SeriesDefinition,
    SourceLicense,
)
from research.official_data import (
    _publish_dashboard,
    _publish_dashboard_core,
    publish_official_dashboards,
)
from research.providers import ProviderResult
from research.services import (
    begin_ingestion,
    ensure_source,
    finish_ingestion,
    record_provider_result,
)


class _Sheet:
    name = "HQM Par Yields"

    def __init__(self, rows):
        self.rows = rows
        self.nrows = len(rows)
        self.ncols = max(map(len, rows))

    def cell_value(self, row, column):
        values = self.rows[row]
        return values[column] if column < len(values) else ""


class _Workbook:
    datemode = 0

    def __init__(self, rows):
        self.sheet = _Sheet(rows)

    def sheet_by_index(self, index):
        assert index == 0
        return self.sheet


def _client(payload: bytes, *, content_type: str):
    return httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=payload, headers={"content-type": content_type}
            )
        ),
    )


def _month_ends(count: int = 121) -> list[date]:
    latest = date(2026, 6, 30)
    result = []
    for offset in range(count - 1, -1, -1):
        month = latest - relativedelta(months=offset)
        result.append(date(month.year, month.month, calendar.monthrange(month.year, month.month)[1]))
    return result


_WORKBOOK_ROWS: dict[bytes, list[list[object]]] = {}


def _hqm_result(monkeypatch, *, marker: bytes = b"exact-hqm-xls"):
    rows = [
        ["Treasury HQM"],
        ["Monthly Average Par Yields, Percent"],
        [],
        ["Date", "", "Maturity", "", "", ""],
        ["", "", "2 Years", "5 Years", "10 Years", "30 Years"],
        [],
    ]
    for index, period in enumerate(_month_ends()):
        rows.append(
            [
                period.strftime("%b %Y"),
                "",
                Decimal("4.00") + Decimal(index) / Decimal("100"),
                Decimal("4.20") + Decimal(index) / Decimal("100"),
                Decimal("4.50") + Decimal(index) / Decimal("100"),
                Decimal("4.80") + Decimal(index) / Decimal("100"),
            ]
        )
    _WORKBOOK_ROWS[marker] = rows
    monkeypatch.setattr(
        credit_official.xlrd,
        "open_workbook",
        lambda *, file_contents, on_demand: _Workbook(_WORKBOOK_ROWS[file_contents]),
    )
    return TreasuryHQMProvider(
        client=_client(marker, content_type="application/vnd.ms-excel")
    ).par_yields()


def _quarter_ends(count: int = 81) -> list[date]:
    latest = date(2026, 6, 30)
    return [latest - relativedelta(months=3 * offset) for offset in range(count - 1, -1, -1)]


def _sloos_result(*, latest: date = date(2026, 6, 30)):
    series_ids = tuple(FederalReserveSLOOSProvider.DEFAULT_SERIES)
    series_xml = []
    for series_index, series_id in enumerate(series_ids):
        observations = "".join(
            (
                f'<frb:Obs OBS_STATUS="A" OBS_VALUE="{series_index + index / 10:.1f}" '
                f'TIME_PERIOD="{period.isoformat()}" />'
            )
            for index, period in enumerate(
                [latest - relativedelta(months=3 * offset) for offset in range(80, -1, -1)]
            )
        )
        series_xml.append(
            f'<kf:Series SERIES_NAME="{series_id}" UNIT="Percent" UNIT_MULT="1">'
            "<frb:Annotations><common:Annotation>"
            "<common:AnnotationType>Short Description</common:AnnotationType>"
            f"<common:AnnotationText>{series_id} fixture</common:AnnotationText>"
            "</common:Annotation></frb:Annotations>"
            f"{observations}</kf:Series>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<message:MessageGroup xmlns:message="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message" '
        'xmlns:common="http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common" '
        'xmlns:frb="http://www.federalreserve.gov/structure/compact/common" '
        'xmlns:kf="http://www.federalreserve.gov/structure/compact/SLOOS_SLOOS">'
        "<message:Header><message:Prepared>2026-07-01T12:00:00</message:Prepared></message:Header>"
        f"<kf:DataSet>{''.join(series_xml)}</kf:DataSet></message:MessageGroup>"
    ).encode()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SLOOS_data.xml", xml)
    return FederalReserveSLOOSProvider(
        client=_client(target.getvalue(), content_type="application/zip")
    ).quarterly_series()


@pytest.fixture
def strict_credit_runs(db, monkeypatch, settings, tmp_path):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "artifacts"
    refresh_id = "credit-cycle-fixture"
    hqm = _hqm_result(monkeypatch)
    sloos = _sloos_result()
    assert hqm.ok and sloos.ok
    fetched_at = timezone.now() - timedelta(seconds=1)
    hqm.fetched_at = fetched_at
    sloos.fetched_at = fetched_at
    hqm.metadata["credit_refresh_id"] = refresh_id
    sloos.metadata["credit_refresh_id"] = refresh_id
    hqm_run = record_provider_result(hqm, persist=persist_credit_official_result)
    sloos_run = record_provider_result(sloos, persist=persist_credit_official_result)
    assert hqm_run.status == "success", hqm_run.error
    assert sloos_run.status == "success", sloos_run.error
    return {"hqm": hqm_run, "sloos": sloos_run}


def _record_same_value_cycle(monkeypatch, refresh_id: str):
    hqm = _hqm_result(monkeypatch)
    sloos = _sloos_result()
    for result in (hqm, sloos):
        result.fetched_at = timezone.now() - timedelta(milliseconds=100)
        result.metadata["credit_refresh_id"] = refresh_id
    return {
        "hqm": record_provider_result(hqm, persist=persist_credit_official_result),
        "sloos": record_provider_result(sloos, persist=persist_credit_official_result),
    }


EXPECTED_CREDIT_SECTION_COLUMNS = {
    "recent-hqm-observations": (
        "date",
        "hqm-2y",
        "hqm-5y",
        "hqm-10y",
        "hqm-30y",
        "quality",
        "batch",
        "artifact",
    ),
    "hqm-source-freshness-methodology": (
        "source-dataset",
        "run-batch",
        "fetched",
        "latest-month",
        "fresh-until",
        "artifact-sha-size",
        "rows",
        "licence",
        "fallback",
    ),
    "licensed-spread-data-gaps": (
        "market-data",
        "status",
        "public-value",
        "provider-guidance",
    ),
    "latest-sloos-survey-table": (
        "metric",
        "value",
        "previous",
        "change-pp",
        "positive-meaning",
        "value-date",
        "quality",
        "batch",
    ),
    "sloos-source-freshness-methodology": (
        "source-dataset",
        "run-batch",
        "file-prepared",
        "fetched",
        "latest-quarter",
        "fresh-until",
        "archive-sha-size",
        "member-sha-size",
        "rows",
        "licence",
        "fallback",
    ),
    "licensed-credit-stress-gaps": (
        "market-data",
        "status",
        "public-value",
        "provider-guidance",
    ),
    "credit-component-ledger": (
        "component",
        "snapshot-batch",
        "payload-hashes",
        "input-run-batch",
        "value-date",
        "fetched",
        "fresh-until",
        "artifact",
        "quality",
        "licence",
        "fallback",
    ),
    "credit-semantic-boundary": (
        "evidence",
        "can-state",
        "cannot-state",
        "status",
        "source",
    ),
    "licensed-credit-market-gaps": (
        "market-data",
        "status",
        "public-value",
        "provider-guidance",
    ),
}


def test_credit_v1_exports_all_nine_exact_section_schemas():
    assert len(CREDIT_SECTION_COLUMNS) == 9
    assert CREDIT_SECTION_COLUMNS == EXPECTED_CREDIT_SECTION_COLUMNS
    assert CREDIT_SPREAD_SECTION_COLUMNS == {
        key: EXPECTED_CREDIT_SECTION_COLUMNS[key] for key in CREDIT_SPREAD_SECTIONS
    }
    assert CREDIT_STRESS_SECTION_COLUMNS == {
        key: EXPECTED_CREDIT_SECTION_COLUMNS[key] for key in CREDIT_STRESS_SECTIONS
    }
    assert CREDIT_OVERVIEW_SECTION_COLUMNS == {
        key: EXPECTED_CREDIT_SECTION_COLUMNS[key] for key in CREDIT_OVERVIEW_SECTIONS
    }


def test_credit_timeline_accepts_long_fetch_to_persistence_delay():
    now = timezone.now()
    assert (
        credit_contract._validate_credit_timeline(
            fetched_at=now - timedelta(hours=2),
            started_at=now - timedelta(minutes=1),
            completed_at=now,
            file_prepared_at=(now - timedelta(hours=3)).isoformat(),
        )
        == (now - timedelta(hours=3)).astimezone(UTC)
    )


def test_credit_timeline_rejects_future_and_impossible_tampering():
    now = timezone.now()
    with pytest.raises(ValueError, match="fetched_at is in the future"):
        credit_contract._validate_credit_timeline(
            fetched_at=now + timedelta(minutes=6)
        )
    with pytest.raises(ValueError, match="started_at is in the future"):
        credit_contract._validate_credit_timeline(
            fetched_at=now,
            started_at=now + timedelta(minutes=6),
        )
    with pytest.raises(ValueError, match="fetched_at is after run started_at"):
        credit_contract._validate_credit_timeline(
            fetched_at=now,
            started_at=now - timedelta(minutes=6),
        )
    with pytest.raises(ValueError, match="completed_at chronology is invalid"):
        credit_contract._validate_credit_timeline(
            fetched_at=now - timedelta(minutes=10),
            started_at=now - timedelta(minutes=2),
            completed_at=now - timedelta(minutes=3),
        )
    with pytest.raises(ValueError, match="completed_at is in the future"):
        credit_contract._validate_credit_timeline(
            fetched_at=now,
            started_at=now,
            completed_at=now + timedelta(minutes=6),
        )
    with pytest.raises(ValueError, match="fetched_at is after run completion"):
        credit_contract._validate_credit_timeline(
            fetched_at=now,
            started_at=now - timedelta(minutes=10),
            completed_at=now - timedelta(minutes=6),
        )
    with pytest.raises(ValueError, match="file_prepared_at is in the future"):
        credit_contract._validate_credit_timeline(
            fetched_at=now,
            file_prepared_at=(now + timedelta(minutes=6)).isoformat(),
        )
    with pytest.raises(ValueError, match="file_prepared_at is after fetched_at"):
        credit_contract._validate_credit_timeline(
            fetched_at=now - timedelta(minutes=2),
            file_prepared_at=(now - timedelta(minutes=1)).isoformat(),
        )


@pytest.mark.django_db
def test_strict_credit_children_and_parent_publish_exact_contract(strict_credit_runs):
    published, unavailable = coordinate_credit_dashboards(strict_credit_runs.values())

    assert unavailable == set()
    assert {item.key for item in published} == {"credit-spreads", "credit-stress", "credit"}
    spread = select_public_credit_spreads_snapshot()
    stress = select_public_credit_stress_snapshot()
    overview = select_public_credit_snapshot()
    assert spread and stress and overview
    assert spread.data["contract_version"] == CREDIT_CONTRACT_VERSION
    assert spread.data["formula_version"] == CREDIT_FORMULA_VERSION
    assert {item["key"] for item in spread.data["metrics"]} == set(CREDIT_SPREAD_METRICS)
    assert {item["key"] for item in spread.data["charts"]} == set(CREDIT_SPREAD_CHARTS)
    assert [item["key"] for item in spread.data["sections"]] == list(CREDIT_SPREAD_SECTIONS)
    assert len(spread.data["charts"][1]["data"]) == 120
    assert len(spread.data["sections"][0]["rows"]) == 24
    curve = next(
        item
        for item in spread.data["charts"]
        if item["key"] == "hqm-latest-par-yield-curve"
    )
    assert curve["x_key"] == "tenor"
    assert curve["time_axis"] == "tenor"
    assert curve["series_keys"] == ["HQM par yield"]
    assert [row["tenor"] for row in curve["data"]] == ["2Y", "5Y", "10Y", "30Y"]
    assert all(
        isinstance(row["HQM par yield"], float) and row["value_date"] == "2026-06-30"
        for row in curve["data"]
    )
    assert {item["key"] for item in stress.data["metrics"]} == set(CREDIT_STRESS_METRICS)
    assert {item["key"] for item in stress.data["charts"]} == set(CREDIT_STRESS_CHARTS)
    assert [item["key"] for item in stress.data["sections"]] == list(CREDIT_STRESS_SECTIONS)
    assert all(len(item["data"]) == 80 for item in stress.data["charts"])
    assert {item["key"] for item in overview.data["metrics"]} == set(CREDIT_OVERVIEW_METRICS)
    assert {item["key"] for item in overview.data["charts"]} == set(CREDIT_OVERVIEW_CHARTS)
    assert [item["key"] for item in overview.data["sections"]] == list(CREDIT_OVERVIEW_SECTIONS)
    for snapshot in (spread, stress, overview):
        for section in snapshot.data["sections"]:
            expected_columns = EXPECTED_CREDIT_SECTION_COLUMNS[section["key"]]
            assert tuple(item["key"] for item in section["columns"]) == expected_columns
            assert all(
                tuple(item["key"] for item in row["cells_list"])
                == expected_columns
                for row in section["rows"]
            )
        assert all(
            "change_unit" not in metric and "change_display" not in metric
            for metric in snapshot.data["metrics"]
        )
    assert credit_child_snapshot_base_is_valid(spread, page_key="credit-spreads")
    assert credit_child_snapshot_base_is_valid(stress, page_key="credit-stress")
    assert credit_snapshot_base_is_valid(overview)
    assert MetricSnapshot.objects.filter(batch_id=spread.batch_id).count() == 4
    assert MetricSnapshot.objects.filter(batch_id=stress.batch_id).count() == 6
    assert MetricSnapshot.objects.filter(batch_id=overview.batch_id).count() == 4
    assert RawArtifact.objects.filter(run__in=strict_credit_runs.values()).count() == 2


@pytest.mark.django_db
def test_credit_coordinator_locks_children_metrics_artifacts_and_observations(
    strict_credit_runs, monkeypatch
):
    locked_models = []
    original_select_for_update = QuerySet.select_for_update

    def recording_select_for_update(queryset, *args, **kwargs):
        locked_models.append(queryset.model)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)

    published, unavailable = coordinate_credit_dashboards(strict_credit_runs.values())

    assert unavailable == set()
    assert {item.key for item in published} == {
        "credit-spreads",
        "credit-stress",
        "credit",
    }
    assert DashboardSnapshot in locked_models
    assert MetricSnapshot in locked_models
    assert RawArtifact in locked_models
    assert Observation in locked_models


@pytest.mark.django_db
def test_credit_locked_input_signature_detects_in_transaction_mutation(
    strict_credit_runs,
):
    run = strict_credit_runs["hqm"]
    with transaction.atomic():
        locked = credit_contract._lock_credit_run_inputs(run)
        row = Observation.objects.filter(batch_id=run.batch_id).order_by("pk").first()
        assert row is not None
        row.value += Decimal("1")
        row.save(update_fields=["value"])
        with pytest.raises(
            ValueError, match="locked raw artifact or observation rows changed"
        ):
            credit_contract._assert_credit_run_inputs_unchanged(run, locked)


@pytest.mark.django_db
def test_success_run_missing_completion_fails_closed(strict_credit_runs):
    coordinate_credit_dashboards(strict_credit_runs.values())
    run = strict_credit_runs["hqm"]
    run.completed_at = None
    run.save(update_fields=["completed_at"])

    assert select_public_credit_spreads_snapshot() is None
    assert select_public_credit_snapshot() is None


@pytest.mark.django_db
def test_selector_rejects_embedded_run_with_future_fetched_at(strict_credit_runs):
    coordinate_credit_dashboards(strict_credit_runs.values())
    run = strict_credit_runs["hqm"]
    metadata = dict(run.metadata)
    metadata["fetched_at"] = (timezone.now() + timedelta(minutes=6)).isoformat()
    run.metadata = metadata
    run.save(update_fields=["metadata"])

    assert select_public_credit_spreads_snapshot() is None
    assert select_public_credit_snapshot() is None


@pytest.mark.django_db
def test_retry_is_idempotent_and_payload_tamper_fails_closed(strict_credit_runs):
    first, unavailable = coordinate_credit_dashboards(strict_credit_runs.values())
    assert not unavailable
    counts = (
        DashboardSnapshot.objects.count(),
        MetricSnapshot.objects.count(),
    )
    second, unavailable = coordinate_credit_dashboards(strict_credit_runs.values())
    assert second == []
    assert unavailable == set()
    assert counts == (DashboardSnapshot.objects.count(), MetricSnapshot.objects.count())

    spread = DashboardSnapshot.objects.get(key="credit-spreads")
    data = dict(spread.data)
    data["metrics"] = [dict(item) for item in data["metrics"]]
    data["metrics"][0]["value"] += 1
    spread.data = data
    spread.save(update_fields=["data"])
    assert select_public_credit_spreads_snapshot() is None


@pytest.mark.django_db
def test_old_unversioned_credit_snapshot_is_rejected(db):

    old = DashboardSnapshot.objects.create(
        key="credit-spreads",
        title="legacy",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        summary="legacy",
        data={"metrics": []},
        source=ensure_source("internal"),
        is_published=True,
    )
    assert select_public_credit_spreads_snapshot([old]) is None


@pytest.mark.django_db
def test_same_values_in_a_new_fresh_run_append_new_immutable_revisions(
    strict_credit_runs, monkeypatch
):
    first, unavailable = coordinate_credit_dashboards(strict_credit_runs.values())
    assert not unavailable
    first_by_key = {item.key: item for item in first}
    second_runs = _record_same_value_cycle(monkeypatch, "credit-cycle-fixture-2")

    second, unavailable = coordinate_credit_dashboards(second_runs.values())

    assert not unavailable
    second_by_key = {item.key: item for item in second}
    assert set(second_by_key) == {"credit-spreads", "credit-stress", "credit"}
    assert DashboardSnapshot.objects.filter(key="credit-spreads").count() == 2
    assert DashboardSnapshot.objects.filter(key="credit-stress").count() == 2
    assert DashboardSnapshot.objects.filter(key="credit").count() == 2
    for key in second_by_key:
        assert first_by_key[key].pk != second_by_key[key].pk
        assert first_by_key[key].batch_id != second_by_key[key].batch_id
        assert (
            first_by_key[key].data["semantic_fingerprint"]
            == second_by_key[key].data["semantic_fingerprint"]
        )
        assert (
            first_by_key[key].data["payload_integrity_hash"]
            != second_by_key[key].data["payload_integrity_hash"]
        )


@pytest.mark.django_db
def test_cross_cycle_children_never_form_a_new_overview(
    strict_credit_runs, monkeypatch
):
    coordinate_credit_dashboards(strict_credit_runs.values())
    original_parent = DashboardSnapshot.objects.get(key="credit")
    next_hqm = _hqm_result(monkeypatch)
    next_hqm.fetched_at = timezone.now() - timedelta(milliseconds=100)
    next_hqm.metadata["credit_refresh_id"] = "hqm-only-cycle"
    hqm_run = record_provider_result(next_hqm, persist=persist_credit_official_result)

    published, unavailable = coordinate_credit_dashboards([hqm_run])

    assert not unavailable
    assert {item.key for item in published} == {"credit-spreads"}
    assert DashboardSnapshot.objects.filter(key="credit").count() == 1
    selected = select_public_credit_snapshot()
    assert selected is not None
    assert selected.pk == original_parent.pk
    assert selected.credit_publication_state == "retained_failure"
    assert selected.data["refresh_failure"]["reason_code"] == "cycle-incomplete"
    assert {
        item["credit_refresh_id"] for item in selected.data["component_snapshots"]
    } == {"credit-cycle-fixture"}


@pytest.mark.django_db
def test_running_successor_is_transition_without_terminal_failure_marker(
    strict_credit_runs,
):
    coordinate_credit_dashboards(strict_credit_runs.values())
    running = begin_ingestion(
        HQM_SOURCE,
        HQM_DATASET,
        metadata={"credit_refresh_id": "running-cycle"},
    )

    spread = select_public_credit_spreads_snapshot()
    overview = select_public_credit_snapshot()

    assert spread is not None and overview is not None
    assert spread.credit_publication_state == "transition_pending"
    assert overview.credit_publication_state == "transition_pending"
    assert "refresh_failure" not in spread.data
    assert "refresh_failure" not in overview.data
    published, unavailable = coordinate_credit_dashboards([running])
    assert published == []
    assert unavailable == set()


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["failed", "partial"])
def test_terminal_incomplete_attempt_retains_revalidated_child_and_parent(
    strict_credit_runs, status
):
    coordinate_credit_dashboards(strict_credit_runs.values())
    successor = begin_ingestion(
        HQM_SOURCE,
        HQM_DATASET,
        metadata={"credit_refresh_id": f"{status}-cycle"},
    )
    finish_ingestion(
        successor,
        status=status,
        row_count=0,
        error="fixture terminal failure" if status == "failed" else "",
    )

    published, unavailable = coordinate_credit_dashboards([successor])
    spread = select_public_credit_spreads_snapshot()
    overview = select_public_credit_snapshot()

    assert published == []
    assert unavailable == set()
    assert spread is not None and overview is not None
    assert spread.credit_publication_state == "retained_failure"
    assert overview.credit_publication_state == "retained_failure"
    assert spread.data["refresh_failure"]["reason_code"] == "latest-attempt-incomplete"
    assert overview.data["refresh_failure"]["reason_code"] == "latest-attempt-incomplete"


@pytest.mark.django_db
def test_zero_row_attempt_without_retained_snapshot_is_loudly_unavailable(db):
    result = ProviderResult(
        provider=HQM_SOURCE,
        dataset=HQM_DATASET,
        records=[],
        metadata={"credit_refresh_id": "zero-cycle"},
    )
    run = record_provider_result(result)
    published, unavailable = credit_contract.coordinate_credit_child(
        "credit-spreads", [run]
    )
    assert published == []
    assert unavailable == {"credit-spreads"}


@pytest.mark.django_db
def test_natural_expiry_is_stale_without_an_ingestion_failure_marker(
    strict_credit_runs, monkeypatch
):
    coordinate_credit_dashboards(strict_credit_runs.values())
    monkeypatch.setattr(
        credit_contract.timezone,
        "now",
        lambda: datetime(2027, 12, 1, tzinfo=UTC),
    )

    spread = select_public_credit_spreads_snapshot()
    stress = select_public_credit_stress_snapshot()
    overview = select_public_credit_snapshot()

    assert spread and stress and overview
    assert spread.credit_publication_state == "natural_expiry"
    assert stress.credit_publication_state == "natural_expiry"
    assert overview.credit_publication_state == "natural_expiry"
    assert all("refresh_failure" not in item.data for item in (spread, stress, overview))
    assert all(item.quality_status == "stale" for item in (spread, stress, overview))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tamper",
    ["artifact", "observation", "metric", "missing", "duplicate", "extra"],
)
def test_child_selector_fails_closed_on_lineage_or_normalized_row_tamper(
    strict_credit_runs, tamper, settings
):
    coordinate_credit_dashboards(strict_credit_runs.values())
    run = strict_credit_runs["hqm"]
    spread = DashboardSnapshot.objects.get(key="credit-spreads")
    if tamper == "artifact":
        artifact = RawArtifact.objects.get(run=run)
        path = settings.RAW_ARTIFACT_ROOT / artifact.sha256[:2] / f"{artifact.sha256}.bin"
        path.write_bytes(b"tampered")
    elif tamper == "observation":
        row = Observation.objects.filter(batch_id=run.batch_id).first()
        assert row is not None
        row.value += Decimal("1")
        row.save(update_fields=["value"])
    elif tamper == "metric":
        row = MetricSnapshot.objects.filter(batch_id=spread.batch_id).first()
        assert row is not None
        row.display_value = "tampered"
        row.save(update_fields=["display_value"])
    elif tamper == "missing":
        row = Observation.objects.filter(batch_id=run.batch_id).first()
        assert row is not None
        row.delete()
    elif tamper == "duplicate":
        row = Observation.objects.filter(batch_id=run.batch_id).first()
        assert row is not None
        Observation.objects.create(
            series=row.series,
            value=row.value,
            value_date=row.value_date,
            as_of=row.as_of,
            fetched_at=row.fetched_at,
            batch_id=row.batch_id,
            source=row.source,
            quality_status=row.quality_status,
            metadata=row.metadata,
        )
    else:
        row = Observation.objects.filter(batch_id=run.batch_id).first()
        assert row is not None and row.series is not None
        extra_series = SeriesDefinition.objects.create(
            key="hqm-extra-series",
            name="Extra",
            source=run.source,
            unit="%",
            frequency="monthly",
        )
        Observation.objects.create(
            series=extra_series,
            value=1,
            value_date=row.value_date,
            as_of=row.as_of,
            fetched_at=row.fetched_at,
            batch_id=run.batch_id,
            source=run.source,
        )
    assert select_public_credit_spreads_snapshot() is None
    assert select_public_credit_snapshot() is None


@pytest.mark.django_db
def test_current_licence_withdrawal_invalidates_children_and_parent(strict_credit_runs):
    coordinate_credit_dashboards(strict_credit_runs.values())
    licence = SourceLicense.objects.get(source__key=HQM_SOURCE, is_current=True)
    licence.public_display_allowed = False
    licence.save(update_fields=["public_display_allowed"])
    assert select_public_credit_spreads_snapshot() is None
    assert select_public_credit_snapshot() is None


@pytest.mark.django_db
def test_generic_and_core_publishers_hard_reject_all_credit_keys(db):
    for key in ("credit", "credit-spreads", "credit-stress"):
        with pytest.raises(ValueError, match="dedicated"):
            publish_official_dashboards(keys={key})
        kwargs = {
            "key": key,
            "title": "forbidden",
            "summary": "forbidden",
            "metrics": [],
            "batch_id": __import__("uuid").uuid4(),
        }
        with pytest.raises(ValueError, match="dedicated"):
            _publish_dashboard(**kwargs)
        with pytest.raises(ValueError, match="dedicated"):
            _publish_dashboard_core(**kwargs)


@pytest.mark.django_db
def test_credit_get_controls_normalize_and_tables_do_not_leak_series_ids(
    strict_credit_runs, client
):
    coordinate_credit_dashboards(strict_credit_runs.values())

    spread = client.get("/credit/spreads/?period=bogus&tab=bogus")
    stress = client.get("/credit/stress/?period=full&tab=score")
    overview = client.get("/credit/?tab=score")

    assert spread.status_code == stress.status_code == overview.status_code == 200
    assert spread.context["selected_period"] == "10y"
    assert spread.context["selected_tab"] == "curve"
    assert stress.context["selected_period"] == "20y"
    assert stress.context["selected_tab"] == "standards"
    assert overview.context["selected_tab"] == "hqm"
    assert "SUBLPDMBS_XWB_N.Q" not in stress.content.decode()
    assert "HQM-PAR-10Y" not in spread.content.decode()
    curve = spread.context["charts"][0]
    assert curve["key"] == "hqm-latest-par-yield-curve"
    assert curve["x_key"] == "tenor"
    assert curve["series_keys"] == ["HQM par yield"]
    assert curve["presentation_x_key"] == "label"
    assert [row["label"] for row in curve["data"]] == ["2Y", "5Y", "10Y", "30Y"]
    assert all(
        set(row) == {"label", "HQM par yield"}
        and isinstance(row["HQM par yield"], float)
        for row in curve["data"]
    )
    assert all(
        metric["change_unit"] == "bp" and metric["change_display"] == "+1.00"
        for metric in spread.context["metrics"]
    )
    assert all(
        metric["change_unit"] == "pp" and metric["change_display"] == "+0.10"
        for metric in stress.context["metrics"]
    )
    spread_html = spread.content.decode()
    stress_html = stress.content.decode()
    assert spread_html.count("<span>bp</span>") == 4
    assert stress_html.count("<span>pp</span>") == 6
    assert "bp bp" not in spread_html
    assert "pp pp" not in stress_html

    valid = client.get("/credit/spreads/?period=3y&tab=history")
    assert valid.context["selected_period"] == "3y"
    assert valid.context["selected_tab"] == "history"
    assert valid.context["charts"][0]["key"] == "hqm-par-yield-history"
