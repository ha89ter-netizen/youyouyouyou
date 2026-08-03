"""
TradeJournal: пишет причину каждого входа и результат каждого выхода
в отдельную таблицу. Цель — чтобы через неделю торговли можно было
посмотреть не только "PnL = -50", а РАЗОБРАТЬСЯ, какие сигналы
(rule/ai/rule+ai) реально приносят прибыль, а какие только шумят.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

from storage.db import Database
from storage.models import TradeClosure, TradeExchangeOrder, TradeExpertVote, TradeLog
from storage.trade_memory import (
    clamp_confidence,
    non_negative_int,
    normalize_exit_type,
    pnl_pct_from_notional,
    safe_float,
    safe_json,
    sanitize_text,
    validate_time_order,
)
from timeutils import ensure_aware_utc, from_epoch_ms, to_epoch_ms, utc_today, utcnow

logger = logging.getLogger(__name__)

# Сколько последних закрытых сделок просматриваем при восстановлении дневного PnL.
# Фильтровать по дню в SQL нельзя переносимо: в SQLite время хранится naive, в
# PostgreSQL — timestamptz, и сравнение с aware-параметром ведёт себя по-разному.
# Берём заведомо избыточное окно и отбираем день уже в Python через ensure_aware_utc.
# При max_daily_trades=50 запас более чем достаточный.
_DAILY_PNL_SCAN_LIMIT = 1000

# Статусы, по которым сделка считается ещё не разобранной и подлежит сверке с биржей.
UNRESOLVED_STATUSES = ("open", "orphaned")


@dataclass
class ExitResult:
    """
    Результат попытки записать выход.

    recorded=True означает: ИМЕННО ЭТОТ вызов перевёл сделку в "closed".
    Только по нему разрешено прибавлять PnL к дневному итогу — это и есть
    граница идемпотентности всей реконсиляции.
    """
    recorded: bool
    recovered_from_orphan: bool = False
    closed_at: Optional[datetime] = None
    symbol: Optional[str] = None
    already_closed: bool = False
    reason: str = ""

    def __bool__(self) -> bool:
        # Сохраняет прежний контракт `if journal.log_exit(...)`.
        return self.recorded


class TradeJournal:
    def __init__(self, db: Database):
        self.db = db

    def log_entry(
        self, symbol: str, action, source: str, reason: str,
        entry_price: float, size_usdt: float, leverage: int,
        stop_loss_pct: Optional[float], take_profit_pct: Optional[float],
        order_link_id: str,
        market_context: Optional[str] = None,
        regime: Optional[str] = None,
        trend: Optional[str] = None,
        decision_confidence: Optional[float] = None,
        expected_rr: Optional[float] = None,
        confirmation_count: Optional[int] = None,
        confirmation_families: Optional[str] = None,
        entry_reason: Optional[str] = None,
        entry_snapshot: Optional[dict] = None,
        expert_votes: Optional[list[dict]] = None,
        run_id: Optional[str] = None,
        exchange_entry_order_id: Optional[str] = None,
        entry_requested_qty: Optional[float] = None,
        entry_filled_qty: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        entry_fee_usdt: Optional[float] = None,
    ) -> bool:
        if not order_link_id:
            logger.error("Журнал: отказ записи входа %s без order_link_id", symbol)
            return False
        session = self.db.get_session()
        try:
            existing = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if existing is not None:
                logger.info("Журнал: вход %s уже существует, повторная запись игнорируется", order_link_id)
                return False

            entry = TradeLog(
                symbol=symbol, action=action.value if hasattr(action, "value") else str(action),
                source=source, reason=sanitize_text(reason, 1000) or "", order_link_id=order_link_id,
                entry_reason=sanitize_text(entry_reason or reason, 2000),
                market_context=sanitize_text(market_context, 2000),
                regime=regime,
                trend=trend,
                decision_confidence=clamp_confidence(decision_confidence, "decision_confidence"),
                expected_rr=safe_float(expected_rr, "expected_rr"),
                confirmation_count=non_negative_int(confirmation_count, "confirmation_count"),
                confirmation_families=sanitize_text(confirmation_families, 500),
                entry_snapshot=safe_json(entry_snapshot) if entry_snapshot is not None else None,
                entry_price=entry_price, size_usdt=size_usdt, leverage=leverage,
                stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                stop_loss_price=safe_float(stop_loss_price, "stop_loss_price"),
                take_profit_price=safe_float(take_profit_price, "take_profit_price"),
                entry_fee_usdt=safe_float(entry_fee_usdt, "entry_fee_usdt"),
                run_id=sanitize_text(run_id, 100),
                exchange_entry_order_id=sanitize_text(exchange_entry_order_id, 100),
                entry_requested_qty=safe_float(entry_requested_qty, "entry_requested_qty"),
                entry_filled_qty=safe_float(entry_filled_qty, "entry_filled_qty"),
                status="open",
            )
            session.add(entry)
            session.flush()
            for vote in expert_votes or []:
                source_name = sanitize_text(vote.get("source"), 80)
                if not source_name:
                    continue
                session.add(TradeExpertVote(
                    trade_log_id=entry.id,
                    order_link_id=order_link_id,
                    symbol=symbol,
                    source=source_name,
                    family=sanitize_text(vote.get("family"), 50),
                    action=sanitize_text(vote.get("action"), 20) or "unknown",
                    confidence=clamp_confidence(vote.get("confidence"), f"{source_name}.confidence"),
                    reason=sanitize_text(vote.get("reason"), 2000),
                    weight=safe_float(vote.get("weight"), f"{source_name}.weight"),
                    contributed_to_final_decision=bool(vote.get("contributed_to_final_decision")),
                ))
            session.commit()
            logger.info("Журнал: записан вход %s %s (order_link_id=%s)", symbol, action, order_link_id)
            return True
        except Exception:
            logger.exception("Не удалось записать вход в журнал сделок")
            session.rollback()
            return False
        finally:
            session.close()

    def log_exit(
        self,
        order_link_id: str,
        exit_price: float,
        pnl_usdt: float,
        exit_reason: str = "manual/unknown",
        exit_type: Optional[str] = None,
        exit_snapshot: Optional[dict] = None,
        closed_at: Optional[datetime] = None,
        exchange_exit_order_id: Optional[str] = None,
        exit_fee_usdt: Optional[float] = None,
        total_fee_usdt: Optional[float] = None,
        closure_records: Optional[list[dict]] = None,
        closure_executions: Optional[dict[str, list[dict]]] = None,
    ) -> ExitResult:
        """
        Фиксирует выход. Работает и для status="open" (обычное закрытие), и для
        status="orphaned" (восстановление найденного позже результата).

        ИДЕМПОТЕНТНОСТЬ: единственный источник истины — переход status -> "closed",
        выполняемый под тем же commit, что и запись PnL. Уже закрытая сделка
        возвращает recorded=False, и вызывающий код по этому признаку понимает,
        что PnL учитывать в дневном итоге НЕ нужно. Именно этот возврат защищает
        от двойного счёта при повторной реконсиляции.

        closed_at: реальное время закрытия с биржи. Если не передано — utcnow().
        Подставлять "сейчас" для сделки, закрывшейся вчера, нельзя: это исказит
        и holding_seconds, и принадлежность PnL к торговому дню.
        """
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning("Журнал: не найдена запись входа для order_link_id=%s", order_link_id)
                return ExitResult(recorded=False, reason="запись входа не найдена")
            if row.status == "closed":
                logger.info("Журнал: сделка %s уже закрыта, повторный выход игнорируется", order_link_id)
                return ExitResult(recorded=False, reason="уже закрыта", already_closed=True)

            closures = []
            for record in closure_records or []:
                exchange_oid = sanitize_text(record.get("orderId"), 100)
                if not exchange_oid:
                    session.rollback()
                    return ExitResult(recorded=False, reason="closed-PnL без exchange orderId")
                existing_closure = session.query(TradeClosure).filter(
                    TradeClosure.exchange_exit_order_id == exchange_oid
                ).first()
                if existing_closure is not None:
                    if existing_closure.trade_log_id != row.id:
                        session.rollback()
                        logger.critical(
                            "RECONCILIATION_CONFLICT exchange order %s уже принадлежит trade_log.id=%s, "
                            "не присваиваю trade_log.id=%s",
                            exchange_oid, existing_closure.trade_log_id, row.id,
                        )
                        return ExitResult(recorded=False, reason="exchange order уже связан с другой сделкой")
                    closures.append(existing_closure)
                    continue
                record_closed_at = from_epoch_ms(
                    record.get("updatedTime") or record.get("createdTime")
                ) or ensure_aware_utc(closed_at) or utcnow()
                closure = TradeClosure(
                    trade_log_id=row.id,
                    order_link_id=row.order_link_id,
                    exchange_exit_order_id=exchange_oid,
                    closed_qty=safe_float(
                        record.get("closedSize") or record.get("qty"), "closed_qty"
                    ),
                    avg_exit_price=safe_float(record.get("avgExitPrice"), "avg_exit_price"),
                    closed_pnl=safe_float(record.get("closedPnl"), "closed_pnl"),
                    open_fee=safe_float(record.get("openFee"), "open_fee"),
                    close_fee=safe_float(record.get("closeFee"), "close_fee"),
                    exit_reason=sanitize_text(exit_reason, 100),
                    closed_at=record_closed_at,
                    raw_record=safe_json(record),
                    executions=safe_json((closure_executions or {}).get(exchange_oid, [])),
                )
                session.add(closure)
                closures.append(closure)
            if closures:
                session.flush()
                closed_qty_total = sum(float(c.closed_qty) for c in closures)
                if closed_qty_total <= 0:
                    session.rollback()
                    return ExitResult(recorded=False, reason="суммарный closed qty равен нулю")
                exit_price = sum(
                    float(c.avg_exit_price) * float(c.closed_qty) for c in closures
                ) / closed_qty_total
                pnl_usdt = sum(float(c.closed_pnl) for c in closures)
                open_fees = [float(c.open_fee) for c in closures if c.open_fee is not None]
                close_fees = [float(c.close_fee) for c in closures if c.close_fee is not None]
                exit_fee_usdt = sum(close_fees) if close_fees else None
                total_fee_usdt = (
                    sum(open_fees) + sum(close_fees)
                    if len(open_fees) == len(closures) and len(close_fees) == len(closures)
                    else None
                )
                closed_at = max(c.closed_at for c in closures)
                exchange_exit_order_id = closures[0].exchange_exit_order_id
                row.exchange_exit_order_ids = [c.exchange_exit_order_id for c in closures]

            recovered_from_orphan = row.status == "orphaned"
            row.exit_price = exit_price
            row.pnl_usdt = pnl_usdt
            row.pnl_pct = pnl_pct_from_notional(pnl_usdt, row.size_usdt)
            row.exit_reason = sanitize_text(exit_reason, 100) or "manual/unknown"
            row.exit_type = exit_type or normalize_exit_type(row.exit_reason)
            row.exit_snapshot = safe_json(exit_snapshot) if exit_snapshot is not None else None
            row.exchange_exit_order_id = sanitize_text(exchange_exit_order_id, 100)
            row.exit_fee_usdt = safe_float(exit_fee_usdt, "exit_fee_usdt")
            row.total_fee_usdt = safe_float(total_fee_usdt, "total_fee_usdt")
            row.status = "closed"
            real_closed_at = ensure_aware_utc(closed_at) or utcnow()
            row.closed_at = real_closed_at
            row.holding_seconds = validate_time_order(row.opened_at, real_closed_at)
            session.commit()

            if recovered_from_orphan:
                logger.warning(
                    "ORPHAN_RECOVERED: для сделки %s (%s) найден реальный closed PnL=%.4f USDT, "
                    "закрытие %s. Статус orphaned снят, результат восстановлен, "
                    "сделка снова участвует в статистике.",
                    order_link_id, row.symbol, pnl_usdt, real_closed_at.isoformat(),
                )
            else:
                logger.info(
                    "Журнал: записан выход %s pnl=%.2f USDT pnl_pct=%s exit_type=%s holding_seconds=%s",
                    order_link_id, pnl_usdt, row.pnl_pct, row.exit_type, row.holding_seconds,
                )
            return ExitResult(
                recorded=True,
                recovered_from_orphan=recovered_from_orphan,
                closed_at=real_closed_at,
                symbol=row.symbol,
            )
        except Exception:
            logger.exception("Не удалось записать выход в журнал сделок")
            session.rollback()
            return ExitResult(recorded=False, reason="ошибка записи")
        finally:
            session.close()

    def get_open_trades(self, symbol: Optional[str] = None) -> list:
        """
        Возвращает открытые (ещё не закрытые в журнале) сделки со всеми полями,
        нужными для сверки с биржей: order_link_id, entry_price, action, opened_at.

        ВАЖНО: сверка с get_closed_pnl идёт НЕ по order_link_id — в реальном
        ответе Bybit для сделок, закрытых по стоп-лоссу/тейк-профиту/trailing
        stop, поле orderLinkId отсутствует вовсе (закрывающий ордер создаётся
        биржей автоматически, без нашего order_link_id). Матчим по символу +
        цене входа + времени — это надёжно, поскольку Risk Manager не даёт
        открыть вторую позицию по тому же символу, пока не закрыта текущая.
        """
        session = self.db.get_session()
        try:
            query = session.query(TradeLog).filter(TradeLog.status == "open")
            if symbol:
                query = query.filter(TradeLog.symbol == symbol)
            return [self._trade_row_to_dict(r) for r in query.all()]
        finally:
            session.close()

    def get_unresolved_trades(self, symbol: Optional[str] = None) -> List[dict]:
        """
        Сделки, по которым финансовый результат ещё НЕ известен: status "open"
        или "orphaned".

        Отдельно от get_open_trades(), потому что у них разные задачи:
        - get_open_trades() отвечает на "занят ли символ" (orphaned не занимает:
          живой позиции по нему заведомо нет);
        - get_unresolved_trades() отвечает на "что ещё надо сверить с биржей"
          (orphaned сверять обязательно — иначе он навсегда выпадет из учёта).
        """
        session = self.db.get_session()
        try:
            query = session.query(TradeLog).filter(TradeLog.status.in_(UNRESOLVED_STATUSES))
            if symbol:
                query = query.filter(TradeLog.symbol == symbol)
            return [self._trade_row_to_dict(r) for r in query.all()]
        finally:
            session.close()

    def get_orphaned_trades(self, symbol: Optional[str] = None) -> List[dict]:
        session = self.db.get_session()
        try:
            query = session.query(TradeLog).filter(TradeLog.status == "orphaned")
            if symbol:
                query = query.filter(TradeLog.symbol == symbol)
            return [self._trade_row_to_dict(r) for r in query.all()]
        finally:
            session.close()

    def get_recent_trades_missing_exchange_order_evidence(
        self,
        symbol: str,
        current_run_id: Optional[str] = None,
        lookback_hours: int = 24,
    ) -> List[dict]:
        """Return recent/current trades whose durable order linkage is incomplete.

        A transient empty Bybit order-history response must not make exchange
        evidence permanently unavailable after a trade changes to ``closed``.
        The caller can safely retry these rows: the evidence writer is keyed by
        the immutable exchange order ID.
        """
        session = self.db.get_session()
        try:
            rows = (
                session.query(TradeLog)
                .filter(TradeLog.symbol == symbol)
                .order_by(TradeLog.opened_at.desc())
                .limit(100)
                .all()
            )
            cutoff = utcnow() - timedelta(hours=max(1, lookback_hours))
            result = []
            for row in rows:
                opened_at = ensure_aware_utc(row.opened_at)
                if row.run_id != current_run_id and (opened_at is None or opened_at < cutoff):
                    continue

                evidence = session.query(TradeExchangeOrder).filter_by(trade_log_id=row.id).all()
                observed_ids = {item.exchange_order_id for item in evidence}
                observed_roles = {item.role for item in evidence}
                closure_ids = {
                    item.exchange_exit_order_id
                    for item in session.query(TradeClosure).filter_by(trade_log_id=row.id).all()
                }
                needs_entry = bool(row.exchange_entry_order_id) and (
                    row.exchange_entry_order_id not in observed_ids or "entry" not in observed_roles
                )
                needs_protection = bool(row.stop_loss_price or row.take_profit_price) and not (
                    {"protective", "protective_exit"} & observed_roles
                )
                needs_exit = bool(closure_ids - observed_ids)
                if not (needs_entry or needs_protection or needs_exit):
                    continue

                item = self._trade_row_to_dict(row)
                item["known_exchange_exit_order_ids"] = sorted(closure_ids)
                result.append(item)
            return result
        finally:
            session.close()

    @staticmethod
    def _trade_row_to_dict(r: TradeLog) -> dict:
        return {
            "order_link_id": r.order_link_id,
            "run_id": r.run_id,
            "symbol": r.symbol,
            "action": r.action,
            "entry_price": float(r.entry_price),
            "size_usdt": float(r.size_usdt),
            "stop_loss_price": float(r.stop_loss_price) if r.stop_loss_price is not None else None,
            "take_profit_price": (
                float(r.take_profit_price) if r.take_profit_price is not None else None
            ),
            "range_tightened_at": ensure_aware_utc(r.range_tightened_at),
            "tightened_stop_loss_price": (
                float(r.tightened_stop_loss_price)
                if r.tightened_stop_loss_price is not None else None
            ),
            "tightened_take_profit_price": (
                float(r.tightened_take_profit_price)
                if r.tightened_take_profit_price is not None else None
            ),
            "range_second_tightened_at": ensure_aware_utc(r.range_second_tightened_at),
            "second_tightened_stop_loss_price": (
                float(r.second_tightened_stop_loss_price)
                if r.second_tightened_stop_loss_price is not None else None
            ),
            "second_tightened_take_profit_price": (
                float(r.second_tightened_take_profit_price)
                if r.second_tightened_take_profit_price is not None else None
            ),
            "source": r.source,
            "status": r.status,
            "opened_at": ensure_aware_utc(r.opened_at),
            # to_epoch_ms трактует naive как UTC — иначе сделки, записанные
            # до перехода на aware-время, дали бы смещение на часовой пояс
            # и сломали сверку с closed PnL по времени.
            "opened_at_ms": to_epoch_ms(r.opened_at),
            "entry_filled_qty": (
                float(r.entry_filled_qty) if r.entry_filled_qty is not None else None
            ),
            "exchange_entry_order_id": r.exchange_entry_order_id,
            "submitted_exit_order_id": r.submitted_exit_order_id,
            "submitted_exit_order_link_id": r.submitted_exit_order_link_id,
            "known_exchange_exit_order_ids": list(r.exchange_exit_order_ids or []),
        }

    def record_submitted_exit_order(
        self, order_link_id: str, exchange_order_id: Optional[str], exchange_order_link_id: Optional[str]
    ) -> bool:
        """Persist an accepted Exit Manager order before asynchronous reconciliation."""
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(
                TradeLog.order_link_id == order_link_id,
                TradeLog.status.in_(UNRESOLVED_STATUSES),
            ).first()
            if row is None:
                return False
            row.submitted_exit_order_id = sanitize_text(exchange_order_id, 100)
            row.submitted_exit_order_link_id = sanitize_text(exchange_order_link_id, 100)
            session.commit()
            return True
        except Exception:
            logger.exception("Не удалось сохранить идентификаторы exit-ордера для %s", order_link_id)
            session.rollback()
            return False
        finally:
            session.close()

    def upsert_exchange_order_evidence(self, order_link_id: str, evidence: list[dict]) -> None:
        """Idempotently preserve exchange order/protection lifecycle records."""
        session = self.db.get_session()
        try:
            trade = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if trade is None:
                return
            now = utcnow()
            for item in evidence:
                order = item.get("order") or {}
                exchange_oid = sanitize_text(order.get("orderId"), 100)
                if not exchange_oid:
                    continue
                row = session.query(TradeExchangeOrder).filter(
                    TradeExchangeOrder.exchange_order_id == exchange_oid
                ).first()
                if row is not None and row.trade_log_id != trade.id:
                    logger.critical(
                        "ORDER_LINK_CONFLICT exchange order %s уже связан с trade_log.id=%s",
                        exchange_oid, row.trade_log_id,
                    )
                    continue
                if row is None:
                    row = TradeExchangeOrder(
                        trade_log_id=trade.id,
                        internal_order_link_id=order_link_id,
                        exchange_order_id=exchange_oid,
                        role=sanitize_text(item.get("role"), 30) or "unknown",
                        first_observed_at=now,
                    )
                    session.add(row)
                row.role = sanitize_text(item.get("role"), 30) or row.role
                row.exchange_order_link_id = sanitize_text(order.get("orderLinkId"), 100)
                row.parent_order_link_id = sanitize_text(order.get("parentOrderLinkId"), 100)
                row.order_status = sanitize_text(order.get("orderStatus"), 40)
                row.stop_order_type = sanitize_text(order.get("stopOrderType"), 40)
                row.trigger_price = safe_float(order.get("triggerPrice"), "trigger_price")
                row.requested_qty = safe_float(order.get("qty"), "requested_qty")
                row.filled_qty = safe_float(order.get("cumExecQty"), "filled_qty")
                row.avg_price = safe_float(order.get("avgPrice"), "avg_price")
                row.exchange_created_at = from_epoch_ms(order.get("createdTime"))
                row.exchange_updated_at = from_epoch_ms(order.get("updatedTime"))
                row.last_observed_at = now
                row.raw_payload = safe_json(order)
            session.commit()
        except Exception:
            logger.exception("Не удалось сохранить exchange-order evidence для %s", order_link_id)
            session.rollback()
        finally:
            session.close()

    def mark_range_tightened(
        self,
        order_link_id: str,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> bool:
        """Идемпотентно фиксирует однократное сужение защиты открытой сделки."""
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(
                TradeLog.order_link_id == order_link_id,
                TradeLog.status == "open",
            ).first()
            if row is None:
                logger.warning(
                    "Журнал: открытая сделка %s не найдена для фиксации сужения",
                    order_link_id,
                )
                return False
            if row.range_tightened_at is not None:
                return False
            row.range_tightened_at = utcnow()
            row.tightened_stop_loss_price = safe_float(
                stop_loss_price, "tightened_stop_loss_price"
            )
            row.tightened_take_profit_price = safe_float(
                take_profit_price, "tightened_take_profit_price"
            )
            session.commit()
            logger.info(
                "Журнал: сужение защиты %s сохранено (SL=%s TP=%s)",
                order_link_id, stop_loss_price, take_profit_price,
            )
            return True
        except Exception:
            logger.exception("Не удалось сохранить сужение защиты для %s", order_link_id)
            session.rollback()
            return False
        finally:
            session.close()

    def mark_range_second_tightened(
        self,
        order_link_id: str,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> bool:
        """Идемпотентно фиксирует второй этап сужения защиты."""
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(
                TradeLog.order_link_id == order_link_id,
                TradeLog.status == "open",
            ).first()
            if row is None:
                logger.warning(
                    "Журнал: открытая сделка %s не найдена для второго сужения",
                    order_link_id,
                )
                return False
            if row.range_tightened_at is None or row.range_second_tightened_at is not None:
                return False
            row.range_second_tightened_at = utcnow()
            row.second_tightened_stop_loss_price = safe_float(
                stop_loss_price, "second_tightened_stop_loss_price"
            )
            row.second_tightened_take_profit_price = safe_float(
                take_profit_price, "second_tightened_take_profit_price"
            )
            session.commit()
            logger.info(
                "Журнал: второй этап сужения защиты %s сохранён (SL=%s TP=%s)",
                order_link_id, stop_loss_price, take_profit_price,
            )
            return True
        except Exception:
            logger.exception("Не удалось сохранить второй этап сужения для %s", order_link_id)
            session.rollback()
            return False
        finally:
            session.close()

    def record_exit_trigger(self, order_link_id: str, trigger: dict) -> bool:
        """
        Сохраняет снимок решения, из-за которого инициировано закрытие сделки
        (обычно — Exit Manager: разворотный сигнал прошёл тот же барьер
        комитета, что и вход). Вызывать СРАЗУ после того, как биржа приняла
        close-ордер, а не задним числом при сверке.

        Ключ — order_link_id ОТКРЫВАЮЩЕЙ сделки, а не символ. Это осознанно:
        symbol-keyed кэш (_pending_exit_reasons) раньше был источником риска
        неверной атрибуции, если по одному символу реконсилируется несколько
        записей за цикл. Запись прямо в строку конкретной сделки этого риска
        не несёт — перепутать её с чужой строкой невозможно.

        Не блокирует и не участвует в идемпотентности закрытия: это
        вспомогательный снимок для последующего анализа, а не источник PnL.
        Тихо игнорирует отсутствующую или уже закрытую запись — потеря этого
        снимка ухудшает наблюдаемость, но не должна прерывать закрытие позиции.
        """
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning(
                    "Журнал: не найдена запись %s для exit_trigger — снимок решения потерян",
                    order_link_id,
                )
                return False
            row.exit_trigger = safe_json(trigger)
            session.commit()
            return True
        except Exception:
            logger.exception("Не удалось сохранить exit_trigger для %s", order_link_id)
            session.rollback()
            return False
        finally:
            session.close()

    def mark_orphaned(self, order_link_id: str, reason: str) -> bool:
        """
        Помечает сделку как orphaned: журнал считал её открытой, но ни живой
        позиции, ни закрытого PnL на бирже найти не удалось.

        Это НЕ закрытие: pnl_usdt остаётся пустым, потому что финансовый
        результат действительно неизвестен. Записать сюда 0 было бы враньём,
        которое обнулило бы дневной убыток.
        """
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning("Журнал: не найдена запись для orphan-пометки order_link_id=%s", order_link_id)
                return False
            if row.status != "open":
                logger.info(
                    "Журнал: сделка %s уже в статусе %s, orphan-пометка не нужна",
                    order_link_id, row.status,
                )
                return False
            row.status = "orphaned"
            row.exit_reason = sanitize_text(reason, 100) or "orphaned"
            row.exit_type = "orphaned"
            row.closed_at = utcnow()
            session.commit()
            logger.critical(
                "Журнал: сделка %s (%s) помечена ORPHANED — финансовый результат неизвестен: %s",
                order_link_id, row.symbol, reason,
            )
            return True
        except Exception:
            logger.exception("Не удалось пометить сделку %s как orphaned", order_link_id)
            session.rollback()
            return False
        finally:
            session.close()

    def mark_not_filled(self, order_link_id: str, reason: str) -> bool:
        """
        Ордер был принят биржей, но НИКОГДА не исполнился: позиции не было,
        финансового результата не существует.

        Это не "закрытие с PnL=0", а признание, что сделки не было вовсе.
        pnl_usdt остаётся NULL — ноль попал бы в статистику как безубыточная
        сделка и исказил бы win rate и expectancy.

        Идемпотентно: применимо только к сделке в статусе orphaned/open.
        """
        return self._set_terminal_status(
            order_link_id,
            new_status="not_filled",
            allowed_from=UNRESOLVED_STATUSES,
            exit_type="not_filled",
            reason=reason,
            log_prefix="NOT_FILLED",
        )

    def reopen_orphaned(self, order_link_id: str, reason: str) -> bool:
        """
        Возврат orphaned-сделки в "open": на бирже нашлась живая позиция, то
        есть сдались мы преждевременно, а сделка всё это время была настоящей.

        Идемпотентно: применимо только к orphaned.
        """
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning("Журнал: не найдена запись %s для возврата в open", order_link_id)
                return False
            if row.status != "orphaned":
                logger.info(
                    "Журнал: сделка %s в статусе %s — возврат в open не требуется",
                    order_link_id, row.status,
                )
                return False
            row.status = "open"
            row.exit_type = None
            row.exit_reason = None
            row.closed_at = None
            session.commit()
            logger.warning(
                "ORPHAN_REOPENED: сделка %s (%s) возвращена в open — на бирже есть живая позиция: %s",
                order_link_id, row.symbol, reason,
            )
            return True
        except Exception:
            logger.exception("Не удалось вернуть сделку %s в open", order_link_id)
            session.rollback()
            return False
        finally:
            session.close()

    def _set_terminal_status(
        self,
        order_link_id: str,
        new_status: str,
        allowed_from: Tuple[str, ...],
        exit_type: str,
        reason: str,
        log_prefix: str,
    ) -> bool:
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning("Журнал: не найдена запись %s для перевода в %s", order_link_id, new_status)
                return False
            if row.status not in allowed_from:
                logger.info(
                    "Журнал: сделка %s уже в статусе %s — перевод в %s не требуется",
                    order_link_id, row.status, new_status,
                )
                return False
            row.status = new_status
            row.exit_type = exit_type
            row.exit_reason = sanitize_text(reason, 100) or new_status
            row.closed_at = utcnow()
            session.commit()
            logger.warning(
                "%s: сделка %s (%s) переведена в статус %s: %s",
                log_prefix, order_link_id, row.symbol, new_status, reason,
            )
            return True
        except Exception:
            logger.exception("Не удалось перевести сделку %s в %s", order_link_id, new_status)
            session.rollback()
            return False
        finally:
            session.close()

    def sum_closed_pnl_for_utc_day(self, day: Optional[date] = None) -> Tuple[float, int]:
        """
        Сумма реализованного PnL по закрытым сделкам за UTC-день.
        Источник правды для сверки состояния Risk Manager при старте.

        Возвращает (сумма USDT, количество закрытых сделок).
        Orphaned-сделки не учитываются: их результат неизвестен, и подмешивать
        их как 0 означало бы занизить реальный убыток.
        """
        target_day = day or utc_today()
        session = self.db.get_session()
        try:
            rows = (
                session.query(TradeLog)
                .filter(TradeLog.status == "closed", TradeLog.closed_at.isnot(None))
                .order_by(TradeLog.id.desc())
                .limit(_DAILY_PNL_SCAN_LIMIT)
                .all()
            )
            total = 0.0
            count = 0
            for row in rows:
                closed_at = ensure_aware_utc(row.closed_at)
                if closed_at is None or closed_at.date() != target_day:
                    continue
                pnl = safe_float(row.pnl_usdt, "pnl_usdt")
                if pnl is None:
                    logger.warning(
                        "Журнал: закрытая сделка %s за %s без pnl_usdt — не учитываю в дневной сумме",
                        row.order_link_id, target_day,
                    )
                    continue
                total += pnl
                count += 1
            return round(total, 8), count
        except Exception:
            logger.exception("Не удалось посчитать дневной PnL по журналу за %s", target_day)
            return 0.0, 0
        finally:
            session.close()

    def count_orphaned(self) -> int:
        session = self.db.get_session()
        try:
            return session.query(TradeLog).filter(TradeLog.status == "orphaned").count()
        except Exception:
            logger.exception("Не удалось посчитать orphaned-сделки")
            return 0
        finally:
            session.close()
