"""Pure, dependency-free financial calculations used by the research platform.

The functions in this module deliberately operate on plain Python numbers and
collections.  That keeps ingestion jobs deterministic and makes every derived
metric independently testable.  Percent-valued outputs are documented at the
function boundary; Black--Scholes inputs use decimal rates and volatility.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def net_liquidity(
    fed_assets: float,
    treasury_general_account: float,
    reverse_repo: float,
) -> float:
    """Return the common Fed net-liquidity proxy: assets - TGA - RRP.

    All three inputs must use the same unit (for example USD millions).  The
    output therefore keeps that unit and is not silently scaled.
    """

    return (
        _number(fed_assets, "fed_assets")
        - _number(treasury_general_account, "treasury_general_account")
        - _number(reverse_repo, "reverse_repo")
    )


def yield_spread(long_yield: float, short_yield: float, *, basis_points: bool = False) -> float:
    """Return ``long_yield - short_yield`` in percentage points or basis points."""

    spread = _number(long_yield, "long_yield") - _number(short_yield, "short_yield")
    return spread * 100.0 if basis_points else spread


def yield_curve_spreads(
    yields: Mapping[str, float], *, basis_points: bool = False
) -> dict[str, float]:
    """Calculate the platform's canonical 2s10s, 3m10s and 5s30s spreads.

    Accepted tenor aliases include ``2y``/``2yr``/``DGS2`` and their
    equivalents for the other required maturities.
    """

    normalized = {str(key).lower().replace("_", ""): value for key, value in yields.items()}

    def tenor(*aliases: str) -> float:
        for alias in aliases:
            key = alias.lower().replace("_", "")
            if key in normalized:
                return normalized[key]
        raise KeyError(f"missing yield tenor; expected one of {', '.join(aliases)}")

    y3m = tenor("3m", "3mo", "dgs3mo")
    y2 = tenor("2y", "2yr", "dgs2")
    y5 = tenor("5y", "5yr", "dgs5")
    y10 = tenor("10y", "10yr", "dgs10")
    y30 = tenor("30y", "30yr", "dgs30")
    return {
        "2s10s": yield_spread(y10, y2, basis_points=basis_points),
        "3m10s": yield_spread(y10, y3m, basis_points=basis_points),
        "5s30s": yield_spread(y30, y5, basis_points=basis_points),
    }


def pearson_correlation(left: Sequence[float], right: Sequence[float]) -> float:
    """Return the population Pearson correlation for two equal-length series."""

    if len(left) != len(right):
        raise ValueError("correlation series must have equal lengths")
    if len(left) < 2:
        raise ValueError("correlation needs at least two observations")
    xs = [_number(value, "left value") for value in left]
    ys = [_number(value, "right value") for value in right]
    mean_x = math.fsum(xs) / len(xs)
    mean_y = math.fsum(ys) / len(ys)
    covariance = math.fsum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    variance_x = math.fsum((x - mean_x) ** 2 for x in xs)
    variance_y = math.fsum((y - mean_y) ** 2 for y in ys)
    denominator = math.sqrt(variance_x * variance_y)
    if denominator == 0:
        raise ValueError("correlation is undefined for a constant series")
    return covariance / denominator


def correlation(
    left: Sequence[float], right: Sequence[float], *, window: int | None = None
) -> float:
    """Return correlation for the full series or the most recent ``window`` values."""

    if window is not None:
        if window < 2:
            raise ValueError("window must be at least two")
        if len(left) < window or len(right) < window:
            raise ValueError("window is larger than the available series")
        left = left[-window:]
        right = right[-window:]
    return pearson_correlation(left, right)


calculate_correlation = correlation


def rolling_correlation(
    left: Sequence[float], right: Sequence[float], window: int
) -> list[float | None]:
    """Return an input-aligned rolling correlation series.

    Values before the first complete window are ``None``.  A constant window
    also produces ``None`` instead of aborting the complete time series.
    """

    if len(left) != len(right):
        raise ValueError("correlation series must have equal lengths")
    if window < 2:
        raise ValueError("window must be at least two")
    result: list[float | None] = [None] * min(window - 1, len(left))
    for end in range(window, len(left) + 1):
        try:
            result.append(pearson_correlation(left[end - window : end], right[end - window : end]))
        except ValueError:
            result.append(None)
    return result


def percentile_rank(values: Iterable[float], value: float | None = None) -> float:
    """Return a weak percentile rank in the inclusive range 0..100.

    A weak rank is the percentage of observations less than or equal to the
    target.  If ``value`` is omitted, the latest observation is ranked against
    the full supplied history.
    """

    series = [_number(item, "percentile value") for item in values]
    if not series:
        raise ValueError("percentile rank needs at least one observation")
    target = series[-1] if value is None else _number(value, "value")
    return 100.0 * sum(item <= target for item in series) / len(series)


def annualized_btc_basis(
    spot_price: float,
    futures_price: float,
    days_to_expiry: float,
    *,
    as_percent: bool = True,
) -> float:
    """Annualize a simple BTC futures basis on an ACT/365 convention."""

    spot = _number(spot_price, "spot_price")
    future = _number(futures_price, "futures_price")
    days = _number(days_to_expiry, "days_to_expiry")
    if spot <= 0:
        raise ValueError("spot_price must be positive")
    if days <= 0:
        raise ValueError("days_to_expiry must be positive")
    basis = ((future - spot) / spot) * (365.0 / days)
    return basis * 100.0 if as_percent else basis


annualized_basis = annualized_btc_basis


@dataclass(frozen=True)
class OptionGreeks(Mapping[str, float]):
    """Black--Scholes Greeks with both attribute and mapping access."""

    delta: float
    gamma: float
    vanna: float
    charm: float

    def __getitem__(self, key: str) -> float:
        if key not in {"delta", "gamma", "vanna", "charm"}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("delta", "gamma", "vanna", "charm"))

    def __len__(self) -> int:
        return 4


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def black_scholes_greeks(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    option_type: str = "call",
) -> OptionGreeks:
    """Return delta, gamma, vanna and charm under Black--Scholes.

    ``time_to_expiry`` is measured in years.  Rates and volatility are decimal
    values (``0.20`` means 20%).  Charm is calendar-time delta decay per year;
    vanna is delta sensitivity to a one-unit volatility change.
    """

    s = _number(spot, "spot")
    k = _number(strike, "strike")
    t = _number(time_to_expiry, "time_to_expiry")
    sigma = _number(volatility, "volatility")
    r = _number(risk_free_rate, "risk_free_rate")
    q = _number(dividend_yield, "dividend_yield")
    kind = option_type.lower()
    if s <= 0 or k <= 0:
        raise ValueError("spot and strike must be positive")
    if t <= 0:
        raise ValueError("time_to_expiry must be positive")
    if sigma <= 0:
        raise ValueError("volatility must be positive")
    if kind not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")

    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount_q = math.exp(-q * t)
    pdf = _normal_pdf(d1)
    call_delta = discount_q * _normal_cdf(d1)
    delta = call_delta if kind == "call" else call_delta - discount_q
    gamma = discount_q * pdf / (s * sigma * sqrt_t)
    vanna = -discount_q * pdf * d2 / sigma

    common_charm = (
        -discount_q * pdf * (2.0 * (r - q) * t - d2 * sigma * sqrt_t) / (2.0 * t * sigma * sqrt_t)
    )
    if kind == "call":
        charm = q * discount_q * _normal_cdf(d1) + common_charm
    else:
        charm = -q * discount_q * _normal_cdf(-d1) + common_charm
    return OptionGreeks(delta=delta, gamma=gamma, vanna=vanna, charm=charm)


def option_delta(*args: Any, **kwargs: Any) -> float:
    return black_scholes_greeks(*args, **kwargs).delta


def option_gamma(*args: Any, **kwargs: Any) -> float:
    return black_scholes_greeks(*args, **kwargs).gamma


def option_vanna(*args: Any, **kwargs: Any) -> float:
    return black_scholes_greeks(*args, **kwargs).vanna


def option_charm(*args: Any, **kwargs: Any) -> float:
    return black_scholes_greeks(*args, **kwargs).charm


def _option_sign(option_type: str) -> float:
    kind = option_type.lower()
    if kind == "call":
        return 1.0
    if kind == "put":
        return -1.0
    raise ValueError("option_type must be 'call' or 'put'")


def calculate_gex(
    gamma: float,
    open_interest: float,
    spot: float,
    option_type: str,
    *,
    contract_multiplier: float = 100.0,
    per_one_percent_move: bool = True,
) -> float:
    """Calculate signed dollar gamma exposure.

    Puts use the conventional dealer-dashboard negative sign.  The default
    result is exposure for a one-percent underlying move.
    """

    scale = 0.01 if per_one_percent_move else 1.0
    return (
        _option_sign(option_type)
        * _number(gamma, "gamma")
        * _number(open_interest, "open_interest")
        * _number(contract_multiplier, "contract_multiplier")
        * _number(spot, "spot") ** 2
        * scale
    )


def calculate_dex(
    delta: float,
    open_interest: float,
    spot: float,
    *,
    contract_multiplier: float = 100.0,
) -> float:
    """Calculate dollar delta exposure; put deltas should be supplied as negative."""

    return (
        _number(delta, "delta")
        * _number(open_interest, "open_interest")
        * _number(contract_multiplier, "contract_multiplier")
        * _number(spot, "spot")
    )


def calculate_vanna_exposure(
    vanna: float,
    open_interest: float,
    spot: float,
    option_type: str = "call",
    *,
    contract_multiplier: float = 100.0,
    per_vol_point: bool = True,
) -> float:
    """Calculate signed dollar vanna exposure for a volatility change."""

    scale = 0.01 if per_vol_point else 1.0
    return (
        _option_sign(option_type)
        * _number(vanna, "vanna")
        * _number(open_interest, "open_interest")
        * _number(contract_multiplier, "contract_multiplier")
        * _number(spot, "spot")
        * scale
    )


def calculate_charm_exposure(
    charm: float,
    open_interest: float,
    spot: float,
    *,
    contract_multiplier: float = 100.0,
    per_day: bool = True,
) -> float:
    """Calculate dollar delta decay; Black--Scholes charm is annual by default."""

    scale = 1.0 / 365.0 if per_day else 1.0
    return (
        _number(charm, "charm")
        * _number(open_interest, "open_interest")
        * _number(contract_multiplier, "contract_multiplier")
        * _number(spot, "spot")
        * scale
    )


def _contract_value(contract: Any, key: str, default: Any = None) -> Any:
    if isinstance(contract, Mapping):
        return contract.get(key, default)
    return getattr(contract, key, default)


def calculate_max_pain(contracts: Iterable[Any], *, contract_multiplier: float = 100.0) -> float:
    """Return the settlement strike minimizing aggregate option-holder payoff.

    Contracts may be dictionaries or model-like objects with ``strike``,
    ``option_type`` and ``open_interest`` attributes.
    """

    rows = list(contracts)
    if not rows:
        raise ValueError("max pain needs at least one option contract")
    strikes = sorted({_number(_contract_value(row, "strike"), "strike") for row in rows})
    multiplier = _number(contract_multiplier, "contract_multiplier")
    pains: dict[float, float] = {}
    for settlement in strikes:
        total = 0.0
        for row in rows:
            strike = _number(_contract_value(row, "strike"), "strike")
            oi = _number(_contract_value(row, "open_interest", 0), "open_interest")
            kind = str(_contract_value(row, "option_type", "")).lower()
            if kind == "call":
                intrinsic = max(settlement - strike, 0.0)
            elif kind == "put":
                intrinsic = max(strike - settlement, 0.0)
            else:
                raise ValueError("option_type must be 'call' or 'put'")
            total += intrinsic * oi * multiplier
        pains[settlement] = total
    return min(strikes, key=lambda strike: (pains[strike], strike))


max_pain = calculate_max_pain
