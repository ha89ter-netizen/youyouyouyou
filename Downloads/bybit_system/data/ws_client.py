"""
WebSocket-клиент поверх pybit.unified_trading.WebSocket.

Публичный поток: orderbook, trades, klines, ликвидации — без ключей.
Приватный поток: позиции, ордера, баланс в реальном времени — нужны ключи.

pybit сам управляет reconnect и heartbeat, но мы добавляем обработку
ошибок в колбэках, чтобы одно исключение не роняло весь поток.
"""

import logging
from typing import Callable, Optional

from pybit.unified_trading import WebSocket

from config.settings import BybitConfig

logger = logging.getLogger(__name__)


def _safe_callback(name: str, fn: Callable):
    """Оборачивает пользовательский колбэк, чтобы исключения не убивали WS-поток."""

    def wrapper(message):
        try:
            fn(message)
        except Exception:
            logger.exception("Ошибка в колбэке '%s' на сообщении: %s", name, message)

    return wrapper


class BybitPublicStream:
    """Публичные каналы: не требуют авторизации."""

    def __init__(self, cfg: BybitConfig):
        self.cfg = cfg
        self.ws = WebSocket(
            testnet=cfg.testnet,
            channel_type=cfg.ws_channel_type,
        )
        logger.info("Публичный WS-поток инициализирован (testnet=%s)", cfg.testnet)

    def subscribe_orderbook(self, symbol: str, depth: int, on_message: Callable):
        """depth для linear: 1, 50, 200, 500"""
        self.ws.orderbook_stream(
            depth=depth, symbol=symbol, callback=_safe_callback("orderbook", on_message)
        )

    def subscribe_trades(self, symbol: str, on_message: Callable):
        self.ws.trade_stream(
            symbol=symbol, callback=_safe_callback("trades", on_message)
        )

    def subscribe_kline(self, symbol: str, interval: str, on_message: Callable):
        self.ws.kline_stream(
            interval=interval, symbol=symbol,
            callback=_safe_callback("kline", on_message),
        )

    def subscribe_liquidations(self, symbol: str, on_message: Callable):
        """
        Bybit переименовал канал в 'allLiquidation' (частота push 500ms).
        Метод всё ещё принимает конкретный symbol, несмотря на название 'all'.
        """
        self.ws.all_liquidation_stream(
            symbol=symbol, callback=_safe_callback("liquidation", on_message)
        )

    def subscribe_ticker(self, symbol: str, on_message: Callable):
        """Тикер включает funding rate и open interest в реальном времени."""
        self.ws.ticker_stream(
            symbol=symbol, callback=_safe_callback("ticker", on_message)
        )


class BybitPrivateStream:
    """Приватные каналы: требуют api_key/api_secret."""

    def __init__(self, cfg: BybitConfig):
        if not (cfg.api_key and cfg.api_secret):
            raise RuntimeError(
                "Для приватного WS-потока нужны BYBIT_API_KEY и BYBIT_API_SECRET."
            )
        self.cfg = cfg
        self.ws = WebSocket(
            testnet=cfg.testnet,
            channel_type="private",
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
        )
        logger.info("Приватный WS-поток инициализирован (testnet=%s)", cfg.testnet)

    def subscribe_positions(self, on_message: Callable):
        self.ws.position_stream(callback=_safe_callback("position", on_message))

    def subscribe_orders(self, on_message: Callable):
        self.ws.order_stream(callback=_safe_callback("order", on_message))

    def subscribe_wallet(self, on_message: Callable):
        self.ws.wallet_stream(callback=_safe_callback("wallet", on_message))

    def subscribe_executions(self, on_message: Callable):
        """Исполнения (fills) сделок — важно для точного учёта PnL."""
        self.ws.execution_stream(callback=_safe_callback("execution", on_message))
