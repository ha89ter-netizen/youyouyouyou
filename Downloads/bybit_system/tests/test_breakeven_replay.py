from copy import deepcopy

import pytest

from analytics.breakeven_replay import (
    PriceSample, ReplayTrade, activation_price, cost_adjusted_breakeven,
    performance, replay_trade,
)


def trade(side="long", trajectory=None, baseline=-1.0, funding=0.0):
    return ReplayTrade(
        trade_id=1, symbol="TESTUSDT", side=side, entry_price=100,
        quantity=1, initial_stop=98 if side == "long" else 102,
        baseline_pnl=baseline, entry_fee=0.05, funding_pnl=funding,
        tick_size=0.01, closed_at_ms=10_000,
        trajectory=[PriceSample(ts, price) for ts, price in (trajectory or [])],
    )


@pytest.mark.parametrize("side", ["long", "short"])
def test_cost_adjusted_breakeven_covers_commission_funding_and_slippage(side):
    result = cost_adjusted_breakeven(
        side=side, entry_price=100, quantity=2, entry_fee=0.11,
        expected_exit_fee_rate=0.00055, funding_pnl=-0.02,
        tick_size=0.01, expected_slippage_bps=2,
    )
    assert result["expected_net_pnl"] >= -0.01
    if side == "long":
        assert result["trigger_price"] > 100
        assert result["expected_fill_price"] < result["trigger_price"]
    else:
        assert result["trigger_price"] < 100
        assert result["expected_fill_price"] > result["trigger_price"]


def test_tick_rounding_is_conservative_for_long_and_short():
    long = cost_adjusted_breakeven(
        side="long", entry_price=100, quantity=1, entry_fee=0.001,
        expected_exit_fee_rate=0, funding_pnl=0, tick_size=0.1,
    )
    short = cost_adjusted_breakeven(
        side="short", entry_price=100, quantity=1, entry_fee=0.001,
        expected_exit_fee_rate=0, funding_pnl=0, tick_size=0.1,
    )
    assert long["trigger_price"] == 100.1
    assert short["trigger_price"] == 99.9


def test_percent_and_r_activation_are_directionally_symmetric():
    assert activation_price(trade("long"), "percent", 0.5) == 100.5
    assert activation_price(trade("short"), "percent", 0.5) == 99.5
    assert activation_price(trade("long"), "r", 0.5) == 101
    assert activation_price(trade("short"), "r", 0.5) == 99


@pytest.mark.parametrize("side,trajectory", [
    ("long", [(1, 100), (2, 101), (3, 100.2), (4, 99.9)]),
    ("short", [(1, 100), (2, 99), (3, 99.8), (4, 100.1)]),
])
def test_trajectory_activates_then_touches_breakeven(side, trajectory):
    row = replay_trade(
        trade(side, trajectory), threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055, expected_slippage_bps=0,
    )
    assert row["activation_reached"] is True
    assert row["breakeven_triggered"] is True
    assert row["classification"] == "rescued loser"


def test_trajectory_that_never_returns_keeps_baseline_exit():
    row = replay_trade(
        trade("long", [(1, 100), (2, 100.6), (3, 101), (4, 102)], baseline=2),
        threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055,
    )
    assert row["activation_reached"] is True
    assert row["breakeven_triggered"] is False
    assert row["counterfactual_result"] == 2
    assert row["classification"] == "unchanged"


def test_winner_can_be_damaged_without_hindsight_selection():
    row = replay_trade(
        trade("long", [(1, 100), (2, 101), (3, 100), (4, 104)], baseline=3),
        threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055,
    )
    assert row["breakeven_triggered"] is True
    assert row["classification"] == "damaged winner"
    assert row["breakeven_touch_timestamp_ms"] == 3


def test_loser_is_rescued_only_after_activation_occurs_first():
    not_activated = replay_trade(
        trade("long", [(1, 100), (2, 99), (3, 100.4)], baseline=-2),
        threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055,
    )
    activated = replay_trade(
        trade("long", [(1, 100), (2, 100.6), (3, 99)], baseline=-2),
        threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055,
    )
    assert not_activated["classification"] == "unchanged"
    assert activated["classification"] == "rescued loser"


@pytest.mark.parametrize("field", ["entry_fee", "tick_size", "trajectory"])
def test_insufficient_historical_data_stays_unresolved(field):
    value = trade("long", [(1, 100), (2, 101)])
    setattr(value, field, None if field != "trajectory" else [])
    row = replay_trade(
        value, threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055,
    )
    assert row["status"] == "insufficient_data"
    assert field in row["missing_fields"]
    assert row["counterfactual_result"] is None


def test_missing_funding_is_explicit_zero_assumption_not_silent_failure():
    row = replay_trade(
        trade("long", [(1, 100), (2, 101), (3, 100)], funding=None),
        threshold_type="percent", threshold=0.5,
        expected_exit_fee_rate=0.00055,
    )
    assert row["status"] == "replayed"
    assert row["funding_assumed_zero"] is True
    assert "funding" in row["missing_fields"]


def test_replay_is_deterministic_and_does_not_mutate_input():
    original = trade("short", [(3, 100), (1, 100), (2, 99), (4, 101)])
    before = deepcopy(original)
    kwargs = dict(threshold_type="r", threshold=0.25, expected_exit_fee_rate=0.00055)
    assert replay_trade(original, **kwargs) == replay_trade(original, **kwargs)
    assert original == before


def test_performance_reports_rescued_and_damaged_counts():
    rows = [
        replay_trade(trade("long", [(1, 100), (2, 101), (3, 99)], -2),
                     threshold_type="percent", threshold=.5, expected_exit_fee_rate=.00055),
        replay_trade(trade("long", [(1, 100), (2, 101), (3, 99)], 2),
                     threshold_type="percent", threshold=.5, expected_exit_fee_rate=.00055),
    ]
    metrics = performance(rows)
    assert metrics["losers_rescued"] == 1
    assert metrics["winners_damaged"] == 1
    assert metrics["breakeven_exits"] == 2
