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

from config.settings import BybitConfig
from storage.db import Database
from strategy.engine import StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("trading_main")


def main():
    cfg = BybitConfig()

    if not cfg.testnet:
        logger.error(
            "BYBIT_TESTNET не равен true. Из соображений безопасности "
            "отказываюсь запускаться на реальном счёте без явного "
            "осознанного шага — измените этот скрипт, если вы точно уверены."
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

    engine = StrategyEngine(cfg, db)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем.")


if __name__ == "__main__":
    main()
