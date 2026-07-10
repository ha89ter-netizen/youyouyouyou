import logging
from typing import Dict

from sqlalchemy import inspect, text

from storage.models import TradeExpertVote

logger = logging.getLogger(__name__)


TRADE_LOG_ANALYTICS_COLUMNS: Dict[str, str] = {
    "entry_reason": "VARCHAR(2000)",
    "market_context": "VARCHAR(2000)",
    "regime": "VARCHAR(30)",
    "trend": "VARCHAR(30)",
    "decision_confidence": "NUMERIC",
    "expected_rr": "NUMERIC",
    "confirmation_count": "INTEGER",
    "confirmation_families": "VARCHAR(500)",
    "entry_snapshot": "JSONB",
    "pnl_pct": "NUMERIC",
    "mfe_pct": "NUMERIC",
    "mae_pct": "NUMERIC",
    "exit_reason": "VARCHAR(100)",
    "exit_type": "VARCHAR(30)",
    "exit_snapshot": "JSONB",
    "holding_seconds": "INTEGER",
}


def ensure_trade_log_analytics_columns(engine) -> None:
    """
    Backward-compatible schema extension for existing trade_log tables.

    SQLAlchemy create_all() does not alter existing tables. This helper only
    adds missing nullable columns, so it preserves all existing trade history.
    """
    inspector = inspect(engine)
    if not inspector.has_table("trade_log"):
        return

    existing = {column["name"] for column in inspector.get_columns("trade_log")}
    missing = [
        (name, sql_type)
        for name, sql_type in TRADE_LOG_ANALYTICS_COLUMNS.items()
        if name not in existing
    ]
    if not missing:
        return

    dialect = engine.dialect.name
    with engine.begin() as conn:
        for name, sql_type in missing:
            if dialect == "postgresql":
                statement = f"ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS {name} {sql_type}"
            elif sql_type == "JSONB":
                statement = f"ALTER TABLE trade_log ADD COLUMN {name} JSON"
            else:
                statement = f"ALTER TABLE trade_log ADD COLUMN {name} {sql_type}"
            conn.execute(text(statement))
            logger.info("DB migration: trade_log.%s added", name)


def ensure_trade_expert_votes_table(engine) -> None:
    """Creates normalized expert-vote storage if it is missing."""
    inspector = inspect(engine)
    if not inspector.has_table("trade_log"):
        logger.warning("DB migration: trade_log missing, skip trade_expert_votes creation")
        return
    TradeExpertVote.__table__.create(bind=engine, checkfirst=True)


def ensure_analytics_indexes(engine) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_trade_log_status_closed_at ON trade_log (status, closed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_symbol_closed_at ON trade_log (symbol, closed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_regime ON trade_log (regime)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_exit_type ON trade_log (exit_type)",
        "CREATE INDEX IF NOT EXISTS ix_trade_expert_votes_order_link_id ON trade_expert_votes (order_link_id)",
        "CREATE INDEX IF NOT EXISTS ix_trade_expert_votes_source ON trade_expert_votes (source)",
        "CREATE INDEX IF NOT EXISTS ix_trade_expert_votes_family ON trade_expert_votes (family)",
    ]
    inspector = inspect(engine)
    if not inspector.has_table("trade_log") or not inspector.has_table("trade_expert_votes"):
        return
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def run_safe_migrations(engine) -> None:
    ensure_trade_log_analytics_columns(engine)
    ensure_trade_expert_votes_table(engine)
    ensure_analytics_indexes(engine)
