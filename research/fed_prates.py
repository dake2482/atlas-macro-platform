"""Federal Reserve Board Policy Rates (PRATES) data provider.

The Board publishes the IORB rate in its Data Download Program.  This adapter
reads the official SDMX ZIP directly and retains both the Board series ID and
the archive hash; it does not route the value through FRED.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
import zipfile
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

import httpx

from .providers import HTTPProvider, ProviderResult

PRATES_ZIP_URL = (
    "https://www.federalreserve.gov/datadownload/Output.aspx?rel=PRATES&filetype=zip"
)
PRATES_DATA_MEMBER = "PRATES_data.xml"
PRATES_TARGET_SERIES = {
    "RESBM_N.D": {
        "series_id": "IORB",
        "name": "Interest rate on reserve balances",
    }
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decimal_or_none(raw_value: str | None, status: str) -> Decimal | None:
    # DDP releases can encode unavailable observations as numeric sentinels
    # while carrying the real meaning in OBS_STATUS. Treat status as
    # authoritative so a value such as -9999 can never be published as IORB.
    if status not in {"", "A"}:
        return None
    if raw_value is None or raw_value.strip().upper() in {"", ".", "NA", "NC", "ND"}:
        return None
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


class FederalReservePRATESProvider(HTTPProvider):
    """Fetch the daily IORB series directly from the Board's PRATES package."""

    key = "federal-reserve"
    base_url = "https://www.federalreserve.gov"
    archive_path = "/datadownload/Output.aspx"
    archive_params = {"rel": "PRATES", "filetype": "zip"}

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        max_archive_bytes: int = 8 * 1024 * 1024,
        max_xml_bytes: int = 32 * 1024 * 1024,
        max_download_seconds: float = 120.0,
    ) -> None:
        self.max_archive_bytes = max_archive_bytes
        self.max_xml_bytes = max_xml_bytes
        self.max_download_seconds = max_download_seconds
        super().__init__(
            client=client,
            timeout=timeout,
            headers={
                "Accept": "application/zip, application/x-zip-compressed",
                "User-Agent": "AtlasMacro/0.1 PRATES data downloader",
            },
        )

    def iorb(self) -> ProviderResult:
        dataset = "prates:iorb"
        try:
            with tempfile.TemporaryFile(mode="w+b") as archive_file:
                archive_size, archive_sha256 = self._download_archive(archive_file)
                archive_file.seek(0)
                records, metadata = self._parse_archive(archive_file)
        except (httpx.HTTPError, OSError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
            return ProviderResult(
                provider=self.key,
                dataset=dataset,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                metadata={"source_url": PRATES_ZIP_URL},
            )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "source_url": PRATES_ZIP_URL,
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
                    raise OSError("PRATES ZIP exceeded configured download time")
                archive_size += len(chunk)
                if archive_size > self.max_archive_bytes:
                    raise OSError("PRATES ZIP exceeded configured compressed-size limit")
                destination.write(chunk)
                digest.update(chunk)
        return archive_size, digest.hexdigest()

    def _parse_archive(self, archive_file: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        records: list[dict[str, Any]] = []
        found: set[str] = set()
        status_counts: Counter[str] = Counter()
        prepared_at = ""
        missing_observations = 0

        with zipfile.ZipFile(archive_file) as archive:
            member = self._data_member(archive)
            if member.file_size > self.max_xml_bytes:
                raise OSError("PRATES XML exceeded configured expanded-size limit")
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
                            if board_series_id in PRATES_TARGET_SERIES
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

        missing_series = sorted(PRATES_TARGET_SERIES.keys() - found)
        return records, {
            "archive_member": member.filename,
            "prepared_at": prepared_at,
            "requested_series": sorted(PRATES_TARGET_SERIES),
            "found_series": sorted(found),
            "missing_series": missing_series,
            "quality_status": "partial" if missing_series else "complete",
            "status_counts": dict(sorted(status_counts.items())),
            "missing_observation_count": missing_observations,
        }

    @staticmethod
    def _data_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
        for member in archive.infolist():
            if member.filename.rsplit("/", 1)[-1] == PRATES_DATA_MEMBER:
                return member
        raise zipfile.BadZipFile(f"{PRATES_DATA_MEMBER} is missing from PRATES archive")

    @staticmethod
    def _observation_record(
        active: Mapping[str, Any], observation: Mapping[str, str]
    ) -> dict[str, Any]:
        board_series_id = str(active["board_series_id"])
        target = PRATES_TARGET_SERIES[board_series_id]
        attributes = dict(active["series_attributes"])
        status = observation.get("OBS_STATUS", "")
        raw_value = observation.get("OBS_VALUE")
        value = _decimal_or_none(raw_value, status)
        return {
            "series_id": target["series_id"],
            "source_series_id": board_series_id,
            "date": observation.get("TIME_PERIOD"),
            "value": value,
            "status": status,
            "is_missing": value is None,
            "metadata": {
                "board_series_id": board_series_id,
                "description": active.get("description") or target["name"],
                "prates_status": status,
                "raw_value": raw_value,
                "frequency_code": attributes.get("FREQ"),
                "unit": attributes.get("UNIT"),
                "unit_multiplier": attributes.get("UNIT_MULT"),
                "currency": attributes.get("CURRENCY"),
                "interest_rate_type": attributes.get("INT_RATES_PAID"),
            },
        }
