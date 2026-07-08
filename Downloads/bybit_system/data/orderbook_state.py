"""
Локальная реконструкция стакана из WebSocket-потока Bybit.

Bybit шлёт "snapshot" (полное состояние) один раз при подписке, а дальше —
только "delta" (сообщения ТОЛЬКО с изменившимися уровнями цены). Уровень
с size="0" означает "удалить эту цену из стакана". Если наивно брать
data["b"][0]/["a"][0] из КАЖДОГО сообщения как "лучшую цену" (как делал
старый код) — на delta-сообщении это может оказаться любой изменившийся
уровень в середине стакана, а не реальный лучший бид/аск.

Правильный подход: держать в памяти полное состояние (цена -> размер)
для каждого символа, применять snapshot как полную замену, а delta —
как точечные правки, и брать топ стакана из АКТУАЛЬНОГО состояния,
а не из сырого сообщения.
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class OrderBookState:
    """Состояние стакана для ОДНОГО символа."""

    def __init__(self):
        self.bids: Dict[float, float] = {}  # price -> size
        self.asks: Dict[float, float] = {}
        self._initialized = False

    def apply(self, msg_type: str, data: dict):
        b = data.get("b", [])
        a = data.get("a", [])

        if msg_type == "snapshot":
            self.bids = {float(p): float(s) for p, s in b}
            self.asks = {float(p): float(s) for p, s in a}
            self._initialized = True
            return

        # delta: применяем точечные правки к уже накопленному состоянию.
        # Если snapshot ещё не приходил (реконнект в середине потока) —
        # применять delta бессмысленно, дождёмся следующего snapshot.
        if not self._initialized:
            return

        for p, s in b:
            price, size = float(p), float(s)
            if size == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size

        for p, s in a:
            price, size = float(p), float(s)
            if size == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size

    def top_of_book(self) -> Optional[dict]:
        """Возвращает реальный лучший бид/аск из текущего состояния, либо None."""
        if not self._initialized or not self.bids or not self.asks:
            return None
        best_bid_price = max(self.bids)
        best_ask_price = min(self.asks)
        return {
            "best_bid_price": best_bid_price,
            "best_bid_size": self.bids[best_bid_price],
            "best_ask_price": best_ask_price,
            "best_ask_size": self.asks[best_ask_price],
        }


class OrderBookRegistry:
    """Держит по одному OrderBookState на символ."""

    def __init__(self):
        self._books: Dict[str, OrderBookState] = {}

    def apply_message(self, msg: dict) -> Optional[dict]:
        """
        Принимает сырое сообщение Bybit WS (orderbook.*), обновляет локальное
        состояние и возвращает АКТУАЛЬНЫЙ top_of_book для этого символа,
        либо None, если состояние ещё не готово (snapshot не пришёл).
        """
        data = msg.get("data", {})
        symbol = data.get("s")
        msg_type = msg.get("type")
        if not symbol or msg_type not in ("snapshot", "delta"):
            return None

        book = self._books.setdefault(symbol, OrderBookState())
        book.apply(msg_type, data)
        return book.top_of_book()
