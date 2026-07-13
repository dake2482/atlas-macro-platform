"""Direct Federal Reserve Board H.4.1 balance-sheet data provider.

The Board's fixed ``FRB_H41.zip`` endpoint is the source of truth here.  The
archive contains a roughly 125 MB SDMX XML member, so the response is spooled
to a temporary file and parsed observation-by-observation.  Neither the ZIP
payload nor the expanded XML document is materialized as one large ``bytes``
object.
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

H41_ZIP_URL = "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H41&filetype=zip"
H41_DATA_MEMBER = "H41_data.xml"

# ``series_id`` intentionally uses the familiar application/FRED-compatible
# identifier already used by Atlas Macro.  ``source_series_id`` and metadata
# always retain the authoritative Board DDP identifier, so the source is never
# represented as FRED data.
H41_TARGET_SERIES: dict[str, dict[str, str]] = {
    "RESPPMA_N.WW": {
        "series_id": "WALCL",
        "name": "Federal Reserve total assets",
        "fred_series_id": "WALCL",
    },
    "RESPPALGUO_N.WW": {
        "series_id": "WSHOTSL",
        "name": "U.S. Treasury securities held outright",
        "fred_series_id": "WSHOTSL",
    },
    "RESPPALGASMO_N.WW": {
        "series_id": "WSHOMCB",
        "name": "Mortgage-backed securities held outright",
        "fred_series_id": "WSHOMCB",
    },
    "RESH4R_N.WW": {
        "series_id": "WRBWFRBL",
        "name": "Reserve balances with Federal Reserve Banks",
        "fred_series_id": "WRBWFRBL",
    },
    "RESPPLLDT_N.WW": {
        "series_id": "WDTGAL",
        "name": "U.S. Treasury General Account",
        "fred_series_id": "WDTGAL",
    },
    "RESH4SCS_N.WW": {
        "series_id": "SWPT",
        "name": "Central bank liquidity swaps",
        "fred_series_id": "SWPT",
    },
}

H41_OBSERVATION_STATUSES = {
    "A": "Normal",
    "NC": "Not calculable",
    "NA": "Not available",
    "ND": "No data",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decimal_or_none(raw_value: str | None) -> Decimal | None:
    if raw_value is None or raw_value.strip().upper() in {"", ".", "NA", "NC", "ND"}:
        return None
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


class FederalReserveH41Provider(HTTPProvider):
    """Fetch six core H.4.1 balance-sheet series directly from the Board."""

    key = "federal-reserve"
    base_url = "https://www.federalreserve.gov"
    archive_path = "/datadownload/Output.aspx"
    archive_params = {"rel": "H41", "filetype": "zip"}

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
        max_archive_bytes: int = 64 * 1024 * 1024,
        max_xml_bytes: int = 512 * 1024 * 1024,
        max_download_seconds: float = 300.0,
    ) -> None:
        self.max_archive_bytes = max_archive_bytes
        self.max_xml_bytes = max_xml_bytes
        self.max_download_seconds = max_download_seconds
        super().__init__(
            client=client,
            timeout=timeout,
            headers={
                "Accept": "application/zip, application/x-zip-compressed",
                "User-Agent": "AtlasMacro/0.1 H41 data downloader",
            },
        )

    def h41(self, *, series_ids: Iterable[str] | None = None) -> ProviderResult:
        """Download and normalize the requested subset of the six target series."""

        dataset = "h41"
        requested_source = H41_TARGET_SERIES if series_ids is None else series_ids
        requested = tuple(dict.fromkeys(requested_source))
        unsupported = sorted(set(requested) - H41_TARGET_SERIES.keys())
        if unsupported:
            return ProviderResult.failure(
                self.key,
                dataset,
                f"unsupported H.4.1 series: {', '.join(unsupported)}",
            )
        if not requested:
            return ProviderResult.failure(self.key, dataset, "no H.4.1 series requested")

        try:
            with tempfile.TemporaryFile(mode="w+b") as archive_file:
                archive_size, archive_sha256 = self._download_archive(archive_file)
                archive_file.seek(0)
                records, parse_metadata = self._parse_archive(archive_file, requested)
        except (httpx.HTTPError, OSError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
            return ProviderResult(
                provider=self.key,
                dataset=dataset,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                metadata={"source_url": H41_ZIP_URL},
            )

        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "source_url": H41_ZIP_URL,
                "archive_member": parse_metadata.pop("archive_member"),
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
                **parse_metadata,
            },
        )

    def _download_archive(self, destination: Any) -> tuple[int, str]:
        digest = hashlib.sha256()
        archive_size = 0
        started_at = time.monotonic()
        with self.client.stream(
            "GET",
            self.archive_path,
            params=self.archive_params,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if time.monotonic() - started_at > self.max_download_seconds:
                    raise OSError(
                        "H.4.1 ZIP exceeded configured total download time "
                        f"({self.max_download_seconds:g} seconds)"
                    )
                archive_size += len(chunk)
                if archive_size > self.max_archive_bytes:
                    raise OSError(
                        "H.4.1 ZIP exceeded configured compressed-size limit "
                        f"({self.max_archive_bytes} bytes)"
                    )
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
        missing_observations = 0
        prepared_at = ""

        with zipfile.ZipFile(archive_file) as archive:
            member = self._data_member(archive)
            if member.file_size > self.max_xml_bytes:
                raise OSError(
                    "H.4.1 XML exceeded configured expanded-size limit "
                    f"({self.max_xml_bytes} bytes)"
                )
            with archive.open(member) as xml_stream:
                active: dict[str, Any] | None = None
                for event, element in ElementTree.iterparse(
                    xml_stream,
                    events=("start", "end"),
                ):
                    name = _local_name(element.tag)
                    if event == "start" and name == "Series":
                        board_series_id = element.attrib.get("SERIES_NAME", "")
                        if board_series_id in requested_set:
                            active = {
                                "board_series_id": board_series_id,
                                "series_attributes": dict(element.attrib),
                                "description": "",
                            }
                        else:
                            active = None
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
                        # Clearing each observation keeps memory bounded even while a
                        # source series contains its complete multi-decade history.
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

    def _data_member(self, archive: zipfile.ZipFile) -> zipfile.ZipInfo:
        for member in archive.infolist():
            if member.filename.rsplit("/", 1)[-1] == H41_DATA_MEMBER:
                return member
        raise zipfile.BadZipFile(f"{H41_DATA_MEMBER} is missing from H.4.1 archive")

    @staticmethod
    def _observation_record(
        active: Mapping[str, Any],
        observation: Mapping[str, str],
    ) -> dict[str, Any]:
        board_series_id = str(active["board_series_id"])
        target = H41_TARGET_SERIES[board_series_id]
        attributes = dict(active["series_attributes"])
        raw_value = observation.get("OBS_VALUE")
        status = observation.get("OBS_STATUS", "")
        value = _decimal_or_none(raw_value)
        status_label = H41_OBSERVATION_STATUSES.get(status, "Unknown")
        metadata = {
            "board_series_id": board_series_id,
            "fred_series_id": target["fred_series_id"],
            "description": active.get("description") or target["name"],
            "h41_status": status,
            "h41_status_label": status_label,
            "raw_value": raw_value,
            "frequency_code": attributes.get("FREQ"),
            "series_type": attributes.get("SERIESTYPE"),
            "unit": attributes.get("UNIT"),
            "unit_multiplier": attributes.get("UNIT_MULT"),
            "currency": attributes.get("CURRENCY"),
            "category": attributes.get("CATEGORY"),
            "subcategory": attributes.get("SUBCATEGORY"),
            "component": attributes.get("COMPONENT"),
            "distribution": attributes.get("DISTRIBUTION"),
        }
        return {
            "series_id": target["series_id"],
            "source_series_id": board_series_id,
            "date": observation.get("TIME_PERIOD"),
            "value": value,
            "status": status,
            "status_label": status_label,
            "is_missing": value is None,
            "metadata": metadata,
        }
