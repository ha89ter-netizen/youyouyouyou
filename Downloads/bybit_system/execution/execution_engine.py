"""
Execution Engine: единственный компонент, которому разрешено вызывать
place_order/set_leverage на Bybit. Strategy Engine и Risk Manager сами
ничего на биржу не отправляют — только через этот класс.

Идемпотентность: каждому ордеру присваивается уникальный orderLinkId,
чтобы повторная отправка (например, после retry при таймауте) не создала
дублирующую позицию.
"""

import logging
import os
import re
import time
import uuid
from typing import Optional, Dict, Any

from pybit.unified_trading import HTTP

from config.settings import BybitConfig
from strategy.signal import Action

logger = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(self, cfg: BybitConfig):
        if not (cfg.api_key and cfg.api_secret):
            raise RuntimeError(
                "Execution Engine требует BYBIT_API_KEY и BYBIT_API_SECRET "
                "(даже для testnet — создайте ключи на testnet.bybit.com)"
            )
        if not cfg.testnet and os.getenv("ALLOW_PRODUCTION_ORDERS", "").lower() != "true":
            raise RuntimeError(
                "ExecutionEngine refuses to start outside Bybit Testnet. "
                "Set ALLOW_PRODUCTION_ORDERS=true only after a manual production readiness review."
            )
        self.cfg = cfg
        self.session = HTTP(
            testnet=cfg.testnet, api_key=cfg.api_key, api_secret=cfg.api_secret
        )
        self._lot_size_cache: Dict[str, Dict[str, float]] = {}  # symbol -> {qtyStep, minOrderQty}

    def get_account_balance_usdt(self) -> float:
        resp = self.session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        try:
            coins = resp["result"]["list"][0]["coin"]
            usdt = next(c for c in coins if c["coin"] == "USDT")
            return float(usdt["walletBalance"] or 0)
        except (KeyError, IndexError, StopIteration):
            logger.warning("Не удалось прочитать баланс USDT из ответа: %s", resp)
            return 0.0

    def get_open_positions(self) -> list:
        resp = self.session.get_positions(category=self.cfg.category, settleCoin="USDT")
        return resp["result"]["list"]

    def set_leverage(self, symbol: str, leverage: int):
        try:
            self.session.set_leverage(
                category=self.cfg.category, symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage),
            )
        except Exception as e:
            # Bybit возвращает ошибку, если плечо уже установлено в это значение — не критично
            if "leverage not modified" in str(e).lower():
                logger.debug("Плечо для %s уже равно %dx", symbol, leverage)
            else:
                raise

    def _get_lot_size(self, symbol: str) -> Dict[str, float]:
        """
        Кэшируем qtyStep/minOrderQty на процесс — они не меняются на лету.
        Без этого округление количества "на глаз" (например, всегда до 6
        знаков) может выдать qty, не кратный шагу лота конкретного инструмента,
        и биржа отклонит ордер с ошибкой точности.
        """
        if symbol not in self._lot_size_cache:
            info = self.session.get_instruments_info(category=self.cfg.category, symbol=symbol)
            item = info["result"]["list"][0]
            lot = item["lotSizeFilter"]
            self._lot_size_cache[symbol] = {
                "qtyStep": float(lot["qtyStep"]),
                "minOrderQty": float(lot["minOrderQty"]),
            }
        return self._lot_size_cache[symbol]

    def _round_qty(self, symbol: str, raw_qty: float) -> float:
        lot = self._get_lot_size(symbol)
        step = lot["qtyStep"]
        # Округляем ВНИЗ до ближайшего шага -- никогда не открываем позицию БОЛЬШЕ,
        # чем одобрил Risk Manager, лишь немного меньше из-за округления.
        steps = int(raw_qty / step)
        qty = round(steps * step, 10)
        if qty < lot["minOrderQty"]:
            raise ValueError(
                f"Рассчитанное количество {qty} для {symbol} меньше минимального "
                f"{lot['minOrderQty']} -- размер позиции слишком мал для этого инструмента"
            )
        return qty

    def open_position(
        self,
        symbol: str,
        action: Action,
        size_usdt: float,
        leverage: int,
        last_price: float,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None,
        source: str = "unknown",
    ) -> Dict[str, Any]:
        """
        size_usdt — номинальный размер позиции в USDT (с учётом плеча).
        Реальное количество монет = size_usdt / last_price, округлённое
        вниз до шага лота инструмента (qtyStep).
        """
        if action not in (Action.OPEN_LONG, Action.OPEN_SHORT):
            raise ValueError(f"open_position accepts only OPEN_LONG/OPEN_SHORT, got {action}")
        side = "Buy" if action == Action.OPEN_LONG else "Sell"
        qty = self._round_qty(symbol, size_usdt / last_price)

        self.set_leverage(symbol, leverage)

        safe_source = re.sub(r"[^A-Za-z0-9_-]", "_", source)[:10] or "unknown"
        order_link_id = f"{safe_source}-{uuid.uuid4().hex[:16]}"

        params: Dict[str, Any] = {
            "category": self.cfg.category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "orderLinkId": order_link_id,
        }

        if stop_loss_pct:
            sl_price = self._calc_price_offset(last_price, stop_loss_pct, side, is_stop_loss=True)
            params["stopLoss"] = str(sl_price)
        if take_profit_pct:
            tp_price = self._calc_price_offset(last_price, take_profit_pct, side, is_stop_loss=False)
            params["takeProfit"] = str(tp_price)

        logger.info("Отправляю ордер: %s", params)
        resp = self.session.place_order(**params)
        resp["local_order_link_id"] = order_link_id
        logger.info("Ответ биржи: retCode=%s retMsg=%s orderId=%s orderLinkId=%s",
                     resp.get("retCode"), resp.get("retMsg"),
                     resp.get("result", {}).get("orderId"), order_link_id)
        return resp

    def close_position(self, symbol: str, side_to_close: str, qty: float, source: str = "unknown") -> Dict[str, Any]:
        """side_to_close — сторона ТЕКУЩЕЙ позиции ('Buy'/'Sell'); закрываем встречным ордером."""
        close_side = "Sell" if side_to_close == "Buy" else "Buy"
        safe_source = re.sub(r"[^A-Za-z0-9_-]", "_", source)[:10] or "unknown"
        order_link_id = f"{safe_source}-close-{uuid.uuid4().hex[:12]}"
        resp = self.session.place_order(
            category=self.cfg.category, symbol=symbol, side=close_side,
            orderType="Market", qty=str(qty), reduceOnly=True,
            orderLinkId=order_link_id,
        )
        resp["local_order_link_id"] = order_link_id
        logger.info("Закрытие позиции %s: retCode=%s retMsg=%s orderLinkId=%s",
                    symbol, resp.get("retCode"), resp.get("retMsg"), order_link_id)
        return resp

    def set_trailing_stop(self, symbol: str, last_price: float, distance_pct: float):
        """
        Bybit принимает trailing stop как АБСОЛЮТНОЕ расстояние в цене, не в процентах —
        поэтому переводим процент в цену прямо перед вызовом.
        """
        distance_price = round(last_price * distance_pct / 100, 4)
        resp = self.session.set_trading_stop(
            category=self.cfg.category, symbol=symbol,
            trailingStop=str(distance_price), positionIdx=0,
        )
        logger.info(
            "Trailing stop для %s: расстояние=%.4f (%.2f%% от цены %.4f), retCode=%s",
            symbol, distance_price, distance_pct, last_price, resp.get("retCode"),
        )
        return resp

    def get_closed_pnl(self, symbol: str, limit: int = 20) -> list:
        """Последние закрытые сделки с реализованным PnL — источник для журнала и Risk Manager."""
        resp = self.session.get_closed_pnl(category=self.cfg.category, symbol=symbol, limit=limit)
        return resp["result"]["list"]

    @staticmethod
    def _calc_price_offset(price: float, pct: float, side: str, is_stop_loss: bool) -> float:
        direction = 1 if side == "Buy" else -1
        if is_stop_loss:
            direction *= -1  # SL всегда против направления позиции
        return round(price * (1 + direction * pct / 100), 4)
