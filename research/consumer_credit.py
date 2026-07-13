"""Official U.S. consumer-credit and household-debt source adapters.

The Board's G.19 Data Download Program supplies monthly consumer-credit
balances and growth rates.  The New York Fed publishes the quarterly
Household Debt and Credit workbook, based on its Consumer Credit Panel /
Equifax data.  Both adapters preserve the official file fingerprint and fail
closed when the expected release structure or latest period is inconsistent.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import html
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl

import httpx
from openpyxl import load_workbook

from .providers import HTTPProvider, ProviderResult

G19_CHOOSE_PATH = "/datadownload/Choose.aspx"
G19_OUTPUT_PATH = "/datadownload/Output.aspx"
G19_SOURCE_PAGE = "https://www.federalreserve.gov/releases/g19/current/"
G19_CHOOSE_URL = "https://www.federalreserve.gov/datadownload/Choose.aspx?rel=G19"

NYFED_DATABANK_PATH = "/microeconomics/databank.html"
NYFED_DATABANK_URL = "https://www.newyorkfed.org/microeconomics/databank.html"
NYFED_HHDC_PAGE = "https://www.newyorkfed.org/microeconomics/hhdc"

G19_SERIES = {
    "Percent change of total consumer credit, seasonally adjusted at an annual rate": (
        "G19-CONSUMER-CREDIT-GROWTH-SAAR",
        "% annual rate",
    ),
    "Percent change of total revolving consumer credit, seasonally adjusted at an annual rate": (
        "G19-REVOLVING-CREDIT-GROWTH-SAAR",
        "% annual rate",
    ),
    "Percent change of total nonrevolving consumer credit, seasonally adjusted at an annual rate": (
        "G19-NONREVOLVING-CREDIT-GROWTH-SAAR",
        "% annual rate",
    ),
    "Total consumer credit owned and securitized, seasonally adjusted level": (
        "G19-CONSUMER-CREDIT-OUTSTANDING-SA",
        "USD millions",
    ),
    "Revolving consumer credit owned and securitized, seasonally adjusted level": (
        "G19-REVOLVING-CREDIT-OUTSTANDING-SA",
        "USD millions",
    ),
    "Nonrevolving consumer credit owned and securitized, seasonally adjusted level": (
        "G19-NONREVOLVING-CREDIT-OUTSTANDING-SA",
        "USD millions",
    ),
    "Total consumer credit owned and securitized, seasonally adjusted flow, monthly rate": (
        "G19-CONSUMER-CREDIT-FLOW-SA",
        "USD millions per month",
    ),
    "Revolving consumer credit owned and securitized, seasonally adjusted flow, monthly rate": (
        "G19-REVOLVING-CREDIT-FLOW-SA",
        "USD millions per month",
    ),
    "Nonrevolving consumer credit owned and securitized, seasonally adjusted flow, monthly rate": (
        "G19-NONREVOLVING-CREDIT-FLOW-SA",
        "USD millions per month",
    ),
}

HHDC_BALANCE_SERIES = {
    "Mortgage": "HHDC-MORTGAGE-BALANCE",
    "HE Revolving": "HHDC-HELOC-BALANCE",
    "Auto Loan": "HHDC-AUTO-LOAN-BALANCE",
    "Credit Card": "HHDC-CREDIT-CARD-BALANCE",
    "Student Loan": "HHDC-STUDENT-LOAN-BALANCE",
    "Other": "HHDC-OTHER-BALANCE",
    "Total": "HHDC-TOTAL-DEBT-BALANCE",
}

HHDC_DELINQUENCY_SERIES = {
    "MORTGAGE": "HHDC-MORTGAGE-90D-DELINQUENT",
    "HELOC": "HHDC-HELOC-90D-DELINQUENT",
    "AUTO": "HHDC-AUTO-90D-DELINQUENT",
    "CC": "HHDC-CREDIT-CARD-90D-DELINQUENT",
    "STUDENT LOAN": "HHDC-STUDENT-LOAN-90D-DELINQUENT",
    "OTHER": "HHDC-OTHER-90D-DELINQUENT",
    "ALL": "HHDC-ALL-90D-DELINQUENT",
}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", ".", "n.a."):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _artifact(url: str, content: bytes, content_type: str) -> dict[str, Any]:
    return {
        "url": url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "content_type": content_type,
    }


class FederalReserveG19Provider(HTTPProvider):
    """Monthly seasonally adjusted G.19 history from the Board DDP CSV."""

    key = "federal-reserve-g19"
    base_url = "https://www.federalreserve.gov"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        max_html_bytes: int = 2 * 1024 * 1024,
        max_csv_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.max_html_bytes = max_html_bytes
        self.max_csv_bytes = max_csv_bytes
        super().__init__(
            client=client,
            timeout=timeout,
            headers={
                "Accept": "text/html,text/csv",
                "User-Agent": "AtlasMacro/0.1 G19 data downloader",
            },
        )

    def consumer_credit(self) -> ProviderResult:
        dataset = "consumer-credit"
        try:
            choose = self.client.get(G19_CHOOSE_PATH, params={"rel": "G19"})
            choose.raise_for_status()
            choose_bytes = choose.content
            if not choose_bytes or len(choose_bytes) > self.max_html_bytes:
                raise ValueError("G.19 package page exceeded configured size or was empty")
            choose_text = choose_bytes.decode(choose.encoding or "utf-8", errors="replace")
            package_params = self._package_params(choose_text)
            release_date = self._release_date(choose_text)

            response = self.client.get(G19_OUTPUT_PATH, params=package_params)
            response.raise_for_status()
            csv_bytes = response.content
            if not csv_bytes or len(csv_bytes) > self.max_csv_bytes:
                raise ValueError("G.19 CSV exceeded configured size or was empty")
            records, latest_period = self._parse_csv(csv_bytes)
            for record in records:
                record["metadata"]["source_revision_date"] = release_date.isoformat()
                record["metadata"]["release_freshness_days"] = 45
        except (httpx.HTTPError, UnicodeError, csv.Error, ValueError) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        output_url = str(response.url)
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "source_page": G19_SOURCE_PAGE,
                "release_date": release_date.isoformat(),
                "latest_value_date": latest_period,
                "frequency": "monthly",
                "seasonal_adjustment": "seasonally adjusted",
                "quality_status": "complete",
                "artifacts": [
                    _artifact(G19_CHOOSE_URL, choose_bytes, "text/html"),
                    _artifact(output_url, csv_bytes, "text/csv"),
                ],
            },
        )

    @staticmethod
    def _package_params(page: str) -> dict[str, str]:
        match = re.search(
            r'name="FreqRequest"\s+value="([^"]+)"[^>]*>'
            r'<label[^>]*>Consumer Credit Outstanding \(S\.A\.\)',
            page,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise ValueError("G.19 seasonally adjusted package link not found")
        params = dict(parse_qsl(html.unescape(match.group(1)), keep_blank_values=True))
        expected = {"rel", "series", "filetype", "label", "layout", "type"}
        if not expected <= params.keys() or params.get("rel") != "G19":
            raise ValueError("G.19 package parameters are incomplete")
        return params

    @staticmethod
    def _release_date(page: str) -> date:
        match = re.search(r"last released\s+([A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})", page)
        if match is None:
            raise ValueError("G.19 release date not found")
        raw = match.group(1).replace(",", "", 1)
        try:
            return datetime.strptime(raw, "%A %B %d, %Y").date()
        except ValueError as exc:
            raise ValueError("G.19 release date is invalid") from exc

    @staticmethod
    def _parse_csv(payload: bytes) -> tuple[list[dict[str, Any]], str]:
        text = payload.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        if len(rows) < 7 or not rows[0] or rows[0][0] != "Series Description":
            raise ValueError("G.19 CSV header is missing")
        descriptions = rows[0][1:]
        units = rows[1][1:] if rows[1] and rows[1][0] == "Unit:" else []
        multipliers = rows[2][1:] if rows[2] and rows[2][0] == "Multiplier:" else []
        identifiers = rows[4][1:] if rows[4] and rows[4][0] == "Unique Identifier:" else []
        if not (len(descriptions) == len(units) == len(multipliers) == len(identifiers)):
            raise ValueError("G.19 CSV metadata columns are inconsistent")
        missing_descriptions = sorted(set(G19_SERIES) - set(descriptions))
        if missing_descriptions:
            raise ValueError(f"G.19 required columns missing: {', '.join(missing_descriptions)}")

        records: list[dict[str, Any]] = []
        latest_by_series: dict[str, str] = {}
        for row in rows[6:]:
            if not row or not re.fullmatch(r"\d{4}-\d{2}", row[0].strip()):
                continue
            value_date = f"{row[0].strip()}-01"
            for index, description in enumerate(descriptions, start=1):
                target = G19_SERIES.get(description)
                if target is None:
                    continue
                value = _decimal(row[index] if index < len(row) else None)
                if value is None:
                    continue
                series_id, normalized_unit = target
                latest_by_series[series_id] = value_date
                records.append(
                    {
                        "series_id": series_id,
                        "date": value_date,
                        "value": value,
                        "metadata": {
                            "source_series_id": identifiers[index - 1],
                            "description": description,
                            "source_unit": units[index - 1],
                            "source_multiplier": multipliers[index - 1],
                            "unit": normalized_unit,
                            "frequency": "monthly",
                            "seasonal_adjustment": "seasonally adjusted",
                            "official_source_url": G19_SOURCE_PAGE,
                        },
                    }
                )
        if not records or set(latest_by_series) != {item[0] for item in G19_SERIES.values()}:
            raise ValueError("G.19 CSV did not provide complete required history")
        latest_periods = set(latest_by_series.values())
        if len(latest_periods) != 1:
            raise ValueError("G.19 required series have inconsistent latest months")
        return records, latest_periods.pop()


class NYFedHouseholdDebtProvider(HTTPProvider):
    """Quarterly national household-debt balances and delinquency rates."""

    key = "ny-fed-household-credit"
    base_url = "https://www.newyorkfed.org"

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 90.0,
        max_html_bytes: int = 4 * 1024 * 1024,
        max_workbook_bytes: int = 8 * 1024 * 1024,
        max_expanded_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.max_html_bytes = max_html_bytes
        self.max_workbook_bytes = max_workbook_bytes
        self.max_expanded_bytes = max_expanded_bytes
        super().__init__(
            client=client,
            timeout=timeout,
            headers={
                "Accept": "text/html,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "User-Agent": "AtlasMacro/0.1 NY Fed household debt downloader",
            },
        )

    def household_debt(self) -> ProviderResult:
        dataset = "household-debt-credit"
        try:
            bank = self.client.get(NYFED_DATABANK_PATH)
            bank.raise_for_status()
            bank_bytes = bank.content
            if not bank_bytes or len(bank_bytes) > self.max_html_bytes:
                raise ValueError("NY Fed data bank page exceeded configured size or was empty")
            bank_text = bank_bytes.decode(bank.encoding or "utf-8", errors="replace")
            workbook_path, expected_period = self._latest_workbook(bank_text)

            response = self.client.get(workbook_path)
            response.raise_for_status()
            workbook_bytes = response.content
            if not workbook_bytes or len(workbook_bytes) > self.max_workbook_bytes:
                raise ValueError("NY Fed household debt workbook exceeded configured size or was empty")
            self._validate_archive(workbook_bytes)
            records, latest_period, workbook_title = self._parse_workbook(workbook_bytes)
            if latest_period != expected_period:
                raise ValueError(
                    f"NY Fed workbook latest period {latest_period} does not match filename {expected_period}"
                )
        except (httpx.HTTPError, UnicodeError, OSError, ValueError, zipfile.BadZipFile) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        workbook_url = str(response.url)
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "source_page": NYFED_HHDC_PAGE,
                "data_bank_url": NYFED_DATABANK_URL,
                "workbook_url": workbook_url,
                "workbook_title": workbook_title,
                "latest_value_date": latest_period,
                "frequency": "quarterly",
                "quality_status": "complete",
                "attribution": "New York Fed Consumer Credit Panel / Equifax",
                "artifacts": [
                    _artifact(NYFED_DATABANK_URL, bank_bytes, "text/html"),
                    _artifact(
                        workbook_url,
                        workbook_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ),
                ],
            },
        )

    @staticmethod
    def _latest_workbook(page: str) -> tuple[str, str]:
        candidates: dict[tuple[int, int], str] = {}
        pattern = re.compile(
            r'href=["\']([^"\']*hhd_c_report_(\d{4})q([1-4])\.xlsx)["\']',
            flags=re.IGNORECASE,
        )
        for match in pattern.finditer(page):
            year, quarter = int(match.group(2)), int(match.group(3))
            candidates[(year, quarter)] = html.unescape(match.group(1))
        if not candidates:
            raise ValueError("NY Fed household debt workbook link not found")
        year, quarter = max(candidates)
        return candidates[(year, quarter)], _quarter_end(year, quarter).isoformat()

    def _validate_archive(self, payload: bytes) -> None:
        if not payload.startswith(b"PK"):
            raise ValueError("NY Fed household debt response is not an XLSX workbook")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if len(archive.infolist()) > 500:
                raise ValueError("NY Fed workbook contains too many archive members")
            expanded = sum(member.file_size for member in archive.infolist())
            if expanded > self.max_expanded_bytes:
                raise ValueError("NY Fed workbook exceeds configured expanded-size limit")
            if "xl/workbook.xml" not in archive.namelist():
                raise ValueError("NY Fed workbook archive is missing xl/workbook.xml")

    @staticmethod
    def _parse_workbook(payload: bytes) -> tuple[list[dict[str, Any]], str, str]:
        try:
            workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        except Exception as exc:
            raise ValueError(f"NY Fed workbook could not be parsed: {exc}") from exc
        try:
            required_sheets = {"TABLE OF CONTENTS", "Page 3 Data", "Page 12 Data"}
            missing_sheets = sorted(required_sheets - set(workbook.sheetnames))
            if missing_sheets:
                raise ValueError(f"NY Fed workbook sheets missing: {', '.join(missing_sheets)}")
            title = str(workbook["TABLE OF CONTENTS"].cell(2, 2).value or "").strip()
            if "HOUSEHOLD DEBT AND CREDIT" not in title.upper():
                raise ValueError("NY Fed workbook title is invalid")
            balance_records, balance_latest = _parse_hhdc_sheet(
                workbook["Page 3 Data"],
                HHDC_BALANCE_SERIES,
                unit="USD trillions",
                chart_name="Total Debt Balance and Its Composition",
            )
            delinquency_records, delinquency_latest = _parse_hhdc_sheet(
                workbook["Page 12 Data"],
                HHDC_DELINQUENCY_SERIES,
                unit="% of balance 90+ days delinquent",
                chart_name="Percent of Balance 90+ Days Delinquent by Loan Type",
            )
        finally:
            workbook.close()
        if balance_latest != delinquency_latest:
            raise ValueError("NY Fed balance and delinquency sheets have different latest periods")
        return [*balance_records, *delinquency_records], balance_latest, title


def _quarter_end(year: int, quarter: int) -> date:
    month = quarter * 3
    return date(year, month, calendar.monthrange(year, month)[1])


def _quarter_label(value: Any) -> tuple[str, str] | None:
    if isinstance(value, datetime):
        quarter = (value.month - 1) // 3 + 1
        end = _quarter_end(value.year, quarter)
        return end.isoformat(), f"{value.year}:Q{quarter}"
    match = re.fullmatch(r"(\d{2,4}):Q([1-4])", str(value or "").strip(), re.IGNORECASE)
    if match is None:
        return None
    raw_year = int(match.group(1))
    year = raw_year if raw_year >= 100 else (1900 + raw_year if raw_year >= 90 else 2000 + raw_year)
    quarter = int(match.group(2))
    return _quarter_end(year, quarter).isoformat(), f"{year}:Q{quarter}"


def _parse_hhdc_sheet(
    sheet: Any,
    series_map: dict[str, str],
    *,
    unit: str,
    chart_name: str,
) -> tuple[list[dict[str, Any]], str]:
    headers = [str(cell.value or "").strip() for cell in sheet[4]]
    found_headers = set(headers)
    missing_headers = sorted(set(series_map) - found_headers)
    if missing_headers:
        raise ValueError(f"NY Fed {chart_name} columns missing: {', '.join(missing_headers)}")
    positions = {name: headers.index(name) for name in series_map}
    records: list[dict[str, Any]] = []
    latest_by_series: dict[str, str] = {}
    for row in sheet.iter_rows(min_row=5, values_only=True):
        period = _quarter_label(row[0] if row else None)
        if period is None:
            continue
        value_date, period_label = period
        for name, series_id in series_map.items():
            index = positions[name]
            value = _decimal(row[index] if index < len(row) else None)
            if value is None:
                continue
            latest_by_series[series_id] = value_date
            records.append(
                {
                    "series_id": series_id,
                    "date": value_date,
                    "value": value,
                    "metadata": {
                        "description": name,
                        "chart_name": chart_name,
                        "period_label": period_label,
                        "unit": unit,
                        "frequency": "quarterly",
                        "official_source_url": NYFED_HHDC_PAGE,
                        "attribution": "New York Fed Consumer Credit Panel / Equifax",
                    },
                }
            )
    if not records or set(latest_by_series) != set(series_map.values()):
        raise ValueError(f"NY Fed {chart_name} did not provide complete required history")
    latest_periods = set(latest_by_series.values())
    if len(latest_periods) != 1:
        raise ValueError(f"NY Fed {chart_name} required series have inconsistent latest periods")
    return records, latest_periods.pop()
