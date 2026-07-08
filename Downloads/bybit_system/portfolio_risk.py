"""
Portfolio Risk Engine.

Проверяет риск на уровне портфеля: несколько одинаково направленных позиций по
сильно коррелированным монетам считаются как один перегруженный сценарий.
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

from strategy.signal import Action, Signal


@dataclass
class PortfolioRiskResult:
    approved: bool
    reason: str


class PortfolioRiskEngine:
    DEFAULT_CORRELATION_GROUPS: Tuple[Tuple[str, ...], ...] = (
        ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        ("BNBUSDT", "ETHUSDT"),
    )

    def __init__(self, max_same_direction_per_group: int = 2):
        self.max_same_direction_per_group = max_same_direction_per_group
        self.symbol_to_group: Dict[str, Tuple[str, ...]] = {}
        for group in self.DEFAULT_CORRELATION_GROUPS:
            for symbol in group:
                self.symbol_to_group[symbol] = group

    def evaluate(self, signal: Signal, open_positions: List[dict]) -> PortfolioRiskResult:
        if signal.action not in (Action.OPEN_LONG, Action.OPEN_SHORT):
            return PortfolioRiskResult(True, "Нет новой портфельной экспозиции")

        group = self.symbol_to_group.get(signal.symbol)
        if not group:
            return PortfolioRiskResult(True, "Для символа нет заданной корреляционной группы")

        desired_side = "Buy" if signal.action == Action.OPEN_LONG else "Sell"
        same_direction = 0
        for position in open_positions:
            if position.get("symbol") not in group:
                continue
            if float(position.get("size", 0) or 0) <= 0:
                continue
            if position.get("side") == desired_side:
                same_direction += 1

        if same_direction >= self.max_same_direction_per_group:
            return PortfolioRiskResult(
                approved=False,
                reason=(
                    f"Корреляционный риск слишком высокий: уже есть {same_direction} "
                    f"позиций той же стороны в группе {', '.join(group)}"
                ),
            )
        return PortfolioRiskResult(True, "Портфельный риск допустим")
