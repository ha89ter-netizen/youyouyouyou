"""
TradeJournal: пишет причину каждого входа и результат каждого выхода
в отдельную таблицу. Цель — чтобы через неделю торговли можно было
посмотреть не только "PnL = -50", а РАЗОБРАТЬСЯ, какие сигналы
(rule/ai/rule+ai) реально приносят прибыль, а какие только шумят.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from storage.db import Database
from storage.models import TradeExpertVote, TradeLog
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

logger = logging.getLogger(__name__)


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
    ) -> bool:
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning("Журнал: не найдена запись входа для order_link_id=%s", order_link_id)
                return False
            if row.status == "closed":
                logger.info("Журнал: сделка %s уже закрыта, повторный выход игнорируется", order_link_id)
                return False
            row.exit_price = exit_price
            row.pnl_usdt = pnl_usdt
            row.pnl_pct = pnl_pct_from_notional(pnl_usdt, row.size_usdt)
            row.exit_reason = sanitize_text(exit_reason, 100) or "manual/unknown"
            row.exit_type = exit_type or normalize_exit_type(row.exit_reason)
            row.exit_snapshot = safe_json(exit_snapshot) if exit_snapshot is not None else None
            row.status = "closed"
            closed_at = datetime.now(timezone.utc)
            row.closed_at = closed_at
            row.holding_seconds = validate_time_order(row.opened_at, closed_at)
            session.commit()
            logger.info(
                "Журнал: записан выход %s pnl=%.2f USDT pnl_pct=%s exit_type=%s holding_seconds=%s",
                order_link_id, pnl_usdt, row.pnl_pct, row.exit_type, row.holding_seconds,
            )
            return True
        except Exception:
            logger.exception("Не удалось записать выход в журнал сделок")
            session.rollback()
            return False
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
            rows = query.all()
            return [
                {
                    "order_link_id": r.order_link_id,
                    "symbol": r.symbol,
                    "action": r.action,
                    "entry_price": float(r.entry_price),
                    "size_usdt": float(r.size_usdt),
                    "source": r.source,
                    "opened_at": r.opened_at,
                    "opened_at_ms": int(r.opened_at.timestamp() * 1000) if r.opened_at else None,
                }
                for r in rows
            ]
        finally:
            session.close()
