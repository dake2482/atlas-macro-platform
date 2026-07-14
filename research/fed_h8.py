"""Direct Federal Reserve Board H.8 commercial-bank data provider.

The Board Data Download Program ZIP is the source of truth.  H.8 history is
downloaded into a temporary file and parsed observation-by-observation so the
compressed archive and expanded XML are never materialized as one in-memory
object.
"""

from __future__ import annotations

import hashlib
import tempfile
import time
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

import httpx

from .providers import HTTPProvider, ProviderResult

H8_ZIP_URL = "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H8&filetype=zip"
H8_DATA_MEMBER = "H8_data.xml"
H8_TARGET_SERIES: dict[str, dict[str, str]] = {
    "B1151NCBA": {
        "series_id": "H8-B1151NCBA",
        "name": "Total assets, all commercial banks, seasonally adjusted",
    }
}
H8_OBSERVATION_STATUSES = {
    "A": "Normal",
    "NA": "Not available",
    "NC": "Not calculable",
    "ND": "No data",
}
H8_REQUIRED_DIMENSIONS = {
    "B1151NCBA": {
        "BG": "CB",
        "CATEGORY": "A",
        "CURRENCY": "USD",
        "FREQ": "19",
        "H8_UNITS": "LEVEL",
        "ITEM": "1151",
        "SA": "SA",
        "UNIT": "Currency",
        "UNIT_MULT": "1000000",
    }
}
H8_RELEASE_FRESHNESS_DAYS = 8
H8_RELEASE_FUTURE_TOLERANCE = timedelta(minutes=5)
H8_MAX_RELEASE_LAG_DAYS = 12


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decimal_or_none(raw_value: str | None, status: str) -> Decimal | None:
    """Treat the Board status as authoritative, including numeric sentinels."""

    if status != "A" or raw_value is None:
        return None
    try:
        value = Decimal(raw_value)
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def validate_h8_release_time(
    raw_prepared_at: Any,
    *,
    fetched_at: datetime,
    observation_dates: Iterable[str],
) -> datetime:
    """Validate the official archive release time against fetch and A rows."""

    if not raw_prepared_at:
        raise ValueError("H.8 XML Prepared timestamp is missing")
    try:
        prepared_at = datetime.fromisoformat(
            str(raw_prepared_at).strip().replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("H.8 XML Prepared timestamp is invalid") from exc
    if prepared_at.tzinfo is None:
        prepared_at = prepared_at.replace(tzinfo=UTC)
    else:
        prepared_at = prepared_at.astimezone(UTC)
    normalized_fetched_at = fetched_at
    if normalized_fetched_at.tzinfo is None:
        normalized_fetched_at = normalized_fetched_at.replace(tzinfo=UTC)
    else:
        normalized_fetched_at = normalized_fetched_at.astimezone(UTC)
    if prepared_at > normalized_fetched_at + H8_RELEASE_FUTURE_TOLERANCE:
        raise ValueError("H.8 XML Prepared timestamp is in the future")
    try:
        parsed_observation_dates = [
            date.fromisoformat(value[:10]) for value in observation_dates
        ]
    except (TypeError, ValueError) as exc:
        raise ValueError("H.8 latest A observation date is invalid") from exc
    if parsed_observation_dates:
        latest_observation = max(parsed_observation_dates)
        release_lag_days = (prepared_at.date() - latest_observation).days
        if release_lag_days < 0:
            raise ValueError(
                "H.8 XML Prepared timestamp predates its latest A observation"
            )
        if release_lag_days > H8_MAX_RELEASE_LAG_DAYS:
            raise ValueError(
                "H.8 latest A observation is too old for the Prepared release: "
                f"{release_lag_days} days"
            )
    return prepared_at


class FederalReserveH8Provider(HTTPProvider):
    """Fetch the authoritative weekly all-commercial-bank asset series."""

    key = "federal-reserve"
    base_url = "https://www.federalreserve.gov"
    archive_path = "/datadownload/Output.aspx"
    archive_params = {"rel": "H8", "filetype": "zip"}

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 90.0,
        max_archive_bytes: int = 32 * 1024 * 1024,
        max_xml_bytes: int = 192 * 1024 * 1024,
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
                "User-Agent": "AtlasMacro/0.1 H8 data downloader",
            },
        )

    def h8(self, *, series_ids: Iterable[str] | None = None) -> ProviderResult:
        dataset = "h8"
        requested_source = H8_TARGET_SERIES if series_ids is None else series_ids
        requested = tuple(dict.fromkeys(requested_source))
        unsupported = sorted(set(requested) - H8_TARGET_SERIES.keys())
        if unsupported:
            return ProviderResult.failure(
                self.key,
                dataset,
                f"unsupported H.8 series: {', '.join(unsupported)}",
            )
        if not requested:
            return ProviderResult.failure(self.key, dataset, "no H.8 series requested")

        try:
            with tempfile.TemporaryFile(mode="w+b") as archive_file:
                archive_size, archive_sha256 = self._download_archive(archive_file)
                archive_file.seek(0)
                records, metadata = self._parse_archive(archive_file, requested)
            fetched_at = datetime.now(UTC)
            source_release_time = validate_h8_release_time(
                metadata.get("prepared_at"),
                fetched_at=fetched_at,
                observation_dates=(
                    str(record.get("date") or "")
                    for record in records
                    if record.get("status") == "A" and record.get("value") is not None
                ),
            )
            for record in records:
                record_metadata = dict(record.get("metadata") or {})
                record_metadata.update(
                    {
                        "source_release_time": source_release_time.isoformat(),
                        "release_freshness_days": H8_RELEASE_FRESHNESS_DAYS,
                    }
                )
                record["metadata"] = record_metadata
            metadata.update(
                {
                    "source_release_time": source_release_time.isoformat(),
                    "release_freshness_days": H8_RELEASE_FRESHNESS_DAYS,
                }
            )
        except (
            httpx.HTTPError,
            OSError,
            ElementTree.ParseError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            return ProviderResult(
                provider=self.key,
                dataset=dataset,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                metadata={"source_url": H8_ZIP_URL},
            )

        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            fetched_at=fetched_at,
            metadata={
                "source_url": H8_ZIP_URL,
                "archive_size": archive_size,
                "archive_sha256": archive_sha256,
                **metadata,
            },
        )

    def _download_archive(self, destination: Any) -> tuple[int, str]:
        digest = hashlib.sha256()
        archive_size = 0
        started_at = time.monotonic()
        with self.client.stream(
            "GET", self.archive_path, params=self.archive_params
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if time.monotonic() - started_at > self.max_download_seconds:
                    raise OSError("H.8 ZIP exceeded configured total download time")
                archive_size += len(chunk)
                if archive_size > self.max_archive_bytes:
                    raise OSError("H.8 ZIP exceeded configured compressed-size limit")
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
        series_counts: Counter[str] = Counter()
        seen_observations: set[tuple[str, date]] = set()
        prepared_at = ""
        missing_observations = 0

        with zipfile.ZipFile(archive_file) as archive:
            member = self._data_member(archive)
            if member.file_size > self.max_xml_bytes:
                raise OSError("H.8 XML exceeded configured expanded-size limit")
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
                            series_counts[board_series_id] += 1
                            if series_counts[board_series_id] > 1:
                                raise ValueError(
                                    "H.8 requested Board series must appear exactly "
                                    f"once: {board_series_id}"
                                )
                            attributes = dict(element.attrib)
                            self._validate_series_dimensions(
                                board_series_id, attributes
                            )
                            active = {
                                "board_series_id": board_series_id,
                                "series_attributes": attributes,
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
                            board_series_id = str(active["board_series_id"])
                            period = self._validate_observation_period(
                                board_series_id,
                                element.attrib.get("TIME_PERIOD"),
                            )
                            observation_key = (board_series_id, period)
                            if observation_key in seen_observations:
                                raise ValueError(
                                    "H.8 duplicate observation for "
                                    f"{board_series_id} on {period.isoformat()}"
                                )
                            seen_observations.add(observation_key)
                            record = self._observation_record(active, element.attrib)
                            records.append(record)
                            status_counts[record["status"] or "MISSING"] += 1
                            missing_observations += int(record["is_missing"])
                        element.clear()
                    elif name == "Series":
                        if active is not None:
                            found.add(active["board_series_id"])
                        active = None
                        element.clear()

        invalid_series_counts = {
            series_id: series_counts[series_id]
            for series_id in sorted(requested_set)
            if series_counts[series_id] != 1
        }
        if invalid_series_counts:
            raise ValueError(
                "H.8 requested Board series must appear exactly once: "
                f"{invalid_series_counts}"
            )
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
        members = [
            member
            for member in archive.infolist()
            if member.filename.rsplit("/", 1)[-1] == H8_DATA_MEMBER
        ]
        if len(members) != 1:
            raise zipfile.BadZipFile(
                "H.8 archive must contain exactly one "
                f"{H8_DATA_MEMBER}; found {len(members)}"
            )
        return members[0]

    @staticmethod
    def _validate_series_dimensions(
        board_series_id: str, attributes: Mapping[str, str]
    ) -> None:
        expected = H8_REQUIRED_DIMENSIONS[board_series_id]
        mismatches = {
            key: {"expected": expected_value, "actual": attributes.get(key)}
            for key, expected_value in expected.items()
            if attributes.get(key) != expected_value
        }
        if mismatches:
            raise ValueError(
                f"H.8 semantic dimension drift for {board_series_id}: {mismatches}"
            )

    @staticmethod
    def _validate_observation_period(
        board_series_id: str, raw_period: str | None
    ) -> date:
        try:
            period = date.fromisoformat(str(raw_period or ""))
        except ValueError as exc:
            raise ValueError(
                f"H.8 invalid observation date for {board_series_id}: {raw_period!r}"
            ) from exc
        if period.weekday() != 2:
            raise ValueError(
                f"H.8 non-Wednesday observation for {board_series_id}: "
                f"{period.isoformat()}"
            )
        return period

    @staticmethod
    def _observation_record(
        active: Mapping[str, Any], observation: Mapping[str, str]
    ) -> dict[str, Any]:
        board_series_id = str(active["board_series_id"])
        target = H8_TARGET_SERIES[board_series_id]
        attributes = dict(active["series_attributes"])
        status = observation.get("OBS_STATUS", "")
        if status not in H8_OBSERVATION_STATUSES:
            raise ValueError(
                f"H.8 unknown observation status for {board_series_id}: {status!r}"
            )
        raw_value = observation.get("OBS_VALUE")
        value = _decimal_or_none(raw_value, status)
        if status == "A" and value is None:
            raise ValueError(
                f"H.8 A-status observation is not numeric for {board_series_id}"
            )
        status_label = H8_OBSERVATION_STATUSES.get(status, "Unknown")
        board_dimensions = {
            key: attributes.get(key)
            for key in (
                "BG",
                "CATEGORY",
                "CURRENCY",
                "FREQ",
                "H8_UNITS",
                "ITEM",
                "SA",
                "UNIT",
                "UNIT_MULT",
            )
        }
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
                "h8_status": status,
                "h8_status_label": status_label,
                "raw_value": raw_value,
                "bg": attributes.get("BG"),
                "category": attributes.get("CATEGORY"),
                "currency": attributes.get("CURRENCY"),
                "frequency_code": attributes.get("FREQ"),
                "h8_units": attributes.get("H8_UNITS"),
                "item": attributes.get("ITEM"),
                "seasonal_adjustment": attributes.get("SA"),
                "unit": attributes.get("UNIT"),
                "unit_multiplier": attributes.get("UNIT_MULT"),
                "board_dimensions": board_dimensions,
            },
        }
