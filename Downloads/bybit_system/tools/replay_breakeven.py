#!/usr/bin/env python3
"""Read-only counterfactual replay over durable PostgreSQL evidence."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.breakeven_replay import (  # noqa: E402
    PriceSample, ReplayTrade, performance, replay_trade,
)
from config.settings import BybitConfig  # noqa: E402
from storage.db import Database  # noqa: E402


PERCENT_THRESHOLDS = (0.25, 0.50, 0.75, 1.00)
R_THRESHOLDS = (0.25, 0.50, 0.75, 1.00)


def _ms(value) -> int:
    if value is None:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _float(value):
    return float(value) if value is not None else None


def _infer_tick(prices: list[float]) -> Tuple[Optional[float], str]:
    unique = sorted(set(Decimal(str(value)) for value in prices if value and value > 0))
    differences = [b - a for a, b in zip(unique, unique[1:]) if b > a]
    if differences:
        tick = min(differences)
        return float(tick), "inferred minimum observed public-trade increment"
    if unique:
        exponent = max(0, -unique[0].as_tuple().exponent)
        return 10 ** (-exponent), "inferred from sole observed price precision"
    return None, "unavailable"


def _load_trades(db, run_id: str, anomalous_ids: set[int], fee_rate: float) -> list[ReplayTrade]:
    session = db.get_session()
    try:
        rows = session.execute(text("""
            SELECT id, symbol, action, entry_price, entry_filled_qty,
                   stop_loss_price, pnl_usdt, entry_fee_usdt, opened_at, closed_at,
                   COALESCE(second_tightened_stop_loss_price,
                            tightened_stop_loss_price, stop_loss_price) AS final_stop
            FROM trade_log
            WHERE run_id=:run_id AND status='closed'
            ORDER BY closed_at, id
        """), {"run_id": run_id}).mappings().all()
        result = []
        for row in rows:
            executions = session.execute(text("""
                SELECT role, execution_time, execution_price, execution_quantity,
                       execution_fee
                FROM normalized_executions
                WHERE trade_log_id=:trade_id
                ORDER BY execution_time, id
            """), {"trade_id": row["id"]}).mappings().all()
            entries = [item for item in executions if item["role"] == "entry"]
            exits = [item for item in executions if item["role"] == "exit"]
            entry_qty = sum(_float(item["execution_quantity"]) or 0 for item in entries)
            entry_price = (
                sum((_float(item["execution_price"]) or 0) * (_float(item["execution_quantity"]) or 0)
                    for item in entries) / entry_qty
                if entry_qty else _float(row["entry_price"])
            )
            quantity = entry_qty or _float(row["entry_filled_qty"])
            start_ms = _ms(entries[0]["execution_time"] if entries else row["opened_at"])
            end_ms = _ms(exits[-1]["execution_time"] if exits else row["closed_at"])
            public_rows = session.execute(text("""
                SELECT ts, price FROM trades
                WHERE symbol=:symbol AND ts BETWEEN :start_ms AND :end_ms
                ORDER BY ts, trade_id
            """), {
                "symbol": row["symbol"], "start_ms": start_ms, "end_ms": end_ms,
            }).all()
            trajectory = [
                PriceSample(int(ts), float(price), "public_trade") for ts, price in public_rows
            ]
            # Position lastPrice observations supplement gaps but never replace
            # exact public trade ordering. MarkPrice is intentionally excluded
            # from a LastPrice counterfactual.
            snapshot_rows = session.execute(text("""
                SELECT observed_at, last_price FROM position_snapshots
                WHERE trade_log_id=:trade_id AND last_price IS NOT NULL
                ORDER BY observed_at
            """), {"trade_id": row["id"]}).all()
            trajectory.extend(
                PriceSample(_ms(observed_at), float(price), "position_last_price")
                for observed_at, price in snapshot_rows
                if start_ms <= _ms(observed_at) <= end_ms
            )
            trajectory.sort(key=lambda item: (item.timestamp_ms, item.source))
            tick, tick_note = _infer_tick([item.price for item in trajectory])
            entry_fee = _float(row["entry_fee_usdt"])
            final_stop = _float(row["final_stop"])
            side = "short" if row["action"] == "open_short" else "long"
            notes = [tick_note]
            if not entries:
                notes.append("weighted entry unavailable; journal entry used")
            if not exits:
                notes.append("final exit execution time unavailable; journal close used")
            if not trajectory:
                notes.append("no ordered LastPrice trajectory retained")
            # Funding was not persisted at trade scope in this run. Keep it
            # NULL; replay_trade explicitly labels the zero assumption.
            funding = None
            normalized = None
            if final_stop and quantity and entry_fee is not None:
                gross = (
                    quantity * (final_stop - entry_price) if side == "long"
                    else quantity * (entry_price - final_stop)
                )
                normalized = gross - entry_fee - quantity * final_stop * fee_rate
            result.append(ReplayTrade(
                trade_id=int(row["id"]), symbol=row["symbol"], side=side,
                entry_price=entry_price, quantity=quantity or 0.0,
                initial_stop=_float(row["stop_loss_price"]) or 0.0,
                baseline_pnl=_float(row["pnl_usdt"]) or 0.0,
                entry_fee=entry_fee, funding_pnl=funding, tick_size=tick,
                closed_at_ms=end_ms, trajectory=trajectory,
                anomalous_fill=int(row["id"]) in anomalous_ids,
                normalized_baseline_pnl=(
                    normalized if int(row["id"]) in anomalous_ids else _float(row["pnl_usdt"])
                ), data_notes=notes,
            ))
        return result
    finally:
        session.close()


def _baseline_rows(trades: list[ReplayTrade], baseline_kind: str) -> list[dict]:
    rows = []
    for trade in trades:
        baseline = (
            trade.normalized_baseline_pnl
            if baseline_kind == "normalized_anomalies" else trade.baseline_pnl
        )
        risk = trade.initial_risk
        prices = [sample.price for sample in trade.trajectory]
        if prices and risk:
            mfe_price = max(prices) if trade.side == "long" else min(prices)
            move = mfe_price - trade.entry_price if trade.side == "long" else trade.entry_price - mfe_price
            mfe_r = move * trade.quantity / risk
        else:
            mfe_price = mfe_r = None
        rows.append({
            "trade_id": trade.trade_id, "symbol": trade.symbol, "side": trade.side,
            "baseline_result": baseline, "counterfactual_result": baseline,
            "counterfactual_r": baseline / risk if risk else None,
            "max_mfe_price": mfe_price, "max_mfe_r": mfe_r,
            "breakeven_triggered": False, "classification": "unchanged",
            "delta_vs_baseline": 0.0,
        })
    return rows


def run_analysis(trades, fee_rate: float, slippage_bps: float) -> dict:
    samples = {
        "all_26": list(trades),
        "excluding_5_anomalous_fills": [t for t in trades if not t.anomalous_fill],
        "normalized_5_anomalous_fills": list(trades),
    }
    output = {
        "assumptions": {
            "expected_exit_fee_rate": fee_rate,
            "expected_normal_slippage_bps": slippage_bps,
            "funding": "unavailable for every trade; assumed zero and flagged per row",
            "trajectory": "ordered persisted public trades plus position lastPrice samples",
            "tick_size": "inferred from retained trajectory; not exchange-certified historical metadata",
            "anomalous_trade_ids": sorted(t.trade_id for t in trades if t.anomalous_fill),
        },
        "samples": {},
    }
    for sample_name, sample_trades in samples.items():
        baseline_kind = "normalized_anomalies" if sample_name.startswith("normalized") else "actual"
        sample_result = {
            "baseline": performance(_baseline_rows(sample_trades, baseline_kind)),
            "thresholds": {},
        }
        for threshold_type, thresholds in (
            ("percent", PERCENT_THRESHOLDS), ("r", R_THRESHOLDS)
        ):
            for threshold in thresholds:
                label = f"{threshold:.2f}{'%' if threshold_type == 'percent' else 'R'}"
                rows = [
                    replay_trade(
                        trade, threshold_type=threshold_type, threshold=threshold,
                        expected_exit_fee_rate=fee_rate,
                        expected_slippage_bps=slippage_bps,
                        baseline_override=(
                            trade.normalized_baseline_pnl
                            if baseline_kind == "normalized_anomalies" else None
                        ),
                    ) for trade in sample_trades
                ]
                sample_result["thresholds"][label] = {
                    "threshold_type": threshold_type, "threshold": threshold,
                    "metrics": performance(rows), "trades": rows,
                }
        output["samples"][sample_name] = sample_result
    return output


def _fmt(value):
    if value is None:
        return "N/A"
    return f"{value:.4f}" if isinstance(value, (float, int)) else str(value)


def write_markdown(result: dict, run_id: str, path: Path) -> None:
    lines = [
        "# Read-only breakeven counterfactual replay",
        "",
        f"Run: `{run_id}`  ",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Method and limitations",
        "",
        "This analysis does not import into or alter the live trading path. Activation and the subsequent return to cost-adjusted breakeven are evaluated in timestamp order; MFE is never used as an exit. The replay uses retained public LastPrice trades plus position `last_price` samples. Collector gaps can miss an activation or return, so rows without sufficient ordered evidence remain unresolved.",
        "",
    ]
    for key, value in result["assumptions"].items():
        lines.append(f"- {key}: `{value}`")
    for sample_name, sample in result["samples"].items():
        lines += ["", f"## Sample: {sample_name}", "", "### Comparison", "",
                  "| Policy | P&L | R | PF | Expectancy | Win % | Avg win | Avg loss | Max DD | BE exits | Losers saved | Winners damaged |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        baseline = sample["baseline"]
        lines.append(
            f"| baseline | {_fmt(baseline['total_pnl'])} | {_fmt(baseline['total_r'])} | {_fmt(baseline['profit_factor'])} | {_fmt(baseline['expectancy_per_trade'])} | {_fmt(baseline['win_rate_pct'])} | {_fmt(baseline['average_winner'])} | {_fmt(baseline['average_loser'])} | {_fmt(baseline['max_drawdown'])} | 0 | 0 | 0 |"
        )
        for label, item in sample["thresholds"].items():
            m = item["metrics"]
            lines.append(
                f"| {label} | {_fmt(m['total_pnl'])} | {_fmt(m['total_r'])} | {_fmt(m['profit_factor'])} | {_fmt(m['expectancy_per_trade'])} | {_fmt(m['win_rate_pct'])} | {_fmt(m['average_winner'])} | {_fmt(m['average_loser'])} | {_fmt(m['max_drawdown'])} | {m['breakeven_exits']} | {m['losers_rescued']} | {m['winners_damaged']} |"
            )
        lines += ["", "### Per-trade results", "",
                  "| Policy | Trade | Symbol | Side | Baseline | MFE % | Activated | BE exit | Counterfactual | Delta | Class |",
                  "|---|---:|---|---|---:|---:|---|---|---:|---:|---|"]
        for label, item in sample["thresholds"].items():
            for row in item["trades"]:
                lines.append(
                    f"| {label} | {row['trade_id']} | {row['symbol']} | {row['side']} | {_fmt(row['baseline_result'])} | {_fmt(row.get('max_mfe_pct'))} | {row.get('activation_reached')} | {row.get('breakeven_triggered')} | {_fmt(row.get('counterfactual_result'))} | {_fmt(row.get('delta_vs_baseline'))} | {row.get('classification')} |"
                )
    lines += [
        "", "## Descriptive ranking on all 26 trades", "",
        "| Rank | Policy | P&L | Delta vs baseline | PF | Losers saved | Winners damaged |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    actual = result["samples"]["all_26"]
    baseline_pnl = actual["baseline"]["total_pnl"]
    ranking = sorted(
        actual["thresholds"].items(),
        key=lambda item: item[1]["metrics"]["total_pnl"], reverse=True,
    )
    for index, (label, item) in enumerate(ranking, 1):
        metrics = item["metrics"]
        lines.append(
            f"| {index} | {label} | {_fmt(metrics['total_pnl'])} | "
            f"{_fmt(metrics['total_pnl'] - baseline_pnl)} | "
            f"{_fmt(metrics['profit_factor'])} | {metrics['losers_rescued']} | "
            f"{metrics['winners_damaged']} |"
        )
    lines += [
        "", "## Interpretation", "",
        "No tested policy is profitable. The apparent leaders at 0.25% and 0.25R are not stable: the adjacent 0.50%/0.50R policies deteriorate sharply, seven original winners are damaged, and Profit Factor falls despite lower absolute loss. The 0.75%–1.00% and 0.75R–1.00R neighborhood is directionally more consistent and damages fewer winners, but its improvement is small and remains negative in all three samples. Therefore no threshold is authorized for trading.",
        "",
        "Thresholds are descriptive candidates only. With 26 trades, inferred tick sizes, unavailable trade-scoped funding, and incomplete LastPrice sampling during collector degradation, this replay cannot authorize a live policy change. A candidate is considered comparatively stable only when adjacent percent and R thresholds improve expectancy/PF without a sharp increase in damaged winners.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sample", "policy", "trade_id", "symbol", "side", "baseline_result",
        "max_mfe_pct", "activation_reached", "breakeven_triggered",
        "counterfactual_result", "delta_vs_baseline", "classification", "status",
        "missing_fields", "trajectory_samples",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for sample_name, sample in result["samples"].items():
            for label, item in sample["thresholds"].items():
                for row in item["trades"]:
                    writer.writerow({
                        key: (
                            json.dumps(row.get(key), ensure_ascii=False)
                            if key == "missing_fields" else row.get(key)
                        ) for key in fields
                    } | {"sample": sample_name, "policy": label})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--anomalous-trade-ids", default="24,26,30,32,44")
    parser.add_argument("--expected-exit-fee-rate", type=float, default=0.00055)
    parser.add_argument("--expected-normal-slippage-bps", type=float, default=2.0)
    parser.add_argument("--output", default="docs/breakeven_counterfactual_report.md")
    parser.add_argument("--json-output", default="artifacts/breakeven_counterfactual_results.json")
    parser.add_argument("--csv-output", default="artifacts/breakeven_counterfactual_trades.csv")
    args = parser.parse_args()
    anomalous = {int(value) for value in args.anomalous_trade_ids.split(",") if value.strip()}
    cfg = BybitConfig()
    db = Database(cfg)
    trades = _load_trades(db, args.run_id, anomalous, args.expected_exit_fee_rate)
    if not trades:
        raise SystemExit(f"no closed trades found for run {args.run_id}")
    result = run_analysis(trades, args.expected_exit_fee_rate, args.expected_normal_slippage_bps)
    result["run_id"] = args.run_id
    json_path = Path(args.json_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    write_markdown(result, args.run_id, Path(args.output))
    write_csv(result, Path(args.csv_output))
    print(json.dumps({
        "run_id": args.run_id, "trades": len(trades), "report": args.output,
        "json": args.json_output, "csv": args.csv_output,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
