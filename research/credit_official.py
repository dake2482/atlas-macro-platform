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
import hashlib
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

    def _get_bytes_with_evidence(
        self,
        dataset: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[bytes | None, dict[str, Any], ProviderResult | None]:
        """Fetch binary input while binding parsing to the exact response bytes."""

        try:
            response = self.client.get(path, params=params)
            response.raise_for_status()
            payload = bytes(response.content)
        except httpx.HTTPError as exc:
            return (
                None,
                {},
                ProviderResult.failure(
                    self.key, dataset, f"{type(exc).__name__}: {exc}"
                ),
            )
        return (
            payload,
            {
                "endpoint": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "byte_length": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            None,
        )


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
    MAX_ARCHIVE_SIZE = 75_000_000
    MAX_COMPRESSION_RATIO = 250

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

        payload, response_evidence, failure = self._get_bytes_with_evidence(
            dataset,
            self.DATA_PATH,
            params={"rel": "SLOOS", "filetype": "zip"},
        )
        if failure:
            return failure

        try:
            records, archive_evidence = self.parse_archive_bytes(
                payload or b"", series_ids=requested
            )
        except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError) as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

        missing = sorted(requested - set(archive_evidence["found_series"]))
        if not records:
            detail = f"; missing series: {', '.join(missing)}" if missing else ""
            return ProviderResult.failure(self.key, dataset, f"no SLOOS observations found{detail}")
        metadata = {
            "frequency": "quarterly",
            "date_convention": "survey quarter end",
            "prepared_at": archive_evidence["prepared_at"],
            "file_prepared_at": archive_evidence["file_prepared_at"],
            "requested_series": sorted(requested),
            "found_series": archive_evidence["found_series"],
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
            **response_evidence,
            **archive_evidence,
        }
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata=metadata,
            raw_bytes=payload,
        )

    @classmethod
    def parse_archive_bytes(
        cls,
        payload: bytes,
        *,
        series_ids: Iterable[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Replay the exact Board ZIP and return normalized rows plus member evidence."""

        requested = set(cls.DEFAULT_SERIES if series_ids is None else series_ids)
        if not payload:
            raise ValueError("SLOOS archive bytes are empty")
        if len(payload) > cls.MAX_ARCHIVE_SIZE:
            raise ValueError("SLOOS archive exceeds safe size limit")

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if names.count("SLOOS_data.xml") != 1:
                raise ValueError("SLOOS archive must contain one exact XML member")
            member = archive.getinfo("SLOOS_data.xml")
            if member.filename != "SLOOS_data.xml" or "/" in member.filename:
                raise ValueError("SLOOS XML member path is invalid")
            if member.flag_bits & 0x1:
                raise ValueError("SLOOS XML member must not be encrypted")
            if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("SLOOS XML member compression method is not allowed")
            if member.file_size > cls.MAX_XML_SIZE:
                raise ValueError("SLOOS XML exceeds safe size limit")
            if member.file_size and not member.compress_size:
                raise ValueError("SLOOS XML compressed size is invalid")
            if (
                member.compress_size
                and member.file_size / member.compress_size
                > cls.MAX_COMPRESSION_RATIO
            ):
                raise ValueError("SLOOS XML compression ratio exceeds safe limit")
            member_bytes = archive.read(member)
        root = ElementTree.fromstring(member_bytes)

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
        series_occurrences: dict[str, int] = {}
        observation_keys: set[tuple[str, str]] = set()
        for series in root.iter():
            if _local_name(series.tag) != "Series":
                continue
            series_id = series.attrib.get("SERIES_NAME", "")
            if series_id not in requested:
                continue
            series_occurrences[series_id] = series_occurrences.get(series_id, 0) + 1
            if series_occurrences[series_id] != 1:
                raise ValueError(f"duplicate SLOOS series element: {series_id}")
            found_series.add(series_id)
            description = cls._short_description(series)
            for observation in series:
                if _local_name(observation.tag) != "Obs":
                    continue
                value = _decimal_or_none(observation.attrib.get("OBS_VALUE"))
                value_date = observation.attrib.get("TIME_PERIOD")
                if value is None or not value_date:
                    continue
                observation_key = (series_id, value_date)
                if observation_key in observation_keys:
                    raise ValueError(
                        f"duplicate SLOOS observation: {series_id} {value_date}"
                    )
                observation_keys.add(observation_key)
                records.append(
                    {
                        "series_id": series_id,
                        "date": value_date,
                        "value": value,
                        "metadata": {
                            "description": description,
                            "dashboard_label": cls.DEFAULT_SERIES.get(series_id, (series_id, ""))[
                                0
                            ],
                            "interpretation": cls.DEFAULT_SERIES.get(series_id, ("", ""))[1],
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
                            "official_source_url": cls.SOURCE_PAGE,
                            "download_url": f"{cls.base_url}{cls.DATA_PATH}?rel=SLOOS&filetype=zip",
                            "terms_url": FEDERAL_RESERVE_BOARD_TERMS_URL,
                            "license_status": "open",
                            "public_display_allowed": True,
                            "attribution": "Board of Governors of the Federal Reserve System",
                        },
                    }
                )

        return records, {
            "archive_member": "SLOOS_data.xml",
            "archive_member_name": "SLOOS_data.xml",
            "archive_member_size": len(member_bytes),
            "archive_member_sha256": hashlib.sha256(member_bytes).hexdigest(),
            "prepared_at": prepared,
            "file_prepared_at": prepared,
            "found_series": sorted(found_series),
        }


class TreasuryHQMProvider(BinaryHTTPProvider):
    """Monthly-average Treasury HQM corporate-bond par-yield curve."""

    key = "us-treasury-hqm"
    base_url = "https://home.treasury.gov"
    DATA_PATH = "/system/files/226/hqm_qh_pars.xls"
    SOURCE_PAGE = (
        "https://home.treasury.gov/data/treasury-coupon-issues-and-corporate-bond-"
        "yield-curve/corporate-bond-yield-curve"
    )
    MAX_WORKBOOK_SIZE = 25_000_000

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
        payload, response_evidence, failure = self._get_bytes_with_evidence(
            dataset, self.DATA_PATH
        )
        if failure:
            return failure

        try:
            records, workbook_evidence = self.parse_workbook_bytes(payload or b"")
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
                "tenors": list(workbook_evidence["workbook_validation"]["maturities"]),
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
                **response_evidence,
                **workbook_evidence,
            },
            raw_bytes=payload,
        )

    @classmethod
    def parse_workbook_bytes(
        cls, payload: bytes
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Replay the exact Treasury XLS and freeze the validated workbook shape."""

        if not payload:
            raise ValueError("HQM workbook bytes are empty")
        if len(payload) > cls.MAX_WORKBOOK_SIZE:
            raise ValueError("HQM workbook exceeds safe size limit")
        workbook = xlrd.open_workbook(file_contents=payload, on_demand=True)
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
            if (maturity := cls._maturity_years(sheet.cell_value(maturity_row, column)))
        }
        if not maturities:
            raise ValueError("HQM maturity headers not found")

        records: list[dict[str, Any]] = []
        for row in range(maturity_row + 1, sheet.nrows):
            parsed_month = cls._parse_month(
                sheet.cell_value(row, 0), workbook.datemode
            )
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
                                "Treasury HQM high-quality corporate-bond "
                                f"{maturity}-year monthly-average par yield"
                            ),
                            "maturity_years": maturity,
                            "curve": "HQM high-quality corporate bond",
                            "rate_type": "par yield",
                            "statistic": "monthly average",
                            "unit": "percent",
                            "frequency": "monthly",
                            "date_convention": "reference month end",
                            "official_source_url": cls.SOURCE_PAGE,
                            "download_url": f"{cls.base_url}{cls.DATA_PATH}",
                            "terms_url": TREASURY_SITE_POLICIES_URL,
                            "copyright_basis_url": US_GOVERNMENT_WORKS_URL,
                            "license_status": "open",
                            "public_display_allowed": True,
                            "attribution": "U.S. Department of the Treasury",
                            "not_oas": True,
                        },
                    }
                )
        sheet_name = str(getattr(sheet, "name", "Sheet 1"))
        return records, {
            "file_type": "application/vnd.ms-excel",
            "workbook_file_type": "xls",
            "workbook_validation": {
                "sheet_index": 0,
                "sheet_name": sheet_name,
                "date_header_row": date_header_row,
                "maturity_header_row": maturity_row,
                "maturities": sorted(maturities.values()),
                "series_ids": sorted(
                    {str(record["series_id"]) for record in records}
                ),
                "headers_validated": True,
            },
        }
