"""Deterministic, read-only breakeven counterfactual primitives.

This module is deliberately disconnected from the live strategy engine.  It
cannot submit/amend an order and is not imported by the trading path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable, Optional


@dataclass(frozen=True)
class PriceSample:
    timestamp_ms: int
    price: float
    source: str = "public_trade"


@dataclass
class ReplayTrade:
    trade_id: int
    symbol: str
    side: str
    entry_price: float
    quantity: float
    initial_stop: float
    baseline_pnl: float
    entry_fee: Optional[float]
    funding_pnl: Optional[float]
    tick_size: Optional[float]
    closed_at_ms: int
    trajectory: list[PriceSample] = field(default_factory=list)
    anomalous_fill: bool = False
    normalized_baseline_pnl: Optional[float] = None
    data_notes: list[str] = field(default_factory=list)

    @property
    def initial_risk(self) -> Optional[float]:
        risk = self.quantity * abs(self.entry_price - self.initial_stop)
        return risk if risk > 0 else None


def _round_to_tick(value: float, tick_size: float, *, upward: bool) -> float:
    tick = Decimal(str(tick_size))
    units = Decimal(str(value)) / tick
    rounded = units.to_integral_value(rounding=ROUND_CEILING if upward else ROUND_FLOOR)
    return float(rounded * tick)


def cost_adjusted_breakeven(
    *, side: str, entry_price: float, quantity: float, entry_fee: float,
    expected_exit_fee_rate: float, funding_pnl: float, tick_size: float,
    expected_slippage_bps: float = 0.0,
) -> dict:
    """Price a stop whose expected fill nets zero after known/declared costs.

    ``funding_pnl`` is signed: positive is a credit, negative is a cost.
    The stop is moved farther into profit by the expected adverse slippage
    buffer.  Long stops round upward and short stops downward so tick rounding
    cannot turn the declared breakeven into a loss by itself.
    """
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    if min(entry_price, quantity, tick_size) <= 0:
        raise ValueError("entry_price, quantity and tick_size must be positive")
    if not 0 <= expected_exit_fee_rate < 1:
        raise ValueError("expected_exit_fee_rate must be in [0, 1)")
    buffer = entry_price * max(0.0, expected_slippage_bps) / 10_000.0
    if side == "long":
        raw_trigger = (
            quantity * entry_price + entry_fee - funding_pnl + quantity * buffer
        ) / (quantity * (1.0 - expected_exit_fee_rate))
        trigger = _round_to_tick(raw_trigger, tick_size, upward=True)
        expected_fill = trigger - buffer
        gross = quantity * (expected_fill - entry_price)
    else:
        raw_trigger = (
            quantity * entry_price - entry_fee + funding_pnl - quantity * buffer
        ) / (quantity * (1.0 + expected_exit_fee_rate))
        trigger = _round_to_tick(raw_trigger, tick_size, upward=False)
        expected_fill = trigger + buffer
        gross = quantity * (entry_price - expected_fill)
    expected_exit_fee = quantity * expected_fill * expected_exit_fee_rate
    expected_net = gross - entry_fee - expected_exit_fee + funding_pnl
    return {
        "raw_trigger_price": raw_trigger,
        "trigger_price": trigger,
        "expected_fill_price": expected_fill,
        "expected_exit_fee": expected_exit_fee,
        "expected_net_pnl": expected_net,
        "slippage_buffer_price": buffer,
    }


def activation_price(trade: ReplayTrade, threshold_type: str, threshold: float) -> float:
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if threshold_type == "percent":
        distance = trade.entry_price * threshold / 100.0
    elif threshold_type == "r":
        if not trade.initial_risk:
            raise ValueError("initial risk is unavailable")
        distance = threshold * trade.initial_risk / trade.quantity
    else:
        raise ValueError("threshold_type must be percent or r")
    return trade.entry_price + distance if trade.side == "long" else trade.entry_price - distance


def replay_trade(
    trade: ReplayTrade, *, threshold_type: str, threshold: float,
    expected_exit_fee_rate: float, expected_slippage_bps: float = 0.0,
    baseline_override: Optional[float] = None,
) -> dict:
    baseline = trade.baseline_pnl if baseline_override is None else baseline_override
    missing = []
    if trade.entry_fee is None:
        missing.append("entry_fee")
    if trade.funding_pnl is None:
        missing.append("funding")
    if trade.tick_size is None:
        missing.append("tick_size")
    if not trade.trajectory:
        missing.append("trajectory")
    if trade.initial_risk is None:
        missing.append("initial_risk")
    if any(name in missing for name in ("entry_fee", "tick_size", "trajectory", "initial_risk")):
        return {
            "trade_id": trade.trade_id, "symbol": trade.symbol, "side": trade.side,
            "baseline_result": baseline, "status": "insufficient_data",
            "missing_fields": missing, "counterfactual_result": None,
            "delta_vs_baseline": None, "classification": "unresolved",
        }
    funding = trade.funding_pnl if trade.funding_pnl is not None else 0.0
    be = cost_adjusted_breakeven(
        side=trade.side, entry_price=trade.entry_price, quantity=trade.quantity,
        entry_fee=trade.entry_fee, expected_exit_fee_rate=expected_exit_fee_rate,
        funding_pnl=funding, tick_size=trade.tick_size,
        expected_slippage_bps=expected_slippage_bps,
    )
    activate_at = activation_price(trade, threshold_type, threshold)
    samples = sorted(trade.trajectory, key=lambda row: row.timestamp_ms)
    favorable_prices = [row.price for row in samples]
    mfe_price = max(favorable_prices) if trade.side == "long" else min(favorable_prices)
    mfe_move = (
        mfe_price - trade.entry_price if trade.side == "long"
        else trade.entry_price - mfe_price
    )
    mfe_pct = mfe_move / trade.entry_price * 100.0
    mfe_usdt = mfe_move * trade.quantity
    mfe_r = mfe_usdt / trade.initial_risk
    activation_index = next((
        index for index, row in enumerate(samples)
        if (row.price >= activate_at if trade.side == "long" else row.price <= activate_at)
    ), None)
    be_sample = None
    if activation_index is not None:
        be_sample = next((
            row for row in samples[activation_index + 1:]
            if (row.price <= be["trigger_price"] if trade.side == "long"
                else row.price >= be["trigger_price"])
        ), None)
    triggered = be_sample is not None
    counterfactual = be["expected_net_pnl"] if triggered else baseline
    delta = counterfactual - baseline
    epsilon = 1e-9
    if not triggered or abs(delta) <= epsilon:
        classification = "unchanged"
    elif baseline < 0 and counterfactual > baseline:
        classification = "rescued loser"
    elif baseline > 0 and counterfactual < baseline:
        classification = "damaged winner"
    else:
        classification = "improved trade" if delta > 0 else "damaged trade"
    return {
        "trade_id": trade.trade_id, "symbol": trade.symbol, "side": trade.side,
        "baseline_result": baseline, "baseline_r": baseline / trade.initial_risk,
        "max_mfe_price": mfe_price, "max_mfe_pct": mfe_pct,
        "max_mfe_usdt": mfe_usdt, "max_mfe_r": mfe_r,
        "activation_threshold_type": threshold_type,
        "activation_threshold": threshold, "activation_price": activate_at,
        "activation_reached": activation_index is not None,
        "activation_timestamp_ms": (
            samples[activation_index].timestamp_ms if activation_index is not None else None
        ),
        "breakeven_trigger_price": be["trigger_price"],
        "breakeven_expected_fill_price": be["expected_fill_price"],
        "breakeven_triggered": triggered,
        "breakeven_touch_timestamp_ms": be_sample.timestamp_ms if be_sample else None,
        "counterfactual_result": counterfactual,
        "counterfactual_r": counterfactual / trade.initial_risk,
        "delta_vs_baseline": delta, "classification": classification,
        "status": "replayed", "missing_fields": missing,
        "funding_assumed_zero": trade.funding_pnl is None,
        "trajectory_samples": len(samples), "data_notes": list(trade.data_notes),
    }


def performance(rows: Iterable[dict]) -> dict:
    rows = [row for row in rows if row.get("counterfactual_result") is not None]
    pnls = [float(row["counterfactual_result"]) for row in rows]
    rs = [float(row["counterfactual_r"]) for row in rows if row.get("counterfactual_r") is not None]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    equity = drawdown = peak = 0.0
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
    sorted_pnls = sorted(pnls)
    median = (
        sorted_pnls[len(sorted_pnls) // 2] if len(sorted_pnls) % 2
        else sum(sorted_pnls[len(sorted_pnls) // 2 - 1:len(sorted_pnls) // 2 + 1]) / 2
    ) if sorted_pnls else None
    captures = [
        row["counterfactual_r"] / row["max_mfe_r"]
        for row in rows
        if row.get("max_mfe_r") and row["max_mfe_r"] > 0
        and row.get("counterfactual_r") is not None
        and row["counterfactual_r"] > 0
    ]
    return {
        "trades": len(rows), "total_pnl": sum(pnls), "total_r": sum(rs),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "expectancy_per_trade": sum(pnls) / len(pnls) if pnls else None,
        "win_rate_pct": len(wins) / len(pnls) * 100 if pnls else None,
        "average_winner": sum(wins) / len(wins) if wins else None,
        "average_loser": sum(losses) / len(losses) if losses else None,
        "median_trade": median, "max_drawdown": max_drawdown,
        "breakeven_exits": sum(bool(row.get("breakeven_triggered")) for row in rows),
        "losers_rescued": sum(row.get("classification") == "rescued loser" for row in rows),
        "full_loss_avoided": sum(
            row.get("delta_vs_baseline") or 0 for row in rows
            if row.get("classification") == "rescued loser"
        ),
        "winners_damaged": sum(row.get("classification") == "damaged winner" for row in rows),
        "winner_pnl_lost": -sum(
            row.get("delta_vs_baseline") or 0 for row in rows
            if row.get("classification") == "damaged winner"
        ),
        "unaffected_trades": sum(row.get("classification") == "unchanged" for row in rows),
        "payoff_ratio": (
            (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
            if wins and losses else None
        ),
        "average_realized_mfe_capture": (
            sum(captures) / len(captures) if captures else None
        ),
    }
