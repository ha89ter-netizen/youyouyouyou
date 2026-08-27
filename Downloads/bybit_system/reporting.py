"""Owner-facing trading report built from one canonical source per metric.

Every number here comes from ``trade_log`` rows that the journal only writes
after Bybit has confirmed the closure, plus ``position_snapshots`` for live
unrealized values. That single choice is what prevents the classic double
count: Bybit executions, closed-PnL records, orders and fills all describe the
*same* trade, and summing more than one of them inflates every metric.

Definitions used throughout, so a number in Telegram always means one thing:

``qualifying closed trade``  a ``trade_log`` row with ``status='closed'``, a
    non-null ``pnl_usdt`` and ``closed_at`` inside the reporting period. Rows
    still ``open`` or ``orphaned`` are excluded and counted separately —
    an orphaned trade has an unknown result and must never silently score as a
    loss of zero.
``Realized PnL``    sum of ``pnl_usdt`` over qualifying closed trades. This is
    Bybit's ``closedPnl``, already net of fees, as persisted by the journal.
``Gross Profit``    sum of ``pnl_usdt`` over qualifying closed trades where it
    is positive.
``Gross Loss``      sum of ``pnl_usdt`` over qualifying closed trades where it
    is negative (reported as a negative number).
``Win Rate``        positive-PnL qualifying closed trades / all qualifying
    closed trades * 100. Exactly-zero PnL counts as a non-win, not a win.
``Unrealized PnL``  sum of the newest ``PositionSnapshot.unrealized_pnl`` per
    currently open trade. Unavailable rather than zero when no snapshot exists.
``Best/Worst symbol`` highest/lowest aggregate realized PnL per symbol over the
    same qualifying closed trades.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func

from storage.models import AccountSnapshot, PositionSnapshot, TradeLog
from timeutils import ensure_aware_utc, utcnow

logger = logging.getLogger(__name__)

# Supported reporting periods. The label is printed verbatim in the report so
# the owner never has to guess which window a number covers.
PERIODS = {
    "24h": "rolling 24h",
    "utc_day": "today (UTC day)",
    "run": "this run",
}
DEFAULT_PERIOD = "24h"


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class SymbolResult:
    symbol: str
    realized_pnl: float
    trades: int


@dataclass
class OpenPosition:
    trade_id: int
    symbol: str
    side: str
    unrealized_pnl: Optional[float]
    entry_price: Optional[float]
    snapshot_age_seconds: Optional[float]


@dataclass
class TradingReport:
    """One period's worth of trading facts; ``None`` always means unknown."""

    period_key: str = DEFAULT_PERIOD
    period_label: str = PERIODS[DEFAULT_PERIOD]
    period_start: Any = None
    generated_at: Any = None
    closed_trades: int = 0
    realized_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    wins: int = 0
    win_rate: Optional[float] = None
    fees: float = 0.0
    unresolved_trades: int = 0
    open_positions: list[OpenPosition] = field(default_factory=list)
    open_position_count: Optional[int] = None
    position_state_available: bool = True
    position_state_reason: Optional[str] = None
    unrealized_pnl: Optional[float] = None
    unrealized_unavailable_count: int = 0
    best_symbol: Optional[SymbolResult] = None
    worst_symbol: Optional[SymbolResult] = None
    wallet_balance: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "period": self.period_key, "period_label": self.period_label,
            "period_start": self.period_start, "generated_at": self.generated_at,
            "closed_trades": self.closed_trades, "realized_pnl": self.realized_pnl,
            "gross_profit": self.gross_profit, "gross_loss": self.gross_loss,
            "wins": self.wins, "win_rate": self.win_rate, "fees": self.fees,
            "unresolved_trades": self.unresolved_trades,
            "open_position_count": self.open_position_count,
            "position_state_available": self.position_state_available,
            "unrealized_pnl": self.unrealized_pnl,
            "best_symbol": self.best_symbol.symbol if self.best_symbol else None,
            "worst_symbol": self.worst_symbol.symbol if self.worst_symbol else None,
            "wallet_balance": self.wallet_balance,
        }


class TradingReportBuilder:
    """Compute report metrics once, so every surface shows the same numbers."""

    def __init__(self, db, cfg, run_id: str):
        self.db = db
        self.cfg = cfg
        self.run_id = run_id

    def period_start(self, period: str, now):
        if period == "utc_day":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "run":
            return None  # bounded by run_id instead of time
        return now - timedelta(hours=24)

    def build(self, period: str = DEFAULT_PERIOD) -> TradingReport:
        period = period if period in PERIODS else DEFAULT_PERIOD
        now = utcnow()
        start = self.period_start(period, now)
        report = TradingReport(
            period_key=period, period_label=PERIODS[period],
            period_start=start, generated_at=now,
        )
        session = None
        try:
            session = self.db.get_session()
            self._closed_metrics(session, report, period, start)
            self._open_positions(session, report, now)
            self._wallet(session, report)
        except Exception as exc:
            if session is not None:
                session.rollback()
            # A failed query must degrade into an explicit "unknown", never
            # into a report of zeros that reads like a flat, quiet market.
            report.position_state_available = False
            report.position_state_reason = (
                f"trading state could not be read ({type(exc).__name__})"
            )
            report.open_position_count = None
            logger.exception("Trading report could not be built")
        finally:
            if session is not None:
                session.close()
        return report

    # -- closed trades -----------------------------------------------------

    def _closed_metrics(self, session, report: TradingReport, period, start) -> None:
        query = session.query(TradeLog).filter(TradeLog.status == "closed")
        if period == "run":
            query = query.filter(TradeLog.run_id == self.run_id)
        else:
            query = query.filter(TradeLog.closed_at >= start)
        rows = [row for row in query.all() if row.pnl_usdt is not None]

        report.closed_trades = len(rows)
        report.realized_pnl = sum(_f(row.pnl_usdt) for row in rows)
        report.gross_profit = sum(_f(row.pnl_usdt) for row in rows if _f(row.pnl_usdt) > 0)
        report.gross_loss = sum(_f(row.pnl_usdt) for row in rows if _f(row.pnl_usdt) < 0)
        report.wins = sum(1 for row in rows if _f(row.pnl_usdt) > 0)
        report.win_rate = (report.wins / len(rows) * 100) if rows else None
        report.fees = sum(_f(row.total_fee_usdt) for row in rows)

        # Trades whose financial result is unknown are reported separately
        # instead of being averaged into performance as if they were flat.
        unresolved = session.query(func.count(TradeLog.id)).filter(
            TradeLog.status == "orphaned"
        ).scalar()
        report.unresolved_trades = int(unresolved or 0)

        by_symbol: dict[str, list[float]] = {}
        for row in rows:
            by_symbol.setdefault(row.symbol, []).append(_f(row.pnl_usdt))
        results = [
            SymbolResult(symbol, sum(values), len(values))
            for symbol, values in by_symbol.items()
        ]
        if results:
            report.best_symbol = max(results, key=lambda item: item.realized_pnl)
            report.worst_symbol = min(results, key=lambda item: item.realized_pnl)
            # With a single symbol, "best" and "worst" are the same fact stated
            # twice; showing it once is more honest.
            if report.best_symbol is report.worst_symbol:
                report.worst_symbol = None

    # -- open positions ----------------------------------------------------

    def _open_positions(self, session, report: TradingReport, now) -> None:
        open_rows = session.query(TradeLog).filter(
            TradeLog.status == "open"
        ).order_by(TradeLog.id.asc()).all()
        report.open_position_count = len(open_rows)
        report.position_state_available = True
        total, unavailable = 0.0, 0
        for row in open_rows:
            snapshot = session.query(PositionSnapshot).filter_by(
                trade_log_id=row.id
            ).order_by(PositionSnapshot.observed_at.desc()).first()
            unrealized = (
                _f(snapshot.unrealized_pnl)
                if snapshot is not None and snapshot.unrealized_pnl is not None
                else None
            )
            observed = ensure_aware_utc(snapshot.observed_at) if snapshot else None
            if unrealized is None:
                unavailable += 1
            else:
                total += unrealized
            report.open_positions.append(OpenPosition(
                trade_id=row.id, symbol=row.symbol,
                side="LONG" if row.action == "open_long" else "SHORT",
                unrealized_pnl=unrealized,
                entry_price=_f(row.entry_price) if row.entry_price is not None else None,
                snapshot_age_seconds=(now - observed).total_seconds() if observed else None,
            ))
        report.unrealized_unavailable_count = unavailable
        # Reporting a partial sum as the total would understate exposure, so an
        # incomplete set of snapshots yields no number at all.
        report.unrealized_pnl = (
            total if open_rows and unavailable == 0 else (0.0 if not open_rows else None)
        )

    def _wallet(self, session, report: TradingReport) -> None:
        row = session.query(AccountSnapshot).order_by(
            AccountSnapshot.observed_at.desc()
        ).first()
        if row is not None and row.wallet_balance is not None:
            report.wallet_balance = _f(row.wallet_balance)
