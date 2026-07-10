import argparse
import csv
import json
from pathlib import Path
from typing import Iterable, Optional

from config.settings import BybitConfig
from storage.db import Database
from storage.models import TradeExpertVote, TradeLog
from storage.trade_memory import safe_float, stable_json_dumps


DATASET_FIELDS = [
    "order_link_id",
    "symbol",
    "direction",
    "source",
    "entry_price",
    "exit_price",
    "size_usdt",
    "leverage",
    "pnl_usdt",
    "pnl_pct",
    "holding_seconds",
    "exit_reason",
    "exit_type",
    "opened_at",
    "closed_at",
    "regime",
    "trend",
    "decision_confidence",
    "expected_rr",
    "confirmation_count",
    "confirmation_families",
    "entry_snapshot",
    "exit_snapshot",
    "expert_votes",
]


def trade_to_dataset_row(trade: TradeLog, votes: Iterable[TradeExpertVote]) -> dict:
    vote_rows = [
        {
            "source": vote.source,
            "family": vote.family,
            "action": vote.action,
            "confidence": safe_float(vote.confidence, "vote.confidence"),
            "reason": vote.reason,
            "weight": safe_float(vote.weight, "vote.weight"),
            "contributed_to_final_decision": bool(vote.contributed_to_final_decision),
        }
        for vote in votes
    ]
    return {
        "order_link_id": trade.order_link_id,
        "symbol": trade.symbol,
        "direction": trade.action,
        "source": trade.source,
        "entry_price": safe_float(trade.entry_price, "entry_price"),
        "exit_price": safe_float(trade.exit_price, "exit_price"),
        "size_usdt": safe_float(trade.size_usdt, "size_usdt"),
        "leverage": trade.leverage,
        "pnl_usdt": safe_float(trade.pnl_usdt, "pnl_usdt"),
        "pnl_pct": safe_float(getattr(trade, "pnl_pct", None), "pnl_pct"),
        "holding_seconds": getattr(trade, "holding_seconds", None),
        "exit_reason": getattr(trade, "exit_reason", None),
        "exit_type": getattr(trade, "exit_type", None),
        "opened_at": trade.opened_at.isoformat() if trade.opened_at else None,
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
        "regime": getattr(trade, "regime", None),
        "trend": getattr(trade, "trend", None),
        "decision_confidence": safe_float(getattr(trade, "decision_confidence", None), "decision_confidence"),
        "expected_rr": safe_float(getattr(trade, "expected_rr", None), "expected_rr"),
        "confirmation_count": getattr(trade, "confirmation_count", None),
        "confirmation_families": getattr(trade, "confirmation_families", None),
        "entry_snapshot": trade.entry_snapshot,
        "exit_snapshot": getattr(trade, "exit_snapshot", None),
        "expert_votes": vote_rows,
    }


def export_trade_dataset(csv_path: Path, jsonl_path: Path, db: Optional[Database] = None) -> tuple[int, Path, Path]:
    if db is None:
        cfg = BybitConfig()
        db = Database(cfg)
    session = db.get_session()
    try:
        trades = (
            session.query(TradeLog)
            .filter(TradeLog.status == "closed")
            .order_by(TradeLog.opened_at.asc())
            .all()
        )
        vote_map = {}
        if trades:
            order_ids = [trade.order_link_id for trade in trades]
            votes = (
                session.query(TradeExpertVote)
                .filter(TradeExpertVote.order_link_id.in_(order_ids))
                .order_by(TradeExpertVote.order_link_id.asc(), TradeExpertVote.source.asc())
                .all()
            )
            for vote in votes:
                vote_map.setdefault(vote.order_link_id, []).append(vote)

        rows = [trade_to_dataset_row(trade, vote_map.get(trade.order_link_id, [])) for trade in trades]
    finally:
        session.close()

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASET_FIELDS)
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["entry_snapshot"] = stable_json_dumps(csv_row["entry_snapshot"])
            csv_row["exit_snapshot"] = stable_json_dumps(csv_row["exit_snapshot"])
            csv_row["expert_votes"] = stable_json_dumps(csv_row["expert_votes"])
            writer.writerow(csv_row)

    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")

    return len(rows), csv_path, jsonl_path


def main():
    parser = argparse.ArgumentParser(description="Export closed Bybit bot trades as ML-ready dataset.")
    parser.add_argument("--csv", default="exports/trade_dataset.csv", help="CSV output path")
    parser.add_argument("--jsonl", default="exports/trade_dataset.jsonl", help="JSON Lines output path")
    args = parser.parse_args()

    count, csv_path, jsonl_path = export_trade_dataset(Path(args.csv), Path(args.jsonl))
    print(f"Exported {count} closed trades")
    print(f"CSV : {csv_path}")
    print(f"JSONL: {jsonl_path}")


if __name__ == "__main__":
    main()
