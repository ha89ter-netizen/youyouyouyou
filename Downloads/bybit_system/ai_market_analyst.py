"""
AI Market Analyst.

LLM не открывает сделки и не выдаёт торговый приказ. Этот сервис формирует
аналитическое заключение, которое Decision Engine может показать в отчёте как
дополнительный фактор. По умолчанию используется детерминированная сводка без
сетевых вызовов; реальный LLM можно подключить поверх этого интерфейса.
"""

from dataclasses import dataclass
from typing import List

from market_context import MarketContext


@dataclass
class AIMarketAnalysis:
    symbol: str
    conclusion: str
    factors: List[str]
    confidence: float


class AIMarketAnalyst:
    name = "ai:market_analyst"

    def analyze(self, symbol: str, market_snapshot: dict, context: MarketContext) -> AIMarketAnalysis:
        factors = [
            f"режим={context.regime}",
            f"тренд={context.trend}",
            f"volatility={context.volatility}",
            f"funding={context.funding_bias}",
            f"OI={context.open_interest_trend}",
        ]
        if context.regime == "BREAKOUT" and context.volume == "EXPANDING":
            conclusion = "Рынок показывает признаки пробоя на расширении объёма."
        elif context.regime == "REVERSAL":
            conclusion = "Рынок близок к разворотной зоне; нужен повышенный контроль риска."
        elif context.volatility == "HIGH":
            conclusion = "Рынок волатилен; предпочтительны меньший размер позиции и более строгий фильтр входа."
        elif context.liquidity == "LOW":
            conclusion = "Ликвидность слабая; риск проскальзывания повышен."
        else:
            conclusion = "Рыночная картина без экстремальных перекосов."
        return AIMarketAnalysis(
            symbol=symbol,
            conclusion=conclusion,
            factors=factors,
            confidence=context.confidence,
        )
