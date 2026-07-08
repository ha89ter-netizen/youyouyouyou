"""
Конфигурация подключения к Bybit.

API-ключи НИКОГДА не хранятся в коде — только через переменные окружения.
Для публичных данных (свечи, стакан, funding rate) ключи вообще не нужны.
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class BybitConfig:
    # --- Учётные данные (нужны только для приватных данных: позиции, ордера, баланс) ---
    api_key: str = os.getenv("BYBIT_API_KEY", "")
    api_secret: str = os.getenv("BYBIT_API_SECRET", "")

    # testnet=True — обязательно для первых тестов, чтобы не рисковать реальными деньгами
    testnet: bool = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

    # --- Рынок ---
    # linear = USDT-перпетуалы, inverse = coin-margined
    category: str = "linear"

    # Список инструментов, за которыми следим
    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])

    # --- WebSocket ---
    ws_channel_type: str = "linear"  # соответствует category
    ping_interval: int = 20  # сек, pybit сам шлёт ping, но держим явно для контроля

    # --- База данных ---
    # Формат: postgresql+psycopg2://user:password@host:port/dbname
    db_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/bybit"
    )

    # --- ИИ ("мозг" стратегии) ---
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")

    # --- Risk Manager: жёсткие лимиты, ИИ и правила не могут их обойти ---
    # Риск на ОДНУ сделку в % от текущего баланса — размер позиции считается
    # от этого числа и дистанции до стоп-лосса, а не берётся фиксированной суммой
    risk_per_trade_pct: float = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
    # Жёсткий потолок размера позиции в USDT — не даёт risk-sizing'у улететь
    # в космос, даже если баланс огромный или стоп подозрительно узкий
    max_position_usdt: float = float(os.getenv("MAX_POSITION_USDT", "100"))
    # Максимальное плечо, которое разрешено использовать
    max_leverage: int = int(os.getenv("MAX_LEVERAGE", "3"))
    # Дневной лимит убытка в % от баланса на начало дня (не фиксированная сумма —
    # так лимит масштабируется вместе с балансом)
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    # Максимум одновременно открытых позиций
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
    # Обязательный стоп-лосс в процентах от цены входа, если стратегия не задала свой
    default_stop_loss_pct: float = float(os.getenv("DEFAULT_STOP_LOSS_PCT", "1.5"))

    # --- Volatility / Liquidity гейты: блокируют вход ДО Risk Manager ---
    # Если ATR (в % от цены) выше этого порога — рынок слишком дёрганый, не входим
    max_volatility_atr_pct: float = float(os.getenv("MAX_VOLATILITY_ATR_PCT", "3.0"))
    # Если спред (в % от цены) шире этого порога — низкая ликвидность, не входим
    max_spread_pct: float = float(os.getenv("MAX_SPREAD_PCT", "0.15"))

    # --- Trend Filter (EMA50/200) ---
    trend_filter_enabled: bool = os.getenv("TREND_FILTER_ENABLED", "true").lower() == "true"

    # --- Trailing Stop ---
    trailing_stop_enabled: bool = os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
    # Прибыль в %, при достижении которой активируется trailing stop
    trailing_activation_pct: float = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0"))
    # Дистанция trailing stop от текущей цены, в %
    trailing_distance_pct: float = float(os.getenv("TRAILING_DISTANCE_PCT", "0.8"))

    # --- Торговый цикл ---
    # Как часто (в секундах) Strategy Engine пересматривает рынок и решения
    decision_interval_sec: int = int(os.getenv("DECISION_INTERVAL_SEC", "60"))

    # --- Общее ---
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
