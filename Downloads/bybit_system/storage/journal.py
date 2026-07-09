"""
TradeJournal: пишет причину каждого входа и результат каждого выхода
в отдельную таблицу. Цель — чтобы через неделю торговли можно было
посмотреть не только "PnL = -50", а РАЗОБРАТЬСЯ, какие сигналы
(rule/ai/rule+ai) реально приносят прибыль, а какие только шумят.
"""

import logging
from typing import Optional

from storage.db import Database
from storage.models import TradeLog

logger = logging.getLogger(__name__)


class TradeJournal:
    def __init__(self, db: Database):
        self.db = db

    def log_entry(
        self, symbol: str, action, source: str, reason: str,
        entry_price: float, size_usdt: float, leverage: int,
        stop_loss_pct: Optional[float], take_profit_pct: Optional[float],
        order_link_id: str,
    ) -> bool:
        session = self.db.get_session()
        try:
            entry = TradeLog(
                symbol=symbol, action=action.value if hasattr(action, "value") else str(action),
                source=source, reason=reason[:1000], order_link_id=order_link_id,
                entry_price=entry_price, size_usdt=size_usdt, leverage=leverage,
                stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                status="open",
            )
            session.add(entry)
            session.commit()
            logger.info("Журнал: записан вход %s %s (order_link_id=%s)", symbol, action, order_link_id)
            return True
        except Exception:
            logger.exception("Не удалось записать вход в журнал сделок")
            session.rollback()
            return False
        finally:
            session.close()

    def log_exit(self, order_link_id: str, exit_price: float, pnl_usdt: float):
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).first()
            if row is None:
                logger.warning("Журнал: не найдена запись входа для order_link_id=%s", order_link_id)
                return
            row.exit_price = exit_price
            row.pnl_usdt = pnl_usdt
            row.status = "closed"
            from sqlalchemy import func
            row.closed_at = func.now()
            session.commit()
            logger.info("Журнал: записан выход %s pnl=%.2f USDT", order_link_id, pnl_usdt)
        except Exception:
            logger.exception("Не удалось записать выход в журнал сделок")
            session.rollback()
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
                    "opened_at_ms": int(r.opened_at.timestamp() * 1000) if r.opened_at else None,
                }
                for r in rows
            ]
        finally:
            session.close()
