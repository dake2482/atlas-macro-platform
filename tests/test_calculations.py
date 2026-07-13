from __future__ import annotations

import math

import pytest

from research.calculations import (
    annualized_btc_basis,
    black_scholes_greeks,
    calculate_charm_exposure,
    calculate_dex,
    calculate_gex,
    calculate_max_pain,
    calculate_vanna_exposure,
    net_liquidity,
    pearson_correlation,
    percentile_rank,
    rolling_correlation,
    yield_curve_spreads,
    yield_spread,
)


def test_net_liquidity_preserves_input_units():
    assert net_liquidity(8_000_000, 700_000, 500_000) == 6_800_000


def test_yield_curve_spreads_support_fred_tenors_and_basis_points():
    yields = {"DGS3MO": 5.25, "DGS2": 4.75, "DGS5": 4.40, "DGS10": 4.25, "DGS30": 4.50}

    assert yield_spread(4.25, 4.75) == pytest.approx(-0.50)
    assert yield_curve_spreads(yields, basis_points=True) == pytest.approx(
        {"2s10s": -50.0, "3m10s": -100.0, "5s30s": 10.0}
    )


def test_correlation_and_percentile_formulas():
    assert pearson_correlation([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson_correlation([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert rolling_correlation([1, 2, 3, 4], [2, 4, 6, 8], 3) == [
        None,
        None,
        pytest.approx(1.0),
        pytest.approx(1.0),
    ]
    assert percentile_rank([1, 2, 2, 4], 2) == 75.0


@pytest.mark.parametrize(
    ("spot", "future", "days", "expected"),
    [
        (100_000, 101_000, 30, 12.1666666667),
        (50_000, 49_000, 90, -8.1111111111),
    ],
)
def test_btc_basis_uses_act_365_simple_annualization(spot, future, days, expected):
    assert annualized_btc_basis(spot, future, days) == pytest.approx(expected)


def test_black_scholes_greeks_have_expected_parity_and_shape():
    call = black_scholes_greeks(
        spot=100,
        strike=100,
        time_to_expiry=1,
        volatility=0.20,
        risk_free_rate=0.05,
        option_type="call",
    )
    put = black_scholes_greeks(
        spot=100,
        strike=100,
        time_to_expiry=1,
        volatility=0.20,
        risk_free_rate=0.05,
        option_type="put",
    )

    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.delta - put.delta == pytest.approx(1.0)
    assert call.gamma == pytest.approx(put.gamma)
    assert call.gamma > 0
    assert all(math.isfinite(value) for value in call.values())


def test_option_exposures_apply_contract_and_display_conventions():
    assert calculate_gex(0.02, 1_000, 100, "call") == pytest.approx(200_000)
    assert calculate_gex(0.02, 1_000, 100, "put") == pytest.approx(-200_000)
    assert calculate_dex(0.50, 1_000, 100) == pytest.approx(5_000_000)
    assert calculate_vanna_exposure(0.25, 1_000, 100, "call") == pytest.approx(25_000)
    assert calculate_charm_exposure(0.365, 1_000, 100) == pytest.approx(10_000)


def test_max_pain_minimizes_total_holder_intrinsic_value():
    contracts = [
        {"strike": 90, "option_type": "call", "open_interest": 10},
        {"strike": 100, "option_type": "call", "open_interest": 40},
        {"strike": 100, "option_type": "put", "open_interest": 40},
        {"strike": 110, "option_type": "put", "open_interest": 10},
    ]

    assert calculate_max_pain(contracts) == 100


@pytest.mark.parametrize(
    ("function", "args"),
    [
        (net_liquidity, (float("nan"), 1, 1)),
        (pearson_correlation, ([1, 1], [2, 3])),
        (annualized_btc_basis, (100, 101, 0)),
        (calculate_max_pain, ([],)),
    ],
)
def test_invalid_formula_inputs_fail_loudly(function, args):
    with pytest.raises(ValueError):
        function(*args)
