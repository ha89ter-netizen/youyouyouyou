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
