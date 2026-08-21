"""Durable, run-scoped research telemetry. No method mutates exchange state."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import fields
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from storage.migrations import DATABASE_SCHEMA_VERSION, MIGRATION_VERSION
from storage.durability import DurableOutbox
from storage.models import (
    AccountSnapshot,
    DecisionEvent,
    NormalizedExecution,
    OperationalHealthEvent,
    PositionSnapshot,
    RejectionEvent,
    RunPolicyEpoch,
    Trade,
    TradeExcursion,
    TradeExitEvent,
    TradeExchangeOrder,
    TradeLog,
    TradeProtectionEvent,
    TradingRun,
)
from storage.trade_memory import safe_float, safe_json, sanitize_text, stable_json_dumps
from timeutils import ensure_aware_utc, from_epoch_ms, to_epoch_ms, utcnow

logger = logging.getLogger(__name__)

SECRET_FIELDS = {"api_key", "api_secret", "openai_api_key"}
SECRET_ENV_NAMES = {"BYBIT_API_KEY", "BYBIT_API_SECRET", "OPENAI_API_KEY"}
CONFIG_ENV_NAMES = (
    "RUN_ID", "COMMIT_SHA", "RUNTIME_MODE", "BYBIT_API_KEY", "BYBIT_API_SECRET",
    "OPENAI_API_KEY", "AI_MODEL", "BYBIT_TESTNET", "TRADING_ENABLED", "PAPER_TRADING",
    "SYMBOLS", "DATABASE_URL", "PRIMARY_INTERVAL", "CONFIRMATION_INTERVAL",
    "HIGHER_INTERVAL", "DECISION_INTERVAL_SEC", "MIN_OPEN_CONFIDENCE", "MIN_RR",
    "MIN_DECISION_MARGIN", "MIN_CONFIRMING_FAMILIES", "MAX_NEW_POSITIONS_PER_CYCLE",
    "MIN_SECONDS_BETWEEN_ENTRIES", "RISK_PER_TRADE_PCT", "MAX_POSITION_USDT",
    "MAX_LEVERAGE", "MAX_DAILY_LOSS_PCT", "MAX_OPEN_POSITIONS", "MAX_DAILY_TRADES",
    "MAX_TRADES_PER_SYMBOL", "COOLDOWN_MINUTES", "DEFAULT_STOP_LOSS_PCT",
    "DEFAULT_TP_RR", "MAX_VOLATILITY_ATR_PCT", "MAX_SPREAD_PCT",
    "MAX_LONG_FUNDING_RATE", "MAX_SHORT_FUNDING_RATE_ABS", "TREND_FILTER_ENABLED",
    "TREND_FILTER_REVERSAL_CONFIDENCE", "MAX_CANDLE_AGE_MINUTES",
    "MAX_ORDERBOOK_AGE_SECONDS", "MAX_FUNDING_AGE_MINUTES",
    "MAX_OPEN_INTEREST_AGE_MINUTES", "MAX_TRADE_FLOW_AGE_SECONDS",
    "MAX_SAME_DIRECTION_PER_GROUP", "TRAILING_STOP_ENABLED", "TRAILING_ACTIVATION_PCT",
    "TRAILING_DISTANCE_PCT", "TIME_RANGE_TIGHTENING_ENABLED",
    "TIME_RANGE_TIGHTENING_AFTER_SECONDS", "TIME_RANGE_TIGHTENING_FACTOR",
    "TIME_RANGE_SECOND_TIGHTENING_AFTER_SECONDS", "TIME_RANGE_SECOND_TIGHTENING_FACTOR",
    "LOG_LEVEL",
    "TELEMETRY_ACCOUNT_INTERVAL_SEC", "TELEMETRY_POSITION_INTERVAL_SEC",
    "TELEMETRY_RETRY_ATTEMPTS", "TELEMETRY_RETRY_BASE_SECONDS",
    "TELEMETRY_OUTBOX_MAX_ATTEMPTS", "TELEMETRY_OUTBOX_DELIVERED_RETENTION_HOURS",
    "TELEMETRY_OUTBOX_CLEANUP_BATCH_SIZE", "TELEMETRY_OUTBOX_CLEANUP_MAX_BATCHES",
    "HEALTH_EVENT_DEDUP_WINDOW_SECONDS", "HEALTH_CONDITION_REMINDER_SECONDS",
    "POSITION_CLOSE_VISIBILITY_GRACE_SECONDS",
    "STORAGE_MAX_DATABASE_BYTES",
    "STORAGE_ENTRY_BLOCK_RATIO", "STORAGE_MONITOR_INTERVAL_SEC", "RAW_TRADES_RETENTION_HOURS",
    "ORDERBOOK_RETENTION_HOURS", "LIQUIDATIONS_RETENTION_HOURS",
    "RETENTION_DELETE_BATCH_SIZE", "RETENTION_MAX_ROWS_PER_RUN",
    "PROTECTIVE_TRIGGER_BY", "SLIPPAGE_ELEVATED_PCT", "SLIPPAGE_ANOMALOUS_PCT",
    "SLIPPAGE_ELEVATED_R", "SLIPPAGE_ANOMALOUS_R", "MAX_REALIZED_LOSS_R",
    "COLLECTOR_RESTART_INITIAL_SECONDS", "COLLECTOR_RESTART_MAX_SECONDS",
    "COLLECTOR_RESTART_STABLE_RESET_SECONDS",
    "WS_RECONNECT_INITIAL_SECONDS", "WS_RECONNECT_MAX_SECONDS",
    "WS_RECONNECT_JITTER_RATIO", "WS_RECONNECT_STABLE_RESET_SECONDS",
    "WS_RECONNECT_RESTART_AFTER_SECONDS",
    "STRATEGY_VERSION", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_SERVICE_NAME",
    "RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID",
)

STRATEGY_KEYS = {
    "ai_model", "symbols", "primary_interval", "confirmation_interval", "higher_interval",
    "decision_interval_sec", "min_open_confidence", "min_rr", "min_decision_margin",
    "min_confirming_families", "max_new_positions_per_cycle", "min_seconds_between_entries",
}
RISK_KEYS = {
    "risk_per_trade_pct", "max_position_usdt", "max_leverage", "max_daily_loss_pct",
    "max_open_positions", "max_daily_trades", "max_trades_per_symbol", "cooldown_minutes",
    "max_same_direction_per_group",
}
EXIT_KEYS = {
    "default_stop_loss_pct", "default_take_profit_rr", "trailing_stop_enabled",
    "trailing_activation_pct", "trailing_distance_pct", "time_range_tightening_enabled",
    "time_range_tightening_after_seconds", "time_range_tightening_factor",
    "time_range_second_tightening_after_seconds", "time_range_second_tightening_factor",
}
FILTER_KEYS = {
    "max_volatility_atr_pct", "max_spread_pct", "max_long_funding_rate",
    "max_short_funding_rate_abs", "trend_filter_enabled",
    "trend_filter_reversal_confidence", "max_candle_age_minutes",
    "max_orderbook_age_seconds", "max_funding_age_minutes",
    "max_open_interest_age_minutes", "max_trade_flow_age_seconds",
}


def _sha(value: Any) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def _safe_database_url(raw: str) -> str:
    try:
        return make_url(raw).render_as_string(hide_password=True)
    except Exception:
        return "configured" if raw else "missing"


def dependency_fingerprint() -> str:
    packages = sorted(
        f"{dist.metadata.get('Name', '')}=={dist.version}"
        for dist in importlib.metadata.distributions()
    )
    return hashlib.sha256("\n".join(packages).encode("utf-8")).hexdigest()


def effective_config_document(cfg) -> dict:
    values = {}
    for field in fields(cfg):
        name = field.name
        value = getattr(cfg, name)
        if name in SECRET_FIELDS:
            values[name] = "***" if value else None
        elif name == "db_url":
            values[name] = _safe_database_url(value)
        else:
            values[name] = safe_json(value)
    environment = {}
    for name in CONFIG_ENV_NAMES:
        if name not in os.environ:
            continue
        if name in SECRET_ENV_NAMES:
            environment[name] = "***"
        elif name == "DATABASE_URL":
            environment[name] = _safe_database_url(os.environ[name])
        else:
            environment[name] = sanitize_text(os.environ[name], 2000)
    return {
        "resolved": values,
        "strategy": {key: values.get(key) for key in sorted(STRATEGY_KEYS)},
        "risk": {key: values.get(key) for key in sorted(RISK_KEYS)},
        "exit": {key: values.get(key) for key in sorted(EXIT_KEYS)},
        "filters": {key: values.get(key) for key in sorted(FILTER_KEYS)},
        "timeframes": {
            "primary": values.get("primary_interval"),
            "confirmation": values.get("confirmation_interval"),
            "higher": values.get("higher_interval"),
        },
        "environment": environment,
    }


def config_hash(cfg) -> str:
    return _sha(effective_config_document(cfg))


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if any(part in {"__pycache__", ".runtime"} for part in path.parts):
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git_identity(root: Path) -> dict:
    def command(*args):
        try:
            return subprocess.check_output(
                ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    return {
        "commit": command("rev-parse", "HEAD"),
        "branch": command("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(command("status", "--porcelain", "--", str(root))),
    }


def _config_diff(old: Any, new: Any, prefix: str = "") -> dict:
    if isinstance(old, dict) and isinstance(new, dict):
        result = {}
        for key in sorted(set(old) | set(new)):
            child = f"{prefix}.{key}" if prefix else key
            result.update(_config_diff(old.get(key), new.get(key), child))
        return result
    if old != new:
        return {prefix: {"old": old, "new": new}}
    return {}


class TelemetryStore:
    def __init__(
        self, db, cfg, *, owner_run_id: Optional[str] = None,
        processing_run_id: Optional[str] = None,
    ):
        self.db = db
        self.cfg = cfg
        self.run_id = owner_run_id or cfg.run_id
        self.processing_run_id = processing_run_id or cfg.run_id
        self._owner_stores: dict[str, "TelemetryStore"] = {}
        self.outbox = DurableOutbox(
            db, self.run_id,
            max_attempts=getattr(cfg, "telemetry_outbox_max_attempts", 8),
            base_backoff_seconds=getattr(cfg, "telemetry_retry_base_seconds", 0.25),
        )
        # Bounded emergency breadcrumbs only. Critical payloads themselves use
        # PostgreSQL outbox; this deque can never grow without limit.
        self._pending_health = deque(maxlen=100)
        self._last_account_snapshot_monotonic = 0.0
        self._last_position_snapshot_monotonic = 0.0
        self._health_lock = threading.Lock()
        self._health_state: dict[tuple, dict] = {}
        self._health_conditions: dict[tuple, dict] = {}
        self._monotonic = time.monotonic
        self._outbox_maintenance_lock = threading.Lock()
        self._outbox_maintenance_thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        """No durable run means telemetry is deliberately inactive."""
        return bool(self.run_id)

    def _for_owner(self, owner_run_id: Optional[str]) -> "TelemetryStore":
        owner = owner_run_id or self.processing_run_id
        if owner == self.run_id:
            return self
        if owner not in self._owner_stores:
            self._owner_stores[owner] = TelemetryStore(
                self.db, self.cfg, owner_run_id=owner,
                processing_run_id=self.processing_run_id,
            )
        return self._owner_stores[owner]

    def _trade_owner(self, trade: Any) -> Optional[str]:
        owner = self._trade_value(trade, "run_id")
        if owner:
            return owner
        order_link_id = self._trade_value(trade, "order_link_id")
        if not order_link_id:
            return None
        session = self.db.get_session()
        try:
            row = session.query(TradeLog.run_id).filter_by(
                order_link_id=order_link_id
            ).first()
            return row[0] if row else None
        finally:
            session.close()

    def _write(self, operation, *, retries: Optional[int] = None, raise_on_failure: bool = False):
        if not self.enabled:
            return False
        retries = max(1, min(
            int(retries or getattr(self.cfg, "telemetry_retry_attempts", 3)), 8
        ))
        base_delay = max(0.05, min(
            float(getattr(self.cfg, "telemetry_retry_base_seconds", 0.25)), 5.0
        ))
        last_error = None
        for attempt in range(retries):
            session = None
            try:
                session = self.db.get_session()
                value = operation(session)
                session.commit()
                return value
            except IntegrityError:
                if session is not None:
                    session.rollback()
                return False
            except Exception as exc:
                if session is not None:
                    session.rollback()
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(min(5.0, base_delay * (2 ** attempt)))
            finally:
                if session is not None:
                    session.close()
        logger.error(
            "Telemetry database write failed after %d attempts: %s",
            retries, last_error,
        )
        if raise_on_failure:
            raise RuntimeError(str(last_error) or "required telemetry database write failed") from last_error
        return False

    @staticmethod
    def _payload_time(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return ensure_aware_utc(value)
        if isinstance(value, str):
            try:
                return ensure_aware_utc(datetime.fromisoformat(value))
            except ValueError:
                return None
        return None

    def _enqueue_and_deliver(self, event_type: str, event_key: str, payload: dict) -> bool:
        """Write-ahead enqueue, then atomic target insert + delivered transition."""
        retries = max(1, min(int(getattr(self.cfg, "telemetry_retry_attempts", 3)), 8))
        delay = max(0.05, float(getattr(self.cfg, "telemetry_retry_base_seconds", 0.25)))
        last_error = None
        for attempt in range(retries):
            try:
                if self.outbox.status(event_key) == "delivered":
                    return False
                self._flush_failure_breadcrumbs()
                self.outbox.enqueue(event_type, payload, event_key=event_key)
                rows = [row for row in self.outbox.pending(limit=1000) if row.event_key == event_key]
                if not rows:
                    return True  # already queued under bounded backoff
                return self.outbox.deliver(rows[0].id, self._deliver_outbox_event)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(min(5.0, delay * (2 ** attempt)))
        logger.error("Critical telemetry enqueue failed: type=%s key=%s error=%s",
                     event_type, event_key, last_error)
        self._pending_health.append({"observed_at": utcnow(), "error": str(last_error)})
        return False

    def _flush_failure_breadcrumbs(self) -> None:
        while self._pending_health:
            item = self._pending_health[0]
            observed = item["observed_at"]
            key = _sha({"run": self.run_id, "db_failure": observed.isoformat()})
            command = {
                "run_id": self.run_id, "observed_at": observed,
                "component": "telemetry_store", "event_type": "database_write_failure",
                "severity": "critical", "status": "failed", "symbol": None,
                "data_timestamp": None, "data_age_seconds": None,
                "error_type": "DatabaseUnavailable",
                "error_message": item.get("error"),
                "details": {"bounded_memory_breadcrumb": True}, "policy_epoch": 0,
            }
            self.outbox.enqueue("health", command, event_key=key)
            rows = [row for row in self.outbox.pending(limit=1000) if row.event_key == key]
            if rows:
                self.outbox.deliver(rows[0].id, self._deliver_outbox_event)
            self._pending_health.popleft()

    def replay_outbox(self, limit: int = 100) -> dict:
        delivered = failed = 0
        for row in self.outbox.pending(limit=limit):
            if self.outbox.deliver(row.id, self._deliver_outbox_event):
                delivered += 1
            else:
                failed += 1
        return {"delivered": delivered, "failed": failed}

    def maintain_outbox(self) -> dict:
        """Bounded maintenance invoked outside exchange-position mutations."""
        before = self.outbox.metrics()
        batch_size = max(1, min(int(getattr(
            self.cfg, "telemetry_outbox_cleanup_batch_size", 1000
        )), 10_000))
        max_batches = max(1, min(int(getattr(
            self.cfg, "telemetry_outbox_cleanup_max_batches", 10
        )), 100))
        cleanup = {"deleted": 0, "batches": 0, "cutoff": None}
        for _ in range(max_batches):
            result = self.outbox.cleanup_delivered(
                retention_hours=getattr(
                    self.cfg, "telemetry_outbox_delivered_retention_hours", 24
                ),
                batch_size=batch_size,
            )
            cleanup["deleted"] += int(result["deleted"])
            cleanup["batches"] += 1
            cleanup["cutoff"] = result["cutoff"]
            if int(result["deleted"]) < batch_size:
                break
        after = self.outbox.metrics()
        return {"before": before, "cleanup": cleanup, "after": after}

    def schedule_outbox_maintenance(self) -> bool:
        """Run one bounded cleanup batch off the trading-cycle thread."""
        with self._outbox_maintenance_lock:
            worker = self._outbox_maintenance_thread
            if worker is not None and worker.is_alive():
                return False
            worker = threading.Thread(
                target=self._outbox_maintenance_worker,
                name="telemetry-outbox-maintenance", daemon=True,
            )
            self._outbox_maintenance_thread = worker
            worker.start()
            return True

    def _outbox_maintenance_worker(self) -> None:
        try:
            result = self.maintain_outbox()
            self.record_health(
                "telemetry_outbox", "outbox_maintenance", "info", "ok",
                details=result,
            )
        except Exception as exc:
            self.record_health(
                "telemetry_outbox", "outbox_maintenance_failure",
                "error", "degraded", error=exc,
            )

    def _deliver_outbox_event(self, session, event_type: str, payload: dict, event_key: str):
        if event_type == "decision":
            return self._deliver_decision(session, payload, event_key)
        if event_type == "protection":
            return self._deliver_protection(session, payload, event_key)
        if event_type == "health":
            return self._deliver_health(session, payload, event_key)
        if event_type == "exit":
            return self._deliver_exit(session, payload, event_key)
        raise RuntimeError(f"unsupported telemetry outbox event type: {event_type}")

    def ensure_run(
        self,
        *,
        root: Path,
        started_at: datetime,
        startup_account_snapshot: Optional[dict],
        reason: str = "run_start",
    ) -> TradingRun:
        document = effective_config_document(self.cfg)
        digest = _sha(document)
        git = git_identity(root)
        commit = self.cfg.commit_sha or git["commit"] or "unknown"
        source_sha = source_fingerprint(root)
        dependencies_sha = dependency_fingerprint()

        def operation(session):
            existing = session.query(TradingRun).filter_by(run_id=self.run_id).first()
            if existing is None:
                row = TradingRun(
                    run_id=self.run_id,
                    strategy_version=os.getenv("STRATEGY_VERSION", "frozen-current"),
                    git_commit_sha=commit,
                    git_branch=git["branch"] or None,
                    dirty_worktree=git["dirty"],
                    deployment_environment=self.cfg.runtime_mode,
                    hostname=socket.gethostname(),
                    python_version=platform.python_version(),
                    dependency_fingerprint=dependencies_sha,
                    application_started_at=ensure_aware_utc(started_at) or utcnow(),
                    trading_mode=("paper" if self.cfg.paper_trading else "live_testnet"),
                    testnet=bool(self.cfg.testnet),
                    enabled_symbols=document["resolved"]["symbols"],
                    timeframe_config=document["timeframes"],
                    strategy_config=document["strategy"],
                    risk_config=document["risk"],
                    exit_config=document["exit"],
                    filter_config=document["filters"],
                    runtime_config=document["resolved"],
                    environment_config=document["environment"],
                    effective_config=document,
                    config_hash=digest,
                    source_sha256=source_sha,
                    database_schema_version=DATABASE_SCHEMA_VERSION,
                    migration_version=MIGRATION_VERSION,
                    startup_account_snapshot=safe_json(startup_account_snapshot),
                )
                session.add(row)
                session.flush()
                session.add(RunPolicyEpoch(
                    run_id=self.run_id, epoch=0, effective_at=row.application_started_at,
                    config_hash=digest, effective_config=document, config_diff={},
                    reason=reason, git_commit_sha=commit,
                ))
                return row

            incompatible = []
            if existing.source_sha256 != source_sha:
                incompatible.append("source tree fingerprint")
            if existing.dependency_fingerprint != dependencies_sha:
                incompatible.append("dependency fingerprint")
            if existing.python_version != platform.python_version():
                incompatible.append("Python version")
            if existing.git_commit_sha != commit:
                incompatible.append("git commit")
            if incompatible:
                raise RuntimeError(
                    "refusing to resume immutable run after change to " + ", ".join(incompatible)
                )

            # Compare with the active policy epoch, not only the immutable
            # run-start hash.  A -> B -> A is still a real policy transition
            # and must be represented as a third append-only epoch.
            latest = (
                session.query(RunPolicyEpoch)
                .filter_by(run_id=self.run_id)
                .order_by(RunPolicyEpoch.epoch.desc())
                .first()
            )
            if latest is None or latest.config_hash != digest:
                previous = latest.effective_config if latest else existing.effective_config
                session.add(RunPolicyEpoch(
                    run_id=self.run_id,
                    epoch=(latest.epoch + 1 if latest else 1),
                    effective_at=utcnow(),
                    config_hash=digest,
                    effective_config=document,
                    config_diff=_config_diff(previous, document),
                    reason="effective configuration changed during run",
                    git_commit_sha=commit,
                ))
            session.expunge(existing)
            return existing

        return self._write(operation, raise_on_failure=True)

    def finish_run(self, stopped_at: Optional[datetime] = None) -> bool:
        def operation(session):
            row = session.query(TradingRun).filter_by(run_id=self.run_id).first()
            if row is None or row.application_stopped_at is not None:
                return False
            row.application_stopped_at = ensure_aware_utc(stopped_at) or utcnow()
            return True
        return bool(self._write(operation))

    def current_policy(self) -> tuple[int, str]:
        if not self.enabled:
            return 0, config_hash(self.cfg)
        session = None
        try:
            session = self.db.get_session()
            row = (
                session.query(RunPolicyEpoch)
                .filter_by(run_id=self.run_id)
                .order_by(RunPolicyEpoch.epoch.desc())
                .first()
            )
            if row:
                return row.epoch, row.config_hash
            run = session.query(TradingRun).filter_by(run_id=self.run_id).first()
            return (0, run.config_hash if run else config_hash(self.cfg))
        except Exception:
            # Event writes still get a deterministic fallback epoch/hash while
            # PostgreSQL is unavailable; the write path retries and buffers.
            return 0, config_hash(self.cfg)
        finally:
            if session is not None:
                session.close()

    @staticmethod
    def _bucket(observed_at: datetime, interval_seconds: int) -> int:
        return int(observed_at.timestamp()) // max(1, interval_seconds)

    def account_snapshot_due(self) -> bool:
        return (
            time.monotonic() - self._last_account_snapshot_monotonic
            >= max(1, self.cfg.telemetry_account_interval_sec)
        )

    def position_snapshot_due(self) -> bool:
        return (
            time.monotonic() - self._last_position_snapshot_monotonic
            >= max(1, self.cfg.telemetry_position_interval_sec)
        )

    def persist_account_snapshot(
        self, account: dict, positions: list[dict], *, observed_at: Optional[datetime] = None
    ) -> bool:
        observed = ensure_aware_utc(observed_at) or utcnow()
        interval = max(1, self.cfg.telemetry_account_interval_sec)
        bucket = self._bucket(observed, interval)
        longs = Decimal("0")
        shorts = Decimal("0")
        count = 0
        for position in positions or []:
            qty = Decimal(str(position.get("size") or 0))
            if qty <= 0:
                continue
            price = Decimal(str(position.get("markPrice") or position.get("avgPrice") or 0))
            notional = qty * price
            count += 1
            if position.get("side") == "Buy":
                longs += notional
            else:
                shorts += notional
        equity = safe_float(account.get("equity"), "equity")

        def operation(session):
            if session.query(AccountSnapshot).filter_by(
                run_id=self.run_id, snapshot_bucket=bucket
            ).first():
                return False
            prior_high = session.query(func.max(AccountSnapshot.high_water_equity)).filter_by(
                run_id=self.run_id
            ).scalar()
            high = max(value for value in (safe_float(prior_high), equity) if value is not None) \
                if prior_high is not None or equity is not None else None
            drawdown = equity - high if equity is not None and high is not None else None
            session.add(AccountSnapshot(
                run_id=self.run_id, observed_at=observed, snapshot_bucket=bucket,
                wallet_balance=safe_float(account.get("wallet_balance")),
                equity=equity,
                available_balance=safe_float(account.get("available_balance")),
                total_unrealized_pnl=safe_float(account.get("total_unrealized_pnl")),
                total_realized_pnl=safe_float(account.get("total_realized_pnl")),
                margin_balance=safe_float(account.get("margin_balance")),
                used_margin=safe_float(account.get("used_margin")),
                maintenance_margin=safe_float(account.get("maintenance_margin")),
                drawdown_from_run_high_water=drawdown, high_water_equity=high,
                open_position_count=count, gross_long_notional=float(longs),
                gross_short_notional=float(shorts), net_exposure=float(longs - shorts),
                source=account.get("source") or "Bybit V5 wallet-balance",
                fetch_status=account.get("fetch_status") or "ok",
                is_stale=bool(account.get("is_stale", False)),
                source_timestamp=ensure_aware_utc(account.get("source_timestamp")),
                error_type=sanitize_text(account.get("error_type"), 200),
                error_message=sanitize_text(account.get("error_message"), 2000),
                raw_payload=safe_json(account.get("raw_payload")),
            ))
            return True

        result = bool(self._write(operation))
        if result:
            self._last_account_snapshot_monotonic = time.monotonic()
        return result

    def persist_account_failure(self, exc: Exception, positions: Optional[list] = None) -> bool:
        return self.persist_account_snapshot({
            "source": "Bybit V5 wallet-balance", "fetch_status": "failed",
            "is_stale": True, "error_type": type(exc).__name__, "error_message": str(exc),
        }, positions or [])

    def persist_position_snapshots(
        self,
        positions: list[dict],
        *,
        protective_orders: Optional[dict[str, list[dict]]] = None,
        observed_at: Optional[datetime] = None,
    ) -> int:
        observed = ensure_aware_utc(observed_at) or utcnow()
        interval = max(1, self.cfg.telemetry_position_interval_sec)
        bucket = self._bucket(observed, interval)
        protective_orders = protective_orders or {}
        close_visibility_grace = max(0, int(getattr(
            self.cfg, "position_close_visibility_grace_seconds", 120
        )))
        session = self.db.get_session()
        try:
            open_trades = session.query(TradeLog).filter(
                TradeLog.status == "open",
            ).all()
            recent_closed_trades = session.query(TradeLog).filter(
                TradeLog.status == "closed",
                TradeLog.closed_at.isnot(None),
                TradeLog.closed_at >= observed - timedelta(seconds=close_visibility_grace),
            ).all() if close_visibility_grace else []
            for trade in open_trades:
                session.expunge(trade)
            for trade in recent_closed_trades:
                session.expunge(trade)
        finally:
            session.close()
        by_symbol: dict[str, list[TradeLog]] = {}
        for trade in open_trades:
            by_symbol.setdefault(trade.symbol, []).append(trade)
        recently_closed_by_symbol: dict[str, list[TradeLog]] = {}
        for trade in recent_closed_trades:
            recently_closed_by_symbol.setdefault(trade.symbol, []).append(trade)
        inserted = 0
        for position in positions:
            qty = safe_float(position.get("size")) or 0.0
            if qty <= 0:
                continue
            candidates = by_symbol.get(position.get("symbol"), [])
            side_action = "open_long" if position.get("side") == "Buy" else "open_short"
            candidates = [trade for trade in candidates if trade.action == side_action]
            if len(candidates) != 1:
                lag_candidates = []
                if not candidates:
                    position_entry = safe_float(position.get("avgPrice"))
                    position_qty = safe_float(position.get("size"))
                    for closed in recently_closed_by_symbol.get(position.get("symbol"), []):
                        if closed.action != side_action:
                            continue
                        trade_entry = safe_float(closed.entry_price)
                        trade_qty = safe_float(closed.entry_filled_qty)
                        entry_matches = bool(
                            position_entry and trade_entry
                            and abs(position_entry - trade_entry) / trade_entry <= 0.001
                        )
                        qty_matches = bool(
                            position_qty is not None and trade_qty
                            and abs(position_qty - trade_qty) / trade_qty <= 0.001
                        )
                        if entry_matches and qty_matches:
                            lag_candidates.append(closed)
                if len(lag_candidates) == 1:
                    closed = lag_candidates[0]
                    self.record_health(
                        "position_telemetry", "position_close_visibility_lag",
                        "warning", "exchange_lag", symbol=position.get("symbol"),
                        details={
                            "trade_log_id": closed.id,
                            "closed_at": ensure_aware_utc(closed.closed_at),
                            "reported_size": position.get("size"),
                            "reported_average_entry": position.get("avgPrice"),
                            "classification": "recent_closed_trade_identity_match",
                        },
                    )
                    continue
                self.record_health(
                    "position_telemetry", "position_without_internal_trade", "error", "unresolved",
                    symbol=position.get("symbol"), details={
                        "position": position,
                        "candidate_trade_ids": [trade.id for trade in candidates],
                    },
                )
                continue
            trade = candidates[0]
            orders = protective_orders.get(trade.symbol, [])
            owner_store = self._for_owner(trade.run_id)
            if owner_store._persist_one_position(trade, position, orders, observed, bucket):
                inserted += 1
        if inserted:
            self._last_position_snapshot_monotonic = time.monotonic()
        return inserted

    def _persist_one_position(self, trade, position, orders, observed, bucket) -> bool:
        qty = float(position.get("size") or 0)
        entry = float(position.get("avgPrice") or trade.entry_price)
        mark = safe_float(position.get("markPrice"), "markPrice")
        last = safe_float(position.get("lastPrice"), "lastPrice")
        current_sl = safe_float(position.get("stopLoss"), "stopLoss")
        current_tp = safe_float(position.get("takeProfit"), "takeProfit")
        original_sl = safe_float(trade.stop_loss_price)
        original_tp = safe_float(trade.take_profit_price)
        initial_qty = safe_float(trade.entry_filled_qty) or (float(trade.size_usdt) / entry if entry else None)
        initial_risk = initial_qty * abs(entry - original_sl) if initial_qty and original_sl else None
        upl = safe_float(position.get("unrealisedPnl"), "unrealisedPnl")
        if upl is None and mark is not None:
            upl = (mark - entry) * qty * (1 if position.get("side") == "Buy" else -1)
        unrealized_r = upl / initial_risk if upl is not None and initial_risk else None
        current_risk = qty * abs((mark if mark is not None else entry) - current_sl) if current_sl else None
        latest_market_ts, latest_market_price = self._latest_market_trade(trade.symbol, observed)
        if last is None:
            last = latest_market_price
        market_age = (
            (observed - latest_market_ts).total_seconds() if latest_market_ts is not None else None
        )
        order_ids = [order.get("orderId") for order in orders if order.get("orderId")]
        protection_status = "protected" if current_sl and current_tp else "missing_or_partial"
        volatility = ((trade.entry_snapshot or {}).get("market_context") or {}).get("volatility_state")

        def operation(session):
            if session.query(PositionSnapshot).filter_by(
                run_id=self.run_id, trade_log_id=trade.id, snapshot_bucket=bucket
            ).first():
                return False
            latest_decision = (
                session.query(DecisionEvent)
                .filter(
                    DecisionEvent.run_id == self.run_id,
                    DecisionEvent.symbol == trade.symbol,
                    DecisionEvent.observed_at <= observed,
                )
                .order_by(DecisionEvent.observed_at.desc())
                .first()
            )
            row = PositionSnapshot(
                run_id=self.run_id, processing_run_id=self.processing_run_id,
                trade_log_id=trade.id, order_link_id=trade.order_link_id,
                observed_at=observed, snapshot_bucket=bucket, symbol=trade.symbol,
                side="long" if position.get("side") == "Buy" else "short",
                quantity=qty, average_entry=entry, mark_price=mark, last_price=last,
                unrealized_pnl=upl, unrealized_r=unrealized_r,
                current_stop_loss=current_sl, current_take_profit=current_tp,
                original_stop_loss=original_sl, original_take_profit=original_tp,
                current_estimated_risk=current_risk,
                distance_to_stop_loss=(abs((mark or entry) - current_sl) if current_sl else None),
                distance_to_take_profit=(abs(current_tp - (mark or entry)) if current_tp else None),
                position_age_seconds=max(0, int((observed - ensure_aware_utc(trade.opened_at)).total_seconds())),
                market_data_age_seconds=market_age, protection_status=protection_status,
                protective_order_ids=order_ids, exit_manager_state=safe_json(trade.exit_trigger),
                market_regime=(latest_decision.market_regime if latest_decision else trade.regime),
                volatility_regime=(
                    latest_decision.volatility_regime if latest_decision else volatility
                ),
                source="Bybit V5 positions + durable public trades",
                fetch_status="ok", is_stale=bool(
                    market_age is None or market_age > self.cfg.max_trade_flow_age_seconds
                ), raw_position=safe_json(position),
            )
            session.add(row)
            session.flush()
            self._update_excursion(session, trade, position, row, observed)
            return True

        result = bool(self._write(operation))
        if result and protection_status == "protected" and order_ids:
            acknowledged = [{
                "order_id": order.get("orderId"),
                "order_link_id": order.get("orderLinkId"),
                "status": order.get("orderStatus"),
                "stop_order_type": order.get("stopOrderType"),
                "trigger_price": order.get("triggerPrice"),
            } for order in orders if order.get("orderId")]
            self.record_protection_event(
                trade, "exchange_protection_acknowledged", None,
                {
                    "stop_loss": current_sl,
                    "take_profit": current_tp,
                    "orders": acknowledged,
                },
                reason="exchange-native protection observed",
                source_module="position_telemetry", success=True,
                raw_status={"orders": acknowledged}, observed_at=observed,
                state_deduplicated=True,
            )
        elif protection_status != "protected":
            self.record_protection_event(
                trade, "missing_protection_detected", None,
                {"stop_loss": current_sl, "take_profit": current_tp, "order_ids": order_ids},
                reason="exchange position lacks complete SL/TP", source_module="position_telemetry",
                success=False, raw_status=position, observed_at=observed,
            )
        return result

    def _latest_market_trade(self, symbol: str, observed: datetime):
        session = self.db.get_session()
        try:
            row = session.query(Trade).filter(
                Trade.symbol == symbol, Trade.ts <= to_epoch_ms(observed)
            ).order_by(Trade.ts.desc()).first()
            return (from_epoch_ms(row.ts), float(row.price)) if row else (None, None)
        finally:
            session.close()

    def _update_excursion(self, session, trade, position, snapshot, observed) -> None:
        excursion = session.query(TradeExcursion).filter_by(trade_log_id=trade.id).first()
        side = "long" if position.get("side") == "Buy" else "short"
        entry = float(snapshot.average_entry)
        qty = float(snapshot.quantity)
        original_sl = safe_float(trade.stop_loss_price)
        initial_qty = safe_float(trade.entry_filled_qty) or (float(trade.size_usdt) / entry if entry else None)
        initial_risk = initial_qty * abs(entry - original_sl) if initial_qty and original_sl else None
        entry_time = self._actual_entry_time(session, trade)
        since = (
            to_epoch_ms(excursion.last_market_timestamp)
            if excursion and excursion.last_market_timestamp else to_epoch_ms(entry_time)
        )
        rows = session.query(Trade).filter(
            Trade.symbol == trade.symbol,
            Trade.ts > (since or 0),
            Trade.ts <= to_epoch_ms(observed),
        ).order_by(Trade.ts.asc()).all()
        samples = [(float(row.price), from_epoch_ms(row.ts)) for row in rows]
        if snapshot.mark_price is not None:
            samples.append((float(snapshot.mark_price), observed))
        if snapshot.last_price is not None:
            samples.append((float(snapshot.last_price), observed))
        if not samples:
            return
        if excursion is None:
            excursion = TradeExcursion(
                run_id=self.run_id, trade_log_id=trade.id, order_link_id=trade.order_link_id,
                last_processing_run_id=self.processing_run_id,
                symbol=trade.symbol, side=side, entry_price=entry,
                initial_risk_usdt=initial_risk,
                sampling_method="persisted public trades plus polled Bybit mark/last",
                sampling_limitations=(
                    "Excursions are exact only for received public trades and polling samples; "
                    "WebSocket gaps and quantity changes between samples can understate extrema."
                ),
            )
            session.add(excursion)
        excursion.last_processing_run_id = self.processing_run_id
        favorable = max(samples, key=lambda item: item[0]) if side == "long" else min(samples, key=lambda item: item[0])
        adverse = min(samples, key=lambda item: item[0]) if side == "long" else max(samples, key=lambda item: item[0])
        favorable_distance = max(0.0, favorable[0] - entry if side == "long" else entry - favorable[0])
        adverse_distance = max(0.0, entry - adverse[0] if side == "long" else adverse[0] - entry)
        if excursion.mfe_price_distance is None or favorable_distance > float(excursion.mfe_price_distance):
            excursion.mfe_price_distance = favorable_distance
            excursion.mfe_pct = favorable_distance / entry * 100 if entry else None
            excursion.mfe_usdt = favorable_distance * qty
            excursion.mfe_r = favorable_distance * qty / initial_risk if initial_risk else None
            excursion.mfe_price = favorable[0]
            excursion.mfe_at = favorable[1]
            excursion.mfe_quantity = qty
            excursion.mfe_market_snapshot_id = snapshot.id
            excursion.time_to_mfe_seconds = max(
                0, int((favorable[1] - entry_time).total_seconds())
            )
        if excursion.mae_price_distance is None or adverse_distance > float(excursion.mae_price_distance):
            excursion.mae_price_distance = adverse_distance
            excursion.mae_pct = adverse_distance / entry * 100 if entry else None
            excursion.mae_usdt = adverse_distance * qty
            excursion.mae_r = adverse_distance * qty / initial_risk if initial_risk else None
            excursion.mae_price = adverse[0]
            excursion.mae_at = adverse[1]
            excursion.mae_quantity = qty
            excursion.mae_market_snapshot_id = snapshot.id
            excursion.time_to_mae_seconds = max(
                0, int((adverse[1] - entry_time).total_seconds())
            )
        upl = safe_float(snapshot.unrealized_pnl)
        if upl is not None:
            excursion.maximum_unrealized_profit = max(
                safe_float(excursion.maximum_unrealized_profit) or 0.0, upl
            )
            excursion.maximum_unrealized_loss = min(
                safe_float(excursion.maximum_unrealized_loss) or 0.0, upl
            )
        sl = safe_float(snapshot.current_stop_loss)
        tp = safe_float(snapshot.current_take_profit)
        high = max(price for price, _ in samples)
        low = min(price for price, _ in samples)
        if tp is not None:
            reached = high >= tp if side == "long" else low <= tp
            excursion.tp_reached_intrabar = bool(excursion.tp_reached_intrabar or reached)
        if sl is not None:
            reached = low <= sl if side == "long" else high >= sl
            excursion.sl_reached_intrabar = bool(excursion.sl_reached_intrabar or reached)
        excursion.last_observed_at = observed
        excursion.last_market_timestamp = max(ts for _, ts in samples if ts is not None)
        session.query(TradeLog).filter_by(id=trade.id).update({
            "mfe_pct": excursion.mfe_pct, "mae_pct": excursion.mae_pct,
        })

    @staticmethod
    def _actual_entry_time(session, trade) -> datetime:
        value = session.query(func.min(NormalizedExecution.execution_time)).filter(
            NormalizedExecution.trade_log_id == trade.id,
            NormalizedExecution.role == "entry",
        ).scalar()
        return ensure_aware_utc(value) or ensure_aware_utc(trade.opened_at) or utcnow()

    def record_protection_event(
        self, trade, event_type: str, old_value: Any, new_value: Any, *, reason: str,
        source_module: str, success: bool, raw_status: Any = None,
        exchange_order_id: Optional[str] = None,
        exchange_order_link_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
        state_deduplicated: bool = False,
    ) -> bool:
        owner = self._trade_owner(trade)
        if owner and owner != self.run_id:
            return self._for_owner(owner).record_protection_event(
                trade, event_type, old_value, new_value, reason=reason,
                source_module=source_module, success=success, raw_status=raw_status,
                exchange_order_id=exchange_order_id,
                exchange_order_link_id=exchange_order_link_id,
                observed_at=observed_at, state_deduplicated=state_deduplicated,
            )
        observed = ensure_aware_utc(observed_at) or utcnow()
        epoch, _ = self.current_policy()
        payload = {
            "run_id": self.run_id, "processing_run_id": self.processing_run_id,
            "trade": self._trade_value(trade, "order_link_id"),
            "type": event_type, "old": old_value, "new": new_value,
            "exchange_order_id": exchange_order_id, "success": success,
        }
        if not state_deduplicated:
            payload["observed_at_ms"] = int(observed.timestamp() * 1000)
        key = _sha(payload)

        command = {
            "run_id": self.run_id, "processing_run_id": self.processing_run_id,
            "trade_log_id": self._trade_value(trade, "id"),
            "order_link_id": self._trade_value(trade, "order_link_id"),
            "observed_at": observed, "symbol": self._trade_value(trade, "symbol") or "unknown",
            "event_type": event_type, "old_value": old_value, "new_value": new_value,
            "exchange_order_id": exchange_order_id,
            "exchange_order_link_id": exchange_order_link_id,
            "reason": reason, "source_module": source_module, "success": success,
            "raw_status": raw_status, "policy_epoch": epoch,
        }
        return self._enqueue_and_deliver("protection", key, command)

    def _deliver_protection(self, session, payload: dict, event_key: str):
        if session.query(TradeProtectionEvent).filter_by(event_key=event_key).first():
            return False
        order_link_id = payload.get("order_link_id")
        trade_log_id = payload.get("trade_log_id")
        if trade_log_id is None and order_link_id:
            linked = session.query(TradeLog.id).filter_by(order_link_id=order_link_id).first()
            trade_log_id = linked[0] if linked else None
        session.add(TradeProtectionEvent(
            event_key=event_key, run_id=payload["run_id"],
            processing_run_id=payload.get("processing_run_id"), trade_log_id=trade_log_id,
            order_link_id=order_link_id,
            observed_at=self._payload_time(payload.get("observed_at")) or utcnow(),
            symbol=payload.get("symbol") or "unknown", event_type=payload["event_type"],
            old_value=safe_json(payload.get("old_value")),
            new_value=safe_json(payload.get("new_value")),
            exchange_order_id=sanitize_text(payload.get("exchange_order_id"), 100),
            exchange_order_link_id=sanitize_text(payload.get("exchange_order_link_id"), 100),
            reason=sanitize_text(payload.get("reason"), 1000),
            source_module=payload.get("source_module") or "unknown",
            success=bool(payload.get("success")),
            raw_exchange_status=safe_json(payload.get("raw_status")),
            policy_epoch=int(payload.get("policy_epoch") or 0),
        ))
        return True

    @staticmethod
    def _trade_value(trade: Any, name: str) -> Any:
        return trade.get(name) if isinstance(trade, dict) else getattr(trade, name, None)

    def record_decision(self, payload: dict) -> str:
        observed = ensure_aware_utc(payload.get("observed_at")) or utcnow()
        epoch, digest = self.current_policy()
        evaluation_id = payload.get("evaluation_id") or _sha({
            "run": self.run_id, "symbol": payload.get("symbol"),
            "market": safe_json(payload.get("market_data_timestamp")),
            "phase": payload.get("phase", "evaluation"),
        })
        key = _sha({"evaluation_id": evaluation_id, "phase": payload.get("phase", "evaluation")})
        command = {
            "run_id": self.run_id, "observed_at": observed,
            "evaluation_id": evaluation_id, "policy_epoch": epoch,
            "commit_sha": self.cfg.commit_sha or "unknown", "config_hash": digest,
            "payload": payload,
        }
        self._enqueue_and_deliver("decision", key, command)
        return key

    def _deliver_decision(self, session, command: dict, event_key: str):
        if session.query(DecisionEvent).filter_by(event_key=event_key).first():
            return False
        payload = command["payload"]
        observed = self._payload_time(command.get("observed_at")) or utcnow()
        epoch = int(command.get("policy_epoch") or 0)
        row = DecisionEvent(
            event_key=event_key, run_id=command["run_id"], observed_at=observed,
            evaluation_id=command["evaluation_id"], phase=payload.get("phase", "evaluation"),
            symbol=payload["symbol"], side=payload.get("side"),
            market_data_timestamp=self._payload_time(payload.get("market_data_timestamp")),
            market_data_age_seconds=safe_float(payload.get("market_data_age_seconds")),
            signal_outputs=safe_json(payload.get("signal_outputs") or []),
            confirmation_families=safe_json(payload.get("confirmation_families") or []),
            decision_score=safe_float(payload.get("decision_score")),
            market_regime=payload.get("market_regime"), volatility_regime=payload.get("volatility_regime"),
            trend_state=payload.get("trend_state"), spread=safe_float(payload.get("spread")),
            funding=safe_float(payload.get("funding")), risk_score=safe_float(payload.get("risk_score")),
            proposed_entry=safe_float(payload.get("proposed_entry")),
            proposed_stop_loss=safe_float(payload.get("proposed_stop_loss")),
            proposed_take_profit=safe_float(payload.get("proposed_take_profit")),
            proposed_quantity=safe_float(payload.get("proposed_quantity")),
            estimated_risk=safe_float(payload.get("estimated_risk")),
            filter_results=safe_json(payload.get("filter_results") or {}),
            final_decision=payload.get("final_decision", "unknown"),
            decision_reason=sanitize_text(payload.get("decision_reason") or "unspecified", 2000),
            accepted=bool(payload.get("accepted")), policy_epoch=epoch,
            commit_sha=command.get("commit_sha") or "unknown",
            config_hash=command.get("config_hash") or "unknown",
            structured_payload=safe_json(payload),
        )
        session.add(row)
        for index, rejection in enumerate(payload.get("rejections") or []):
            rejection_key = _sha({"decision": event_key, "index": index, "value": rejection})
            session.add(RejectionEvent(
                event_key=rejection_key, run_id=command["run_id"], decision_event_key=event_key,
                observed_at=observed, symbol=payload["symbol"], requested_side=rejection.get("side"),
                rejection_stage=rejection.get("stage", payload.get("phase", "evaluation")),
                rejection_code=rejection.get("code", "rejected"),
                rejection_reason=sanitize_text(rejection.get("reason") or "unspecified", 2000),
                structured_context=safe_json(rejection.get("context") or {}), policy_epoch=epoch,
            ))
        return True

    def record_health(
        self, component: str, event_type: str, severity: str, status: str, *,
        symbol: Optional[str] = None, data_timestamp: Optional[datetime] = None,
        data_age_seconds: Optional[float] = None, error: Optional[Exception] = None,
        details: Optional[dict] = None, observed_at: Optional[datetime] = None,
    ) -> bool:
        observed = ensure_aware_utc(observed_at) or utcnow()
        details = dict(details or {})
        dedup_window = max(
            0, int(getattr(self.cfg, "health_event_dedup_window_seconds", 60))
        )
        identity = (
            component, event_type, severity, status, symbol,
            type(error).__name__ if error else None,
            sanitize_text(error, 500) if error else None,
        )
        now_mono = self._monotonic()
        with self._health_lock:
            state = self._health_state.get(identity)
            if (
                dedup_window > 0 and state is not None
                and now_mono - state["emitted_at"] < dedup_window
            ):
                state["suppressed"] += 1
                state["last_seen_at"] = observed
                return False
            if state and state["suppressed"]:
                details["suppressed_identical_events"] = state["suppressed"]
                details["suppressed_window_started_at"] = state["first_seen_at"].isoformat()
                details["suppressed_last_seen_at"] = state["last_seen_at"].isoformat()
            self._health_state[identity] = {
                "emitted_at": now_mono, "suppressed": 0,
                "first_seen_at": observed, "last_seen_at": observed,
            }
            if status in ("recovered", "ok"):
                recovered_suppressed = 0
                for key in list(self._health_state):
                    if key[:2] == (component, event_type) and key[4] == symbol and key != identity:
                        recovered_suppressed += self._health_state[key].get("suppressed", 0)
                        self._health_state.pop(key, None)
                if recovered_suppressed:
                    details["suppressed_before_recovery"] = recovered_suppressed
        epoch, _ = self.current_policy()
        payload = {
            "run": self.run_id, "component": component, "type": event_type,
            "status": status, "symbol": symbol,
            "observed_bucket": int(observed.timestamp()), "details": details,
        }
        key = _sha(payload)

        command = {
            "run_id": self.run_id, "observed_at": observed, "component": component,
            "event_type": event_type, "severity": severity, "status": status,
            "symbol": symbol, "data_timestamp": data_timestamp,
            "data_age_seconds": data_age_seconds,
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
            "details": details, "policy_epoch": epoch,
        }
        return self._enqueue_and_deliver("health", key, command)

    def record_health_condition(
        self, component: str, event_type: str, *, active: bool,
        symbol: Optional[str] = None, severity: str = "warning",
        status: str = "degraded", details: Optional[dict] = None,
        observed_at: Optional[datetime] = None,
    ) -> bool:
        """Persist condition transitions plus bounded reminders, not every cycle.

        High-cardinality conditions such as stale order books are evaluated on
        every symbol/cycle.  The durable audit trail needs the beginning,
        periodic reminders with a suppression counter, and recovery -- not one
        nearly identical PostgreSQL row per minute.
        """
        observed = ensure_aware_utc(observed_at) or utcnow()
        now_mono = self._monotonic()
        reminder = max(1, int(getattr(
            self.cfg, "health_condition_reminder_seconds", 900
        )))
        identity = (component, event_type, symbol)
        emit_type = event_type
        emit_severity = severity
        emit_status = status
        emit_details = dict(details or {})
        with self._health_lock:
            state = self._health_conditions.get(identity)
            if active:
                if state is None:
                    self._health_conditions[identity] = {
                        "active_since": observed,
                        "last_seen_at": observed,
                        "last_emitted_monotonic": now_mono,
                        "suppressed": 0,
                    }
                    emit_details["condition_transition"] = "entered"
                elif now_mono - state["last_emitted_monotonic"] < reminder:
                    state["last_seen_at"] = observed
                    state["suppressed"] += 1
                    return False
                else:
                    emit_details.update({
                        "condition_transition": "reminder",
                        "active_since": state["active_since"].isoformat(),
                        "suppressed_identical_events": state["suppressed"],
                        "suppressed_last_seen_at": state["last_seen_at"].isoformat(),
                    })
                    state["last_seen_at"] = observed
                    state["last_emitted_monotonic"] = now_mono
                    state["suppressed"] = 0
            else:
                if state is None:
                    return False
                self._health_conditions.pop(identity, None)
                # A quick stale -> recovered -> stale transition must emit the
                # second onset even inside the generic dedup window.
                for key in list(self._health_state):
                    if key[:2] == (component, event_type) and key[4] == symbol:
                        self._health_state.pop(key, None)
                emit_type = f"{event_type}_recovered"
                emit_severity = "info"
                emit_status = "recovered"
                emit_details.update({
                    "condition_transition": "recovered",
                    "active_since": state["active_since"].isoformat(),
                    "last_seen_at": state["last_seen_at"].isoformat(),
                    "suppressed_identical_events": state["suppressed"],
                    "duration_seconds": max(
                        0.0, (observed - state["active_since"]).total_seconds()
                    ),
                })
        return self.record_health(
            component, emit_type, emit_severity, emit_status,
            symbol=symbol, details=emit_details, observed_at=observed,
        )

    def _deliver_health(self, session, payload: dict, event_key: str):
        if session.query(OperationalHealthEvent).filter_by(event_key=event_key).first():
            return False
        session.add(OperationalHealthEvent(
            event_key=event_key, run_id=payload["run_id"],
            observed_at=self._payload_time(payload.get("observed_at")) or utcnow(),
            component=payload["component"], event_type=payload["event_type"],
            severity=payload["severity"], status=payload["status"], symbol=payload.get("symbol"),
            data_timestamp=self._payload_time(payload.get("data_timestamp")),
            data_age_seconds=safe_float(payload.get("data_age_seconds")),
            error_type=sanitize_text(payload.get("error_type"), 200),
            error_message=sanitize_text(payload.get("error_message"), 2000),
            details=safe_json(payload.get("details") or {}),
            policy_epoch=int(payload.get("policy_epoch") or 0),
        ))
        return True

    def finalize_trade(
        self, order_link_id: str, *, actual_exit_reason: str, records: list[dict],
        executions_by_order: dict[str, list[dict]], realized_pnl: float,
        fees: Optional[float], funding: Optional[float] = None,
        reconciliation_status: str = "matched",
    ) -> bool:
        session = self.db.get_session()
        try:
            owner_row = session.query(TradeLog.run_id).filter_by(
                order_link_id=order_link_id
            ).first()
            owner = owner_row[0] if owner_row else None
        finally:
            session.close()
        if owner and owner != self.run_id:
            return self._for_owner(owner).finalize_trade(
                order_link_id, actual_exit_reason=actual_exit_reason,
                records=records, executions_by_order=executions_by_order,
                realized_pnl=realized_pnl, fees=fees, funding=funding,
                reconciliation_status=reconciliation_status,
            )
        observed = utcnow()
        epoch, _ = self.current_policy()
        key = _sha({"run": self.run_id, "type": "exit", "order_link_id": order_link_id})
        command = {
            "run_id": self.run_id, "processing_run_id": self.processing_run_id,
            "order_link_id": order_link_id, "observed_at": observed,
            "actual_exit_reason": actual_exit_reason, "records": records,
            "executions_by_order": executions_by_order, "realized_pnl": realized_pnl,
            "fees": fees, "funding": funding,
            "reconciliation_status": reconciliation_status, "policy_epoch": epoch,
        }
        return self._enqueue_and_deliver("exit", key, command)

    def _deliver_exit(self, session, payload: dict, _event_key: str):
        order_link_id = payload["order_link_id"]
        trade = session.query(TradeLog).filter_by(order_link_id=order_link_id).first()
        if trade is None:
            raise RuntimeError(f"exit telemetry trade missing: {order_link_id}")
        if session.query(TradeExitEvent).filter_by(trade_log_id=trade.id).first():
            return False
        observed = self._payload_time(payload.get("observed_at")) or utcnow()
        records = payload.get("records") or []
        executions_by_order = payload.get("executions_by_order") or {}
        realized_pnl = float(payload["realized_pnl"])
        excursion = session.query(TradeExcursion).filter_by(trade_log_id=trade.id).first()
        excursion = self._finalize_excursion_samples(
            session, trade, excursion, records, executions_by_order, observed
        )
        initial_risk = safe_float(excursion.initial_risk_usdt) if excursion else None
        realized_r = realized_pnl / initial_risk if initial_risk else None
        closing_ids = [record.get("orderId") for record in records if record.get("orderId")]
        execution_ids = [
            execution.get("execId") for oid in closing_ids
            for execution in executions_by_order.get(oid, []) if execution.get("execId")
        ]
        slippage = self._protective_slippage_evidence(
            session, trade, closing_ids, executions_by_order, initial_risk, observed
        )
        if excursion:
            excursion.finalized_at = observed
            excursion.finalized_by_run_id = payload.get("processing_run_id")
            excursion.last_processing_run_id = payload.get("processing_run_id")
            excursion.profitable_before_closing_loss = bool(
                realized_pnl < 0 and (safe_float(excursion.mfe_usdt) or 0) > 0
            )
            excursion.losing_before_closing_profit = bool(
                realized_pnl > 0 and (safe_float(excursion.mae_usdt) or 0) > 0
            )
            trade.mfe_pct, trade.mae_pct = excursion.mfe_pct, excursion.mae_pct
        mechanisms = sorted({
            execution.get("stopOrderType") or "direct_order"
            for oid in closing_ids for execution in executions_by_order.get(oid, [])
        })
        requested_reason = (trade.exit_trigger or {}).get("reason") if trade.exit_trigger else None
        latest_stop = (
            trade.second_tightened_stop_loss_price
            or trade.tightened_stop_loss_price or trade.stop_loss_price
        )
        latest_take = (
            trade.second_tightened_take_profit_price
            or trade.tightened_take_profit_price or trade.take_profit_price
        )
        session.add(TradeExitEvent(
            run_id=payload["run_id"], processing_run_id=payload.get("processing_run_id"),
            trade_log_id=trade.id, order_link_id=order_link_id, observed_at=observed,
            symbol=trade.symbol, actual_exit_reason=payload["actual_exit_reason"],
            requested_exit_reason=sanitize_text(requested_reason, None),
            exchange_exit_mechanism=",".join(mechanisms) if mechanisms else None,
            exit_manager_signal=safe_json(trade.exit_trigger, string_limit=None),
            protection_trigger={
                "stop_loss": safe_float(latest_stop),
                "take_profit": safe_float(latest_take),
                "trigger_source": slippage.get("trigger_source"),
            }, reconciliation_status=payload.get("reconciliation_status") or "matched",
            closing_order_ids=closing_ids, closing_execution_ids=execution_ids,
            realized_pnl=realized_pnl, fees=safe_float(payload.get("fees")),
            funding=safe_float(payload.get("funding")), realized_r=realized_r,
            mfe=(self._excursion_payload(excursion, "mfe") if excursion else None),
            mae=(self._excursion_payload(excursion, "mae") if excursion else None),
            intended_trigger_price=slippage.get("intended_trigger_price"),
            trigger_source=slippage.get("trigger_source"),
            price_near_trigger=slippage.get("price_near_trigger"),
            mark_price_near_trigger=slippage.get("mark_price_near_trigger"),
            last_price_near_trigger=slippage.get("last_price_near_trigger"),
            actual_fill_price=slippage.get("actual_fill_price"),
            slippage_absolute=slippage.get("slippage_absolute"),
            slippage_pct=slippage.get("slippage_pct"),
            slippage_r=slippage.get("slippage_r"),
            slippage_classification=slippage.get("classification"),
            trigger_at=slippage.get("trigger_at"), fill_at=slippage.get("fill_at"),
            protective_execution_id=slippage.get("execution_id"),
            policy_epoch=int(payload.get("policy_epoch") or 0),
            raw_payload=safe_json({"records": records, "executions": executions_by_order,
                                   "requested_exit_reason": requested_reason,
                                   "protective_slippage": slippage},
                                  string_limit=None),
        ))
        self._finalize_protection_lifecycle(
            session, trade, closing_ids, slippage, observed,
            payload.get("processing_run_id"), int(payload.get("policy_epoch") or 0),
        )
        return True

    def _protective_slippage_evidence(
        self, session, trade, closing_ids, executions_by_order, initial_risk, observed
    ) -> dict:
        """Build exchange-ID-first protective fill evidence without inventing gaps."""
        order = session.query(TradeExchangeOrder).filter(
            TradeExchangeOrder.trade_log_id == trade.id,
            TradeExchangeOrder.exchange_order_id.in_(closing_ids or [""]),
        ).order_by(TradeExchangeOrder.last_observed_at.desc()).first()
        raw_order = (order.raw_payload or {}) if order else {}
        trigger = safe_float(order.trigger_price) if order else None
        trigger_source = (
            raw_order.get("triggerBy") or raw_order.get("slTriggerBy")
            or raw_order.get("tpTriggerBy")
        )
        executions = [
            item for oid in closing_ids for item in executions_by_order.get(oid, [])
        ]
        qty_total = sum(safe_float(item.get("execQty")) or 0.0 for item in executions)
        fill = (
            sum(
                (safe_float(item.get("execPrice")) or 0.0)
                * (safe_float(item.get("execQty")) or 0.0)
                for item in executions
            ) / qty_total
            if qty_total > 0 else safe_float(trade.exit_price)
        )
        fill_times = [
            from_epoch_ms(item.get("execTime")) for item in executions
            if from_epoch_ms(item.get("execTime")) is not None
        ]
        fill_at = max(fill_times) if fill_times else observed
        snapshots = session.query(PositionSnapshot).filter(
            PositionSnapshot.trade_log_id == trade.id,
            PositionSnapshot.observed_at >= fill_at - timedelta(minutes=2),
            PositionSnapshot.observed_at <= fill_at + timedelta(minutes=2),
        ).all()
        nearest = min(
            snapshots,
            key=lambda row: abs((ensure_aware_utc(row.observed_at) - fill_at).total_seconds()),
            default=None,
        )
        mark = safe_float(nearest.mark_price) if nearest else None
        last = safe_float(nearest.last_price) if nearest else None
        near = mark if trigger_source == "MarkPrice" else last if trigger_source == "LastPrice" else None
        adverse = pct = slippage_r = None
        if trigger is not None and fill is not None:
            is_long = trade.action == "open_long"
            # Positive means execution was worse than the intended protective
            # trigger; negative means price improvement.
            adverse = trigger - fill if is_long else fill - trigger
            pct = adverse / trigger * 100 if trigger else None
            qty = safe_float(trade.entry_filled_qty) or qty_total
            slippage_r = adverse * qty / initial_risk if initial_risk and qty else None
        elevated_pct = float(getattr(self.cfg, "slippage_elevated_pct", 0.25))
        anomalous_pct = float(getattr(self.cfg, "slippage_anomalous_pct", 1.0))
        elevated_r = float(getattr(self.cfg, "slippage_elevated_r", 0.25))
        anomalous_r = float(getattr(self.cfg, "slippage_anomalous_r", 0.75))
        adverse_pct = max(0.0, pct or 0.0)
        adverse_r = max(0.0, slippage_r or 0.0)
        if trigger is None or fill is None:
            classification = "unavailable"
        elif adverse_pct >= anomalous_pct or adverse_r >= anomalous_r:
            classification = "anomalous"
        elif adverse_pct >= elevated_pct or adverse_r >= elevated_r:
            classification = "elevated"
        else:
            classification = "normal"
        return {
            "intended_trigger_price": trigger, "trigger_source": trigger_source,
            "price_near_trigger": near, "mark_price_near_trigger": mark,
            "last_price_near_trigger": last, "actual_fill_price": fill,
            "slippage_absolute": adverse, "slippage_pct": pct,
            "slippage_r": slippage_r, "classification": classification,
            # Bybit closed-PnL/order history has no certified trigger timestamp.
            "trigger_at": None, "fill_at": fill_at,
            "execution_id": executions[-1].get("execId") if executions else None,
            "exchange_order_id": order.exchange_order_id if order else None,
            "is_protective": bool(order and order.stop_order_type),
        }

    def _finalize_protection_lifecycle(
        self, session, trade, closing_ids, slippage, observed, processing_run_id, policy_epoch
    ) -> None:
        """Make protective order evidence terminal and append deterministic lifecycle events."""
        orders = session.query(TradeExchangeOrder).filter_by(trade_log_id=trade.id).all()
        closing = set(closing_ids)
        for order in orders:
            if order.exchange_order_id in closing:
                order.order_status = "Filled"
                if order.stop_order_type:
                    order.role = "protective_exit"
            elif order.role == "protective" and order.order_status not in (
                "Filled", "Cancelled", "Deactivated", "Rejected"
            ):
                order.order_status = "Deactivated"
        terminal = []
        if slippage.get("exchange_order_id") and slippage.get("is_protective"):
            terminal.extend(("triggered", "filled"))
        terminal.extend(("position_closed", "reconciled"))
        for event_type in terminal:
            key = _sha({
                "trade": trade.id, "terminal": event_type,
                "closing_order_ids": sorted(closing),
            })
            if session.query(TradeProtectionEvent).filter_by(event_key=key).first():
                continue
            session.add(TradeProtectionEvent(
                event_key=key, run_id=trade.run_id,
                processing_run_id=processing_run_id, trade_log_id=trade.id,
                order_link_id=trade.order_link_id, observed_at=observed,
                symbol=trade.symbol, event_type=event_type,
                old_value=None, new_value=safe_json({
                    "closing_order_ids": sorted(closing),
                    "slippage_classification": slippage.get("classification"),
                }), exchange_order_id=slippage.get("exchange_order_id"),
                exchange_order_link_id=None,
                reason="exchange-confirmed terminal protection lifecycle",
                source_module="storage.telemetry", success=True,
                raw_exchange_status=None, policy_epoch=policy_epoch,
            ))

    def get_exit_slippage(self, order_link_id: str) -> Optional[dict]:
        session = self.db.get_session()
        try:
            row = session.query(TradeExitEvent).filter_by(order_link_id=order_link_id).first()
            if row is None:
                return None
            return {
                "classification": row.slippage_classification,
                "slippage_pct": safe_float(row.slippage_pct),
                "slippage_r": safe_float(row.slippage_r),
                "realized_r": safe_float(row.realized_r),
                "realized_pnl": safe_float(row.realized_pnl),
                "actual_exit_reason": row.actual_exit_reason,
                "exchange_order_id": (
                    (row.closing_order_ids or [None])[0]
                    if row.closing_order_ids else None
                ),
            }
        finally:
            session.close()

    def _finalize_excursion_samples(
        self, session, trade, excursion, records, executions_by_order, observed
    ):
        """Include only market/fill observations at or before confirmed closure."""
        entry = float(trade.entry_price)
        entry_time = self._actual_entry_time(session, trade)
        qty = safe_float(trade.entry_filled_qty) or (
            float(trade.size_usdt) / entry if entry else 0.0
        )
        original_sl = safe_float(trade.stop_loss_price)
        initial_risk = qty * abs(entry - original_sl) if qty and original_sl else None
        close_ms_values = []
        samples = []
        for record in records:
            close_ms = record.get("updatedTime") or record.get("createdTime")
            try:
                close_ms_values.append(int(close_ms))
            except (TypeError, ValueError):
                pass
            price = safe_float(record.get("avgExitPrice"))
            if price is not None:
                samples.append((price, from_epoch_ms(close_ms) or observed))
            for execution in executions_by_order.get(record.get("orderId"), []):
                execution_price = safe_float(execution.get("execPrice"))
                if execution_price is not None:
                    samples.append((
                        execution_price,
                        from_epoch_ms(execution.get("execTime")) or observed,
                    ))
        close_ms = max(close_ms_values) if close_ms_values else to_epoch_ms(observed)
        market_rows = session.query(Trade).filter(
            Trade.symbol == trade.symbol,
            Trade.ts >= (to_epoch_ms(entry_time) or 0),
            Trade.ts <= close_ms,
        ).order_by(Trade.ts.asc()).all()
        samples.extend((float(row.price), from_epoch_ms(row.ts)) for row in market_rows)
        if not samples:
            return excursion
        side = "short" if trade.action == "open_short" else "long"
        if excursion is None:
            excursion = TradeExcursion(
                run_id=self.run_id, trade_log_id=trade.id,
                last_processing_run_id=self.processing_run_id,
                order_link_id=trade.order_link_id, symbol=trade.symbol, side=side,
                entry_price=entry, initial_risk_usdt=initial_risk,
                sampling_method="persisted public trades plus exchange closing executions",
                sampling_limitations=(
                    "Extrema can be understated during collector gaps; public trades are not "
                    "an exchange-certified complete tick archive."
                ),
            )
            session.add(excursion)
        excursion.last_processing_run_id = self.processing_run_id
        favorable = max(samples, key=lambda item: item[0]) if side == "long" else min(
            samples, key=lambda item: item[0]
        )
        adverse = min(samples, key=lambda item: item[0]) if side == "long" else max(
            samples, key=lambda item: item[0]
        )
        favorable_distance = max(0.0, favorable[0] - entry if side == "long" else entry - favorable[0])
        adverse_distance = max(0.0, entry - adverse[0] if side == "long" else adverse[0] - entry)
        if excursion.mfe_price_distance is None or favorable_distance > float(excursion.mfe_price_distance):
            excursion.mfe_price_distance = favorable_distance
            excursion.mfe_pct = favorable_distance / entry * 100 if entry else None
            excursion.mfe_usdt = favorable_distance * qty
            excursion.mfe_r = favorable_distance * qty / initial_risk if initial_risk else None
            excursion.mfe_price, excursion.mfe_at, excursion.mfe_quantity = favorable[0], favorable[1], qty
            excursion.time_to_mfe_seconds = max(
                0, int((favorable[1] - entry_time).total_seconds())
            )
        if excursion.mae_price_distance is None or adverse_distance > float(excursion.mae_price_distance):
            excursion.mae_price_distance = adverse_distance
            excursion.mae_pct = adverse_distance / entry * 100 if entry else None
            excursion.mae_usdt = adverse_distance * qty
            excursion.mae_r = adverse_distance * qty / initial_risk if initial_risk else None
            excursion.mae_price, excursion.mae_at, excursion.mae_quantity = adverse[0], adverse[1], qty
            excursion.time_to_mae_seconds = max(
                0, int((adverse[1] - entry_time).total_seconds())
            )
        current_sl = safe_float(trade.tightened_stop_loss_price or trade.stop_loss_price)
        current_tp = safe_float(trade.tightened_take_profit_price or trade.take_profit_price)
        high, low = max(price for price, _ in samples), min(price for price, _ in samples)
        if current_tp is not None:
            reached = high >= current_tp if side == "long" else low <= current_tp
            excursion.tp_reached_intrabar = bool(excursion.tp_reached_intrabar or reached)
        if current_sl is not None:
            reached = low <= current_sl if side == "long" else high >= current_sl
            excursion.sl_reached_intrabar = bool(excursion.sl_reached_intrabar or reached)
        excursion.last_observed_at = observed
        excursion.last_market_timestamp = max(ts for _, ts in samples)
        trade.mfe_pct, trade.mae_pct = excursion.mfe_pct, excursion.mae_pct
        return excursion

    @staticmethod
    def _excursion_payload(excursion, prefix: str) -> dict:
        occurred_at = ensure_aware_utc(getattr(excursion, f"{prefix}_at"))
        return {
            "price_distance": safe_float(getattr(excursion, f"{prefix}_price_distance")),
            "pct": safe_float(getattr(excursion, f"{prefix}_pct")),
            "usdt": safe_float(getattr(excursion, f"{prefix}_usdt")),
            "r": safe_float(getattr(excursion, f"{prefix}_r")),
            "price": safe_float(getattr(excursion, f"{prefix}_price")),
            "at": occurred_at.isoformat() if occurred_at else None,
        }
