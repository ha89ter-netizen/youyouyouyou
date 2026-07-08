"""
Paper Trading Engine.

Эмулирует исполнение без реальных ордеров: вход, выход, PnL, комиссии и
проскальзывание считаются локально.
"""

from dataclasses import dataclass, field
from typing import List, Optional
import uuid

from strategy.signal import Action, Signal


@dataclass
class PaperPosition:
    order_id: str
    symbol: str
    side: str
    entry_price: float
    size_usdt: float
    leverage: int
    stop_loss_pct: Optional[float]
    take_profit_pct: Optional[float]


@dataclass
class PaperTrade:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    size_usdt: float
    pnl_usdt: float
    fees_usdt: float
    slippage_usdt: float
    reason: str


@dataclass
class PaperTradingEngine:
    starting_balance: float = 10_000.0
    fee_rate: float = 0.0006
    slippage_pct: float = 0.03
    balance: float = field(init=False)
    positions: List[PaperPosition] = field(default_factory=list)
    trades: List[PaperTrade] = field(default_factory=list)

    def __post_init__(self):
        self.balance = self.starting_balance

    def open_position(
        self,
        signal: Signal,
        last_price: float,
        size_usdt: float,
        leverage: int = 1,
    ) -> Optional[PaperPosition]:
        if signal.action not in (Action.OPEN_LONG, Action.OPEN_SHORT):
            return None
        side = "long" if signal.action == Action.OPEN_LONG else "short"
        fill_price = self._apply_slippage(last_price, side, is_entry=True)
        position = PaperPosition(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            symbol=signal.symbol,
            side=side,
            entry_price=fill_price,
            size_usdt=size_usdt,
            leverage=leverage,
            stop_loss_pct=signal.stop_loss_pct,
            take_profit_pct=signal.take_profit_pct,
        )
        self.positions.append(position)
        self.balance -= self._fee(size_usdt)
        return position

    def mark_candle(self, symbol: str, high: float, low: float, close: float):
        for position in list(self.positions):
            if position.symbol != symbol:
                continue
            exit_price = None
            reason = ""
            if position.side == "long":
                sl = position.entry_price * (1 - (position.stop_loss_pct or 0) / 100)
                tp = position.entry_price * (1 + (position.take_profit_pct or 0) / 100)
                if position.stop_loss_pct and low <= sl:
                    exit_price, reason = sl, "stop_loss"
                elif position.take_profit_pct and high >= tp:
                    exit_price, reason = tp, "take_profit"
            else:
                sl = position.entry_price * (1 + (position.stop_loss_pct or 0) / 100)
                tp = position.entry_price * (1 - (position.take_profit_pct or 0) / 100)
                if position.stop_loss_pct and high >= sl:
                    exit_price, reason = sl, "stop_loss"
                elif position.take_profit_pct and low <= tp:
                    exit_price, reason = tp, "take_profit"

            if exit_price is not None:
                self.close_position(position, exit_price, reason)

    def close_position(self, position: PaperPosition, exit_price: float, reason: str) -> PaperTrade:
        fill_price = self._apply_slippage(exit_price, position.side, is_entry=False)
        pnl_pct = (
            (fill_price - position.entry_price) / position.entry_price
            if position.side == "long"
            else (position.entry_price - fill_price) / position.entry_price
        )
        gross_pnl = position.size_usdt * pnl_pct
        fees = self._fee(position.size_usdt) * 2
        slippage = position.size_usdt * (self.slippage_pct / 100)
        pnl = gross_pnl - fees - slippage
        self.balance += pnl
        self.positions.remove(position)
        trade = PaperTrade(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=fill_price,
            size_usdt=position.size_usdt,
            pnl_usdt=pnl,
            fees_usdt=fees,
            slippage_usdt=slippage,
            reason=reason,
        )
        self.trades.append(trade)
        return trade

    def _fee(self, size_usdt: float) -> float:
        return size_usdt * self.fee_rate

    def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
        direction = 1 if side == "long" else -1
        if not is_entry:
            direction *= -1
        return price * (1 + direction * self.slippage_pct / 100)
