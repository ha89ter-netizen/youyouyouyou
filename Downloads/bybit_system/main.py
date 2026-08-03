"""
Пример запуска: REST подтягивает историю и складывает в БД,
WebSocket пишет живой поток через MarketDataStore (с буферизацией).

Запуск:
    python main.py

Перед первым запуском один раз: python -m storage.init_db
"""

import logging
import os
import signal
import socket
import time

from config.settings import BybitConfig
from data.rest_client import BybitRestClient
from logging_config import configure_app_logging
from data.ws_client import BybitPublicStream
from storage.db import Database
from storage.repository import MarketDataStore
from storage.migrations import run_safe_migrations
from storage.telemetry import TelemetryStore
from runtime_control import RuntimeService

configure_app_logging("main", "main.log")
logger = logging.getLogger("main")


class _PybitTelemetryHandler(logging.Handler):
    """Mirror pybit transport lifecycle messages into durable health telemetry."""

    _EVENTS = (
        ("encountered error", "websocket_disconnect", "error", "disconnected"),
        ("attempting connection", "websocket_reconnect_attempt", "warning", "reconnecting"),
        ("connected", "websocket_connected", "info", "recovered"),
    )

    def __init__(self, telemetry):
        super().__init__(level=logging.INFO)
        self.telemetry = telemetry

    def emit(self, record):
        try:
            message = record.getMessage()
            lowered = message.lower()
            for marker, event_type, severity, status in self._EVENTS:
                if marker in lowered:
                    self.telemetry.record_health(
                        "market_collector", event_type, severity, status,
                        details={"library": "pybit", "message": message[:2000]},
                    )
                    break
        except Exception:
            # Telemetry failure must never disrupt market-data collection.
            self.handleError(record)


def _is_dns_failure(exc: Exception) -> bool:
    current = exc
    for _ in range(6):
        if isinstance(current, socket.gaierror):
            return True
        text = str(current).lower()
        if "name resolution" in text or "getaddrinfo" in text:
            return True
        current = current.__cause__ or current.__context__
        if current is None:
            break
    return False


def _start_public_stream(cfg, store, telemetry=None):
    def callback_failure(channel, exc):
        if telemetry is not None:
            telemetry.record_health(
                "market_collector", "websocket_callback_failure", "error", "failed",
                error=exc, details={"channel": channel},
            )

    stream = BybitPublicStream(cfg, on_health_event=callback_failure)
    try:
        for symbol in cfg.symbols:
            stream.subscribe_orderbook(symbol, depth=50, on_message=store.on_orderbook_ws)
            stream.subscribe_trades(symbol, on_message=store.on_trade_ws)
            stream.subscribe_kline(
                symbol,
                interval=cfg.primary_interval,
                on_message=store.on_kline_ws,
            )
            stream.subscribe_liquidations(
                symbol,
                on_message=store.on_liquidation_ws,
            )
            stream.subscribe_ticker(symbol, on_message=store.on_ticker_ws)
    except Exception:
        stream.close()
        raise
    return stream


def main():
    cfg = BybitConfig()
    if os.getenv("BYBIT_TESTNET", "").lower() != "true" or not cfg.testnet:
        logger.error("Market collector refuses to start outside explicit Testnet mode")
        return
    logger.info("Символы для отслеживания: %s", cfg.symbols)

    # --- БД ---
    db = Database(cfg)
    if not db.check_connection():
        logger.error(
            "БД недоступна. Запустите TimescaleDB (см. README.md, docker-compose) "
            "и выполните: python -m storage.init_db"
        )
        return
    run_safe_migrations(db.engine)
    telemetry = TelemetryStore(db, cfg)
    pybit_logger = logging.getLogger("pybit._websocket_stream")
    pybit_telemetry_handler = _PybitTelemetryHandler(telemetry)
    pybit_logger.addHandler(pybit_telemetry_handler)
    runtime = RuntimeService(
        db, cfg.run_id, "collector",
        health_callback=lambda event, severity, status, error: telemetry.record_health(
            "runtime_control", event, severity, status, error=error
        ),
    )
    runtime.start()
    store = MarketDataStore(db)

    # --- 1. REST: исторические данные сразу в БД ---
    # limit=210 (не 200!) -- trend filter (EMA200) требует минимум 202 свечи,
    # без запаса он не заработал бы первые ~30 минут после старта, пока
    # недостающие свечи не накопятся через WebSocket.
    rest = BybitRestClient(cfg)
    try:
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
    except Exception as exc:
        telemetry.record_health(
            "market_collector",
            "dns_failure" if _is_dns_failure(exc) else "rest_refresh_failure",
            "critical", "failed",
            symbol=locals().get("symbol"), error=exc,
        )
        raise

    # --- 2. WebSocket: живой поток пишем через store, буферизация внутри ---
    stream = _start_public_stream(cfg, store, telemetry)
    ws_stale_timeout = max(
        30.0,
        float(os.getenv("WS_STALE_TIMEOUT_SECONDS", "120")),
    )
    ws_reconnect_delay = max(
        1.0,
        float(os.getenv("WS_RECONNECT_DELAY_SECONDS", "5")),
    )

    logger.info(
        "Поток запущен, данные пишутся в БД; WS watchdog=%.0fs. Ctrl+C для остановки.",
        ws_stale_timeout,
    )
    def stop_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        while True:
            time.sleep(1)
            age = stream.seconds_since_message()
            if age <= ws_stale_timeout:
                continue

            logger.error(
                "WS watchdog: сообщений нет %.1fs (лимит %.1fs); "
                "пересоздаю Testnet WebSocket и подписки",
                age,
                ws_stale_timeout,
            )
            telemetry.record_health(
                "market_collector", "websocket_stale", "error", "reconnecting",
                data_age_seconds=age, details={"timeout_seconds": ws_stale_timeout},
            )
            stream.close()
            while True:
                try:
                    stream = _start_public_stream(cfg, store, telemetry)
                    telemetry.record_health(
                        "market_collector", "websocket_reconnect", "info", "recovered"
                    )
                    logger.info("WS watchdog: WebSocket и подписки восстановлены")
                    break
                except Exception as exc:
                    telemetry.record_health(
                        "market_collector", "websocket_reconnect_failure", "error", "failed",
                        error=exc,
                    )
                    logger.exception(
                        "WS watchdog: переподключение не удалось; повтор через %.1fs",
                        ws_reconnect_delay,
                    )
                    time.sleep(ws_reconnect_delay)
    except KeyboardInterrupt:
        logger.info("Останавливаюсь, сбрасываю буферы в БД...")
    finally:
        stream.close()
        store.stop()
        runtime.stop()
        pybit_logger.removeHandler(pybit_telemetry_handler)
        logger.info("Готово.")


if __name__ == "__main__":
    main()
