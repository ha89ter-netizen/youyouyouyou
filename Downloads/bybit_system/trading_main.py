"""
Точка входа торгового цикла. Отдельно от main.py (который только собирает
данные) — так вы можете гонять сбор данных постоянно, а торговлю
включать/выключать независимо.

ВАЖНО перед запуском:
1. main.py должен уже поработать какое-то время — нужны свечи в БД
   (минимум ~30-40 штук на символ для расчёта EMA, 202+ для trend filter).
2. Нужны переменные окружения:
   BYBIT_API_KEY / BYBIT_API_SECRET — ключи с testnet.bybit.com
   BYBIT_TESTNET=true — обязательно на старте

Запуск:
    python trading_main.py
"""

import logging
import os
import signal

from config.settings import BybitConfig
from logging_config import configure_app_logging
from storage.migrations import run_safe_migrations
from storage.db import Database
from storage.telemetry import TelemetryStore
from storage.durability import StorageGuard, apply_high_frequency_retention
from strategy.engine import StrategyEngine
from runtime_control import RuntimeService

configure_app_logging("trading", "trading.log")
logger = logging.getLogger("trading_main")


def main():
    cfg = BybitConfig()

    if os.getenv("BYBIT_TESTNET", "").lower() != "true" or not cfg.testnet:
        logger.error(
            "BYBIT_TESTNET должен быть явно установлен в true. "
            "Торговый цикл не стартует без явного подтверждения Testnet-режима."
        )
        return

    logger.info(
        "Запуск торгового цикла. testnet=%s, символы=%s, "
        "risk_per_trade=%.1f%%, max_position=%.2f USDT, max_leverage=%dx, "
        "max_daily_loss=%.1f%% от баланса",
        cfg.testnet, cfg.symbols, cfg.risk_per_trade_pct,
        cfg.max_position_usdt, cfg.max_leverage, cfg.max_daily_loss_pct,
    )

    db = Database(cfg)
    if not db.check_connection():
        logger.error("БД недоступна. Запустите docker compose up -d и python -m storage.init_db")
        return
    run_safe_migrations(db.engine)
    apply_high_frequency_retention(db.engine, cfg)
    storage = StorageGuard(db, cfg).status()
    if not storage["entry_allowed"]:
        logger.warning("Новые входы будут заблокированы storage guard: %s", storage["reason"])
    telemetry = TelemetryStore(db, cfg)
    runtime = RuntimeService(
        db, cfg.run_id, "trader",
        health_callback=lambda event, severity, status, error: telemetry.record_health(
            "runtime_control", event, severity, status, error=error
        ),
    )
    runtime.start()

    engine = StrategyEngine(cfg, db)
    def stop_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем.")
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
