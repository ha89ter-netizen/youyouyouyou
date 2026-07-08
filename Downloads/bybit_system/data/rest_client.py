"""
REST-клиент поверх pybit.unified_trading.HTTP.

Отвечает за разовые/периодические запросы: исторические свечи,
funding rate history, open interest, инструменты, баланс/позиции (приватные).

Публичные методы работают без API-ключей.
Приватные (get_positions, get_wallet_balance, place_order...) требуют ключи.
"""

import logging
from typing import List, Dict, Any, Optional

from pybit.unified_trading import HTTP

from config.settings import BybitConfig

logger = logging.getLogger(__name__)


class BybitRestClient:
    def __init__(self, cfg: BybitConfig):
        self.cfg = cfg
        # Если ключи не заданы — pybit всё равно создаст клиент,
        # но приватные эндпоинты вернут ошибку авторизации при вызове.
        self.session = HTTP(
            testnet=cfg.testnet,
            api_key=cfg.api_key or None,
            api_secret=cfg.api_secret or None,
        )
        logger.info(
            "REST-клиент инициализирован (testnet=%s, category=%s)",
            cfg.testnet, cfg.category,
        )

    # ------------------------------------------------------------------
    # ПУБЛИЧНЫЕ ДАННЫЕ (ключи не нужны)
    # ------------------------------------------------------------------

    def get_klines(
        self,
        symbol: str,
        interval: str = "15",  # минуты: 1,3,5,15,30,60,120,240,360,720,D,W,M
        limit: int = 200,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Исторические свечи. Возвращает список словарей вместо сырого ответа биржи."""
        resp = self.session.get_kline(
            category=self.cfg.category,
            symbol=symbol,
            interval=interval,
            limit=limit,
            start=start,
            end=end,
        )
        rows = resp["result"]["list"]
        # Bybit отдаёт [start, open, high, low, close, volume, turnover], новые сначала
        keys = ["start", "open", "high", "low", "close", "volume", "turnover"]
        return [dict(zip(keys, row)) for row in rows]

    def get_orderbook(self, symbol: str, limit: int = 50) -> Dict[str, Any]:
        """Текущий снепшот стакана (глубина до 500 для linear)."""
        resp = self.session.get_orderbook(
            category=self.cfg.category, symbol=symbol, limit=limit
        )
        return resp["result"]

    def get_tickers(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Текущие цены, funding rate, open interest, объёмы за 24ч."""
        resp = self.session.get_tickers(category=self.cfg.category, symbol=symbol)
        return resp["result"]["list"]

    def get_funding_rate_history(
        self, symbol: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        resp = self.session.get_funding_rate_history(
            category=self.cfg.category, symbol=symbol, limit=limit
        )
        return resp["result"]["list"]

    def get_open_interest(
        self, symbol: str, interval_time: str = "1h", limit: int = 200
    ) -> List[Dict[str, Any]]:
        resp = self.session.get_open_interest(
            category=self.cfg.category,
            symbol=symbol,
            intervalTime=interval_time,
            limit=limit,
        )
        return resp["result"]["list"]

    def get_instruments_info(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Тик-сайз, шаг лота, максимальное плечо и т.д. — нужно для Risk Manager."""
        resp = self.session.get_instruments_info(
            category=self.cfg.category, symbol=symbol
        )
        return resp["result"]["list"]

    # ------------------------------------------------------------------
    # ПРИВАТНЫЕ ДАННЫЕ (требуют api_key/api_secret)
    # ------------------------------------------------------------------

    def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        self._require_auth()
        resp = self.session.get_wallet_balance(accountType=account_type)
        return resp["result"]

    def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        self._require_auth()
        resp = self.session.get_positions(
            category=self.cfg.category, symbol=symbol
        )
        return resp["result"]["list"]

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        self._require_auth()
        resp = self.session.get_open_orders(
            category=self.cfg.category, symbol=symbol
        )
        return resp["result"]["list"]

    def _require_auth(self):
        if not (self.cfg.api_key and self.cfg.api_secret):
            raise RuntimeError(
                "Для этого запроса нужны BYBIT_API_KEY и BYBIT_API_SECRET "
                "в переменных окружения."
            )
