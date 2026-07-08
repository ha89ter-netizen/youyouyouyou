"""
Rule-based стратегия: жёсткая, предсказуемая схема без права на
интерпретацию. Если условие выполнено — сигнал есть. Не выполнено — нет.

Пример здесь: пересечение быстрой/медленной EMA + фильтр по funding rate
(не открываем лонг, если funding аномально высокий — платить за удержание
позиции дорого). Это ЗАМЕНЯЕМЫЙ пример — вашу реальную схему/паттерн
подставляете сюда же, интерфейс (generate_signal) остаётся тем же.
"""

import logging
from typing import Optional, List

import pandas as pd

from strategy.signal import Signal, Action
from strategy.indicators import rsi, macd, bollinger_position

logger = logging.getLogger(__name__)


class EmaCrossStrategy:
    """
    Правило:
    - EMA(fast) пересекает EMA(slow) снизу вверх → LONG
    - EMA(fast) пересекает EMA(slow) сверху вниз → SHORT
    - |funding_rate| > funding_threshold → сигнал игнорируется (слишком дорого держать)
    """

    name = "rule:ema_cross"

    def __init__(self, fast: int = 12, slow: int = 26, funding_threshold: float = 0.0005):
        self.fast = fast
        self.slow = slow
        self.funding_threshold = funding_threshold

    def generate_signal(
        self, symbol: str, candles_df: pd.DataFrame, funding_rate: float
    ) -> Optional[Signal]:
        """
        candles_df: DataFrame с колонками ['start_time','open','high','low','close','volume'],
        отсортирован по времени по возрастанию. Обычно берётся из БД (candles table).
        """
        if len(candles_df) < self.slow + 2:
            return None  # недостаточно данных для расчёта EMA

        closes = candles_df["close"].astype(float)
        ema_fast = closes.ewm(span=self.fast, adjust=False).mean()
        ema_slow = closes.ewm(span=self.slow, adjust=False).mean()

        prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]
        curr_diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]

        crossed_up = prev_diff <= 0 and curr_diff > 0
        crossed_down = prev_diff >= 0 and curr_diff < 0

        if not (crossed_up or crossed_down):
            return Signal(symbol=symbol, action=Action.HOLD, source=self.name,
                           reason="Нет пересечения EMA")

        if abs(funding_rate) > self.funding_threshold:
            return Signal(
                symbol=symbol, action=Action.HOLD, source=self.name,
                reason=f"Funding rate {funding_rate:.5f} выше порога {self.funding_threshold}",
            )

        action = Action.OPEN_LONG if crossed_up else Action.OPEN_SHORT
        return Signal(
            symbol=symbol, action=action, source=self.name,
            confidence=0.6,
            reason=f"EMA{self.fast}/EMA{self.slow} пересечение "
                   f"({'вверх' if crossed_up else 'вниз'}), funding={funding_rate:.5f}",
            stop_loss_pct=1.5,
            take_profit_pct=3.0,
        )


class TechnicalRuleCommittee:
    """
    Комитет из нескольких независимых индикаторов вместо одной схемы.
    Каждый индикатор голосует за LONG/SHORT/нейтрально. Сигнал выдаётся,
    только если минимум min_votes индикаторов согласны в одном направлении —
    так одна случайная аномалия одного индикатора не откроет сделку.

    Голосующие правила:
    - EMA(fast/slow) пересечение — импульс тренда
    - RSI выход из зоны экстремума (>70 -> вниз, <30 -> вверх), т.е. коррекция
    - MACD histogram пересекает 0 — смена импульса
    - Bollinger: цена у края полосы (>0.95 или <0.05) — тоже сигнал на коррекцию

    Это НАМЕРЕННО простые, explainable правила — цель не "оптимальная" торговая
    система, а надёжная база, где всегда понятно, почему сработал сигнал.
    """

    name = "rule:committee"

    def __init__(
        self,
        ema_fast: int = 12, ema_slow: int = 26,
        rsi_period: int = 14, rsi_overbought: float = 70, rsi_oversold: float = 30,
        funding_threshold: float = 0.0005,
        min_votes: int = 2,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.funding_threshold = funding_threshold
        self.min_votes = min_votes

    def generate_signal(
        self, symbol: str, candles_df: pd.DataFrame, funding_rate: float
    ) -> Optional[Signal]:
        if len(candles_df) < self.ema_slow + 2:
            return None

        closes = candles_df["close"].astype(float)
        votes: List[str] = []  # каждый элемент "long" или "short"
        reasons: List[str] = []

        # --- Голос 1: EMA cross ---
        ema_fast = closes.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow = closes.ewm(span=self.ema_slow, adjust=False).mean()
        prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]
        curr_diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]
        if prev_diff <= 0 and curr_diff > 0:
            votes.append("long")
            reasons.append(f"EMA{self.ema_fast}/{self.ema_slow} пересечение вверх")
        elif prev_diff >= 0 and curr_diff < 0:
            votes.append("short")
            reasons.append(f"EMA{self.ema_fast}/{self.ema_slow} пересечение вниз")

        # --- Голос 2: RSI выход из экстремума (коррекционный сигнал) ---
        rsi_series = rsi(closes, self.rsi_period)
        rsi_prev, rsi_curr = rsi_series.iloc[-2], rsi_series.iloc[-1]
        if rsi_prev < self.rsi_oversold <= rsi_curr:
            votes.append("long")
            reasons.append(f"RSI вышел из перепроданности ({rsi_prev:.1f}->{rsi_curr:.1f})")
        elif rsi_prev > self.rsi_overbought >= rsi_curr:
            votes.append("short")
            reasons.append(f"RSI вышел из перекупленности ({rsi_prev:.1f}->{rsi_curr:.1f})")

        # --- Голос 3: MACD histogram пересекает 0 ---
        _, _, hist = macd(closes)
        hist_prev, hist_curr = hist.iloc[-2], hist.iloc[-1]
        if hist_prev <= 0 < hist_curr:
            votes.append("long")
            reasons.append("MACD histogram пересёк 0 вверх")
        elif hist_prev >= 0 > hist_curr:
            votes.append("short")
            reasons.append("MACD histogram пересёк 0 вниз")

        # --- Голос 4: Bollinger — цена у края полосы ---
        if len(candles_df) >= 20:
            bp = bollinger_position(closes)
            if bp <= 0.05:
                votes.append("long")
                reasons.append(f"цена у нижней полосы Боллинджера ({bp:.2f})")
            elif bp >= 0.95:
                votes.append("short")
                reasons.append(f"цена у верхней полосы Боллинджера ({bp:.2f})")

        long_votes = votes.count("long")
        short_votes = votes.count("short")

        if long_votes >= self.min_votes and long_votes > short_votes:
            direction, count = "long", long_votes
        elif short_votes >= self.min_votes and short_votes > long_votes:
            direction, count = "short", short_votes
        else:
            return Signal(
                symbol=symbol, action=Action.HOLD, source=self.name,
                reason=f"Недостаточно согласованных голосов (long={long_votes}, short={short_votes}, "
                       f"нужно минимум {self.min_votes} в одну сторону)",
            )

        # Funding-фильтр направленный: высокий ПОЛОЖИТЕЛЬНЫЙ funding означает, что
        # платят лонги (это невыгодно открывать LONG, но не мешает SHORT — шорты
        # его получают). Аналогично отрицательный funding невыгоден для SHORT.
        # Блокируем только ту сторону, которая реально платит, а не сделку целиком.
        if direction == "long" and funding_rate > self.funding_threshold:
            return Signal(
                symbol=symbol, action=Action.HOLD, source=self.name,
                reason=f"{count} индикаторов за long, но funding rate {funding_rate:.5f} "
                       f"выше порога {self.funding_threshold} -- лонги платят шортам, невыгодно",
            )
        if direction == "short" and funding_rate < -self.funding_threshold:
            return Signal(
                symbol=symbol, action=Action.HOLD, source=self.name,
                reason=f"{count} индикаторов за short, но funding rate {funding_rate:.5f} "
                       f"ниже -{self.funding_threshold} -- шорты платят лонгам, невыгодно",
            )

        total_indicators_checked = 4 if len(candles_df) >= 20 else 3  # Bollinger нужен минимум 20 свечей

        if direction == "long":
            matched_reasons = [r for r in reasons if "вверх" in r or "перепроданности" in r or "нижней" in r]
        else:
            matched_reasons = [r for r in reasons if "вниз" in r or "перекупленности" in r or "верхней" in r]

        return Signal(
            symbol=symbol,
            action=Action.OPEN_LONG if direction == "long" else Action.OPEN_SHORT,
            source=self.name,
            confidence=min(0.5 + 0.15 * count, 0.9),  # больше согласных голосов -> выше уверенность
            reason=f"{count}/{total_indicators_checked} индикаторов согласны ({direction}): "
                   + "; ".join(matched_reasons),
            stop_loss_pct=1.5,
            take_profit_pct=3.0,
        )
