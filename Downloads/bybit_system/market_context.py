"""
Market Context Engine.

Этот слой анализирует рынок ДО запуска стратегий. Он не ищет точку входа и
не открывает позиции. Его задача -- дать остальным компонентам общий контекст:
режим рынка, тренд, волатильность, ликвидность, volume/funding/OI bias и
уверенность в оценке.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any

import pandas as pd


@dataclass
class MarketContext:
    symbol: str
    regime: str = "UNKNOWN"  # TREND | RANGE | BREAKOUT | REVERSAL | UNKNOWN
    trend: str = "NEUTRAL"  # UP | DOWN | NEUTRAL
    volatility: str = "NORMAL"  # LOW | NORMAL | HIGH
    liquidity: str = "UNKNOWN"  # GOOD | NORMAL | LOW | UNKNOWN
    volume: str = "NORMAL"  # EXPANDING | NORMAL | CONTRACTING
    funding_bias: str = "NEUTRAL"  # POSITIVE | NEGATIVE | NEUTRAL
    open_interest_trend: str = "UNKNOWN"  # RISING | FALLING | STABLE | UNKNOWN
    confidence: float = 0.0  # 0..1
    risk_score: float = 0.0  # 0..1, выше = опаснее
    metrics: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["confidence_pct"] = round(self.confidence * 100, 1)
        data["risk_score_pct"] = round(self.risk_score * 100, 1)
        return data

    def summary(self) -> str:
        return (
            f"Trend: {self.trend}, Regime: {self.regime}, "
            f"Volatility: {self.volatility}, Funding: {self.funding_bias}, "
            f"Volume: {self.volume}, Liquidity: {self.liquidity}, "
            f"Open Interest: {self.open_interest_trend}, "
            f"Confidence: {self.confidence * 100:.0f}%"
        )


class MarketContextEngine:
    """
    Детерминированный анализатор контекста. Пороговые значения намеренно
    простые и прозрачные: их легко проверять по журналу и улучшать вручную.
    """

    def analyze(self, symbol: str, candles_df: pd.DataFrame, snapshot: dict) -> MarketContext:
        ctx = MarketContext(symbol=symbol)
        if candles_df is None or candles_df.empty:
            ctx.reasons.append("Нет свечей для анализа контекста")
            return ctx

        closes = candles_df["close"].astype(float)
        volumes = candles_df["volume"].astype(float)
        last_price = float(closes.iloc[-1])

        indicators = snapshot.get("indicators") or {}
        price_change_20 = float(snapshot.get("price_change_pct_last_20_candles") or 0.0)
        price_change_50 = float(snapshot.get("price_change_pct_last_50_candles") or 0.0)
        atr_pct = indicators.get("atr_pct_of_price")
        volatility_pct = snapshot.get("volatility_pct")
        funding_rate = snapshot.get("funding_rate") or 0.0
        funding_trend = snapshot.get("funding_trend") or {}
        oi_trend = snapshot.get("open_interest_trend") or {}
        orderbook = snapshot.get("orderbook") or {}

        ctx.trend = self._detect_trend(snapshot.get("trend_filter"), price_change_20, price_change_50)
        ctx.volatility = self._detect_volatility(atr_pct, volatility_pct)
        ctx.liquidity = self._detect_liquidity(orderbook.get("spread_pct"))
        ctx.volume = self._detect_volume(volumes)
        ctx.funding_bias = self._detect_funding(funding_rate, funding_trend.get("trend"))
        ctx.open_interest_trend = self._detect_open_interest(oi_trend.get("change_pct"))
        ctx.regime = self._detect_regime(candles_df, ctx, indicators, last_price)
        ctx.risk_score = self._risk_score(ctx, atr_pct, orderbook.get("spread_pct"))
        ctx.confidence = self._confidence(ctx, snapshot)

        ctx.metrics = {
            "last_price": last_price,
            "price_change_pct_20": price_change_20,
            "price_change_pct_50": price_change_50,
            "atr_pct_of_price": atr_pct,
            "volatility_pct": volatility_pct,
            "spread_pct": orderbook.get("spread_pct"),
            "funding_rate": funding_rate,
            "open_interest_change_pct": oi_trend.get("change_pct"),
        }

        ctx.reasons.extend(self._build_reasons(ctx))
        return ctx

    @staticmethod
    def _detect_trend(trend_filter: Optional[str], change_20: float, change_50: float) -> str:
        if trend_filter == "long":
            return "UP"
        if trend_filter == "short":
            return "DOWN"
        if change_20 > 1.0 and change_50 > 1.5:
            return "UP"
        if change_20 < -1.0 and change_50 < -1.5:
            return "DOWN"
        return "NEUTRAL"

    @staticmethod
    def _detect_volatility(atr_pct: Optional[float], volatility_pct: Optional[float]) -> str:
        value = atr_pct if atr_pct is not None else volatility_pct
        if value is None:
            return "NORMAL"
        if value >= 2.5:
            return "HIGH"
        if value <= 0.35:
            return "LOW"
        return "NORMAL"

    @staticmethod
    def _detect_liquidity(spread_pct: Optional[float]) -> str:
        if spread_pct is None:
            return "UNKNOWN"
        if spread_pct <= 0.05:
            return "GOOD"
        if spread_pct > 0.15:
            return "LOW"
        return "NORMAL"

    @staticmethod
    def _detect_volume(volumes: pd.Series) -> str:
        if len(volumes) < 25:
            return "NORMAL"
        recent = float(volumes.tail(5).mean())
        baseline = float(volumes.iloc[-25:-5].mean())
        if baseline <= 0:
            return "NORMAL"
        ratio = recent / baseline
        if ratio >= 1.25:
            return "EXPANDING"
        if ratio <= 0.70:
            return "CONTRACTING"
        return "NORMAL"

    @staticmethod
    def _detect_funding(funding_rate: float, funding_trend: Optional[str]) -> str:
        if funding_rate > 0.0002 or funding_trend == "растёт":
            return "POSITIVE"
        if funding_rate < -0.0002 or funding_trend == "падает":
            return "NEGATIVE"
        return "NEUTRAL"

    @staticmethod
    def _detect_open_interest(change_pct: Optional[float]) -> str:
        if change_pct is None:
            return "UNKNOWN"
        if change_pct >= 1.0:
            return "RISING"
        if change_pct <= -1.0:
            return "FALLING"
        return "STABLE"

    @staticmethod
    def _detect_regime(
        candles_df: pd.DataFrame,
        ctx: MarketContext,
        indicators: dict,
        last_price: float,
    ) -> str:
        recent = candles_df.tail(20)
        high_20 = float(recent["high"].astype(float).max())
        low_20 = float(recent["low"].astype(float).min())
        rsi_value = indicators.get("rsi")

        if ctx.volume == "EXPANDING" and (last_price >= high_20 * 0.998 or last_price <= low_20 * 1.002):
            return "BREAKOUT"
        if rsi_value is not None and (rsi_value >= 72 or rsi_value <= 28):
            return "REVERSAL"
        if ctx.trend in ("UP", "DOWN") and ctx.volatility != "LOW":
            return "TREND"
        if ctx.volatility == "LOW" or ctx.trend == "NEUTRAL":
            return "RANGE"
        return "UNKNOWN"

    @staticmethod
    def _risk_score(ctx: MarketContext, atr_pct: Optional[float], spread_pct: Optional[float]) -> float:
        score = 0.20
        if ctx.volatility == "HIGH":
            score += 0.25
        elif ctx.volatility == "LOW":
            score -= 0.05
        if ctx.liquidity == "LOW":
            score += 0.30
        elif ctx.liquidity == "GOOD":
            score -= 0.05
        if ctx.regime in ("BREAKOUT", "REVERSAL"):
            score += 0.10
        if ctx.open_interest_trend == "FALLING" and ctx.regime == "TREND":
            score += 0.10
        if atr_pct is not None:
            score += min(max((atr_pct - 1.5) / 10, 0), 0.15)
        if spread_pct is not None:
            score += min(max((spread_pct - 0.05) / 0.5, 0), 0.15)
        return max(0.0, min(score, 1.0))

    @staticmethod
    def _confidence(ctx: MarketContext, snapshot: dict) -> float:
        known = 0
        total = 7
        for key in ("funding_rate", "funding_trend", "open_interest_trend", "orderbook", "trade_flow_last_minutes"):
            if snapshot.get(key) is not None:
                known += 1
        if snapshot.get("indicators"):
            known += 1
        if ctx.regime != "UNKNOWN":
            known += 1
        confidence = 0.35 + 0.08 * known
        if ctx.volume == "EXPANDING" and ctx.regime in ("BREAKOUT", "TREND"):
            confidence += 0.08
        if ctx.liquidity == "UNKNOWN":
            confidence -= 0.05
        return max(0.0, min(confidence, 0.95))

    @staticmethod
    def _build_reasons(ctx: MarketContext) -> List[str]:
        reasons = [
            f"Режим рынка: {ctx.regime}",
            f"Тренд: {ctx.trend}",
            f"Волатильность: {ctx.volatility}",
            f"Ликвидность: {ctx.liquidity}",
            f"Объём: {ctx.volume}",
            f"Funding bias: {ctx.funding_bias}",
            f"Open Interest: {ctx.open_interest_trend}",
        ]
        return reasons
