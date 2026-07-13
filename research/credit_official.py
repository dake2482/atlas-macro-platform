"""Official credit-market proxy adapters with explicit redistribution metadata.

These providers deliberately do not label any output as ICE BofA OAS, CDX, or
single-name CDS.  They expose three different official inputs:

* Chicago Fed NFCI/ANFCI weekly indexes.  The files are technically public, but
  the Chicago Fed's site licence only expressly grants attributed,
  non-commercial reproduction.  Results therefore remain ``license_review``
  and must not be wired into a public commercial dashboard without permission.
* Federal Reserve Board SLOOS quarterly lending-standard and demand measures.
  Board-produced website information is public domain unless otherwise noted.
* Treasury HQM monthly-average high-quality corporate-bond par yields.  HQM is
  a transparent corporate-yield proxy, not a rating-bucket OAS series.

The adapters return :class:`research.providers.ProviderResult` and perform no
database writes.  Publishing remains a separate, licence-aware decision.
"""

from __future__ import annotations

import calendar
import csv
import io
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

import httpx
import xlrd

from .providers import HTTPProvider, ProviderResult

CHICAGO_FED_NFCI_TERMS_URL = "https://www.chicagofed.org/utilities/legal-notices"
FEDERAL_RESERVE_BOARD_TERMS_URL = "https://www.federalreserve.gov/disclaimer.htm"
TREASURY_SITE_POLICIES_URL = "https://home.treasury.gov/subfooter/site-policies-and-notices"
US_GOVERNMENT_WORKS_URL = (
    "https://www.govinfo.gov/content/pkg/USCODE-2024-title17/html/"
    "USCODE-2024-title17-chap1-sec105.htm"
)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, "", "."):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class BinaryHTTPProvider(HTTPProvider):
    """HTTPProvider extension for official ZIP/XLS downloads."""

    def _get_bytes(
        self,
        dataset: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[bytes | None, ProviderResult | None]:
        try:
            response = self.client.get(path, params=params)
            response.raise_for_status()
            return response.content, None
        except httpx.HTTPError as exc:
            return None, ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")


class ChicagoFedNFCIProvider(HTTPProvider):
    """Weekly Chicago Fed financial-conditions indexes.

    Technical access is keyless, but the Chicago Fed legal notice does not
    grant blanket commercial republication rights.  Callers must preserve the
    ``license_review`` metadata and keep these records out of public commercial
    dashboards until written permission is recorded.
    """

    key = "chicago-fed-nfci"
    base_url = "https://api.data.chicagofed.org"
    DATA_PATH = "/NFCI/nfci-data-series-csv.csv"
    SOURCE_PAGE = "https://www.chicagofed.org/research/data/nfci/current-data"
    SERIES = {
        "NFCI": ("NFCI", "National Financial Conditions Index"),
        "ANFCI": ("ANFCI", "Adjusted National Financial Conditions Index"),
        "Risk": ("NFCI-RISK", "NFCI risk subindex"),
        "Credit": ("NFCI-CREDIT", "NFCI credit subindex"),
        "Leverage": ("NFCI-LEVERAGE", "NFCI leverage subindex"),
        "Nonfinancial_Leverage": (
            "NFCI-NONFINANCIAL-LEVERAGE",
            "NFCI nonfinancial leverage subindex",
        ),
    }

    def weekly_indexes(self) -> ProviderResult:
        dataset = "weekly-indexes"
        payload, failure = self._get_text(dataset, self.DATA_PATH)
        if failure:
            return failure

        records: list[dict[str, Any]] = []
        try:
            rows = csv.DictReader(io.StringIO(payload or ""))
            if rows.fieldnames is None or "Friday_of_Week" not in rows.fieldnames:
                raise ValueError("missing Friday_of_Week column")
            for row in rows:
                raw_date = (row.get("Friday_of_Week") or "").strip()
                try:
                    value_date = datetime.strptime(raw_date, "%m/%d/%Y").date()
                except ValueError:
                    continue
                for column, (series_id, description) in self.SERIES.items():
                    value = _decimal_or_none(row.get(column))
                    if value is None:
                        continue
                    records.append(
                        {
                            "series_id": series_id,
                            "date": value_date.isoformat(),
                            "value": value,
                            "metadata": {
                                "description": description,
                                "unit": "index (standard deviations)",
                                "frequency": "weekly",
                                "date_convention": "week ending Friday",
                                "official_source_url": self.SOURCE_PAGE,
                                "download_url": f"{self.base_url}{self.DATA_PATH}",
                                "terms_url": CHICAGO_FED_NFCI_TERMS_URL,
                                "license_status": "review",
                                "public_display_allowed": False,
                                "commercial_republication_requires_permission": True,
                            },
                        }
                    )
        except (csv.Error, ValueError) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        if not records:
            return ProviderResult.failure(self.key, dataset, "no NFCI observations found")
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "frequency": "weekly",
                "date_convention": "week ending Friday",
                "source_page": self.SOURCE_PAGE,
                "download_url": f"{self.base_url}{self.DATA_PATH}",
                "terms_url": CHICAGO_FED_NFCI_TERMS_URL,
                "license_status": "review",
                "public_display_allowed": False,
                "license_note": (
                    "Chicago Fed expressly permits attributed non-commercial reproduction; "
                    "obtain written permission for commercial republication or distribution."
                ),
            },
        )


class FederalReserveSLOOSProvider(BinaryHTTPProvider):
    """Quarterly SLOOS series from the Board's stable DDP SDMX ZIP."""

    key = "federal-reserve-sloos"
    base_url = "https://www.federalreserve.gov"
    DATA_PATH = "/datadownload/Output.aspx"
    SOURCE_PAGE = "https://www.federalreserve.gov/datadownload/Choose.aspx?rel=SLOOS"
    DEFAULT_SERIES = {
        "SUBLPDMBS_XWB_N.Q": (
            "Business-loan lending standards",
            "Net percentage tightening, loan-balance weighted",
        ),
        "SUBLPDMBD_XWB_N.Q": (
            "Business-loan demand",
            "Net percentage reporting stronger demand, loan-balance weighted",
        ),
        "SUBLPDMHS_XWB_N.Q": (
            "Household-loan lending standards",
            "Net percentage tightening, loan-balance weighted",
        ),
        "SUBLPDMHD_XWB_N.Q": (
            "Household-loan demand",
            "Net percentage reporting stronger demand, loan-balance weighted",
        ),
        "SUBLPDCILS_N.Q": (
            "C&I lending standards: large and middle-market firms",
            "Net percentage tightening",
        ),
        "SUBLPDCISS_N.Q": (
            "C&I lending standards: small firms",
            "Net percentage tightening",
        ),
    }
    MAX_XML_SIZE = 50_000_000

    @staticmethod
    def _short_description(series: ElementTree.Element) -> str:
        for annotation in series.iter():
            if _local_name(annotation.tag) != "Annotation":
                continue
            annotation_type = ""
            annotation_text = ""
            for child in annotation:
                name = _local_name(child.tag)
                if name == "AnnotationType":
                    annotation_type = child.text or ""
                elif name == "AnnotationText":
                    annotation_text = child.text or ""
            if annotation_type == "Short Description":
                return annotation_text
        return ""

    def quarterly_series(self, *, series_ids: Iterable[str] | None = None) -> ProviderResult:
        dataset = "quarterly-series"
        requested = set(self.DEFAULT_SERIES if series_ids is None else series_ids)
        if not requested:
            return ProviderResult.failure(self.key, dataset, "series_ids cannot be empty")

        payload, failure = self._get_bytes(
            dataset,
            self.DATA_PATH,
            params={"rel": "SLOOS", "filetype": "zip"},
        )
        if failure:
            return failure

        try:
            with zipfile.ZipFile(io.BytesIO(payload or b"")) as archive:
                member = archive.getinfo("SLOOS_data.xml")
                if member.file_size > self.MAX_XML_SIZE:
                    raise ValueError("SLOOS XML exceeds safe size limit")
                root = ElementTree.fromstring(archive.read(member))
        except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        prepared = next(
            (
                element.text
                for element in root.iter()
                if _local_name(element.tag) == "Prepared" and element.text
            ),
            None,
        )
        records: list[dict[str, Any]] = []
        found_series: set[str] = set()
        for series in root.iter():
            if _local_name(series.tag) != "Series":
                continue
            series_id = series.attrib.get("SERIES_NAME", "")
            if series_id not in requested:
                continue
            found_series.add(series_id)
            description = self._short_description(series)
            for observation in series:
                if _local_name(observation.tag) != "Obs":
                    continue
                value = _decimal_or_none(observation.attrib.get("OBS_VALUE"))
                value_date = observation.attrib.get("TIME_PERIOD")
                if value is None or not value_date:
                    continue
                records.append(
                    {
                        "series_id": series_id,
                        "date": value_date,
                        "value": value,
                        "metadata": {
                            "description": description,
                            "dashboard_label": self.DEFAULT_SERIES.get(series_id, (series_id, ""))[
                                0
                            ],
                            "interpretation": self.DEFAULT_SERIES.get(series_id, ("", ""))[1],
                            "unit": series.attrib.get("UNIT", "Percentage"),
                            "unit_multiplier": series.attrib.get("UNIT_MULT", "1"),
                            "frequency": "quarterly",
                            "date_convention": "survey quarter end",
                            "observation_status": observation.attrib.get("OBS_STATUS"),
                            "panel": series.attrib.get("PANEL"),
                            "measure": series.attrib.get("MEASURE"),
                            "loan_group": series.attrib.get("LOANGROUP"),
                            "loan_type": series.attrib.get("LOANTYPE"),
                            "bank_size": series.attrib.get("BANKSIZE"),
                            "official_source_url": self.SOURCE_PAGE,
                            "download_url": f"{self.base_url}{self.DATA_PATH}?rel=SLOOS&filetype=zip",
                            "terms_url": FEDERAL_RESERVE_BOARD_TERMS_URL,
                            "license_status": "open",
                            "public_display_allowed": True,
                            "attribution": "Board of Governors of the Federal Reserve System",
                        },
                    }
                )

        missing = sorted(requested - found_series)
        if not records:
            detail = f"; missing series: {', '.join(missing)}" if missing else ""
            return ProviderResult.failure(self.key, dataset, f"no SLOOS observations found{detail}")
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "frequency": "quarterly",
                "date_convention": "survey quarter end",
                "prepared_at": prepared,
                "requested_series": sorted(requested),
                "found_series": sorted(found_series),
                "missing_series": missing,
                "source_page": self.SOURCE_PAGE,
                "download_url": f"{self.base_url}{self.DATA_PATH}?rel=SLOOS&filetype=zip",
                "terms_url": FEDERAL_RESERVE_BOARD_TERMS_URL,
                "license_status": "open",
                "public_display_allowed": True,
                "license_note": (
                    "Board-produced website information is public domain unless otherwise "
                    "indicated; cite the Board and do not reuse seals or imply endorsement."
                ),
            },
        )


class TreasuryHQMProvider(BinaryHTTPProvider):
    """Monthly-average Treasury HQM corporate-bond par-yield curve."""

    key = "us-treasury-hqm"
    base_url = "https://home.treasury.gov"
    DATA_PATH = "/system/files/226/hqm_qh_pars.xls"
    SOURCE_PAGE = (
        "https://home.treasury.gov/data/treasury-coupon-issues-and-corporate-bond-"
        "yield-curve/corporate-bond-yield-curve"
    )

    @staticmethod
    def _parse_month(value: Any, datemode: int) -> datetime | None:
        if isinstance(value, (int, float)) and value:
            try:
                parsed = xlrd.xldate_as_datetime(value, datemode)
                return parsed.replace(tzinfo=UTC)
            except (OverflowError, ValueError, xlrd.XLDateError):
                return None
        if isinstance(value, str):
            try:
                return datetime.strptime(value.strip(), "%b %Y").replace(tzinfo=UTC)
            except ValueError:
                return None
        return None

    @staticmethod
    def _maturity_years(value: Any) -> int | None:
        text = str(value).strip().lower()
        if not text:
            return None
        try:
            return int(text.split()[0])
        except (ValueError, IndexError):
            return None

    def par_yields(self) -> ProviderResult:
        dataset = "monthly-average-par-yields"
        payload, failure = self._get_bytes(dataset, self.DATA_PATH)
        if failure:
            return failure

        try:
            workbook = xlrd.open_workbook(file_contents=payload or b"", on_demand=True)
            sheet = workbook.sheet_by_index(0)
            date_header_row = next(
                row
                for row in range(min(sheet.nrows, 20))
                if str(sheet.cell_value(row, 0)).strip().lower() == "date"
            )
            maturity_row = date_header_row + 1
            maturities = {
                column: maturity
                for column in range(sheet.ncols)
                if (maturity := self._maturity_years(sheet.cell_value(maturity_row, column)))
            }
            if not maturities:
                raise ValueError("HQM maturity headers not found")

            records: list[dict[str, Any]] = []
            for row in range(maturity_row + 1, sheet.nrows):
                parsed_month = self._parse_month(sheet.cell_value(row, 0), workbook.datemode)
                if parsed_month is None:
                    continue
                last_day = calendar.monthrange(parsed_month.year, parsed_month.month)[1]
                value_date = parsed_month.replace(day=last_day)
                for column, maturity in maturities.items():
                    value = _decimal_or_none(sheet.cell_value(row, column))
                    if value is None:
                        continue
                    records.append(
                        {
                            "series_id": f"HQM-PAR-{maturity}Y",
                            "date": value_date.date().isoformat(),
                            "value": value,
                            "metadata": {
                                "description": (
                                    f"Treasury HQM high-quality corporate-bond {maturity}-year "
                                    "monthly-average par yield"
                                ),
                                "maturity_years": maturity,
                                "curve": "HQM high-quality corporate bond",
                                "rate_type": "par yield",
                                "statistic": "monthly average",
                                "unit": "percent",
                                "frequency": "monthly",
                                "date_convention": "reference month end",
                                "official_source_url": self.SOURCE_PAGE,
                                "download_url": f"{self.base_url}{self.DATA_PATH}",
                                "terms_url": TREASURY_SITE_POLICIES_URL,
                                "copyright_basis_url": US_GOVERNMENT_WORKS_URL,
                                "license_status": "open",
                                "public_display_allowed": True,
                                "attribution": "U.S. Department of the Treasury",
                                "not_oas": True,
                            },
                        }
                    )
        except (StopIteration, IndexError, ValueError, xlrd.XLRDError) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        if not records:
            return ProviderResult.failure(self.key, dataset, "no HQM observations found")
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "frequency": "monthly",
                "date_convention": "reference month end",
                "curve": "HQM high-quality corporate bond",
                "rate_type": "par yield",
                "statistic": "monthly average",
                "tenors": sorted(maturities.values()),
                "source_page": self.SOURCE_PAGE,
                "download_url": f"{self.base_url}{self.DATA_PATH}",
                "terms_url": TREASURY_SITE_POLICIES_URL,
                "copyright_basis_url": US_GOVERNMENT_WORKS_URL,
                "license_status": "open",
                "public_display_allowed": True,
                "license_note": (
                    "Treasury-produced HQM data is treated as a U.S. Government work under "
                    "17 U.S.C. 105; attribute Treasury, do not imply endorsement, and do not "
                    "present HQM as an OAS or CDS quote."
                ),
            },
        )
