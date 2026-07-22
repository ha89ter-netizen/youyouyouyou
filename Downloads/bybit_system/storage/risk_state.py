"""
RiskStateStore: загрузка и сохранение состояния Risk Manager.

Отдельный слой намеренно: Risk Manager не должен знать про SQLAlchemy, а
хранилище не должно знать про торговые лимиты. Между ними — обычный dict.

Отказ БД здесь НИКОГДА не должен снимать ограничения: save() возвращает False и
пишет ошибку, но состояние в памяти остаётся, а вызывающий код продолжает
работать по нему. Это осознанный компромисс — потерять запись состояния менее
опасно, чем уронить торговый цикл в момент, когда позиция уже открыта.
"""

import logging
from typing import Any, Dict, Optional

from storage.models import RiskState
from timeutils import utcnow

logger = logging.getLogger(__name__)

# Состояние — синглтон: одна строка на всю систему.
RISK_STATE_ROW_ID = 1


def _as_float_map(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        try:
            result[str(key)] = float(item)
        except (TypeError, ValueError):
            logger.warning("risk_state: значение %r для ключа %s не число, пропускаю", item, key)
    return result


def _as_int_map(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        try:
            result[str(key)] = int(item)
        except (TypeError, ValueError):
            logger.warning("risk_state: значение %r для ключа %s не целое, пропускаю", item, key)
    return result


def _as_str_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


class RiskStateStore:
    def __init__(self, db):
        self.db = db

    def load(self) -> Optional[dict]:
        """Возвращает сохранённое состояние или None, если его ещё нет."""
        session = self.db.get_session()
        try:
            row = session.query(RiskState).filter(RiskState.id == RISK_STATE_ROW_ID).first()
            if row is None:
                return None
            return {
                "day_utc": row.day_utc,
                "daily_start_balance": (
                    float(row.daily_start_balance) if row.daily_start_balance is not None else None
                ),
                "daily_pnl_usdt": float(row.daily_pnl_usdt or 0),
                "daily_trade_count": int(row.daily_trade_count or 0),
                "symbol_trade_counts": _as_int_map(row.symbol_trade_counts),
                "last_entry_ts_by_symbol": _as_float_map(row.last_entry_ts_by_symbol),
                "pending_entries": _as_float_map(row.pending_entries),
                "blocked_symbols": _as_str_map(row.blocked_symbols),
                "circuit_breaker_tripped": bool(row.circuit_breaker_tripped),
                "circuit_breaker_reason": row.circuit_breaker_reason or "",
                "circuit_breaker_sticky": bool(row.circuit_breaker_sticky),
                "circuit_breaker_causes": row.circuit_breaker_causes or {},
            }
        except Exception:
            logger.exception(
                "risk_state: не удалось загрузить состояние. Risk Manager стартует с пустым "
                "состоянием — дневные лимиты могут быть занижены до первой успешной записи."
            )
            return None
        finally:
            session.close()

    def save(self, state: dict) -> bool:
        """
        Upsert синглтон-строки. Возвращает False при ошибке — состояние в памяти
        при этом сохраняется, лимиты продолжают действовать.
        """
        session = self.db.get_session()
        try:
            row = session.query(RiskState).filter(RiskState.id == RISK_STATE_ROW_ID).first()
            if row is None:
                row = RiskState(id=RISK_STATE_ROW_ID)
                session.add(row)

            row.day_utc = state["day_utc"]
            row.daily_start_balance = state.get("daily_start_balance")
            row.daily_pnl_usdt = state.get("daily_pnl_usdt", 0.0)
            row.daily_trade_count = state.get("daily_trade_count", 0)
            row.symbol_trade_counts = dict(state.get("symbol_trade_counts") or {})
            row.last_entry_ts_by_symbol = dict(state.get("last_entry_ts_by_symbol") or {})
            row.pending_entries = dict(state.get("pending_entries") or {})
            row.blocked_symbols = dict(state.get("blocked_symbols") or {})
            row.circuit_breaker_tripped = bool(state.get("circuit_breaker_tripped"))
            row.circuit_breaker_reason = (state.get("circuit_breaker_reason") or "")[:500]
            row.circuit_breaker_sticky = bool(state.get("circuit_breaker_sticky"))
            row.circuit_breaker_causes = dict(state.get("circuit_breaker_causes") or {})
            row.updated_at = utcnow()

            session.commit()
            return True
        except Exception:
            logger.exception(
                "risk_state: не удалось сохранить состояние. Лимиты в памяти продолжают "
                "действовать, но перезапуск потеряет изменения с момента последней записи."
            )
            session.rollback()
            return False
        finally:
            session.close()
