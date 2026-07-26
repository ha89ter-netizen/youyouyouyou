"""
Strategy Engine: главный оркестратор торгового цикла.

Каждые decision_interval_sec секунд, для каждого символа:
1. Достаёт свежие данные из БД (свечи, funding rate).
2. Спрашивает rule-based стратегию (жёсткая схема).
3. Спрашивает AI-стратегию ("мозг", ищет неочевидное).
4. Если оба источника согласны (или хотя бы один даёт уверенный сигнал
   при отсутствии противоречия) — сигнал уходит в Risk Manager.
5. Risk Manager одобряет/режет параметры.
6. Одобренное уходит в Execution Engine.

Логика примирения сигналов (rule vs AI) — намеренно простая и explicit,
чтобы всегда можно было объяснить, ПОЧЕМУ система открыла сделку.
"""

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.settings import BybitConfig
from storage.db import Database
from storage.models import Candle, FundingRate, OpenInterest, Liquidation, Trade, OrderbookSnapshot
from storage.journal import TradeJournal
from storage.risk_state import RiskStateStore
from strategy.signal import Signal, Action
from timeutils import from_epoch_ms, utc_today, utcnow
from strategy.experts import ExpertSignalCollector
from strategy.indicators import compute_all_indicators, trend_direction
from market_context import MarketContextEngine
from meta_strategy import MetaStrategyManager
from decision_engine import DecisionEngine
from portfolio_risk import PortfolioRiskEngine
from ai_market_analyst import AIMarketAnalyst
from risk.risk_manager import RiskManager, orphan_cause
from execution.execution_engine import ExecutionEngine, FillStatus, OrderConfirmation

logger = logging.getLogger(__name__)


def _unknown_confirmation(detail: str) -> OrderConfirmation:
    return OrderConfirmation(status=FillStatus.UNKNOWN, detail=detail)


def _optional_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _orphan_breaker_reason(order_link_id: str, symbol: str) -> str:
    """
    Единый текст причины breaker для orphaned-сделки.

    Одна функция намеренно: причина взводится из двух мест (обнаружение в цикле
    и восстановление из журнала при старте), а trip_circuit_breaker считает
    повтор no-op только при совпадении и ключа, И текста. Разъехавшиеся строки
    молча превратили бы идемпотентный вызов в переписывание причины.
    """
    return (
        f"orphaned-сделка {order_link_id} по {symbol}: результат неизвестен, "
        f"дневной лимит убытка нельзя считать достоверным"
    )

# Сколько циклов подряд пытаемся найти закрытие для сделки, которую журнал
# считает открытой, а живой позиции нет. После этого — orphaned + circuit breaker.
_ORPHAN_MAX_ATTEMPTS = 3

# Допуск на проскальзывание при сверке цены входа с closed PnL. Нужен потому,
# что цена входа в журнале может быть оценкой (если биржа не подтвердила
# фактическую цену исполнения). Когда фактическая цена известна, совпадение
# получается практически точным и допуск не задействуется.
_PRICE_TOLERANCE_PCT = 0.5

# Окно, за которое Bybit ещё отдаёт closed PnL. Orphaned-сделку старше него
# автоматически восстановить невозможно.
_CLOSED_PNL_WINDOW_SECONDS = 7 * 24 * 60 * 60

# Сколько ждём подтверждения fill, прежде чем считать состояние неизвестным.
# Ордер, зависший в pending дольше этого срока без живой позиции, эскалируется
# в блокировку символа: продолжать вслепую опаснее, чем остановиться.
_PENDING_MAX_AGE_SECONDS = 300

# Bybit's /v5/position/closed-pnl НЕ содержит stopOrderType — только orderType
# ("Market"/"Limit") и execType, которые для любого закрытия позиции одинаковы.
# Проверено по официальной документации: раньше _infer_exit_reason искал
# несуществующее поле и в 90% из 59 закрытых тестовых сделок всегда возвращал
# "manual/unknown". Реальная причина берётся из /v5/execution/list (см.
# _infer_exit_reason) — там stopOrderType есть, и это его допустимые значения.
_STOP_ORDER_TYPE_TO_EXIT_REASON = {
    "TakeProfit": "TP",
    "PartialTakeProfit": "TP",
    "StopLoss": "SL",
    "PartialStopLoss": "SL",
    "TrailingStop": "trailing",
    "Stop": "SL",
    "MmRateClose": "manual (MMR)",
}


@dataclass
class EntryCandidate:
    symbol: str
    final_signal: Signal
    decision_report: object
    last_price: float
    risk_check: object
    atr_pct_of_price: Optional[float]
    spread_pct: Optional[float]
    funding_rate: Optional[float]
    position_size_multiplier: float
    rank_score: float
    entry_snapshot: Optional[dict] = None
    expert_vote_rows: Optional[list] = None


class StrategyEngine:
    def __init__(self, cfg: BybitConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.experts = ExpertSignalCollector()
        self.market_context_engine = MarketContextEngine()
        self.meta_strategy = MetaStrategyManager()
        self.decision_engine = DecisionEngine(
            min_open_confidence=cfg.min_open_confidence,
            min_margin=cfg.min_decision_margin,
            min_rr=cfg.min_rr,
            default_stop_loss_pct=cfg.default_stop_loss_pct,
            default_take_profit_rr=cfg.default_take_profit_rr,
            min_confirming_families=cfg.min_confirming_families,
        )
        self.portfolio_risk = PortfolioRiskEngine(cfg)
        self.ai_market_analyst = AIMarketAnalyst()
        # Состояние Risk Manager персистится: дневной лимит убытка, cooldown и
        # circuit breaker обязаны пережить перезапуск процесса.
        self.risk_manager = RiskManager(cfg, state_store=RiskStateStore(db))
        self.execution = ExecutionEngine(cfg)
        self.journal = TradeJournal(db)
        self._trailing_activated: set = set()  # order_link_id, для которых уже включили trailing
        self._last_entry_ts: Optional[float] = None
        # order_link_id -> сколько циклов подряд не можем найти закрытие
        self._orphan_attempts: dict[str, int] = {}
        self._reconcile_daily_pnl_with_journal()

    def _reconcile_daily_pnl_with_journal(self):
        """
        Сверяет восстановленное состояние Risk Manager с журналом при старте.

        risk_state мог не дописаться (падение между закрытием сделки и записью
        состояния), поэтому журнал закрытых сделок — независимый источник правды.
        При расхождении Risk Manager берёт более консервативное значение.
        """
        try:
            journal_pnl, closed_count = self.journal.sum_closed_pnl_for_utc_day(utc_today())
        except Exception:
            logger.exception(
                "Не удалось свериться с журналом по дневному PnL при старте. "
                "Risk Manager продолжает с сохранённым состоянием."
            )
            return
        self.risk_manager.restore_daily_pnl_from_journal(journal_pnl, closed_count)

        self._restore_breaker_from_orphaned_trades()

    def _restore_breaker_from_orphaned_trades(self):
        """
        Взводит circuit breaker по orphaned-сделкам, найденным в журнале.

        risk_state — кэш, а журнал — источник правды. Если строку состояния
        потеряли (свежая БД, ручная чистка), но в trade_log есть сделки с
        неизвестным результатом, торговать нельзя: без этого бот стартовал бы
        со снятым breaker и считал дневной лимит по неполным данным.

        Идемпотентно: причина именована по order_link_id, повторный взвод той же
        причины с тем же текстом — no-op.
        """
        try:
            orphaned = self.journal.get_orphaned_trades()
        except Exception:
            logger.exception("Не удалось проверить журнал на orphaned-сделки при старте")
            return
        if not orphaned:
            return

        logger.critical(
            "В журнале %d сделок в статусе orphaned — их финансовый результат неизвестен. "
            "Circuit breaker взведён. Автоматический поиск закрытия продолжится в торговом цикле; "
            "состояние смотрите через `python risk_admin.py status`.",
            len(orphaned),
        )
        for trade in orphaned:
            self.risk_manager.trip_circuit_breaker(
                _orphan_breaker_reason(trade["order_link_id"], trade["symbol"]),
                sticky=True,
                cause=orphan_cause(trade["order_link_id"]),
            )

    def run_forever(self):
        logger.info("Strategy Engine запущен, интервал решений: %d сек", self.cfg.decision_interval_sec)
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("Ошибка в торговом цикле, продолжаю после паузы")
            time.sleep(self.cfg.decision_interval_sec)

    def run_once(self):
        balance = self.execution.get_account_balance_usdt()
        positions = self.execution.get_open_positions()
        logger.info("Баланс: %.2f USDT, открытых позиций: %d", balance, len(positions))

        self.risk_manager.ensure_daily_reset(balance)
        self._resolve_pending_entries(positions)
        self._manage_trailing_stops(positions)
        self._sync_closed_trades(positions)

        candidates = []
        for symbol in self.cfg.symbols:
            try:
                result = self._process_symbol(symbol, balance, positions, execute=False)
                if isinstance(result, EntryCandidate):
                    candidates.append(result)
                elif result:
                    positions = self.execution.get_open_positions()
                    logger.info("Позиции обновлены после изменения по %s: %d", symbol, len(positions))
            except Exception:
                logger.exception("Ошибка обработки символа %s", symbol)

        self._execute_ranked_candidates(candidates, balance, positions)

    def _resolve_pending_entries(self, positions: list):
        """
        Разбирает ордера, принятые биржей, но не подтверждённые как исполненные.

        Такое состояние переживает перезапуск (оно в risk_state), поэтому его
        нужно чем-то закрывать, иначе символ останется заблокирован навсегда:
        - есть живая позиция -> судьба ясна, снимаем pending;
        - позиции нет и ждём дольше лимита -> экспозиция неизвестна, блокируем
          символ до ручного разбора вместо того, чтобы молча забыть.
        """
        pending_symbols = self.risk_manager.pending_entry_symbols()
        if not pending_symbols:
            return

        live_symbols = {
            p.get("symbol") for p in positions
            if float(p.get("size", 0) or 0) > 0
        }
        for symbol in pending_symbols:
            if symbol in live_symbols:
                logger.info(
                    "%s: неподтверждённый ордер разрешён — позиция видна на бирже, снимаю pending",
                    symbol,
                )
                self.risk_manager.clear_entry_pending(symbol)
                continue

            age = self.risk_manager.pending_entry_age_seconds(symbol)
            if age is not None and age > _PENDING_MAX_AGE_SECONDS:
                self.risk_manager.clear_entry_pending(symbol)
                self.risk_manager.block_symbol(
                    symbol,
                    f"ордер принят биржей {age:.0f}s назад, но ни исполнение, ни позиция "
                    f"так и не подтвердились",
                )
                logger.critical(
                    "%s: неподтверждённый ордер завис на %.0fs без живой позиции. "
                    "Символ заблокирован до ручной сверки с биржей.",
                    symbol, age,
                )

    def _manage_trailing_stops(self, positions: list):
        """
        Проверяет открытые позиции: если нереализованная прибыль достигла
        порога активации — включает trailing stop (один раз на позицию).
        """
        if not self.cfg.trailing_stop_enabled:
            return
        for p in positions:
            try:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                symbol = p["symbol"]
                entry_price = float(p["avgPrice"])
                mark_price = float(p["markPrice"])
                side = p["side"]  # "Buy" (long) | "Sell" (short)

                pnl_pct = ((mark_price - entry_price) / entry_price * 100
                           if side == "Buy" else (entry_price - mark_price) / entry_price * 100)

                already_trailing = float(p.get("trailingStop", 0) or 0) > 0
                if pnl_pct >= self.cfg.trailing_activation_pct and not already_trailing:
                    if not self.cfg.trading_enabled:
                        # Решение логируем, действие не выполняем. Guard в
                        # ExecutionEngine всё равно заблокировал бы вызов —
                        # здесь ранний выход только ради читаемого лога.
                        logger.info(
                            "%s: SAFE MODE: trailing stop НЕ выставлен (TRADING_ENABLED=false), "
                            "прибыль %.2f%% >= порога %.2f%%",
                            symbol, pnl_pct, self.cfg.trailing_activation_pct,
                        )
                        continue
                    self.execution.set_trailing_stop(symbol, mark_price, self.cfg.trailing_distance_pct)
                    logger.info(
                        "%s: активирован trailing stop, прибыль %.2f%% >= порога %.2f%%",
                        symbol, pnl_pct, self.cfg.trailing_activation_pct,
                    )
            except Exception:
                logger.exception("Ошибка управления trailing stop для позиции %s", p.get("symbol"))

    def _sync_closed_trades(self, positions: Optional[list] = None):
        """
        Сверяет журнал с биржей: если сделка помечена у нас как "open", а на бирже
        уже закрыта (по SL, TP, trailing stop или вручную) — подтягивает реальный
        PnL и обновляет запись + Risk Manager.

        Матчинг closed PnL с нашей сделкой НЕ по orderLinkId: в ответе
        get_closed_pnl этого поля нет вообще (закрывающий ордер при срабатывании
        SL/TP создаётся биржей автоматически). Вместо этого матчим по символу +
        цене входа (с допуском на проскальзывание) + времени (закрытие должно
        быть позже открытия). Это надёжно, так как Risk Manager не даёт открыть
        вторую позицию по тому же символу, пока текущая не закрыта — на символ
        в любой момент максимум одна открытая сделка.

        ПРИЧИНА закрытия (SL/TP/trailing/наш Exit Manager/ручное) берётся
        отдельно — из /v5/execution/list, сматченного по orderId с закрывающим
        ордером (см. _infer_exit_reason). get_closed_pnl этой информации не
        содержит вовсе.
        """
        live_symbols = {
            p.get("symbol")
            for p in (positions or [])
            if float(p.get("size", 0) or 0) > 0
        }

        for symbol in self.cfg.symbols:
            # И "open", и "orphaned": orphaned обязан пересверяться, иначе его
            # результат навсегда выпадет из дневного PnL. Раньше сюда попадали
            # только "open", и orphaned становился вечным тупиком.
            trades = self.journal.get_unresolved_trades(symbol)
            if not trades:
                continue
            trades = [t for t in trades if self._is_worth_reconciling(t)]
            if not trades:
                continue

            try:
                # Ищем от момента открытия САМОЙ СТАРОЙ неразобранной сделки и с
                # пагинацией. Прежний get_closed_pnl(limit=50) отдавал только
                # последние записи: если после нашей сделки прошло больше
                # закрытий или бот стоял долго, нужное закрытие не попадало в
                # окно, и сделка навсегда оставалась "открытой" в журнале.
                oldest_ms = min(
                    (t["opened_at_ms"] for t in trades if t.get("opened_at_ms") is not None),
                    default=None,
                )
                closed = self.execution.get_closed_pnl_since(symbol, start_time_ms=oldest_ms)
            except Exception:
                # Ошибка API != "закрытий нет". Пропускаем символ целиком, не
                # трогая счётчики orphan: иначе сетевой сбой пометил бы живые
                # сделки orphaned.
                logger.exception("Не удалось получить closed PnL для %s — символ пропущен в этом цикле", symbol)
                continue

            # Executions — только для ТОЧНОСТИ exit_reason, не для решения о
            # закрытии сделки. В отличие от ошибки closed_pnl выше, ошибка
            # здесь не должна блокировать реконсиляцию — просто exit_reason
            # в этом цикле деградирует до "manual/unknown", а не блокирует
            # сам факт учёта PnL.
            try:
                executions = self.execution.get_executions(symbol, start_time_ms=oldest_ms)
            except Exception:
                logger.warning(
                    "%s: не удалось получить executions — exit_reason в этом цикле будет неточным",
                    symbol, exc_info=True,
                )
                executions = []
            exec_by_order_id = self._index_executions_by_order_id(executions)

            has_live_position = symbol in live_symbols
            for trade in trades:
                match = self._find_matching_closed_pnl(trade, closed)
                if match is None:
                    self._handle_unmatched_trade(
                        symbol, trade, len(closed), positions, has_live_position,
                    )
                    continue
                self._apply_closed_pnl(symbol, trade, match, exec_by_order_id)

    @staticmethod
    def _index_executions_by_order_id(executions: list) -> dict:
        """
        orderId -> первый execution этого ордера. stopOrderType и orderLinkId
        одинаковы у всех fill'ов одного ордера, поэтому достаточно одного.
        """
        index: dict = {}
        for e in executions:
            oid = e.get("orderId")
            if oid and oid not in index:
                index[oid] = e
        return index

    def _apply_closed_pnl(self, symbol: str, trade: dict, match: dict, exec_by_order_id: Optional[dict] = None):
        """
        Записывает найденный результат сделки. Единственная точка, где дневной
        PnL пополняется реализованным результатом.

        Идемпотентность обеспечивает журнал: record_closed_pnl вызывается ТОЛЬКО
        если log_exit сообщил recorded=True, то есть если именно этот вызов
        перевёл сделку в "closed". Повторная реконсиляция уже закрытой сделки
        вернёт recorded=False и ничего не прибавит.
        """
        order_link_id = trade["order_link_id"]
        self._orphan_attempts.pop(order_link_id, None)

        exit_price = float(match.get("avgExitPrice", 0) or 0)
        pnl_usdt = float(match.get("closedPnl", 0) or 0)
        closed_at = self._closed_at_from_match(match)
        execution_record = (exec_by_order_id or {}).get(match.get("orderId"))
        exit_reason = self._infer_exit_reason(match, execution_record)
        exit_snapshot = self._build_exit_snapshot(symbol, match)
        exit_fee = _optional_float(
            match.get("closeFee") or (execution_record or {}).get("execFee")
        )
        open_fee = _optional_float(match.get("openFee"))
        total_fee = (
            open_fee + exit_fee
            if open_fee is not None and exit_fee is not None
            else None
        )

        result = self.journal.log_exit(
            order_link_id,
            exit_price,
            pnl_usdt,
            exit_reason=exit_reason,
            exit_snapshot=exit_snapshot,
            closed_at=closed_at,
            exchange_exit_order_id=match.get("orderId"),
            exit_fee_usdt=exit_fee,
            total_fee_usdt=total_fee,
        )
        if not result.recorded:
            if result.already_closed:
                logger.debug(
                    "%s: сделка %s уже закрыта — PnL повторно не учитывается",
                    symbol, order_link_id,
                )
            return

        effective_closed_at = result.closed_at or utcnow()
        if effective_closed_at.date() == utc_today():
            self.risk_manager.record_closed_pnl(pnl_usdt)
        else:
            # Сделка закрылась в прошлые сутки (например, восстановленный orphan).
            # Прибавлять её к сегодняшнему дневному лимиту было бы неверно:
            # лимит считает убыток за текущий торговый день.
            logger.warning(
                "%s: сделка %s закрылась %s (не сегодня) — её PnL=%.4f USDT записан в журнал, "
                "но в дневной лимит за сегодня не включён",
                symbol, order_link_id, effective_closed_at.date(), pnl_usdt,
            )

        if result.recovered_from_orphan:
            self._on_orphan_recovered(symbol, order_link_id, pnl_usdt, effective_closed_at)

        holding_seconds = self._holding_seconds(trade.get("opened_at_ms"))
        pnl_pct = self._position_pnl_pct(trade, exit_price)
        logger.info(
            "TRADE_CLOSE symbol=%s direction=%s entry=%.6f exit=%.6f pnl_usdt=%.4f "
            "pnl_pct=%.3f holding_seconds=%s exit_reason=%s recovered=%s orderLinkId=%s",
            symbol, trade.get("action"), trade["entry_price"], exit_price, pnl_usdt,
            pnl_pct, holding_seconds, exit_reason, result.recovered_from_orphan, order_link_id,
        )

    def _on_orphan_recovered(self, symbol: str, order_link_id: str, pnl_usdt: float, closed_at):
        """
        Сделка вернулась из orphaned. Снимаем ровно ту причину circuit breaker,
        которую она породила — остальные (например, дневной лимит убытка)
        остаются в силе.

        Идемпотентно: resolve_breaker_cause по уже снятой причине вернёт False.
        """
        resolved = self.risk_manager.resolve_breaker_cause(orphan_cause(order_link_id))
        logger.warning(
            "ORPHAN_RECOVERED symbol=%s orderLinkId=%s pnl_usdt=%.4f closed_at=%s "
            "breaker_cause_resolved=%s remaining_causes=%s",
            symbol, order_link_id, pnl_usdt, closed_at.isoformat(),
            resolved, ", ".join(self.risk_manager.breaker_causes()) or "нет",
        )

    def _is_worth_reconciling(self, trade: dict) -> bool:
        """
        Orphaned-сделку старше окна closed PnL (7 суток) биржа уже не отдаст —
        сверять её бессмысленно, и незачем каждый цикл расширять окно запроса
        до её времени открытия. Такая сделка остаётся orphaned навсегда и
        разбирается только человеком.
        """
        if trade.get("status") != "orphaned":
            return True
        opened_at_ms = trade.get("opened_at_ms")
        if opened_at_ms is None:
            return False
        age_seconds = (time.time() * 1000 - opened_at_ms) / 1000
        if age_seconds > _CLOSED_PNL_WINDOW_SECONDS:
            logger.debug(
                "%s: orphaned-сделка %s старше окна closed PnL — автоматическая сверка невозможна",
                trade.get("symbol"), trade.get("order_link_id"),
            )
            return False
        return True

    @staticmethod
    def _closed_at_from_match(match: dict):
        """Реальное время закрытия с биржи, а не 'сейчас'."""
        for key in ("updatedTime", "createdTime"):
            closed_at = from_epoch_ms(match.get(key))
            if closed_at is not None:
                return closed_at
        return None

    def _handle_unmatched_trade(
        self,
        symbol: str,
        trade: dict,
        scanned: int,
        positions: Optional[list],
        has_live_position: bool,
    ):
        """
        Закрытие для неразобранной сделки не найдено. Что делать — зависит от
        того, есть ли живая позиция.
        """
        order_link_id = trade["order_link_id"]

        if has_live_position:
            # Позиция жива: сделка совершенно нормальна, закрытия и не должно быть.
            # Счётчик безуспешных попыток сбрасываем — иначе редкие моменты, когда
            # биржа не показала позицию, накапливались бы за часы и в итоге
            # пометили бы orphaned полностью здоровую сделку.
            self._orphan_attempts.pop(order_link_id, None)
            return

        if positions is None:
            # Снимка позиций нет — судить не о чем.
            return

        if trade.get("status") == "orphaned":
            # Уже orphaned и breaker уже взведён: просто продолжаем тихо искать.
            # Повторно взводить причину не нужно — она и так активна.
            logger.info(
                "%s: orphaned-сделка %s пока не восстановлена (просмотрено %d записей closed PnL). "
                "Поиск продолжится в следующих циклах.",
                symbol, order_link_id, scanned,
            )
            return

        attempts = self._orphan_attempts.get(order_link_id, 0) + 1
        self._orphan_attempts[order_link_id] = attempts

        logger.warning(
            "%s: журнал считает сделку %s открытой, но live-позиции нет; "
            "closed_pnl не найден по цене %.4f среди %d записей (попытка %d/%d). "
            "Нужна ручная сверка или расширение окна closed_pnl.",
            symbol, order_link_id, trade["entry_price"], scanned, attempts, _ORPHAN_MAX_ATTEMPTS,
        )

        if attempts < _ORPHAN_MAX_ATTEMPTS:
            return

        reason = (
            f"закрытие не найдено за {attempts} попыток: позиции нет, "
            f"closed_pnl по цене входа {trade['entry_price']} отсутствует"
        )
        if not self.journal.mark_orphaned(order_link_id, reason):
            return
        self._orphan_attempts.pop(order_link_id, None)
        logger.critical(
            "%s: сделка %s признана ORPHANED — финансовый результат неизвестен, "
            "дневной PnL считается по неполным данным. Торговля останавливается. "
            "Поиск закрытия продолжится автоматически; при находке статус снимется сам.",
            symbol, order_link_id,
        )
        self.risk_manager.trip_circuit_breaker(
            _orphan_breaker_reason(order_link_id, symbol),
            # Неизвестный результат не рассасывается от наступления полуночи.
            # Причина именована: когда сделка восстановится, снимется ровно она,
            # не задев остальные причины.
            sticky=True,
            cause=orphan_cause(order_link_id),
        )

    @classmethod
    def _find_matching_closed_pnl(
        cls,
        trade: dict,
        closed_pnl_list: list,
        tolerance_pct: float = _PRICE_TOLERANCE_PCT,
    ) -> Optional[dict]:
        """
        Ищет запись closed_pnl, соответствующую нашей неразобранной сделке.

        Критерии, от жёсткого к мягкому:
        1. направление: сторона закрывающего ордера должна быть ПРОТИВОПОЛОЖНА
           направлению нашей позиции (long закрывается Sell). Отсекает ложный
           матч длинной и короткой сделки, открытых по одной цене;
        2. время: закрытие не может быть раньше открытия;
        3. цена входа: avgEntryPrice с допуском на проскальзывание.

        Из подходящих берём запись с БЛИЖАЙШЕЙ ценой входа, а при равной цене —
        самую раннюю по времени. Раньше отбор шёл только по времени, из-за чего
        при нескольких близких по цене закрытиях мог выиграть не тот кандидат.
        """
        entry_price = trade.get("entry_price")
        if not entry_price:
            return None
        expected_close_side = cls._expected_close_side(trade.get("action"))

        candidates = []
        for c in closed_pnl_list:
            avg_entry = c.get("avgEntryPrice")
            # In Bybit closed_pnl, createdTime can describe the opening order;
            # updatedTime is the actual close/update timestamp.
            closed_time = c.get("updatedTime") or c.get("createdTime")
            if avg_entry is None or closed_time is None:
                continue
            try:
                avg_entry = float(avg_entry)
                closed_time_ms = int(closed_time)
            except (TypeError, ValueError):
                continue

            side = c.get("side")
            if expected_close_side and side and side != expected_close_side:
                continue  # закрытие позиции другого направления

            if trade["opened_at_ms"] is not None and closed_time_ms < trade["opened_at_ms"]:
                continue  # закрытие раньше открытия -- не может относиться к этой сделке

            price_diff_pct = abs(avg_entry - entry_price) / entry_price * 100
            if price_diff_pct <= tolerance_pct:
                candidates.append((price_diff_pct, closed_time_ms, c))

        if not candidates:
            return None
        candidates.sort(key=lambda x: (round(x[0], 6), x[1]))
        return candidates[0][2]

    @staticmethod
    def _expected_close_side(action: Optional[str]) -> Optional[str]:
        """Сторона ордера, которым закрывается наша позиция."""
        if action in (Action.OPEN_LONG.value, "open_long"):
            return "Sell"
        if action in (Action.OPEN_SHORT.value, "open_short"):
            return "Buy"
        return None

    # ------------------------------------------------------------------

    def _process_symbol(self, symbol: str, balance: float, positions: list, execute: bool = True):
        candles_df = self._load_recent_candles(symbol, limit=210)
        if candles_df is None or len(candles_df) < 30:
            logger.debug("%s: недостаточно свечей в БД для анализа", symbol)
            return False

        funding_info = self._load_latest_funding(symbol)
        funding_rate = funding_info["rate"] if funding_info else None
        funding_trend = self._load_funding_trend(symbol, limit=8)
        oi_trend = self._load_oi_trend(symbol, limit=20)
        orderbook = self._load_latest_orderbook(symbol)
        trade_flow = self._load_trade_flow(symbol, minutes=15)
        liquidations = self._load_recent_liquidations(symbol, minutes=60)
        freshness = self._check_data_freshness(symbol, candles_df, funding_info, oi_trend, orderbook, trade_flow)
        if freshness["critical"]:
            logger.warning("%s: пропускаю символ из-за устаревших критичных данных: %s", symbol, "; ".join(freshness["warnings"]))
            return False

        indicators = compute_all_indicators(candles_df)
        trend = trend_direction(candles_df)  # "long" | "short" | "neutral" | None (мало данных)

        market_snapshot = self._build_market_snapshot(
            symbol, candles_df, funding_rate, funding_trend, oi_trend,
            orderbook, trade_flow, liquidations, indicators,
        )
        market_snapshot["trend_filter"] = trend
        market_snapshot["data_warnings"] = freshness["warnings"]

        market_context = self.market_context_engine.analyze(symbol, candles_df, market_snapshot)
        meta_decision = self.meta_strategy.evaluate(market_context)
        expert_signals = self.experts.collect(symbol, candles_df, funding_rate or 0.0, market_snapshot)
        ai_analysis = self.ai_market_analyst.analyze(symbol, market_snapshot, market_context)
        decision_report = self.decision_engine.decide(
            symbol=symbol,
            context=market_context,
            meta=meta_decision,
            expert_signals=expert_signals,
            ai_analysis=ai_analysis.conclusion,
        )
        final_signal = self._apply_trend_filter(decision_report.final_signal, trend, market_context, symbol)
        self._log_decision_summary(symbol, decision_report, final_signal, market_context, trend)

        logger.info(
            "%s: context=[%s] experts=%s trend=%s data=%s -> итог=%s (%s)",
            symbol,
            market_context.summary(),
            ", ".join(f"{s.source}:{s.action.value}:{s.confidence:.2f}" for s in expert_signals),
            trend or "недостаточно данных",
            "OK" if not freshness["warnings"] else "; ".join(freshness["warnings"]),
            final_signal.action, final_signal.reason,
        )

        existing_position = self._find_open_position(symbol, positions)
        if existing_position is not None:
            # Позиция уже открыта -- проверяем, не пора ли её ЗАКРЫТЬ по новым
            # данным (разворотный сигнал или смена старшего тренда), вместо
            # того чтобы пассивно ждать, пока цена дойдёт до фиксированного
            # SL/TP. Пока позиция открыта, новую по этому же символу не открываем.
            return self._manage_exit(symbol, existing_position, final_signal, trend)

        if final_signal.action == Action.HOLD:
            return False

        block_reason = self._entry_block_reason(symbol, positions)
        if block_reason:
            logger.info("%s: новый вход заблокирован: %s", symbol, block_reason)
            return False

        portfolio_check = self.portfolio_risk.evaluate(final_signal, positions)
        if not portfolio_check.approved:
            logger.info("%s: сигнал отклонён Portfolio Risk Engine: %s", symbol, portfolio_check.reason)
            return False

        last_price = float(candles_df["close"].iloc[-1])
        check = self.risk_manager.evaluate(
            final_signal, positions, balance,
            atr_pct_of_price=indicators.get("atr_pct_of_price") if indicators else None,
            spread_pct=orderbook.get("spread_pct") if orderbook else None,
            funding_rate=funding_rate,
            position_size_multiplier=meta_decision.position_size_multiplier,
        )

        if not check.approved:
            logger.info("%s: сигнал отклонён Risk Manager: %s", symbol, check.reason)
            return False

        candidate = EntryCandidate(
            symbol=symbol,
            final_signal=final_signal,
            decision_report=decision_report,
            last_price=last_price,
            risk_check=check,
            atr_pct_of_price=indicators.get("atr_pct_of_price") if indicators else None,
            spread_pct=orderbook.get("spread_pct") if orderbook else None,
            funding_rate=funding_rate,
            position_size_multiplier=meta_decision.position_size_multiplier,
            rank_score=self._rank_entry_candidate(final_signal, decision_report),
            entry_snapshot=self._build_entry_snapshot(
                symbol=symbol,
                final_signal=final_signal,
                decision_report=decision_report,
                market_context=market_context,
                market_snapshot=market_snapshot,
                candles_df=candles_df,
                indicators=indicators,
                last_price=last_price,
                risk_check=check,
                trend_filter=trend,
                meta_decision=meta_decision,
            ),
            expert_vote_rows=self._expert_vote_rows(decision_report),
        )
        logger.info(
            "%s: кандидат на вход прошёл фильтры: action=%s confidence=%.3f expected_rr=%s "
            "rank=%.3f confirmations=%d families=%s regime=%s trend=%s rejected=%s",
            symbol,
            final_signal.action.value,
            final_signal.confidence,
            decision_report.expected_rr,
            candidate.rank_score,
            decision_report.confirmation_count,
            ", ".join(decision_report.confirmation_families) or "нет",
            market_context.regime,
            market_context.trend,
            decision_report.rejected_actions,
        )
        if not execute:
            return candidate
        return self._execute_candidate(candidate)

    def _execute_ranked_candidates(self, candidates: list, balance: float, positions: list):
        if not candidates:
            return

        ranked = sorted(candidates, key=lambda c: c.rank_score, reverse=True)
        logger.info(
            "Кандидаты цикла по качеству: %s",
            "; ".join(f"{c.symbol}:{c.final_signal.action.value}:rank={c.rank_score:.3f}" for c in ranked),
        )

        opened = 0
        max_new = max(0, self.cfg.max_new_positions_per_cycle)
        for candidate in ranked:
            if opened >= max_new:
                logger.info(
                    "%s: кандидат отложен anti-burst: достигнут лимит новых входов за цикл %d",
                    candidate.symbol, max_new,
                )
                continue

            # Повторная проверка перед самым ордером: между сбором кандидатов и
            # этим моментом мог открыться вход по тому же символу (в том числе
            # предыдущим кандидатом этого же цикла).
            block_reason = self._entry_block_reason(candidate.symbol, positions)
            if block_reason:
                logger.info("%s: кандидат пропущен: %s", candidate.symbol, block_reason)
                continue

            portfolio_check = self.portfolio_risk.evaluate(candidate.final_signal, positions)
            if not portfolio_check.approved:
                logger.info(
                    "%s: кандидат отклонён при повторной проверке Portfolio Risk: %s",
                    candidate.symbol, portfolio_check.reason,
                )
                continue

            fresh_risk_check = self.risk_manager.evaluate(
                candidate.final_signal,
                positions,
                balance,
                atr_pct_of_price=candidate.atr_pct_of_price,
                spread_pct=candidate.spread_pct,
                funding_rate=candidate.funding_rate,
                position_size_multiplier=candidate.position_size_multiplier,
            )
            if not fresh_risk_check.approved:
                logger.info(
                    "%s: кандидат отклонён при повторной проверке Risk Manager: %s",
                    candidate.symbol, fresh_risk_check.reason,
                )
                continue
            candidate.risk_check = fresh_risk_check

            last_entry_ts = getattr(self, "_last_entry_ts", None)
            if last_entry_ts is not None:
                elapsed = time.time() - last_entry_ts
                if elapsed < self.cfg.min_seconds_between_entries:
                    logger.info(
                        "%s: кандидат отложен anti-burst: прошло %.1fs из %ds после последнего входа",
                        candidate.symbol, elapsed, self.cfg.min_seconds_between_entries,
                    )
                    continue

            if self._execute_candidate(candidate):
                opened += 1
                self._last_entry_ts = time.time()
                positions = self.execution.get_open_positions()

    @staticmethod
    def _rank_entry_candidate(signal: Signal, decision_report) -> float:
        rr = decision_report.expected_rr or 0.0
        rr_component = min(rr, 4.0) * 0.03
        confirmation_component = min(decision_report.confirmation_count, 4) * 0.08
        risk_penalty = decision_report.risk_score * 0.25
        return round(signal.confidence + confirmation_component + rr_component - risk_penalty, 4)

    def _execute_candidate(self, candidate: EntryCandidate) -> bool:
        symbol = candidate.symbol
        final_signal = candidate.final_signal
        check = candidate.risk_check
        decision_report = candidate.decision_report
        last_price = candidate.last_price

        if not self.cfg.trading_enabled:
            logger.info("%s: сигнал %s не исполнен: TRADING_ENABLED=false", symbol, final_signal.action.value)
            return False

        resp = self.execution.open_position(
            symbol=symbol,
            action=final_signal.action,
            size_usdt=check.approved_size_usdt,
            leverage=check.approved_leverage,
            last_price=last_price,
            stop_loss_pct=final_signal.stop_loss_pct or self.cfg.default_stop_loss_pct,
            take_profit_pct=final_signal.take_profit_pct,
            source=final_signal.source,
	)
        if resp.get("retCode") == 0:
            order_link_id = (
                resp.get("local_order_link_id")
                or resp.get("result", {}).get("orderLinkId")
                or resp.get("retExtInfo", {}).get("orderLinkId")
                or ""
            )

            # Биржа приняла запрос — с этой секунды возможна реальная экспозиция.
            # Счётчики и cooldown фиксируем ПЕРВЫМ делом, до журнала и до
            # подтверждения fill: любая последующая ошибка (БД, сеть, падение
            # процесса) не должна давать право на повторный вход по символу.
            self.risk_manager.record_open_trade(symbol)
            self.risk_manager.mark_entry_pending(symbol)

            if not order_link_id:
                # Ордер живёт на бирже, но идентифицировать его мы не можем:
                # ни подтвердить, ни сопоставить с закрытием.
                self.risk_manager.block_symbol(
                    symbol, "биржа приняла ордер, но order_link_id потерян"
                )
                logger.critical(
                    "%s: биржа приняла ордер, но order_link_id потерян — символ заблокирован, "
                    "требуется ручная сверка позиции с биржей: %s",
                    symbol, resp,
                )
                return True

            confirmation = self._confirm_entry_fill(symbol, order_link_id)

            # Реальные цифры исполнения важнее наших предположений.
            # last_price — это закрытие свечи из БД, а не цена сделки; именно
            # из-за этого расхождения сверка с closed PnL вынуждена работать с
            # допуском ±0.5%. Если биржа сказала фактическую цену и объём —
            # пишем в журнал их, и тогда avgEntryPrice совпадает почти точно.
            entry_price = confirmation.avg_price or last_price
            filled_size_usdt = check.approved_size_usdt
            if confirmation.filled_qty > 0 and confirmation.avg_price:
                filled_size_usdt = confirmation.filled_qty * confirmation.avg_price
                if abs(filled_size_usdt - check.approved_size_usdt) > 0.01:
                    logger.info(
                        "%s: фактический размер позиции %.4f USDT отличается от одобренного "
                        "%.4f USDT (округление лота или частичное исполнение) — в журнал идёт фактический",
                        symbol, filled_size_usdt, check.approved_size_usdt,
                    )

            if confirmation.status == FillStatus.REJECTED:
                # Экспозиции нет. Cooldown и счётчик оставляем взведёнными
                # намеренно: отклонённый ордер — сигнал, что по символу что-то
                # не так, и долбиться в него в том же цикле не нужно.
                logger.warning(
                    "%s: ордер %s отклонён биржей после принятия (%s) — позиции нет",
                    symbol, order_link_id, confirmation.detail,
                )
                return False

            sl_pct = final_signal.stop_loss_pct or self.cfg.default_stop_loss_pct
            tp_pct = final_signal.take_profit_pct
            stop_loss_price = self._price_from_pct(
                last_price, final_signal.action, sl_pct, is_stop=True
            )
            take_profit_price = self._price_from_pct(
                last_price, final_signal.action, tp_pct, is_stop=False
            )
            stop_loss_price = _optional_float(
                resp.get("local_stop_loss_price")
            ) or stop_loss_price
            take_profit_price = _optional_float(
                resp.get("local_take_profit_price")
            ) or take_profit_price
            exchange_entry_order_id = (
                confirmation.raw.get("orderId")
                or resp.get("result", {}).get("orderId")
            )
            fee_reader = getattr(self.execution, "get_order_fee_usdt", None)
            entry_fee_usdt = fee_reader(symbol, order_link_id) if fee_reader else None
            logger.info(
                "TRADE_OPEN symbol=%s direction=%s size_usdt=%.4f leverage=%sx entry=%.6f "
                "sl_pct=%s sl_price=%s tp_pct=%s tp_price=%s orderLinkId=%s supporters=%s reason=%s",
                symbol,
                final_signal.action.value,
                filled_size_usdt,
                check.approved_leverage,
                entry_price,
                sl_pct,
                stop_loss_price,
                tp_pct,
                take_profit_price,
                order_link_id,
                self._supporting_experts(decision_report),
                final_signal.reason,
            )

            journal_saved = self.journal.log_entry(
                symbol=symbol,
                action=final_signal.action,
                source=final_signal.source,
                reason=decision_report.journal_reason(),
                entry_price=entry_price,
                size_usdt=filled_size_usdt,
                leverage=check.approved_leverage,
                stop_loss_pct=final_signal.stop_loss_pct or self.cfg.default_stop_loss_pct,
                take_profit_pct=final_signal.take_profit_pct,
                order_link_id=order_link_id,
                market_context=decision_report.market_context.summary(),
                regime=decision_report.market_context.regime,
                trend=decision_report.market_context.trend,
                decision_confidence=decision_report.confidence,
                expected_rr=decision_report.expected_rr,
                confirmation_count=decision_report.confirmation_count,
                confirmation_families=", ".join(decision_report.confirmation_families),
                entry_reason=decision_report.journal_reason(limit=2000),
                entry_snapshot=candidate.entry_snapshot,
                expert_votes=candidate.expert_vote_rows,
                run_id=self.cfg.run_id,
                exchange_entry_order_id=exchange_entry_order_id,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                entry_fee_usdt=entry_fee_usdt,
            )
            if not journal_saved:
                # Счётчик и cooldown уже зафиксированы выше, поэтому повторного
                # входа по символу не будет. Но сделку теперь нечем сверять с
                # закрытием, поэтому символ блокируется до ручного разбора.
                self.risk_manager.block_symbol(
                    symbol, "ордер исполнен, но вход не записан в журнал — сверка с закрытием невозможна"
                )
                logger.critical(
                    "%s: ордер создан, но вход не записан в trade_log; позиция требует ручной сверки. "
                    "Символ заблокирован для новых входов. order_link_id=%s",
                    symbol, order_link_id,
                )
            return True

        logger.warning(
            "%s: ордер отклонён биржей retCode=%s retMsg=%s",
            symbol, resp.get("retCode"), resp.get("retMsg"),
        )
        return False

    def _confirm_entry_fill(self, symbol: str, order_link_id: str):
        """
        Выясняет судьбу принятого ордера и приводит состояние Risk Manager
        в соответствие. Возвращает OrderConfirmation.

        Ключевое правило: UNKNOWN — это НЕ "ордера нет". Мы не знаем, есть ли
        экспозиция, поэтому символ блокируется до ручного разбора, и пишется
        CRITICAL. Сам факт входа при этом считается состоявшимся.
        """
        try:
            confirmation = self.execution.confirm_order(symbol, order_link_id)
        except Exception:
            logger.exception("%s: подтверждение ордера %s упало с ошибкой", symbol, order_link_id)
            self.risk_manager.block_symbol(
                symbol, f"подтверждение ордера {order_link_id} завершилось ошибкой"
            )
            logger.critical(
                "%s: состояние ордера %s неизвестно из-за ошибки подтверждения — "
                "символ заблокирован, требуется ручная сверка с биржей",
                symbol, order_link_id,
            )
            return _unknown_confirmation("исключение при подтверждении")

        if confirmation.status == FillStatus.UNKNOWN:
            self.risk_manager.block_symbol(
                symbol, f"состояние ордера {order_link_id} не подтверждено: {confirmation.detail}"
            )
            logger.critical(
                "%s: ордер %s принят биржей, но исполнение НЕ подтверждено (%s). "
                "Возможна незарегистрированная позиция. Символ заблокирован для новых входов, "
                "требуется ручная сверка с биржей.",
                symbol, order_link_id, confirmation.detail,
            )
            # pending намеренно НЕ снимаем: неизвестность не должна выглядеть
            # как разрешённая ситуация.
            return confirmation

        # Судьба ордера выяснена — снимаем признак "ждём подтверждения".
        self.risk_manager.clear_entry_pending(symbol)

        if confirmation.status == FillStatus.PARTIALLY_FILLED:
            logger.warning(
                "%s: ордер %s исполнен частично (qty=%.8f) — позиция меньше одобренной Risk Manager",
                symbol, order_link_id, confirmation.filled_qty,
            )
        return confirmation

    @staticmethod
    def _find_open_position(symbol: str, positions: list) -> Optional[dict]:
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("size", 0)) > 0:
                return p
        return None

    def _entry_block_reason(self, symbol: str, positions: list) -> Optional[str]:
        """
        Единый гейт повторного входа. Опрашивает ВСЕ источники, которые могут
        знать, что символ занят, и блокирует вход, если хотя бы один так считает:

        1. живая позиция на бирже;
        2. открытая сделка в журнале (переживает перезапуск, в отличие от памяти);
        3. неподтверждённый ордер, cooldown, лимит сделок по символу, ручная
           блокировка — всё это знает Risk Manager.

        Раньше проверялся только источник (1) — снимок позиций, снятый один раз
        в начале цикла. Любое запаздывание биржи, сбой журнала или перезапуск
        процесса открывали окно для второй позиции по тому же символу.
        """
        if self._find_open_position(symbol, positions) is not None:
            return f"по {symbol} уже есть живая позиция на бирже"

        try:
            open_trades = self.journal.get_open_trades(symbol)
        except Exception:
            # Журнал недоступен -> мы не знаем, есть ли незакрытая сделка.
            # Отказ от входа здесь безопаснее, чем вход вслепую.
            logger.exception(
                "%s: не удалось проверить журнал на открытые сделки — вход заблокирован из осторожности",
                symbol,
            )
            return f"журнал недоступен, состояние {symbol} неизвестно"
        if open_trades:
            return (
                f"журнал считает сделку по {symbol} открытой "
                f"(order_link_id={open_trades[0].get('order_link_id')})"
            )

        return self.risk_manager.symbol_block_reason(symbol)
    
    def _manage_exit(self, symbol: str, position: dict, final_signal: Signal, trend: Optional[str]) -> bool:
        """
        Exit Manager: закрывает позицию только при явном разворотном сигнале.
        Выход по смене EMA trend отключён, чтобы сделка не закрывалась слишком рано.
        """
        side = position.get("side")
        size = float(position.get("size", 0))

        if size <= 0 or side not in ("Buy", "Sell"):
            return False

        position_direction = "long" if side == "Buy" else "short"

        close_reason = None

        if final_signal.action == Action.OPEN_LONG and position_direction == "short":
            close_reason = f"Явный разворотный сигнал против SHORT: {final_signal.reason}"
        elif final_signal.action == Action.OPEN_SHORT and position_direction == "long":
            close_reason = f"Явный разворотный сигнал против LONG: {final_signal.reason}"

        if close_reason is None:
            return False

        if not self.cfg.trading_enabled:
            # Решение зафиксировано в логе, но настоящий reduceOnly-ордер в
            # safe mode не отправляется. Guard в ExecutionEngine — вторая линия
            # защиты.
            logger.info(
                "%s: SAFE MODE: позиция (%s) НЕ закрыта (TRADING_ENABLED=false) — %s",
                symbol, position_direction, close_reason,
            )
            return False

        logger.info("%s: закрываю позицию (%s) -- %s", symbol, position_direction, close_reason)

        try:
            resp = self.execution.close_position(symbol, side, size, source="exit_manager")
        except Exception:
            logger.exception("Не удалось закрыть позицию %s через Exit Manager", symbol)
            return False

        if resp.get("retCode") != 0:
            # Биржа НЕ приняла запрос на закрытие: позиция всё ещё живая.
            logger.warning(
                "%s: закрытие позиции через Exit Manager отклонено биржей retCode=%s retMsg=%s — "
                "позиция остаётся открытой",
                symbol, resp.get("retCode"), resp.get("retMsg"),
            )
            return False

        # Категория причины закрытия (TP/SL/trailing/exit_manager/manual) сюда
        # специально НЕ записывается: она определяется позже, в
        # _sync_closed_trades, по orderLinkId закрывающего ордера (см.
        # _infer_exit_reason) — надёжнее, чем гадать заранее по символу.
        #
        # А вот САМО решение — какой разворотный сигнал вызвал закрытие —
        # больше нигде не появится: closed_pnl/execution list с биржи ничего
        # не знают про наш DecisionEngine. Сохраняем его сразу, в строку
        # конкретной открывающей сделки (по order_link_id, не по символу —
        # неверная атрибуция здесь невозможна в принципе). Потеря снимка
        # ухудшает наблюдаемость, но не должна мешать закрытию.
        self._record_exit_trigger(symbol, final_signal)
        return True

    def _record_exit_trigger(self, symbol: str, final_signal: Signal):
        try:
            open_trades = self.journal.get_open_trades(symbol)
        except Exception:
            logger.exception("%s: не удалось прочитать журнал для записи exit_trigger", symbol)
            return
        if not open_trades:
            logger.warning(
                "%s: Exit Manager закрыл позицию, но в журнале нет открытой сделки — "
                "снимок решения записать некуда", symbol,
            )
            return
        if len(open_trades) > 1:
            logger.warning(
                "%s: в журнале %d открытых сделок одновременно (ожидалась одна) — "
                "снимок решения записываю в самую старую", symbol, len(open_trades),
            )
        order_link_id = min(
            open_trades, key=lambda t: t.get("opened_at_ms") or 0
        )["order_link_id"]

        expected_rr = None
        if final_signal.stop_loss_pct and final_signal.take_profit_pct and final_signal.stop_loss_pct > 0:
            expected_rr = round(final_signal.take_profit_pct / final_signal.stop_loss_pct, 3)

        trigger = {
            "action": final_signal.action.value,
            "source": final_signal.source,
            "confidence": final_signal.confidence,
            "reason": final_signal.reason,
            "expected_rr": expected_rr,
        }
        self.journal.record_exit_trigger(order_link_id, trigger)

    def _apply_trend_filter(self, signal: Signal, trend: Optional[str], context, symbol: str) -> Signal:
        """
        Блокирует сигналы против старшего тренда (EMA50/200). trend=None означает
        "недостаточно данных для расчёта" — в этом случае фильтр НЕ блокирует,
        чтобы не парализовать систему на старте, пока не накопится 200+ свечей.
        trend="neutral" (EMA50/200 переплетены, тренда нет) — тоже не блокирует,
        так как в этом состоянии направление старшего тренда неопределённо.
        """
        if not self.cfg.trend_filter_enabled or trend is None or trend == "neutral":
            return signal
        is_counter_trend = (
            signal.action == Action.OPEN_LONG and trend == "short"
            or signal.action == Action.OPEN_SHORT and trend == "long"
        )
        if not is_counter_trend:
            return signal

        if (
            context.regime == "REVERSAL"
            and signal.confidence >= self.cfg.trend_filter_reversal_confidence
        ):
            logger.info(
                "%s: trend filter разрешил сильный REVERSAL против старшего тренда: signal=%s confidence=%.2f threshold=%.2f",
                symbol, signal.action.value, signal.confidence, self.cfg.trend_filter_reversal_confidence,
            )
            return signal

        if signal.action == Action.OPEN_LONG and trend == "short":
            return Signal(symbol=symbol, action=Action.HOLD, source=signal.source,
                           reason=f"Заблокировано trend filter: сигнал LONG против старшего тренда SHORT "
                                  f"при режиме {context.regime}, confidence={signal.confidence:.2f} "
                                  f"(было: {signal.reason})")
        if signal.action == Action.OPEN_SHORT and trend == "long":
            return Signal(symbol=symbol, action=Action.HOLD, source=signal.source,
                           reason=f"Заблокировано trend filter: сигнал SHORT против старшего тренда LONG "
                                  f"при режиме {context.regime}, confidence={signal.confidence:.2f} "
                                  f"(было: {signal.reason})")
        return signal

    def _build_entry_snapshot(
        self,
        symbol: str,
        final_signal: Signal,
        decision_report,
        market_context,
        market_snapshot: dict,
        candles_df: pd.DataFrame,
        indicators: dict,
        last_price: float,
        risk_check,
        trend_filter: Optional[str],
        meta_decision,
    ) -> dict:
        technical = self._technical_snapshot(candles_df, indicators)
        orderbook = market_snapshot.get("orderbook") or {}
        trade_flow = market_snapshot.get("trade_flow_last_minutes") or {}
        funding_trend = market_snapshot.get("funding_trend") or {}
        oi_trend = market_snapshot.get("open_interest_trend") or {}
        liquidations = market_snapshot.get("liquidations_last_hour") or {}
        return {
            "basic": {
                "symbol": symbol,
                "direction": final_signal.action.value,
                "entry_price": last_price,
                "position_size_usdt": risk_check.approved_size_usdt,
                "leverage": risk_check.approved_leverage,
                "primary_interval": self.cfg.primary_interval,
            },
            "market_context": {
                "trend": market_context.trend,
                "regime": market_context.regime,
                "volatility_state": market_context.volatility,
                "liquidity_state": market_context.liquidity,
                "volume_state": market_context.volume,
                "funding_state": market_context.funding_bias,
                "open_interest_state": market_context.open_interest_trend,
                "context_confidence": market_context.confidence,
                "risk_score": market_context.risk_score,
                "trend_filter": trend_filter,
            },
            "technical": technical,
            "microstructure": {
                "spread_pct": orderbook.get("spread_pct"),
                "orderbook_imbalance": orderbook.get("bid_ask_imbalance"),
                "trade_flow_imbalance": trade_flow.get("imbalance"),
                "funding_rate": market_snapshot.get("funding_rate"),
                "funding_trend": funding_trend.get("trend"),
                "oi_change_pct": oi_trend.get("change_pct"),
                "liquidation_count": liquidations.get("count"),
                "liquidation_volume": liquidations.get("total_volume"),
            },
            "decision": {
                "final_action": final_signal.action.value,
                "decision_confidence": decision_report.confidence,
                "expected_rr": decision_report.expected_rr,
                "risk_score": decision_report.risk_score,
                "confirmation_count": decision_report.confirmation_count,
                "confirmation_families": list(decision_report.confirmation_families),
                "selected_expert_votes": [
                    vote.source for vote in decision_report.votes
                    if not vote.ignored and vote.action == decision_report.winning_action
                ],
                "rejected_scenarios": decision_report.rejected_actions,
                "entry_reason": final_signal.reason,
                "meta_strategy_reasoning": list(meta_decision.notes),
                "ai_analyst_conclusion": decision_report.ai_analysis,
            },
        }

    def _build_exit_snapshot(self, symbol: str, closed_pnl: dict) -> dict:
        try:
            candles_df = self._load_recent_candles(symbol, limit=210)
            if candles_df is None or len(candles_df) < 30:
                return {
                    "symbol": symbol,
                    "closed_pnl": closed_pnl,
                    "market_state_available": False,
                    "reason": "not enough candles for exit market snapshot",
                }
            funding_info = self._load_latest_funding(symbol)
            funding_rate = funding_info["rate"] if funding_info else None
            funding_trend = self._load_funding_trend(symbol, limit=8)
            oi_trend = self._load_oi_trend(symbol, limit=20)
            orderbook = self._load_latest_orderbook(symbol)
            trade_flow = self._load_trade_flow(symbol, minutes=15)
            liquidations = self._load_recent_liquidations(symbol, minutes=60)
            indicators = compute_all_indicators(candles_df)
            trend = trend_direction(candles_df)
            market_snapshot = self._build_market_snapshot(
                symbol, candles_df, funding_rate, funding_trend, oi_trend,
                orderbook, trade_flow, liquidations, indicators,
            )
            market_snapshot["trend_filter"] = trend
            market_context = self.market_context_engine.analyze(symbol, candles_df, market_snapshot)
            return {
                "symbol": symbol,
                "market_state_available": True,
                "trend": market_context.trend,
                "regime": market_context.regime,
                "volatility_state": market_context.volatility,
                "liquidity_state": market_context.liquidity,
                "volume_state": market_context.volume,
                "funding_state": market_context.funding_bias,
                "open_interest_state": market_context.open_interest_trend,
                "context_confidence": market_context.confidence,
                "technical": self._technical_snapshot(candles_df, indicators),
                "microstructure": {
                    "spread_pct": (orderbook or {}).get("spread_pct"),
                    "orderbook_imbalance": (orderbook or {}).get("bid_ask_imbalance"),
                    "trade_flow_imbalance": (trade_flow or {}).get("imbalance"),
                    "funding_rate": funding_rate,
                    "funding_trend": (funding_trend or {}).get("trend"),
                    "oi_change_pct": (oi_trend or {}).get("change_pct"),
                    "liquidation_count": (liquidations or {}).get("count"),
                    "liquidation_volume": (liquidations or {}).get("total_volume"),
                },
                "signal_changes_vs_entry": None,
                "mfe_mae_note": "MFE/MAE not calculated: exact excursion requires candle-path attribution in a later stage",
            }
        except Exception:
            logger.exception("%s: не удалось построить exit snapshot", symbol)
            return {"symbol": symbol, "market_state_available": False, "reason": "snapshot build failed"}

    @staticmethod
    def _technical_snapshot(candles_df: pd.DataFrame, indicators: dict) -> dict:
        closes = candles_df["close"].astype(float)
        volumes = candles_df["volume"].astype(float)
        last_price = float(closes.iloc[-1])
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()

        recent_20 = closes.tail(20)
        recent_40_vol = volumes.tail(min(40, len(volumes)))
        recent_volume_change = None
        if len(recent_40_vol) >= 10:
            recent = float(recent_40_vol.tail(5).mean())
            baseline = float(recent_40_vol.head(max(len(recent_40_vol) - 5, 1)).mean())
            recent_volume_change = (recent / baseline - 1) * 100 if baseline else None

        vwap_deviation = None
        if len(candles_df) >= 20:
            recent = candles_df.tail(20)
            volume = recent["volume"].astype(float)
            total_volume = float(volume.sum())
            if total_volume > 0:
                typical = (
                    recent["high"].astype(float)
                    + recent["low"].astype(float)
                    + recent["close"].astype(float)
                ) / 3
                vwap = float((typical * volume).sum() / total_volume)
                vwap_deviation = (last_price / vwap - 1) * 100 if vwap else None

        def finite(value):
            if value is None:
                return None
            value = float(value)
            return round(value, 6) if math.isfinite(value) else None

        return {
            "rsi": indicators.get("rsi"),
            "ema_fast": finite(ema_fast.iloc[-1]),
            "ema_slow": finite(ema_slow.iloc[-1]),
            "ema_distance_pct": finite((ema_fast.iloc[-1] - ema_slow.iloc[-1]) / last_price * 100 if last_price else None),
            "macd": finite(macd_line.iloc[-1]),
            "macd_signal": finite(macd_signal.iloc[-1]),
            "macd_histogram": indicators.get("macd_histogram"),
            "atr": indicators.get("atr"),
            "atr_pct_of_price": indicators.get("atr_pct_of_price"),
            "vwap_deviation_pct": finite(vwap_deviation),
            "recent_price_change_pct": finite((recent_20.iloc[-1] / recent_20.iloc[0] - 1) * 100 if len(recent_20) > 1 and recent_20.iloc[0] else None),
            "recent_volume_change_pct": finite(recent_volume_change),
        }

    @staticmethod
    def _expert_vote_rows(decision_report) -> list[dict]:
        rows = []
        for vote in decision_report.votes:
            rows.append({
                "source": vote.source,
                "family": DecisionEngine._source_family(vote.source),
                "action": vote.action.value,
                "confidence": vote.confidence,
                "reason": vote.reason,
                "weight": None,
                "contributed_to_final_decision": (
                    not vote.ignored
                    and decision_report.winning_action != Action.HOLD
                    and vote.action == decision_report.winning_action
                ),
            })
        return rows

    @staticmethod
    def _log_decision_summary(symbol: str, decision_report, final_signal: Signal, market_context, trend: Optional[str]):
        rejected = dict(decision_report.rejected_actions)
        if decision_report.final_signal.action != Action.HOLD and final_signal.action == Action.HOLD:
            rejected["trend_filter"] = final_signal.reason
        logger.info(
            "TRADE_CANDIDATE symbol=%s final_action=%s confidence=%.3f expected_rr=%s "
            "confirmation_count=%d confirmation_families=%s regime=%s context_trend=%s trend_filter=%s rejected=%s",
            symbol,
            final_signal.action.value,
            final_signal.confidence,
            decision_report.expected_rr,
            decision_report.confirmation_count,
            ",".join(decision_report.confirmation_families) or "none",
            market_context.regime,
            market_context.trend,
            trend or "unknown",
            rejected or "none",
        )

    @staticmethod
    def _supporting_experts(decision_report) -> str:
        supporters = [
            vote.source
            for vote in decision_report.votes
            if not vote.ignored and vote.action == decision_report.winning_action
        ]
        return ",".join(supporters) if supporters else "none"

    @staticmethod
    def _price_from_pct(last_price: float, action: Action, pct: Optional[float], is_stop: bool) -> Optional[float]:
        if pct is None:
            return None
        multiplier = pct / 100
        if action == Action.OPEN_LONG:
            price = last_price * (1 - multiplier if is_stop else 1 + multiplier)
        elif action == Action.OPEN_SHORT:
            price = last_price * (1 + multiplier if is_stop else 1 - multiplier)
        else:
            return None
        return round(price, 6)

    @staticmethod
    def _position_pnl_pct(trade: dict, exit_price: float) -> float:
        entry_price = float(trade.get("entry_price") or 0)
        if entry_price <= 0:
            return 0.0
        action = trade.get("action")
        if action == Action.OPEN_SHORT.value or action == "open_short":
            return round((entry_price - exit_price) / entry_price * 100, 4)
        return round((exit_price - entry_price) / entry_price * 100, 4)

    @staticmethod
    def _holding_seconds(opened_at_ms: Optional[int]) -> Optional[int]:
        if opened_at_ms is None:
            return None
        return max(0, int((time.time() * 1000 - opened_at_ms) / 1000))

    @staticmethod
    def _infer_exit_reason(closed_pnl: dict, execution_record: Optional[dict] = None) -> str:
        """
        Определяет причину закрытия позиции.

        closed_pnl (/v5/position/closed-pnl) НЕ содержит stopOrderType — только
        orderType ("Market"/"Limit") и execType ("Trade"/"BustTrade"/...),
        которые для любого закрытия позиции одинаковы. Раньше эта функция
        искала подстроки "takeprofit"/"trailing"/"stoploss" именно в этих
        полях и поэтому НИКОГДА не находила совпадения: в реальном 59-сделочном
        прогоне на testnet 53 из 59 (90%) закрытий получили "manual/unknown",
        хотя часть из них точно была по стопу или тейку.

        Реальная причина берётся из execution_record — записи из
        /v5/execution/list, сматченной по orderId закрывающего ордера
        (см. _index_executions_by_order_id):
        - orderLinkId с нашим префиксом ("exit_manag"/"self_check") — сделку
          закрыл наш собственный код, а не биржа-триггер;
        - иначе stopOrderType биржи (TakeProfit/StopLoss/TrailingStop/...).

        Если execution_record не нашли (ошибка API этого цикла, старая запись
        без сматченных executions) — честно возвращаем "manual/unknown",
        вместо того чтобы гадать по полям, которые заведомо ничего не скажут.
        """
        if not execution_record:
            return "manual/unknown"

        link = str(execution_record.get("orderLinkId") or "")
        if link.startswith("exit_manag"):
            return "exit_manager"
        if link.startswith("self_check"):
            return "self_check_manual"

        stop_type = str(execution_record.get("stopOrderType") or "").strip()
        return _STOP_ORDER_TYPE_TO_EXIT_REASON.get(stop_type, "manual/unknown")

    # ------------------------------------------------------------------

    def _load_recent_candles(self, symbol: str, limit: int = 100) -> Optional[pd.DataFrame]:
        session = self.db.get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.interval == self.cfg.primary_interval)
                .order_by(Candle.start_time.desc())
                .limit(limit)
                .all()
            )
            if not rows:
                return None
            data = [{
                "start_time": r.start_time, "open": r.open, "high": r.high,
                "low": r.low, "close": r.close, "volume": r.volume,
            } for r in reversed(rows)]  # разворачиваем в хронологический порядок
            return pd.DataFrame(data)
        finally:
            session.close()

    def _load_latest_funding(self, symbol: str) -> Optional[dict]:
        session = self.db.get_session()
        try:
            row = (
                session.query(FundingRate)
                .filter(FundingRate.symbol == symbol)
                .order_by(FundingRate.funding_ts.desc())
                .first()
            )
            return {"rate": float(row.funding_rate), "ts": int(row.funding_ts)} if row else None
        finally:
            session.close()

    def _load_funding_trend(self, symbol: str, limit: int = 8) -> Optional[dict]:
        """
        Последние N значений funding rate. Тренд важен не меньше, чем текущее
        значение: устойчиво растущий funding говорит о нарастающем перекосе
        рынка в сторону лонгов (и наоборот).
        """
        session = self.db.get_session()
        try:
            rows = (
                session.query(FundingRate)
                .filter(FundingRate.symbol == symbol)
                .order_by(FundingRate.funding_ts.desc())
                .limit(limit)
                .all()
            )
            if not rows:
                return None
            values = [float(r.funding_rate) for r in reversed(rows)]
            return {
                "recent_values": values,
                "trend": "растёт" if values[-1] > values[0] else "падает" if values[-1] < values[0] else "стабилен",
                "latest_ts": int(max(r.funding_ts for r in rows)),
            }
        finally:
            session.close()

    def _load_oi_trend(self, symbol: str, limit: int = 20) -> Optional[dict]:
        """
        Open Interest: растущий OI при растущей цене — сильный тренд (новые деньги
        заходят в лонг). Растущий OI при падающей цене — усиление шортов.
        Падающий OI — закрытие позиций, тренд слабеет.
        """
        session = self.db.get_session()
        try:
            rows = (
                session.query(OpenInterest)
                .filter(OpenInterest.symbol == symbol)
                .order_by(OpenInterest.ts.desc())
                .limit(limit)
                .all()
            )
            if not rows:
                return None
            values = [float(r.open_interest) for r in reversed(rows)]
            change_pct = round((values[-1] / values[0] - 1) * 100, 3) if values[0] else 0.0
            return {"current": values[-1], "change_pct": change_pct, "latest_ts": int(max(r.ts for r in rows))}
        finally:
            session.close()

    def _load_latest_orderbook(self, symbol: str) -> Optional[dict]:
        """Топ стакана — спред и дисбаланс объёма bid/ask (кто сейчас агрессивнее давит)."""
        session = self.db.get_session()
        try:
            row = (
                session.query(OrderbookSnapshot)
                .filter(OrderbookSnapshot.symbol == symbol)
                .order_by(OrderbookSnapshot.ts.desc())
                .first()
            )
            if not row:
                return None
            bid_size = float(row.best_bid_size)
            ask_size = float(row.best_ask_size)
            total = bid_size + ask_size
            imbalance = round((bid_size - ask_size) / total, 3) if total > 0 else 0.0
            spread_pct = round(
                (float(row.best_ask_price) - float(row.best_bid_price)) / float(row.best_bid_price) * 100, 4
            )
            return {
                "ts": int(row.ts),
                "spread_pct": spread_pct,
                # imbalance: >0 значит больше объёма на покупку (bid), <0 — на продажу (ask)
                "bid_ask_imbalance": imbalance,
            }
        finally:
            session.close()

    def _load_trade_flow(self, symbol: str, minutes: int = 15) -> Optional[dict]:
        """
        Соотношение объёма покупок/продаж за последние N минут по реальным сделкам
        (не по стакану, а по факту исполненных сделок) — order flow imbalance.
        """
        session = self.db.get_session()
        try:
            since_ts = int(time.time() * 1000) - minutes * 60_000
            rows = (
                session.query(Trade)
                .filter(Trade.symbol == symbol, Trade.ts >= since_ts)
                .all()
            )
            if not rows:
                return None
            buy_vol = sum(float(r.size) for r in rows if r.side == "Buy")
            sell_vol = sum(float(r.size) for r in rows if r.side == "Sell")
            total = buy_vol + sell_vol
            if total == 0:
                return None
            return {
                "buy_volume": round(buy_vol, 4),
                "sell_volume": round(sell_vol, 4),
                # >0 значит покупки преобладают, <0 — продажи
                "imbalance": round((buy_vol - sell_vol) / total, 3),
                "window_minutes": minutes,
                "latest_ts": int(max(r.ts for r in rows)),
            }
        finally:
            session.close()

    def _load_recent_liquidations(self, symbol: str, minutes: int = 60) -> Optional[dict]:
        """
        Ликвидации — сигнал стресса рынка. Каскад ликвидаций часто предшествует
        развороту (капитуляция) или продолжению движения (шорт/лонг-сквиз).
        """
        session = self.db.get_session()
        try:
            since_ts = int(time.time() * 1000) - minutes * 60_000
            rows = (
                session.query(Liquidation)
                .filter(Liquidation.symbol == symbol, Liquidation.ts >= since_ts)
                .all()
            )
            if not rows:
                return {"count": 0, "total_volume": 0, "window_minutes": minutes}
            total_volume = sum(float(r.size) for r in rows)
            long_liqs = sum(1 for r in rows if r.side == "Sell")  # ликвидация лонга = принудительная продажа
            short_liqs = sum(1 for r in rows if r.side == "Buy")
            return {
                "count": len(rows),
                "total_volume": round(total_volume, 4),
                "long_liquidations": long_liqs,
                "short_liquidations": short_liqs,
                "window_minutes": minutes,
            }
        finally:
            session.close()

    def _build_market_snapshot(
        self, symbol: str, candles_df: pd.DataFrame, funding_rate: Optional[float],
        funding_trend: Optional[dict], oi_trend: Optional[dict],
        orderbook: Optional[dict], trade_flow: Optional[dict], liquidations: Optional[dict],
        indicators: Optional[dict],
    ) -> dict:
        recent_20 = candles_df.tail(20)
        recent_50 = candles_df.tail(min(50, len(candles_df)))
        closes = candles_df["close"].astype(float)
        returns = closes.pct_change().dropna()

        snapshot = {
            "last_price": float(recent_20["close"].iloc[-1]),
            "price_change_pct_last_20_candles": round(
                (float(recent_20["close"].iloc[-1]) / float(recent_20["close"].iloc[0]) - 1) * 100, 3
            ),
            "price_change_pct_last_50_candles": round(
                (float(recent_50["close"].iloc[-1]) / float(recent_50["close"].iloc[0]) - 1) * 100, 3
            ),
            "high_20": float(recent_20["high"].max()),
            "low_20": float(recent_20["low"].min()),
            "avg_volume_20": float(recent_20["volume"].astype(float).mean()),
            "volatility_pct": round(float(returns.tail(20).std() * 100), 4) if len(returns) >= 20 else None,
            "funding_rate": funding_rate,
            "funding_trend": funding_trend,
            "open_interest_trend": oi_trend,
            "orderbook": orderbook,
            "trade_flow_last_minutes": trade_flow,
            "liquidations_last_hour": liquidations,
        }
        if indicators:
            snapshot["indicators"] = indicators
        return snapshot

    def _check_data_freshness(
        self,
        symbol: str,
        candles_df: pd.DataFrame,
        funding_info: Optional[dict],
        oi_trend: Optional[dict],
        orderbook: Optional[dict],
        trade_flow: Optional[dict],
    ) -> dict:
        warnings = []
        critical = False

        last_candle_ts = int(candles_df["start_time"].iloc[-1])
        candle_age_min = self._age_seconds(last_candle_ts) / 60
        if candle_age_min > self.cfg.max_candle_age_minutes:
            critical = True
            warnings.append(
                f"candles stale {candle_age_min:.1f}m > {self.cfg.max_candle_age_minutes}m; проверь main.py/kline WS"
            )

        if orderbook is None:
            warnings.append("orderbook missing; проверь main.py/orderbook WS")
        else:
            orderbook_age = self._age_seconds(orderbook["ts"])
            if orderbook_age > self.cfg.max_orderbook_age_seconds:
                warnings.append(
                    f"orderbook stale {orderbook_age:.0f}s > {self.cfg.max_orderbook_age_seconds}s"
                )

        if trade_flow is None:
            warnings.append("trade flow missing; momentum будет HOLD")
        else:
            trade_flow_age = self._age_seconds(trade_flow["latest_ts"])
            if trade_flow_age > self.cfg.max_trade_flow_age_seconds:
                warnings.append(
                    f"trade flow stale {trade_flow_age:.0f}s > {self.cfg.max_trade_flow_age_seconds}s"
                )

        if funding_info is None:
            warnings.append("funding missing; funding expert ослаблен")
        else:
            funding_age_min = self._age_seconds(funding_info["ts"]) / 60
            if funding_age_min > self.cfg.max_funding_age_minutes:
                warnings.append(
                    f"funding stale {funding_age_min:.1f}m > {self.cfg.max_funding_age_minutes}m"
                )

        if oi_trend is None:
            warnings.append("open interest missing")
        else:
            oi_age_min = self._age_seconds(oi_trend["latest_ts"]) / 60
            if oi_age_min > self.cfg.max_open_interest_age_minutes:
                warnings.append(
                    f"open interest stale {oi_age_min:.1f}m > {self.cfg.max_open_interest_age_minutes}m"
                )

        if warnings:
            logger.info("%s: data freshness warnings: %s", symbol, "; ".join(warnings))
        return {"critical": critical, "warnings": warnings}

    @staticmethod
    def _age_seconds(ts_ms: int) -> float:
        return max(0.0, (time.time() * 1000 - ts_ms) / 1000)
