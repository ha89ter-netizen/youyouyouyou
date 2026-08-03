"""
Модели таблиц. Каждая — тайм-серия с обязательной колонкой времени,
которая станет partitioning key для TimescaleDB hypertable.

Почему TimescaleDB, а не просто PostgreSQL:
- автоматическое партиционирование по времени (chunks) — быстрые запросы
  на диапазоны дат даже при миллиардах строк
- сжатие старых данных (compression policy) — экономия места
- всё это через обычный SQL/SQLAlchemy, без смены драйвера
"""

from sqlalchemy import (
    Column, BigInteger, Numeric, String, Boolean, Integer, DateTime,
    ForeignKey, JSON, PrimaryKeyConstraint, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base

from timeutils import utcnow

Base = declarative_base()
JSONB_COMPAT = JSON().with_variant(JSONB, "postgresql")


class TradeLog(Base):
    """
    Журнал всех сделок: почему открыли, почему закрыли, какой был результат.
    Обычная (не hypertable) таблица — записей немного, партиционирование
    по времени тут не нужно, зато удобно апдейтить строку при закрытии.
    """
    __tablename__ = "trade_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    action = Column(String(10), nullable=False)  # open_long / open_short
    source = Column(String(50), nullable=False)  # rule:committee | ai:openai | rule+ai
    reason = Column(String(1000), nullable=False)
    entry_reason = Column(String(2000), nullable=True)
    order_link_id = Column(String(100), nullable=False, unique=True)
    run_id = Column(String(100), nullable=True, index=True)
    exchange_entry_order_id = Column(String(100), nullable=True)
    entry_requested_qty = Column(Numeric, nullable=True)
    entry_filled_qty = Column(Numeric, nullable=True)

    market_context = Column(String(2000), nullable=True)
    regime = Column(String(30), nullable=True)
    trend = Column(String(30), nullable=True)
    decision_confidence = Column(Numeric, nullable=True)
    expected_rr = Column(Numeric, nullable=True)
    confirmation_count = Column(Integer, nullable=True)
    confirmation_families = Column(String(500), nullable=True)
    entry_snapshot = Column(JSONB_COMPAT, nullable=True)

    entry_price = Column(Numeric, nullable=False)
    size_usdt = Column(Numeric, nullable=False)
    leverage = Column(Integer, nullable=False)
    stop_loss_pct = Column(Numeric, nullable=True)
    take_profit_pct = Column(Numeric, nullable=True)
    stop_loss_price = Column(Numeric, nullable=True)
    take_profit_price = Column(Numeric, nullable=True)
    # Факт однократного time-based сужения хранится в PostgreSQL, чтобы
    # рестарт trader не применил правило повторно к той же позиции.
    range_tightened_at = Column(DateTime(timezone=True), nullable=True)
    tightened_stop_loss_price = Column(Numeric, nullable=True)
    tightened_take_profit_price = Column(Numeric, nullable=True)
    entry_fee_usdt = Column(Numeric, nullable=True)

    exit_price = Column(Numeric, nullable=True)
    pnl_usdt = Column(Numeric, nullable=True)
    pnl_pct = Column(Numeric, nullable=True)
    mfe_pct = Column(Numeric, nullable=True)
    mae_pct = Column(Numeric, nullable=True)
    # exit_reason/exit_type — КАТЕГОРИЯ закрытия (TP/SL/trailing/exit_manager/
    # manual), определяется постфактум при сверке с биржей (см. strategy/engine.py
    # _infer_exit_reason). exit_trigger — это ДРУГОЕ: снимок решения, из-за
    # которого Exit Manager инициировал закрытие (если оно было инициировано
    # им), записанный СРАЗУ в момент отправки close-ордера, а не задним числом.
    # Без него нельзя понять, почему сработал разворотный выход — только то,
    # что он сработал.
    exit_trigger = Column(JSONB_COMPAT, nullable=True)
    exit_reason = Column(String(100), nullable=True)
    exit_type = Column(String(30), nullable=True)
    exit_snapshot = Column(JSONB_COMPAT, nullable=True)
    exchange_exit_order_id = Column(String(100), nullable=True)
    exchange_exit_order_ids = Column(JSONB_COMPAT, nullable=True)
    submitted_exit_order_id = Column(String(100), nullable=True)
    submitted_exit_order_link_id = Column(String(100), nullable=True)
    exit_fee_usdt = Column(Numeric, nullable=True)
    total_fee_usdt = Column(Numeric, nullable=True)
    holding_seconds = Column(Integer, nullable=True)
    # open      — позиция считается живой
    # closed    — выход подтверждён реальным closed PnL с биржи
    # orphaned  — журнал считал сделку открытой, но ни живой позиции, ни closed PnL
    #             найти не удалось: финансовый результат неизвестен (см. circuit breaker)
    status = Column(String(20), nullable=False, default="open")
    legacy_orphan_reason = Column(String(500), nullable=True)
    legacy_classified_at = Column(DateTime(timezone=True), nullable=True)

    # default=utcnow (а не только server_default) — чтобы время входа было
    # timezone-aware уже в момент создания объекта и не зависело от того,
    # в какой зоне живёт сервер БД.
    opened_at = Column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)


class TradeClosure(Base):
    """One immutable Bybit closed-PnL record belonging to an internal trade."""
    __tablename__ = "trade_closures"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=False, index=True)
    order_link_id = Column(String(100), nullable=False, index=True)
    exchange_exit_order_id = Column(String(100), nullable=False, unique=True)
    closed_qty = Column(Numeric, nullable=False)
    avg_exit_price = Column(Numeric, nullable=False)
    closed_pnl = Column(Numeric, nullable=False)
    open_fee = Column(Numeric, nullable=True)
    close_fee = Column(Numeric, nullable=True)
    exit_reason = Column(String(100), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=False)
    raw_record = Column(JSONB_COMPAT, nullable=True)
    executions = Column(JSONB_COMPAT, nullable=True)


class TradeExchangeOrder(Base):
    """Durable exchange-order linkage and protective-order lifecycle evidence."""
    __tablename__ = "trade_exchange_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=False, index=True)
    internal_order_link_id = Column(String(100), nullable=False, index=True)
    exchange_order_id = Column(String(100), nullable=False, unique=True)
    role = Column(String(30), nullable=False)
    exchange_order_link_id = Column(String(100), nullable=True)
    parent_order_link_id = Column(String(100), nullable=True)
    order_status = Column(String(40), nullable=True)
    stop_order_type = Column(String(40), nullable=True)
    trigger_price = Column(Numeric, nullable=True)
    requested_qty = Column(Numeric, nullable=True)
    filled_qty = Column(Numeric, nullable=True)
    avg_price = Column(Numeric, nullable=True)
    exchange_created_at = Column(DateTime(timezone=True), nullable=True)
    exchange_updated_at = Column(DateTime(timezone=True), nullable=True)
    first_observed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_observed_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    raw_payload = Column(JSONB_COMPAT, nullable=True)


class RiskState(Base):
    """
    Персистентное состояние Risk Manager — одна строка (singleton, id=1).

    Существует ровно по одной причине: дневной лимит убытка, cooldown и счётчики
    сделок обязаны переживать перезапуск процесса. Пока это состояние жило только
    в памяти, упавший и перезапущенный бот забывал сегодняшние убытки, снимал
    circuit breaker и мог заново войти в тот же символ.

    Дневные значения сбрасываются ТОЛЬКО при смене UTC-дня (day_utc), никогда —
    просто из-за рестарта.
    """
    __tablename__ = "risk_state"

    id = Column(Integer, primary_key=True, autoincrement=False)  # всегда 1
    day_utc = Column(String(10), nullable=False)  # "YYYY-MM-DD"

    daily_start_balance = Column(Numeric, nullable=True)
    daily_pnl_usdt = Column(Numeric, nullable=False, default=0)
    daily_trade_count = Column(Integer, nullable=False, default=0)

    # {"ETHUSDT": 2} — сколько входов сделано по символу за текущий UTC-день
    symbol_trade_counts = Column(JSONB_COMPAT, nullable=True)
    # {"ETHUSDT": 1752480000.0} — unix-секунды последнего входа, для cooldown
    last_entry_ts_by_symbol = Column(JSONB_COMPAT, nullable=True)
    # {"ETHUSDT": 1752480000.0} — ордер принят биржей, но fill ещё не подтверждён
    pending_entries = Column(JSONB_COMPAT, nullable=True)
    # {"ETHUSDT": "причина"} — символ заблокирован до ручного разбора
    blocked_symbols = Column(JSONB_COMPAT, nullable=True)

    # Источник правды по circuit breaker: {cause_key: {"reason": str, "sticky": bool}}.
    # Breaker взведён, пока словарь непуст. Именованные причины нужны, чтобы
    # снятие одной (восстановленная orphaned-сделка) не снимало остальные.
    circuit_breaker_causes = Column(JSONB_COMPAT, nullable=True)
    # Денормализация ниже — только для чтения человеком через psql.
    # Логика читает circuit_breaker_causes, а не эти три поля.
    circuit_breaker_tripped = Column(Boolean, nullable=False, default=False)
    circuit_breaker_reason = Column(String(500), nullable=True)
    circuit_breaker_sticky = Column(Boolean, nullable=False, default=False)

    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TradeExpertVote(Base):
    """Нормализованные голоса экспертов по каждой сделке для будущей аналитики."""
    __tablename__ = "trade_expert_votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=True)
    order_link_id = Column(String(100), nullable=False)
    symbol = Column(String(20), nullable=False)
    source = Column(String(80), nullable=False)
    family = Column(String(50), nullable=True)
    action = Column(String(20), nullable=False)
    confidence = Column(Numeric, nullable=True)
    reason = Column(String(2000), nullable=True)
    weight = Column(Numeric, nullable=True)
    contributed_to_final_decision = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("order_link_id", "source", name="uq_trade_expert_vote_order_source"),
    )


class RunMetadata(Base):
    """Immutable run identity plus lightweight service liveness."""
    __tablename__ = "run_metadata"

    run_id = Column(String(100), primary_key=True)
    commit_sha = Column(String(64), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    environment_summary = Column(JSONB_COMPAT, nullable=False)
    status = Column(String(20), nullable=False, default="starting")
    collector_pid = Column(Integer, nullable=True)
    trader_pid = Column(Integer, nullable=True)
    collector_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    trader_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)


class TradingRun(Base):
    """Immutable scientific identity of one trading run (liveness lives elsewhere)."""
    __tablename__ = "trading_runs"

    run_id = Column(String(100), primary_key=True)
    strategy_version = Column(String(100), nullable=False)
    git_commit_sha = Column(String(64), nullable=False)
    git_branch = Column(String(200), nullable=True)
    dirty_worktree = Column(Boolean, nullable=False)
    deployment_environment = Column(String(40), nullable=False)
    hostname = Column(String(255), nullable=True)
    python_version = Column(String(100), nullable=False)
    dependency_fingerprint = Column(String(64), nullable=False)
    application_started_at = Column(DateTime(timezone=True), nullable=False)
    application_stopped_at = Column(DateTime(timezone=True), nullable=True)
    trading_mode = Column(String(30), nullable=False)
    testnet = Column(Boolean, nullable=False)
    enabled_symbols = Column(JSONB_COMPAT, nullable=False)
    timeframe_config = Column(JSONB_COMPAT, nullable=False)
    strategy_config = Column(JSONB_COMPAT, nullable=False)
    risk_config = Column(JSONB_COMPAT, nullable=False)
    exit_config = Column(JSONB_COMPAT, nullable=False)
    filter_config = Column(JSONB_COMPAT, nullable=False)
    runtime_config = Column(JSONB_COMPAT, nullable=False)
    environment_config = Column(JSONB_COMPAT, nullable=False)
    effective_config = Column(JSONB_COMPAT, nullable=False)
    config_hash = Column(String(64), nullable=False, index=True)
    source_sha256 = Column(String(64), nullable=False)
    database_schema_version = Column(String(40), nullable=False)
    migration_version = Column(String(100), nullable=False)
    startup_account_snapshot = Column(JSONB_COMPAT, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class RunPolicyEpoch(Base):
    __tablename__ = "run_policy_epochs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(100), ForeignKey("trading_runs.run_id"), nullable=False)
    epoch = Column(Integer, nullable=False)
    effective_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    config_hash = Column(String(64), nullable=False)
    effective_config = Column(JSONB_COMPAT, nullable=False)
    config_diff = Column(JSONB_COMPAT, nullable=False)
    reason = Column(String(500), nullable=False)
    git_commit_sha = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "epoch", name="uq_run_policy_epoch"),
    )


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(100), ForeignKey("trading_runs.run_id"), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    snapshot_bucket = Column(BigInteger, nullable=False)
    wallet_balance = Column(Numeric, nullable=True)
    equity = Column(Numeric, nullable=True)
    available_balance = Column(Numeric, nullable=True)
    total_unrealized_pnl = Column(Numeric, nullable=True)
    total_realized_pnl = Column(Numeric, nullable=True)
    margin_balance = Column(Numeric, nullable=True)
    used_margin = Column(Numeric, nullable=True)
    maintenance_margin = Column(Numeric, nullable=True)
    drawdown_from_run_high_water = Column(Numeric, nullable=True)
    high_water_equity = Column(Numeric, nullable=True)
    open_position_count = Column(Integer, nullable=True)
    gross_long_notional = Column(Numeric, nullable=True)
    gross_short_notional = Column(Numeric, nullable=True)
    net_exposure = Column(Numeric, nullable=True)
    source = Column(String(80), nullable=False)
    fetch_status = Column(String(30), nullable=False)
    is_stale = Column(Boolean, nullable=False, default=False)
    source_timestamp = Column(DateTime(timezone=True), nullable=True)
    error_type = Column(String(200), nullable=True)
    error_message = Column(String(2000), nullable=True)
    raw_payload = Column(JSONB_COMPAT, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "snapshot_bucket", name="uq_account_snapshot_run_bucket"),
    )


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(100), ForeignKey("trading_runs.run_id"), nullable=False)
    # The owner run made the trade; the processing run observed/recovered it.
    # They differ only for explicitly classified inherited positions.
    processing_run_id = Column(String(100), nullable=True, index=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=False)
    order_link_id = Column(String(100), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    snapshot_bucket = Column(BigInteger, nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    quantity = Column(Numeric, nullable=False)
    average_entry = Column(Numeric, nullable=False)
    mark_price = Column(Numeric, nullable=True)
    last_price = Column(Numeric, nullable=True)
    unrealized_pnl = Column(Numeric, nullable=True)
    unrealized_r = Column(Numeric, nullable=True)
    current_stop_loss = Column(Numeric, nullable=True)
    current_take_profit = Column(Numeric, nullable=True)
    original_stop_loss = Column(Numeric, nullable=True)
    original_take_profit = Column(Numeric, nullable=True)
    current_estimated_risk = Column(Numeric, nullable=True)
    distance_to_stop_loss = Column(Numeric, nullable=True)
    distance_to_take_profit = Column(Numeric, nullable=True)
    position_age_seconds = Column(Integer, nullable=True)
    market_data_age_seconds = Column(Numeric, nullable=True)
    protection_status = Column(String(40), nullable=False)
    protective_order_ids = Column(JSONB_COMPAT, nullable=True)
    exit_manager_state = Column(JSONB_COMPAT, nullable=True)
    market_regime = Column(String(30), nullable=True)
    volatility_regime = Column(String(30), nullable=True)
    source = Column(String(80), nullable=False)
    fetch_status = Column(String(30), nullable=False)
    is_stale = Column(Boolean, nullable=False, default=False)
    raw_position = Column(JSONB_COMPAT, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "trade_log_id", "snapshot_bucket",
            name="uq_position_snapshot_run_trade_bucket",
        ),
    )


class TradeExcursion(Base):
    __tablename__ = "trade_excursions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(100), nullable=False, index=True)
    last_processing_run_id = Column(String(100), nullable=True, index=True)
    finalized_by_run_id = Column(String(100), nullable=True, index=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=False, unique=True)
    order_link_id = Column(String(100), nullable=False, unique=True)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    entry_price = Column(Numeric, nullable=False)
    initial_risk_usdt = Column(Numeric, nullable=True)
    mfe_price_distance = Column(Numeric, nullable=True)
    mfe_pct = Column(Numeric, nullable=True)
    mfe_usdt = Column(Numeric, nullable=True)
    mfe_r = Column(Numeric, nullable=True)
    mfe_price = Column(Numeric, nullable=True)
    mfe_at = Column(DateTime(timezone=True), nullable=True)
    mfe_quantity = Column(Numeric, nullable=True)
    mfe_market_snapshot_id = Column(Integer, nullable=True)
    mae_price_distance = Column(Numeric, nullable=True)
    mae_pct = Column(Numeric, nullable=True)
    mae_usdt = Column(Numeric, nullable=True)
    mae_r = Column(Numeric, nullable=True)
    mae_price = Column(Numeric, nullable=True)
    mae_at = Column(DateTime(timezone=True), nullable=True)
    mae_quantity = Column(Numeric, nullable=True)
    mae_market_snapshot_id = Column(Integer, nullable=True)
    maximum_unrealized_profit = Column(Numeric, nullable=True)
    maximum_unrealized_loss = Column(Numeric, nullable=True)
    time_to_mfe_seconds = Column(Integer, nullable=True)
    time_to_mae_seconds = Column(Integer, nullable=True)
    tp_reached_intrabar = Column(Boolean, nullable=True)
    sl_reached_intrabar = Column(Boolean, nullable=True)
    profitable_before_closing_loss = Column(Boolean, nullable=True)
    losing_before_closing_profit = Column(Boolean, nullable=True)
    last_observed_at = Column(DateTime(timezone=True), nullable=True)
    last_market_timestamp = Column(DateTime(timezone=True), nullable=True)
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    sampling_method = Column(String(100), nullable=False)
    sampling_limitations = Column(String(1000), nullable=False)


class TradeProtectionEvent(Base):
    __tablename__ = "trade_protection_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(64), nullable=False, unique=True)
    run_id = Column(String(100), nullable=False, index=True)
    processing_run_id = Column(String(100), nullable=True, index=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=True)
    order_link_id = Column(String(100), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(String(20), nullable=False)
    event_type = Column(String(50), nullable=False)
    old_value = Column(JSONB_COMPAT, nullable=True)
    new_value = Column(JSONB_COMPAT, nullable=True)
    exchange_order_id = Column(String(100), nullable=True)
    exchange_order_link_id = Column(String(100), nullable=True)
    reason = Column(String(1000), nullable=True)
    source_module = Column(String(100), nullable=False)
    success = Column(Boolean, nullable=False)
    raw_exchange_status = Column(JSONB_COMPAT, nullable=True)
    policy_epoch = Column(Integer, nullable=False)


class TradeExitEvent(Base):
    __tablename__ = "trade_exit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(100), nullable=False, index=True)
    processing_run_id = Column(String(100), nullable=True, index=True)
    trade_log_id = Column(Integer, ForeignKey("trade_log.id"), nullable=False, unique=True)
    order_link_id = Column(String(100), nullable=False, unique=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(String(20), nullable=False)
    actual_exit_reason = Column(String(100), nullable=False)
    requested_exit_reason = Column(String(100), nullable=True)
    exchange_exit_mechanism = Column(String(100), nullable=True)
    exit_manager_signal = Column(JSONB_COMPAT, nullable=True)
    protection_trigger = Column(JSONB_COMPAT, nullable=True)
    reconciliation_status = Column(String(40), nullable=False)
    closing_order_ids = Column(JSONB_COMPAT, nullable=False)
    closing_execution_ids = Column(JSONB_COMPAT, nullable=False)
    realized_pnl = Column(Numeric, nullable=False)
    fees = Column(Numeric, nullable=True)
    funding = Column(Numeric, nullable=True)
    realized_r = Column(Numeric, nullable=True)
    mfe = Column(JSONB_COMPAT, nullable=True)
    mae = Column(JSONB_COMPAT, nullable=True)
    policy_epoch = Column(Integer, nullable=False)
    raw_payload = Column(JSONB_COMPAT, nullable=True)


class DecisionEvent(Base):
    __tablename__ = "decision_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(64), nullable=False, unique=True)
    run_id = Column(String(100), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    evaluation_id = Column(String(64), nullable=False, index=True)
    phase = Column(String(40), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(20), nullable=True)
    market_data_timestamp = Column(DateTime(timezone=True), nullable=True)
    market_data_age_seconds = Column(Numeric, nullable=True)
    signal_outputs = Column(JSONB_COMPAT, nullable=False)
    confirmation_families = Column(JSONB_COMPAT, nullable=False)
    decision_score = Column(Numeric, nullable=True)
    market_regime = Column(String(30), nullable=True)
    volatility_regime = Column(String(30), nullable=True)
    trend_state = Column(String(30), nullable=True)
    spread = Column(Numeric, nullable=True)
    funding = Column(Numeric, nullable=True)
    risk_score = Column(Numeric, nullable=True)
    proposed_entry = Column(Numeric, nullable=True)
    proposed_stop_loss = Column(Numeric, nullable=True)
    proposed_take_profit = Column(Numeric, nullable=True)
    proposed_quantity = Column(Numeric, nullable=True)
    estimated_risk = Column(Numeric, nullable=True)
    filter_results = Column(JSONB_COMPAT, nullable=False)
    final_decision = Column(String(40), nullable=False)
    decision_reason = Column(String(2000), nullable=False)
    accepted = Column(Boolean, nullable=False)
    policy_epoch = Column(Integer, nullable=False)
    commit_sha = Column(String(64), nullable=False)
    config_hash = Column(String(64), nullable=False)
    structured_payload = Column(JSONB_COMPAT, nullable=False)


class RejectionEvent(Base):
    __tablename__ = "rejection_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(64), nullable=False, unique=True)
    run_id = Column(String(100), nullable=False, index=True)
    decision_event_key = Column(String(64), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    symbol = Column(String(20), nullable=False)
    requested_side = Column(String(20), nullable=True)
    rejection_stage = Column(String(50), nullable=False)
    rejection_code = Column(String(100), nullable=False)
    rejection_reason = Column(String(2000), nullable=False)
    structured_context = Column(JSONB_COMPAT, nullable=False)
    policy_epoch = Column(Integer, nullable=False)


class OperationalHealthEvent(Base):
    __tablename__ = "operational_health_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(64), nullable=False, unique=True)
    run_id = Column(String(100), nullable=False, index=True)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    component = Column(String(100), nullable=False)
    event_type = Column(String(80), nullable=False)
    severity = Column(String(20), nullable=False)
    status = Column(String(30), nullable=False)
    symbol = Column(String(20), nullable=True)
    data_timestamp = Column(DateTime(timezone=True), nullable=True)
    data_age_seconds = Column(Numeric, nullable=True)
    error_type = Column(String(200), nullable=True)
    error_message = Column(String(2000), nullable=True)
    details = Column(JSONB_COMPAT, nullable=False)
    policy_epoch = Column(Integer, nullable=False)


class Candle(Base):
    """Свечи (klines). Уникальность: (symbol, interval, start_time)."""
    __tablename__ = "candles"

    symbol = Column(String(20), nullable=False)
    interval = Column(String(5), nullable=False)  # "1","15","60","D"...
    start_time = Column(BigInteger, nullable=False)  # unix ms, начало свечи
    open = Column(Numeric, nullable=False)
    high = Column(Numeric, nullable=False)
    low = Column(Numeric, nullable=False)
    close = Column(Numeric, nullable=False)
    volume = Column(Numeric, nullable=False)
    turnover = Column(Numeric, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "interval", "start_time"),
    )


class Trade(Base):
    """Публичная лента сделок."""
    __tablename__ = "trades"

    symbol = Column(String(20), nullable=False)
    trade_id = Column(String(64), nullable=False)
    ts = Column(BigInteger, nullable=False)  # unix ms
    side = Column(String(4), nullable=False)  # Buy/Sell
    price = Column(Numeric, nullable=False)
    size = Column(Numeric, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "trade_id", "ts"),
    )


class FundingRate(Base):
    """История funding rate — важно для анализа стоимости удержания позиции."""
    __tablename__ = "funding_rate"

    symbol = Column(String(20), nullable=False)
    funding_ts = Column(BigInteger, nullable=False)  # unix ms
    funding_rate = Column(Numeric, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "funding_ts"),
    )


class OpenInterest(Base):
    """Открытый интерес по инструменту во времени."""
    __tablename__ = "open_interest"

    symbol = Column(String(20), nullable=False)
    ts = Column(BigInteger, nullable=False)
    open_interest = Column(Numeric, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts"),
    )


class Liquidation(Base):
    """Лента ликвидаций — полезно для анализа резких движений рынка."""
    __tablename__ = "liquidations"

    symbol = Column(String(20), nullable=False)
    ts = Column(BigInteger, nullable=False)
    side = Column(String(4), nullable=False)
    price = Column(Numeric, nullable=False)
    size = Column(Numeric, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts", "side", "price"),
    )


class OrderbookSnapshot(Base):
    """
    Периодический снепшот стакана (не каждое сообщение — иначе объём
    данных огромный). top-of-book сохраняем чаще, полную глубину — реже.
    """
    __tablename__ = "orderbook_snapshots"

    symbol = Column(String(20), nullable=False)
    ts = Column(BigInteger, nullable=False)
    best_bid_price = Column(Numeric, nullable=False)
    best_bid_size = Column(Numeric, nullable=False)
    best_ask_price = Column(Numeric, nullable=False)
    best_ask_size = Column(Numeric, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("symbol", "ts"),
    )


# Таблицы, которые нужно превратить в TimescaleDB hypertable (по колонке времени)
HYPERTABLE_CONFIG = {
    "candles": "start_time",
    "trades": "ts",
    "funding_rate": "funding_ts",
    "open_interest": "ts",
    "liquidations": "ts",
    "orderbook_snapshots": "ts",
}
