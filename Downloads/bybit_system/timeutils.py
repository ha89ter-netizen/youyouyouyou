"""
Единый формат времени для всего проекта: timezone-aware UTC.

Зачем отдельный модуль: часть datetime приходит из БД (в PostgreSQL timestamptz
возвращается aware, в SQLite tz теряется и значение читается как naive), часть
создаётся кодом. Сравнение naive и aware даёт TypeError, поэтому любое значение
перед сравнением или сортировкой прогоняется через ensure_aware_utc().

Naive-значения трактуются как UTC — исторически весь проект писал время только
в UTC (server_default=func.now() на UTC-сервере, datetime.now(timezone.utc)),
так что это восстанавливает исходный смысл, а не додумывает его.
"""

from datetime import date, datetime, timezone
from typing import Any, Optional

UTC = timezone.utc

# Сортировочный ключ для строк без времени вообще: они уходят в начало списка,
# а не роняют сравнение.
_MIN_SORT_KEY = float("-inf")


def utcnow() -> datetime:
    """Текущее время как timezone-aware UTC. Единственный источник 'сейчас'."""
    return datetime.now(UTC)


def utc_today() -> date:
    """Текущая UTC-дата. Граница торгового дня определяется только по ней."""
    return utcnow().date()


def utc_day_str(value: Optional[date] = None) -> str:
    """UTC-дата как 'YYYY-MM-DD' — формат хранения дня в risk_state."""
    return (value or utc_today()).isoformat()


def parse_utc_day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return ensure_aware_utc(value).date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def ensure_aware_utc(value: Any) -> Optional[datetime]:
    """
    Приводит datetime к aware UTC. Naive считается UTC (см. docstring модуля).
    Не-datetime возвращает None, чтобы вызывающий код мог явно решить, что делать.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def timestamp_sort_key(value: Any) -> float:
    """
    Ключ сортировки по времени, безопасный для смеси naive/aware/None.
    Возвращает float, поэтому сравнение никогда не упадёт на TypeError.
    """
    aware = ensure_aware_utc(value)
    return aware.timestamp() if aware is not None else _MIN_SORT_KEY


def trade_time_sort_key(row: dict) -> float:
    """Сортировка сделок по времени закрытия, с откатом на время открытия."""
    return timestamp_sort_key(row.get("closed_at") or row.get("opened_at"))


def to_epoch_ms(value: Any) -> Optional[int]:
    aware = ensure_aware_utc(value)
    return int(aware.timestamp() * 1000) if aware is not None else None


def from_epoch_ms(value: Any) -> Optional[datetime]:
    """
    Unix-миллисекунды (в т.ч. строкой, как их отдаёт Bybit) -> aware UTC.
    Нужен, чтобы время закрытия сделки бралось с биржи, а не подставлялось
    как "сейчас": сделка могла закрыться вчера, и тогда её PnL не относится
    к сегодняшнему дневному лимиту.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
