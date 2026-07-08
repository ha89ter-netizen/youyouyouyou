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
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]
        prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]

        if prev_diff <= 0 < diff:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:ema",
                confidence=0.68,
                reason="EMA12 пересекла EMA26 вверх",
                stop_loss_pct=1.5,
                take_profit_pct=3.0,
            )
        if prev_diff >= 0 > diff:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:ema",
                confidence=0.68,
                reason="EMA12 пересекла EMA26 вниз",
                stop_loss_pct=1.5,
                take_profit_pct=3.0,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:ema", reason="EMA не дала пересечения")

    @staticmethod
    def _rsi_expert(symbol: str, candles_df: pd.DataFrame) -> Optional[Signal]:
        if len(candles_df) < 16:
            return None
        closes = candles_df["close"].astype(float)
        values = rsi(closes)
        prev_value = float(values.iloc[-2])
        current = float(values.iloc[-1])
        if prev_value < 30 <= current:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:rsi",
                confidence=0.64,
                reason=f"RSI вышел из перепроданности ({prev_value:.1f}->{current:.1f})",
                stop_loss_pct=1.3,
                take_profit_pct=2.4,
            )
        if prev_value > 70 >= current:
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:rsi",
                confidence=0.64,
                reason=f"RSI вышел из перекупленности ({prev_value:.1f}->{current:.1f})",
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
        trade_flow = snapshot.get("trade_flow_last_minutes") or {}
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
        if funding_rate > 0.0005 and trend == "растёт":
            return Signal(
                symbol=symbol,
                action=Action.OPEN_SHORT,
                source="expert:funding",
                confidence=0.61,
                reason=f"Funding высокий и растёт ({funding_rate:.5f}), лонги переполнены",
                stop_loss_pct=1.4,
                take_profit_pct=2.4,
            )
        if funding_rate < -0.0005 and trend == "падает":
            return Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:funding",
                confidence=0.61,
                reason=f"Funding отрицательный и падает ({funding_rate:.5f}), шорты переполнены",
                stop_loss_pct=1.4,
                take_profit_pct=2.4,
            )
        return Signal(symbol=symbol, action=Action.HOLD, source="expert:funding", reason="Funding без экстремального перекоса")
