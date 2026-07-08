"""
Технические индикаторы, посчитанные КОДОМ (не ИИ). Это принципиально:
RSI/MACD/Bollinger — это точная математика, у неё нет права на "интерпретацию"
или галлюцинацию. Считаем их здесь один раз и отдаём готовые числа и в
rule-based схему, и в снепшот для ИИ — так оба "мозга" смотрят на одни и те же
корректно посчитанные данные, а не каждый сам пытается прикинуть RSI по глазам.
"""

import pandas as pd
import numpy as np
from typing import Optional


def ema(closes: pd.Series, span: int) -> pd.Series:
    return closes.ewm(span=span, adjust=False).mean()


def trend_direction(candles_df: pd.DataFrame, fast: int = 50, slow: int = 200) -> Optional[str]:
    """
    Определяет старший тренд по EMA50/EMA200 (классический фильтр — торговать
    только по направлению старшего тренда, отсекая контр-трендовые сигналы
    на боковике/пиле).

    Возвращает "long" (цена выше обеих EMA, быстрая выше медленной),
    "short" (зеркально), "neutral" (EMA переплетены — тренда нет) или
    None, если данных недостаточно (тогда фильтр не должен блокировать
    сигналы — молчание не равно "нет тренда").
    """
    if len(candles_df) < slow + 2:
        return None

    closes = candles_df["close"].astype(float)
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    price = closes.iloc[-1]
    f, s = ema_fast.iloc[-1], ema_slow.iloc[-1]

    if price > f > s:
        return "long"
    if price < f < s:
        return "short"
    return "neutral"


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index. 0-100. Классическая интерпретация:
    >70 — перекуплен (риск разворота вниз), <30 — перепродан (риск разворота вверх).
    """
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # avg_loss == 0 и avg_gain > 0 -> нет ни одного отката, максимальная перекупленность (100)
    result = result.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    # avg_gain == 0 и avg_loss > 0 -> нет ни одного роста, максимальная перепроданность (0)
    result = result.where(~((avg_gain == 0) & (avg_loss > 0)), 0.0)
    # оба нулевые (цена вообще не менялась) -> нейтральные 50
    return result.fillna(50)


def macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    MACD: разница между быстрой и медленной EMA + сигнальная линия (EMA от MACD).
    Гистограмма (macd - signal) пересекает 0 — момент смены импульса.
    Возвращает (macd_line, signal_line, histogram).
    """
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(closes: pd.Series, period: int = 20, num_std: float = 2.0):
    """
    Полосы Боллинджера: средняя (SMA) +/- N стандартных отклонений.
    Возвращает (upper, middle, lower).
    """
    middle = closes.rolling(window=period).mean()
    std = closes.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def bollinger_position(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> float:
    """
    Где находится текущая цена относительно полос, в диапазоне 0..1:
    0 = на нижней полосе, 0.5 = по центру (SMA), 1 = на верхней полосе.
    Может выходить за 0..1, если цена пробила полосу — это тоже полезный сигнал.
    """
    upper, middle, lower = bollinger_bands(closes, period, num_std)
    band_width = upper.iloc[-1] - lower.iloc[-1]
    if band_width == 0 or pd.isna(band_width):
        return 0.5
    return float((closes.iloc[-1] - lower.iloc[-1]) / band_width)


def atr(candles_df: pd.DataFrame, period: int = 14) -> float:
    """
    Average True Range — средний размах движения цены. Полезен для того,
    чтобы ставить stop-loss не "на глаз" в процентах, а по реальной
    волатильности инструмента прямо сейчас.
    """
    high = candles_df["high"].astype(float)
    low = candles_df["low"].astype(float)
    close = candles_df["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return float(tr.rolling(window=period).mean().iloc[-1])


def compute_all_indicators(candles_df: pd.DataFrame) -> dict:
    """Единая точка расчёта всех индикаторов сразу — используется и схемой, и снепшотом для ИИ."""
    closes = candles_df["close"].astype(float)

    if len(candles_df) < 26:
        return {}  # недостаточно данных для надёжного расчёта MACD/EMA26

    rsi_series = rsi(closes)
    macd_line, signal_line, hist = macd(closes)

    result = {
        "rsi": round(float(rsi_series.iloc[-1]), 2),
        "rsi_prev": round(float(rsi_series.iloc[-2]), 2),
        "macd_histogram": round(float(hist.iloc[-1]), 4),
        "macd_histogram_prev": round(float(hist.iloc[-2]), 4),
    }

    if len(candles_df) >= 20:
        result["bollinger_position"] = round(bollinger_position(closes), 3)

    if len(candles_df) >= 15:
        result["atr"] = round(atr(candles_df), 4)
        result["atr_pct_of_price"] = round(result["atr"] / float(closes.iloc[-1]) * 100, 3)

    return result
