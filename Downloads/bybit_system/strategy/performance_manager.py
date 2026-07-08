"""
Strategy Performance Manager.

Считает статистику по источникам сигналов: win rate, profit factor, average RR,
holding time, max drawdown, Sharpe, expectancy, последние 30/100 сделок и общий
рейтинг. Данные берутся из trade_log, поэтому модуль ничего не меняет в торговле.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import math

from storage.db import Database
from storage.models import TradeLog


@dataclass
class StrategyPerformance:
    source: str
    total_trades: int
    last_30_trades: int
    last_100_trades: int
    win_rate: float
    profit_factor: float
    average_rr: float
    average_holding_minutes: float
    max_drawdown: float
    sharpe_ratio: float
    expectancy: float
    rating: float


class StrategyPerformanceManager:
    def __init__(self, db: Database):
        self.db = db

    def calculate_all(self) -> Dict[str, StrategyPerformance]:
        session = self.db.get_session()
        try:
            rows = (
                session.query(TradeLog)
                .filter(TradeLog.status == "closed", TradeLog.pnl_usdt.isnot(None))
                .order_by(TradeLog.closed_at.asc())
                .all()
            )
            grouped: Dict[str, List[TradeLog]] = {}
            for row in rows:
                grouped.setdefault(row.source, []).append(row)
            return {source: self._calculate(source, trades) for source, trades in grouped.items()}
        finally:
            session.close()

    def _calculate(self, source: str, trades: List[TradeLog]) -> StrategyPerformance:
        pnls = [float(t.pnl_usdt) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total = len(trades)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)
        win_rate = len(wins) / total if total else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
        average_rr = self._average_rr(trades)
        holding = self._average_holding_minutes(trades)
        max_drawdown = self._max_drawdown(pnls)
        sharpe = self._sharpe(pnls)
        rating = self._rating(win_rate, profit_factor, expectancy, max_drawdown, sharpe)
        return StrategyPerformance(
            source=source,
            total_trades=total,
            last_30_trades=min(total, 30),
            last_100_trades=min(total, 100),
            win_rate=round(win_rate * 100, 2),
            profit_factor=round(profit_factor, 3) if math.isfinite(profit_factor) else profit_factor,
            average_rr=round(average_rr, 3),
            average_holding_minutes=round(holding, 2),
            max_drawdown=round(max_drawdown, 3),
            sharpe_ratio=round(sharpe, 3),
            expectancy=round(expectancy, 3),
            rating=round(rating, 2),
        )

    @staticmethod
    def _average_rr(trades: List[TradeLog]) -> float:
        ratios = []
        for trade in trades:
            if trade.stop_loss_pct and trade.take_profit_pct and float(trade.stop_loss_pct) > 0:
                ratios.append(float(trade.take_profit_pct) / float(trade.stop_loss_pct))
        return sum(ratios) / len(ratios) if ratios else 0.0

    @staticmethod
    def _average_holding_minutes(trades: List[TradeLog]) -> float:
        values = []
        for trade in trades:
            if trade.opened_at and trade.closed_at:
                values.append((trade.closed_at - trade.opened_at).total_seconds() / 60)
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _max_drawdown(pnls: List[float]) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @staticmethod
    def _sharpe(pnls: List[float]) -> float:
        if len(pnls) < 2:
            return 0.0
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(variance)
        return mean / std * math.sqrt(len(pnls)) if std else 0.0

    @staticmethod
    def _rating(win_rate: float, profit_factor: float, expectancy: float, max_drawdown: float, sharpe: float) -> float:
        pf_score = min(profit_factor if math.isfinite(profit_factor) else 3.0, 3.0) / 3.0
        exp_score = 1.0 if expectancy > 0 else 0.3
        dd_penalty = min(max_drawdown / 100, 0.5)
        sharpe_score = min(max(sharpe, 0.0), 3.0) / 3.0
        return max(0.0, min(100.0, (win_rate * 0.35 + pf_score * 0.30 + exp_score * 0.20 + sharpe_score * 0.15 - dd_penalty) * 100))
