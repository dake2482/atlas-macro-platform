"""Official U.S. unemployment-insurance claims ingestion.

The ETA historical XML can return HTTP 200 while lagging the current weekly
release.  This adapter therefore combines the national XML history with the
latest immutable DOL news-release PDF and lets the release rows replace the
overlapping tail of the XML vintage.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfReader

from .providers import HTTPProvider, ProviderResult

HISTORY_URL = "https://oui.doleta.gov/unemploy/wkclaims/report.asp"
CURRENT_RELEASE_URL = "https://www.dol.gov/ui/data.pdf"
MAX_XML_BYTES = 8_000_000
MAX_PDF_BYTES = 8_000_000

INITIAL_SA = "DOL-UI-INITIAL-CLAIMS-SA"
INITIAL_4WK = "DOL-UI-INITIAL-CLAIMS-SA-4WK"
CONTINUED_SA = "DOL-UI-CONTINUED-CLAIMS-SA"
CONTINUED_4WK = "DOL-UI-CONTINUED-CLAIMS-SA-4WK"
IUR_SA = "DOL-UI-IUR-SA"

REQUIRED_SERIES = frozenset(
    {INITIAL_SA, INITIAL_4WK, CONTINUED_SA, CONTINUED_4WK, IUR_SA}
)

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
_RELEASE_ROW = re.compile(
    rf"^(?P<date>(?:{_MONTHS}) \d{{1,2}}, \d{{4}}) "
    r"(?P<initial>[\d,]+) (?P<initial_change>[+\-]?\d+) "
    r"(?P<initial_avg>[\d,.]+)"
    r"(?: (?P<continued>[\d,]+) (?P<continued_change>[+\-]?\d+) "
    r"(?P<continued_avg>[\d,.]+) (?P<iur>[\d.]+))?$"
)


def _number(value: str | None) -> Decimal | None:
    cleaned = (value or "").replace("\xa0", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return number if number >= 0 else None


def _parse_us_date(value: str) -> date:
    for pattern in ("%m/%d/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError(f"invalid DOL date: {value!r}")


def _record(
    series_id: str,
    week: date,
    value: Decimal,
    *,
    estimate_status: str,
    source_url: str,
    release_date: date | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "unit": "%" if series_id == IUR_SA else "claims",
        "frequency": "weekly",
        "seasonal_adjustment": "seasonally adjusted",
        "estimate_status": estimate_status,
        "official_source_url": source_url,
        "observation_semantics": "week ending",
    }
    if series_id.startswith("DOL-UI-CONTINUED"):
        metadata["measure_semantics"] = (
            "continued weeks claimed; not a count of unique recipients"
        )
    if release_date is not None:
        released_at = datetime.combine(
            release_date,
            time(8, 30),
            tzinfo=ZoneInfo("America/New_York"),
        )
        metadata.update(
            {
                "source_revision_date": release_date.isoformat(),
                "source_release_time": released_at.isoformat(),
                "release_freshness_days": 8,
            }
        )
    return {
        "series_id": series_id,
        "date": week.isoformat(),
        "value": value,
        "quality_status": (
            "estimated" if estimate_status == "advance" else "fresh"
        ),
        "metadata": metadata,
    }


def parse_weekly_claims_history_xml(content: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the national ETA 539 XML without resolving external resources."""

    upper = content[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DOL XML contains a forbidden document type or entity")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid DOL XML: {exc}") from exc
    if root.tag != "r539cyNational":
        raise ValueError(f"unexpected DOL XML root: {root.tag}")
    run_date_raw = root.attrib.get("rundate", "")
    run_date = _parse_us_date(run_date_raw)

    rows: list[dict[str, Any]] = []
    weeks_seen: set[date] = set()
    ordered_weeks: list[date] = []
    skipped_future_rows = 0
    for node in root.findall("week"):
        week = _parse_us_date(node.findtext("weekEnded") or "")
        if week.weekday() != 5:
            raise ValueError(f"DOL week is not a Saturday: {week}")
        if week in weeks_seen:
            raise ValueError(f"duplicate DOL week: {week}")
        weeks_seen.add(week)
        ordered_weeks.append(week)
        values = {
            INITIAL_SA: _number(node.findtext("InitialClaims/SA")),
            INITIAL_4WK: _number(node.findtext("InitialClaims/SA4WK")),
            CONTINUED_SA: _number(node.findtext("ContinuedClaims/SA")),
            CONTINUED_4WK: _number(node.findtext("ContinuedClaims/SA4WK")),
            IUR_SA: _number(node.findtext("IUR/SA")),
        }
        present = {key for key, value in values.items() if value is not None}
        if not present:
            skipped_future_rows += 1
            continue
        if present != REQUIRED_SERIES:
            raise ValueError(
                f"incomplete DOL national week {week}: missing "
                f"{sorted(REQUIRED_SERIES - present)}"
            )
        for series_id, value in values.items():
            assert value is not None
            rows.append(
                _record(
                    series_id,
                    week,
                    value,
                    estimate_status="eta539_current_vintage",
                    source_url=HISTORY_URL,
                )
            )
    if ordered_weeks != sorted(ordered_weeks):
        raise ValueError("DOL XML weeks are not strictly ascending")
    if not rows:
        raise ValueError("DOL XML contains no complete national observations")
    return rows, {
        "xml_run_date": run_date.isoformat(),
        "history_latest_week": max(
            date.fromisoformat(item["date"]) for item in rows
        ).isoformat(),
        "future_rows_skipped": skipped_future_rows,
    }


def _validate_four_week_average(
    rows: list[dict[str, Any]],
    *,
    value_key: str,
    average_key: str,
) -> None:
    for index in range(3, len(rows)):
        expected = sum(
            (item[value_key] for item in rows[index - 3 : index + 1]),
            Decimal("0"),
        ) / Decimal("4")
        if rows[index][average_key] != expected:
            raise ValueError(
                f"DOL four-week average mismatch for {rows[index]['date']}: "
                f"{rows[index][average_key]} != {expected}"
            )


def parse_weekly_claims_release_text(
    text: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the release table extracted from the official weekly PDF."""

    normalized = text.replace("\u2212", "-").replace("\u2013", "-")
    release_match = re.search(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+"
        rf"((?:{_MONTHS}) \d{{1,2}}, \d{{4}})",
        normalized,
    )
    if release_match is None:
        raise ValueError("DOL PDF release date was not found")
    release_date = _parse_us_date(release_match.group(1))
    marker = "Seasonally Adjusted US Weekly UI Claims (in thousands)"
    if marker not in normalized:
        raise ValueError("DOL PDF seasonally adjusted table was not found")
    table_text = normalized.split(marker, 1)[1]
    table_text = table_text.split("INITIAL CLAIMS FILED DURING WEEK ENDED", 1)[0]

    parsed: list[dict[str, Any]] = []
    for raw_line in table_text.splitlines():
        line = " ".join(raw_line.split())
        match = _RELEASE_ROW.fullmatch(line)
        if match is None:
            continue
        week = _parse_us_date(match.group("date"))
        initial = _number(match.group("initial"))
        initial_avg = _number(match.group("initial_avg"))
        if initial is None or initial_avg is None:
            raise ValueError(f"invalid DOL initial-claims row: {line}")
        row: dict[str, Any] = {
            "date": week,
            "initial": initial * Decimal("1000"),
            "initial_avg": initial_avg * Decimal("1000"),
        }
        if match.group("continued") is not None:
            continued = _number(match.group("continued"))
            continued_avg = _number(match.group("continued_avg"))
            iur = _number(match.group("iur"))
            if continued is None or continued_avg is None or iur is None:
                raise ValueError(f"invalid DOL continued-claims row: {line}")
            row.update(
                {
                    "continued": continued * Decimal("1000"),
                    "continued_avg": continued_avg * Decimal("1000"),
                    "iur": iur,
                }
            )
        parsed.append(row)
    if len(parsed) < 40:
        raise ValueError(f"DOL PDF table yielded only {len(parsed)} weekly rows")
    if len({item["date"] for item in parsed}) != len(parsed):
        raise ValueError("DOL PDF table contains duplicate weeks")
    if parsed != sorted(parsed, key=lambda item: item["date"]):
        raise ValueError("DOL PDF weeks are not ascending")
    if any(item["date"].weekday() != 5 for item in parsed):
        raise ValueError("DOL PDF contains a non-Saturday week")
    if any(
        current["date"] - previous["date"] != timedelta(days=7)
        for previous, current in zip(parsed, parsed[1:], strict=False)
    ):
        raise ValueError("DOL PDF weekly table is not contiguous")

    _validate_four_week_average(
        parsed,
        value_key="initial",
        average_key="initial_avg",
    )
    continued_rows = [item for item in parsed if "continued" in item]
    if len(continued_rows) < 4:
        raise ValueError("DOL PDF lacks continued-claims history")
    _validate_four_week_average(
        continued_rows,
        value_key="continued",
        average_key="continued_avg",
    )
    latest_initial = parsed[-1]["date"]
    latest_continued = continued_rows[-1]["date"]
    if latest_initial - latest_continued != timedelta(days=7):
        raise ValueError("DOL advance initial and continued weeks are not seven days apart")
    if not 0 <= (release_date - latest_initial).days <= 10:
        raise ValueError("DOL release date is inconsistent with its latest initial-claims week")

    archive_url = (
        f"https://oui.doleta.gov/press/{release_date.year}/"
        f"{release_date:%m%d%y}.pdf"
    )
    records: list[dict[str, Any]] = []
    for item in parsed:
        initial_status = "advance" if item["date"] == latest_initial else "revised"
        records.extend(
            (
                _record(
                    INITIAL_SA,
                    item["date"],
                    item["initial"],
                    estimate_status=initial_status,
                    source_url=archive_url,
                    release_date=release_date,
                ),
                _record(
                    INITIAL_4WK,
                    item["date"],
                    item["initial_avg"],
                    estimate_status=initial_status,
                    source_url=archive_url,
                    release_date=release_date,
                ),
            )
        )
        if "continued" not in item:
            continue
        continued_status = (
            "advance" if item["date"] == latest_continued else "revised"
        )
        for series_id, key in (
            (CONTINUED_SA, "continued"),
            (CONTINUED_4WK, "continued_avg"),
            (IUR_SA, "iur"),
        ):
            records.append(
                _record(
                    series_id,
                    item["date"],
                    item[key],
                    estimate_status=continued_status,
                    source_url=archive_url,
                    release_date=release_date,
                )
            )
    return records, {
        "release_date": release_date.isoformat(),
        "release_initial_week": latest_initial.isoformat(),
        "release_continued_week": latest_continued.isoformat(),
        "archive_url": archive_url,
        "release_table_rows": len(parsed),
    }


def parse_weekly_claims_release_pdf(
    content: bytes,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not content.startswith(b"%PDF-"):
        raise ValueError("DOL release response is not a PDF")
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ValueError(f"DOL release PDF could not be read: {exc}") from exc
    return parse_weekly_claims_release_text(text)


class DOLWeeklyClaimsProvider(HTTPProvider):
    """ETA national history plus the current immutable weekly release."""

    key = "dol-eta-ui"
    base_url = "https://oui.doleta.gov"

    def _bytes(
        self,
        dataset: str,
        url: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        allowed_hosts: frozenset[str],
        maximum: int,
    ) -> tuple[bytes | None, ProviderResult | None]:
        try:
            response = self.client.request(method, url, data=data)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return None, ProviderResult.failure(
                self.key, dataset, f"{type(exc).__name__}: {exc}"
            )
        host = (urlparse(str(response.url)).hostname or "").lower()
        if host not in allowed_hosts:
            return None, ProviderResult.failure(
                self.key, dataset, f"unexpected redirect host: {host}"
            )
        content = bytes(response.content)
        if not content or len(content) > maximum:
            return None, ProviderResult.failure(
                self.key,
                dataset,
                f"invalid response size: {len(content)} bytes",
            )
        return content, None

    def weekly_claims(self, *, start_year: int, end_year: int) -> ProviderResult:
        dataset = "national-weekly-claims"
        if start_year > end_year or start_year < 1967:
            return ProviderResult.failure(
                self.key, dataset, "invalid DOL weekly-claims year range"
            )
        history_records: list[dict[str, Any]] = []
        history_artifacts: list[dict[str, Any]] = []
        history_run_dates: set[str] = set()
        skipped_future_rows = 0
        # The legacy Informix query is substantially more reliable for a
        # single year than for a multi-year range. Keep one ingestion batch,
        # but fetch and fingerprint each requested calendar year separately.
        for year in range(start_year, end_year + 1):
            form = {
                "level": "us",
                "strtdate": str(year),
                "enddate": str(year),
                "filetype": "xml",
            }
            xml = None
            year_records = None
            year_metadata = None
            history_error = f"DOL history request failed for {year}"
            for _attempt in range(3):
                xml, failure = self._bytes(
                    dataset,
                    HISTORY_URL,
                    method="POST",
                    data=form,
                    allowed_hosts=frozenset({"oui.doleta.gov"}),
                    maximum=MAX_XML_BYTES,
                )
                if failure:
                    history_error = failure.error
                    continue
                assert xml is not None
                try:
                    year_records, year_metadata = (
                        parse_weekly_claims_history_xml(xml)
                    )
                except ValueError as exc:
                    # The endpoint intermittently returns an Informix error as
                    # a 200 text body. Retry the bounded request, then fail closed.
                    history_error = str(exc)
                    continue
                break
            if xml is None or year_records is None or year_metadata is None:
                return ProviderResult.failure(self.key, dataset, history_error)
            if any(date.fromisoformat(item["date"]).year != year for item in year_records):
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    f"DOL single-year response leaked observations outside {year}",
                )
            history_records.extend(year_records)
            history_run_dates.add(str(year_metadata["xml_run_date"]))
            skipped_future_rows += int(year_metadata["future_rows_skipped"])
            history_artifacts.append(
                {
                    "url": HISTORY_URL,
                    "sha256": hashlib.sha256(xml).hexdigest(),
                    "content_type": "application/xml",
                    "size": len(xml),
                    "request": form,
                }
            )
        if len(history_run_dates) != 1:
            return ProviderResult.failure(
                self.key,
                dataset,
                "DOL yearly XML responses have inconsistent run dates",
            )
        if len(
            {(item["series_id"], item["date"]) for item in history_records}
        ) != len(history_records):
            return ProviderResult.failure(
                self.key, dataset, "DOL yearly XML responses contain duplicate observations"
            )
        history_metadata = {
            "xml_run_date": next(iter(history_run_dates)),
            "history_latest_week": max(
                date.fromisoformat(item["date"]) for item in history_records
            ).isoformat(),
            "future_rows_skipped": skipped_future_rows,
        }

        current_pdf, failure = self._bytes(
            dataset,
            CURRENT_RELEASE_URL,
            allowed_hosts=frozenset({"www.dol.gov", "dol.gov"}),
            maximum=MAX_PDF_BYTES,
        )
        if failure:
            return failure
        assert current_pdf is not None
        try:
            release_records, release_metadata = parse_weekly_claims_release_pdf(
                current_pdf
            )
        except ValueError as exc:
            return ProviderResult.failure(self.key, dataset, str(exc))

        archive_url = str(release_metadata["archive_url"])
        archive_pdf, failure = self._bytes(
            dataset,
            archive_url,
            allowed_hosts=frozenset({"oui.doleta.gov"}),
            maximum=MAX_PDF_BYTES,
        )
        if failure:
            return failure
        assert archive_pdf is not None
        current_hash = hashlib.sha256(current_pdf).hexdigest()
        archive_hash = hashlib.sha256(archive_pdf).hexdigest()
        if current_hash != archive_hash:
            return ProviderResult.failure(
                self.key,
                dataset,
                "current DOL release and immutable archive hashes differ",
            )

        merged = {
            (item["series_id"], item["date"]): item for item in history_records
        }
        merged.update(
            {(item["series_id"], item["date"]): item for item in release_records}
        )
        records = sorted(
            merged.values(), key=lambda item: (item["date"], item["series_id"])
        )
        metadata = {
            **history_metadata,
            **release_metadata,
            "requested_start_year": start_year,
            "requested_end_year": end_year,
            "history_record_count": len(history_records),
            "release_record_count": len(release_records),
            "quality_status": "complete",
            "artifacts": [
                *history_artifacts,
                {
                    "url": archive_url,
                    "sha256": archive_hash,
                    "content_type": "application/pdf",
                    "size": len(archive_pdf),
                },
            ],
        }
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            fetched_at=datetime.now(UTC),
            metadata=metadata,
        )
