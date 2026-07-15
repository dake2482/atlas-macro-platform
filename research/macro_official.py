"""Credential-gated adapters for official BEA and Census macro data.

The upstream APIs expose only their current/latest vintage.  BEA includes a
``LastRevised`` date in NIPA table notes, which is retained on every normalized
record.  Census EITS/MARTS does not expose a release or revision timestamp in
the data response, so this module deliberately records that fact instead of
using the fetch time as a made-up vintage.

Official references:

* BEA API guide: https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf
* BEA API terms: https://apps.bea.gov/API/_pdf/bea_api_tos.pdf
* Census EITS: https://www.census.gov/data/developers/data-sets/economic-indicators.html
* Census API terms: https://www.census.gov/data/developers/about/terms-of-service.html
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .providers import HTTPProvider, ProviderResult
from .raw_evidence import EvidenceResponse, build_evidence_bundle, parse_evidence_bundle

BEA_ATTRIBUTION_NOTICE = (
    "This product uses the Bureau of Economic Analysis (BEA) Data API "
    "but is not endorsed or certified by BEA."
)
CENSUS_ATTRIBUTION_NOTICE = (
    "This product uses the Census Bureau Data API but is not endorsed or "
    "certified by the Census Bureau."
)
CENSUS_MARTS_ENDPOINT = "https://api.census.gov/data/timeseries/eits/marts"
CENSUS_MARTS_FIELDS = (
    "program_code",
    "cell_value",
    "time_slot_id",
    "time_slot_date",
    "time_slot_name",
    "error_data",
    "seasonally_adj",
    "category_code",
    "data_type_code",
)
CENSUS_MARTS_RESPONSE_HEADERS = (*CENSUS_MARTS_FIELDS, "time")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, "", ".", "--", "---", "NA", "N/A", "(NA)"):
        return None
    normalized = str(value).strip().replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _mapping_value(item: Mapping[str, Any], *names: str) -> Any:
    """Return a response field without depending on BEA's casing."""

    casefolded = {str(key).casefold(): value for key, value in item.items()}
    for name in names:
        if name.casefold() in casefolded:
            return casefolded[name.casefold()]
    return None


def _bea_period_date(value: Any) -> str | None:
    period = str(value or "").strip().upper()
    if match := re.fullmatch(r"(\d{4})Q([1-4])", period):
        year, quarter = (int(part) for part in match.groups())
        return f"{year:04d}-{(quarter - 1) * 3 + 1:02d}-01"
    if match := re.fullmatch(r"(\d{4})M(\d{1,2})", period):
        year, month = (int(part) for part in match.groups())
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}-01"
    if re.fullmatch(r"\d{4}", period):
        return f"{period}-01-01"
    return None


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in (r"(\d{4})-(\d{2})-(\d{2})", r"(\d{4})-(\d{2})"):
        if match := re.fullmatch(pattern, text):
            parts = [int(part) for part in match.groups()]
            year, month = parts[:2]
            day = parts[2] if len(parts) == 3 else 1
            try:
                return datetime(year, month, day, tzinfo=UTC).date().isoformat()
            except ValueError:
                return None
    return None


def _month_from_census_row(item: Mapping[str, Any]) -> str | None:
    for key in ("time_slot_date", "time"):
        if result := _iso_date(item.get(key)):
            return result
    year = str(item.get("time") or "").strip()
    slot = str(item.get("time_slot_id") or "").strip().upper()
    match = re.fullmatch(r"M(\d{1,2})", slot)
    if re.fullmatch(r"\d{4}", year) and match:
        month = int(match.group(1))
        if 1 <= month <= 12:
            return f"{year}-{month:02d}-01"
    return None


def _normalize_years(years: str | int | Iterable[str | int] | None) -> str:
    if years is None:
        current_year = datetime.now(UTC).year
        return f"{current_year - 1},{current_year}"
    if isinstance(years, (str, int)):
        return str(years)
    return ",".join(str(year) for year in years)


def _bea_revision(notes: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    for note in notes:
        text = str(_mapping_value(note, "NoteText") or "")
        if match := re.search(r"Last\s*Revised\s*:\s*([^\r\n]+)", text, re.IGNORECASE):
            raw = match.group(1).strip().rstrip(". ")
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).date().isoformat(), raw
                except ValueError:
                    pass
            return None, raw
    return None, None


class BEANIPAProvider(HTTPProvider):
    """BEA National Income and Product Accounts data adapter.

    NIPA values are retained in the reporting unit supplied by BEA.  Consumers
    must use ``metric_name``, ``calculation_type`` and ``unit_multiplier``
    together; this adapter does not silently rescale or round source values.
    """

    key = "bea"
    base_url = "https://apps.bea.gov"
    documentation_url = "https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf"
    terms_url = "https://apps.bea.gov/API/_pdf/bea_api_tos.pdf"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("BEA_API_KEY", "")
        super().__init__(**kwargs)

    @staticmethod
    def _error_text(error: Any) -> str:
        items = _as_list(error)
        messages = []
        for item in items:
            if isinstance(item, Mapping):
                code = _mapping_value(item, "APIErrorCode", "ErrorCode")
                description = _mapping_value(
                    item,
                    "APIErrorDescription",
                    "ErrorDescription",
                    "ErrorDetail",
                )
                messages.append(": ".join(str(value) for value in (code, description) if value))
            elif item:
                messages.append(str(item))
        return "; ".join(message for message in messages if message) or "BEA request failed"

    def nipa_table(
        self,
        table_name: str,
        *,
        frequency: str = "Q",
        years: str | int | Iterable[str | int] | None = None,
    ) -> ProviderResult:
        table_name = str(table_name).strip().upper()
        frequency = ",".join(
            part.strip().upper() for part in str(frequency).split(",") if part.strip()
        )
        dataset = f"nipa:{table_name}:{frequency}"
        if not self.api_key:
            return ProviderResult.skip(self.key, dataset, "BEA_API_KEY is not configured")
        if not re.fullmatch(r"T[0-9A-Z]+", table_name):
            return ProviderResult.failure(self.key, dataset, "invalid NIPA TableName")
        if not frequency or any(part not in {"A", "Q", "M"} for part in frequency.split(",")):
            return ProviderResult.failure(self.key, dataset, "frequency must contain only A, Q or M")

        year_value = _normalize_years(years)
        payload, failure = self._get_json(
            dataset,
            "/api/data",
            params={
                "UserID": self.api_key,
                "method": "GetData",
                "DataSetName": "NIPA",
                "TableName": table_name,
                "Frequency": frequency,
                "Year": year_value,
                "ResultFormat": "JSON",
            },
        )
        if failure:
            return failure
        if not isinstance(payload, Mapping) or not isinstance(payload.get("BEAAPI"), Mapping):
            return ProviderResult.failure(self.key, dataset, "unexpected BEA response shape")

        api_root = payload["BEAAPI"]
        results_value = api_root.get("Results")
        if isinstance(results_value, list):
            results_value = results_value[0] if results_value else {}
        if not isinstance(results_value, Mapping):
            return ProviderResult.failure(self.key, dataset, "missing BEA Results object")
        if "Error" in results_value:
            return ProviderResult.failure(
                self.key,
                dataset,
                self._error_text(results_value.get("Error")),
            )

        notes = [item for item in _as_list(results_value.get("Notes")) if isinstance(item, Mapping)]
        revision_date, revision_text = _bea_revision(notes)
        production_time = _mapping_value(results_value, "UTCProductionTime")
        records = []
        for item in _as_list(results_value.get("Data")):
            if not isinstance(item, Mapping):
                continue
            value = _decimal_or_none(_mapping_value(item, "DataValue"))
            value_date = _bea_period_date(_mapping_value(item, "TimePeriod"))
            series_code = str(_mapping_value(item, "SeriesCode") or "").strip().upper()
            if value is None or value_date is None or not series_code:
                continue
            records.append(
                {
                    "series_id": f"BEA-{series_code}",
                    "date": value_date,
                    "value": value,
                    "metadata": {
                        "table_name": _mapping_value(item, "TableName") or table_name,
                        "series_code": series_code,
                        "line_number": _mapping_value(item, "LineNumber"),
                        "line_description": _mapping_value(item, "LineDescription"),
                        "time_period": _mapping_value(item, "TimePeriod"),
                        "frequency": frequency,
                        "metric_name": _mapping_value(item, "METRIC_NAME", "Metric_Name"),
                        "calculation_type": _mapping_value(item, "CL_UNIT"),
                        "unit_multiplier": _mapping_value(item, "UNIT_MULT"),
                        "note_refs": _mapping_value(item, "NoteRef"),
                        "source_revision_date": revision_date,
                        "source_revision_text": revision_text,
                        "api_production_time": production_time,
                    },
                }
            )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "table_name": table_name,
                "frequency": frequency,
                "years": year_value,
                "api_production_time": production_time,
                "source_revision_date": revision_date,
                "source_revision_text": revision_text,
                "vintage_policy": "latest-vintage-only",
                "unit_policy": "source value retained; apply CL_UNIT and UNIT_MULT",
                "attribution": "U.S. Bureau of Economic Analysis",
                "attribution_notice": BEA_ATTRIBUTION_NOTICE,
                "documentation_url": self.documentation_url,
                "terms_url": self.terms_url,
            },
        )

    def gdp_pce(
        self,
        *,
        years: str | int | Iterable[str | int] | None = None,
    ) -> ProviderResult:
        """Return quarterly annualized real GDP and real PCE growth.

        NIPA table 1.1.1 (``T10101``) line 1 is real GDP and line 2 is
        personal consumption expenditures.  Quarterly percent changes in this
        table are published at seasonally adjusted annual rates.
        """

        result = self.nipa_table("T10101", frequency="Q", years=years)
        if not result.ok:
            return result
        result.records = [
            record
            for record in result.records
            if str(record.get("metadata", {}).get("line_number")) in {"1", "2"}
        ]
        result.dataset = "nipa:gdp-pce-growth:Q"
        result.metadata["line_contract"] = {
            "1": "real GDP percent change from preceding period, SAAR",
            "2": "real PCE percent change from preceding period, SAAR",
        }
        return result


class CensusMARTSProvider(HTTPProvider):
    """Credential-gated Advance Monthly Retail Sales (EITS/MARTS) adapter."""

    key = "census"
    base_url = "https://api.census.gov"
    documentation_url = (
        "https://www.census.gov/data/developers/data-sets/economic-indicators.html"
    )
    dataset_metadata_url = "https://api.census.gov/data/timeseries/eits/marts.json"
    terms_url = "https://www.census.gov/data/developers/about/terms-of-service.html"
    license_url = "https://creativecommons.org/publicdomain/zero/1.0/"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("CENSUS_API_KEY", "")
        super().__init__(**kwargs)

    def monthly_retail_sales(
        self,
        *,
        time: str,
        category_code: str = "44X72",
        seasonally_adjusted: bool = True,
        require_complete_history: bool = False,
    ) -> ProviderResult:
        """Return MARTS monthly sales and transparent one-month/year changes.

        ``44X72`` is Retail Trade and Food Services; ``SM`` is Sales -
        Monthly. Source levels retain Census precision. Derived rates use the
        one-decimal precision of the public MARTS release tables.
        """

        category_code = str(category_code).strip().upper()
        seasonal_value = "yes" if seasonally_adjusted else "no"
        dataset = f"marts:{category_code}:SM:{seasonal_value}"
        if not self.api_key:
            return ProviderResult.skip(self.key, dataset, "CENSUS_API_KEY is not configured")
        if not str(time).strip():
            return ProviderResult.failure(self.key, dataset, "time is required")
        if not re.fullmatch(r"[0-9A-Z]+", category_code):
            return ProviderResult.failure(self.key, dataset, "invalid MARTS category_code")

        requested_time = str(time).strip()
        fetched_at = datetime.now(UTC)
        fields = ",".join(CENSUS_MARTS_FIELDS)
        params = {
            "get": fields,
            "category_code": category_code,
            "seasonally_adj": seasonal_value,
            "data_type_code": "SM",
            "time": requested_time,
            "key": self.api_key,
        }
        try:
            response = self.client.get(CENSUS_MARTS_ENDPOINT, params=params)
            response.raise_for_status()
            raw_content = response.content
            parsed_url = urlparse(str(response.url))
            returned_query = parse_qs(parsed_url.query, keep_blank_values=True)
            if (
                parsed_url.scheme != "https"
                or parsed_url.netloc.lower() != "api.census.gov"
                or parsed_url.path != "/data/timeseries/eits/marts"
                or any(returned_query.get(key) != [str(value)] for key, value in params.items())
                or set(returned_query) != set(params)
            ):
                raise ValueError("Census MARTS transport endpoint is invalid")
            content_type = (
                response.headers.get("content-type", "application/json")
                .split(";", 1)[0]
                .lower()
            )
            if "json" not in content_type or not raw_content:
                raise ValueError("Census MARTS response is not non-empty JSON")
            if self.api_key.encode() in raw_content:
                raise ValueError("Census MARTS response echoed a credential")
            raw_bundle, bundle_metadata = build_evidence_bundle(
                provider=self.key,
                dataset=dataset,
                responses=(
                    EvidenceResponse(
                        role="marts-api-response",
                        url=CENSUS_MARTS_ENDPOINT,
                        content_type=content_type,
                        raw_bytes=raw_content,
                        request_witness={
                            "method": "GET",
                            "fields": list(CENSUS_MARTS_FIELDS),
                            "category_code": category_code,
                            "seasonally_adj": seasonal_value,
                            "data_type_code": "SM",
                            "time": requested_time,
                            "require_complete_history": require_complete_history,
                        },
                        response_witness={"retrieved_at": fetched_at.isoformat()},
                    ),
                ),
            )
            records, replay_metadata = self.replay_evidence_bundle(
                raw_bundle,
                expected_dataset=dataset,
            )
        except (httpx.HTTPError, ValueError) as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = (
                f"HTTP {status_code}"
                if status_code is not None
                else str(exc)
                if isinstance(exc, ValueError)
                else type(exc).__name__
            )
            return ProviderResult.failure(
                self.key,
                dataset,
                f"Census MARTS request failed: {detail}",
            )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            fetched_at=fetched_at,
            raw_bytes=raw_bundle,
            metadata={
                **bundle_metadata,
                **replay_metadata,
                "artifacts": [
                    {
                        "url": CENSUS_MARTS_ENDPOINT,
                        "sha256": hashlib.sha256(raw_content).hexdigest(),
                        "size": len(raw_content),
                        "content_type": content_type,
                    }
                ],
            },
        )

    @classmethod
    def replay_evidence_bundle(
        cls,
        raw_bytes: bytes,
        *,
        expected_dataset: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        match = re.fullmatch(r"marts:([0-9A-Z]+):SM:(yes|no)", expected_dataset)
        if match is None:
            raise ValueError("Census MARTS expected dataset is invalid")
        category_code, seasonal_value = match.groups()
        evidence = parse_evidence_bundle(
            raw_bytes,
            expected_provider=cls.key,
            expected_dataset=expected_dataset,
        )
        if set(evidence.responses) != {"marts-api-response"}:
            raise ValueError("Census MARTS API evidence roles are incomplete")
        entry = evidence.manifest["responses"][0]
        request_witness = entry["request_witness"]
        response_witness = entry["response_witness"]
        expected_request_keys = {
            "method",
            "fields",
            "category_code",
            "seasonally_adj",
            "data_type_code",
            "time",
            "require_complete_history",
        }
        if (
            entry["role"] != "marts-api-response"
            or entry["url"] != CENSUS_MARTS_ENDPOINT
            or "json" not in entry["content_type"].lower()
            or set(request_witness) != expected_request_keys
            or request_witness["method"] != "GET"
            or request_witness["fields"] != list(CENSUS_MARTS_FIELDS)
            or request_witness["category_code"] != category_code
            or request_witness["seasonally_adj"] != seasonal_value
            or request_witness["data_type_code"] != "SM"
            or not isinstance(request_witness["time"], str)
            or not request_witness["time"]
            or not isinstance(request_witness["require_complete_history"], bool)
            or set(response_witness) != {"retrieved_at"}
        ):
            raise ValueError("Census MARTS API evidence contract is invalid")
        try:
            retrieved_at = datetime.fromisoformat(
                str(response_witness["retrieved_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("Census MARTS retrieval time is invalid") from exc
        if retrieved_at.tzinfo is None:
            raise ValueError("Census MARTS retrieval time lacks a timezone")
        retrieved_at = retrieved_at.astimezone(UTC)
        if retrieved_at > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("Census MARTS retrieval time is in the future")
        records, parser_metadata = cls.parse_response_bytes(
            evidence.responses["marts-api-response"],
            category_code=category_code,
            seasonally_adjusted=seasonal_value == "yes",
            require_complete_history=request_witness["require_complete_history"],
            as_of_date=retrieved_at.date(),
        )
        return records, {
            **parser_metadata,
            "requested_time": request_witness["time"],
            "require_complete_history": request_witness[
                "require_complete_history"
            ],
        }

    @classmethod
    def parse_response_bytes(
        cls,
        raw_bytes: bytes,
        *,
        category_code: str,
        seasonally_adjusted: bool,
        require_complete_history: bool,
        as_of_date: date,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        try:
            payload = json.loads(raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("unexpected Census response JSON") from exc
        if isinstance(payload, Mapping):
            error = payload.get("error") or payload.get("errors")
            raise ValueError(str(error or "unexpected Census response shape"))
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            raise ValueError("unexpected Census response shape")

        seasonal_value = "yes" if seasonally_adjusted else "no"
        headers = [str(name) for name in payload[0]]
        if headers != list(CENSUS_MARTS_RESPONSE_HEADERS):
            raise ValueError("Census MARTS response headers do not match the requested schema")
        levels: dict[date, dict[str, Any]] = {}
        for row in payload[1:]:
            if not isinstance(row, list):
                raise ValueError("Census MARTS response contains a non-row item")
            if len(row) != len(headers):
                raise ValueError("Census MARTS response row width does not match its headers")
            item = dict(zip(headers, row, strict=True))
            value = _decimal_or_none(item.get("cell_value"))
            value_date = _month_from_census_row(item)
            returned_program = str(item.get("program_code") or "").strip().upper()
            returned_category = str(item.get("category_code") or "").strip().upper()
            returned_seasonal = str(item.get("seasonally_adj") or "").strip().lower()
            returned_type = str(item.get("data_type_code") or "").strip().upper()
            if (
                returned_program != "MARTS"
                or returned_category != category_code
                or returned_seasonal != seasonal_value
                or returned_type != "SM"
            ):
                raise ValueError("Census MARTS response violated requested dimensions")
            if value is None or value_date is None:
                continue
            period = date.fromisoformat(value_date[:10]).replace(day=1)
            if period > as_of_date.replace(day=1):
                raise ValueError("Census MARTS response contains a future month")
            if period in levels:
                raise ValueError("Census MARTS response duplicated a month")
            levels[period] = {
                "value": value,
                "source_fields": item,
            }
        ordered_periods = sorted(levels)
        if not ordered_periods:
            raise ValueError("Census MARTS returned no values")
        if require_complete_history:
            if ordered_periods[0] != date(1992, 1, 1):
                raise ValueError("Census MARTS history does not begin in 1992-01")
            expected = []
            cursor = ordered_periods[0]
            while cursor <= ordered_periods[-1]:
                expected.append(cursor)
                cursor = (
                    date(cursor.year + 1, 1, 1)
                    if cursor.month == 12
                    else date(cursor.year, cursor.month + 1, 1)
                )
            if ordered_periods != expected:
                raise ValueError("Census MARTS history contains a missing month")

        records: list[dict[str, Any]] = []
        latest_period = ordered_periods[-1]
        adjustment_code = "SA" if seasonally_adjusted else "NSA"
        for position, period in enumerate(ordered_periods):
            level = levels[period]
            estimate_status = (
                "advance"
                if period == latest_period
                else "preliminary"
                if position == len(ordered_periods) - 2
                else "current_latest_vintage"
            )
            source_fields = level["source_fields"]
            records.append(
                {
                    "series_id": f"CENSUS-MRTS-{category_code}-SM-{adjustment_code}",
                    "date": period.isoformat(),
                    "value": level["value"],
                    "metadata": {
                        "program_code": source_fields["program_code"],
                        "category_code": category_code,
                        "data_type_code": "SM",
                        "seasonally_adjusted": seasonally_adjusted,
                        "time_slot_id": source_fields.get("time_slot_id"),
                        "time_slot_date": source_fields.get("time_slot_date"),
                        "time_slot_name": source_fields.get("time_slot_name"),
                        "error_data": source_fields.get("error_data"),
                        "unit": "USD millions",
                        "estimate_status": estimate_status,
                        "source_revision_date": None,
                        "vintage_policy": "current-latest-vintage",
                    },
                }
            )
            for months, suffix in ((1, "MOM"), (12, "YOY")):
                prior_position = position - months
                if prior_position < 0:
                    continue
                prior_period = ordered_periods[prior_position]
                expected_prior = period.year * 12 + period.month - months
                actual_prior = prior_period.year * 12 + prior_period.month
                if actual_prior != expected_prior or levels[prior_period]["value"] == 0:
                    continue
                derived = (
                    (level["value"] / levels[prior_period]["value"] - Decimal("1"))
                    * Decimal("100")
                ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                records.append(
                    {
                        "series_id": (
                            f"CENSUS-MRTS-{category_code}-SM-{adjustment_code}-{suffix}"
                        ),
                        "date": period.isoformat(),
                        "value": derived,
                        "metadata": {
                            "unit": "percent",
                            "category_code": category_code,
                            "seasonally_adjusted": seasonally_adjusted,
                            "estimate_status": estimate_status,
                            "calculation_owner": "Atlas Macro",
                            "formula": f"(level_t / level_t-{months} - 1) * 100",
                            "input_series": [
                                f"CENSUS-MRTS-{category_code}-SM-{adjustment_code}"
                            ],
                            "input_value_dates": [
                                prior_period.isoformat(),
                                period.isoformat(),
                            ],
                            "input_values": [
                                str(levels[prior_period]["value"]),
                                str(level["value"]),
                            ],
                            "precision_policy": (
                                "round half up to 0.1 percentage point"
                            ),
                            "source_revision_date": None,
                            "vintage_policy": "current-latest-vintage",
                        },
                    }
                )
        return records, {
            "program": "Advance Monthly Sales for Retail and Food Services",
            "category_code": category_code,
            "data_type_code": "SM",
            "seasonally_adjusted": seasonally_adjusted,
            "unit": "USD millions",
            "precision_policy": "retain cell_value precision without rounding",
            "derived_precision_policy": "round half up to 0.1 percentage point",
            "latest_value_date": latest_period.isoformat(),
            "history_start": ordered_periods[0].isoformat(),
            "level_count": len(ordered_periods),
            "source_revision_date": None,
            "vintage_policy": "current-latest-vintage",
            "revision_note": (
                "The EITS/MARTS response does not expose a release or revision timestamp; "
                "fetched_at is retrieval time, not a source vintage."
            ),
            "attribution": "U.S. Census Bureau, Advance Monthly Retail Trade Survey",
            "attribution_notice": CENSUS_ATTRIBUTION_NOTICE,
            "license": "CC0-1.0 dataset catalogue; API terms and attribution apply",
            "license_url": cls.license_url,
            "dataset_metadata_url": cls.dataset_metadata_url,
            "documentation_url": cls.documentation_url,
            "terms_url": cls.terms_url,
        }


# Backward-compatible import for callers that used the old, inaccurate class name.
CensusMRTSProvider = CensusMARTSProvider
