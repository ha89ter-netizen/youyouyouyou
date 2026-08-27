"""Deterministic control seam between Telegram and the trading process.

Telegram never changes trading state. It *requests* a change by appending a row
to ``operator_control_commands``; the trader consumes that row inside its own
cycle, re-validates every precondition against live state and only then calls
the Risk Manager.

Two independent reasons this indirection is mandatory, not ceremony:

1. **Correctness.** ``OperatorMonitor`` runs in the supervisor process while the
   Risk Manager lives in the trader process and holds ``risk_state`` in memory.
   A Telegram callback that wrote ``risk_state`` directly would be silently
   overwritten by the trader's next persist.
2. **Safety.** Authorization proves *who* asked. It says nothing about whether
   resuming is safe. The checks below run in the process that actually knows
   the current positions, protection and market-data freshness.

A future natural-language assistant gets no additional power: it can only
produce one of these same explicit commands, which still pass every check here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from operational_status import (
    DEGRADED, HEALTHY, OPERATOR_PAUSE_CAUSE, OperationalStatus, humanize_age,
)
from storage.models import OperatorControlCommand
from timeutils import utcnow

logger = logging.getLogger(__name__)

PAUSE = "pause"
RESUME = "resume"
SUPPORTED_COMMANDS = (PAUSE, RESUME)

PENDING, APPLIED, REJECTED = "pending", "applied", "rejected"


@dataclass(frozen=True)
class ControlOutcome:
    command_id: int
    command: str
    state: str
    outcome: str


class OperatorControlStore:
    """Append-only request log; the trader owns every state transition."""

    def __init__(self, db):
        self.db = db

    def request(self, command: str, *, requested_by: str, run_id: Optional[str] = None,
                details: Optional[dict] = None) -> Optional[int]:
        if command not in SUPPORTED_COMMANDS:
            raise ValueError(f"unsupported operator command: {command!r}")
        session = self.db.get_session()
        try:
            # One outstanding request at a time: a double tap on an inline
            # button must not queue two resumes.
            existing = session.query(OperatorControlCommand).filter_by(
                command=command, state=PENDING
            ).first()
            if existing is not None:
                return int(existing.id)
            row = OperatorControlCommand(
                command=command, requested_by=str(requested_by)[:64],
                requested_at=utcnow(), run_id=run_id, state=PENDING,
                details=dict(details or {}),
            )
            session.add(row)
            session.commit()
            return int(row.id)
        except Exception:
            session.rollback()
            logger.exception("Operator control request could not be persisted")
            return None
        finally:
            session.close()

    def pending(self) -> list[OperatorControlCommand]:
        session = self.db.get_session()
        try:
            return list(
                session.query(OperatorControlCommand)
                .filter_by(state=PENDING)
                .order_by(OperatorControlCommand.id.asc()).all()
            )
        finally:
            session.close()

    def finish(self, command_id: int, state: str, outcome: str) -> None:
        session = self.db.get_session()
        try:
            row = session.query(OperatorControlCommand).filter_by(id=command_id).first()
            if row is None or row.state != PENDING:
                return
            row.state = state
            row.outcome = outcome[:2000]
            row.processed_at = utcnow()
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Operator control outcome could not be persisted")
        finally:
            session.close()

    def drain_processed(self, after_id: int) -> tuple[list[ControlOutcome], int]:
        """Return terminal outcomes the owner has not been told about yet."""
        session = self.db.get_session()
        try:
            rows = session.query(OperatorControlCommand).filter(
                OperatorControlCommand.id > int(after_id or 0),
                OperatorControlCommand.state.in_((APPLIED, REJECTED)),
            ).order_by(OperatorControlCommand.id.asc()).all()
            outcomes = [
                ControlOutcome(int(row.id), row.command, row.state, row.outcome or "")
                for row in rows
            ]
            highest = max([int(row.id) for row in rows] + [int(after_id or 0)])
            return outcomes, highest
        finally:
            session.close()


def resume_preconditions(status: OperationalStatus) -> list[str]:
    """Deterministic reasons a resume must be refused, evaluated live.

    Returning an empty list is the only thing that may unblock entries. The
    caller re-checks the remaining circuit-breaker causes separately, because
    an owner pause must never clear an orphan or risk-envelope cause.
    """
    failures = []
    if not status.database_available:
        failures.append("PostgreSQL is not available")
    if status.state not in (HEALTHY, DEGRADED):
        failures.append(f"runtime state is {status.state}, not running normally")
    if not status.trader_healthy:
        failures.append(
            f"the trading process last reported {humanize_age(status.trader_age_seconds)}"
        )
    if not status.collector_healthy:
        failures.append(
            f"the collector last reported {humanize_age(status.collector_age_seconds)}"
        )
    if status.market_data_age_seconds is None:
        failures.append("no market data has been stored")
    elif status.market_data_age_seconds > 300:
        failures.append(
            f"market data is stale ({humanize_age(status.market_data_age_seconds)})"
        )
    if not status.position_state_available:
        failures.append("open-position state could not be read")
    if int(status.outbox.get("dead_letter", 0) or 0):
        failures.append(
            f"{status.outbox['dead_letter']} telemetry events failed to persist"
        )
    blocking = {
        key: value for key, value in (status.breaker_causes or {}).items()
        if key != OPERATOR_PAUSE_CAUSE
    }
    if blocking:
        failures.append(
            "the circuit breaker still has unresolved causes: "
            + ", ".join(sorted(blocking))
        )
    return failures


class OperatorControlApplier:
    """Applies owner requests inside the trading process, after re-validation."""

    def __init__(self, store: OperatorControlStore, risk_manager,
                 status_provider: Callable[[], OperationalStatus]):
        self.store = store
        self.risk_manager = risk_manager
        self.status_provider = status_provider

    def apply_pending(self) -> list[ControlOutcome]:
        results = []
        for row in self.store.pending():
            command_id, command = int(row.id), row.command
            try:
                state, outcome = self._apply(command)
            except Exception as exc:
                # A control failure must never take down the trading cycle.
                state, outcome = REJECTED, f"internal error ({type(exc).__name__})"
                logger.exception("Operator command %s failed", command)
            self.store.finish(command_id, state, outcome)
            results.append(ControlOutcome(command_id, command, state, outcome))
        return results

    def _apply(self, command: str) -> tuple[str, str]:
        if command == PAUSE:
            self.risk_manager.trip_circuit_breaker(
                "trading paused by the owner from Telegram",
                sticky=True, cause=OPERATOR_PAUSE_CAUSE,
                category="operator_pause",
            )
            return APPLIED, (
                "Trading is paused. Open positions are still managed and "
                "protected; only new entries are blocked."
            )
        if command == RESUME:
            status = self.status_provider()
            failures = resume_preconditions(status)
            if failures:
                return REJECTED, (
                    "Resume refused — safety checks did not pass:\n"
                    + "\n".join(f"• {item}" for item in failures)
                )
            if not self.risk_manager.resolve_breaker_cause(OPERATOR_PAUSE_CAUSE):
                return APPLIED, "Trading was not paused by you; nothing to resume."
            return APPLIED, "Safety checks passed. New entries are allowed again."
        return REJECTED, f"unsupported command {command}"
