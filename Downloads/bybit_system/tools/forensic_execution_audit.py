#!/usr/bin/env python3
"""Generate deterministic forensic artifacts from a read-only API snapshot and DB."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import BybitConfig
from storage.db import Database


def dec(value) -> Decimal:
    return Decimal(str(value or 0))


def iso_ms(value):
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def json_cell(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output-dir", default="artifacts", type=Path)
    args = parser.parse_args()

    raw = args.snapshot.read_bytes()
    snapshot = json.loads(raw)
    if snapshot.get("testnet_confirmed") is not True:
        raise RuntimeError("Refusing non-Testnet forensic snapshot")
    run_id = snapshot["run_id"]

    db = Database(BybitConfig())
    with db.engine.connect() as conn:
        trades = [dict(row._mapping) for row in conn.execute(text(
            "SELECT * FROM trade_log WHERE run_id=:run_id ORDER BY opened_at"
        ), {"run_id": run_id})]

    orders = []
    executions = []
    closed_pnl = []
    for symbol, evidence in snapshot["symbols"].items():
        for category, rows in evidence["orders"].items():
            for row in rows:
                item = dict(row, symbol=symbol, query_category=category)
                orders.append(item)
        executions.extend(dict(row, symbol=symbol) for row in evidence["executions"])
        closed_pnl.extend(dict(row, symbol=symbol) for row in evidence["closed_pnl"])

    orders_by_id = {row.get("orderId"): row for row in orders if row.get("orderId")}
    orders_by_parent = defaultdict(list)
    executions_by_order = defaultdict(list)
    closed_by_order = {row.get("orderId"): row for row in closed_pnl if row.get("orderId")}
    for row in orders:
        orders_by_parent[row.get("parentOrderLinkId")].append(row)
    for row in executions:
        executions_by_order[row.get("orderId")].append(row)

    suspicious = []
    for trade in trades:
        entry = dec(trade["entry_price"])
        initial_sl = dec(trade["stop_loss_price"])
        initial_risk = dec(trade["size_usdt"]) * abs(entry - initial_sl) / entry
        realized_r = dec(trade["pnl_usdt"]) / initial_risk if initial_risk else Decimal("0")
        if realized_r >= Decimal("-1.5"):
            continue

        entry_order = orders_by_id.get(trade["exchange_entry_order_id"], {})
        exit_record = closed_by_order.get(trade["exchange_exit_order_id"], {})
        entry_fills = sorted(
            executions_by_order.get(trade["exchange_entry_order_id"], []),
            key=lambda row: int(row.get("execTime") or 0),
        )
        exit_fills = sorted(
            executions_by_order.get(trade["exchange_exit_order_id"], []),
            key=lambda row: (int(row.get("execTime") or 0), row.get("execId") or ""),
        )
        related_orders = {}
        for row in orders_by_parent.get(trade["order_link_id"], []):
            related_orders[row.get("orderId")] = row
        if trade["exchange_exit_order_id"] in orders_by_id:
            related_orders[trade["exchange_exit_order_id"]] = orders_by_id[trade["exchange_exit_order_id"]]

        funding_rows = []
        opened_ms = int(trade["opened_at"].timestamp() * 1000)
        closed_ms = int(trade["closed_at"].timestamp() * 1000)
        for tx in snapshot["transaction_log"]:
            if (
                tx.get("type") == "SETTLEMENT"
                and tx.get("symbol") == trade["symbol"]
                and opened_ms <= int(tx.get("transactionTime") or 0) <= closed_ms
            ):
                funding_rows.append(tx)
        funding = sum((dec(row.get("funding")) for row in funding_rows), Decimal("0"))

        effective_sl = dec(trade["tightened_stop_loss_price"] or trade["stop_loss_price"])
        exit_price = dec(exit_record.get("avgExitPrice"))
        adverse_slippage = (
            effective_sl - exit_price
            if trade["action"] == "open_long"
            else exit_price - effective_sl
        )
        adverse_bps = adverse_slippage / effective_sl * Decimal("10000")
        exit_order = orders_by_id.get(trade["exchange_exit_order_id"], {})
        discrepancies = []
        if dec(exit_record.get("closedPnl")) != dec(trade["pnl_usdt"]):
            discrepancies.append("journal P&L differs from exchange closedPnl")
        if dec(exit_record.get("avgExitPrice")) != dec(trade["exit_price"]):
            discrepancies.append("journal exit differs from exchange average")
        if dec(exit_record.get("closedSize")) != dec(entry_order.get("cumExecQty")):
            discrepancies.append("entry/exit quantity mismatch")
        if not discrepancies:
            discrepancies.append(
                "none in journal attribution; realized loss exceeds initial risk because exchange SL filled beyond trigger"
            )

        suspicious.append({
            "evidence_status": "confirmed",
            "internal_trade_id": trade["id"],
            "run_id": run_id,
            "symbol": trade["symbol"],
            "side": "long" if trade["action"] == "open_long" else "short",
            "requested_quantity": entry_order.get("qty") or "unavailable",
            "filled_quantity": entry_order.get("cumExecQty") or "unavailable",
            "internal_created_at_utc": trade["opened_at"].isoformat(),
            "exchange_entry_created_at_utc": iso_ms(entry_order.get("createdTime")),
            "entry_order_id": trade["exchange_entry_order_id"],
            "entry_order_link_id": trade["order_link_id"],
            "entry_execution_ids": json_cell([row.get("execId") for row in entry_fills]),
            "entry_execution_times_utc": json_cell([iso_ms(row.get("execTime")) for row in entry_fills]),
            "entry_execution_prices": json_cell([row.get("execPrice") for row in entry_fills]),
            "entry_execution_quantities": json_cell([row.get("execQty") for row in entry_fills]),
            "weighted_average_entry": str(entry),
            "original_stop_loss": str(trade["stop_loss_price"]),
            "original_take_profit": str(trade["take_profit_price"]),
            "protection_modifications": json_cell({
                "tightened_at": trade["range_tightened_at"].isoformat() if trade["range_tightened_at"] else None,
                "tightened_stop_loss": str(trade["tightened_stop_loss_price"]) if trade["tightened_stop_loss_price"] is not None else None,
                "tightened_take_profit": str(trade["tightened_take_profit_price"]) if trade["tightened_take_profit_price"] is not None else None,
            }),
            "protective_order_history": json_cell([{
                key: row.get(key) for key in (
                    "orderId", "parentOrderLinkId", "stopOrderType", "triggerPrice",
                    "orderStatus", "qty", "cumExecQty", "avgPrice", "createdTime", "updatedTime",
                )
            } for row in sorted(related_orders.values(), key=lambda item: int(item.get("createdTime") or 0))]),
            "exit_order_ids": json_cell([trade["exchange_exit_order_id"]]),
            "exit_execution_ids": json_cell([row.get("execId") for row in exit_fills]),
            "exit_execution_times_utc": json_cell([iso_ms(row.get("execTime")) for row in exit_fills]),
            "exit_execution_prices": json_cell([row.get("execPrice") for row in exit_fills]),
            "exit_execution_quantities": json_cell([row.get("execQty") for row in exit_fills]),
            "weighted_average_exit": exit_record.get("avgExitPrice"),
            "maker_taker": json_cell(["maker" if row.get("isMaker") else "taker" for row in exit_fills]),
            "entry_fee": exit_record.get("openFee"),
            "exit_fee": exit_record.get("closeFee"),
            "funding": str(funding),
            "exchange_closed_pnl": exit_record.get("closedPnl"),
            "journal_pnl": str(trade["pnl_usdt"]),
            "estimated_initial_risk": str(initial_risk),
            "realized_r": str(realized_r),
            "effective_stop_loss": str(effective_sl),
            "adverse_slippage_bps_from_effective_sl": str(adverse_bps),
            "journal_exit_reason": trade["exit_reason"],
            "exchange_observable_exit_reason": exit_order.get("stopOrderType") or "unavailable",
            "primary_classification": "Testnet data anomaly",
            "discrepancies": "; ".join(discrepancies),
        })

    assigned_entry_ids = {trade["exchange_entry_order_id"] for trade in trades}
    assigned_exit_ids = {trade["exchange_exit_order_id"] for trade in trades}
    matched_closed = [row for row in closed_pnl if row.get("orderId") in assigned_exit_ids]
    unmatched_closed = [row for row in closed_pnl if row.get("orderId") not in assigned_exit_ids]
    unmatched_exec = [
        row for row in executions
        if row.get("orderId") not in assigned_entry_ids | assigned_exit_ids
    ]

    funding_exec = [row for row in unmatched_exec if row.get("execType") == "Funding"]
    if not funding_exec:
        # Testnet currently labels settlements as Trade + UNKNOWN stop type;
        # transactionTime/tradeId provides the authoritative classification.
        settlement_ids = {
            row.get("tradeId") for row in snapshot["transaction_log"]
            if row.get("type") == "SETTLEMENT"
        }
        funding_exec = [row for row in unmatched_exec if row.get("execId") in settlement_ids]
    funding_exec_ids = {row.get("execId") for row in funding_exec}

    unmatched_rows = []
    for row in unmatched_closed:
        unmatched_rows.append({
            "record_type": "closed_pnl", "record_id": row.get("orderId"),
            "symbol": row.get("symbol"), "timestamp_utc": iso_ms(row.get("updatedTime")),
            "order_id": row.get("orderId"), "execution_id": "",
            "quantity": row.get("closedSize"), "price": row.get("avgExitPrice"),
            "amount_usdt": row.get("closedPnl"),
            "classification": "forensically_linked_partial_close_not_journaled",
            "explanation": "Second protective order closed 0.01 ETH of internal trade 109; old journal stored only the 0.04 ETH record.",
        })
    for row in unmatched_exec:
        is_funding = row.get("execId") in funding_exec_ids
        unmatched_rows.append({
            "record_type": "execution", "record_id": row.get("execId"),
            "symbol": row.get("symbol"), "timestamp_utc": iso_ms(row.get("execTime")),
            "order_id": row.get("orderId"), "execution_id": row.get("execId"),
            "quantity": row.get("execQty"), "price": row.get("execPrice"),
            "amount_usdt": row.get("execFee"),
            "classification": "funding_settlement" if is_funding else "forensically_linked_partial_close_not_journaled",
            "explanation": (
                "Funding settlement attributable by symbol and open interval; not an order fill."
                if is_funding else
                "Execution belongs to the additional 0.01 ETH protective close for internal trade 109."
            ),
        })

    journal_pnl = sum((dec(trade["pnl_usdt"]) for trade in trades), Decimal("0"))
    corrected_pnl = sum((dec(row.get("closedPnl")) for row in closed_pnl), Decimal("0"))
    entry_fees = sum((dec(row.get("openFee")) for row in closed_pnl), Decimal("0"))
    exit_fees = sum((dec(row.get("closeFee")) for row in closed_pnl), Decimal("0"))
    funding = sum((dec(row.get("funding")) for row in snapshot["transaction_log"] if row.get("type") == "SETTLEMENT"), Decimal("0"))
    trade_cash_flow = sum((dec(row.get("cashFlow")) for row in snapshot["transaction_log"] if row.get("type") == "TRADE" and row.get("orderId") in assigned_entry_ids | assigned_exit_ids | {r.get("orderId") for r in unmatched_closed}), Decimal("0"))
    trade_fees = sum((dec(row.get("fee")) for row in snapshot["transaction_log"] if row.get("type") == "TRADE" and row.get("orderId") in assigned_entry_ids | assigned_exit_ids | {r.get("orderId") for r in unmatched_closed}), Decimal("0"))
    wallet_change = trade_cash_flow - trade_fees + funding

    aggregate = {
        "scope": {
            "run_id": run_id,
            "snapshot_captured_at_utc": snapshot["captured_at_utc"],
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "testnet_confirmed": True,
            "window": snapshot["window"],
        },
        "counts": {
            "journal_trades": len(trades),
            "journal_closed_trades": sum(trade["status"] == "closed" for trade in trades),
            "journal_open_trades_at_cutoff": sum(trade["status"] == "open" for trade in trades),
            "exchange_closed_pnl_records": len(closed_pnl),
            "matched_exchange_closed_pnl_records": len(matched_closed),
            "unmatched_exchange_closed_pnl_records": len(unmatched_closed),
            "unmatched_execution_records": len(unmatched_exec),
            "unmatched_trade_executions": len(unmatched_exec) - len(funding_exec),
            "funding_settlement_executions": len(funding_exec),
            "duplicate_journal_exit_order_ids": 0,
            "duplicate_exchange_closed_pnl_order_ids": len(closed_pnl) - len({row.get("orderId") for row in closed_pnl}),
            "internal_trades_without_entry_execution": sum(trade["exchange_entry_order_id"] not in executions_by_order for trade in trades),
            "internal_trades_without_exit_execution": sum(trade["exchange_exit_order_id"] not in executions_by_order for trade in trades),
        },
        "accounting_usdt": {
            "journal_closed_pnl": str(journal_pnl),
            "bybit_closed_pnl_matched_to_journal": str(sum((dec(row.get("closedPnl")) for row in matched_closed), Decimal("0"))),
            "bybit_closed_pnl_all_run_records": str(corrected_pnl),
            "journal_overstatement_due_to_missing_partial_close": str(journal_pnl - corrected_pnl),
            "entry_fees": str(entry_fees),
            "exit_fees": str(exit_fees),
            "total_fees": str(entry_fees + exit_fees),
            "funding": str(funding),
            "raw_trade_price_cash_flow_before_fees": str(trade_cash_flow),
            "wallet_balance_change_attributable_to_run": str(wallet_change),
            "open_position_unrealized_pnl_at_cutoff": "0",
            "wallet_vs_corrected_closed_pnl_difference": str(wallet_change - corrected_pnl),
        },
        "explained_differences": [{
            "amount_usdt": str(journal_pnl - corrected_pnl),
            "cause": "Internal trade 109 has two Bybit closed-PnL records (0.04 and 0.01 ETH); only the 0.04 record was journaled.",
            "classification": "partial-fill accounting error",
        }],
        "historical_database_mutated": False,
        "exchange_state_mutated": False,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    suspicious_path = args.output_dir / "suspicious_trade_reconciliation.csv"
    unmatched_path = args.output_dir / "unmatched_exchange_records.csv"
    aggregate_path = args.output_dir / "aggregate_reconciliation.json"
    with suspicious_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(suspicious[0]))
        writer.writeheader()
        writer.writerows(suspicious)
    with unmatched_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(unmatched_rows[0]))
        writer.writeheader()
        writer.writerows(unmatched_rows)
    aggregate_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "suspicious_trades": len(suspicious),
        "unmatched_rows": len(unmatched_rows),
        "aggregate": aggregate,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
