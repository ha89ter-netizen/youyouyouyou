"""
Пример запуска: REST подтягивает историю и складывает в БД,
WebSocket пишет живой поток через MarketDataStore (с буферизацией).

Запуск:
    python main.py

Перед первым запуском один раз: python -m storage.init_db
"""

import logging
import time

from config.settings import BybitConfig
from data.rest_client import BybitRestClient
from logging_config import configure_app_logging
from data.ws_client import BybitPublicStream
from storage.db import Database
from storage.repository import MarketDataStore

configure_app_logging("main", "main.log")
logger = logging.getLogger("main")


def main():
    cfg = BybitConfig()
    logger.info("Символы для отслеживания: %s", cfg.symbols)

    # --- БД ---
    db = Database(cfg)
    if not db.check_connection():
        logger.error(
            "БД недоступна. Запустите TimescaleDB (см. README.md, docker-compose) "
            "и выполните: python -m storage.init_db"
        )
        return
    store = MarketDataStore(db)

    # --- 1. REST: исторические данные сразу в БД ---
    # limit=210 (не 200!) -- trend filter (EMA200) требует минимум 202 свечи,
    # без запаса он не заработал бы первые ~30 минут после старта, пока
    # недостающие свечи не накопятся через WebSocket.
    rest = BybitRestClient(cfg)
    for symbol in cfg.symbols:
        klines = rest.get_klines(symbol, interval=cfg.primary_interval, limit=210)
        store.save_historical_klines(symbol, cfg.primary_interval, klines)

        funding_history = rest.get_funding_rate_history(symbol, limit=200)
        store.save_funding_history(symbol, funding_history)

        oi_history = rest.get_open_interest(symbol, interval_time="15min", limit=200)
        store.save_open_interest_history(symbol, oi_history)

        tickers = rest.get_tickers(symbol)
        if tickers:
            t = tickers[0]
            logger.info(
                "%s: last=%s funding_rate=%s open_interest=%s",
                symbol, t.get("lastPrice"), t.get("fundingRate"), t.get("openInterest"),
            )

    # --- 2. WebSocket: живой поток пишем через store, буферизация внутри ---
    stream = BybitPublicStream(cfg)
    for symbol in cfg.symbols:
        stream.subscribe_orderbook(symbol, depth=50, on_message=store.on_orderbook_ws)
        stream.subscribe_trades(symbol, on_message=store.on_trade_ws)
        stream.subscribe_kline(symbol, interval=cfg.primary_interval, on_message=store.on_kline_ws)
        stream.subscribe_liquidations(symbol, on_message=store.on_liquidation_ws)
        stream.subscribe_ticker(symbol, on_message=store.on_ticker_ws)

    logger.info("Поток запущен, данные пишутся в БД. Ctrl+C для остановки.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Останавливаюсь, сбрасываю буферы в БД...")
        store.stop()
        logger.info("Готово.")


if __name__ == "__main__":
    main()
