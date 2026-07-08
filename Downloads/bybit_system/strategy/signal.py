"""
Единый формат сигнала — и rule-based стратегия, и ИИ-стратегия должны
приводить свои выводы к этому виду. Это то, что видит Risk Manager
и Execution Engine, — они не знают и не должны знать, кто именно
сгенерировал сигнал.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Action(str, Enum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE = "close"
    HOLD = "hold"  # явное "ничего не делать" — тоже валидный ответ


@dataclass
class Signal:
    symbol: str
    action: Action
    # Источник сигнала — важно для логов и последующего анализа,
    # какая часть системы принесла прибыль/убыток
    source: str  # "rule:ema_cross" | "ai:claude" и т.д.
    confidence: float = 0.5  # 0..1, насколько стратегия уверена
    reason: str = ""  # human-readable объяснение, обязательно для ИИ-сигналов
    suggested_size_usdt: Optional[float] = None
    suggested_leverage: Optional[int] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence должен быть 0..1, получено {self.confidence}")
