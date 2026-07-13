from __future__ import annotations

import html
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from research.data_catalog import DATA_REQUIREMENTS
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
from research.models import DashboardSnapshot, MetricSnapshot, Observation, RawArtifact
from research.official_data import (
    BLS_SERIES,
    EMPLOYMENT_PUBLICATION_GROUPS,
    _employment_page_is_buildable,
    _keys_with_current_required_batches,
    _mark_latest_dashboards_stale,
    _publishable_keys_for_source_groups,
    _store_series_with_artifacts,
    publish_official_dashboards,
)
from research.providers import ProviderResult
from research.services import record_provider_result, store_series_observations


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )


def _history_xml(*, year: int = 2026, duplicate: bool = False) -> bytes:
    first_of_june = date(year, 6, 1)
    week_date = first_of_june + timedelta(
        days=(5 - first_of_june.weekday()) % 7 + 7
    )
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
            sum(initial_values[index - 3 : index + 1], Decimal("0"))
            / Decimal("4")
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
            sum(continued_values[index - 3 : index + 1], Decimal("0"))
            / Decimal("4")
            if index >= 3
            else continued
        )
        lines.append(
            f"{prefix} {continued:.0f} 0 "
            f"{_format_thousands(continued_average)} 1.2"
        )
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
        item
        for item in records
        if item["series_id"] == INITIAL_SA and item["date"] == "2026-07-04"
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
    assert latest_continued["metadata"]["measure_semantics"].startswith(
        "continued weeks claimed"
    )
    with pytest.raises(ValueError, match="four-week average mismatch"):
        parse_weekly_claims_release_text(
            _release_text(corrupt_latest_average=True)
        )


@pytest.mark.django_db
def test_dol_provider_posts_exact_form_merges_pdf_and_stores_artifacts(monkeypatch):
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
            return httpx.Response(
                200, content=_history_xml(year=int(form["strtdate"][0]))
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
    result = provider.weekly_claims(start_year=2021, end_year=2026)

    assert result.ok
    assert result.metadata["history_latest_week"] == "2026-06-13"
    assert result.metadata["release_initial_week"] == "2026-07-04"
    assert len(result.metadata["artifacts"]) == 7
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

    run = record_provider_result(result, persist=_store_series_with_artifacts)
    assert run.status == "success"
    assert RawArtifact.objects.filter(run=run).count() == 7
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
                        "footnotes": ([{"code": "P", "text": "preliminary"}] if index == 15 else []),
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


@pytest.mark.django_db
def test_employment_publication_derives_exact_metrics_and_preserves_lineage(client):
    bls_run, dol_run = _employment_runs()
    runs = [bls_run, dol_run]

    assert _publishable_keys_for_source_groups(
        runs, EMPLOYMENT_PUBLICATION_GROUPS
    ) == {"employment"}
    assert _keys_with_current_required_batches({"employment"}, runs) == {
        "employment"
    }
    assert _employment_page_is_buildable()
    dashboards = publish_official_dashboards(keys={"employment"})
    assert [item.key for item in dashboards] == ["employment"]

    snapshot = dashboards[0]
    metrics = {item["key"]: item for item in snapshot.data["metrics"]}
    assert metrics["nonfarm-payroll-change"]["value"] == 100.0
    assert metrics["nonfarm-payroll-change-3m"]["value"] == 100.0
    assert metrics["average-hourly-earnings-yoy"]["value"] == pytest.approx(
        float((Decimal("31.5") / Decimal("30.3") - 1) * 100)
    )
    assert metrics["nonfarm-payroll-change"]["metadata"]["input_series"] == [
        "ces0000000001"
    ]
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

    response = client.get(
        "/economy/employment/", {"period": "1y", "tab": "claims"}
    )
    assert response.status_code == 200
    assert response.context["selected_period"] == "1y"
    assert response.context["selected_tab"] == "claims"
    assert [item["key"] for item in response.context["charts"]] == [
        "initial-claims",
        "continued-claims",
    ]
    assert len(response.context["charts"][0]["data"]) == 53
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
def test_employment_failed_source_keeps_last_snapshot_and_marks_it_stale():
    bls_run, dol_run = _employment_runs()
    published = publish_official_dashboards(keys={"employment"})[0]
    failed_dol = record_provider_result(
        ProviderResult.failure(
            "dol-eta-ui",
            "national-weekly-claims",
            "upstream fixture failure",
        )
    )
    current_runs = [bls_run, failed_dol]

    assert _publishable_keys_for_source_groups(
        current_runs, EMPLOYMENT_PUBLICATION_GROUPS
    ) == set()
    _mark_latest_dashboards_stale(
        {"employment"},
        current_runs,
        groups=EMPLOYMENT_PUBLICATION_GROUPS,
    )
    published.refresh_from_db()
    assert published.quality_status == "stale"
    assert published.data["refresh_failure"]["sources"] == [
        {
            "source": "bls",
            "status": "success",
            "row_count": bls_run.row_count,
            "error": "",
        },
        {
            "source": "dol-eta-ui",
            "status": "failed",
            "row_count": 0,
            "error": "upstream fixture failure",
        },
    ]
    assert DashboardSnapshot.objects.filter(key="employment").count() == 1
    assert dol_run.status == "success"


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
