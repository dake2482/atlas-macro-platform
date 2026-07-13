"""Direct Federal Reserve Board H.10 foreign-exchange reference data.

The Board's Data Download Program is the authoritative source for these daily
reference series.  They are deliberately named ``H10-*`` inside Atlas Macro so
the public dashboard cannot mistake them for an exchange feed, an intraday
spot quote, or ICE's licensed DXY index.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

import httpx

from .providers import HTTPProvider, ProviderResult

H10_ZIP_URL = "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H10&filetype=zip"
H10_DATA_MEMBER = "H10_data.xml"

H10_TARGET_SERIES: dict[str, dict[str, str]] = {
    "JRXWTFB_N.B": {
        "series_id": "H10-BROAD-DOLLAR",
        "name": "Nominal Broad Dollar Index",
        "quote_convention": "Index, January 2006 = 100",
    },
    "RXI$US_N.B.EU": {
        "series_id": "H10-EURUSD",
        "name": "Euro exchange rate",
        "quote_convention": "U.S. dollars per euro",
    },
    "RXI_N.B.CH": {
        "series_id": "H10-USDCNY",
        "name": "Chinese yuan exchange rate",
        "quote_convention": "Chinese yuan per U.S. dollar",
    },
    "RXI_N.B.JA": {
        "series_id": "H10-USDJPY",
        "name": "Japanese yen exchange rate",
        "quote_convention": "Japanese yen per U.S. dollar",
    },
}

H10_OBSERVATION_STATUSES = {
    "A": "Normal",
    "NA": "Not available",
    "NC": "Not calculable",
    "ND": "No data",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decimal_or_none(raw_value: str | None, status: str) -> Decimal | None:
    # H.10 encodes unavailable observations as numeric -9999 together with ND.
    # The status is therefore authoritative and must be checked before parsing.
    if status != "A" or raw_value is None:
        return None
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


class FederalReserveH10Provider(HTTPProvider):
    """Fetch selected daily H.10 FX reference series directly from the Board."""

    key = "federal-reserve"
    base_url = "https://www.federalreserve.gov"
    archive_path = "/datadownload/Output.aspx"
    archive_params = {"rel": "H10", "filetype": "zip"}

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 90.0,
        max_archive_bytes: int = 16 * 1024 * 1024,
        max_xml_bytes: int = 64 * 1024 * 1024,
        max_download_seconds: float = 180.0,
    ) -> None:
        self.max_archive_bytes = max_archive_bytes
        self.max_xml_bytes = max_xml_bytes
        self.max_download_seconds = max_download_seconds
        super().__init__(
            client=client,
            timeout=timeout,
            headers={
                "Accept": "application/zip, application/x-zip-compressed",
                "User-Agent": "AtlasMacro/0.1 H10 data downloader",
            },
        )

    def h10(self, *, series_ids: Iterable[str] | None = None) -> ProviderResult:
        dataset = "h10"
        requested_source = H10_TARGET_SERIES if series_ids is None else series_ids
        requested = tuple(dict.fromkeys(requested_source))
        unsupported = sorted(set(requested) - H10_TARGET_SERIES.keys())
        if unsupported:
            return ProviderResult.failure(
                self.key,
                dataset,
                f"unsupported H.10 series: {', '.join(unsupported)}",
            )
        if not requested:
            return ProviderResult.failure(self.key, dataset, "no H.10 series requested")

        try:
            with tempfile.TemporaryFile(mode="w+b") as archive_file:
                archive_size, archive_sha256 = self._download_archive(archive_file)
                archive_file.seek(0)
                records, metadata = self._parse_archive(archive_file, requested)
        except (httpx.HTTPError, OSError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
            return ProviderResult(
                provider=self.key,
                dataset=dataset,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                metadata={"source_url": H10_ZIP_URL},
            )

        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "source_url": H10_ZIP_URL,
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
                **metadata,
            },
        )

    def _download_archive(self, destination: Any) -> tuple[int, str]:
        digest = hashlib.sha256()
        archive_size = 0
        started_at = time.monotonic()
        with self.client.stream("GET", self.archive_path, params=self.archive_params) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if time.monotonic() - started_at > self.max_download_seconds:
                    raise OSError("H.10 ZIP exceeded configured download time")
                archive_size += len(chunk)
                if archive_size > self.max_archive_bytes:
                    raise OSError("H.10 ZIP exceeded configured compressed-size limit")
                destination.write(chunk)
                digest.update(chunk)
        return archive_size, digest.hexdigest()

    def _parse_archive(
        self,
        archive_file: Any,
        requested: Iterable[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        requested_set = set(requested)
        records: list[dict[str, Any]] = []
        found: set[str] = set()
        status_counts: Counter[str] = Counter()
        prepared_at = ""
        missing_observations = 0

        with zipfile.ZipFile(archive_file) as archive:
            member = self._data_member(archive)
            if member.file_size > self.max_xml_bytes:
                raise OSError("H.10 XML exceeded configured expanded-size limit")
            with archive.open(member) as xml_stream:
                active: dict[str, Any] | None = None
                for event, element in ElementTree.iterparse(
                    xml_stream,
                    events=("start", "end"),
                ):
                    name = _local_name(element.tag)
                    if event == "start" and name == "Series":
                        board_series_id = element.attrib.get("SERIES_NAME", "")
                        active = (
                            {
                                "board_series_id": board_series_id,
                                "series_attributes": dict(element.attrib),
                                "description": "",
                            }
                            if board_series_id in requested_set
                            else None
                        )
                        continue
                    if event != "end":
                        continue
                    if name == "Prepared" and not prepared_at:
                        prepared_at = (element.text or "").strip()
                    elif name == "AnnotationText" and active is not None:
                        if not active["description"]:
                            active["description"] = (element.text or "").strip()
                    elif name == "Obs":
                        if active is not None:
                            record = self._observation_record(active, element.attrib)
                            records.append(record)
                            status_counts[record["status"] or "UNKNOWN"] += 1
                            missing_observations += int(record["is_missing"])
                        element.clear()
                    elif name == "Series":
                        if active is not None:
                            found.add(active["board_series_id"])
                        active = None
                        element.clear()

        missing_series = sorted(requested_set - found)
        return records, {
            "archive_member": member.filename,
            "prepared_at": prepared_at,
            "requested_series": list(requested),
            "found_series": sorted(found),
            "missing_series": missing_series,
            "quality_status": (
                "partial"
                if missing_series
                else "complete_with_missing_observations"
                if missing_observations
                else "complete"
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "missing_observation_count": missing_observations,
        }

    @staticmethod
    def _data_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
        for member in archive.infolist():
            if member.filename.rsplit("/", 1)[-1] == H10_DATA_MEMBER:
                return member
        raise zipfile.BadZipFile(f"{H10_DATA_MEMBER} is missing from H.10 archive")

    @staticmethod
    def _observation_record(
        active: Mapping[str, Any],
        observation: Mapping[str, str],
    ) -> dict[str, Any]:
        board_series_id = str(active["board_series_id"])
        target = H10_TARGET_SERIES[board_series_id]
        attributes = dict(active["series_attributes"])
        status = observation.get("OBS_STATUS", "")
        raw_value = observation.get("OBS_VALUE")
        value = _decimal_or_none(raw_value, status)
        status_label = H10_OBSERVATION_STATUSES.get(status, "Unknown")
        return {
            "series_id": target["series_id"],
            "source_series_id": board_series_id,
            "date": observation.get("TIME_PERIOD"),
            "value": value,
            "status": status,
            "status_label": status_label,
            "is_missing": value is None,
            "metadata": {
                "board_series_id": board_series_id,
                "description": active.get("description") or target["name"],
                "quote_convention": target["quote_convention"],
                "h10_status": status,
                "h10_status_label": status_label,
                "raw_value": raw_value,
                "frequency_code": attributes.get("FREQ"),
                "currency": attributes.get("CURRENCY"),
                "fx_code": attributes.get("FX"),
                "unit": attributes.get("UNIT"),
                "unit_multiplier": attributes.get("UNIT_MULT"),
            },
        }
