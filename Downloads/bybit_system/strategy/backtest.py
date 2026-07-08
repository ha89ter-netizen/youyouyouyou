"""
Бэктест: прогоняет rule-based комитет (TechnicalRuleCommittee) + trend filter
на исторических данных ИЗ ВАШЕЙ ЖЕ БД и считает, как бы вела себя схема.

ВАЖНО про AI-часть: бэктест НЕ вызывает OpenAI на каждой исторической свече.
Прогнать реальные вызовы модели по всей истории стоило бы денег (тысячи
вызовов) и заняло бы часы — а решение AI-стратегии не детерминировано
(зависит от текущего состояния модели), так что "бэктест ИИ" в строгом
смысле не особо осмыслен. Оценивайте AI-часть иначе: смотрите на реальный
журнал (trade_log) после нескольких дней живой торговли на testnet —
там будет видно, какие сигналы от source="ai:openai" реально прибыльны.

Защита от заглядывания в будущее (look-ahead bias):
- Решение о сигнале принимается ТОЛЬКО на данных ДО текущей свечи.
- Вход выполняется по цене ОТКРЫТИЯ следующей свечи после сигнала,
  а не по цене, на которой сигнал был обнаружен постфактум.
- Пока позиция "открыта", новый сигнал не ищется (как и в реальной системе).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from storage.db import Database
from storage.models import Candle, FundingRate
from strategy.rule_based import TechnicalRuleCommittee
from strategy.indicators import trend_direction
from strategy.signal import Action

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    exit_reason: str
    pnl_pct: float
    pnl_usdt: float


@dataclass
class BacktestResult:
    symbol: str
    starting_balance: float
    trades: List[BacktestTrade] = field(default_factory=list)
    ending_balance: float = 0.0

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_usdt > 0)
        return wins / len(self.trades) * 100

    @property
    def total_pnl_usdt(self) -> float:
        return sum(t.pnl_usdt for t in self.trades)

    @property
    def total_return_pct(self) -> float:
        if self.starting_balance == 0:
            return 0.0
        return (self.ending_balance - self.starting_balance) / self.starting_balance * 100

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_usdt for t in self.trades if t.pnl_usdt > 0)
        losses = abs(sum(t.pnl_usdt for t in self.trades if t.pnl_usdt < 0))
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_pct(self) -> float:
        if not self.trades:
            return 0.0
        balance = self.starting_balance
        peak = balance
        max_dd = 0.0
        for t in self.trades:
            balance += t.pnl_usdt
            peak = max(peak, balance)
            if peak > 0:
                max_dd = max(max_dd, (peak - balance) / peak * 100)
        return max_dd

    def summary(self) -> str:
        return (
            f"Символ: {self.symbol}\n"
            f"Сделок: {self.total_trades}\n"
            f"Win rate: {self.win_rate_pct:.1f}%\n"
            f"Итоговый PnL: {self.total_pnl_usdt:.2f} USDT ({self.total_return_pct:+.2f}%)\n"
            f"Profit factor: {self.profit_factor:.2f}\n"
            f"Максимальная просадка: {self.max_drawdown_pct:.2f}%\n"
            f"Баланс: {self.starting_balance:.2f} -> {self.ending_balance:.2f} USDT"
        )


class Backtester:
    def __init__(
        self,
        db: Database,
        risk_per_trade_pct: float = 1.0,
        max_position_usdt: float = 100.0,
        default_stop_loss_pct: float = 1.5,
        trend_filter_enabled: bool = True,
        starting_balance: float = 10_000.0,
    ):
        self.db = db
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_usdt = max_position_usdt
        self.default_stop_loss_pct = default_stop_loss_pct
        self.trend_filter_enabled = trend_filter_enabled
        self.starting_balance = starting_balance
        self.committee = TechnicalRuleCommittee()

    def run(self, symbol: str, interval: str = "15", min_history: int = 210) -> BacktestResult:
        candles = self._load_candles(symbol, interval)
        if len(candles) < min_history + 10:
            raise ValueError(
                f"Недостаточно исторических свечей для {symbol}: {len(candles)} шт, "
                f"нужно минимум {min_history + 10}. Дайте main.py поработать дольше."
            )

        candles = self._attach_funding(symbol, candles)

        result = BacktestResult(symbol=symbol, starting_balance=self.starting_balance)
        balance = self.starting_balance
        open_trade: Optional[dict] = None

        for i in range(min_history, len(candles)):
            row = candles.iloc[i]

            if open_trade is not None:
                exit_price, exit_reason = self._check_exit(row, open_trade)
                if exit_price is not None:
                    pnl_pct = self._calc_pnl_pct(open_trade["direction"], open_trade["entry_price"], exit_price)
                    pnl_usdt = open_trade["size_usdt"] * pnl_pct
                    balance += pnl_usdt
                    result.trades.append(BacktestTrade(
                        symbol=symbol, direction=open_trade["direction"],
                        entry_time=open_trade["entry_time"], entry_price=open_trade["entry_price"],
                        exit_time=int(row["start_time"]), exit_price=exit_price, exit_reason=exit_reason,
                        pnl_pct=pnl_pct * 100, pnl_usdt=pnl_usdt,
                    ))
                    open_trade = None
                continue  # пока позиция открыта -- новую не ищем, как и в реальной системе

            # Решение принимаем СТРОГО на данных до текущей свечи (candles[:i]),
            # входим по цене открытия ТЕКУЩЕЙ свечи -- без заглядывания в будущее.
            decision_window = candles.iloc[:i]
            funding_rate = float(decision_window["funding_rate"].iloc[-1])
            signal = self.committee.generate_signal(symbol, decision_window, funding_rate)
            if signal is None or signal.action == Action.HOLD:
                continue

            direction = "long" if signal.action == Action.OPEN_LONG else "short"
            if self.trend_filter_enabled:
                trend = trend_direction(decision_window)
                if trend is not None and trend != "neutral" and trend != direction:
                    continue

            entry_price = float(row["open"])
            stop_loss_pct = signal.stop_loss_pct or self.default_stop_loss_pct
            take_profit_pct = signal.take_profit_pct or (stop_loss_pct * 2)

            risk_amount = balance * (self.risk_per_trade_pct / 100)
            size_usdt = min(risk_amount / (stop_loss_pct / 100), self.max_position_usdt, balance * 0.9)
            if size_usdt <= 0:
                continue

            if direction == "long":
                stop_price = entry_price * (1 - stop_loss_pct / 100)
                tp_price = entry_price * (1 + take_profit_pct / 100)
            else:
                stop_price = entry_price * (1 + stop_loss_pct / 100)
                tp_price = entry_price * (1 - take_profit_pct / 100)

            open_trade = {
                "direction": direction, "entry_price": entry_price,
                "entry_time": int(row["start_time"]),
                "stop_price": stop_price, "tp_price": tp_price, "size_usdt": size_usdt,
            }

        # Если к концу доступных данных позиция ещё "открыта" -- закрываем её
        # принудительно по последней известной цене. Без этого реально открытая
        # (и потенциально единственная) сделка молча исчезла бы из статистики,
        # никак не попав в result.trades -- именно это давало ложные "0 сделок"
        # на коротких периодах истории.
        if open_trade is not None:
            last_row = candles.iloc[-1]
            exit_price = float(last_row["close"])
            pnl_pct = self._calc_pnl_pct(open_trade["direction"], open_trade["entry_price"], exit_price)
            pnl_usdt = open_trade["size_usdt"] * pnl_pct
            balance += pnl_usdt
            result.trades.append(BacktestTrade(
                symbol=symbol, direction=open_trade["direction"],
                entry_time=open_trade["entry_time"], entry_price=open_trade["entry_price"],
                exit_time=int(last_row["start_time"]), exit_price=exit_price,
                exit_reason="конец периода бэктеста (позиция ещё не закрылась по SL/TP)",
                pnl_pct=pnl_pct * 100, pnl_usdt=pnl_usdt,
            ))

        result.ending_balance = balance
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _check_exit(row, open_trade: dict):
        direction = open_trade["direction"]
        if direction == "long":
            hit_sl = row["low"] <= open_trade["stop_price"]
            hit_tp = row["high"] >= open_trade["tp_price"]
        else:
            hit_sl = row["high"] >= open_trade["stop_price"]
            hit_tp = row["low"] <= open_trade["tp_price"]

        if hit_sl:
            # Консервативно: если в одной свече задеты и SL, и TP -- считаем,
            # что сработал SL (худший сценарий, а не гадаем порядок внутри свечи)
            return open_trade["stop_price"], "stop_loss"
        if hit_tp:
            return open_trade["tp_price"], "take_profit"
        return None, None

    @staticmethod
    def _calc_pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
        if direction == "long":
            return (exit_price - entry_price) / entry_price
        return (entry_price - exit_price) / entry_price

    def _load_candles(self, symbol: str, interval: str) -> pd.DataFrame:
        session = self.db.get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.interval == interval)
                .order_by(Candle.start_time.asc())
                .all()
            )
            data = [{
                "start_time": r.start_time, "open": float(r.open), "high": float(r.high),
                "low": float(r.low), "close": float(r.close), "volume": float(r.volume),
            } for r in rows]
            return pd.DataFrame(data)
        finally:
            session.close()

    def _attach_funding(self, symbol: str, candles: pd.DataFrame) -> pd.DataFrame:
        """Приклеивает funding_rate к каждой свече (последнее известное значение НА МОМЕНТ свечи)."""
        session = self.db.get_session()
        try:
            rows = (
                session.query(FundingRate)
                .filter(FundingRate.symbol == symbol)
                .order_by(FundingRate.funding_ts.asc())
                .all()
            )
            funding = pd.DataFrame([
                {"funding_ts": r.funding_ts, "funding_rate": float(r.funding_rate)} for r in rows
            ])
        finally:
            session.close()

        if funding.empty:
            candles = candles.copy()
            candles["funding_rate"] = 0.0
            return candles

        merged = pd.merge_asof(
            candles.sort_values("start_time"), funding.sort_values("funding_ts"),
            left_on="start_time", right_on="funding_ts", direction="backward",
        )
        merged["funding_rate"] = merged["funding_rate"].fillna(0.0)
        return merged
