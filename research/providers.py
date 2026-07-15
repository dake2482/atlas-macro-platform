"""Small, side-effect-free adapters for public and official upstream APIs.

Providers never perform I/O during construction.  Every fetch returns a
``ProviderResult`` so a missing credential, rate limit, or upstream outage can
be persisted as ingestion metadata instead of crashing a Celery worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx


@dataclass(slots=True)
class ProviderResult:
    provider: str
    dataset: str
    records: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    skipped: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    supplemental_records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    raw_bytes: bytes | None = None

    @property
    def ok(self) -> bool:
        return not self.error and not self.skipped

    @property
    def row_count(self) -> int:
        return len(self.records)

    @classmethod
    def skip(cls, provider: str, dataset: str, reason: str) -> ProviderResult:
        return cls(provider=provider, dataset=dataset, skipped=True, metadata={"reason": reason})

    @classmethod
    def failure(cls, provider: str, dataset: str, error: str) -> ProviderResult:
        return cls(provider=provider, dataset=dataset, error=error[:2000])


@runtime_checkable
class DataProvider(Protocol):
    key: str

    def fetch(self, dataset: str, **kwargs: Any) -> ProviderResult: ...


class HTTPProvider:
    key = "http"
    base_url = ""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            headers=dict(headers or {}),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> HTTPProvider:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _get_json(
        self,
        dataset: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any | None, ProviderResult | None]:
        try:
            response = self.client.get(path, params=params, headers=headers)
            response.raise_for_status()
            return response.json(), None
        except (httpx.HTTPError, ValueError) as exc:
            return None, ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

    def _get_json_with_raw(
        self,
        dataset: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any | None, bytes | None, dict[str, Any], ProviderResult | None]:
        """Decode JSON while retaining the exact HTTP response bytes.

        Raw retention is deliberately opt-in so existing providers keep their
        current memory and persistence behavior.  The returned metadata binds
        the decoded payload to the exact response that was validated.
        """

        try:
            response = self.client.get(path, params=params, headers=headers)
            response.raise_for_status()
            raw_bytes = bytes(response.content)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return (
                None,
                None,
                {},
                ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}"),
            )
        return (
            payload,
            raw_bytes,
            {
                "endpoint": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "byte_length": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            },
            None,
        )

    def _post_json(
        self,
        dataset: str,
        path: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any | None, ProviderResult | None]:
        try:
            response = self.client.post(path, json=dict(json), headers=headers)
            response.raise_for_status()
            return response.json(), None
        except (httpx.HTTPError, ValueError) as exc:
            return None, ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

    def _post_json_with_raw(
        self,
        dataset: str,
        path: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any | None, bytes | None, dict[str, Any], ProviderResult | None]:
        """Decode a JSON POST response while retaining its exact bytes.

        Request bodies are deliberately excluded from the transport witness so
        optional credentials cannot leak into durable ingestion metadata.
        Callers must add a provider-specific, credential-free request witness.
        """

        try:
            response = self.client.post(path, json=dict(json), headers=headers)
            response.raise_for_status()
            raw_bytes = bytes(response.content)
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return (
                None,
                None,
                {},
                ProviderResult.failure(
                    self.key,
                    dataset,
                    f"{type(exc).__name__}: {exc}",
                ),
            )
        return (
            payload,
            raw_bytes,
            {
                "endpoint": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "byte_length": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            },
            None,
        )

    def _get_text(
        self,
        dataset: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[str | None, ProviderResult | None]:
        try:
            response = self.client.get(path, params=params)
            response.raise_for_status()
            return response.text, None
        except httpx.HTTPError as exc:
            return None, ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")

    def _get_bytes(
        self,
        dataset: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[bytes | None, dict[str, Any], ProviderResult | None]:
        """Return the exact HTTP response bytes and their transport witness."""

        try:
            response = self.client.get(path, params=params, headers=headers)
            response.raise_for_status()
            raw_bytes = bytes(response.content)
        except httpx.HTTPError as exc:
            return (
                None,
                {},
                ProviderResult.failure(
                    self.key, dataset, f"{type(exc).__name__}: {exc}"
                ),
            )
        return (
            raw_bytes,
            {
                "endpoint": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "byte_length": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            },
            None,
        )

    def fetch(self, dataset: str, **kwargs: Any) -> ProviderResult:
        method = getattr(self, dataset, None)
        if method is None or dataset.startswith("_"):
            return ProviderResult.failure(self.key, dataset, f"unsupported dataset: {dataset}")
        return method(**kwargs)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, "", "."):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class FREDProvider(HTTPProvider):
    """Federal Reserve Economic Data adapter.

    FRED requires an API key.  With no key the provider returns a skipped
    result without making a network call.
    """

    key = "fred"
    base_url = "https://api.stlouisfed.org/fred"

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        self.api_key = api_key or os.getenv("FRED_API_KEY", "")
        super().__init__(**kwargs)

    def series_observations(
        self,
        series_id: str,
        *,
        observation_start: str | None = None,
        limit: int | None = None,
    ) -> ProviderResult:
        dataset = f"series:{series_id}"
        if not self.api_key:
            return ProviderResult.skip(self.key, dataset, "FRED_API_KEY is not configured")
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "asc",
        }
        if observation_start:
            params["observation_start"] = observation_start
        if limit:
            params["limit"] = limit
        payload, failure = self._get_json(dataset, "/series/observations", params=params)
        if failure:
            return failure
        records = []
        for item in payload.get("observations", []):
            value = _decimal_or_none(item.get("value"))
            if value is None:
                continue
            records.append(
                {
                    "series_id": series_id,
                    "date": item.get("date"),
                    "value": value,
                    "realtime_start": item.get("realtime_start"),
                    "realtime_end": item.get("realtime_end"),
                }
            )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={"count": payload.get("count", len(records))},
        )

    def fetch(self, dataset: str, **kwargs: Any) -> ProviderResult:
        if dataset in {"series", "series_observations"}:
            return self.series_observations(**kwargs)
        return self.series_observations(dataset, **kwargs)


class NYFedMarketsProvider(HTTPProvider):
    """Adapter for reference rates and Desk operations from the NY Fed.

    Amounts in the Desk APIs are denominated in dollars.  The normalized
    observation contract used by Atlas stores them in USD millions while
    retaining the source operation fields in ``metadata``.  Use is subject to
    the New York Fed Terms of Use and requires attribution when displayed.
    """

    key = "ny-fed-markets"
    base_url = "https://markets.newyorkfed.org"
    attribution = "Federal Reserve Bank of New York"
    terms_url = "https://www.newyorkfed.org/privacy/termsofuse"
    market_timezone = ZoneInfo("America/New_York")

    RATE_GROUPS = {"SOFR": "secured", "EFFR": "unsecured"}
    SOMA_SERIES = {
        "total": "SOMA-TOTAL",
        "bills": "SOMA-BILLS",
        "notesbonds": "SOMA-NOTES-BONDS",
        "tips": "SOMA-TIPS",
        "frn": "SOMA-FRN",
        "tipsInflationCompensation": "SOMA-TIPS-INFLATION-COMPENSATION",
        "mbs": "SOMA-MBS",
        "cmbs": "SOMA-CMBS",
        "agencies": "SOMA-AGENCIES",
    }
    FX_COUNTERPARTY_CODES = {
        "Bank of Canada": "BOC",
        "Bank of England": "BOE",
        "Bank of Japan": "BOJ",
        "European Central Bank": "ECB",
        "Swiss National Bank": "SNB",
    }

    @staticmethod
    def _usd_millions(value: Any) -> Decimal | None:
        amount = _decimal_or_none(value)
        return amount / Decimal("1000000") if amount is not None else None

    @classmethod
    def _counterparty_code(cls, counterparty: str) -> str:
        known = cls.FX_COUNTERPARTY_CODES.get(counterparty)
        if known:
            return known
        return re.sub(r"[^A-Z0-9]+", "-", counterparty.upper()).strip("-") or "UNKNOWN"

    @classmethod
    def validate_usd_fx_swap_operations(
        cls,
        payload: Any,
        *,
        strict: bool,
        fetched_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Validate one USD-liquidity-swap response without skipping bad rows.

        ``strict=False`` exists only for the historical explicit ``last/N``
        fixture interface, whose old payload samples omit several source
        fields. Production search coverage always uses the strict contract.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("invalid fxSwaps payload")
        envelope = payload.get("fxSwaps")
        operations = envelope.get("operations") if isinstance(envelope, Mapping) else None
        if not isinstance(operations, list) or (strict and not operations):
            raise ValueError("invalid fxSwaps.operations payload")

        acquisition_time = fetched_at or datetime.now(UTC)
        if acquisition_time.tzinfo is None:
            acquisition_time = acquisition_time.replace(tzinfo=UTC)
        parsed: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for item in operations:
            if not isinstance(item, Mapping):
                raise ValueError("USD swap response contains a non-object row")
            source = dict(item)
            counterparty = str(item.get("counterparty") or "").strip()
            raw_amount = _decimal_or_none(item.get("amount"))
            if (
                not counterparty
                or raw_amount is None
                or not raw_amount.is_finite()
                or raw_amount < 0
            ):
                raise ValueError("USD swap row has invalid counterparty or amount")

            def canonical_date(field: str, *, required: bool = True) -> date | None:
                raw = item.get(field)
                if raw in (None, "") and not required:
                    return None
                if not isinstance(raw, str):
                    raise ValueError(f"USD swap {field} must be canonical text")
                try:
                    parsed_date = date.fromisoformat(raw)
                except ValueError as exc:
                    raise ValueError(f"USD swap {field} is invalid") from exc
                if raw != parsed_date.isoformat():
                    raise ValueError(f"USD swap {field} is not canonical")
                return parsed_date

            settlement_date = canonical_date("settlementDate")
            maturity_date = canonical_date("maturityDate")
            assert settlement_date is not None and maturity_date is not None
            if settlement_date >= maturity_date:
                raise ValueError("USD swap settlement must precede maturity")

            raw_small_value = item.get("isSmallValue", "")
            if strict and (
                not isinstance(raw_small_value, str)
                or raw_small_value not in {"", "Y"}
            ):
                raise ValueError("USD swap isSmallValue flag is unknown")
            is_small_value = str(raw_small_value or "").strip().upper() in {
                "1",
                "TRUE",
                "Y",
                "YES",
            }

            trade_date = canonical_date("tradeDate", required=strict)
            interest_rate = _decimal_or_none(item.get("interestRate"))
            raw_term = item.get("termInDays")
            if strict:
                if item.get("operationType") != "U.S. Dollar Liquidity Swap":
                    raise ValueError("USD swap operation type changed")
                if item.get("currency") != "USD":
                    raise ValueError("USD swap currency changed")
                if trade_date is None or trade_date > settlement_date:
                    raise ValueError("USD swap trade date exceeds settlement")
                if (
                    isinstance(raw_term, bool)
                    or not isinstance(raw_term, int)
                    or raw_term != (maturity_date - settlement_date).days
                ):
                    raise ValueError("USD swap term does not match date interval")
                if (
                    interest_rate is None
                    or not interest_rate.is_finite()
                    or interest_rate < 0
                ):
                    raise ValueError("USD swap interest rate is invalid")
                raw_last_updated = item.get("lastUpdated")
                if not isinstance(raw_last_updated, str) or not raw_last_updated:
                    raise ValueError("USD swap lastUpdated is missing")
                try:
                    last_updated = datetime.fromisoformat(
                        raw_last_updated.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise ValueError("USD swap lastUpdated is invalid") from exc
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=cls.market_timezone)
                if (
                    trade_date is None
                    or trade_date
                    > last_updated.astimezone(cls.market_timezone).date()
                    or last_updated > acquisition_time + timedelta(minutes=5)
                ):
                    raise ValueError("USD swap lastUpdated is future-dated")

            operation_id = str(item.get("operationId") or "").strip()
            natural_key = (
                ("operationId", operation_id)
                if operation_id
                else (
                    "fields",
                    trade_date.isoformat() if trade_date else "",
                    settlement_date.isoformat(),
                    maturity_date.isoformat(),
                    counterparty,
                    str(raw_small_value),
                )
            )
            if natural_key in seen:
                raise ValueError(
                    "USD swap response contains a duplicate or conflicting business identity"
                )
            seen.add(natural_key)
            parsed.append(
                {
                    "amount": raw_amount / Decimal("1000000"),
                    "counterparty": counterparty,
                    "counterparty_code": cls._counterparty_code(counterparty),
                    "trade_date": trade_date,
                    "settlement_date": settlement_date,
                    "maturity_date": maturity_date,
                    "term_in_days": raw_term,
                    "interest_rate": interest_rate,
                    "is_small_value": is_small_value,
                    "source": source,
                    "natural_key": "|".join(natural_key),
                }
            )
        return parsed

    @classmethod
    def usd_fx_swap_records(
        cls,
        parsed: list[dict[str, Any]],
        *,
        as_of_date: date,
        history_limit: int | None,
    ) -> list[dict[str, Any]]:
        """Build transparent daily and current aggregates from validated rows."""

        records: list[dict[str, Any]] = []
        by_settlement: dict[date, list[dict[str, Any]]] = {}
        for item in parsed:
            by_settlement.setdefault(item["settlement_date"], []).append(item)
        for settlement_date, daily_operations in sorted(by_settlement.items()):
            regular = [item for item in daily_operations if not item["is_small_value"]]
            small = [item for item in daily_operations if item["is_small_value"]]
            records.extend(
                [
                    {
                        "series_id": "FXSWAP-USD-DRAWDOWN",
                        "date": settlement_date.isoformat(),
                        "value": sum(
                            (item["amount"] for item in daily_operations), Decimal("0")
                        ),
                        "metadata": {
                            "unit": "USD millions",
                            "operations": [item["source"] for item in daily_operations],
                        },
                    },
                    {
                        "series_id": "FXSWAP-USD-DRAWDOWN-NON-SMALL-VALUE",
                        "date": settlement_date.isoformat(),
                        "value": sum(
                            (item["amount"] for item in regular), Decimal("0")
                        ),
                        "metadata": {
                            "unit": "USD millions",
                            "operations": [item["source"] for item in regular],
                            "small_value_excluded": True,
                            "classification": "non-small-value",
                        },
                    },
                    {
                        "series_id": "FXSWAP-USD-DRAWDOWN-SMALL-VALUE",
                        "date": settlement_date.isoformat(),
                        "value": sum((item["amount"] for item in small), Decimal("0")),
                        "metadata": {
                            "unit": "USD millions",
                            "operations": [item["source"] for item in small],
                            "small_value_only": True,
                            "classification": "small-value-technical-exercise",
                        },
                    },
                ]
            )

        active = [
            item
            for item in parsed
            if item["settlement_date"] <= as_of_date < item["maturity_date"]
        ]
        common_metadata = {
            "unit": "USD millions",
            "as_of": as_of_date.isoformat(),
            "formula": "settlementDate <= as_of < maturityDate",
            "history_limit": history_limit,
            "active_operations": [item["source"] for item in active],
        }
        regular = [item for item in active if not item["is_small_value"]]
        small = [item for item in active if item["is_small_value"]]
        records.extend(
            [
                {
                    "series_id": "FXSWAP-USD-OUTSTANDING",
                    "date": as_of_date.isoformat(),
                    "value": sum((item["amount"] for item in active), Decimal("0")),
                    "metadata": common_metadata,
                },
                {
                    "series_id": "FXSWAP-USD-OUTSTANDING-NON-SMALL-VALUE",
                    "date": as_of_date.isoformat(),
                    "value": sum((item["amount"] for item in regular), Decimal("0")),
                    "metadata": {
                        **common_metadata,
                        "active_operations": [item["source"] for item in regular],
                        "small_value_excluded": True,
                    },
                },
                {
                    "series_id": "FXSWAP-USD-OUTSTANDING-SMALL-VALUE",
                    "date": as_of_date.isoformat(),
                    "value": sum((item["amount"] for item in small), Decimal("0")),
                    "metadata": {
                        **common_metadata,
                        "active_operations": [item["source"] for item in small],
                        "small_value_only": True,
                    },
                },
            ]
        )
        by_counterparty: dict[str, list[dict[str, Any]]] = {}
        for item in active:
            by_counterparty.setdefault(item["counterparty_code"], []).append(item)
        for code, operations in sorted(by_counterparty.items()):
            records.append(
                {
                    "series_id": f"FXSWAP-USD-{code}-OUTSTANDING",
                    "date": as_of_date.isoformat(),
                    "value": sum((item["amount"] for item in operations), Decimal("0")),
                    "metadata": {
                        **common_metadata,
                        "active_operations": [item["source"] for item in operations],
                        "counterparty": operations[0]["counterparty"],
                    },
                }
            )
        return records

    @staticmethod
    def _operation_metadata(item: Mapping[str, Any]) -> dict[str, Any]:
        """Keep source fields JSON-safe and make the API typo explicit."""

        keys = (
            "operationId",
            "auctionStatus",
            "operationDirection",
            "operationDate",
            "settlementDate",
            "maturityDate",
            "maturityRangeStart",
            "maturityRangeEnd",
            "operationType",
            "operationMethod",
            "auctionMethod",
            "settlementType",
            "termCalenderDays",
            "term",
            "releaseTime",
            "closeTime",
            "isSmallValue",
            "note",
            "lastUpdated",
            "totalAmtSubmitted",
            "totalAmtAccepted",
            "totalParAmtSubmitted",
            "totalParAmtAccepted",
            "participatingCpty",
            "acceptedCpty",
            "details",
            "propositions",
        )
        metadata = {key: item.get(key) for key in keys if item.get(key) is not None}
        if "termCalenderDays" in metadata:
            metadata["term_calendar_days"] = metadata["termCalenderDays"]
        return metadata

    def _desk_result(
        self,
        *,
        dataset: str,
        records: list[dict[str, Any]],
        endpoint: str,
        raw_bytes: bytes | None = None,
        response_metadata: Mapping[str, Any] | None = None,
        **metadata: Any,
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            raw_bytes=raw_bytes,
            metadata={
                "attribution": self.attribution,
                "terms_url": self.terms_url,
                "endpoint": endpoint,
                **dict(response_metadata or {}),
                **metadata,
            },
        )

    @staticmethod
    def _is_small_value_operation(item: Mapping[str, Any]) -> bool:
        """Identify technical exercises without classifying regular operations."""

        explicit = str(item.get("isSmallValue") or "").strip().upper()
        if explicit in {"1", "TRUE", "Y", "YES"}:
            return True
        note = re.sub(r"[-_]+", " ", str(item.get("note") or "").lower())
        return "small value" in note and ("exercise" in note or "test" in note)

    def reference_rate(self, rate_type: str, *, limit: int = 120) -> ProviderResult:
        rate_type = rate_type.upper()
        limit = max(1, min(int(limit), 800))
        dataset = f"reference-rate:{rate_type.lower()}"
        group = self.RATE_GROUPS.get(rate_type)
        if group is None:
            return ProviderResult.failure(self.key, dataset, f"unsupported rate: {rate_type}")
        endpoint = f"/api/rates/{group}/{rate_type.lower()}/last/{limit}.json"
        payload, raw_bytes, response_metadata, failure = self._get_json_with_raw(
            dataset,
            endpoint,
        )
        if failure:
            return failure
        if not isinstance(payload, Mapping):
            return ProviderResult.failure(
                self.key, dataset, "invalid reference-rate payload"
            )
        raw_rates = payload.get("refRates")
        if not isinstance(raw_rates, list):
            return ProviderResult.failure(
                self.key, dataset, "invalid reference-rate refRates payload"
            )
        records = []
        for item in raw_rates:
            if not isinstance(item, Mapping):
                return ProviderResult.failure(
                    self.key, dataset, "invalid reference-rate record"
                )
            value = _decimal_or_none(item.get("percentRate"))
            if value is None or not item.get("effectiveDate"):
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    "reference-rate record missing effectiveDate or percentRate",
                )
            metadata = {
                key: item.get(key)
                for key in (
                    "percentPercentile1",
                    "percentPercentile25",
                    "percentPercentile75",
                    "percentPercentile99",
                    "targetRateFrom",
                    "targetRateTo",
                    "volumeInBillions",
                    "revisionIndicator",
                    "footnoteId",
                )
                if item.get(key) is not None
            }
            records.append(
                {
                    "series_id": rate_type,
                    "date": item["effectiveDate"],
                    "value": value,
                    "metadata": metadata,
                }
            )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            raw_bytes=raw_bytes,
            metadata={
                "attribution": self.attribution,
                "terms_url": self.terms_url,
                **response_metadata,
            },
        )

    def sofr(self, *, limit: int = 120) -> ProviderResult:
        return self.reference_rate("SOFR", limit=limit)

    def effr(self, *, limit: int = 120) -> ProviderResult:
        return self.reference_rate("EFFR", limit=limit)

    def treasury_purchases(self, *, limit: int = 100) -> ProviderResult:
        """Normalize official Treasury outright-purchase result summaries.

        The public result feed combines reserve-management and principal-
        reinvestment purchases.  It does not expose a stable purpose or
        small-value flag, so Atlas publishes the complete official purchase
        set without inventing a regular/test partition.
        """

        limit = max(1, min(int(limit), 100))
        dataset = "treasury:purchases"
        endpoint = f"/api/tsy/purchases/results/summary/last/{limit}.json"
        payload, raw_bytes, response_metadata, failure = self._get_json_with_raw(
            dataset, endpoint
        )
        if failure:
            return failure
        if not isinstance(payload, Mapping):
            return ProviderResult.failure(
                self.key, dataset, "invalid Treasury purchase payload"
            )
        treasury = payload.get("treasury")
        auctions = treasury.get("auctions") if isinstance(treasury, Mapping) else None
        if not isinstance(auctions, list) or not auctions:
            return ProviderResult.failure(
                self.key, dataset, "invalid treasury.auctions payload"
            )

        seen_operation_ids: set[str] = set()
        by_date: dict[str, list[Mapping[str, Any]]] = {}
        for item in auctions:
            if not isinstance(item, Mapping):
                return ProviderResult.failure(
                    self.key, dataset, "invalid Treasury purchase result record"
                )
            operation_id = str(item.get("operationId") or "").strip()
            operation_type = str(item.get("operationType") or "").strip()
            operation_date = str(item.get("operationDate") or "")
            settlement_date = str(item.get("settlementDate") or "")
            maturity_start = str(item.get("maturityRangeStart") or "")
            maturity_end = str(item.get("maturityRangeEnd") or "")
            try:
                parsed_operation_date = date.fromisoformat(operation_date)
                parsed_settlement_date = date.fromisoformat(settlement_date)
                parsed_maturity_start = date.fromisoformat(maturity_start)
                parsed_maturity_end = date.fromisoformat(maturity_end)
                submitted = Decimal(str(item.get("totalParAmtSubmitted")))
                accepted = Decimal(str(item.get("totalParAmtAccepted")))
            except (ArithmeticError, TypeError, ValueError) as exc:
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    f"malformed Treasury purchase result: {exc}",
                )
            if (
                not operation_id
                or operation_id in seen_operation_ids
                or str(item.get("auctionStatus") or "") != "Results"
                or str(item.get("operationDirection") or "") != "P"
                or not operation_type
                or operation_date != parsed_operation_date.isoformat()
                or settlement_date != parsed_settlement_date.isoformat()
                or maturity_start != parsed_maturity_start.isoformat()
                or maturity_end != parsed_maturity_end.isoformat()
                or parsed_maturity_start > parsed_maturity_end
                or not submitted.is_finite()
                or not accepted.is_finite()
                or submitted < 0
                or accepted < 0
                or accepted > submitted
            ):
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    "Treasury purchase result failed identity, status, direction, "
                    "date, amount, or uniqueness checks",
                )
            seen_operation_ids.add(operation_id)
            by_date.setdefault(operation_date, []).append(item)

        records: list[dict[str, Any]] = []
        for operation_date, daily_auctions in sorted(by_date.items()):
            accepted = sum(
                (
                    self._usd_millions(item.get("totalParAmtAccepted"))
                    or Decimal("0")
                )
                for item in daily_auctions
            )
            submitted = sum(
                (
                    self._usd_millions(item.get("totalParAmtSubmitted"))
                    or Decimal("0")
                )
                for item in daily_auctions
            )
            records.append(
                {
                    "series_id": "TREASURY-PURCHASES",
                    "date": operation_date,
                    "value": accepted,
                    "metadata": {
                        "unit": "USD millions",
                        "submitted": str(submitted),
                        "operation_count": len(daily_auctions),
                        "operations": [
                            self._operation_metadata(item) for item in daily_auctions
                        ],
                        "coverage": (
                            "all official purchase results; includes RMP and "
                            "principal reinvestment"
                        ),
                        "small_value_classification": (
                            "unavailable in the official result summary feed"
                        ),
                    },
                }
            )
        return self._desk_result(
            dataset=dataset,
            records=records,
            endpoint=endpoint,
            raw_bytes=raw_bytes,
            response_metadata=response_metadata,
            amount_unit="USD millions",
            coverage=(
                "all operationDirection=P and auctionStatus=Results rows; "
                "purpose and small-value status are not inferred"
            ),
        )

    def reverse_repo_results(self, *, limit: int = 120) -> ProviderResult:
        """Normalize fixed-rate overnight reverse-repo operation results.

        The API's ``latest`` route means *today*, not latest available.  The
        ``last`` route therefore remains reliable on weekends and holidays.
        """

        limit = max(1, min(int(limit), 10000))
        dataset = "repo:reverse-repo-fixed-results"
        endpoint = f"/api/rp/reverserepo/fixed/results/last/{limit}.json"
        payload, raw_bytes, response_metadata, failure = self._get_json_with_raw(
            dataset, endpoint
        )
        if failure:
            return failure
        if not isinstance(payload, Mapping):
            return ProviderResult.failure(self.key, dataset, "invalid repo payload")
        repo = payload.get("repo")
        operations = repo.get("operations") if isinstance(repo, Mapping) else None
        if not isinstance(operations, list) or not operations:
            return ProviderResult.failure(self.key, dataset, "invalid repo.operations payload")

        by_date: dict[str, list[Mapping[str, Any]]] = {}
        seen_operation_ids: set[str] = set()
        for item in operations:
            if not isinstance(item, Mapping):
                return ProviderResult.failure(
                    self.key, dataset, "invalid reverse-repo result record"
                )
            operation_id = str(item.get("operationId") or "").strip()
            operation_date = str(item.get("operationDate") or "")
            try:
                parsed_date = date.fromisoformat(operation_date)
                submitted = Decimal(str(item.get("totalAmtSubmitted")))
                accepted = Decimal(str(item.get("totalAmtAccepted")))
            except (ArithmeticError, TypeError, ValueError) as exc:
                return ProviderResult.failure(
                    self.key, dataset, f"malformed reverse-repo result: {exc}"
                )
            if (
                not operation_id
                or operation_id in seen_operation_ids
                or operation_date != parsed_date.isoformat()
                or str(item.get("auctionStatus") or "") != "Results"
                or str(item.get("operationType") or "") != "Reverse Repo"
                or str(item.get("operationMethod") or "") != "Fixed Rate"
                or not submitted.is_finite()
                or not accepted.is_finite()
                or submitted < 0
                or accepted < 0
                or accepted > submitted
            ):
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    "reverse-repo result failed identity, schema, amount, or "
                    "uniqueness checks",
                )
            seen_operation_ids.add(operation_id)
            by_date.setdefault(operation_date, []).append(item)

        records: list[dict[str, Any]] = []
        for operation_date, daily_operations in sorted(by_date.items()):
            accepted = sum(
                (self._usd_millions(item.get("totalAmtAccepted")) or Decimal("0"))
                for item in daily_operations
            )
            source_operations = [self._operation_metadata(item) for item in daily_operations]
            regular_operations = [
                item
                for item in daily_operations
                if not self._is_small_value_operation(item)
            ]
            small_value_operations = [
                item for item in daily_operations if self._is_small_value_operation(item)
            ]
            regular_accepted = sum(
                (self._usd_millions(item.get("totalAmtAccepted")) or Decimal("0"))
                for item in regular_operations
            )
            small_value_accepted = sum(
                (self._usd_millions(item.get("totalAmtAccepted")) or Decimal("0"))
                for item in small_value_operations
            )
            if accepted != regular_accepted + small_value_accepted:
                return ProviderResult.failure(
                    self.key, dataset, "reverse-repo partition reconciliation failed"
                )
            primary = (regular_operations or daily_operations)[0]
            metadata = {
                **self._operation_metadata(primary),
                "unit": "USD millions",
                "operation_count": len(daily_operations),
                "operations": source_operations,
                "has_small_value_exercise": bool(small_value_operations),
                "non_small_value_operation_count": len(regular_operations),
                "small_value_operation_count": len(small_value_operations),
            }
            records.append(
                {
                    "series_id": "ONRRP",
                    "date": operation_date,
                    "value": accepted,
                    "metadata": metadata,
                }
            )
            regular_metadata = {
                **metadata,
                "operation_count": len(regular_operations),
                "operations": [
                    self._operation_metadata(item) for item in regular_operations
                ],
                "small_value_excluded": True,
                "classification": "non-small-value",
            }
            small_metadata = {
                **metadata,
                "operation_count": len(small_value_operations),
                "operations": [
                    self._operation_metadata(item) for item in small_value_operations
                ],
                "small_value_only": True,
                "classification": "small-value-technical-exercise",
            }
            records.extend(
                [
                    {
                        "series_id": "ONRRP-NON-SMALL-VALUE-TOTAL",
                        "date": operation_date,
                        "value": regular_accepted,
                        "metadata": regular_metadata,
                    },
                    {
                        "series_id": "ONRRP-SMALL-VALUE-TOTAL",
                        "date": operation_date,
                        "value": small_value_accepted,
                        "metadata": small_metadata,
                    },
                ]
            )

            regular_rates: set[Decimal] = set()
            for operation in regular_operations:
                details = operation.get("details")
                if not isinstance(details, list):
                    return ProviderResult.failure(
                        self.key, dataset, "reverse-repo details must be a list"
                    )
                for detail in details:
                    if not isinstance(detail, Mapping):
                        return ProviderResult.failure(
                            self.key, dataset, "invalid reverse-repo detail"
                        )
                    raw_rate = detail.get("percentAwardRate")
                    if raw_rate is None:
                        raw_rate = detail.get("percentOfferingRate")
                    if raw_rate is None:
                        continue
                    rate = _decimal_or_none(raw_rate)
                    if rate is None or not rate.is_finite() or rate < 0:
                        return ProviderResult.failure(
                            self.key, dataset, "invalid reverse-repo regular rate"
                        )
                    regular_rates.add(rate)
            if len(regular_rates) > 1:
                return ProviderResult.failure(
                    self.key, dataset, "ambiguous reverse-repo regular rates"
                )
            if regular_rates:
                rate = next(iter(regular_rates))
                records.append(
                    {
                        "series_id": "ONRRP-RATE",
                        "date": operation_date,
                        "value": rate,
                        "metadata": {
                            **regular_metadata,
                            "unit": "%",
                            "reported_rates": [str(rate)],
                        },
                    }
                )
            participant_values: list[Decimal] = []
            for operation in regular_operations:
                participants = _decimal_or_none(
                    operation.get("acceptedCpty")
                    if operation.get("acceptedCpty") is not None
                    else operation.get("participatingCpty")
                )
                if (
                    participants is None
                    or not participants.is_finite()
                    or participants < 0
                    or participants != participants.to_integral_value()
                ):
                    return ProviderResult.failure(
                        self.key, dataset, "invalid reverse-repo participant count"
                    )
                participant_values.append(participants)
            if participant_values:
                participants = sum(participant_values, Decimal("0"))
                records.append(
                    {
                        "series_id": "ONRRP-PARTICIPANTS",
                        "date": operation_date,
                        "value": participants,
                        "metadata": {
                            **regular_metadata,
                            "unit": "counterparties",
                        },
                    }
                )

            # Counterparty-type propositions are historical-only in recent
            # releases.  Missing arrays are deliberately not converted to zero.
            proposition_totals: dict[str, Decimal] = {}
            for item in regular_operations:
                for proposition in item.get("propositions") or []:
                    if not isinstance(proposition, Mapping):
                        continue
                    counterparty_type = str(proposition.get("counterpartyType") or "").upper()
                    amount = self._usd_millions(proposition.get("amtAccepted"))
                    if not counterparty_type or amount is None:
                        continue
                    proposition_totals[counterparty_type] = (
                        proposition_totals.get(counterparty_type, Decimal("0")) + amount
                    )
            for counterparty_type, amount in proposition_totals.items():
                records.append(
                    {
                        "series_id": f"ONRRP-{counterparty_type}",
                        "date": operation_date,
                        "value": amount,
                        "metadata": {**metadata, "unit": "USD millions"},
                    }
                )
        return self._desk_result(
            dataset=dataset,
            records=records,
            endpoint=endpoint,
            raw_bytes=raw_bytes,
            response_metadata=response_metadata,
            amount_unit="USD millions",
            counterparty_breakdown="present only when propositions is returned",
            partition_formula="ONRRP = non-small-value + small-value",
        )

    def standing_repo_results(
        self, *, limit: int = 240, strict: bool = False
    ) -> ProviderResult:
        """Normalize current full-allotment standing-repo results by day.

        Since December 11, 2025 the Desk normally runs morning and afternoon
        full-allotment operations.  Both windows are summed into one daily
        observation; the individual source records remain in metadata.
        """

        limit = max(1, min(int(limit), 10000))
        dataset = "repo:standing-repo-full-allotment-results"
        endpoint = f"/api/rp/repo/allotment/results/last/{limit}.json"
        payload, raw_bytes, response_metadata, failure = self._get_json_with_raw(
            dataset, endpoint
        )
        if failure:
            return failure
        if not isinstance(payload, Mapping):
            return ProviderResult.failure(self.key, dataset, "invalid repo payload")
        repo = payload.get("repo")
        operations = repo.get("operations") if isinstance(repo, Mapping) else None
        if not isinstance(operations, list) or not operations:
            return ProviderResult.failure(self.key, dataset, "invalid repo.operations payload")

        by_date: dict[str, list[Mapping[str, Any]]] = {}
        seen_operation_ids: set[str] = set()
        for item in operations:
            if not isinstance(item, Mapping):
                if strict:
                    return ProviderResult.failure(
                        self.key, dataset, "invalid standing-repo result record"
                    )
                continue
            operation_id = str(item.get("operationId") or "").strip()
            operation_date = str(item.get("operationDate") or "")
            accepted_value = _decimal_or_none(item.get("totalAmtAccepted"))
            details = item.get("details")
            if (
                accepted_value is None
                or not accepted_value.is_finite()
                or accepted_value < 0
                or not isinstance(details, list)
            ):
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    "standing-repo result has invalid accepted amount or details",
                )
            for detail in details:
                detail_amount = (
                    _decimal_or_none(detail.get("amtAccepted"))
                    if isinstance(detail, Mapping)
                    else None
                )
                if (
                    detail_amount is None
                    or not detail_amount.is_finite()
                    or detail_amount < 0
                ):
                    return ProviderResult.failure(
                        self.key, dataset, "invalid standing-repo detail amount"
                    )
            if strict:
                try:
                    parsed_date = date.fromisoformat(operation_date)
                    submitted = Decimal(str(item.get("totalAmtSubmitted")))
                    accepted = Decimal(str(item.get("totalAmtAccepted")))
                except (ArithmeticError, TypeError, ValueError) as exc:
                    return ProviderResult.failure(
                        self.key, dataset, f"malformed standing-repo result: {exc}"
                    )
                if (
                    not operation_id
                    or operation_id in seen_operation_ids
                    or operation_date != parsed_date.isoformat()
                    or str(item.get("auctionStatus") or "") != "Results"
                    or str(item.get("operationType") or "") != "Repo"
                    or str(item.get("operationMethod") or "") != "Full Allotment"
                    or not submitted.is_finite()
                    or not accepted.is_finite()
                    or submitted < 0
                    or accepted < 0
                    or accepted > submitted
                    or not isinstance(details, list)
                ):
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        "standing-repo result failed identity, schema, amount, "
                        "or uniqueness checks",
                    )
                detail_total = Decimal("0")
                for detail in details:
                    if not isinstance(detail, Mapping):
                        return ProviderResult.failure(
                            self.key, dataset, "invalid standing-repo detail"
                        )
                    detail_amount = _decimal_or_none(detail.get("amtAccepted"))
                    if (
                        detail_amount is None
                        or not detail_amount.is_finite()
                        or detail_amount < 0
                    ):
                        return ProviderResult.failure(
                            self.key, dataset, "invalid standing-repo detail amount"
                        )
                    detail_total += detail_amount
                if detail_total != accepted:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        "standing-repo collateral details do not reconcile",
                    )
            elif not operation_date:
                continue
            if operation_id:
                if operation_id in seen_operation_ids:
                    return ProviderResult.failure(
                        self.key, dataset, "duplicate standing-repo operation ID"
                    )
                seen_operation_ids.add(operation_id)
            by_date.setdefault(operation_date, []).append(item)

        security_series = {
            "Treasury": "SRP-TREASURY",
            "Agency": "SRP-AGENCY",
            "Mortgage-Backed": "SRP-MBS",
        }
        non_small_value_security_series = {
            "Treasury": "SRP-NON-SMALL-VALUE-TREASURY",
            "Agency": "SRP-NON-SMALL-VALUE-AGENCY",
            "Mortgage-Backed": "SRP-NON-SMALL-VALUE-MBS",
        }
        records: list[dict[str, Any]] = []
        for operation_date, daily_operations in by_date.items():
            small_value_operations = [
                item for item in daily_operations if self._is_small_value_operation(item)
            ]
            non_small_value_operations = [
                item for item in daily_operations if not self._is_small_value_operation(item)
            ]
            total = sum(
                (self._usd_millions(item.get("totalAmtAccepted")) or Decimal("0"))
                for item in daily_operations
            )
            source_operations = [self._operation_metadata(item) for item in daily_operations]
            metadata = {
                "unit": "USD millions",
                "operation_count": len(daily_operations),
                "operations": source_operations,
                "has_small_value_exercise": bool(small_value_operations),
                "non_small_value_operation_count": len(non_small_value_operations),
                "small_value_operation_count": len(small_value_operations),
            }
            records.append(
                {
                    "series_id": "SRP",
                    "date": operation_date,
                    "value": total,
                    "metadata": metadata,
                }
            )

            non_small_value_total = sum(
                (self._usd_millions(item.get("totalAmtAccepted")) or Decimal("0"))
                for item in non_small_value_operations
            )
            small_value_total = sum(
                (self._usd_millions(item.get("totalAmtAccepted")) or Decimal("0"))
                for item in small_value_operations
            )
            non_small_value_metadata = {
                **metadata,
                "operation_count": len(non_small_value_operations),
                "operations": [
                    self._operation_metadata(item) for item in non_small_value_operations
                ],
                "small_value_excluded": True,
                "classification": "non-small-value",
            }
            small_value_metadata = {
                **metadata,
                "operation_count": len(small_value_operations),
                "operations": [
                    self._operation_metadata(item) for item in small_value_operations
                ],
                "small_value_only": True,
                "classification": "small-value-technical-exercise",
            }
            records.extend(
                [
                    {
                        "series_id": "SRP-NON-SMALL-VALUE-TOTAL",
                        "date": operation_date,
                        "value": non_small_value_total,
                        "metadata": non_small_value_metadata,
                    },
                    {
                        "series_id": "SRP-SMALL-VALUE-TOTAL",
                        "date": operation_date,
                        "value": small_value_total,
                        "metadata": small_value_metadata,
                    },
                ]
            )

            collateral_totals = {name: Decimal("0") for name in security_series}
            non_small_value_collateral_totals = {
                name: Decimal("0") for name in non_small_value_security_series
            }
            rates: list[Decimal] = []
            for item in daily_operations:
                for detail in item.get("details") or []:
                    if not isinstance(detail, Mapping):
                        continue
                    security_type = str(detail.get("securityType") or "")
                    if security_type in collateral_totals:
                        collateral_totals[security_type] += self._usd_millions(
                            detail.get("amtAccepted")
                        ) or Decimal("0")
                    rate = _decimal_or_none(
                        detail.get("percentOfferingRate")
                        if detail.get("percentOfferingRate") is not None
                        else detail.get("minimumBidRate")
                    )
                    if rate is not None:
                        rates.append(rate)
            non_small_value_rates: list[Decimal] = []
            for item in non_small_value_operations:
                for detail in item.get("details") or []:
                    if not isinstance(detail, Mapping):
                        continue
                    security_type = str(detail.get("securityType") or "")
                    if security_type in non_small_value_collateral_totals:
                        non_small_value_collateral_totals[
                            security_type
                        ] += self._usd_millions(detail.get("amtAccepted")) or Decimal("0")
                    rate = _decimal_or_none(
                        detail.get("percentOfferingRate")
                        if detail.get("percentOfferingRate") is not None
                        else detail.get("minimumBidRate")
                    )
                    if rate is not None:
                        non_small_value_rates.append(rate)
            for security_type, series_id in security_series.items():
                records.append(
                    {
                        "series_id": series_id,
                        "date": operation_date,
                        "value": collateral_totals[security_type],
                        "metadata": {**metadata, "security_type": security_type},
                    }
                )
            for security_type, series_id in non_small_value_security_series.items():
                records.append(
                    {
                        "series_id": series_id,
                        "date": operation_date,
                        "value": non_small_value_collateral_totals[security_type],
                        "metadata": {
                            **non_small_value_metadata,
                            "security_type": security_type,
                        },
                    }
                )
            if rates:
                records.append(
                    {
                        "series_id": "SRP-RATE",
                        "date": operation_date,
                        "value": rates[0],
                        "metadata": {
                            **metadata,
                            "unit": "%",
                            "reported_rates": [str(rate) for rate in sorted(set(rates))],
                        },
                    }
                )
            if len(set(non_small_value_rates)) > 1:
                return ProviderResult.failure(
                    self.key, dataset, "ambiguous standing-repo regular rates"
                )
            if non_small_value_rates:
                records.append(
                    {
                        "series_id": "SRP-NON-SMALL-VALUE-RATE",
                        "date": operation_date,
                        "value": non_small_value_rates[0],
                        "metadata": {
                            **non_small_value_metadata,
                            "unit": "%",
                            "reported_rates": [
                                str(rate) for rate in sorted(set(non_small_value_rates))
                            ],
                        },
                    }
                )
        return self._desk_result(
            dataset=dataset,
            records=records,
            endpoint=endpoint,
            raw_bytes=raw_bytes,
            response_metadata=response_metadata,
            amount_unit="USD millions",
            aggregation="sum of all operation windows by operationDate",
            non_small_value_classification=(
                "explicit affirmative isSmallValue or note containing small-value exercise/test"
            ),
        )

    def soma_summary(self, *, limit: int | None = None) -> ProviderResult:
        """Normalize weekly SOMA domestic-security summary history."""

        dataset = "soma:summary"
        endpoint = "/api/soma/summary.json"
        payload, raw_bytes, response_metadata, failure = self._get_json_with_raw(
            dataset, endpoint
        )
        if failure:
            return failure
        if not isinstance(payload, Mapping):
            return ProviderResult.failure(self.key, dataset, "invalid soma payload")
        soma = payload.get("soma")
        summaries = soma.get("summary") if isinstance(soma, Mapping) else None
        if not isinstance(summaries, list) or not summaries:
            return ProviderResult.failure(self.key, dataset, "invalid soma.summary payload")
        dated_summaries: list[tuple[date, Mapping[str, Any]]] = []
        seen_dates: set[str] = set()
        for item in summaries:
            if not isinstance(item, Mapping):
                return ProviderResult.failure(self.key, dataset, "invalid SOMA summary row")
            raw_date = str(item.get("asOfDate") or "")
            try:
                as_of_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                return ProviderResult.failure(
                    self.key, dataset, f"invalid SOMA asOfDate: {exc}"
                )
            if raw_date != as_of_date.isoformat() or raw_date in seen_dates:
                return ProviderResult.failure(
                    self.key, dataset, "duplicate or non-canonical SOMA asOfDate"
                )
            emitted_total = False
            for source_field in self.SOMA_SERIES:
                raw_value = item.get(source_field)
                if raw_value in (None, "", "."):
                    continue
                value = self._usd_millions(raw_value)
                if value is None or not value.is_finite() or value < 0:
                    return ProviderResult.failure(
                        self.key, dataset, f"invalid SOMA amount: {source_field}"
                    )
                if source_field == "total":
                    emitted_total = True
            if not emitted_total:
                return ProviderResult.failure(
                    self.key, dataset, "SOMA summary row lacks total"
                )
            seen_dates.add(raw_date)
            dated_summaries.append((as_of_date, item))
        dated_summaries.sort(key=lambda item: item[0])
        if limit is not None:
            dated_summaries = dated_summaries[-max(1, min(int(limit), 10000)) :]

        records: list[dict[str, Any]] = []
        for as_of_date, item in dated_summaries:
            raw_date = as_of_date.isoformat()
            emitted_total = False
            for source_field, series_id in self.SOMA_SERIES.items():
                raw_value = item.get(source_field)
                if raw_value in (None, "", "."):
                    continue
                value = self._usd_millions(raw_value)
                if value is None or not value.is_finite() or value < 0:
                    return ProviderResult.failure(
                        self.key, dataset, f"invalid SOMA amount: {source_field}"
                    )
                if source_field == "total":
                    emitted_total = True
                records.append(
                    {
                        "series_id": series_id,
                        "date": item["asOfDate"],
                        "value": value,
                        "metadata": {
                            "unit": "USD millions",
                            "source_field": source_field,
                            "publication_frequency": "weekly",
                        },
                    }
                )
            if not emitted_total:
                return ProviderResult.failure(
                    self.key, dataset, "SOMA summary row lacks total"
                )
        return self._desk_result(
            dataset=dataset,
            records=records,
            endpoint=endpoint,
            raw_bytes=raw_bytes,
            response_metadata=response_metadata,
            amount_unit="USD millions",
        )

    def usd_fx_swaps(
        self,
        *,
        limit: int | None = None,
        as_of: date | str | None = None,
        start_date: date | str = "2007-01-01",
        end_date: date | str | None = None,
        date_type: str = "trade",
    ) -> ProviderResult:
        """Normalize U.S.-dollar central-bank liquidity swap operations.

        Production uses the explicit search window. Passing ``limit`` keeps a
        deliberately coverage-incomplete compatibility interface for fixtures
        and callers that do not claim historical completeness.
        """

        dataset = "fx-swaps:usdollar"

        def parsed_date(raw: date | str | None, *, default: date | None = None) -> date:
            if raw is None:
                if default is None:
                    raise ValueError("date is required")
                return default
            if isinstance(raw, datetime):
                return raw.date()
            if isinstance(raw, date):
                return raw
            try:
                value = date.fromisoformat(str(raw))
            except ValueError as exc:
                raise ValueError(f"invalid date: {exc}") from exc
            if str(raw) != value.isoformat():
                raise ValueError("date must use canonical YYYY-MM-DD form")
            return value

        today_et = datetime.now(self.market_timezone).date()
        try:
            if limit is not None:
                resolved_limit = max(1, min(int(limit), 10000))
                as_of_date = parsed_date(as_of, default=today_et)
                endpoint = f"/api/fxs/usdollar/last/{resolved_limit}.json"
                params = None
                strict = False
                coverage_start = None
                coverage_end = None
            else:
                coverage_start = parsed_date(start_date)
                coverage_end = parsed_date(end_date, default=today_et)
                as_of_date = parsed_date(as_of, default=coverage_end)
                if (
                    date_type != "trade"
                    or coverage_start > coverage_end
                    or coverage_end > today_et
                    or as_of_date != coverage_end
                ):
                    raise ValueError("USD swap search coverage window is invalid")
                resolved_limit = None
                endpoint = "/api/fxs/usdollar/search.json"
                params = {
                    "startDate": coverage_start.isoformat(),
                    "endDate": coverage_end.isoformat(),
                    "dateType": date_type,
                }
                strict = True
        except (TypeError, ValueError) as exc:
            return ProviderResult.failure(self.key, dataset, str(exc))

        payload, raw_bytes, response_metadata, failure = self._get_json_with_raw(
            dataset, endpoint, params=params
        )
        if failure:
            return failure
        acquired_at = datetime.now(UTC)
        try:
            parsed = self.validate_usd_fx_swap_operations(
                payload,
                strict=strict,
                fetched_at=acquired_at,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            return ProviderResult.failure(self.key, dataset, str(exc))
        if strict and any(
            item["trade_date"] is None
            or item["trade_date"] < coverage_start
            or item["trade_date"] > coverage_end
            for item in parsed
        ):
            return ProviderResult.failure(
                self.key,
                dataset,
                "USD swap search returned a row outside requested trade-date coverage",
            )
        records = self.usd_fx_swap_records(
            parsed,
            as_of_date=as_of_date,
            history_limit=resolved_limit,
        )
        result = self._desk_result(
            dataset=dataset,
            records=records,
            endpoint=endpoint,
            raw_bytes=raw_bytes,
            response_metadata=response_metadata,
            amount_unit="USD millions",
            outstanding_as_of=as_of_date.isoformat(),
            coverage_mode="explicit-search" if strict else "limited-last-fixture",
            coverage_complete=strict,
            coverage_start=(coverage_start.isoformat() if coverage_start else None),
            coverage_end=(coverage_end.isoformat() if coverage_end else None),
            date_type=(date_type if strict else None),
            returned_count=len(parsed),
        )
        result.fetched_at = acquired_at
        return result


TREASURY_CURVE_CANONICAL_FEED_IDS = {
    "daily_treasury_yield_curve": (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml-item?data=daily_treasury_yield_curve"
    ),
    "daily_treasury_real_yield_curve": (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        "interest-rates/pages/xml-item?data=daily_treasury_real_yield_curve"
    ),
}


class TreasuryRatesProvider(HTTPProvider):
    """Direct U.S. Treasury nominal and real par-yield curve adapter."""

    key = "us-treasury-rates"
    base_url = "https://home.treasury.gov"
    NOMINAL_FIELDS = {
        "BC_1MONTH": "UST-1M",
        "BC_2MONTH": "UST-2M",
        "BC_3MONTH": "UST-3M",
        "BC_4MONTH": "UST-4M",
        "BC_6MONTH": "UST-6M",
        "BC_1YEAR": "UST-1Y",
        "BC_2YEAR": "UST-2Y",
        "BC_3YEAR": "UST-3Y",
        "BC_5YEAR": "UST-5Y",
        "BC_7YEAR": "UST-7Y",
        "BC_10YEAR": "UST-10Y",
        "BC_20YEAR": "UST-20Y",
        "BC_30YEAR": "UST-30Y",
    }
    REAL_FIELDS = {
        "TC_5YEAR": "TIPS-5Y",
        "TC_7YEAR": "TIPS-7Y",
        "TC_10YEAR": "TIPS-10Y",
        "TC_20YEAR": "TIPS-20Y",
        "TC_30YEAR": "TIPS-30Y",
    }
    CURVE_TITLES = {
        "daily_treasury_yield_curve": "DailyTreasuryYieldCurveRateData",
        "daily_treasury_real_yield_curve": "DailyTreasuryRealYieldCurveRateData",
    }
    FOUR_MONTH_CMT_START = date(2022, 10, 19)
    XML_NS = {
        "atom": "http://www.w3.org/2005/Atom",
        "data": "http://schemas.microsoft.com/ado/2007/08/dataservices",
        "meta": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
    }
    BILL_13W_DATASET = "treasury-bill-rates:13w-coupon-equivalent"
    BILL_13W_SERIES = "UST-BILL-13W-COUPON-EQUIVALENT"
    BILL_13W_FEED = "daily_treasury_bill_rates"
    BILL_13W_COUPON_EQUIVALENT_FIELD = "ROUND_B1_YIELD_13WK_2"
    BILL_13W_BANK_DISCOUNT_FIELD = "ROUND_B1_CLOSE_13WK_2"
    BILL_13W_QUOTE_CONVENTION = "13-week Coupon Equivalent"

    @staticmethod
    def _treasury_timestamp(raw_value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    def treasury_bill_rates_13w_coupon_equivalent(
        self, *, current_year: int | None = None
    ) -> ProviderResult:
        """Fetch two annual bill feeds and expose only the 13-week CE quote.

        The Treasury response carries both Bank Discount and Coupon Equivalent
        quotations.  The former is retained as metadata only; it is never
        substituted for the displayed comparison rate.
        """

        dataset = self.BILL_13W_DATASET
        fetched_at = datetime.now(UTC)
        resolved_year = current_year or fetched_at.year
        if resolved_year - 1 < 1990 or resolved_year > fetched_at.year:
            return ProviderResult.failure(
                self.key,
                dataset,
                f"Treasury bill current year is out of range: {resolved_year}",
            )
        requested_years = [resolved_year - 1, resolved_year]
        responses: list[tuple[int, str]] = []
        for requested_year in requested_years:
            payload, failure = self._get_text(
                dataset,
                "/resource-center/data-chart-center/interest-rates/pages/xml",
                params={
                    "data": self.BILL_13W_FEED,
                    "field_tdr_date_value": str(requested_year),
                },
            )
            if failure:
                return failure
            responses.append((requested_year, payload or ""))
        fetched_at = datetime.now(UTC)
        today = fetched_at.date()
        records: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        seen: dict[date, tuple[Decimal, Decimal, str, date]] = {}
        feed_updates: dict[str, str] = {}
        years_with_quotes: set[int] = set()

        for requested_year, payload in responses:
            try:
                root = ElementTree.fromstring(payload)
            except ElementTree.ParseError as exc:
                return ProviderResult.failure(
                    self.key, dataset, f"ParseError for {requested_year}: {exc}"
                )
            title = (root.findtext("atom:title", default="", namespaces=self.XML_NS) or "").strip()
            if title != "DailyTreasuryBillRateData":
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    "Treasury bill feed convention drift: unexpected feed title",
                )
            raw_feed_updated = (
                root.findtext("atom:updated", default="", namespaces=self.XML_NS) or ""
            ).strip()
            feed_updated = self._treasury_timestamp(raw_feed_updated)
            if (
                feed_updated is None
                or feed_updated > fetched_at + timedelta(minutes=5)
            ):
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    f"Treasury bill feed has invalid or future updated time: {raw_feed_updated}",
                )
            feed_updates[str(requested_year)] = feed_updated.isoformat()
            entries = root.findall("atom:entry", self.XML_NS)
            if not entries:
                return ProviderResult.failure(
                    self.key,
                    dataset,
                    f"Treasury bill response for {requested_year} contains no entries",
                )
            for entry in entries:
                properties = entry.find(
                    "atom:content/meta:properties", self.XML_NS
                )
                if properties is None:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill entry for {requested_year} lacks properties",
                    )
                elements = {
                    child.tag.rsplit("}", 1)[-1]: child for child in properties
                }
                values = {key: element.text for key, element in elements.items()}
                raw_index_date = str(values.get("INDEX_DATE") or "")[:10]
                raw_quote_date = str(values.get("QUOTE_DATE") or "")[:10]
                raw_maturity_date = str(values.get("MATURITY_DATE_13WK") or "")[:10]
                try:
                    period = date.fromisoformat(raw_index_date)
                    quote_date = date.fromisoformat(raw_quote_date)
                    maturity_date = date.fromisoformat(raw_maturity_date)
                except ValueError:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        "Treasury bill response contains a malformed observation, quote, or maturity date",
                    )
                if period.year != requested_year:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill response year {period.year} does not match requested year {requested_year}",
                    )
                if period > today:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill response contains future date {period.isoformat()}",
                    )
                if quote_date != period:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill INDEX_DATE and QUOTE_DATE mismatch on {period.isoformat()}",
                    )
                maturity_days = (maturity_date - period).days
                if not 70 <= maturity_days <= 110:
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill 13-week maturity convention drift on {period.isoformat()}",
                    )
                typed_fields = {
                    "INDEX_DATE": "Edm.DateTime",
                    "QUOTE_DATE": "Edm.DateTime",
                    "MATURITY_DATE_13WK": "Edm.DateTime",
                    self.BILL_13W_COUPON_EQUIVALENT_FIELD: "Edm.Double",
                    self.BILL_13W_BANK_DISCOUNT_FIELD: "Edm.Double",
                }
                metadata_type = f"{{{self.XML_NS['meta']}}}type"
                if any(
                    field not in elements
                    or elements[field].attrib.get(metadata_type) != expected_type
                    for field, expected_type in typed_fields.items()
                ):
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill quotation field convention drift on {period.isoformat()}",
                    )
                coupon_equivalent = _decimal_or_none(
                    values.get(self.BILL_13W_COUPON_EQUIVALENT_FIELD)
                )
                bank_discount = _decimal_or_none(
                    values.get(self.BILL_13W_BANK_DISCOUNT_FIELD)
                )
                cusip = str(values.get("CUSIP_13WK") or "").strip()
                if (
                    coupon_equivalent is None
                    or not coupon_equivalent.is_finite()
                    or bank_discount is None
                    or not bank_discount.is_finite()
                    or not re.fullmatch(r"[A-Z0-9]{9}", cusip)
                ):
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill 13-week quotation is missing or malformed on {period.isoformat()}",
                    )
                identity = (coupon_equivalent, bank_discount, cusip, maturity_date)
                if period in seen:
                    kind = "conflicting duplicate" if seen[period] != identity else "duplicate"
                    return ProviderResult.failure(
                        self.key,
                        dataset,
                        f"Treasury bill response contains {kind} quote for {period.isoformat()}",
                    )
                seen[period] = identity
                years_with_quotes.add(period.year)
                records.append(
                    {
                        "series_id": self.BILL_13W_SERIES,
                        "date": period.isoformat(),
                        "value": coupon_equivalent,
                        "metadata": {
                            "treasury_field": self.BILL_13W_COUPON_EQUIVALENT_FIELD,
                            "bank_discount_field": self.BILL_13W_BANK_DISCOUNT_FIELD,
                            "bank_discount_rate": str(bank_discount),
                            "cusip": cusip,
                            "maturity_date": maturity_date.isoformat(),
                            "quote_convention": self.BILL_13W_QUOTE_CONVENTION,
                            "tenor": "13-week",
                            "requested_year": requested_year,
                            "requested_years": requested_years,
                            "feed_updated_time": feed_updated.isoformat(),
                            "dataset": dataset,
                        },
                    }
                )
            content = payload.encode()
            source_url = (
                f"{self.base_url}/resource-center/data-chart-center/interest-rates/pages/xml"
                f"?data={self.BILL_13W_FEED}&field_tdr_date_value={requested_year}"
            )
            artifacts.append(
                {
                    "url": source_url,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "content_type": "application/atom+xml",
                    "requested_year": requested_year,
                }
            )

        if years_with_quotes != set(requested_years) or not records:
            return ProviderResult.failure(
                self.key,
                dataset,
                "Treasury bill response does not cover both requested years",
            )
        latest_value_date = max(seen)
        if latest_value_date.year != resolved_year:
            return ProviderResult.failure(
                self.key,
                dataset,
                "Treasury bill response is missing the latest current-year 13-week quote",
            )
        records.sort(key=lambda item: str(item["date"]))
        latest_feed_updated = max(
            datetime.fromisoformat(value) for value in feed_updates.values()
        )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            fetched_at=fetched_at,
            metadata={
                "series_id": self.BILL_13W_SERIES,
                "quote_convention": self.BILL_13W_QUOTE_CONVENTION,
                "coupon_equivalent_field": self.BILL_13W_COUPON_EQUIVALENT_FIELD,
                "bank_discount_field": self.BILL_13W_BANK_DISCOUNT_FIELD,
                "requested_years": requested_years,
                "feed_updated_time": latest_feed_updated.isoformat(),
                "feed_updated_times": feed_updates,
                "latest_value_date": latest_value_date.isoformat(),
                "artifacts": artifacts,
            },
        )

    @classmethod
    def parse_curve_xml_bytes(
        cls,
        raw_bytes: bytes,
        *,
        curve: str,
        fields: Mapping[str, str],
        year: int,
        fetched_at: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Replay one exact annual Treasury Atom feed into normalized rows."""

        if curve not in cls.CURVE_TITLES or not raw_bytes:
            raise ValueError("Treasury curve identity or raw XML bytes are invalid")
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        wall_now = datetime.now(UTC)
        if year < 1990 or year > min(fetched_at.astimezone(UTC).year, wall_now.year):
            raise ValueError("Treasury curve year is outside the supported historical range")
        try:
            root = ElementTree.fromstring(raw_bytes)
        except ElementTree.ParseError as exc:
            raise ValueError(f"Treasury curve XML parse failed: {exc}") from exc
        atom_namespace = cls.XML_NS["atom"]
        if root.tag != f"{{{atom_namespace}}}feed":
            raise ValueError("Treasury curve XML root is not the expected Atom feed")
        title = (
            root.findtext("atom:title", default="", namespaces=cls.XML_NS) or ""
        ).strip()
        if title != cls.CURVE_TITLES[curve]:
            raise ValueError("Treasury curve feed title does not match the dataset")
        feed_id = (
            root.findtext("atom:id", default="", namespaces=cls.XML_NS) or ""
        ).strip()
        expected_feed_identity = TREASURY_CURVE_CANONICAL_FEED_IDS[curve]
        if feed_id != expected_feed_identity:
            raise ValueError("Treasury curve feed id does not match the dataset")
        raw_updated = (
            root.findtext("atom:updated", default="", namespaces=cls.XML_NS) or ""
        ).strip()
        feed_updated = cls._treasury_timestamp(raw_updated)
        if (
            feed_updated is None
            or feed_updated > fetched_at.astimezone(UTC) + timedelta(minutes=5)
            or feed_updated > wall_now + timedelta(minutes=5)
        ):
            raise ValueError("Treasury curve feed updated time is invalid or future")

        entries = root.findall("atom:entry", cls.XML_NS)
        if not entries:
            raise ValueError("Treasury curve feed contains no entries")
        metadata_type = f"{{{cls.XML_NS['meta']}}}type"
        metadata_null = f"{{{cls.XML_NS['meta']}}}null"
        records: list[dict[str, Any]] = []
        seen_dates: set[date] = set()
        series_by_date: dict[date, set[str]] = {}
        series_coverage: set[str] = set()
        for entry in entries:
            content = entry.find("atom:content", cls.XML_NS)
            if content is None or content.attrib.get("type") != "application/xml":
                raise ValueError("Treasury curve entry content type drifted")
            properties = content.find("meta:properties", cls.XML_NS)
            if properties is None:
                raise ValueError("Treasury curve entry lacks properties")
            elements: dict[str, ElementTree.Element] = {}
            for child in properties:
                local_name = child.tag.rsplit("}", 1)[-1]
                if local_name in elements:
                    raise ValueError(
                        "Treasury curve entry contains duplicate field "
                        f"{local_name}"
                    )
                elements[local_name] = child
            date_element = elements.get("NEW_DATE")
            raw_date = str(date_element.text if date_element is not None else "")[:10]
            if (
                date_element is None
                or date_element.attrib.get(metadata_type) != "Edm.DateTime"
            ):
                raise ValueError("Treasury curve NEW_DATE type drifted")
            try:
                value_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise ValueError(
                    f"invalid Treasury observation date: {raw_date}"
                ) from exc
            if value_date in seen_dates:
                raise ValueError(
                    f"duplicate Treasury curve entry for {value_date.isoformat()}"
                )
            if value_date.year != year:
                raise ValueError(
                    f"Treasury response year {value_date.year} does not match requested year {year}"
                )
            if value_date > fetched_at.astimezone(UTC).date() or value_date > wall_now.date():
                raise ValueError(
                    f"Treasury response contains future date {value_date.isoformat()}"
                )
            seen_dates.add(value_date)
            series_by_date[value_date] = set()
            for field_name, series_id in fields.items():
                element = elements.get(field_name)
                if element is None:
                    continue
                is_null = str(element.attrib.get(metadata_null) or "").lower() == "true"
                if element.attrib.get(metadata_type) != "Edm.Double":
                    raise ValueError(
                        f"Treasury curve field type drifted for {field_name}"
                    )
                if is_null or element.text in {None, ""}:
                    continue
                value = _decimal_or_none(element.text)
                if value is None or not value.is_finite():
                    raise ValueError(
                        f"Treasury curve contains a non-finite value for {field_name}"
                    )
                series_by_date[value_date].add(series_id)
                series_coverage.add(series_id)
                records.append(
                    {
                        "series_id": series_id,
                        "date": value_date.isoformat(),
                        "value": value,
                        "metadata": {
                            "treasury_field": field_name,
                            "curve": curve,
                            "requested_year": year,
                            "dataset": f"{curve}:{year}",
                        },
                    }
                )
            if not series_by_date[value_date]:
                raise ValueError(
                    f"Treasury curve entry has no usable values on {value_date.isoformat()}"
                )
        latest_date = max(seen_dates)
        missing_latest = sorted(series_coverage - series_by_date[latest_date])
        if missing_latest:
            raise ValueError(
                "Treasury curve latest date is missing previously published series: "
                + ", ".join(missing_latest)
            )
        if year >= 2021:
            minimum_series = (
                set(cls.REAL_FIELDS.values())
                if curve == "daily_treasury_real_yield_curve"
                else set(cls.NOMINAL_FIELDS.values()) - {"UST-4M"}
            )
            if (
                curve == "daily_treasury_yield_curve"
                and latest_date >= cls.FOUR_MONTH_CMT_START
            ):
                minimum_series.add("UST-4M")
            if not minimum_series <= series_coverage:
                raise ValueError(
                    "Treasury curve annual series coverage is incomplete"
                )
        records.sort(key=lambda item: (str(item["date"]), str(item["series_id"])))
        return records, {
            "curve": curve,
            "requested_year": year,
            "feed_title": title,
            "feed_id": feed_id,
            "feed_updated_time": feed_updated.isoformat(),
            "entry_count": len(entries),
            "record_count": len(records),
            "series_coverage": sorted(series_coverage),
            "latest_value_date": latest_date.isoformat(),
            "missing_latest_series": [],
        }

    def _curve(self, *, curve: str, fields: Mapping[str, str], year: int) -> ProviderResult:
        dataset = f"{curve}:{year}"
        current_year = datetime.now(UTC).year
        if year < 1990 or year > current_year:
            return ProviderResult.failure(
                self.key,
                dataset,
                "Treasury curve year is outside the supported historical range",
            )
        raw_bytes, response_metadata, failure = self._get_bytes(
            dataset,
            "/resource-center/data-chart-center/interest-rates/pages/xml",
            params={"data": curve, "field_tdr_date_value": str(year)},
            headers={"Accept": "application/atom+xml, application/xml, text/xml"},
        )
        if failure:
            return failure
        content_type = str(response_metadata.get("content_type") or "").lower()
        if not content_type.startswith(("application/xml", "application/atom+xml", "text/xml")):
            return ProviderResult.failure(
                self.key, dataset, "Treasury curve response is not XML"
            )
        fetched_at = datetime.now(UTC)
        try:
            records, evidence = self.parse_curve_xml_bytes(
                bytes(raw_bytes or b""),
                curve=curve,
                fields=fields,
                year=year,
                fetched_at=fetched_at,
            )
        except ValueError as exc:
            return ProviderResult.failure(self.key, dataset, str(exc))
        artifact = {
            "url": response_metadata["endpoint"],
            "sha256": response_metadata["sha256"],
            "size": response_metadata["byte_length"],
            "content_type": response_metadata["content_type"],
        }
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            fetched_at=fetched_at,
            raw_bytes=raw_bytes,
            metadata={
                **response_metadata,
                **evidence,
                "artifacts": [artifact],
            },
        )

    def yield_curve(self, *, year: int | None = None) -> ProviderResult:
        return self._curve(
            curve="daily_treasury_yield_curve",
            fields=self.NOMINAL_FIELDS,
            year=year or datetime.now(UTC).year,
        )

    def real_yield_curve(self, *, year: int | None = None) -> ProviderResult:
        return self._curve(
            curve="daily_treasury_real_yield_curve",
            fields=self.REAL_FIELDS,
            year=year or datetime.now(UTC).year,
        )


class FiscalDataProvider(HTTPProvider):
    """Treasury FiscalData adapter for the Daily Treasury Statement."""

    key = "treasury-fiscal-data"
    base_url = "https://api.fiscaldata.treasury.gov"

    def tga(self, *, page_size: int = 400) -> ProviderResult:
        dataset = "daily-treasury-statement:tga"
        payload, failure = self._get_json(
            dataset,
            "/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance",
            params={"sort": "-record_date", "page[size]": min(int(page_size), 10000)},
        )
        if failure:
            return failure
        records = []
        for item in payload.get("data", []):
            if item.get("account_type") != "Treasury General Account (TGA) Closing Balance":
                continue
            value = _decimal_or_none(item.get("open_today_bal"))
            if value is None:
                continue
            records.append(
                {
                    "series_id": "TGA",
                    "date": item.get("record_date"),
                    "value": value,
                    "metadata": {"unit": "USD millions", "account_type": item["account_type"]},
                }
            )
        return ProviderResult(provider=self.key, dataset=dataset, records=records)

    def treasury_auctions(
        self,
        *,
        page_size: int = 1000,
        as_of_date: date | None = None,
    ) -> ProviderResult:
        dataset = "treasury-securities-auctions"
        today_et = as_of_date or datetime.now(
            ZoneInfo("America/New_York")
        ).date()
        bounded_page_size = max(1, min(int(page_size), 10000))
        auction_start = today_et - timedelta(days=90)
        auction_end = today_et + timedelta(days=14)
        issue_end = today_et + timedelta(days=14)
        endpoint = "/services/api/fiscal_service/v1/accounting/od/auctions_query"
        requests = (
            {
                "name": "auction_window",
                "filter": (
                    f"auction_date:gte:{auction_start.isoformat()},"
                    f"auction_date:lt:{auction_end.isoformat()}"
                ),
                "sort": "auction_date,cusip",
                "lower": auction_start.isoformat(),
                "upper_exclusive": auction_end.isoformat(),
                "date_field": "auction_date",
                "priority": 2,
            },
            {
                "name": "issue_window",
                "filter": (
                    f"issue_date:gte:{today_et.isoformat()},"
                    f"issue_date:lt:{issue_end.isoformat()}"
                ),
                "sort": "issue_date,auction_date,cusip",
                "lower": today_et.isoformat(),
                "upper_exclusive": issue_end.isoformat(),
                "date_field": "issue_date",
                "priority": 1,
            },
        )
        numeric_fields = (
            "offering_amt",
            "total_tendered",
            "total_accepted",
            "bid_to_cover_ratio",
            "high_yield",
            "indirect_bidder_accepted",
            "direct_bidder_accepted",
            "primary_dealer_accepted",
        )
        requested_fields = (
            "record_date",
            "cusip",
            "security_type",
            "security_term",
            "announcemt_date",
            "auction_date",
            "issue_date",
            "maturity_date",
            *numeric_fields,
        )
        slice_states: list[dict[str, Any]] = []
        slice_records: list[tuple[int, dict[str, Any]]] = []
        coverage_complete = True
        for request_spec in requests:
            params = {
                "fields": ",".join(requested_fields),
                "filter": request_spec["filter"],
                "sort": request_spec["sort"],
                "page[size]": bounded_page_size,
            }
            payload, failure = self._get_json(dataset, endpoint, params=params)
            if failure:
                slice_states.append(
                    {
                        **request_spec,
                        "page_size": bounded_page_size,
                        "returned_count": 0,
                        "total_count": None,
                        "total_pages": None,
                        "count": None,
                        "coverage_complete": False,
                        "error": failure.error,
                    }
                )
                return ProviderResult(
                    provider=self.key,
                    dataset=dataset,
                    error=f"{request_spec['name']}: {failure.error}"[:2000],
                    metadata={
                        "as_of_date_et": today_et.isoformat(),
                        "timezone": "America/New_York",
                        "coverage_complete": False,
                        "slices": slice_states,
                    },
                )
            data = payload.get("data") if isinstance(payload, dict) else None
            meta = payload.get("meta") if isinstance(payload, dict) else None
            rows = data if isinstance(data, list) else []

            def meta_int(key: str) -> int | None:
                if not isinstance(meta, dict) or meta.get(key) in (None, ""):
                    return None
                try:
                    return int(meta[key])
                except (TypeError, ValueError):
                    return None

            total_count = meta_int("total-count")
            total_pages = meta_int("total-pages")
            count = meta_int("count")
            valid_rows: list[dict[str, Any]] = []
            rejected_count = 0
            slice_lower = date.fromisoformat(str(request_spec["lower"]))
            slice_upper = date.fromisoformat(
                str(request_spec["upper_exclusive"])
            )
            for item in rows:
                if not isinstance(item, dict):
                    rejected_count += 1
                    continue
                required_values = (item.get("cusip"), item.get("auction_date"))
                if not all(required_values):
                    rejected_count += 1
                    continue
                date_fields = (
                    "record_date",
                    "announcemt_date",
                    "auction_date",
                    "issue_date",
                    "maturity_date",
                )
                try:
                    for field in date_fields:
                        raw_date = item.get(field)
                        if raw_date not in (None, "", "null"):
                            date.fromisoformat(str(raw_date))
                except ValueError:
                    rejected_count += 1
                    continue
                slice_raw_date = item.get(str(request_spec["date_field"]))
                if slice_raw_date in (None, "", "null"):
                    rejected_count += 1
                    continue
                slice_date = date.fromisoformat(str(slice_raw_date))
                if not slice_lower <= slice_date < slice_upper:
                    rejected_count += 1
                    continue
                numeric_values_are_valid = all(
                    item.get(field) in (None, "", "null")
                    or _decimal_or_none(item.get(field)) is not None
                    for field in numeric_fields
                )
                if not numeric_values_are_valid:
                    rejected_count += 1
                    continue
                valid_rows.append(item)

            empty_complete = (
                total_count == 0
                and not rows
                and total_pages in {0, 1}
            )
            nonempty_complete = (
                total_count is not None
                and total_count > 0
                and total_pages == 1
                and total_count == len(rows)
                and total_count <= bounded_page_size
            )
            slice_complete = bool(
                isinstance(data, list)
                and isinstance(meta, dict)
                and count == len(rows)
                and rejected_count == 0
                and len(valid_rows) == len(rows)
                and (empty_complete or nonempty_complete)
            )
            coverage_complete = coverage_complete and slice_complete
            slice_states.append(
                {
                    **request_spec,
                    "page_size": bounded_page_size,
                    "returned_count": len(rows),
                    "normalized_count": len(valid_rows),
                    "rejected_count": rejected_count,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "count": count,
                    "coverage_complete": slice_complete,
                }
            )
            for item in valid_rows:
                record = {
                    "record_date": item.get("record_date"),
                    "cusip": item["cusip"],
                    "security_type": item.get("security_type") or "",
                    "security_term": item.get("security_term") or "",
                    "announcement_date": item.get("announcemt_date"),
                    "auction_date": item["auction_date"],
                    "issue_date": item.get("issue_date"),
                    "maturity_date": item.get("maturity_date"),
                }
                record.update(
                    {
                        field: _decimal_or_none(item.get(field))
                        for field in numeric_fields
                    }
                )
                slice_records.append((int(request_spec["priority"]), record))

        metadata = {
            "as_of_date_et": today_et.isoformat(),
            "timezone": "America/New_York",
            "coverage_complete": coverage_complete,
            "allow_empty_success": coverage_complete,
            "record_date_semantics": (
                "FiscalData record_date is not used as fetched_at or as_of; "
                "ProviderResult.fetched_at records the actual retrieval time"
            ),
            "slices": slice_states,
        }
        if not coverage_complete:
            return ProviderResult(
                provider=self.key,
                dataset=dataset,
                records=[],
                metadata={**metadata, "quality_status": "partial"},
            )

        merged: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        for priority, candidate in slice_records:
            identity = (str(candidate["cusip"]), str(candidate["auction_date"]))
            current = merged.get(identity)
            if current is None:
                merged[identity] = (priority, candidate)
                continue

            def completeness(record: dict[str, Any]) -> int:
                return sum(value not in (None, "") for value in record.values())

            current_priority, current_record = current
            conflicts = {
                field
                for field in candidate
                if field != "record_date"
                and candidate.get(field) not in (None, "")
                and current_record.get(field) not in (None, "")
                and candidate[field] != current_record[field]
            }
            if conflicts:
                return ProviderResult(
                    provider=self.key,
                    dataset=dataset,
                    error=(
                        "conflicting duplicate auction identity "
                        f"{identity[0]} {identity[1]}: {', '.join(sorted(conflicts))}"
                    )[:2000],
                    metadata={
                        **metadata,
                        "coverage_complete": False,
                        "conflicting_identity": list(identity),
                        "conflicting_fields": sorted(conflicts),
                    },
                )
            candidate_rank = (
                str(candidate.get("record_date") or ""),
                completeness(candidate),
                priority,
            )
            current_rank = (
                str(current_record.get("record_date") or ""),
                completeness(current_record),
                current_priority,
            )
            preferred, fallback = (
                (candidate, current_record)
                if candidate_rank > current_rank
                else (current_record, candidate)
            )
            combined = dict(preferred)
            for field, value in fallback.items():
                if combined.get(field) in (None, "") and value not in (None, ""):
                    combined[field] = value
            merged[identity] = (max(priority, current_priority), combined)

        records = [
            item[1]
            for _identity, item in sorted(
                merged.items(),
                key=lambda pair: (
                    str(pair[1][1].get("auction_date") or ""),
                    str(pair[1][1].get("cusip") or ""),
                ),
            )
        ]
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                **metadata,
                "merged_record_count": len(records),
                "deduplicated_record_count": len(slice_records) - len(records),
            },
        )


class BLSProvider(HTTPProvider):
    """BLS Public Data API adapter.

    A free registration key raises the official request limits.  The basic
    request signature is kept within the smaller unregistered limits when no
    key is configured.
    """

    key = "bls"
    base_url = "https://api.bls.gov"

    @classmethod
    def parse_series_json_bytes(
        cls,
        raw_bytes: bytes,
        *,
        series_ids: list[str] | tuple[str, ...],
        start_year: int,
        end_year: int,
        fetched_at: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Replay one exact BLS Public Data API response into normalized rows."""

        requested_series = tuple(str(item) for item in series_ids)
        if (
            not raw_bytes
            or not requested_series
            or len(set(requested_series)) != len(requested_series)
            or any(not item for item in requested_series)
            or start_year > end_year
        ):
            raise ValueError("BLS request identity or raw JSON bytes are invalid")
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        try:
            payload = json.loads(raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"BLS JSON parse failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("BLS response root is not an object")
        if payload.get("status") != "REQUEST_SUCCEEDED":
            messages = payload.get("message")
            rendered = "; ".join(messages) if isinstance(messages, list) else ""
            raise ValueError(rendered or "BLS request failed")
        results = payload.get("Results")
        response_series = results.get("series") if isinstance(results, dict) else None
        if not isinstance(response_series, list):
            raise ValueError("BLS response lacks a series array")

        requested = set(requested_series)
        returned_series: set[str] = set()
        seen_rows: set[tuple[str, int, int]] = set()
        latest_value_dates: dict[str, date] = {}
        records: list[dict[str, Any]] = []
        for series in response_series:
            if not isinstance(series, dict):
                raise ValueError("BLS response contains a malformed series")
            series_id = str(series.get("seriesID") or "")
            if series_id not in requested or series_id in returned_series:
                raise ValueError("BLS response series identity is unexpected or duplicated")
            data = series.get("data")
            if not isinstance(data, list):
                raise ValueError("BLS response series lacks a data array")
            returned_series.add(series_id)
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("BLS response contains a malformed observation")
                period = str(item.get("period") or "")
                if period == "M13":
                    continue
                if not re.fullmatch(r"M(?:0[1-9]|1[0-2])", period):
                    raise ValueError("BLS observation period is invalid")
                try:
                    year = int(item.get("year"))
                    month = int(period[1:])
                    value = Decimal(str(item.get("value")))
                    value_date = date(year, month, 1)
                except (ArithmeticError, TypeError, ValueError) as exc:
                    raise ValueError("BLS observation is malformed") from exc
                if (
                    year < start_year
                    or year > end_year
                    or value_date > fetched_at.astimezone(UTC).date()
                    or not value.is_finite()
                ):
                    raise ValueError("BLS observation is outside the request contract")
                identity = (series_id, year, month)
                if identity in seen_rows:
                    raise ValueError("BLS response contains a duplicate observation")
                seen_rows.add(identity)
                latest_value_dates[series_id] = max(
                    value_date,
                    latest_value_dates.get(series_id, value_date),
                )
                footnotes = item.get("footnotes") or []
                if not isinstance(footnotes, list) or any(
                    not isinstance(footnote, dict) for footnote in footnotes
                ):
                    raise ValueError("BLS observation footnotes are malformed")
                preliminary = any(
                    str(footnote.get("code") or "").upper() == "P"
                    or "preliminary" in str(footnote.get("text") or "").lower()
                    for footnote in footnotes
                )
                records.append(
                    {
                        "series_id": series_id,
                        "date": value_date.isoformat(),
                        "value": value,
                        "quality_status": "estimated" if preliminary else "fresh",
                        "metadata": {
                            "period_name": item.get("periodName"),
                            "latest": item.get("latest") == "true",
                            "footnotes": footnotes,
                            "preliminary": preliminary,
                        },
                    }
                )
        records.sort(key=lambda item: (str(item["series_id"]), str(item["date"])))
        messages = payload.get("message") or []
        if not isinstance(messages, list):
            raise ValueError("BLS response messages are malformed")
        missing_series = sorted(requested - returned_series)
        return records, {
            "requested_series": list(requested_series),
            "returned_series": sorted(returned_series),
            "missing_series": missing_series,
            "messages": list(messages),
            "quality_status": "partial" if missing_series else "complete",
            "start_year": start_year,
            "end_year": end_year,
            "latest_value_dates": {
                key: value.isoformat()
                for key, value in sorted(latest_value_dates.items())
            },
        }

    def series(
        self,
        series_ids: list[str] | tuple[str, ...],
        *,
        start_year: int,
        end_year: int,
    ) -> ProviderResult:
        dataset = "series:" + ",".join(series_ids)
        body: dict[str, Any] = {
            "seriesid": list(series_ids),
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        registration_key = os.getenv("BLS_REGISTRATION_KEY", "")
        if registration_key:
            body["registrationkey"] = registration_key
        _payload, raw_bytes, transport_metadata, failure = self._post_json_with_raw(
            dataset,
            "/publicAPI/v2/timeseries/data/",
            json=body,
        )
        if failure:
            return failure
        fetched_at = datetime.now(UTC)
        try:
            records, replay_metadata = self.parse_series_json_bytes(
                raw_bytes or b"",
                series_ids=series_ids,
                start_year=start_year,
                end_year=end_year,
                fetched_at=fetched_at,
            )
        except ValueError as exc:
            return ProviderResult.failure(
                self.key,
                dataset,
                str(exc),
            )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            fetched_at=fetched_at,
            metadata={
                **transport_metadata,
                **replay_metadata,
                "request_witness": {
                    "series_ids": list(series_ids),
                    "start_year": start_year,
                    "end_year": end_year,
                },
            },
            raw_bytes=raw_bytes,
        )


class CFTCProvider(HTTPProvider):
    """CFTC Public Reporting Environment Commitments of Traders adapter."""

    key = "cftc"
    base_url = "https://publicreporting.cftc.gov"
    DATASETS = {
        "tff-futures": "gpe5-46if",
        "tff-combined": "yw9f-hn96",
    }

    def positions(
        self,
        *,
        report_type: str = "tff-futures",
        start_date: str | None = None,
        limit: int = 50000,
    ) -> ProviderResult:
        dataset_id = self.DATASETS.get(report_type)
        dataset = f"cot:{report_type}"
        if dataset_id is None:
            return ProviderResult.failure(self.key, dataset, f"unsupported report: {report_type}")
        params: dict[str, Any] = {
            "$select": (
                ":created_at,:updated_at,report_date_as_yyyy_mm_dd,"
                "market_and_exchange_names,contract_market_name,"
                "cftc_contract_market_code,open_interest_all,"
                "dealer_positions_long_all,dealer_positions_short_all,"
                "asset_mgr_positions_long,asset_mgr_positions_short,"
                "lev_money_positions_long,lev_money_positions_short,"
                "other_rept_positions_long,other_rept_positions_short,"
                "nonrept_positions_long_all,nonrept_positions_short_all"
            ),
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": min(int(limit), 50000),
        }
        if start_date:
            params["$where"] = f"report_date_as_yyyy_mm_dd >= '{start_date}T00:00:00.000'"
        payload, failure = self._get_json(dataset, f"/resource/{dataset_id}.json", params=params)
        if failure:
            return failure
        if not isinstance(payload, list):
            return ProviderResult.failure(self.key, dataset, "unexpected PRE response shape")
        groups = {
            "dealer": ("dealer_positions_long_all", "dealer_positions_short_all"),
            "asset-manager": ("asset_mgr_positions_long", "asset_mgr_positions_short"),
            "leveraged-money": ("lev_money_positions_long", "lev_money_positions_short"),
            "other-reportables": (
                "other_rept_positions_long",
                "other_rept_positions_short",
            ),
            "non-reportables": (
                "nonrept_positions_long_all",
                "nonrept_positions_short_all",
            ),
        }
        records = []
        missing_publication_timestamps = 0
        for item in payload:
            market_code = item.get("cftc_contract_market_code")
            report_date = (item.get("report_date_as_yyyy_mm_dd") or "")[:10]
            if not market_code or not report_date:
                continue
            published_at = item.get(":created_at")
            if not published_at:
                missing_publication_timestamps += 1
            open_interest = _decimal_or_none(item.get("open_interest_all"))
            for trader_group, (long_key, short_key) in groups.items():
                long_positions = _decimal_or_none(item.get(long_key))
                short_positions = _decimal_or_none(item.get(short_key))
                if long_positions is None or short_positions is None:
                    continue
                records.append(
                    {
                        "report_type": report_type,
                        "report_date": report_date,
                        "published_at": published_at,
                        "source_updated_at": item.get(":updated_at"),
                        "market_code": market_code,
                        "market_name": (
                            item.get("market_and_exchange_names")
                            or item.get("contract_market_name")
                            or market_code
                        ),
                        "trader_group": trader_group,
                        "long_positions": int(long_positions),
                        "short_positions": int(short_positions),
                        "open_interest": int(open_interest) if open_interest is not None else None,
                    }
                )
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=records,
            metadata={
                "dataset_id": dataset_id,
                "source_rows": len(payload),
                "missing_publication_timestamps": missing_publication_timestamps,
                "publication_timestamp_field": ":created_at",
                "source_revision_timestamp_field": ":updated_at",
                "report_date_semantics": "COT positions as of the report date, usually Tuesday",
                "publication_semantics": "PRE initial row publication timestamp",
                "quality_status": ("partial" if missing_publication_timestamps else "complete"),
            },
        )


class FederalReserveRSSProvider(HTTPProvider):
    """Federal Reserve Board RSS metadata for statements, releases and speeches."""

    key = "federal-reserve"
    base_url = "https://www.federalreserve.gov"
    FEEDS = {
        "press-monetary": "/feeds/press_monetary.xml",
        "press-all": "/feeds/press_all.xml",
        "speeches": "/feeds/speeches.xml",
    }

    def feed(self, feed_name: str, *, document_type: str) -> ProviderResult:
        dataset = f"rss:{feed_name}"
        path = self.FEEDS.get(feed_name)
        if path is None:
            return ProviderResult.failure(self.key, dataset, f"unsupported feed: {feed_name}")
        payload, failure = self._get_text(dataset, path)
        if failure:
            return failure
        try:
            root = ElementTree.fromstring((payload or "").lstrip("\ufeff"))
        except ElementTree.ParseError as exc:
            return ProviderResult.failure(self.key, dataset, f"ParseError: {exc}")
        records = []
        for item in root.findall(".//item"):
            values = {child.tag: (child.text or "").strip() for child in item}
            url = values.get("link") or values.get("guid")
            title = values.get("title")
            if not url or not title:
                continue
            filename = PurePosixPath(urlparse(url).path).stem
            published = parsedate_to_datetime(values["pubDate"]) if values.get("pubDate") else None
            records.append(
                {
                    "slug": filename.lower(),
                    "document_type": document_type,
                    "title": title,
                    "official_description": values.get("description", ""),
                    "published_at": published.isoformat() if published else None,
                    "original_url": url,
                    "category": values.get("category", ""),
                }
            )
        return ProviderResult(provider=self.key, dataset=dataset, records=records)


class SECProvider(HTTPProvider):
    """SEC submissions and company-facts adapter."""

    key = "sec"
    base_url = "https://data.sec.gov"
    min_request_interval = 0.2
    max_retries = 3

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
        now: Any = lambda: datetime.now(UTC),
        **kwargs: Any,
    ) -> None:
        configured_user_agent = os.getenv("SEC_USER_AGENT", "") if user_agent is None else user_agent
        self.user_agent = str(configured_user_agent or "").strip()
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._last_request_at: float | None = None
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        headers.setdefault("Accept-Encoding", "gzip, deflate")
        self._request_headers = headers
        super().__init__(headers=headers, **kwargs)

    def _require_identity(self, dataset: str) -> ProviderResult | None:
        if self.user_agent:
            return None
        return ProviderResult.skip(
            self.key,
            dataset,
            "SEC_USER_AGENT is not configured; the SEC job is skipped",
        )

    @staticmethod
    def normalize_cik(cik: str | int) -> str:
        digits = "".join(character for character in str(cik) if character.isdigit())
        if not digits:
            raise ValueError("CIK must contain digits")
        return digits.zfill(10)

    def submissions(self, cik: str | int) -> ProviderResult:
        normalized = self.normalize_cik(cik)
        dataset = f"submissions:{normalized}"
        if skipped := self._require_identity(dataset):
            return skipped
        return self._sec_json(dataset, f"/submissions/CIK{normalized}.json")

    def company_facts(self, cik: str | int) -> ProviderResult:
        normalized = self.normalize_cik(cik)
        dataset = f"companyfacts:{normalized}"
        if skipped := self._require_identity(dataset):
            return skipped
        return self._sec_json(dataset, f"/api/xbrl/companyfacts/CIK{normalized}.json")

    def _sec_json(self, dataset: str, path: str) -> ProviderResult:
        """Fetch JSON while retaining the exact response bytes for audit storage."""

        base_url = str(getattr(self.client, "base_url", "") or "")
        request_path = path if base_url else f"{self.base_url}{path}"
        response = None
        retryable_statuses = {403, 429, 500, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            elapsed = (
                self._clock() - self._last_request_at
                if self._last_request_at is not None
                else self.min_request_interval
            )
            if elapsed < self.min_request_interval:
                self._sleep(self.min_request_interval - elapsed)
            self._last_request_at = self._clock()
            try:
                response = self.client.get(request_path, headers=self._request_headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    return ProviderResult.failure(
                        self.key, dataset, f"{type(exc).__name__}: {exc}"
                    )
                self._sleep(self._retry_delay(None, attempt))
                continue
            if response.status_code not in retryable_statuses or attempt >= self.max_retries:
                break
            self._sleep(self._retry_delay(response, attempt))
        if response is None:
            return ProviderResult.failure(self.key, dataset, "SEC request returned no response")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            # Parse/validation errors are deterministic input failures and are
            # deliberately not retried.
            return ProviderResult.failure(self.key, dataset, f"{type(exc).__name__}: {exc}")
        raw = bytes(response.content)
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=[payload],
            fetched_at=self._now(),
            raw_bytes=raw,
            metadata={
                "endpoint": str(response.url),
                "content_type": response.headers.get("content-type", ""),
                "byte_length": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        """Return a bounded Retry-After/backoff delay for retryable failures."""

        retry_after = response.headers.get("retry-after") if response is not None else None
        if retry_after:
            try:
                return min(8.0, max(0.2, float(retry_after)))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    now = self._now()
                    if now.tzinfo is None:
                        now = now.replace(tzinfo=UTC)
                    return min(8.0, max(0.2, (retry_at - now).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(8.0, max(0.2, 0.5 * (2**attempt)))


class GitHubProvider(HTTPProvider):
    key = "github"
    base_url = "https://api.github.com"

    def __init__(self, token: str | None = None, **kwargs: Any) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/vnd.github+json")
        headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        super().__init__(headers=headers, **kwargs)

    def repository(self, repo: str) -> ProviderResult:
        dataset = f"repository:{repo}"
        if repo.count("/") != 1:
            return ProviderResult.failure(self.key, dataset, "repo must be in owner/name form")
        payload, failure = self._get_json(dataset, f"/repos/{repo}")
        if failure:
            return failure
        record = {
            "repo": payload.get("full_name", repo),
            "description": payload.get("description") or "",
            "stars": payload.get("stargazers_count", 0),
            "forks": payload.get("forks_count", 0),
            "open_issues": payload.get("open_issues_count", 0),
            "pushed_at": payload.get("pushed_at"),
            "homepage": payload.get("html_url", f"https://github.com/{repo}"),
            "topics": payload.get("topics", []),
            "archived": bool(payload.get("archived", False)),
            "is_fork": bool(payload.get("fork", False)),
            "license": (payload.get("license") or {}).get("spdx_id", ""),
        }
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=[record],
            metadata={"authenticated": bool(self.token)},
        )


class OKXProvider(HTTPProvider):
    key = "okx"
    base_url = "https://www.okx.com"

    def market_tickers(self, inst_type: str = "SPOT") -> ProviderResult:
        dataset = f"market-tickers:{inst_type.upper()}"
        payload, failure = self._get_json(
            dataset, "/api/v5/market/tickers", params={"instType": inst_type.upper()}
        )
        if failure:
            return failure
        if str(payload.get("code", "0")) != "0":
            return ProviderResult.failure(self.key, dataset, payload.get("msg", "OKX API error"))
        return ProviderResult(
            provider=self.key, dataset=dataset, records=list(payload.get("data", []))
        )

    def ticker(self, instrument_id: str) -> ProviderResult:
        dataset = f"ticker:{instrument_id}"
        payload, failure = self._get_json(
            dataset, "/api/v5/market/ticker", params={"instId": instrument_id}
        )
        if failure:
            return failure
        if str(payload.get("code", "0")) != "0":
            return ProviderResult.failure(self.key, dataset, payload.get("msg", "OKX API error"))
        return ProviderResult(
            provider=self.key, dataset=dataset, records=list(payload.get("data", []))
        )


class DeribitProvider(HTTPProvider):
    key = "deribit"
    base_url = "https://www.deribit.com"

    def book_summary(self, currency: str = "BTC", kind: str = "option") -> ProviderResult:
        dataset = f"book-summary:{currency.upper()}:{kind}"
        payload, failure = self._get_json(
            dataset,
            "/api/v2/public/get_book_summary_by_currency",
            params={"currency": currency.upper(), "kind": kind},
        )
        if failure:
            return failure
        if payload.get("error"):
            return ProviderResult.failure(self.key, dataset, str(payload["error"]))
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=list(payload.get("result", [])),
        )

    def instruments(
        self, currency: str = "BTC", kind: str = "option", expired: bool = False
    ) -> ProviderResult:
        dataset = f"instruments:{currency.upper()}:{kind}"
        payload, failure = self._get_json(
            dataset,
            "/api/v2/public/get_instruments",
            params={"currency": currency.upper(), "kind": kind, "expired": str(expired).lower()},
        )
        if failure:
            return failure
        if payload.get("error"):
            return ProviderResult.failure(self.key, dataset, str(payload["error"]))
        return ProviderResult(
            provider=self.key,
            dataset=dataset,
            records=list(payload.get("result", [])),
        )


# Conventional aliases keep imports ergonomic without weakening the canonical
# acronym-preserving class names.
FredProvider = FREDProvider
SecProvider = SECProvider
GithubProvider = GitHubProvider
OkxProvider = OKXProvider
NyFedMarketsProvider = NYFedMarketsProvider
TreasuryProvider = TreasuryRatesProvider
FiscalDataTreasuryProvider = FiscalDataProvider
