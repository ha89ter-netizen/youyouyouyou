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

    exit_price = Column(Numeric, nullable=True)
    pnl_usdt = Column(Numeric, nullable=True)
    pnl_pct = Column(Numeric, nullable=True)
    mfe_pct = Column(Numeric, nullable=True)
    mae_pct = Column(Numeric, nullable=True)
    exit_reason = Column(String(100), nullable=True)
    exit_type = Column(String(30), nullable=True)
    exit_snapshot = Column(JSONB_COMPAT, nullable=True)
    holding_seconds = Column(Integer, nullable=True)
    status = Column(String(10), nullable=False, default="open")  # open | closed

    opened_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)


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
