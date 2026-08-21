"""Durability primitives for exchange mutations and bounded PostgreSQL operation.

This module never calls Bybit.  It only decides whether a *new* mutation may be
started and persists its identity before the execution layer is invoked.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from storage.models import EntryIntent, TelemetryOutbox
from storage.trade_memory import safe_float, safe_json, sanitize_text, stable_json_dumps
from timeutils import ensure_aware_utc, utcnow

logger = logging.getLogger(__name__)


ENTRY_TRANSITIONS = {
    "prepared": {"submitted"},
    "submitted": {"accepted", "rejected"},
    "accepted": {"partially_filled", "filled", "journaled", "rejected"},
    "partially_filled": {"partially_filled", "filled", "journaled"},
    "filled": {"journaled"},
    "journaled": {"closed"},
    "closed": {"reconciled"},
    "reconciled": set(),
    "rejected": set(),
}


def _digest(payload: Any) -> str:
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedIntent:
    intent_id: str
    order_link_id: str
    state: str
    created: bool


class EntryIntentStore:
    """Idempotent entry state machine stored before any exchange request."""

    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg

    def prepare(self, *, evaluation_id: str, symbol: str, side: str,
                requested_quantity: Optional[float], requested_notional: Optional[float],
                proposed_entry: Optional[float], proposed_stop_loss: Optional[float],
                proposed_take_profit: Optional[float], policy_epoch: int,
                config_hash: str, structured_payload: dict) -> PreparedIntent:
        identity = {
            "run_id": self.cfg.run_id, "evaluation_id": evaluation_id,
            "symbol": symbol, "side": side,
        }
        intent_id = _digest(identity)
        # 31 chars, within Bybit's orderLinkId limit, stable across restart.
        order_link_id = "intent-" + intent_id[:24]
        session = self.db.get_session()
        try:
            existing = session.query(EntryIntent).filter_by(intent_id=intent_id).first()
            if existing:
                return PreparedIntent(intent_id, existing.order_link_id, existing.state, False)
            row = EntryIntent(
                intent_id=intent_id, run_id=self.cfg.run_id, evaluation_id=evaluation_id,
                symbol=symbol, side=side, order_link_id=order_link_id, state="prepared",
                requested_quantity=safe_float(requested_quantity),
                requested_notional=safe_float(requested_notional),
                proposed_entry=safe_float(proposed_entry),
                proposed_stop_loss=safe_float(proposed_stop_loss),
                proposed_take_profit=safe_float(proposed_take_profit),
                policy_epoch=policy_epoch, config_hash=config_hash,
                structured_payload=safe_json(structured_payload),
            )
            session.add(row)
            session.commit()
            return PreparedIntent(intent_id, order_link_id, "prepared", True)
        except IntegrityError:
            session.rollback()
            existing = session.query(EntryIntent).filter_by(intent_id=intent_id).first()
            if existing is None:
                # Only the expected concurrent duplicate is idempotent.  A
                # constraint failure caused by malformed input must remain
                # visible and must never be reported as a prepared intent.
                raise
            return PreparedIntent(intent_id, existing.order_link_id, existing.state, False)
        finally:
            session.close()

    def transition(self, intent_id: str, new_state: str, **values) -> bool:
        session = self.db.get_session()
        try:
            row = session.query(EntryIntent).filter_by(intent_id=intent_id).first()
            if row is None:
                raise RuntimeError(f"entry intent {intent_id} is missing")
            if row.state == new_state:
                changed = False
                for name in (
                    "exchange_order_id", "filled_quantity", "weighted_entry",
                    "trade_log_id", "last_error",
                ):
                    if name in values and getattr(row, name) != values[name]:
                        setattr(row, name, values[name])
                        changed = True
                if changed:
                    session.commit()
                return changed
            allowed = ENTRY_TRANSITIONS.get(row.state, set())
            if new_state not in allowed:
                raise RuntimeError(f"invalid entry intent transition {row.state} -> {new_state}")
            row.state = new_state
            now = utcnow()
            stamp = {
                "submitted": "submitted_at", "accepted": "accepted_at",
                "partially_filled": "filled_at", "filled": "filled_at",
                "journaled": "journaled_at", "closed": "closed_at",
                "reconciled": "reconciled_at",
                "rejected": "rejected_at",
            }.get(new_state)
            if stamp and getattr(row, stamp) is None:
                setattr(row, stamp, now)
            for name in (
                "exchange_order_id", "filled_quantity", "weighted_entry",
                "trade_log_id", "last_error",
            ):
                if name in values:
                    setattr(row, name, values[name])
            if new_state == "journaled" and row.trade_log_id is None:
                from storage.models import TradeLog
                linked = session.query(TradeLog.id).filter_by(
                    order_link_id=row.order_link_id
                ).first()
                row.trade_log_id = linked[0] if linked else None
            session.commit()
            return True
        finally:
            session.close()

    def unresolved(self) -> list[dict]:
        session = self.db.get_session()
        try:
            rows = session.query(EntryIntent).filter(
                EntryIntent.state.notin_(("reconciled", "rejected"))
            ).all()
            return [{
                "intent_id": row.intent_id, "order_link_id": row.order_link_id,
                "run_id": row.run_id, "symbol": row.symbol, "side": row.side,
                "state": row.state, "exchange_order_id": row.exchange_order_id,
                "requested_quantity": safe_float(row.requested_quantity),
                "requested_notional": safe_float(row.requested_notional),
                "proposed_entry": safe_float(row.proposed_entry),
                "proposed_stop_loss": safe_float(row.proposed_stop_loss),
                "proposed_take_profit": safe_float(row.proposed_take_profit),
                "filled_quantity": safe_float(row.filled_quantity),
                "weighted_entry": safe_float(row.weighted_entry),
                "trade_log_id": row.trade_log_id,
                "structured_payload": row.structured_payload or {},
            } for row in rows]
        finally:
            session.close()

    def transition_by_order_link(self, order_link_id: str, new_state: str, **values) -> bool:
        session = self.db.get_session()
        try:
            row = session.query(EntryIntent.intent_id).filter_by(
                order_link_id=order_link_id
            ).first()
            intent_id = row[0] if row else None
        finally:
            session.close()
        return self.transition(intent_id, new_state, **values) if intent_id else False

    def blocking_intent(self, symbol: Optional[str] = None) -> Optional[dict]:
        """Return durable exchange-ambiguity that must block another new entry."""
        session = self.db.get_session()
        try:
            query = session.query(EntryIntent).filter(EntryIntent.state.in_((
                "submitted", "accepted", "partially_filled", "filled",
            )))
            if symbol:
                query = query.filter(EntryIntent.symbol == symbol)
            row = query.order_by(EntryIntent.prepared_at).first()
            return ({"intent_id": row.intent_id, "order_link_id": row.order_link_id,
                     "symbol": row.symbol, "state": row.state}
                    if row else None)
        finally:
            session.close()


class DurableOutbox:
    """PostgreSQL-backed bounded retry queue with deterministic event keys."""

    def __init__(self, db, run_id: str, *, max_attempts: int = 8,
                 base_backoff_seconds: float = 1.0):
        self.db = db
        self.run_id = run_id
        self.max_attempts = max(1, max_attempts)
        self.base_backoff_seconds = max(0.1, base_backoff_seconds)

    def enqueue(self, event_type: str, payload: dict, *, event_key: Optional[str] = None) -> str:
        clean = safe_json(payload, string_limit=None)
        key = event_key or _digest({"run": self.run_id, "type": event_type, "payload": clean})
        session = self.db.get_session()
        try:
            if session.query(TelemetryOutbox).filter_by(event_key=key).first() is None:
                session.add(TelemetryOutbox(
                    event_key=key, run_id=self.run_id, event_type=event_type,
                    payload=clean, status="pending", attempts=0,
                    next_attempt_at=utcnow(),
                ))
                session.commit()
            return key
        except IntegrityError:
            session.rollback()
            return key
        finally:
            session.close()

    def pending(self, limit: int = 100) -> list[TelemetryOutbox]:
        session = self.db.get_session()
        try:
            rows = session.query(TelemetryOutbox).filter(
                TelemetryOutbox.status == "pending",
                TelemetryOutbox.next_attempt_at <= utcnow(),
            ).order_by(TelemetryOutbox.id).limit(max(1, min(limit, 1000))).all()
            for row in rows:
                session.expunge(row)
            return rows
        finally:
            session.close()

    def status(self, event_key: str) -> Optional[str]:
        session = self.db.get_session()
        try:
            row = session.query(TelemetryOutbox.status).filter_by(event_key=event_key).first()
            return row[0] if row else None
        finally:
            session.close()

    def deliver(self, row_id: int, handler) -> bool:
        session = self.db.get_session()
        try:
            row = session.query(TelemetryOutbox).filter_by(id=row_id).first()
            if row is None or row.status == "delivered":
                return False
            try:
                handler(session, row.event_type, row.payload, row.event_key)
                row.status = "delivered"
                row.delivered_at = utcnow()
                row.last_error = None
                session.commit()
                return True
            except Exception as exc:
                session.rollback()
                row = session.query(TelemetryOutbox).filter_by(id=row_id).one()
                row.attempts += 1
                row.last_error = sanitize_text(exc, 4000)
                if row.attempts >= self.max_attempts:
                    row.status = "dead_letter"
                else:
                    delay = min(300.0, self.base_backoff_seconds * (2 ** (row.attempts - 1)))
                    row.next_attempt_at = utcnow() + timedelta(seconds=delay)
                session.commit()
                return False
        finally:
            session.close()

    def metrics(self) -> dict:
        """Small operational summary; no payloads are loaded into memory."""
        session = self.db.get_session()
        try:
            counts = dict(session.query(
                TelemetryOutbox.status, func.count(TelemetryOutbox.id)
            ).group_by(TelemetryOutbox.status).all())
            oldest = session.query(func.min(TelemetryOutbox.created_at)).filter(
                TelemetryOutbox.status == "pending"
            ).scalar()
            age = (
                max(0.0, (utcnow() - ensure_aware_utc(oldest)).total_seconds())
                if oldest is not None else None
            )
            return {
                "pending": int(counts.get("pending", 0)),
                "delivered": int(counts.get("delivered", 0)),
                "failed": int(counts.get("dead_letter", 0)),
                "oldest_pending_age_seconds": age,
            }
        finally:
            session.close()

    def cleanup_delivered(self, *, retention_hours: int, batch_size: int = 1000) -> dict:
        """Delete one committed batch of old confirmed deliveries only."""
        retention_hours = max(0, int(retention_hours))
        batch_size = max(1, min(int(batch_size), 10_000))
        cutoff = utcnow() - timedelta(hours=retention_hours)
        session = self.db.get_session()
        try:
            ids = [row[0] for row in session.query(TelemetryOutbox.id).filter(
                TelemetryOutbox.status == "delivered",
                TelemetryOutbox.delivered_at.isnot(None),
                TelemetryOutbox.delivered_at < cutoff,
            ).order_by(TelemetryOutbox.id).limit(batch_size).all()]
            if ids:
                session.query(TelemetryOutbox).filter(
                    TelemetryOutbox.id.in_(ids),
                    TelemetryOutbox.status == "delivered",
                ).delete(synchronize_session=False)
            session.commit()
            return {"deleted": len(ids), "cutoff": cutoff.isoformat()}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class StorageGuard:
    """Read-only capacity gate for new entries; open-position management is unaffected."""

    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg

    def status(self) -> dict:
        session = None
        try:
            session = self.db.get_session()
            session.execute(text("SELECT 1"))
            size = None
            if self.db.engine.dialect.name == "postgresql":
                size = int(session.execute(
                    text("SELECT pg_database_size(current_database())")
                ).scalar_one())
            maximum = int(getattr(self.cfg, "storage_max_database_bytes", 0) or 0)
            ratio = (size / maximum) if size is not None and maximum > 0 else None
            threshold = float(getattr(self.cfg, "storage_entry_block_ratio", 0.85))
            return {
                "available": True, "database_bytes": size, "maximum_bytes": maximum or None,
                "usage_ratio": ratio, "entry_allowed": ratio is None or ratio < threshold,
                "reason": None if ratio is None or ratio < threshold else
                    f"database usage {ratio:.1%} reached safety threshold {threshold:.1%}",
            }
        except Exception as exc:
            return {
                "available": False, "database_bytes": None, "maximum_bytes": None,
                "usage_ratio": None, "entry_allowed": False,
                "reason": f"durable database unavailable: {type(exc).__name__}",
            }
        finally:
            if session is not None:
                session.close()


def apply_high_frequency_retention(engine, cfg) -> dict[str, int]:
    """Delete only raw technical rows older than configured research windows."""
    policies = {
        "trades": ("ts", int(getattr(cfg, "raw_trades_retention_hours", 168))),
        "orderbook_snapshots": ("ts", int(getattr(cfg, "orderbook_retention_hours", 168))),
        "liquidations": ("ts", int(getattr(cfg, "liquidations_retention_hours", 720))),
    }
    now_ms = int(utcnow().timestamp() * 1000)
    deleted: dict[str, int] = {}
    batch_size = max(100, min(int(getattr(cfg, "retention_delete_batch_size", 10_000)), 100_000))
    max_rows = max(batch_size, min(
        int(getattr(cfg, "retention_max_rows_per_run", 100_000)), 1_000_000
    ))
    with engine.begin() as conn:
        oldest_open = conn.execute(text(
            "SELECT MIN(opened_at) FROM trade_log WHERE status = 'open'"
        )).scalar_one_or_none()
        oldest_open_ms = None
        if oldest_open is not None:
            if isinstance(oldest_open, str):
                try:
                    oldest_open = datetime.fromisoformat(oldest_open)
                except ValueError:
                    oldest_open = None
            aware = ensure_aware_utc(oldest_open)
            oldest_open_ms = int(aware.timestamp() * 1000) if aware else None
    for table, (column, hours) in policies.items():
        if hours <= 0:
            continue
        cutoff = now_ms - hours * 3_600_000
        # Raw ticks remain available for the complete lifetime of every
        # still-open trade, even if it outlives the normal retention window.
        if oldest_open_ms is not None:
            cutoff = min(cutoff, oldest_open_ms)
        total = 0
        while total < max_rows:
            with engine.begin() as conn:
                if engine.dialect.name == "postgresql":
                    statement = text(
                        f"DELETE FROM {table} WHERE ctid IN "
                        f"(SELECT ctid FROM {table} WHERE {column} < :cutoff LIMIT :batch)"
                    )
                    result = conn.execute(statement, {"cutoff": cutoff, "batch": batch_size})
                else:
                    # SQLite is used only in tests/local development and does
                    # not expose PostgreSQL ctid.
                    result = conn.execute(
                        text(f"DELETE FROM {table} WHERE {column} < :cutoff"),
                        {"cutoff": cutoff},
                    )
            count = max(0, result.rowcount or 0)
            total += count
            if count < batch_size or engine.dialect.name != "postgresql":
                break
        deleted[table] = total
    return deleted
