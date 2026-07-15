from __future__ import annotations

import hashlib
import html
import json
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from django.db import connection
from django.db.models.query import QuerySet
from django.test.utils import CaptureQueriesContext

from research.data_catalog import DATA_REQUIREMENTS
from research.employment_contract import (
    EMPLOYMENT_BLS_REQUEST_SERIES,
    EMPLOYMENT_CONTRACT_VERSION,
    EMPLOYMENT_REQUIRED_CHART_KEYS,
    EMPLOYMENT_REQUIRED_METRIC_KEYS,
    EMPLOYMENT_REQUIRED_SECTION_KEYS,
    _is_employment_bls_dataset,
    coordinate_employment_dashboard,
    publish_employment_revision,
    select_public_employment_snapshot,
)
from research.labor_official import (
    CONTINUED_4WK,
    CONTINUED_SA,
    CURRENT_RELEASE_URL,
    HISTORY_URL,
    INITIAL_4WK,
    INITIAL_SA,
    IUR_SA,
    DOLWeeklyClaimsProvider,
    parse_weekly_claims_history_xml,
    parse_weekly_claims_release_text,
)
from research.models import (
    DashboardSnapshot,
    IngestionRun,
    MetricSnapshot,
    Observation,
    RawArtifact,
)
from research.official_data import (
    BLS_SERIES,
    ECONOMY_COMPONENTS,
    _economy_component_payload,
    _employment_page_is_buildable,
    _publish_dashboard,
    _publish_dashboard_core,
    _store_bls_observations_v2,
    _store_dol_claims_observations_v2,
    publish_official_dashboards,
)
from research.providers import BLSProvider, ProviderResult
from research.raw_evidence import parse_evidence_bundle
from research.services import record_provider_result, store_series_observations


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def _history_xml(*, year: int = 2026, duplicate: bool = False) -> bytes:
    first_of_june = date(year, 6, 1)
    week_date = first_of_june + timedelta(days=(5 - first_of_june.weekday()) % 7 + 7)
    future_date = week_date + timedelta(days=7)
    week = f"""
    <week>
      <weekEnded>{week_date:%m/%d/%Y}</weekEnded>
      <InitialClaims><NSA>220,642</NSA><SF>97.0</SF><SA>227,000</SA><SA4WK>223,500</SA4WK></InitialClaims>
      <ContinuedClaims><NSA>1,720,948</NSA><SF>95.0</SF><SA>1,812,000</SA><SA4WK>1,792,250</SA4WK></ContinuedClaims>
      <IUR><NSA>1.1</NSA><SA>1.2</SA></IUR>
      <CoveredEmployment>153,547,535</CoveredEmployment>
    </week>
    """
    future = f"""
    <week>
      <weekEnded>{future_date:%m/%d/%Y}</weekEnded>
      <InitialClaims><NSA>&#160;</NSA><SF>96.3</SF><SA>&#160;</SA><SA4WK>&#160;</SA4WK></InitialClaims>
      <ContinuedClaims><NSA>&#160;</NSA><SF>96.8</SF><SA>&#160;</SA><SA4WK>&#160;</SA4WK></ContinuedClaims>
      <IUR><NSA>&#160;</NSA><SA>&#160;</SA></IUR>
      <CoveredEmployment>&#160;</CoveredEmployment>
    </week>
    """
    return (
        f'<r539cyNational rundate="7/12/2026">{week}{week if duplicate else ""}{future}'
        "</r539cyNational>"
    ).encode()


def _format_thousands(value: Decimal) -> str:
    if value == value.to_integral():
        return f"{value:.0f}"
    return f"{value:.2f}"


def _release_text(*, corrupt_latest_average: bool = False) -> str:
    latest = date(2026, 7, 4)
    first = latest - timedelta(weeks=44)
    initial_values = [Decimal("200") + index for index in range(45)]
    continued_values = [Decimal("1800") + index for index in range(44)]
    lines = [
        "TRANSMISSION OF MATERIALS IN THIS RELEASE IS EMBARGOED UNTIL",
        "8:30 A.M. (Eastern) Thursday, July 9, 2026",
        "Seasonally Adjusted US Weekly UI Claims (in thousands)",
    ]
    for index, initial in enumerate(initial_values):
        week = first + timedelta(weeks=index)
        initial_average = (
            sum(initial_values[index - 3 : index + 1], Decimal("0")) / Decimal("4")
            if index >= 3
            else initial
        )
        if corrupt_latest_average and index == len(initial_values) - 1:
            initial_average += Decimal("1")
        prefix = (
            f"{week:%B} {week.day}, {week.year} {initial:.0f} 0 "
            f"{_format_thousands(initial_average)}"
        )
        if index == len(initial_values) - 1:
            lines.append(prefix)
            continue
        continued = continued_values[index]
        continued_average = (
            sum(continued_values[index - 3 : index + 1], Decimal("0")) / Decimal("4")
            if index >= 3
            else continued
        )
        lines.append(f"{prefix} {continued:.0f} 0 {_format_thousands(continued_average)} 1.2")
    lines.append("INITIAL CLAIMS FILED DURING WEEK ENDED")
    return "\n".join(lines)


def test_dol_history_parser_skips_future_nbsp_and_rejects_duplicates():
    records, metadata = parse_weekly_claims_history_xml(_history_xml())

    assert len(records) == 5
    assert metadata == {
        "xml_run_date": "2026-07-12",
        "history_latest_week": "2026-06-13",
        "future_rows_skipped": 1,
    }
    values = {item["series_id"]: item["value"] for item in records}
    assert values[INITIAL_SA] == Decimal("227000")
    assert values[CONTINUED_4WK] == Decimal("1792250")
    with pytest.raises(ValueError, match="duplicate DOL week"):
        parse_weekly_claims_history_xml(_history_xml(duplicate=True))


def test_dol_release_text_parses_advance_tail_and_validates_four_week_average():
    records, metadata = parse_weekly_claims_release_text(_release_text())

    assert metadata["release_date"] == "2026-07-09"
    assert metadata["release_initial_week"] == "2026-07-04"
    assert metadata["release_continued_week"] == "2026-06-27"
    assert metadata["archive_url"] == "https://oui.doleta.gov/press/2026/070926.pdf"
    latest_initial = next(
        item for item in records if item["series_id"] == INITIAL_SA and item["date"] == "2026-07-04"
    )
    latest_continued = next(
        item
        for item in records
        if item["series_id"] == CONTINUED_SA and item["date"] == "2026-06-27"
    )
    assert latest_initial["value"] == Decimal("244000")
    assert latest_initial["quality_status"] == "estimated"
    assert latest_initial["metadata"]["estimate_status"] == "advance"
    assert latest_continued["value"] == Decimal("1843000")
    assert latest_continued["metadata"]["measure_semantics"].startswith("continued weeks claimed")
    with pytest.raises(ValueError, match="four-week average mismatch"):
        parse_weekly_claims_release_text(_release_text(corrupt_latest_average=True))


@pytest.mark.django_db
def test_dol_provider_posts_exact_form_merges_pdf_and_stores_artifacts(
    monkeypatch,
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    parsed_release = parse_weekly_claims_release_text(_release_text())
    fake_pdf = b"%PDF-fixture"
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        if str(request.url) == HISTORY_URL:
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            assert form["strtdate"] == form["enddate"]
            assert {key: value for key, value in form.items() if key != "enddate"} == {
                "level": ["us"],
                "strtdate": form["enddate"],
                "filetype": ["xml"],
            }
            return httpx.Response(200, content=_history_xml(year=int(form["strtdate"][0])))
        if str(request.url) in {
            CURRENT_RELEASE_URL,
            "https://oui.doleta.gov/press/2026/070926.pdf",
        }:
            return httpx.Response(200, content=fake_pdf)
        raise AssertionError(str(request.url))

    monkeypatch.setattr(
        "research.labor_official.parse_weekly_claims_release_pdf",
        lambda _: parsed_release,
    )
    provider = DOLWeeklyClaimsProvider(client=_client(handler))
    result = provider.weekly_claims(start_year=2021, end_year=2026)

    assert result.ok
    assert result.metadata["history_latest_week"] == "2026-06-13"
    assert result.metadata["release_initial_week"] == "2026-07-04"
    assert len(result.metadata["artifacts"]) == 7
    evidence = parse_evidence_bundle(
        result.raw_bytes,
        expected_provider="dol-eta-ui",
        expected_dataset="national-weekly-claims",
    )
    assert len(evidence.responses) == 8
    assert len(evidence.manifest["blobs"]) == 7
    assert evidence.responses["current-release-pdf"] == fake_pdf
    assert evidence.responses["archive-release-pdf"] == fake_pdf
    replay_records, replay_metadata = DOLWeeklyClaimsProvider.replay_evidence_bundle(
        result.raw_bytes
    )
    assert replay_records == result.records
    assert replay_metadata["requested_start_year"] == 2021
    assert replay_metadata["requested_end_year"] == 2026
    assert calls == [
        ("POST", HISTORY_URL),
        ("POST", HISTORY_URL),
        ("POST", HISTORY_URL),
        ("POST", HISTORY_URL),
        ("POST", HISTORY_URL),
        ("POST", HISTORY_URL),
        ("GET", CURRENT_RELEASE_URL),
        ("GET", "https://oui.doleta.gov/press/2026/070926.pdf"),
    ]

    run = record_provider_result(result, persist=_store_dol_claims_observations_v2)
    assert run.status == "success"
    assert RawArtifact.objects.filter(run=run).count() == 1
    artifact = RawArtifact.objects.get(run=run)
    assert artifact.uri.startswith("private://dol-eta-ui/")
    artifact_path = (
        Path(settings.RAW_ARTIFACT_ROOT) / artifact.sha256[:2] / f"{artifact.sha256}.bin"
    )
    assert artifact_path.read_bytes() == result.raw_bytes
    latest = Observation.objects.get(
        series__key=INITIAL_SA.lower(),
        value_date__date=date(2026, 7, 4),
    )
    assert latest.value == Decimal("244000")
    assert latest.quality_status == "estimated"
    assert latest.batch_id == run.batch_id


def _employment_runs():
    fetched_at = datetime(2026, 7, 9, 14, tzinfo=UTC)
    start = date(2025, 3, 1)
    monthly_records = []
    for index in range(16):
        year = start.year + (start.month - 1 + index) // 12
        month = (start.month - 1 + index) % 12 + 1
        period = date(year, month, 1).isoformat()
        values = {
            "CES0000000001": Decimal("150000") + Decimal(index * 100),
            "CES0500000003": Decimal("30") + Decimal(index) / Decimal("10"),
            "LNS14000000": Decimal("4.0") + Decimal(index) / Decimal("100"),
            "LNS11300000": Decimal("62.0") + Decimal(index) / Decimal("100"),
            "JTS000000000000000JOL": Decimal("8000") - Decimal(index * 10),
            "JTS000000000000000JOR": Decimal("4.8"),
            "JTS000000000000000HIL": Decimal("6000"),
            "JTS000000000000000HIR": Decimal("3.8"),
            "JTS000000000000000QUL": Decimal("3200"),
            "JTS000000000000000QUR": Decimal("2.0"),
            "JTS000000000000000LDL": Decimal("1800"),
            "JTS000000000000000LDR": Decimal("1.1"),
            "CUSR0000SA0": Decimal("320"),
            "CUUR0000SA0": Decimal("321"),
            "CUSR0000SA0L1E": Decimal("325"),
            "CUUR0000SA0L1E": Decimal("326"),
            "WPSFD4": Decimal("260"),
            "WPUFD4": Decimal("261"),
        }
        for series_id, value in values.items():
            monthly_records.append(
                {
                    "series_id": series_id,
                    "date": period,
                    "value": value,
                    "quality_status": "estimated" if index == 15 else "fresh",
                    "metadata": {
                        "preliminary": index == 15,
                        "footnotes": (
                            [{"code": "P", "text": "preliminary"}] if index == 15 else []
                        ),
                    },
                }
            )
    bls_run = record_provider_result(
        ProviderResult(
            provider="bls",
            dataset="employment-fixture",
            fetched_at=fetched_at,
            records=monthly_records,
            metadata={
                "requested_series": list(BLS_SERIES),
                "missing_series": [],
                "quality_status": "complete",
            },
        ),
        persist=store_series_observations,
    )

    weekly_records = []
    latest_initial = date(2026, 7, 4)
    first_week = latest_initial - timedelta(weeks=79)
    for index in range(80):
        week = first_week + timedelta(weeks=index)
        common = {
            "source_revision_date": "2026-07-09",
            "release_freshness_days": 8,
            "estimate_status": "advance" if index >= 78 else "revised",
        }
        weekly_records.extend(
            (
                {
                    "series_id": INITIAL_SA,
                    "date": week.isoformat(),
                    "value": Decimal("210000") + index,
                    "quality_status": "estimated" if index == 79 else "fresh",
                    "metadata": common,
                },
                {
                    "series_id": INITIAL_4WK,
                    "date": week.isoformat(),
                    "value": Decimal("211000") + index,
                    "quality_status": "estimated" if index == 79 else "fresh",
                    "metadata": common,
                },
            )
        )
        if index == 79:
            continue
        for series_id, value in (
            (CONTINUED_SA, Decimal("1800000") + index),
            (CONTINUED_4WK, Decimal("1801000") + index),
            (IUR_SA, Decimal("1.2")),
        ):
            weekly_records.append(
                {
                    "series_id": series_id,
                    "date": week.isoformat(),
                    "value": value,
                    "quality_status": "estimated" if index == 78 else "fresh",
                    "metadata": common,
                }
            )
    dol_run = record_provider_result(
        ProviderResult(
            provider="dol-eta-ui",
            dataset="claims-fixture",
            fetched_at=fetched_at,
            records=weekly_records,
            metadata={"quality_status": "complete"},
        ),
        persist=store_series_observations,
    )
    return bls_run, dol_run


def _strict_employment_runs(
    monkeypatch,
    settings,
    tmp_path,
    *,
    cycle="employment-cycle-2026-07-09",
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    fetched_at = datetime(2026, 7, 9, 14, tzinfo=UTC)
    start = date(2025, 3, 1)
    response_series = []
    for series_id in BLS_SERIES:
        points = []
        for index in range(16):
            year = start.year + (start.month - 1 + index) // 12
            month = (start.month - 1 + index) % 12 + 1
            values = {
                "CES0000000001": Decimal("150000") + Decimal(index * 100),
                "CES0500000003": Decimal("30") + Decimal(index) / Decimal("10"),
                "LNS14000000": Decimal("4.0") + Decimal(index) / Decimal("100"),
                "LNS11300000": Decimal("62.0") + Decimal(index) / Decimal("100"),
                "JTS000000000000000JOL": Decimal("8000") - Decimal(index * 10),
                "JTS000000000000000JOR": Decimal("4.8"),
                "JTS000000000000000HIL": Decimal("6000"),
                "JTS000000000000000HIR": Decimal("3.8"),
                "JTS000000000000000QUL": Decimal("3200"),
                "JTS000000000000000QUR": Decimal("2.0"),
                "JTS000000000000000LDL": Decimal("1800"),
                "JTS000000000000000LDR": Decimal("1.1"),
            }
            value = values.get(series_id, Decimal("200") + Decimal(index))
            preliminary = index == 15
            points.append(
                {
                    "year": str(year),
                    "period": f"M{month:02d}",
                    "periodName": str(month),
                    "value": str(value),
                    "latest": "true" if preliminary else "false",
                    "footnotes": ([{"code": "P", "text": "preliminary"}] if preliminary else [{}]),
                }
            )
        response_series.append({"seriesID": series_id, "data": points})
    raw_json = json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "message": [],
            "Results": {"series": response_series},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    bls_records, replay = BLSProvider.parse_series_json_bytes(
        raw_json,
        series_ids=BLS_SERIES,
        start_year=2025,
        end_year=2026,
        fetched_at=fetched_at,
    )
    bls_result = ProviderResult(
        provider="bls",
        dataset="series:" + ",".join(BLS_SERIES),
        fetched_at=fetched_at,
        records=bls_records,
        metadata={
            **replay,
            "endpoint": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            "content_type": "application/json",
            "byte_length": len(raw_json),
            "sha256": hashlib.sha256(raw_json).hexdigest(),
            "request_witness": {
                "series_ids": list(BLS_SERIES),
                "start_year": 2025,
                "end_year": 2026,
            },
            "refresh_cycle_id": cycle,
        },
        raw_bytes=raw_json,
    )
    bls_run = record_provider_result(
        bls_result,
        persist=_store_bls_observations_v2,
    )
    assert bls_run.status == "success"

    parsed_release = parse_weekly_claims_release_text(_release_text())
    fake_pdf = b"%PDF-employment-contract-fixture"

    def handler(request):
        if str(request.url) == HISTORY_URL:
            form = parse_qs(request.content.decode())
            return httpx.Response(
                200,
                content=_history_xml(year=int(form["strtdate"][0])),
            )
        if str(request.url) in {
            CURRENT_RELEASE_URL,
            "https://oui.doleta.gov/press/2026/070926.pdf",
        }:
            return httpx.Response(200, content=fake_pdf)
        raise AssertionError(str(request.url))

    monkeypatch.setattr(
        "research.labor_official.parse_weekly_claims_release_pdf",
        lambda _: parsed_release,
    )
    provider = DOLWeeklyClaimsProvider(client=_client(handler))
    try:
        dol_result = provider.weekly_claims(start_year=2021, end_year=2026)
    finally:
        provider.close()
    dol_result.metadata = {
        **dol_result.metadata,
        "refresh_cycle_id": cycle,
    }
    dol_run = record_provider_result(
        dol_result,
        persist=_store_dol_claims_observations_v2,
    )
    assert dol_run.status == "success"
    return bls_run, dol_run


def test_generic_publishers_reject_employment_contract():
    kwargs = {
        "key": "employment",
        "title": "rogue",
        "summary": "rogue",
        "metrics": [],
        "batch_id": uuid.uuid4(),
    }
    with pytest.raises(ValueError, match="dedicated macro v2"):
        _publish_dashboard(**kwargs)
    with pytest.raises(ValueError, match="dedicated macro v2"):
        _publish_dashboard_core(**kwargs)


@pytest.mark.django_db
def test_employment_publication_derives_exact_metrics_and_preserves_lineage(
    client,
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)

    snapshot = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert snapshot is not None
    assert publish_employment_revision(bls_run=bls_run, dol_run=dol_run) is None
    assert publish_official_dashboards(keys={"employment"}) == []
    assert snapshot.data["contract_version"] == EMPLOYMENT_CONTRACT_VERSION
    assert {item["key"] for item in snapshot.data["metrics"]} == set(
        EMPLOYMENT_REQUIRED_METRIC_KEYS
    )
    assert {item["key"] for item in snapshot.data["charts"]} == set(EMPLOYMENT_REQUIRED_CHART_KEYS)
    assert {item["key"] for item in snapshot.data["sections"]} == set(
        EMPLOYMENT_REQUIRED_SECTION_KEYS
    )
    assert {item["role"] for item in snapshot.data["input_runs"]} == {
        "bls",
        "dol",
    }
    selected = select_public_employment_snapshot()
    assert selected is not None
    assert selected.pk == snapshot.pk
    assert selected.employment_publication_state == "current_candidate"

    component = _economy_component_payload(
        "employment",
        ECONOMY_COMPONENTS["employment"],
        now=datetime(2026, 7, 15, 12, tzinfo=UTC),
    )
    assert not isinstance(component, dict)
    assert component[0]["key"] == "lns14000000"
    assert component[2]["snapshot_id"] == snapshot.pk

    metrics = {item["key"]: item for item in snapshot.data["metrics"]}
    assert metrics["nonfarm-payroll-change"]["value"] == 100.0
    assert metrics["nonfarm-payroll-change-3m"]["value"] == 100.0
    assert metrics["average-hourly-earnings-yoy"]["value"] == pytest.approx(
        float((Decimal("31.5") / Decimal("30.3") - 1) * 100)
    )
    assert metrics["nonfarm-payroll-change"]["metadata"]["input_series"] == ["ces0000000001"]
    assert set(snapshot.data["source_keys"]) == {"bls", "dol-eta-ui", "internal"}
    assert {item["key"] for item in snapshot.data["charts"]} == {
        "payroll-change",
        "average-hourly-earnings-yoy",
        "labor-slack",
        "jolts-rates",
        "initial-claims",
        "continued-claims",
    }
    stored_metric = MetricSnapshot.objects.get(
        key="employment-nonfarm-payroll-change",
        batch_id=snapshot.batch_id,
    )
    assert stored_metric.metadata["input_series"] == ["ces0000000001"]
    assert stored_metric.metadata["input_batch_ids"]

    response = client.get("/economy/employment/", {"period": "1y", "tab": "claims"})
    assert response.status_code == 200
    assert response.context["selected_period"] == "1y"
    assert response.context["selected_tab"] == "claims"
    assert [item["key"] for item in response.context["charts"]] == [
        "initial-claims",
        "continued-claims",
    ]
    assert len(response.context["charts"][0]["data"]) == 45
    body = html.unescape(response.content.decode())
    assert "period=3y&tab=claims" in body or "tab=claims&period=3y" in body
    assert "period=1y&tab=turnover" in body or "tab=turnover&period=1y" in body
    assert "U.S. Department of Labor, Employment and Training Administration" in body

    invalid = client.get(
        "/economy/employment/",
        {"period": "<script>alert(1)</script>", "tab": "missing"},
    )
    assert invalid.status_code == 200
    assert invalid.context["selected_period"] == "3y"
    assert invalid.context["selected_tab"] == "overview"
    assert "<script>alert(1)</script>" not in invalid.content.decode()
    assert "alert%281%29" not in invalid.content.decode()


@pytest.mark.django_db
def test_employment_failed_source_keeps_last_snapshot_and_marks_it_stale(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None
    failure = ProviderResult.failure(
        "dol-eta-ui",
        "national-weekly-claims",
        "upstream fixture failure",
    )
    failure.metadata = {
        **failure.metadata,
        "refresh_cycle_id": "employment-cycle-failed",
    }
    failed_dol = record_provider_result(failure)

    in_flight = select_public_employment_snapshot()
    assert in_flight is not None
    assert in_flight.employment_publication_state == "transition_pending"

    dashboards, stale = coordinate_employment_dashboard([bls_run, failed_dol])
    assert dashboards == []
    assert stale == {"employment"}
    published.refresh_from_db()
    assert published.quality_status == "stale"
    attempts = published.data["refresh_failure"]["attempts"]
    assert attempts["bls"]["ingestion_run_id"] == bls_run.pk
    assert attempts["dol"]["ingestion_run_id"] == failed_dol.pk
    assert attempts["dol"]["status"] == "failed"
    selected = select_public_employment_snapshot()
    assert selected is not None
    assert selected.pk == published.pk
    assert selected.employment_publication_state == "retained_failure"
    assert DashboardSnapshot.objects.filter(key="employment").count() == 1
    assert dol_run.status == "success"

    IngestionRun.objects.create(
        source=bls_run.source,
        dataset=bls_run.dataset,
        started_at=datetime.now(UTC),
        status=IngestionRun.Status.RUNNING,
        metadata={"refresh_cycle_id": "employment-cycle-recovery"},
    )
    recovering = select_public_employment_snapshot()
    assert recovering is not None
    assert recovering.employment_publication_state == "transition_pending"


@pytest.mark.django_db
def test_employment_same_cycle_new_run_pair_creates_append_only_revision(
    monkeypatch,
    settings,
    tmp_path,
):
    first_bls, first_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-cycle-retry",
    )
    first = publish_employment_revision(bls_run=first_bls, dol_run=first_dol)
    assert first is not None
    first_metric_ids = set(
        MetricSnapshot.objects.filter(batch_id=first.batch_id).values_list("id", flat=True)
    )

    second_bls, second_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-cycle-retry",
    )
    second = publish_employment_revision(bls_run=second_bls, dol_run=second_dol)

    assert second is not None
    assert second.pk != first.pk
    assert second.data["fingerprint"] != first.data["fingerprint"]
    assert DashboardSnapshot.objects.filter(key="employment").count() == 2
    assert (
        set(MetricSnapshot.objects.filter(batch_id=first.batch_id).values_list("id", flat=True))
        == first_metric_ids
    )
    selected = select_public_employment_snapshot()
    assert selected is not None
    assert selected.pk == second.pk


@pytest.mark.django_db
def test_employment_publisher_locks_observations_without_nullable_joins(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    original = QuerySet.select_for_update
    lock_models = []
    joined_lock_calls = []
    observation_lock_shapes = []

    def recording_select_for_update(queryset, *args, **kwargs):
        lock_models.append(queryset.model)
        if queryset.model in {IngestionRun, DashboardSnapshot}:
            joined_lock_calls.append((queryset.model, kwargs.get("of")))
        if queryset.model is Observation:
            observation_lock_shapes.append(queryset.query.select_related)
        return original(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", recording_select_for_update)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None
    failure_at = datetime.now(UTC)
    IngestionRun.objects.create(
        source=dol_run.source,
        dataset=dol_run.dataset,
        started_at=failure_at,
        completed_at=failure_at,
        status=IngestionRun.Status.FAILED,
        error="lock-path fixture",
        metadata={"refresh_cycle_id": "lock-path-fixture"},
    )
    coordinate_employment_dashboard()

    assert lock_models.index(bls_run.source.__class__) < lock_models.index(IngestionRun)
    assert joined_lock_calls
    assert all(of == ("self",) for _model, of in joined_lock_calls)
    assert observation_lock_shapes == [False, False]


def test_employment_bls_dataset_identity_is_exact_and_ordered():
    canonical = "series:" + ",".join(EMPLOYMENT_BLS_REQUEST_SERIES)
    assert len(EMPLOYMENT_BLS_REQUEST_SERIES) == 24
    assert _is_employment_bls_dataset(canonical)
    assert not _is_employment_bls_dataset(
        "series:" + ",".join(reversed(EMPLOYMENT_BLS_REQUEST_SERIES))
    )
    assert not _is_employment_bls_dataset("series:" + ",".join(EMPLOYMENT_BLS_REQUEST_SERIES[:-1]))
    assert not _is_employment_bls_dataset(canonical + ",EXTRA")


@pytest.mark.django_db
def test_employment_selector_ignores_newer_unrelated_bls_dataset_attempts(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None

    IngestionRun.objects.bulk_create(
        [
            IngestionRun(
                source=bls_run.source,
                dataset=f"series:UNRELATED-{index:03d}",
                started_at=bls_run.started_at + timedelta(microseconds=index + 1),
                completed_at=bls_run.started_at + timedelta(microseconds=index + 1),
                status=IngestionRun.Status.FAILED,
                error="unrelated BLS dataset fixture",
            )
            for index in range(101)
        ]
    )

    selected = select_public_employment_snapshot()
    assert selected is not None
    assert selected.pk == published.pk
    assert selected.employment_publication_state == "current_candidate"


@pytest.mark.django_db
def test_employment_selector_ignores_rogue_then_rejects_raw_tamper(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None
    rogue_data = deepcopy(published.data)
    rogue_data["unexpected"] = True
    DashboardSnapshot.objects.create(
        key="employment",
        title=published.title,
        summary=published.summary,
        as_of=published.as_of,
        batch_id=uuid.uuid4(),
        quality_status=published.quality_status,
        data=rogue_data,
        source=published.source,
        is_published=True,
    )
    selected = select_public_employment_snapshot()
    assert selected is not None
    assert selected.pk == published.pk

    artifact = RawArtifact.objects.get(run=bls_run)
    artifact_path = (
        Path(settings.RAW_ARTIFACT_ROOT) / artifact.sha256[:2] / f"{artifact.sha256}.bin"
    )
    artifact_path.write_bytes(b"tampered")
    assert select_public_employment_snapshot() is None


@pytest.mark.parametrize(
    "malformed_container",
    ["metrics", "charts", "sections", "component_roles"],
)
@pytest.mark.django_db
def test_employment_selector_skips_malformed_strict_looking_containers(
    client,
    monkeypatch,
    malformed_container,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None
    rogue_batch = uuid.uuid4()
    rogue_data = deepcopy(published.data)
    rogue_data["publication_batch_id"] = str(rogue_batch)
    if malformed_container == "component_roles":
        rogue_data[malformed_container] = {
            key: None for key in published.data[malformed_container]
        }
    else:
        rogue_data[malformed_container] = [
            None for _item in published.data[malformed_container]
        ]
    DashboardSnapshot.objects.create(
        key="employment",
        title=published.title,
        summary=published.summary,
        as_of=published.as_of,
        batch_id=rogue_batch,
        quality_status=published.quality_status,
        data=rogue_data,
        source=published.source,
        is_published=True,
    )

    selected = select_public_employment_snapshot()
    assert selected is not None and selected.pk == published.pk
    assert client.get("/economy/employment/").status_code == 200


@pytest.mark.django_db
def test_employment_natural_expiry_and_running_transition_are_explicit(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None
    future = datetime(2026, 7, 30, 12, tzinfo=UTC)
    monkeypatch.setattr(
        "research.employment_contract.timezone.now",
        lambda: future,
    )
    expired = select_public_employment_snapshot()
    assert expired is not None
    assert expired.employment_publication_state == "natural_expiry"

    IngestionRun.objects.create(
        source=bls_run.source,
        dataset=bls_run.dataset,
        started_at=future - timedelta(minutes=10),
        status=IngestionRun.Status.RUNNING,
        metadata={"refresh_cycle_id": "employment-cycle-running"},
    )
    transition = select_public_employment_snapshot()
    assert transition is not None
    assert transition.employment_publication_state == "transition_pending"


@pytest.mark.django_db
def test_employment_mixed_failure_with_expired_running_attempt_fails_closed(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    published = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert published is not None
    future_now = datetime.now(UTC) + timedelta(hours=4)
    monkeypatch.setattr(
        "research.employment_contract.timezone.now",
        lambda: future_now,
    )
    failed_at = future_now - timedelta(minutes=10)
    IngestionRun.objects.create(
        source=bls_run.source,
        dataset=bls_run.dataset,
        started_at=failed_at,
        completed_at=failed_at,
        status=IngestionRun.Status.FAILED,
        error="mixed transition fixture",
        metadata={"refresh_cycle_id": "employment-mixed-transition"},
    )
    running = IngestionRun.objects.create(
        source=dol_run.source,
        dataset=dol_run.dataset,
        started_at=future_now - timedelta(hours=3),
        status=IngestionRun.Status.RUNNING,
        metadata={"refresh_cycle_id": "employment-mixed-transition"},
    )

    assert select_public_employment_snapshot() is None

    running.started_at = future_now - timedelta(minutes=30)
    running.save(update_fields=["started_at", "updated_at"])
    selected = select_public_employment_snapshot()
    assert selected is not None and selected.pk == published.pk
    assert selected.employment_publication_state == "transition_pending"


@pytest.mark.django_db
def test_employment_expired_success_rolls_back_and_retains_then_recovers(
    monkeypatch,
    settings,
    tmp_path,
):
    first_bls, first_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-baseline",
    )
    baseline = publish_employment_revision(bls_run=first_bls, dol_run=first_dol)
    assert baseline is not None
    second_bls, second_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-expired-success",
    )
    snapshot_count = DashboardSnapshot.objects.filter(key="employment").count()
    metric_count = MetricSnapshot.objects.filter(key__startswith="employment-").count()
    expired_at = datetime.fromisoformat(baseline.data["fresh_until"]) + timedelta(seconds=1)
    monkeypatch.setattr("research.employment_contract.timezone.now", lambda: expired_at)

    dashboards, stale = coordinate_employment_dashboard([second_bls, second_dol])

    assert dashboards == [] and stale == {"employment"}
    assert DashboardSnapshot.objects.filter(key="employment").count() == snapshot_count
    assert MetricSnapshot.objects.filter(key__startswith="employment-").count() == metric_count
    retained = select_public_employment_snapshot()
    assert retained is not None and retained.pk == baseline.pk
    assert retained.employment_publication_state == "retained_failure"
    marker = retained.data["refresh_failure"]
    assert marker["reason_code"] == "publication-postcondition"
    assert marker["attempts"]["bls"]["ingestion_run_id"] == second_bls.pk
    assert marker["attempts"]["dol"]["ingestion_run_id"] == second_dol.pk

    current_now = datetime.now(UTC)
    monkeypatch.setattr("research.employment_contract.timezone.now", lambda: current_now)
    recovered_bls, recovered_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-recovery",
    )
    transitioning = select_public_employment_snapshot()
    assert transitioning is not None
    assert transitioning.employment_publication_state == "transition_pending"
    assert "refresh_failure" not in transitioning.data
    recovered, stale = coordinate_employment_dashboard([recovered_bls, recovered_dol])
    assert len(recovered) == 1 and stale == set()
    assert select_public_employment_snapshot().pk == recovered[0].pk


@pytest.mark.django_db
def test_employment_same_pair_natural_expiry_is_idempotent_and_write_free(
    monkeypatch,
    settings,
    tmp_path,
):
    bls_run, dol_run = _strict_employment_runs(monkeypatch, settings, tmp_path)
    baseline = publish_employment_revision(bls_run=bls_run, dol_run=dol_run)
    assert baseline is not None
    stored_data = deepcopy(baseline.data)
    stored_updated_at = baseline.updated_at
    snapshot_count = DashboardSnapshot.objects.filter(key="employment").count()
    metric_count = MetricSnapshot.objects.filter(key__startswith="employment-").count()
    expired_at = datetime.fromisoformat(baseline.data["fresh_until"]) + timedelta(seconds=1)
    monkeypatch.setattr("research.employment_contract.timezone.now", lambda: expired_at)

    with CaptureQueriesContext(connection) as captured:
        dashboards, stale = coordinate_employment_dashboard([bls_run, dol_run])

    assert dashboards == [] and stale == {"employment"}
    write_queries = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert write_queries == []
    assert DashboardSnapshot.objects.filter(key="employment").count() == snapshot_count
    assert MetricSnapshot.objects.filter(key__startswith="employment-").count() == metric_count
    baseline.refresh_from_db()
    assert baseline.data == stored_data
    assert baseline.updated_at == stored_updated_at
    selected = select_public_employment_snapshot()
    assert selected is not None
    assert selected.employment_publication_state == "natural_expiry"
    assert "refresh_failure" not in selected.data


@pytest.mark.django_db
def test_employment_builder_runtime_rolls_back_retains_and_recovers(
    monkeypatch,
    settings,
    tmp_path,
):
    from research import employment_contract

    first_bls, first_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-runtime-baseline",
    )
    baseline = publish_employment_revision(bls_run=first_bls, dol_run=first_dol)
    assert baseline is not None
    second_bls, second_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-runtime-failure",
    )
    original_builder = employment_contract._build_employment_payload

    def fail_new_pair(evidence, **kwargs):
        if evidence.bls.run.pk == second_bls.pk:
            raise RuntimeError("employment builder runtime fixture")
        return original_builder(evidence, **kwargs)

    monkeypatch.setattr(
        employment_contract,
        "_build_employment_payload",
        fail_new_pair,
    )
    snapshot_count = DashboardSnapshot.objects.filter(key="employment").count()
    metric_count = MetricSnapshot.objects.filter(key__startswith="employment-").count()

    dashboards, stale = coordinate_employment_dashboard([second_bls, second_dol])

    assert dashboards == [] and stale == {"employment"}
    assert DashboardSnapshot.objects.filter(key="employment").count() == snapshot_count
    assert MetricSnapshot.objects.filter(key__startswith="employment-").count() == metric_count
    retained = select_public_employment_snapshot()
    assert retained is not None and retained.pk == baseline.pk
    assert retained.employment_publication_state == "retained_failure"
    assert retained.data["refresh_failure"]["reason_code"] == (
        "publication-postcondition"
    )
    assert "employment builder runtime fixture" in retained.data["refresh_failure"][
        "reason"
    ]

    monkeypatch.setattr(
        employment_contract,
        "_build_employment_payload",
        original_builder,
    )
    recovered_bls, recovered_dol = _strict_employment_runs(
        monkeypatch,
        settings,
        tmp_path,
        cycle="employment-runtime-recovery",
    )
    transitioning = select_public_employment_snapshot()
    assert transitioning is not None
    assert transitioning.employment_publication_state == "transition_pending"
    recovered, stale = coordinate_employment_dashboard([recovered_bls, recovered_dol])
    assert len(recovered) == 1 and stale == set()
    assert select_public_employment_snapshot().pk == recovered[0].pk


@pytest.mark.django_db
def test_employment_derivations_refuse_missing_exact_month_or_zero_denominator():
    _employment_runs()
    missing_month = Observation.objects.get(
        series__key="ces0000000001",
        value_date__date=date(2026, 5, 1),
    )
    missing_month.delete()
    assert not _employment_page_is_buildable()

    Observation.objects.create(
        series=missing_month.series,
        value=missing_month.value,
        value_date=missing_month.value_date,
        as_of=missing_month.as_of,
        fetched_at=missing_month.fetched_at,
        batch_id=missing_month.batch_id,
        source=missing_month.source,
        fallback_source=missing_month.fallback_source,
        quality_status=missing_month.quality_status,
        metadata=missing_month.metadata,
    )
    prior_earnings = Observation.objects.get(
        series__key="ces0500000003",
        value_date__date=date(2025, 6, 1),
    )
    prior_earnings.value = Decimal("0")
    prior_earnings.save(update_fields=["value", "updated_at"])
    assert not _employment_page_is_buildable()


def test_employment_data_catalog_is_explicit_about_live_and_missing_vintages():
    requirements = {item["key"]: item for item in DATA_REQUIREMENTS}

    assert requirements["bls-employment-official"]["status"] == "live"
    assert requirements["bls-jolts-official"]["status"] == "live"
    assert requirements["dol-weekly-claims"]["status"] == "live"
    assert requirements["employment-vintage-trail"]["status"] == "needs_source"
