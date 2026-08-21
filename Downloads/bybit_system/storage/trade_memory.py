import json
import logging
import math
import os
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Optional

from timeutils import ensure_aware_utc

logger = logging.getLogger(__name__)

SENSITIVE_ENV_NAMES = (
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "OPENAI_API_KEY",
    "DATABASE_URL",
)


def safe_float(value: Any, field_name: str = "") -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.debug("Trade memory: numeric field %s is not parseable: %r", field_name, value)
        return None
    if not math.isfinite(number):
        logger.warning("Trade memory: numeric field %s is not finite: %r", field_name, value)
        return None
    return number


def clamp_confidence(value: Any, field_name: str = "confidence") -> Optional[float]:
    number = safe_float(value, field_name)
    if number is None:
        return None
    if number < 0 or number > 1:
        logger.warning("Trade memory: %s outside 0..1: %.4f", field_name, number)
        return max(0.0, min(number, 1.0))
    return number


def non_negative_int(value: Any, field_name: str = "") -> Optional[int]:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.debug("Trade memory: integer field %s is not parseable: %r", field_name, value)
        return None
    if number < 0:
        logger.warning("Trade memory: integer field %s is negative: %r", field_name, value)
        return 0
    return number


def safe_json(data: Any, string_limit: Optional[int] = 2000) -> Any:
    limit = None if string_limit is None else max(1, int(string_limit))
    sanitized = _sanitize(data, string_limit=limit)
    json.dumps(sanitized, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return sanitized


def stable_json_dumps(data: Any) -> str:
    return json.dumps(
        safe_json(data, string_limit=None),
        ensure_ascii=False, sort_keys=True, allow_nan=False,
    )


def sanitize_text(value: Any, limit: Optional[int] = 2000) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    for secret in _secrets():
        text = text.replace(secret, "***")
    return text if limit is None else text[:limit]


def pnl_pct_from_notional(pnl_usdt: Any, size_usdt: Any) -> Optional[float]:
    pnl = safe_float(pnl_usdt, "pnl_usdt")
    size = safe_float(size_usdt, "size_usdt")
    if pnl is None or size is None or size <= 0:
        return None
    return round(pnl / size * 100, 6)


def normalize_exit_type(exit_reason: str) -> str:
    text = (exit_reason or "").lower()
    if "liquid" in text:
        return "liquidation"
    if "trailing" in text:
        return "trailing_stop"
    if "exit manager" in text or "exit_manager" in text:
        return "exit_manager"
    if text in ("tp", "take_profit") or "takeprofit" in text or "take profit" in text:
        return "take_profit"
    if text in ("sl", "stop_loss") or "stoploss" in text or "stop loss" in text:
        return "stop_loss"
    if "manual" in text:
        return "manual"
    return "unknown"


def validate_time_order(opened_at: Optional[datetime], closed_at: Optional[datetime]) -> Optional[int]:
    """
    Длительность удержания в секундах. Оба конца нормализуются к aware UTC:
    opened_at может прийти из SQLite naive, closed_at пишется кодом как aware,
    и прямое вычитание падало бы на TypeError.
    """
    opened = ensure_aware_utc(opened_at)
    closed = ensure_aware_utc(closed_at)
    if opened is None or closed is None:
        return None
    seconds = int((closed - opened).total_seconds())
    if seconds < 0:
        logger.warning("Trade memory: closed_at is earlier than opened_at")
        return 0
    return seconds


def _sanitize(value: Any, string_limit: Optional[int] = 2000) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return safe_float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return sanitize_text(value, string_limit)
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            if _looks_sensitive_key(key_text):
                clean[key_text] = "***"
            else:
                clean[key_text] = _sanitize(item, string_limit)
        return clean
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return [_sanitize(item, string_limit) for item in value]
    return sanitize_text(value, string_limit)


def _looks_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("secret", "api_key", "apikey", "password", "token"))


def _secrets() -> list[str]:
    return [os.getenv(name) for name in SENSITIVE_ENV_NAMES if os.getenv(name)]
