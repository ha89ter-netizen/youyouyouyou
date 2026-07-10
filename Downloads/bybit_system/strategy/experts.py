"""
Независимые эксперты для Decision Engine.

Каждый эксперт возвращает обычный Signal. Он не знает о риске, исполнении и
портфеле; его зона ответственности -- только мнение: LONG, SHORT или HOLD.
"""

from typing import List, Optional

import pandas as pd

from strategy.indicators import rsi
from strategy.rule_based import TechnicalRuleCommittee
from strategy.signal import Action, Signal


class ExpertSignalCollector:
    def __init__(self):
        self.rule_committee = TechnicalRuleCommittee()

    def collect(
        self,
        symbol: str,
        candles_df: pd.DataFrame,
        funding_rate: float,
        market_snapshot: dict,
    ) -> List[Signal]:
        signals = [
            self._ema_expert(symbol, candles_df),
            self._rsi_expert(symbol, candles_df),
            self._vwap_expert(symbol, candles_df),
            self._momentum_expert(symbol, market_snapshot),
            self._orderbook_expert(symbol, market_snapshot),
            self._funding_expert(symbol, funding_rate, market_snapshot),
            self.rule_committee.generate_signal(symbol, candles_df, funding_rate),
        ]
        return [s for s in signals if s is not None]

    @staticmethod
    def _ema_expert(symbol: str, candles_df: pd.DataFrame) -> Optional[Signal]:
        if len(candles_df) < 28:
            return None

        closes = candles_df["close"].astype(float)
        price = float(closes.iloc[-1])

        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()

        fast = float(ema_fast.iloc[-1])
        slow = float(ema_slow.iloc[-1])
        fast_prev = float(ema_fast.iloc[-2])
        fast_slope_pct = (fast / fast_prev - 1) * 100 if fast_prev else 0.0

        gap_pct = abs(fast - slow) / price * 100

        if gap_pct < 0.03:
            return Signal(
                symbol=symbol,
                action=Action.HOLD,
                source="expert:ema",
                confidence=0.50,
                reason=f"EMA12/26 слишком близко: gap={gap_pct:.3f}%",
            )

        if fast > slow and fast_slope_pct >= -0.03:
            confidence = min(0.58 + gap_pct * 2.0 + max(fast_slope_pct, 0) * 2.0, 0.74)
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:ema",
                confidence=round(confidence, 3),
                reason=f"EMA12 выше EMA26, LONG-состояние, gap={gap_pct:.3f}%, slope={fast_slope_pct:.3f}%",
                stop_loss_pct=1.5,
                take_profit_pct=3.0,
            )

        if fast < slow and fast_slope_pct <= 0.03:
            confidence = min(0.58 + gap_pct * 2.0 + max(-fast_slope_pct, 0) * 2.0, 0.74)
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:ema",
                confidence=round(confidence, 3),
                reason=f"EMA12 ниже EMA26, SHORT-состояние, gap={gap_pct:.3f}%, slope={fast_slope_pct:.3f}%",
                stop_loss_pct=1.5,
                take_profit_pct=3.0,
            )

        return Signal(
            symbol=symbol,
            action=Action.HOLD,
            source="expert:ema",
            confidence=0.50,
            reason="EMA12/26 нейтральны",
        )
    
    @staticmethod
    def _rsi_expert(symbol: str, candles_df: pd.DataFrame) -> Optional[Signal]:
        if len(candles_df) < 16:
            return None
        closes = candles_df["close"].astype(float)
        values = rsi(closes)
        prev_value = float(values.iloc[-2])
        current = float(values.iloc[-1])
        recent_min = float(values.tail(6).min())
        recent_max = float(values.tail(6).max())
        if prev_value < 30 <= current or (recent_min <= 35 and 30 <= current <= 42 and current > prev_value):
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:rsi",
                confidence=0.64 if prev_value < 30 <= current else 0.58,
                reason=f"RSI восстановление после перепроданности (min6={recent_min:.1f}, {prev_value:.1f}->{current:.1f})",
                stop_loss_pct=1.3,
                take_profit_pct=2.4,
            )
        if prev_value > 70 >= current or (recent_max >= 65 and 58 <= current <= 70 and current < prev_value):
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:rsi",
                confidence=0.64 if prev_value > 70 >= current else 0.58,
                reason=f"RSI охлаждение после перекупленности (max6={recent_max:.1f}, {prev_value:.1f}->{current:.1f})",
                stop_loss_pct=1.3,
                take_profit_pct=2.4,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:rsi", reason=f"RSI нейтрален ({current:.1f})")

    @staticmethod
    def _vwap_expert(symbol: str, candles_df: pd.DataFrame) -> Optional[Signal]:
        if len(candles_df) < 20:
            return None
        recent = candles_df.tail(20).copy()
        close = recent["close"].astype(float)
        high = recent["high"].astype(float)
        low = recent["low"].astype(float)
        volume = recent["volume"].astype(float)
        typical_price = (high + low + close) / 3
        total_volume = float(volume.sum())
        if total_volume <= 0:
            return None
        vwap = float((typical_price * volume).sum() / total_volume)
        last = float(close.iloc[-1])
        distance_pct = (last / vwap - 1) * 100

        if 0.10 <= distance_pct <= 1.50:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:vwap",
                confidence=0.62,
                reason=f"Цена удерживается выше VWAP на {distance_pct:.2f}%",
                stop_loss_pct=1.2,
                take_profit_pct=2.6,
            )
        if -1.50 <= distance_pct <= -0.10:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:vwap",
                confidence=0.62,
                reason=f"Цена удерживается ниже VWAP на {abs(distance_pct):.2f}%",
                stop_loss_pct=1.2,
                take_profit_pct=2.6,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:vwap", reason="Цена далеко от VWAP или около него")

    @staticmethod
    def _momentum_expert(symbol: str, snapshot: dict) -> Signal:
        change_20 = float(snapshot.get("price_change_pct_last_20_candles") or 0.0)
        trade_flow = snapshot.get("trade_flow_last_minutes")
        if not trade_flow:
            return Signal(
                symbol=symbol, action=Action.HOLD, source="expert:momentum",
                reason="Нет свежего trade flow: momentum отключён до восстановления data pipeline",
            )
        imbalance = float(trade_flow.get("imbalance") or 0.0)
        if change_20 >= 1.2 and imbalance >= 0.15:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:momentum",
                confidence=0.70,
                reason=f"Импульс вверх {change_20:.2f}% и buy-flow imbalance {imbalance:.2f}",
                stop_loss_pct=1.6,
                take_profit_pct=3.2,
            )
        if change_20 <= -1.2 and imbalance <= -0.15:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:momentum",
                confidence=0.70,
                reason=f"Импульс вниз {change_20:.2f}% и sell-flow imbalance {imbalance:.2f}",
                stop_loss_pct=1.6,
                take_profit_pct=3.2,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:momentum", reason="Нет согласованного импульса")

    @staticmethod
    def _orderbook_expert(symbol: str, snapshot: dict) -> Signal:
        orderbook = snapshot.get("orderbook") or {}
        if not orderbook:
            return Signal(
                symbol=symbol, action=Action.HOLD, source="expert:orderbook",
                reason="Нет свежего orderbook snapshot",
            )
        imbalance = float(orderbook.get("bid_ask_imbalance") or 0.0)
        spread_pct = orderbook.get("spread_pct")
        if spread_pct is not None and spread_pct > 0.15:
            return Signal(symbol=symbol, action=Action.HOLD, source="expert:orderbook", reason="Спред слишком широкий")
        if imbalance >= 0.25:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:orderbook",
                confidence=0.66,
                reason=f"Стакан показывает перевес bid объёма ({imbalance:.2f})",
                stop_loss_pct=1.2,
                take_profit_pct=2.5,
            )
        if imbalance <= -0.25:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:orderbook",
                confidence=0.66,
                reason=f"Стакан показывает перевес ask объёма ({imbalance:.2f})",
                stop_loss_pct=1.2,
                take_profit_pct=2.5,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:orderbook", reason="Нет сильного дисбаланса стакана")

    @staticmethod
    def _funding_expert(symbol: str, funding_rate: float, snapshot: dict) -> Signal:
        funding_trend = snapshot.get("funding_trend") or {}
        trend = funding_trend.get("trend")
        if (funding_rate > 0.0005 and trend in ("растёт", "стабилен")) or funding_rate > 0.0008:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:funding",
                confidence=0.63 if trend == "растёт" else 0.58,
                reason=f"Funding высокий ({funding_rate:.5f}, trend={trend or 'unknown'}), лонги переполнены",
                stop_loss_pct=1.4,
                take_profit_pct=2.8,
            )
        if (funding_rate < -0.0005 and trend in ("падает", "стабилен")) or funding_rate < -0.0008:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:funding",
                confidence=0.63 if trend == "падает" else 0.58,
                reason=f"Funding отрицательный ({funding_rate:.5f}, trend={trend or 'unknown'}), шорты переполнены",
                stop_loss_pct=1.4,
                take_profit_pct=2.8,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:funding", reason="Funding без экстремального перекоса")
