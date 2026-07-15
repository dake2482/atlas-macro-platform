"""Direct Federal Reserve Board H.10 foreign-exchange reference data.

The Board's Data Download Program is the authoritative source for these daily
reference series.  They are deliberately named ``H10-*`` inside Atlas Macro so
the public dashboard cannot mistake them for an exchange feed, an intraday
spot quote, or ICE's licensed DXY index.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
import time
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

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

# These compact DDP attributes identify the source series. They are retained
# separately from the human display convention: the broad-index XML UNIT code
# still carries an older internal base name while the current H.10 convention
# is January 2006 = 100.
H10_SOURCE_ATTRIBUTES: dict[str, dict[str, str]] = {
    "JRXWTFB_N.B": {
        "FREQ": "9",
        "CURRENCY": "NA",
        "FX": "BRD",
        "UNIT": "Index:_1997_Jan_100",
        "UNIT_MULT": "1",
    },
    "RXI$US_N.B.EU": {
        "FREQ": "9",
        "CURRENCY": "EUR",
        "FX": "EUR",
        "UNIT": "Currency",
        "UNIT_MULT": "1",
    },
    "RXI_N.B.CH": {
        "FREQ": "9",
        "CURRENCY": "CNY",
        "FX": "CNY",
        "UNIT": "Currency",
        "UNIT_MULT": "1",
    },
    "RXI_N.B.JA": {
        "FREQ": "9",
        "CURRENCY": "JPY",
        "FX": "JPY",
        "UNIT": "Currency",
        "UNIT_MULT": "1",
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
        max_compression_ratio: int = 200,
    ) -> None:
        self.max_archive_bytes = max_archive_bytes
        self.max_xml_bytes = max_xml_bytes
        self.max_download_seconds = max_download_seconds
        self.max_compression_ratio = max_compression_ratio
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
                records, metadata = self._parse_archive(
                    archive_file,
                    requested,
                    fetched_at=datetime.now(UTC),
                    archive_size=archive_size,
                )
                archive_file.seek(0)
                raw_bytes = archive_file.read()
                if (
                    len(raw_bytes) != archive_size
                    or hashlib.sha256(raw_bytes).hexdigest() != archive_sha256
                ):
                    raise OSError("H.10 ZIP bytes changed before publication")
        except (
            ArithmeticError,
            httpx.HTTPError,
            OSError,
            ElementTree.ParseError,
            TypeError,
            ValueError,
            RuntimeError,
            NotImplementedError,
            zipfile.BadZipFile,
        ) as exc:
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
            raw_bytes=raw_bytes,
            metadata={
                "source_url": H10_ZIP_URL,
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
                "content_type": "application/zip",
                "byte_length": archive_size,
                "sha256": archive_sha256,
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
        *,
        fetched_at: datetime | None = None,
        archive_size: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        requested_set = set(requested)
        records: list[dict[str, Any]] = []
        found: set[str] = set()
        status_counts: Counter[str] = Counter()
        prepared_at = ""
        missing_observations = 0
        prepared_values: list[str] = []
        encountered: set[str] = set()
        row_identities: set[tuple[str, date]] = set()
        latest_valid_dates: dict[str, date] = {}
        effective_fetched_at = fetched_at or datetime.now(UTC)
        if effective_fetched_at.tzinfo is None:
            effective_fetched_at = effective_fetched_at.replace(tzinfo=UTC)

        with zipfile.ZipFile(archive_file) as archive:
            if archive_size is not None and (
                archive_size <= 0 or archive_size > self.max_archive_bytes
            ):
                raise OSError("H.10 ZIP has an invalid compressed size")
            member = self._data_member(archive)
            if member.flag_bits & 0x1:
                raise ValueError("H.10 XML member must not be encrypted")
            if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("H.10 XML member uses unsupported compression")
            if member.file_size > self.max_xml_bytes:
                raise OSError("H.10 XML exceeded configured expanded-size limit")
            compressed_size = member.compress_size or 1
            if (
                member.file_size > 1024 * 1024
                and member.file_size / compressed_size > self.max_compression_ratio
            ):
                raise OSError("H.10 XML exceeded configured compression-ratio limit")
            with archive.open(member) as source_stream:
                member_bytes = source_stream.read(self.max_xml_bytes + 1)
            if len(member_bytes) != member.file_size:
                raise OSError("H.10 XML member size changed while reading archive")
            member_sha256 = hashlib.sha256(member_bytes).hexdigest()
            with io.BytesIO(member_bytes) as xml_stream:
                active: dict[str, Any] | None = None
                for event, element in ElementTree.iterparse(
                    xml_stream,
                    events=("start", "end"),
                ):
                    name = _local_name(element.tag)
                    if event == "start" and name == "Series":
                        board_series_id = element.attrib.get("SERIES_NAME", "")
                        if board_series_id in requested_set:
                            if board_series_id in encountered:
                                raise ValueError(
                                    f"duplicate H.10 series block: {board_series_id}"
                                )
                            self._validate_series_attributes(
                                board_series_id, element.attrib
                            )
                            encountered.add(board_series_id)
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
                    if name == "Prepared":
                        prepared_values.append((element.text or "").strip())
                    elif name == "AnnotationText" and active is not None:
                        if not active["description"]:
                            active["description"] = (element.text or "").strip()
                    elif name == "Obs":
                        if active is not None:
                            record = self._observation_record(active, element.attrib)
                            try:
                                period = date.fromisoformat(str(record["date"] or ""))
                            except ValueError as exc:
                                raise ValueError(
                                    "H.10 observation date is not canonical"
                                ) from exc
                            if str(record["date"]) != period.isoformat():
                                raise ValueError(
                                    "H.10 observation date is not canonical"
                                )
                            if period > effective_fetched_at.astimezone(
                                ZoneInfo("America/New_York")
                            ).date():
                                raise ValueError("H.10 observation is future-dated")
                            identity = (active["board_series_id"], period)
                            if identity in row_identities:
                                raise ValueError(
                                    "duplicate H.10 series/date observation"
                                )
                            row_identities.add(identity)
                            if not record["is_missing"]:
                                latest_valid_dates[active["board_series_id"]] = max(
                                    period,
                                    latest_valid_dates.get(
                                        active["board_series_id"], period
                                    ),
                                )
                            records.append(record)
                            status_counts[record["status"] or "UNKNOWN"] += 1
                            missing_observations += int(record["is_missing"])
                        element.clear()
                    elif name == "Series":
                        if active is not None:
                            found.add(active["board_series_id"])
                        active = None
                        element.clear()

        if len(prepared_values) != 1:
            raise ValueError("H.10 archive requires exactly one Prepared timestamp")
        prepared_at = prepared_values[0]
        missing_series = sorted(requested_set - found)
        if missing_series:
            raise ValueError(
                f"missing required H.10 series: {', '.join(missing_series)}"
            )
        missing_latest = sorted(requested_set - latest_valid_dates.keys())
        if missing_latest:
            raise ValueError(
                "H.10 series lack a valid latest observation: "
                + ", ".join(missing_latest)
            )
        prepared = self._validate_prepared_at(prepared_at, effective_fetched_at)
        if prepared.astimezone(ZoneInfo("America/New_York")).date() < max(
            latest_valid_dates.values()
        ):
            raise ValueError("H.10 Prepared date precedes a latest valid observation")
        return records, {
            "archive_member": member.filename,
            "archive_member_size": member.file_size,
            "archive_member_sha256": member_sha256,
            "prepared_at": prepared_at,
            "source_prepared_at": prepared.isoformat(),
            "requested_series": list(requested),
            "found_series": sorted(found),
            "missing_series": [],
            "latest_valid_dates": {
                key: value.isoformat()
                for key, value in sorted(latest_valid_dates.items())
            },
            "quality_status": (
                "complete_with_missing_observations"
                if missing_observations
                else "complete"
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "missing_observation_count": missing_observations,
        }

    @staticmethod
    def _data_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
        matches = [
            member
            for member in archive.infolist()
            if PurePosixPath(member.filename).name == H10_DATA_MEMBER
            and not member.is_dir()
        ]
        if len(matches) != 1:
            raise zipfile.BadZipFile(
                f"H.10 archive requires exactly one {H10_DATA_MEMBER} member"
            )
        member = matches[0]
        if member.filename != H10_DATA_MEMBER:
            raise zipfile.BadZipFile(
                f"H.10 archive requires {H10_DATA_MEMBER} at the ZIP root"
            )
        return member

    @staticmethod
    def _validate_series_attributes(
        board_series_id: str,
        attributes: Mapping[str, str],
    ) -> None:
        expected = H10_SOURCE_ATTRIBUTES[board_series_id]
        observed = {key: str(attributes.get(key) or "") for key in expected}
        if observed != expected:
            raise ValueError(
                f"H.10 series attributes changed for {board_series_id}"
            )

    @staticmethod
    def _validate_prepared_at(raw_value: str, fetched_at: datetime) -> datetime:
        if not raw_value:
            raise ValueError("H.10 archive lacks Prepared timestamp")
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("H.10 Prepared timestamp is malformed") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
        fetched = (
            fetched_at
            if fetched_at.tzinfo is not None
            else fetched_at.replace(tzinfo=UTC)
        )
        if parsed > fetched + timedelta(minutes=5):
            raise ValueError("H.10 Prepared timestamp is future-dated")
        return parsed.astimezone(UTC)

    @staticmethod
    def _observation_record(
        active: Mapping[str, Any],
        observation: Mapping[str, str],
    ) -> dict[str, Any]:
        board_series_id = str(active["board_series_id"])
        target = H10_TARGET_SERIES[board_series_id]
        attributes = dict(active["series_attributes"])
        status = observation.get("OBS_STATUS", "")
        if status not in H10_OBSERVATION_STATUSES:
            raise ValueError(f"unsupported H.10 observation status: {status}")
        raw_value = observation.get("OBS_VALUE")
        value = _decimal_or_none(raw_value, status)
        if status == "A" and (value is None or not value.is_finite() or value <= 0):
            raise ValueError("H.10 authoritative observation value is invalid")
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
