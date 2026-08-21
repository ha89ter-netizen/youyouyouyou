import logging
from typing import Dict

from sqlalchemy import inspect, text

from storage.models import (
    Base, EntryIntent, NormalizedExecution, ReconciliationAnomaly, RiskState,
    RunMetadata, TelemetryOutbox, TradeClosure, TradeExchangeOrder, TradeExpertVote,
)

logger = logging.getLogger(__name__)

DATABASE_SCHEMA_VERSION = "telemetry-v5-protective-execution"
MIGRATION_VERSION = "2026-08-19-reconnect-slippage-breakeven-v1"


TELEMETRY_ATTRIBUTION_COLUMNS: Dict[str, Dict[str, str]] = {
    "position_snapshots": {"processing_run_id": "VARCHAR(100)"},
    "trade_excursions": {
        "last_processing_run_id": "VARCHAR(100)",
        "finalized_by_run_id": "VARCHAR(100)",
    },
    "trade_protection_events": {"processing_run_id": "VARCHAR(100)"},
    "trade_exit_events": {"processing_run_id": "VARCHAR(100)"},
}

TRADE_EXIT_SLIPPAGE_COLUMNS: Dict[str, str] = {
    "intended_trigger_price": "NUMERIC",
    "trigger_source": "VARCHAR(20)",
    "price_near_trigger": "NUMERIC",
    "mark_price_near_trigger": "NUMERIC",
    "last_price_near_trigger": "NUMERIC",
    "actual_fill_price": "NUMERIC",
    "slippage_absolute": "NUMERIC",
    "slippage_pct": "NUMERIC",
    "slippage_r": "NUMERIC",
    "slippage_classification": "VARCHAR(20)",
    "trigger_at": "TIMESTAMP WITH TIME ZONE",
    "fill_at": "TIMESTAMP WITH TIME ZONE",
    "protective_execution_id": "VARCHAR(100)",
}


def ensure_trade_exit_slippage_columns(engine) -> None:
    inspector = inspect(engine)
    if not inspector.has_table("trade_exit_events"):
        return
    existing = {column["name"] for column in inspector.get_columns("trade_exit_events")}
    with engine.begin() as conn:
        for name, sql_type in TRADE_EXIT_SLIPPAGE_COLUMNS.items():
            if name in existing:
                continue
            clause = " ADD COLUMN IF NOT EXISTS " if engine.dialect.name == "postgresql" else " ADD COLUMN "
            conn.execute(text(f"ALTER TABLE trade_exit_events{clause}{name} {sql_type}"))


def ensure_telemetry_attribution_columns(engine) -> None:
    """Add nullable owner/processor attribution without rewriting history."""
    inspector = inspect(engine)
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for table, columns in TELEMETRY_ATTRIBUTION_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name in existing:
                    continue
                if dialect == "postgresql":
                    statement = (
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql_type}"
                    )
                else:
                    statement = f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
                conn.execute(text(statement))
                logger.info("DB migration: %s.%s added", table, name)


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
    "exit_trigger": "JSONB",
    "holding_seconds": "INTEGER",
    "run_id": "VARCHAR(100)",
    "exchange_entry_order_id": "VARCHAR(100)",
    "entry_requested_qty": "NUMERIC",
    "entry_filled_qty": "NUMERIC",
    "exchange_exit_order_id": "VARCHAR(100)",
    "exchange_exit_order_ids": "JSONB",
    "submitted_exit_order_id": "VARCHAR(100)",
    "submitted_exit_order_link_id": "VARCHAR(100)",
    "stop_loss_price": "NUMERIC",
    "take_profit_price": "NUMERIC",
    "range_tightened_at": "TIMESTAMP WITH TIME ZONE",
    "tightened_stop_loss_price": "NUMERIC",
    "tightened_take_profit_price": "NUMERIC",
    "range_second_tightened_at": "TIMESTAMP WITH TIME ZONE",
    "second_tightened_stop_loss_price": "NUMERIC",
    "second_tightened_take_profit_price": "NUMERIC",
    "entry_fee_usdt": "NUMERIC",
    "exit_fee_usdt": "NUMERIC",
    "total_fee_usdt": "NUMERIC",
    "legacy_orphan_reason": "VARCHAR(500)",
    "legacy_classified_at": "TIMESTAMP WITH TIME ZONE",
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
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE trade_log ALTER COLUMN status TYPE VARCHAR(20)"))


def ensure_trade_expert_votes_table(engine) -> None:
    """Creates normalized expert-vote storage if it is missing."""
    inspector = inspect(engine)
    if not inspector.has_table("trade_log"):
        logger.warning("DB migration: trade_log missing, skip trade_expert_votes creation")
        return
    TradeExpertVote.__table__.create(bind=engine, checkfirst=True)


def ensure_run_metadata_table(engine) -> None:
    RunMetadata.__table__.create(bind=engine, checkfirst=True)


def ensure_trade_exchange_evidence_tables(engine) -> None:
    TradeClosure.__table__.create(bind=engine, checkfirst=True)
    TradeExchangeOrder.__table__.create(bind=engine, checkfirst=True)
    NormalizedExecution.__table__.create(bind=engine, checkfirst=True)
    ReconciliationAnomaly.__table__.create(bind=engine, checkfirst=True)


def ensure_durability_tables(engine) -> None:
    TelemetryOutbox.__table__.create(bind=engine, checkfirst=True)
    EntryIntent.__table__.create(bind=engine, checkfirst=True)
    inspector = inspect(engine)
    if inspector.has_table("entry_intents"):
        existing = {column["name"] for column in inspector.get_columns("entry_intents")}
        if "rejected_at" not in existing:
            clause = " ADD COLUMN IF NOT EXISTS " if engine.dialect.name == "postgresql" else " ADD COLUMN "
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE entry_intents{clause}rejected_at TIMESTAMP WITH TIME ZONE"
                ))


def widen_exit_reason(engine) -> None:
    """Preserve complete sanitized reasons; SQLite already treats VARCHAR as TEXT."""
    inspector = inspect(engine)
    if engine.dialect.name != "postgresql" or not inspector.has_table("trade_exit_events"):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE trade_exit_events ALTER COLUMN requested_exit_reason TYPE TEXT"
        ))


RISK_STATE_COLUMNS: Dict[str, str] = {
    "pending_entries": "JSONB",
    "blocked_symbols": "JSONB",
    "circuit_breaker_causes": "JSONB",
    "circuit_breaker_sticky": "BOOLEAN",
}


def ensure_risk_state_table(engine) -> None:
    """
    Создаёт таблицу персистентного состояния Risk Manager, если её нет, и
    дополняет её недостающими колонками, если таблица уже была создана более
    ранней версией кода. Данных не трогает.
    """
    RiskState.__table__.create(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    if not inspector.has_table("risk_state"):
        return
    existing = {column["name"] for column in inspector.get_columns("risk_state")}
    missing = [(n, t) for n, t in RISK_STATE_COLUMNS.items() if n not in existing]
    if not missing:
        return

    dialect = engine.dialect.name
    with engine.begin() as conn:
        for name, sql_type in missing:
            if dialect == "postgresql":
                statement = f"ALTER TABLE risk_state ADD COLUMN IF NOT EXISTS {name} {sql_type}"
            elif sql_type == "JSONB":
                statement = f"ALTER TABLE risk_state ADD COLUMN {name} JSON"
            else:
                statement = f"ALTER TABLE risk_state ADD COLUMN {name} {sql_type}"
            conn.execute(text(statement))
            logger.info("DB migration: risk_state.%s added", name)


def ensure_analytics_indexes(engine) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS ix_trade_log_status_closed_at ON trade_log (status, closed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_symbol_closed_at ON trade_log (symbol, closed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_regime ON trade_log (regime)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_exit_type ON trade_log (exit_type)",
        "CREATE INDEX IF NOT EXISTS ix_trade_expert_votes_order_link_id ON trade_expert_votes (order_link_id)",
        "CREATE INDEX IF NOT EXISTS ix_trade_expert_votes_source ON trade_expert_votes (source)",
        "CREATE INDEX IF NOT EXISTS ix_trade_expert_votes_family ON trade_expert_votes (family)",
        # status+symbol — горячий путь гейта повторного входа и реконсиляции:
        # на каждый цикл спрашиваем "есть ли открытые сделки по этому символу"
        "CREATE INDEX IF NOT EXISTS ix_trade_log_status_symbol ON trade_log (status, symbol)",
        "CREATE INDEX IF NOT EXISTS ix_trade_log_run_id ON trade_log (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_trade_closures_trade_log_id ON trade_closures (trade_log_id)",
        "CREATE INDEX IF NOT EXISTS ix_trade_exchange_orders_trade_log_id ON trade_exchange_orders (trade_log_id)",
        "CREATE INDEX IF NOT EXISTS ix_run_policy_epochs_run_time ON run_policy_epochs (run_id, effective_at)",
        "CREATE INDEX IF NOT EXISTS ix_account_snapshots_run_time ON account_snapshots (run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_position_snapshots_run_trade_time ON position_snapshots (run_id, trade_log_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_position_snapshots_processing_run ON position_snapshots (processing_run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_excursions_run_symbol ON trade_excursions (run_id, symbol)",
        "CREATE INDEX IF NOT EXISTS ix_trade_protection_events_run_time ON trade_protection_events (run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_protection_events_processing_run ON trade_protection_events (processing_run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_exit_events_run_time ON trade_exit_events (run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_exit_events_processing_run ON trade_exit_events (processing_run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_trade_exit_events_slippage_class ON trade_exit_events (run_id, slippage_classification)",
        "CREATE INDEX IF NOT EXISTS ix_decision_events_run_time ON decision_events (run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_rejection_events_run_time ON rejection_events (run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_operational_health_events_run_time ON operational_health_events (run_id, observed_at)",
        "CREATE INDEX IF NOT EXISTS ix_telemetry_outbox_pending ON telemetry_outbox (status, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS ix_telemetry_outbox_delivered ON telemetry_outbox (status, delivered_at)",
        "CREATE INDEX IF NOT EXISTS ix_entry_intents_run_state ON entry_intents (run_id, state)",
        "CREATE INDEX IF NOT EXISTS ix_normalized_executions_trade_time ON normalized_executions (trade_log_id, execution_time)",
        "CREATE INDEX IF NOT EXISTS ix_reconciliation_anomalies_run_time ON reconciliation_anomalies (run_id, observed_at)",
    ]
    inspector = inspect(engine)
    if not inspector.has_table("trade_log") or not inspector.has_table("trade_expert_votes"):
        return
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def run_safe_migrations(engine) -> None:
    # Railway PostgreSQL may be empty on the first deployment. create_all is
    # idempotent and only creates missing tables; column upgrades remain in the
    # explicit additive migrations below.
    Base.metadata.create_all(engine)
    ensure_trade_log_analytics_columns(engine)
    ensure_trade_expert_votes_table(engine)
    ensure_risk_state_table(engine)
    ensure_run_metadata_table(engine)
    ensure_trade_exchange_evidence_tables(engine)
    ensure_durability_tables(engine)
    widen_exit_reason(engine)
    ensure_trade_exit_slippage_columns(engine)
    ensure_telemetry_attribution_columns(engine)
    ensure_analytics_indexes(engine)
