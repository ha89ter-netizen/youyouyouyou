"""
Replay Engine.

Проигрывает исторический рынок как онлайн-поток. На каждой свече внешний
pipeline может выполнить полный цикл:
Market Context -> Meta Strategy -> Experts -> Decision Engine -> Risk -> Execution.
"""

from dataclasses import dataclass
from typing import Callable, Iterable, Any, List


@dataclass
class ReplayEvent:
    index: int
    candle: Any
    result: Any = None


class ReplayEngine:
    def __init__(self, candles: Iterable[Any]):
        self.candles = list(candles)

    def run(self, on_candle: Callable[[Any, List[Any]], Any], min_history: int = 30) -> List[ReplayEvent]:
        events: List[ReplayEvent] = []
        for index in range(min_history, len(self.candles)):
            history = self.candles[:index]
            candle = self.candles[index]
            result = on_candle(candle, history)
            events.append(ReplayEvent(index=index, candle=candle, result=result))
        return events
