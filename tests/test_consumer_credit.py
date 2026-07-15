from __future__ import annotations

import hashlib
import io
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook

from research.consumer_credit import (
    G19_SERIES,
    FederalReserveG19Provider,
    NYFedHouseholdDebtProvider,
)
from research.models import IngestionRun, Observation, RawArtifact
from research.official_data import _store_consumer_credit_observations_v2
from research.raw_evidence import parse_evidence_bundle
from research.services import record_provider_result


def _g19_csv() -> bytes:
    descriptions = list(G19_SERIES)
    identifiers = [f"G19/TEST/{index}.M" for index in range(len(descriptions))]
    rows = [
        ["Series Description", *descriptions],
        ["Unit:", *("Percent" if "Percent change" in item else "Currency" for item in descriptions)],
        ["Multiplier:", *("1" if "Percent change" in item else "1000000" for item in descriptions)],
        ["Currency:", *("USD" for _ in descriptions)],
        ["Unique Identifier:", *identifiers],
        ["Time Period", *(f"SERIES-{index}" for index in range(len(descriptions)))],
        ["2026-04", "4.87", "10.36", "2.93", "5154721.31", "1349505.63", "3805215.68", "20822.88", "11546.30", "9276.58"],
        ["2026-05", "-0.04", "-4.71", "1.61", "5154538.86", "1344207.79", "3810331.07", "-182.45", "-5297.84", "5115.39"],
    ]
    output = io.StringIO()
    import csv

    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue().encode()


def _g19_page() -> bytes:
    package = (
        "rel=G19&amp;series=test-package&amp;lastObs=&amp;from=&amp;to=&amp;filetype=csv&amp;"
        "label=include&amp;layout=seriescolumn&amp;type=package"
    )
    return (
        '<span id="ReleaseLabel">G.19 - last released Wednesday, July 8, 2026</span>'
        f'<input name="FreqRequest" value="{package}">'
        '<label>Consumer Credit Outstanding (S.A.) [csv]</label>'
    ).encode()


def _g19_client(csv_payload: bytes | None = None) -> httpx.Client:
    payload = csv_payload or _g19_csv()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("Choose.aspx"):
            assert request.url.params["rel"] == "G19"
            return httpx.Response(200, content=_g19_page(), headers={"content-type": "text/html"})
        assert request.url.path.endswith("Output.aspx")
        assert request.url.params["series"] == "test-package"
        return httpx.Response(200, content=payload, headers={"content-type": "text/csv"})

    return httpx.Client(
        base_url="https://www.federalreserve.gov",
        transport=httpx.MockTransport(handler),
    )


def _hhdc_workbook(
    *,
    latest_quarter: str = "26:Q1",
    balance_unit: str = "Trillions of $",
    delinquency_unit: str = "Percent",
) -> bytes:
    workbook = Workbook()
    contents = workbook.active
    contents.title = "TABLE OF CONTENTS"
    contents.cell(2, 2, "QUARTERLY REPORT ON HOUSEHOLD DEBT AND CREDIT")

    balances = workbook.create_sheet("Page 3 Data")
    balances.append(["Total Debt Balance and Its Composition"])
    balances.append([balance_unit])
    balances.append(["Return to Table of Contents"])
    balances.append(
        [None, "Mortgage", "HE Revolving", "Auto Loan", "Credit Card", "Student Loan", "Other", "Total"]
    )
    balances.append(["25:Q4", 13.17, 0.4336, 1.667, 1.277, 1.664, 0.5641, 18.7757])
    balances.append([latest_quarter, 13.191, 0.446, 1.685, 1.252, 1.658, 0.562, 18.794])

    delinquencies = workbook.create_sheet("Page 12 Data")
    delinquencies.append(["Percent of Balance 90+ Days Delinquent by Loan Type"])
    delinquencies.append([delinquency_unit])
    delinquencies.append(["Return to Table of Contents"])
    delinquencies.append(
        [None, "MORTGAGE", "HELOC", "AUTO", "CC", "STUDENT LOAN", "OTHER", "ALL"]
    )
    delinquencies.append(["25:Q4", 0.92, 0.82, 5.21, 12.70, 9.57, 9.52, 3.12])
    delinquencies.append([latest_quarter, 1.09, 0.95, 5.6, 13.12, 10.34, 9.76, 3.36])

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _hhdc_client(workbook: bytes, *, filename_quarter: str = "2026q1") -> httpx.Client:
    page = (
        '<a href="/medialibrary/interactives/householdcredit/data/xls/'
        f'hhd_c_report_{filename_quarter}.xlsx">Data Underlying Report</a>'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("databank.html"):
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        return httpx.Response(
            200,
            content=workbook,
            headers={
                "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            },
        )

    return httpx.Client(
        base_url="https://www.newyorkfed.org",
        transport=httpx.MockTransport(handler),
    )


def test_g19_provider_discovers_package_and_normalizes_full_history():
    payload = _g19_csv()
    result = FederalReserveG19Provider(client=_g19_client(payload)).consumer_credit()

    assert result.ok
    assert result.row_count == 18
    assert result.metadata["release_date"] == "2026-07-08"
    assert result.metadata["latest_value_date"] == "2026-05-01"
    assert result.metadata["artifacts"][1]["sha256"] == hashlib.sha256(payload).hexdigest()
    evidence = parse_evidence_bundle(
        result.raw_bytes,
        expected_provider="federal-reserve-g19",
        expected_dataset="consumer-credit",
    )
    assert set(evidence.responses) == {"choose-page", "output-csv"}
    assert evidence.responses["choose-page"] == _g19_page()
    assert evidence.responses["output-csv"] == payload
    replay_records, replay_metadata = FederalReserveG19Provider.replay_evidence_bundle(
        result.raw_bytes
    )
    assert replay_records == result.records
    assert replay_metadata["release_date"] == "2026-07-08"
    latest = {
        item["series_id"]: item
        for item in result.records
        if item["date"] == "2026-05-01"
    }
    assert latest["G19-CONSUMER-CREDIT-OUTSTANDING-SA"]["value"] == Decimal(
        "5154538.86"
    )
    assert latest["G19-REVOLVING-CREDIT-GROWTH-SAAR"]["value"] == Decimal("-4.71")


def test_g19_provider_fails_closed_when_required_column_is_missing():
    payload = _g19_csv().replace(
        b"Percent change of total revolving consumer credit", b"Removed revolving credit"
    )
    result = FederalReserveG19Provider(client=_g19_client(payload)).consumer_credit()

    assert not result.ok
    assert "required columns missing" in result.error


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (b"Unit:,Percent", b"Unit:,Currency"),
        (b"Multiplier:,1,1,1", b"Multiplier:,2,1,1"),
    ],
)
def test_g19_provider_rejects_wrong_declared_unit_or_multiplier(
    original,
    replacement,
):
    payload = _g19_csv().replace(original, replacement, 1)

    result = FederalReserveG19Provider(
        client=_g19_client(payload)
    ).consumer_credit()

    assert not result.ok
    assert "source unit or multiplier is invalid" in result.error


def test_nyfed_provider_discovers_latest_workbook_and_preserves_attribution():
    payload = _hhdc_workbook()
    result = NYFedHouseholdDebtProvider(
        client=_hhdc_client(payload)
    ).household_debt()

    assert result.ok
    assert result.row_count == 28
    assert result.metadata["latest_value_date"] == "2026-03-31"
    assert result.metadata["attribution"] == "New York Fed Consumer Credit Panel / Equifax"
    assert result.metadata["artifacts"][1]["sha256"] == hashlib.sha256(payload).hexdigest()
    evidence = parse_evidence_bundle(
        result.raw_bytes,
        expected_provider="ny-fed-household-credit",
        expected_dataset="household-debt-credit",
    )
    assert set(evidence.responses) == {
        "databank-page",
        "household-debt-workbook",
    }
    assert evidence.responses["household-debt-workbook"] == payload
    replay_records, replay_metadata = NYFedHouseholdDebtProvider.replay_evidence_bundle(
        result.raw_bytes
    )
    assert replay_records == result.records
    assert replay_metadata["latest_value_date"] == "2026-03-31"
    latest = {
        item["series_id"]: item
        for item in result.records
        if item["date"] == "2026-03-31"
    }
    assert latest["HHDC-TOTAL-DEBT-BALANCE"]["value"] == Decimal("18.794")
    assert latest["HHDC-CREDIT-CARD-90D-DELINQUENT"]["value"] == Decimal("13.12")


def test_nyfed_provider_rejects_filename_and_workbook_period_mismatch():
    result = NYFedHouseholdDebtProvider(
        client=_hhdc_client(_hhdc_workbook(latest_quarter="25:Q4"))
    ).household_debt()

    assert not result.ok
    assert "does not match filename" in result.error


@pytest.mark.parametrize(
    "workbook",
    [
        _hhdc_workbook(balance_unit="USD millions"),
        _hhdc_workbook(delinquency_unit="Basis points"),
    ],
)
def test_nyfed_provider_rejects_wrong_declared_workbook_units(workbook):
    result = NYFedHouseholdDebtProvider(
        client=_hhdc_client(workbook)
    ).household_debt()

    assert not result.ok
    assert "declared unit is invalid" in result.error


@pytest.mark.django_db
def test_consumer_credit_v2_persists_private_append_only_evidence(
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    g19 = FederalReserveG19Provider(client=_g19_client()).consumer_credit()
    household = NYFedHouseholdDebtProvider(
        client=_hhdc_client(_hhdc_workbook())
    ).household_debt()

    g19_run = record_provider_result(
        g19,
        persist=_store_consumer_credit_observations_v2,
    )
    household_run = record_provider_result(
        household,
        persist=_store_consumer_credit_observations_v2,
    )

    assert g19_run.status == IngestionRun.Status.SUCCESS
    assert household_run.status == IngestionRun.Status.SUCCESS
    assert RawArtifact.objects.filter(run=g19_run).count() == 1
    assert RawArtifact.objects.filter(run=household_run).count() == 1
    for run, result in ((g19_run, g19), (household_run, household)):
        artifact = RawArtifact.objects.get(run=run)
        assert artifact.uri.startswith(f"private://{run.source.key}/")
        path = (
            Path(settings.RAW_ARTIFACT_ROOT)
            / artifact.sha256[:2]
            / f"{artifact.sha256}.bin"
        )
        assert path.read_bytes() == result.raw_bytes
        assert Observation.objects.filter(batch_id=run.batch_id).count() == len(
            result.records
        )

    first_g19_rows = list(
        Observation.objects.filter(batch_id=g19_run.batch_id)
        .order_by("pk")
        .values_list("pk", "value", "updated_at")
    )
    repeated_result = FederalReserveG19Provider(
        client=_g19_client()
    ).consumer_credit()
    repeated = record_provider_result(
        repeated_result,
        persist=_store_consumer_credit_observations_v2,
    )
    assert repeated.status == IngestionRun.Status.SUCCESS
    assert repeated.batch_id != g19_run.batch_id
    assert Observation.objects.filter(batch_id=repeated.batch_id).count() == len(
        repeated_result.records
    )
    assert list(
        Observation.objects.filter(batch_id=g19_run.batch_id)
        .order_by("pk")
        .values_list("pk", "value", "updated_at")
    ) == first_g19_rows


@pytest.mark.django_db
def test_consumer_credit_v2_rejects_normalized_and_metadata_tamper(
    settings,
    tmp_path,
):
    settings.RAW_ARTIFACT_ROOT = tmp_path / "raw-artifacts"
    g19 = FederalReserveG19Provider(client=_g19_client()).consumer_credit()
    g19.records[0]["value"] = Decimal("999")
    g19_run = record_provider_result(
        g19,
        persist=_store_consumer_credit_observations_v2,
    )
    assert g19_run.status == IngestionRun.Status.FAILED
    assert "normalized observations" in g19_run.error

    household = NYFedHouseholdDebtProvider(
        client=_hhdc_client(_hhdc_workbook())
    ).household_debt()
    household.metadata["workbook_url"] = "https://www.newyorkfed.org/forged.xlsx"
    household_run = record_provider_result(
        household,
        persist=_store_consumer_credit_observations_v2,
    )
    assert household_run.status == IngestionRun.Status.FAILED
    assert "replay metadata" in household_run.error

    assert not Observation.objects.filter(
        batch_id__in=(g19_run.batch_id, household_run.batch_id)
    )
    assert not RawArtifact.objects.filter(run__in=(g19_run, household_run))
