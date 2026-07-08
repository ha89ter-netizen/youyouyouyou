"""
Meta Strategy Manager.

Этот слой не ищет сделки. Он решает, какие эксперты имеют право голосовать
в текущем рыночном контексте, и насколько нужно уменьшить размер позиции.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set

from market_context import MarketContext


@dataclass
class StrategyPermission:
    strategy_name: str
    allowed: bool
    reason: str


@dataclass
class MetaStrategyDecision:
    allowed_sources: Set[str]
    position_size_multiplier: float = 1.0
    permissions: Dict[str, StrategyPermission] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def is_allowed(self, source: str) -> bool:
        if source in self.allowed_sources:
            return True
        prefix = source.split(":", 1)[0] + ":"
        return any(source.startswith(allowed) or allowed.startswith(prefix) for allowed in self.allowed_sources)


class MetaStrategyManager:
    """
    Политики можно расширять без изменения самих стратегий. Это важно:
    стратегии остаются независимыми экспертами, а режим рынка решает,
    кто вообще получает право голосовать.
    """

    ALL_EXPERTS = {
        "expert:ema",
        "expert:rsi",
        "expert:vwap",
        "expert:momentum",
        "expert:orderbook",
        "expert:funding",
        "rule:committee",
    }

    TREND_EXPERTS = {"expert:ema", "expert:vwap", "expert:momentum", "expert:orderbook", "rule:committee"}
    RANGE_EXPERTS = {"expert:rsi", "expert:vwap", "expert:funding", "rule:committee"}
    BREAKOUT_EXPERTS = {"expert:ema", "expert:momentum", "expert:orderbook", "expert:vwap", "rule:committee"}
    REVERSAL_EXPERTS = {"expert:rsi", "expert:funding", "expert:orderbook", "rule:committee"}

    def evaluate(self, context: MarketContext) -> MetaStrategyDecision:
        allowed = set(self.ALL_EXPERTS)
        notes: List[str] = []

        if context.regime == "TREND":
            allowed = set(self.TREND_EXPERTS)
            notes.append("TREND: разрешены EMA, VWAP, Momentum, OrderBook и rule committee")
        elif context.regime == "RANGE":
            allowed = set(self.RANGE_EXPERTS)
            notes.append("RANGE: приоритет RSI, VWAP, Funding и mean-reversion логики")
        elif context.regime == "BREAKOUT":
            allowed = set(self.BREAKOUT_EXPERTS)
            notes.append("BREAKOUT: разрешены momentum/order-flow эксперты")
        elif context.regime == "REVERSAL":
            allowed = set(self.REVERSAL_EXPERTS)
            notes.append("REVERSAL: приоритет RSI, Funding и OrderBook")

        multiplier = 1.0
        if context.volatility == "HIGH":
            multiplier *= 0.5
            notes.append("HIGH VOLATILITY: размер позиции уменьшен на 50%")
        elif context.volatility == "LOW":
            multiplier *= 0.8
            notes.append("LOW VOLATILITY: размер позиции уменьшен на 20% из-за риска ложных сигналов")

        if context.liquidity == "LOW":
            allowed.discard("expert:momentum")
            multiplier *= 0.6
            notes.append("LOW LIQUIDITY: momentum/scalping логика отключена, размер уменьшен")

        permissions: Dict[str, StrategyPermission] = {}
        for name in sorted(self.ALL_EXPERTS):
            permissions[name] = StrategyPermission(
                strategy_name=name,
                allowed=name in allowed,
                reason="Разрешено Meta Strategy Manager" if name in allowed else "Отключено режимом рынка",
            )

        return MetaStrategyDecision(
            allowed_sources=allowed,
            position_size_multiplier=max(0.1, min(multiplier, 1.0)),
            permissions=permissions,
            notes=notes,
        )
