"""Read-only PostgreSQL growth audit. Never deletes, updates or vacuums.

Answers the questions that have to be settled *before* any retention change:
what is actually large, whether deleting rows would return usable space, and
which growth is reconstructible rather than research-critical.

The distinction that matters most here is heap vs index. Autovacuum reclaims
heap space for reuse but never rebuilds a bloated btree, so a delete-heavy
retention loop can leave an index several times larger than its table. When
that is what dominates, shortening retention returns nothing and only adds
more bloat -- the fix is REINDEX, which is an owner-approved maintenance
action, not something a trading process should do to itself.

    python -m tools.storage_audit            # human-readable breakdown
    python -m tools.storage_audit --json     # machine-readable

Categories reported per table:

    A CRITICAL   durable trading evidence; never auto-deleted
    B AUDIT      useful research/audit history; never auto-deleted
    C REBUILD    reconstructible raw market data under bounded retention
    D EPHEMERAL  operational bookkeeping, safe to bound aggressively
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from config.settings import BybitConfig
from storage.db import Database

# Category per table. Anything unlisted is reported as UNKNOWN rather than
# silently assumed safe to delete.
CATEGORIES = {
    "trade_log": ("A", "closed trades, realized PnL, exit reasons"),
    "trade_exit_events": ("A", "exit evidence, slippage classification"),
    "trade_protection_events": ("A", "protection lifecycle"),
    "trade_exchange_orders": ("A", "exchange order identity"),
    "normalized_executions": ("A", "fills"),
    "entry_intents": ("A", "pre-exchange entry ownership"),
    "risk_state": ("A", "circuit breaker and daily limits"),
    "trade_excursions": ("A", "MFE/MAE reconstruction"),
    "trade_expert_votes": ("B", "per-expert decision attribution"),
    "decision_events": ("B", "every evaluated decision"),
    "rejection_events": ("B", "why entries were rejected"),
    "position_snapshots": ("B", "position trajectory"),
    "account_snapshots": ("B", "balance/equity trajectory"),
    "operational_health_events": ("B", "durable health audit trail"),
    "funding_rate_minute_rollups": ("B", "durable funding aggregates"),
    "open_interest_minute_rollups": ("B", "durable OI aggregates"),
    "candles": ("C", "re-fetchable from Bybit REST"),
    "trades": ("C", "raw public trade flow, bounded retention"),
    "orderbook_snapshots": ("C", "raw order book, bounded retention"),
    "funding_rate": ("C", "raw ticker samples, rolled up before deletion"),
    "open_interest": ("C", "raw ticker samples, rolled up before deletion"),
    "liquidations": ("C", "raw liquidations, bounded retention"),
    "telemetry_outbox": ("D", "delivery queue; delivered rows are not the audit record"),
    "operator_monitor_state": ("D", "alert cursors"),
    "operator_control_commands": ("D", "owner request log"),
    "high_frequency_retention_state": ("D", "retention bookkeeping"),
    "run_metadata": ("D", "runtime liveness"),
    "trading_runs": ("B", "immutable run identity"),
    "run_policy_epochs": ("B", "policy epochs"),
}

RELATION_SQL = """
SELECT c.relname,
       pg_total_relation_size(c.oid) AS total,
       pg_relation_size(c.oid) AS heap,
       COALESCE(pg_total_relation_size(c.reltoastrelid), 0) AS toast,
       pg_indexes_size(c.oid) AS indexes,
       COALESCE(st.n_live_tup, 0) AS live_rows,
       COALESCE(st.n_dead_tup, 0) AS dead_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables st ON st.relid = c.oid
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY total DESC
"""

INDEX_SQL = """
SELECT i.indexrelname, i.relname, pg_relation_size(i.indexrelid) AS bytes,
       i.idx_scan, pg_relation_size(c.oid) AS heap
FROM pg_stat_user_indexes i
JOIN pg_class c ON c.oid = i.relid
ORDER BY bytes DESC
"""

# An index larger than this multiple of its table is reported as bloated. A
# healthy btree is a fraction of its heap; several times larger means the pages
# were emptied by deletes and never reclaimed.
BLOAT_RATIO = 1.5
BLOAT_MIN_BYTES = 50_000_000


def _mb(value) -> str:
    return f"{(value or 0) / 1e6:8.1f}M"


def collect(db, cfg) -> dict:
    session = db.get_session()
    try:
        if db.engine.dialect.name != "postgresql":
            return {"error": "storage audit requires PostgreSQL"}
        size = int(session.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar_one())
        quota = int(getattr(cfg, "storage_max_database_bytes", 0) or 0)
        relations = [
            {
                "table": row[0], "total": int(row[1]), "heap": int(row[2]),
                "toast": int(row[3]), "indexes": int(row[4]),
                "live_rows": int(row[5]), "dead_rows": int(row[6]),
                "category": CATEGORIES.get(row[0], ("?", "unclassified"))[0],
                "note": CATEGORIES.get(row[0], ("?", "unclassified"))[1],
            }
            for row in session.execute(text(RELATION_SQL)).fetchall()
        ]
        indexes = [
            {
                "index": row[0], "table": row[1], "bytes": int(row[2]),
                "scans": int(row[3] or 0), "heap": int(row[4]),
                "ratio": (int(row[2]) / int(row[4])) if int(row[4]) else None,
            }
            for row in session.execute(text(INDEX_SQL)).fetchall()
        ]
        bloated = [
            item for item in indexes
            if item["bytes"] >= BLOAT_MIN_BYTES
            and item["ratio"] is not None and item["ratio"] >= BLOAT_RATIO
        ]
        by_category: dict[str, int] = {}
        for item in relations:
            by_category[item["category"]] = (
                by_category.get(item["category"], 0) + item["total"]
            )
        return {
            "database_bytes": size, "quota_bytes": quota or None,
            "usage_ratio": (size / quota) if quota else None,
            "entry_block_ratio": float(getattr(cfg, "storage_entry_block_ratio", 0.85)),
            "relations": relations, "indexes": indexes,
            "bloated_indexes": bloated,
            "reclaimable_by_reindex": sum(
                int(item["bytes"] - item["heap"] * BLOAT_RATIO) for item in bloated
            ),
            "bytes_by_category": by_category,
        }
    finally:
        session.close()


def render(report: dict) -> str:
    if "error" in report:
        return report["error"]
    lines = [
        f"database: {report['database_bytes'] / 1e9:.3f} GB"
        + (
            f" / {report['quota_bytes'] / 1e9:.1f} GB quota "
            f"({report['usage_ratio']:.1%}, entries block at "
            f"{report['entry_block_ratio']:.0%})"
            if report["quota_bytes"] else "  (STORAGE_MAX_DATABASE_BYTES not set)"
        ),
        "",
        f"{'table':34}{'cat':>4} {'total':>9} {'heap':>9} {'toast':>9} "
        f"{'index':>9} {'rows':>12}",
    ]
    for item in report["relations"][:20]:
        lines.append(
            f"{item['table']:34}{item['category']:>4} {_mb(item['total'])} "
            f"{_mb(item['heap'])} {_mb(item['toast'])} {_mb(item['indexes'])} "
            f"{item['live_rows']:>12,}"
        )
    lines += ["", "bytes by category:"]
    for key in sorted(report["bytes_by_category"]):
        lines.append(f"  {key}  {report['bytes_by_category'][key] / 1e6:9.1f} MB")

    lines += ["", "index bloat (index far larger than its table):"]
    if not report["bloated_indexes"]:
        lines.append("  none")
    else:
        for item in report["bloated_indexes"]:
            lines.append(
                f"  {item['index']:44} {_mb(item['bytes'])} on a "
                f"{_mb(item['heap'])} table ({item['ratio']:.2f}x)"
            )
        lines += [
            "",
            f"  ~{report['reclaimable_by_reindex'] / 1e6:.0f} MB is reclaimable by "
            "REINDEX, not by deleting more rows.",
            "  Deleting rows from these tables will NOT shrink the database.",
            "  See README, 'PostgreSQL maintenance', for the owner-approved procedure.",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()
    cfg = BybitConfig()
    report = collect(Database(cfg), cfg)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    sys.exit(main())
