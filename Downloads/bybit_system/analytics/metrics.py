import math
import statistics
from typing import Any, Callable, Iterable, Optional

from analytics.reliability import ReliabilityThresholds, reliability_status


EPS = 1e-12


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def max_consecutive(results: list[int], target: int) -> int:
    best = current = 0
    for result in results:
        if result == target:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def max_drawdown(pnls: Iterable[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 8)


def top_bottom_concentration(pnls: list[float], positive: bool) -> Optional[float]:
    if not pnls:
        return None
    count = max(1, math.ceil(len(pnls) * 0.10))
    if positive:
        total = sum(p for p in pnls if p > EPS)
        if total <= EPS:
            return None
        selected = sum(sorted([p for p in pnls if p > EPS], reverse=True)[:count])
    else:
        total = abs(sum(p for p in pnls if p < -EPS))
        if total <= EPS:
            return None
        selected = abs(sum(sorted([p for p in pnls if p < -EPS])[:count]))
    return selected / total


def result_metrics(
    records: Iterable[dict],
    pnl_key: str = "pnl_usdt",
    thresholds: ReliabilityThresholds = ReliabilityThresholds(),
) -> dict:
    rows = [r for r in records if safe_float(r.get(pnl_key)) is not None]
    pnls = [safe_float(r.get(pnl_key)) for r in rows]
    pct_values = [safe_float(r.get("pnl_pct")) for r in rows if safe_float(r.get("pnl_pct")) is not None]
    holding = [
        safe_float(r.get("holding_seconds"))
        for r in rows
        if safe_float(r.get("holding_seconds")) is not None and safe_float(r.get("holding_seconds")) >= 0
    ]

    wins = [p for p in pnls if p > EPS]
    losses = [p for p in pnls if p < -EPS]
    breakeven = [p for p in pnls if abs(p) <= EPS]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = len(pnls)
    net = sum(pnls)
    profit_factor = math.inf if gross_loss <= EPS and gross_profit > EPS else (gross_profit / gross_loss if gross_loss > EPS else None)
    average_win = mean(wins)
    average_loss = mean(losses)
    payoff_ratio = (
        average_win / abs(average_loss)
        if average_win is not None and average_loss is not None and abs(average_loss) > EPS
        else None
    )
    expectancy = net / total if total else None
    expectancy_pct = mean(pct_values)
    ordered = sorted(rows, key=lambda r: r.get("closed_at") or r.get("opened_at") or "")
    ordered_pnls = [safe_float(r.get(pnl_key)) or 0.0 for r in ordered]
    sequence = [1 if p > EPS else -1 if p < -EPS else 0 for p in ordered_pnls]
    dd = max_drawdown(ordered_pnls)

    return {
        "sample_size": total,
        "total_closed_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": len(wins) / total if total else None,
        "loss_rate": len(losses) / total if total else None,
        "gross_profit_usdt": gross_profit,
        "gross_loss_usdt": gross_loss,
        "net_pnl_usdt": net,
        "average_pnl_usdt": mean(pnls),
        "median_pnl_usdt": median(pnls),
        "average_win_usdt": average_win,
        "median_win_usdt": median(wins),
        "average_loss_usdt": average_loss,
        "median_loss_usdt": median(losses),
        "payoff_ratio": payoff_ratio,
        "profit_factor": profit_factor,
        "expectancy_usdt": expectancy,
        "expectancy_pct": expectancy_pct,
        "best_trade": max(rows, key=lambda r: safe_float(r.get(pnl_key)) or 0.0, default=None),
        "worst_trade": min(rows, key=lambda r: safe_float(r.get(pnl_key)) or 0.0, default=None),
        "average_holding_seconds": mean(holding),
        "median_holding_seconds": median(holding),
        "maximum_consecutive_wins": max_consecutive(sequence, 1),
        "maximum_consecutive_losses": max_consecutive(sequence, -1),
        "maximum_drawdown_usdt": dd,
        "recovery_factor": (net / abs(dd)) if dd < -EPS else (math.inf if net > EPS else None),
        "top_10pct_profit_share": top_bottom_concentration(pnls, positive=True),
        "bottom_10pct_loss_share": top_bottom_concentration(pnls, positive=False),
        "average_confidence": mean([safe_float(r.get("decision_confidence")) for r in rows if safe_float(r.get("decision_confidence")) is not None]),
        "median_confidence": median([safe_float(r.get("decision_confidence")) for r in rows if safe_float(r.get("decision_confidence")) is not None]),
        "reliability": reliability_status(total, thresholds),
    }


def group_by(
    records: Iterable[dict],
    key_fn: Callable[[dict], Any],
    thresholds: ReliabilityThresholds = ReliabilityThresholds(),
    pnl_key: str = "pnl_usdt",
) -> dict:
    grouped: dict[str, list[dict]] = {}
    for row in records:
        key = key_fn(row)
        key = "unknown" if key is None or key == "" else str(key)
        grouped.setdefault(key, []).append(row)
    return {
        key: result_metrics(rows, pnl_key=pnl_key, thresholds=thresholds)
        for key, rows in sorted(grouped.items(), key=lambda item: item[0])
    }
