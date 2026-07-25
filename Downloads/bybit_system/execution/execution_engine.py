"""
Execution Engine: единственный компонент, которому разрешено вызывать
place_order/set_leverage на Bybit. Strategy Engine и Risk Manager сами
ничего на биржу не отправляют — только через этот класс.

Идемпотентность: каждому ордеру присваивается уникальный orderLinkId,
чтобы повторная отправка (например, после retry при таймауте) не создала
дублирующую позицию.
"""

import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from typing import Optional, Dict, Any, List

from pybit.unified_trading import HTTP

from config.settings import BybitConfig
from strategy.signal import Action

logger = logging.getLogger(__name__)

# Bybit отдаёт closed PnL постранично; окно запроса ограничено 7 сутками.
_CLOSED_PNL_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_HISTORY_PAGE_SIZE = 100

# Синтетический retCode отказа безопасного режима. Заведомо вне диапазона
# реальных кодов Bybit, чтобы в логах отказ guard'а нельзя было спутать
# с ответом биржи.
SAFE_MODE_RET_CODE = 999001


class FillStatus(str, Enum):
    """
    Фактическое состояние ордера. retCode == 0 означает лишь ACCEPTED —
    "биржа приняла запрос", а не "позиция открыта".
    """
    REJECTED = "rejected"            # биржа отвергла/отменила без исполнения
    ACCEPTED = "accepted"            # принят, но исполнение не подтверждено
    FILLED = "filled"                # исполнен полностью
    PARTIALLY_FILLED = "partially_filled"
    UNKNOWN = "unknown"              # выяснить не удалось — считать опасным


# Статусы Bybit v5 -> наши. Всё, чего здесь нет, трактуется как UNKNOWN.
_BYBIT_ORDER_STATUS: Dict[str, FillStatus] = {
    "Created": FillStatus.ACCEPTED,
    "New": FillStatus.ACCEPTED,
    "Untriggered": FillStatus.ACCEPTED,
    "Triggered": FillStatus.ACCEPTED,
    "PartiallyFilled": FillStatus.PARTIALLY_FILLED,
    "Filled": FillStatus.FILLED,
    "Rejected": FillStatus.REJECTED,
    "Cancelled": FillStatus.REJECTED,
    "Deactivated": FillStatus.REJECTED,
    # Частично исполнен и затем отменён: экспозиция есть, добирать не будут.
    "PartiallyFilledCanceled": FillStatus.PARTIALLY_FILLED,
}

# Состояния, после которых опрашивать биржу дальше бессмысленно.
_TERMINAL_STATUSES = (FillStatus.FILLED, FillStatus.REJECTED)


@dataclass
class OrderConfirmation:
    status: FillStatus
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    detail: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_exposure(self) -> bool:
        """Есть ли реальная позиция на бирже в результате этого ордера."""
        return self.status in (FillStatus.FILLED, FillStatus.PARTIALLY_FILLED)

    @property
    def is_conclusive(self) -> bool:
        """Знаем ли мы достоверно, чем закончился ордер."""
        return self.status != FillStatus.UNKNOWN


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

    def _safe_mode_block(self, operation: str, symbol: str = "") -> Optional[Dict[str, Any]]:
        """
        ЕДИНСТВЕННЫЙ централизованный guard безопасного режима.

        Правило: TRADING_ENABLED=false запрещает любой запрос, меняющий
        состояние на Bybit (ордера, стопы, плечо). Чтение (баланс, позиции,
        история ордеров, closed PnL) не ограничивается никогда.

        Каждый мутирующий метод этого класса обязан вызвать guard ПЕРВОЙ
        строкой — до расчётов и до любых обращений к session. Новые мутирующие
        методы добавлять только с этим вызовом. Отдельные проверки
        trading_enabled в других слоях допустимы лишь как ранний выход для
        логов — защитой является только эта точка.

        Возвращает None (разрешено) или синтетический отказ в формате ответа
        Bybit, чтобы вызывающий код обработал его как обычный отклонённый запрос.
        """
        if self.cfg.trading_enabled:
            return None
        logger.warning(
            "SAFE MODE: %s%s заблокирован — TRADING_ENABLED=false, состояние биржи не меняем",
            operation, f" {symbol}" if symbol else "",
        )
        return {
            "retCode": SAFE_MODE_RET_CODE,
            "retMsg": f"safe mode (TRADING_ENABLED=false): {operation} blocked",
            "result": {},
            "safe_mode_blocked": True,
        }

    def set_leverage(self, symbol: str, leverage: int):
        if self._safe_mode_block("set_leverage", symbol) is not None:
            return
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
        Кэшируем qtyStep/minOrderQty/tickSize на процесс — они не меняются на лету.

        qtyStep нужен, чтобы количество было кратно шагу лота инструмента.
        tickSize — чтобы цены стоп-лосса и тейк-профита были кратны шагу ЦЕНЫ:
        Bybit отвергает цену, не попадающую на сетку тика (например, для
        BNBUSDT tickSize=0.10, и цена 564.7005 невалидна). Раньше цены
        округлялись жёстко до 4 знаков, что для BTCUSDT/BNBUSDT (tick 0.10),
        ETHUSDT (0.01) и UNIUSDT (0.001) давало невалидный стоп.
        """
        if symbol not in self._lot_size_cache:
            info = self.session.get_instruments_info(category=self.cfg.category, symbol=symbol)
            item = info["result"]["list"][0]
            lot = item["lotSizeFilter"]
            price_filter = item.get("priceFilter") or {}
            try:
                tick_size = float(price_filter.get("tickSize") or 0)
            except (TypeError, ValueError):
                tick_size = 0.0
            if tick_size <= 0:
                logger.warning(
                    "%s: биржа не отдала tickSize — цены SL/TP будут округляться до 4 знаков, "
                    "что может быть невалидно для этого инструмента",
                    symbol,
                )
            self._lot_size_cache[symbol] = {
                "qtyStep": float(lot["qtyStep"]),
                "minOrderQty": float(lot["minOrderQty"]),
                "tickSize": tick_size,
            }
        return self._lot_size_cache[symbol]

    @staticmethod
    def _snap_to_tick(price: float, tick_size: float, round_down: bool) -> float:
        """
        Прижимает цену к сетке тика инструмента.

        round_down выбирается так, чтобы округление всегда играло В ПОЛЬЗУ
        безопасности: стоп-лосс сдвигается ближе к цене входа (срабатывает
        чуть раньше), тейк-профит — тоже ближе (фиксируется чуть раньше).
        Никогда не наоборот: расширять стоп округлением значит молча увеличить
        риск сделки.

        Считаем в Decimal, а не во float: 0.0027 / 0.000001 во float даёт
        2700.0000000000005, и ceil() поднял бы цену на лишний тик.
        """
        if tick_size <= 0:
            return round(price, 4)
        tick = Decimal(str(tick_size))
        steps = Decimal(str(price)) / tick
        steps = steps.to_integral_value(rounding=ROUND_FLOOR if round_down else ROUND_CEILING)
        snapped = steps * tick
        # normalize() убрал бы хвостовые нули, но вернул бы экспоненту (1E-6),
        # а Bybit ждёт обычную десятичную запись — поэтому квантуем по тику.
        return float(snapped.quantize(tick))

    def _price_with_offset(
        self, symbol: str, price: float, pct: float, side: str, is_stop_loss: bool
    ) -> float:
        """Цена SL/TP со смещением в процентах, прижатая к сетке тика инструмента."""
        # ВАЖНО: считаем смещение в ПОЛНОЙ точности, без промежуточного
        # round(,4) -- для дешёвых монет такое округление разрушало сам стоп:
        # у 1000PEPEUSDT с ценой входа 0.00271 стоп 1.5% превращался в 0.37%.
        raw = self._raw_price_offset(price, pct, side, is_stop_loss)
        try:
            tick_size = self._get_lot_size(symbol)["tickSize"]
        except Exception:
            logger.warning(
                "%s: не удалось получить tickSize, округляю цену до 4 знаков", symbol, exc_info=True,
            )
            return raw

        # Округляем ВСЕГДА в сторону цены входа — и для стопа, и для тейка.
        # Где какая цена лежит относительно входа:
        #   long:  SL ниже (округляем вверх), TP выше (округляем вниз)
        #   short: SL выше (округляем вниз),  TP ниже (округляем вверх)
        is_long = side == "Buy"
        round_down = (not is_long) if is_stop_loss else is_long
        return self._snap_to_tick(raw, tick_size, round_down=round_down)

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
        blocked = self._safe_mode_block("open_position", symbol)
        if blocked is not None:
            return blocked
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
            sl_price = self._price_with_offset(symbol, last_price, stop_loss_pct, side, is_stop_loss=True)
            params["stopLoss"] = str(sl_price)
        if take_profit_pct:
            tp_price = self._price_with_offset(symbol, last_price, take_profit_pct, side, is_stop_loss=False)
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
        blocked = self._safe_mode_block("close_position", symbol)
        if blocked is not None:
            return blocked
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
        blocked = self._safe_mode_block("set_trailing_stop", symbol)
        if blocked is not None:
            return blocked
        # Расстояние trailing stop Bybit тоже принимает по сетке тика.
        # round(..., 4) здесь давал невалидное значение для BNBUSDT/BTCUSDT (tick 0.10).
        raw_distance = last_price * distance_pct / 100
        try:
            tick_size = self._get_lot_size(symbol)["tickSize"]
        except Exception:
            logger.warning("%s: не удалось получить tickSize для trailing stop", symbol, exc_info=True)
            tick_size = 0.0
        # Вверх: расстояние меньше тика округлилось бы в 0 и отключило trailing.
        distance_price = self._snap_to_tick(raw_distance, tick_size, round_down=False)
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

    def get_closed_pnl_since(
        self,
        symbol: str,
        start_time_ms: Optional[int] = None,
        max_pages: int = 5,
    ) -> list:
        """
        Closed PnL с постраничным обходом, начиная от start_time_ms (обычно —
        время открытия сделки).

        Зачем: get_closed_pnl(limit=50) отдаёт только последние записи. Если по
        символу после нашей сделки прошло больше закрытий, или бот стоял долго,
        нужное закрытие в это окно не попадает — и сделка навсегда остаётся
        "открытой" в журнале, а её убыток не доходит до дневного лимита.

        Окно ограничено 7 сутками (лимит Bybit): более старый start_time_ms
        подрезается, и это честно логируется.

        ОШИБКИ НЕ ГЛУШАТСЯ. Пустой список означает ровно "закрытий нет", и
        вызывающий код принимает по нему решение об orphaned-сделке. Если бы
        сетевой сбой возвращался как [], временная недоступность API выглядела
        бы как отсутствие закрытия и за несколько циклов пометила бы живую
        сделку orphaned с остановкой торговли. Поэтому любая ошибка страницы
        пробрасывается наверх — цикл просто пропустит символ и повторит позже.
        """
        params = self._history_window_params(symbol, start_time_ms, "closed PnL")
        return self._paginate(self.session.get_closed_pnl, params, max_pages, symbol, "closed PnL")

    def _history_window_params(
        self, symbol: str, start_time_ms: Optional[int], label: str
    ) -> Dict[str, Any]:
        """
        Общие параметры запроса истории. Окно Bybit ограничено 7 сутками:
        более старый start_time_ms подрезается, и это честно логируется.
        """
        params: Dict[str, Any] = {
            "category": self.cfg.category,
            "symbol": symbol,
            "limit": _HISTORY_PAGE_SIZE,
        }
        if start_time_ms is None:
            return params
        now_ms = int(time.time() * 1000)
        oldest_allowed = now_ms - _CLOSED_PNL_MAX_WINDOW_MS
        if start_time_ms < oldest_allowed:
            logger.warning(
                "%s: запрошенный %s от %d старше 7 суток — окно подрезано до %d. "
                "Записи старше этого порога биржа уже не отдаёт.",
                symbol, label, start_time_ms, oldest_allowed,
            )
            start_time_ms = oldest_allowed
        params["startTime"] = start_time_ms
        params["endTime"] = now_ms
        return params

    def _paginate(
        self,
        fetch,
        params: Dict[str, Any],
        max_pages: int,
        symbol: str,
        label: str,
    ) -> List[dict]:
        """
        Постраничный обход истории Bybit по nextPageCursor.

        ОШИБКИ НЕ ГЛУШАТСЯ. Пустой список означает ровно "записей нет", и
        вызывающий код принимает по нему решения (вплоть до orphaned). Если бы
        сетевой сбой возвращался как [], временная недоступность API выглядела
        бы как отсутствие данных и пометила бы живую сделку orphaned с
        остановкой торговли. Поэтому любая ошибка страницы пробрасывается
        наверх — вызывающий пропустит символ и повторит позже.
        """
        params = dict(params)
        rows: List[dict] = []
        cursor = None
        for page in range(max_pages):
            if cursor:
                params["cursor"] = cursor
            try:
                resp = fetch(**params)
            except Exception:
                logger.warning(
                    "%s: ошибка запроса %s на странице %d из %d (получено %d записей) — "
                    "пробрасываю ошибку, решение по данным не принимается",
                    symbol, label, page + 1, max_pages, len(rows),
                )
                raise
            result = resp.get("result") or {}
            page_rows = result.get("list") or []
            rows.extend(page_rows)
            cursor = result.get("nextPageCursor")
            # Пустой курсор или неполная страница — данные закончились.
            if not cursor or len(page_rows) < _HISTORY_PAGE_SIZE:
                break
        else:
            logger.warning(
                "%s: достигнут лимит страниц %s (%d) — возможно, обработаны не все записи",
                symbol, label, max_pages,
            )
        return rows

    def get_order_history(
        self,
        symbol: str,
        order_link_id: Optional[str] = None,
        start_time_ms: Optional[int] = None,
        max_pages: int = 5,
    ) -> List[dict]:
        """
        История ордеров. ТОЛЬКО ЧТЕНИЕ.

        Запрос по orderLinkId Bybit обслуживает БЕЗ ограничения окна в 7 суток —
        поэтому для конкретного нашего ордера временной диапазон не передаётся
        вовсе. Это единственный способ узнать судьбу входа, которому больше
        недели: был ли он вообще исполнен.
        """
        if order_link_id:
            params: Dict[str, Any] = {
                "category": self.cfg.category,
                "symbol": symbol,
                "orderLinkId": order_link_id,
                "limit": _HISTORY_PAGE_SIZE,
            }
        else:
            params = self._history_window_params(symbol, start_time_ms, "order history")
        return self._paginate(self.session.get_order_history, params, max_pages, symbol, "order history")

    def get_executions(
        self,
        symbol: str,
        order_link_id: Optional[str] = None,
        start_time_ms: Optional[int] = None,
        max_pages: int = 5,
    ) -> List[dict]:
        """
        История исполнений (fills). ТОЛЬКО ЧТЕНИЕ.

        Даёт фактическую цену и объём каждого fill — самое точное, что есть
        для восстановления реального входа, когда цена в журнале была лишь
        оценкой (закрытие свечи).
        """
        params: Dict[str, Any] = self._history_window_params(symbol, start_time_ms, "executions")
        if order_link_id:
            params["orderLinkId"] = order_link_id
        return self._paginate(self.session.get_executions, params, max_pages, symbol, "executions")

    def confirm_order(
        self,
        symbol: str,
        order_link_id: str,
        attempts: int = 3,
        delay_seconds: float = 0.6,
    ) -> OrderConfirmation:
        """
        Выясняет, что реально произошло с ордером после retCode == 0.

        Опрос строго ограничен по числу попыток — бесконечного ожидания нет ни в
        одной ветке. Если после всех попыток ясности нет, возвращается UNKNOWN,
        и вызывающий код обязан трактовать это консервативно (блокировка
        символа), а не как "ордер не прошёл".
        """
        if not order_link_id:
            return OrderConfirmation(
                status=FillStatus.UNKNOWN,
                detail="order_link_id отсутствует — идентифицировать ордер невозможно",
            )

        last_seen = OrderConfirmation(
            status=FillStatus.UNKNOWN,
            detail="ордер не найден ни в активных, ни в истории",
        )

        for attempt in range(1, attempts + 1):
            order = self._find_order(symbol, order_link_id)
            if order is not None:
                confirmation = self._confirmation_from_order(order)
                if confirmation.status in _TERMINAL_STATUSES:
                    logger.info(
                        "%s: ордер %s подтверждён как %s (попытка %d/%d, qty=%.8f)",
                        symbol, order_link_id, confirmation.status.value,
                        attempt, attempts, confirmation.filled_qty,
                    )
                    return confirmation
                last_seen = confirmation

            if attempt < attempts:
                time.sleep(delay_seconds)

        # Частичное исполнение — валидный конечный ответ: экспозиция уже есть.
        if last_seen.status == FillStatus.PARTIALLY_FILLED:
            logger.warning(
                "%s: ордер %s остался частично исполненным после %d попыток (qty=%.8f)",
                symbol, order_link_id, attempts, last_seen.filled_qty,
            )
            return last_seen

        # Ордер виден, но всё ещё не исполнен: проверяем позицию — она и есть
        # источник правды о том, появилась ли экспозиция.
        position_confirmation = self._confirm_via_position(symbol, order_link_id, last_seen)
        if position_confirmation is not None:
            return position_confirmation

        logger.error(
            "%s: не удалось выяснить судьбу ордера %s за %d попыток (последнее состояние: %s). "
            "Возвращаю UNKNOWN.",
            symbol, order_link_id, attempts, last_seen.detail,
        )
        return OrderConfirmation(
            status=FillStatus.UNKNOWN,
            detail=f"не подтверждено за {attempts} попыток: {last_seen.detail}",
        )

    def _find_order(self, symbol: str, order_link_id: str) -> Optional[dict]:
        """
        Ищет ордер сначала среди активных (realtime), затем в истории.
        Порядок важен: только что созданный ордер появляется в realtime раньше,
        чем в истории.
        """
        for fetch, source in (
            (self.session.get_open_orders, "realtime"),
            (self.session.get_order_history, "history"),
        ):
            try:
                resp = fetch(
                    category=self.cfg.category,
                    symbol=symbol,
                    orderLinkId=order_link_id,
                )
            except Exception:
                logger.warning(
                    "%s: запрос ордера %s через %s не удался", symbol, order_link_id, source,
                    exc_info=True,
                )
                continue
            for item in (resp.get("result") or {}).get("list") or []:
                if item.get("orderLinkId") == order_link_id:
                    return item
        return None

    @staticmethod
    def _confirmation_from_order(order: dict) -> OrderConfirmation:
        raw_status = str(order.get("orderStatus") or "")
        status = _BYBIT_ORDER_STATUS.get(raw_status, FillStatus.UNKNOWN)
        try:
            filled_qty = float(order.get("cumExecQty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            avg_price = float(order.get("avgPrice") or 0) or None
        except (TypeError, ValueError):
            avg_price = None

        # Bybit помечает частично исполненный и отменённый ордер по-разному в
        # разных версиях API. Если экспозиция есть, "Cancelled" не должен
        # выглядеть как "ничего не произошло".
        if status == FillStatus.REJECTED and filled_qty > 0:
            status = FillStatus.PARTIALLY_FILLED

        return OrderConfirmation(
            status=status,
            filled_qty=filled_qty,
            avg_price=avg_price,
            detail=f"orderStatus={raw_status or 'нет'}, cumExecQty={filled_qty}",
            raw=order,
        )

    def _confirm_via_position(
        self, symbol: str, order_link_id: str, last_seen: OrderConfirmation
    ) -> Optional[OrderConfirmation]:
        """
        Последний рубеж: живая позиция по символу. Не различает наш ордер и чужой,
        поэтому используется только когда по ордеру ясности нет.
        """
        try:
            positions = self.get_open_positions()
        except Exception:
            logger.exception("%s: не удалось проверить позицию для подтверждения ордера", symbol)
            return None

        for position in positions:
            if position.get("symbol") != symbol:
                continue
            try:
                size = float(position.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            if size > 0:
                logger.warning(
                    "%s: ордер %s не подтверждён напрямую (%s), но по символу есть живая "
                    "позиция size=%s — считаю исполненным",
                    symbol, order_link_id, last_seen.detail, size,
                )
                try:
                    avg_price = float(position.get("avgPrice") or 0) or None
                except (TypeError, ValueError):
                    avg_price = None
                return OrderConfirmation(
                    status=FillStatus.FILLED,
                    filled_qty=size,
                    avg_price=avg_price,
                    detail="подтверждено по живой позиции, а не по статусу ордера",
                    raw=position,
                )
        return None

    @staticmethod
    def _raw_price_offset(price: float, pct: float, side: str, is_stop_loss: bool) -> float:
        """
        Цена со смещением в процентах, БЕЗ округления до сетки тика -- этим
        занимается вызывающий код (_price_with_offset), который знает
        tickSize конкретного инструмента. Единственное место, где считается
        эта формула, чтобы не завести повторно дублирующую (и потенциально
        расходящуюся) копию, как это уже было с прежним _calc_price_offset.
        """
        direction = 1 if side == "Buy" else -1
        if is_stop_loss:
            direction *= -1  # SL всегда против направления позиции
        return price * (1 + direction * pct / 100)
