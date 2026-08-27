"""Canonical owner-facing operational state derived from existing durable data.

This module deliberately introduces **no new source of truth**. Every signal is
read back from the same PostgreSQL rows the trading process already writes:
``run_metadata`` heartbeats, ``risk_state`` circuit-breaker causes, the storage
guard, the telemetry outbox and raw market-data timestamps. It never calls
Bybit and never mutates trading state, so it is safe to evaluate from the
supervisor process alongside the trader.

The four states answer the only question an owner actually asks:

``HEALTHY``  everything fresh, new entries permitted;
``DEGRADED`` runtime alive, but an input the strategy depends on is unhealthy;
``PAUSED``   runtime alive and positions still managed, new entries blocked by
             a deterministic gate (circuit breaker, storage guard, safe mode);
``STOPPED``  the runtime itself is not observable, or PostgreSQL is unusable.

Precedence is STOPPED > PAUSED > DEGRADED > HEALTHY: the most restrictive true
statement wins, because that is the one the owner has to act on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func

from storage.durability import StorageGuard
from storage.models import (
    Candle, OrderbookSnapshot, PositionSnapshot, RiskState, RunMetadata,
    TelemetryOutbox, Trade, TradeLog,
)
from timeutils import ensure_aware_utc, utcnow

logger = logging.getLogger(__name__)

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
PAUSED = "PAUSED"
STOPPED = "STOPPED"

_PRECEDENCE = {STOPPED: 3, PAUSED: 2, DEGRADED: 1, HEALTHY: 0}


def is_worse_than(candidate: str, baseline: str) -> bool:
    """True when ``candidate`` is a strictly more restrictive state."""
    return _PRECEDENCE.get(candidate, 0) > _PRECEDENCE.get(baseline, 0)

# Owner-facing one-liners. The technical detail stays in `reasons`.
STATE_SUMMARY = {
    HEALTHY: "Trading normally",
    DEGRADED: "Running with a degraded input",
    PAUSED: "Trading paused — new entries blocked",
    STOPPED: "Not running",
}


def _age_seconds(value, now) -> Optional[float]:
    aware = ensure_aware_utc(value)
    return (now - aware).total_seconds() if aware else None


def _epoch_ms_age_seconds(value, now) -> Optional[float]:
    if value is None:
        return None
    try:
        return now.timestamp() - float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def humanize_age(seconds: Optional[float]) -> str:
    """Render an age the way a person reads it, never as a bare float."""
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s ago"
    hours, remainder = divmod(seconds, 3600)
    if hours < 48:
        return f"{hours}h {remainder // 60:02d}m ago"
    return f"{hours // 24}d {hours % 24}h ago"


@dataclass
class OperationalStatus:
    """One evaluation of the whole system, safe to render or serialise."""

    state: str = HEALTHY
    reasons: list[str] = field(default_factory=list)
    # Owner-facing facts. `None` always means "not known", never "zero".
    run_id: Optional[str] = None
    testnet: bool = True
    collector_healthy: bool = False
    trader_healthy: bool = False
    collector_age_seconds: Optional[float] = None
    trader_age_seconds: Optional[float] = None
    uptime_seconds: Optional[float] = None
    market_data_age_seconds: Optional[float] = None
    market_data_source: Optional[str] = None
    database_available: bool = False
    database_usage_ratio: Optional[float] = None
    entries_allowed: bool = False
    entry_block_reasons: list[str] = field(default_factory=list)
    breaker_causes: dict = field(default_factory=dict)
    operator_paused: bool = False
    open_positions: Optional[int] = None
    position_state_available: bool = False
    position_state_reason: Optional[str] = None
    outbox: dict = field(default_factory=dict)
    observed_at: Any = None

    @property
    def summary(self) -> str:
        return STATE_SUMMARY.get(self.state, self.state)

    def escalate(self, state: str, reason: str) -> None:
        """Record a reason and keep the most restrictive state seen so far."""
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if _PRECEDENCE[state] > _PRECEDENCE[self.state]:
            self.state = state

    def as_dict(self) -> dict:
        return {
            "state": self.state, "summary": self.summary, "reasons": list(self.reasons),
            "run_id": self.run_id, "testnet": self.testnet,
            "collector_healthy": self.collector_healthy,
            "trader_healthy": self.trader_healthy,
            "collector_age_seconds": self.collector_age_seconds,
            "trader_age_seconds": self.trader_age_seconds,
            "uptime_seconds": self.uptime_seconds,
            "market_data_age_seconds": self.market_data_age_seconds,
            "market_data_source": self.market_data_source,
            "database_available": self.database_available,
            "database_usage_ratio": self.database_usage_ratio,
            "entries_allowed": self.entries_allowed,
            "entry_block_reasons": list(self.entry_block_reasons),
            "breaker_causes": dict(self.breaker_causes),
            "operator_paused": self.operator_paused,
            "open_positions": self.open_positions,
            "position_state_available": self.position_state_available,
            "position_state_reason": self.position_state_reason,
            "outbox": dict(self.outbox),
            "observed_at": self.observed_at,
        }


# Cause key used for an explicit owner-requested pause. It is deliberately its
# own key so resolving it can never clear an orphan, daily-loss or protective
# execution cause that the owner has not actually investigated.
OPERATOR_PAUSE_CAUSE = "operator_pause"


class OperationalStatusEvaluator:
    """Evaluate the canonical operational state without touching the exchange."""

    def __init__(self, db, cfg, run_id: str, *, heartbeat_limit_seconds: float = 90.0):
        self.db = db
        self.cfg = cfg
        self.run_id = run_id
        self.heartbeat_limit_seconds = max(30.0, float(heartbeat_limit_seconds))

    def evaluate(self, storage: Optional[dict] = None) -> OperationalStatus:
        now = utcnow()
        status = OperationalStatus(
            run_id=self.run_id, testnet=bool(getattr(self.cfg, "testnet", True)),
            observed_at=now,
        )
        if storage is None:
            storage = StorageGuard(self.db, self.cfg).status()
        status.database_available = bool(storage.get("available"))
        status.database_usage_ratio = storage.get("usage_ratio")

        if not status.database_available:
            # Without PostgreSQL nothing else can be evaluated honestly, and
            # the trading process itself is fail-closed for new entries.
            status.escalate(STOPPED, storage.get("reason") or "PostgreSQL is unavailable")
            status.entry_block_reasons.append("durable storage unavailable")
            return status

        session = self.db.get_session()
        try:
            self._evaluate_runtime(session, status, now)
            self._evaluate_market_data(session, status, now)
            self._evaluate_entry_gates(session, status, storage)
            self._evaluate_positions(session, status, now)
            self._evaluate_outbox(session, status, now)
        finally:
            session.close()
        return status

    # -- runtime -----------------------------------------------------------

    def _evaluate_runtime(self, session, status: OperationalStatus, now) -> None:
        run = session.query(RunMetadata).filter_by(run_id=self.run_id).first()
        if run is None:
            status.escalate(STOPPED, "no runtime metadata for this run")
            return
        status.uptime_seconds = _age_seconds(run.started_at, now)
        status.collector_age_seconds = _age_seconds(run.collector_heartbeat_at, now)
        status.trader_age_seconds = _age_seconds(run.trader_heartbeat_at, now)
        limit = self.heartbeat_limit_seconds
        status.collector_healthy = (
            status.collector_age_seconds is not None
            and status.collector_age_seconds <= limit
        )
        status.trader_healthy = (
            status.trader_age_seconds is not None
            and status.trader_age_seconds <= limit
        )
        # A container that has just started has not had time to emit a first
        # heartbeat. Reporting STOPPED there would be a false alarm on every
        # deployment.
        starting = (
            status.uptime_seconds is not None and status.uptime_seconds < max(120.0, limit)
        )
        if not status.trader_healthy and not starting:
            status.escalate(
                STOPPED,
                "the trading process has not reported in "
                f"{humanize_age(status.trader_age_seconds)}",
            )
        if not status.collector_healthy and not starting:
            status.escalate(
                DEGRADED,
                "the market-data collector has not reported in "
                f"{humanize_age(status.collector_age_seconds)}",
            )

    # -- market data -------------------------------------------------------

    def _evaluate_market_data(self, session, status: OperationalStatus, now) -> None:
        """Freshest observation across the streams entries actually depend on.

        The strategy blocks a symbol on its own stale-data checks; this is the
        *system-wide* view used to tell the owner whether Bybit data is still
        arriving at all.
        """
        newest, source = None, None
        for label, column in (
            ("public trades", func.max(Trade.ts)),
            ("order book", func.max(OrderbookSnapshot.ts)),
        ):
            age = _epoch_ms_age_seconds(session.query(column).scalar(), now)
            if age is not None and (newest is None or age < newest):
                newest, source = age, label
        if newest is None:
            candle = session.query(func.max(Candle.start_time)).scalar()
            age = _age_seconds(candle, now)
            if age is not None:
                newest, source = age, "candles"
        status.market_data_age_seconds = newest
        status.market_data_source = source
        if newest is None:
            status.escalate(DEGRADED, "no market data has been stored yet")
            return
        limit = max(
            60.0, float(getattr(self.cfg, "max_orderbook_age_seconds", 90)) * 3
        )
        if newest > limit:
            status.escalate(
                DEGRADED,
                f"market data from Bybit stopped updating ({humanize_age(newest)})",
            )

    # -- entry gates -------------------------------------------------------

    def _evaluate_entry_gates(self, session, status: OperationalStatus, storage) -> None:
        risk = session.query(RiskState).filter_by(id=1).first()
        causes = dict((risk.circuit_breaker_causes or {}) if risk else {})
        status.breaker_causes = causes
        status.operator_paused = OPERATOR_PAUSE_CAUSE in causes
        blocked = []
        if causes:
            blocked.append("circuit breaker")
        if not storage.get("entry_allowed", True):
            blocked.append(storage.get("reason") or "storage capacity")
        if not getattr(self.cfg, "trading_enabled", True):
            blocked.append("safe mode (TRADING_ENABLED=false)")
        status.entry_block_reasons = blocked
        status.entries_allowed = not blocked
        if blocked:
            status.escalate(PAUSED, "new entries are blocked: " + ", ".join(blocked))

    # -- positions ---------------------------------------------------------

    def _evaluate_positions(self, session, status: OperationalStatus, now) -> None:
        """Count open positions, or say plainly that the count is unknown.

        A failed query must never be rendered as ``0 positions``: the owner
        would read an unavailable dashboard as a flat book.
        """
        try:
            status.open_positions = int(
                session.query(func.count(TradeLog.id)).filter(
                    TradeLog.status == "open"
                ).scalar() or 0
            )
            status.position_state_available = True
        except Exception as exc:
            session.rollback()
            status.open_positions = None
            status.position_state_available = False
            status.position_state_reason = (
                f"position state could not be read ({type(exc).__name__})"
            )
            status.escalate(DEGRADED, status.position_state_reason)
            return
        if not status.open_positions:
            return
        newest = session.query(func.max(PositionSnapshot.observed_at)).scalar()
        age = _age_seconds(newest, now)
        # Snapshots are written by the trader; a stale one means the numbers
        # shown next to a position are old, which the owner must be told.
        if age is None or age > max(300.0, self.heartbeat_limit_seconds * 4):
            status.position_state_reason = (
                "position values were last refreshed " + humanize_age(age)
            )

    # -- durability --------------------------------------------------------

    def _evaluate_outbox(self, session, status: OperationalStatus, now) -> None:
        counts = dict(
            session.query(TelemetryOutbox.status, func.count(TelemetryOutbox.id))
            .group_by(TelemetryOutbox.status).all()
        )
        status.outbox = {str(key): int(value) for key, value in counts.items()}
        dead = int(status.outbox.get("dead_letter", 0))
        if dead:
            status.escalate(
                DEGRADED, f"{dead} telemetry events could not be persisted"
            )
        oldest = session.query(func.min(TelemetryOutbox.created_at)).filter(
            TelemetryOutbox.status.in_(("pending", "failed"))
        ).scalar()
        age = _age_seconds(oldest, now)
        if age is not None and age > 1800:
            status.escalate(
                DEGRADED,
                "durable telemetry is backing up (oldest unsent " + humanize_age(age) + ")",
            )
