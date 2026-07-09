"""
Конфигурация подключения к Bybit.

Все секреты читаются только из переменных окружения (.env).

В этом файле НЕ должно быть API-ключей.
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class BybitConfig:
    # ==========================================================
    # API
    # ==========================================================

    api_key: str = os.getenv("BYBIT_API_KEY", "")
    api_secret: str = os.getenv("BYBIT_API_SECRET", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")

    # ==========================================================
    # РЕЖИМ РАБОТЫ
    # ==========================================================

    testnet: bool = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

    trading_enabled: bool = (
        os.getenv("TRADING_ENABLED", "true").lower() == "true"
    )

    paper_trading: bool = (
        os.getenv("PAPER_TRADING", "false").lower() == "true"
    )

    category: str = "linear"

    # ==========================================================
    # ТОРГУЕМЫЕ ИНСТРУМЕНТЫ
    # ==========================================================

    symbols: List[str] = field(default_factory=lambda: [
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "TRXUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "SUIUSDT",
        "TONUSDT",
        "HBARUSDT",
        "DOTUSDT",
        "LTCUSDT",
        "BCHUSDT",
        "UNIUSDT",
        "ATOMUSDT",
        "APTUSDT",
        "1000PEPEUSDT",
    ])

    # ==========================================================
    # БАЗА ДАННЫХ
    # ==========================================================

    db_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/bybit"
    )

    # ==========================================================
    # WEBSOCKET
    # ==========================================================

    ws_channel_type: str = "linear"
    ping_interval: int = 20

    # ==========================================================
    # ТАЙМФРЕЙМЫ
    # ==========================================================

    primary_interval: str = os.getenv("PRIMARY_INTERVAL", "15")
    confirmation_interval: str = os.getenv("CONFIRMATION_INTERVAL", "1")
    higher_interval: str = os.getenv("HIGHER_INTERVAL", "60")

    # ==========================================================
    # DECISION ENGINE
    # ==========================================================

    decision_interval_sec: int = int(
        os.getenv("DECISION_INTERVAL_SEC", "30")
    )

    min_open_confidence: float = float(
        os.getenv("MIN_OPEN_CONFIDENCE", "0.50")
    )

    min_rr: float = float(
        os.getenv("MIN_RR", "2.0")
    )

    # ==========================================================
    # RISK MANAGER
    # ==========================================================

    risk_per_trade_pct: float = float(
        os.getenv("RISK_PER_TRADE_PCT", "1.0")
    )

    max_position_usdt: float = float(
        os.getenv("MAX_POSITION_USDT", "250")
    )

    max_leverage: int = int(
        os.getenv("MAX_LEVERAGE", "3")
    )

    max_daily_loss_pct: float = float(
        os.getenv("MAX_DAILY_LOSS_PCT", "3.0")
    )

    max_open_positions: int = int(
        os.getenv("MAX_OPEN_POSITIONS", "2")
    )

    max_daily_trades: int = int(
        os.getenv("MAX_DAILY_TRADES", "50")
    )

    max_trades_per_symbol: int = int(
        os.getenv("MAX_TRADES_PER_SYMBOL", "5")
    )

    cooldown_minutes: int = int(
        os.getenv("COOLDOWN_MINUTES", "5")
    )

    # ==========================================================
    # STOP LOSS / TAKE PROFIT
    # ==========================================================

    default_stop_loss_pct: float = float(
        os.getenv("DEFAULT_STOP_LOSS_PCT", "1.5")
    )

    default_take_profit_rr: float = float(
        os.getenv("DEFAULT_TP_RR", "2.0")
    )

    # ==========================================================
    # VOLATILITY
    # ==========================================================

    max_volatility_atr_pct: float = float(
        os.getenv("MAX_VOLATILITY_ATR_PCT", "3.0")
    )

    max_spread_pct: float = float(
        os.getenv("MAX_SPREAD_PCT", "0.6")
    )

    # ==========================================================
    # TREND FILTER
    # ==========================================================

    trend_filter_enabled: bool = (
        os.getenv("TREND_FILTER_ENABLED", "true").lower() == "true"
    )

    # ==========================================================
    # TRAILING STOP
    # ==========================================================

    trailing_stop_enabled: bool = (
        os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
    )

    trailing_activation_pct: float = float(
        os.getenv("TRAILING_ACTIVATION_PCT", "1.0")
    )

    trailing_distance_pct: float = float(
        os.getenv("TRAILING_DISTANCE_PCT", "0.8")
    )

    # ==========================================================
    # ЛОГИРОВАНИЕ
    # ==========================================================

    log_level: str = os.getenv("LOG_LEVEL", "INFO")