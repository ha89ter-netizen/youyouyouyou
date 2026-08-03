#!/usr/bin/env python3
"""Read-only validation of durable research telemetry for one run."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Local operator convenience only; existing environment always wins. Values
# are never printed by this tool.
env_path = ROOT / ".env"
if env_path.exists():
    import os
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip("'\""))

from sqlalchemy import func, inspect

from config.settings import BybitConfig
from storage.db import Database
from storage.models import (
    AccountSnapshot, DecisionEvent, OperationalHealthEvent, PositionSnapshot,
    RejectionEvent, RunPolicyEpoch, TradeExcursion, TradeExitEvent, TradeLog,
    TradeProtectionEvent, TradingRun,
)

TABLES = (
    "trading_runs", "run_policy_epochs", "account_snapshots", "position_snapshots",
    "trade_excursions", "trade_protection_events", "trade_exit_events",
    "decision_events", "rejection_events", "operational_health_events",
)


def _count(session, model, run_id):
    return session.query(func.count(model.id)).filter(model.run_id == run_id).scalar() or 0


def validate(db: Database, run_id: str | None = None) -> dict:
    inspector = inspect(db.engine)
    missing_tables = [name for name in TABLES if not inspector.has_table(name)]
    if missing_tables:
        return {"status": "schema_missing", "missing_tables": missing_tables}

    session = db.get_session()
    try:
        run = (
            session.query(TradingRun).filter_by(run_id=run_id).first()
            if run_id else session.query(TradingRun)
            .order_by(TradingRun.application_started_at.desc()).first()
        )
        if run is None:
            legacy_runs = [value for (value,) in session.query(TradeLog.run_id).distinct() if value]
            return {
                "status": "no_scientific_run",
                "legacy_run_ids": legacy_runs,
                "message": "Historical rows predate telemetry-v1; absence is preserved, not fabricated.",
            }
        run_id = run.run_id
        trades = session.query(TradeLog).filter_by(run_id=run_id).all()
        open_trades = [trade for trade in trades if trade.status == "open"]
        closed_trades = [trade for trade in trades if trade.status == "closed"]
        account_count = _count(session, AccountSnapshot, run_id)
        position_count = _count(session, PositionSnapshot, run_id)
        excursion_count = _count(session, TradeExcursion, run_id)
        exit_count = _count(session, TradeExitEvent, run_id)
        protection_count = _count(session, TradeProtectionEvent, run_id)
        decision_count = _count(session, DecisionEvent, run_id)
        rejection_count = _count(session, RejectionEvent, run_id)
        health_count = _count(session, OperationalHealthEvent, run_id)
        epochs = session.query(RunPolicyEpoch).filter_by(run_id=run_id).count()
        stale_positions = session.query(PositionSnapshot).filter_by(
            run_id=run_id, is_stale=True
        ).count()
        stale_accounts = session.query(AccountSnapshot).filter_by(
            run_id=run_id, is_stale=True
        ).count()
        health_types = Counter(
            row.event_type for row in session.query(OperationalHealthEvent)
            .filter_by(run_id=run_id).all()
        )
        missing_excursions = [
            trade.order_link_id for trade in trades
            if not session.query(TradeExcursion.id).filter_by(trade_log_id=trade.id).first()
        ]
        missing_exits = [
            trade.order_link_id for trade in closed_trades
            if not session.query(TradeExitEvent.id).filter_by(trade_log_id=trade.id).first()
        ]
        unprotected = session.query(PositionSnapshot).filter(
            PositionSnapshot.run_id == run_id,
            PositionSnapshot.protection_status != "protected",
        ).count()
        duplicate_checks = {}
        for model, columns, label in (
            (AccountSnapshot, (AccountSnapshot.snapshot_bucket,), "account_snapshot_bucket"),
            (PositionSnapshot, (PositionSnapshot.trade_log_id, PositionSnapshot.snapshot_bucket),
             "position_trade_bucket"),
            (DecisionEvent, (DecisionEvent.event_key,), "decision_event_key"),
            (TradeProtectionEvent, (TradeProtectionEvent.event_key,), "protection_event_key"),
        ):
            duplicate_checks[label] = session.query(*columns, func.count(model.id)).filter(
                model.run_id == run_id
            ).group_by(*columns).having(func.count(model.id) > 1).count()

        inherited_positions_processed = session.query(PositionSnapshot).filter(
            PositionSnapshot.processing_run_id == run_id,
            PositionSnapshot.run_id != run_id,
        ).count()
        inherited_protection_processed = session.query(TradeProtectionEvent).filter(
            TradeProtectionEvent.processing_run_id == run_id,
            TradeProtectionEvent.run_id != run_id,
        ).count()
        inherited_exits_processed = session.query(TradeExitEvent).filter(
            TradeExitEvent.processing_run_id == run_id,
            TradeExitEvent.run_id != run_id,
        ).count()
        foreign_owner_mismatches = session.query(PositionSnapshot).join(
            TradeLog, PositionSnapshot.trade_log_id == TradeLog.id
        ).filter(
            PositionSnapshot.processing_run_id == run_id,
            PositionSnapshot.run_id != TradeLog.run_id,
        ).count()
        foreign_exit_mismatches = session.query(TradeExitEvent).join(
            TradeLog, TradeExitEvent.trade_log_id == TradeLog.id
        ).filter(
            TradeExitEvent.processing_run_id == run_id,
            TradeExitEvent.run_id != TradeLog.run_id,
        ).count()

        return {
            "status": "ok" if not missing_exits else "gaps_detected",
            "run_metadata": {
                "run_id": run_id, "strategy_version": run.strategy_version,
                "git_commit_sha": run.git_commit_sha, "git_branch": run.git_branch,
                "dirty_worktree": run.dirty_worktree,
                "started_at": run.application_started_at.isoformat(),
                "stopped_at": run.application_stopped_at.isoformat()
                if run.application_stopped_at else None,
                "testnet": run.testnet, "config_hash": run.config_hash,
                "schema_version": run.database_schema_version,
                "migration_version": run.migration_version,
                "policy_epochs": epochs,
            },
            "coverage": {
                "trades": len(trades), "open_trades": len(open_trades),
                "closed_trades": len(closed_trades), "account_snapshots": account_count,
                "position_snapshots": position_count, "trade_excursions": excursion_count,
                "trade_exit_events": exit_count, "protection_events": protection_count,
                "decision_events": decision_count, "rejection_events": rejection_count,
                "health_events": health_count,
            },
            "missing_data": {
                "trades_without_excursion": missing_excursions,
                "closed_trades_without_exit_event": missing_exits,
            },
            "stale_data": {
                "account_snapshots": stale_accounts, "position_snapshots": stale_positions,
                "health_event_types": dict(sorted(health_types.items())),
            },
            "protection": {"non_protected_position_snapshots": unprotected},
            "duplicates": duplicate_checks,
            "consistency": {
                "exit_event_count_lte_closed_trade_count": exit_count <= len(closed_trades),
                "excursion_count_lte_trade_count": excursion_count <= len(trades),
            },
            "cross_run": {
                "inherited_position_snapshots_processed": inherited_positions_processed,
                "inherited_protection_events_processed": inherited_protection_processed,
                "inherited_exit_events_processed": inherited_exits_processed,
                "position_owner_mismatches": foreign_owner_mismatches,
                "exit_owner_mismatches": foreign_exit_mismatches,
            },
        }
    finally:
        session.close()


def as_markdown(report: dict) -> str:
    return "# Telemetry validation\n\n```json\n" + json.dumps(
        report, indent=2, sort_keys=True, default=str
    ) + "\n```\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(Database(BybitConfig()), args.run_id)
    rendered = as_markdown(report) if args.format == "markdown" else json.dumps(
        report, indent=2, sort_keys=True, default=str
    ) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report.get("status") in ("ok", "no_scientific_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
