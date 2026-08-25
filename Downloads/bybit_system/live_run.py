"""Prepare, launch, inspect, and safely stop one isolated Testnet run."""

import argparse
import hashlib
import importlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.engine import make_url

from config.settings import BybitConfig
from execution.execution_engine import ExecutionEngine
from runtime_control import DatabaseProcessLock, RUNTIME_DIR
from storage.db import Database
from storage.journal import TradeJournal
from storage.migrations import run_safe_migrations
from storage.durability import StorageGuard, apply_high_frequency_retention
from storage.models import Candle, OrderbookSnapshot, PositionSnapshot, RunMetadata, TradeLog
from storage.telemetry import TelemetryStore
from operator_monitor import OperatorMonitor
from timeutils import utcnow

ROOT = Path(__file__).resolve().parent
CURRENT_RUN = RUNTIME_DIR / "current_run.json"
SUPERVISOR_INFO = RUNTIME_DIR / "supervisor.json"
logger = logging.getLogger(__name__)


@dataclass
class CollectorRestartPolicy:
    """Bounded process-level backoff, reset only after a stable collector."""

    initial_seconds: float = 5.0
    maximum_seconds: float = 60.0
    stable_reset_seconds: float = 300.0
    monotonic_fn: object = time.monotonic

    def __post_init__(self):
        self.initial_seconds = max(0.1, float(self.initial_seconds))
        self.maximum_seconds = max(self.initial_seconds, float(self.maximum_seconds))
        self.stable_reset_seconds = max(0.0, float(self.stable_reset_seconds))
        self.failures = 0
        self.started_at = None

    def started(self) -> None:
        self.started_at = self.monotonic_fn()

    def failure_delay(self) -> float:
        if (
            self.started_at is not None
            and self.monotonic_fn() - self.started_at >= self.stable_reset_seconds
        ):
            self.failures = 0
        delay = min(
            self.maximum_seconds,
            self.initial_seconds * (2 ** self.failures),
        )
        self.failures += 1
        self.started_at = None
        return delay


def _load_env_file() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'").strip('"')
        key = key.strip()
        if not os.environ.get(key):
            os.environ[key] = value


def _reload_config():
    """settings.py uses import-time defaults, so reload after populating the environment."""
    global BybitConfig
    import config.settings as settings
    BybitConfig = importlib.reload(settings).BybitConfig


def _commit_sha() -> str:
    configured = os.getenv("COMMIT_SHA") or os.getenv("RAILWAY_GIT_COMMIT_SHA")
    if configured:
        return configured.strip()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Commit SHA is unavailable; set COMMIT_SHA in this container"
        ) from exc


def _process_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _service_info(service: str) -> dict:
    path = RUNTIME_DIR / f"{service}.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_supervisor_info(run_id: str) -> None:
    RUNTIME_DIR.mkdir(exist_ok=True)
    SUPERVISOR_INFO.write_text(json.dumps({
        "service": "supervisor",
        "run_id": run_id,
        "pid": os.getpid(),
        "started_at": utcnow().isoformat(),
    }, indent=2), encoding="utf-8")


def _clear_own_supervisor_info() -> None:
    info = _service_info("supervisor")
    if info.get("pid") == os.getpid():
        try:
            SUPERVISOR_INFO.unlink()
        except FileNotFoundError:
            pass


def _validate_runtime_config(cfg: BybitConfig) -> None:
    if cfg.runtime_mode not in ("local", "railway"):
        raise RuntimeError("RUNTIME_MODE must be either 'local' or 'railway'")
    if getattr(cfg, "telegram_alerts_enabled", False):
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or not os.getenv(
            "TELEGRAM_CHAT_ID", ""
        ).strip():
            raise RuntimeError(
                "TELEGRAM_ALERTS_ENABLED=true requires TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID"
            )
    if cfg.runtime_mode != "railway":
        return

    raw_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_url:
        raise RuntimeError(
            "DATABASE_URL is required in Railway mode; local fallback is forbidden"
        )
    try:
        url = make_url(raw_url)
    except Exception as exc:
        raise RuntimeError("DATABASE_URL is invalid in Railway mode") from exc
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("Railway mode requires a PostgreSQL DATABASE_URL")
    host = (url.host or "").strip("[]").lower()
    if host in ("", "localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
        raise RuntimeError(
            "Railway mode requires external PostgreSQL; localhost DATABASE_URL is forbidden"
        )
    raw_capacity = os.getenv("STORAGE_MAX_DATABASE_BYTES", "").strip()
    if not raw_capacity:
        raise RuntimeError(
            "STORAGE_MAX_DATABASE_BYTES is required in Railway mode so the bot can "
            "block new entries before PostgreSQL fills again"
        )
    try:
        capacity = int(raw_capacity)
    except ValueError as exc:
        raise RuntimeError(
            "STORAGE_MAX_DATABASE_BYTES must be a positive integer in Railway mode"
        ) from exc
    if capacity <= 0:
        raise RuntimeError(
            "STORAGE_MAX_DATABASE_BYTES is required in Railway mode so the bot can "
            "block new entries before PostgreSQL fills again"
        )


def _find_resumable_run(db: Database, commit_sha: str) -> Optional[RunMetadata]:
    """Find the durable active Testnet run without consulting ephemeral files."""
    session = db.get_session()
    try:
        candidates = (
            session.query(RunMetadata)
            .filter(
                RunMetadata.commit_sha == commit_sha,
                RunMetadata.status.in_(("starting", "running")),
            )
            .order_by(RunMetadata.started_at.desc())
            .all()
        )
        for row in candidates:
            summary = row.environment_summary or {}
            if summary.get("testnet") is True:
                session.expunge(row)
                return row
        return None
    finally:
        session.close()


def _assert_clean_exchange(cfg: BybitConfig, db: Database) -> dict:
    """Accept only an empty account or deterministically owned protected inheritance."""
    execution = ExecutionEngine(cfg)
    account = execution.get_account_state()
    positions = [
        p for p in execution.get_open_positions()
        if float(p.get("size") or 0) > 0
    ]
    session = db.get_session()
    try:
        open_trades = session.query(TradeLog).filter_by(status="open").all()
        for trade in open_trades:
            session.expunge(trade)
    finally:
        session.close()

    inherited = []
    owned_order_ids: set[str] = set()
    live_symbols: set[str] = set()
    for position in positions:
        symbol = position.get("symbol")
        live_symbols.add(symbol)
        action = "open_long" if position.get("side") == "Buy" else "open_short"
        candidates = [
            trade for trade in open_trades
            if trade.symbol == symbol and trade.action == action and trade.run_id
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"Testnet inherited position {symbol} has {len(candidates)} deterministic owners"
            )
        trade = candidates[0]
        exchange_qty = float(position.get("size") or 0)
        journal_qty = float(trade.entry_filled_qty or 0)
        exchange_entry = float(position.get("avgPrice") or 0)
        journal_entry = float(trade.entry_price or 0)
        qty_tolerance = max(1e-8, abs(exchange_qty) * 1e-6)
        price_tolerance = max(1e-8, abs(exchange_entry) * 1e-6)
        if journal_qty <= 0 or abs(exchange_qty - journal_qty) > qty_tolerance:
            raise RuntimeError(f"Inherited {symbol} quantity does not match its owner trade")
        if journal_entry <= 0 or abs(exchange_entry - journal_entry) > price_tolerance:
            raise RuntimeError(f"Inherited {symbol} entry does not match its owner trade")
        if not position.get("stopLoss") or not position.get("takeProfit"):
            raise RuntimeError(f"Inherited {symbol} lacks exchange-native SL/TP")

        response = execution.session.get_open_orders(
            category=cfg.category, symbol=symbol, openOnly=0, limit=50
        )
        active = [
            order for order in response["result"]["list"]
            if order.get("orderStatus") in ("New", "PartiallyFilled", "Untriggered")
        ]
        protective = [order for order in active if order.get("reduceOnly")]
        kinds = {order.get("stopOrderType") for order in protective}
        if not {"StopLoss", "TakeProfit"}.issubset(kinds):
            raise RuntimeError(f"Inherited {symbol} lacks active reduceOnly SL/TP orders")
        current_ids = {order.get("orderId") for order in protective if order.get("orderId")}
        session = db.get_session()
        try:
            snapshot = session.query(PositionSnapshot).filter_by(
                trade_log_id=trade.id
            ).order_by(PositionSnapshot.observed_at.desc()).first()
            expected_ids = set(snapshot.protective_order_ids or []) if snapshot else set()
        finally:
            session.close()
        if expected_ids and current_ids != expected_ids:
            if not _replacement_protection_owned_by_trade(
                cfg, trade, position, protective
            ):
                raise RuntimeError(
                    f"Inherited {symbol} protective order IDs changed unexpectedly"
                )
            logger.warning(
                "Inherited %s protective IDs changed through a strongly-owned "
                "exchange replacement; accepting current read-back",
                symbol,
            )
        owned_order_ids.update(current_ids)
        inherited.append({
            "classification": "inherited_live_protected",
            "owner_run_id": trade.run_id,
            "trade_log_id": trade.id,
            "order_link_id": trade.order_link_id,
            "symbol": symbol,
            "side": action,
            "quantity": exchange_qty,
            "average_entry": exchange_entry,
            "stop_loss": position.get("stopLoss"),
            "take_profit": position.get("takeProfit"),
            "protective_order_ids": sorted(current_ids),
        })

    for trade in open_trades:
        if trade.symbol not in live_symbols:
            inherited.append({
                "classification": "inherited_pending_reconciliation",
                "owner_run_id": trade.run_id,
                "trade_log_id": trade.id,
                "order_link_id": trade.order_link_id,
                "symbol": trade.symbol,
                "side": trade.action,
            })

    active_orders = []
    for symbol in cfg.symbols:
        response = execution.session.get_open_orders(
            category=cfg.category, symbol=symbol, openOnly=0, limit=50
        )
        active_orders.extend(
            order for order in response["result"]["list"]
            if order.get("orderStatus") in ("New", "PartiallyFilled", "Untriggered")
        )
    unexpected = [
        order for order in active_orders
        if order.get("orderId") not in owned_order_ids
    ]
    if unexpected:
        raise RuntimeError(f"Testnet has {len(unexpected)} unowned active order(s)")
    account = dict(account)
    account["inherited_positions"] = inherited
    return account


def _replacement_protection_owned_by_trade(
    cfg: BybitConfig, trade: TradeLog, position: dict, protective: list[dict]
) -> bool:
    """Validate replacement child IDs by stable parent ownership and live terms."""
    required = {"StopLoss", "TakeProfit"}
    by_kind = {
        order.get("stopOrderType"): order
        for order in protective
        if order.get("stopOrderType") in required
    }
    if set(by_kind) != required or not trade.order_link_id:
        return False
    expected_source = getattr(cfg, "protective_trigger_by", "LastPrice")
    expected_side = "Sell" if trade.action == "open_long" else "Buy"
    expected_qty = float(position.get("size") or 0)
    expected_prices = {
        "StopLoss": float(position.get("stopLoss") or 0),
        "TakeProfit": float(position.get("takeProfit") or 0),
    }
    if expected_qty <= 0 or not all(expected_prices.values()):
        return False
    for kind, order in by_kind.items():
        if order.get("parentOrderLinkId") != trade.order_link_id:
            return False
        if order.get("triggerBy") != expected_source:
            return False
        if order.get("side") != expected_side or not order.get("reduceOnly"):
            return False
        order_qty = float(order.get("qty") or order.get("leavesQty") or 0)
        qty_tolerance = max(1e-8, abs(expected_qty) * 1e-6)
        if abs(order_qty - expected_qty) > qty_tolerance:
            return False
        trigger_price = float(order.get("triggerPrice") or 0)
        expected_price = expected_prices[kind]
        price_tolerance = max(1e-8, abs(expected_price) * 1e-6)
        if abs(trigger_price - expected_price) > price_tolerance:
            return False
    return True


def _environment_summary(cfg: BybitConfig) -> dict:
    source_files = sorted(
        path for path in ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and ".runtime" not in path.parts
    )
    digest = hashlib.sha256()
    for path in source_files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    try:
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--", str(ROOT)],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        dirty = False
    return {
        "runtime_mode": cfg.runtime_mode,
        "testnet": cfg.testnet,
        "paper_trading": cfg.paper_trading,
        "trading_enabled_at_launch": True,
        "python": sys.version.split()[0],
        "symbols": list(cfg.symbols),
        "primary_interval": cfg.primary_interval,
        "database_host": "localhost" if "localhost" in cfg.db_url else "configured",
        "working_tree_dirty": dirty,
        "source_sha256": digest.hexdigest(),
    }


def _prepare(
    acquire_supervisor_lock: bool = False,
) -> tuple[str, BybitConfig, Database, Optional[DatabaseProcessLock]]:
    _load_env_file()
    requested_testnet = os.getenv("BYBIT_TESTNET", "")
    requested_runtime_mode = os.getenv("RUNTIME_MODE", "local").strip().lower()
    if requested_runtime_mode == "railway" and requested_testnet.lower() != "true":
        raise RuntimeError("BYBIT_TESTNET=true is mandatory in Railway mode")
    os.environ["BYBIT_TESTNET"] = "true"
    os.environ["TRADING_ENABLED"] = "true"
    _reload_config()
    cfg = BybitConfig()
    _validate_runtime_config(cfg)
    if not cfg.testnet:
        raise RuntimeError("Testnet safety gate failed")
    if not (cfg.api_key and cfg.api_secret):
        raise RuntimeError("Testnet API credentials are missing")
    if cfg.runtime_mode == "local":
        for service in ("collector", "trader"):
            info = _service_info(service)
            if _process_alive(info.get("pid")):
                raise RuntimeError(f"Duplicate {service} process is already alive")

    db = Database(cfg)
    if not db.check_connection():
        raise RuntimeError("Database is unavailable")
    run_safe_migrations(db.engine)
    deleted = apply_high_frequency_retention(db.engine, cfg)
    logger.info("High-frequency retention applied before run: %s", deleted)
    storage = StorageGuard(db, cfg).status()
    if not storage["entry_allowed"]:
        # Capacity pressure must fail closed for *new entries*, not prevent the
        # process from supervising exchange-native protection and existing
        # exposure. StrategyEngine applies the same guard before every entry.
        logger.critical(
            "PostgreSQL storage safety gate blocks new entries: %s; "
            "startup continues for open-position management",
            storage["reason"],
        )
    if cfg.runtime_mode == "railway" and db.engine.dialect.name != "postgresql":
        raise RuntimeError("Railway durability check failed: database is not PostgreSQL")

    supervisor_lock = None
    if acquire_supervisor_lock:
        supervisor_lock = DatabaseProcessLock(db, "supervisor")
        supervisor_lock.start()

    journal = TradeJournal(db)
    remaining = journal.get_orphaned_trades()
    if remaining:
        if supervisor_lock is not None:
            supervisor_lock.stop()
        raise RuntimeError(f"{len(remaining)} active orphaned trade(s) remain")

    sha = _commit_sha()
    # Durable run identity lives in PostgreSQL in every runtime mode.  Local
    # PID files are deliberately ephemeral and must not decide whether a run
    # can be recovered after a clean process restart.
    durable_run = _find_resumable_run(db, sha)
    startup_account = None
    if durable_run is not None:
        run_id = durable_run.run_id
        started_at = durable_run.started_at
    else:
        try:
            startup_account = _assert_clean_exchange(cfg, db)
        except Exception:
            if supervisor_lock is not None:
                supervisor_lock.stop()
            raise
        run_id = (
            f"testnet-{utcnow():%Y%m%dT%H%M%SZ}-"
            f"{sha[:7]}-{secrets.token_hex(3)}"
        )
        started_at = utcnow()
        session = db.get_session()
        try:
            session.add(RunMetadata(
                run_id=run_id,
                commit_sha=sha,
                started_at=started_at,
                environment_summary=_environment_summary(cfg),
                status="starting",
            ))
            session.commit()
        finally:
            session.close()
    # Capture values after every default/env override has been resolved.  The
    # scientific run row is immutable; a changed config creates a policy epoch.
    cfg.run_id = run_id
    cfg.commit_sha = sha
    telemetry = TelemetryStore(db, cfg)
    telemetry.ensure_run(
        root=ROOT,
        started_at=started_at,
        startup_account_snapshot=startup_account,
        reason="new run" if durable_run is None else "container restart recovery",
    )
    if startup_account is not None:
        telemetry.persist_account_snapshot(startup_account, [], observed_at=started_at)
        for inherited in startup_account.get("inherited_positions", []):
            telemetry.record_health(
                "cross_run_recovery", inherited["classification"], "info", "classified",
                symbol=inherited.get("symbol"), details=inherited,
                observed_at=started_at,
            )
    RUNTIME_DIR.mkdir(exist_ok=True)
    CURRENT_RUN.write_text(
        json.dumps({"run_id": run_id, "commit_sha": sha}, indent=2),
        encoding="utf-8",
    )
    return run_id, cfg, db, supervisor_lock


def _spawn_process(script: str, run_id: str, sha: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "BYBIT_TESTNET": "true",
        "TRADING_ENABLED": "true",
        "RUN_ID": run_id,
        "COMMIT_SHA": sha,
        "PYTHONUNBUFFERED": "1",
    })
    railway_mode = env.get("RUNTIME_MODE", "local").strip().lower() == "railway"
    return subprocess.Popen(
        [sys.executable, "-u", str(ROOT / script)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=None if railway_mode else subprocess.DEVNULL,
        stderr=None if railway_mode else subprocess.DEVNULL,
        start_new_session=True,
    )


def _spawn(script: str, run_id: str, sha: str) -> int:
    return _spawn_process(script, run_id, sha).pid


def _wait_for_collector(
    db: Database,
    run_id: str,
    timeout: int = 180,
    expected_pid: Optional[int] = None,
    max_candle_age_minutes: int = 45,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _service_info("collector")
        if (
            info
            and info.get("run_id") == run_id
            and (expected_pid is None or info.get("pid") == expected_pid)
            and not _process_alive(info.get("pid"))
        ):
            raise RuntimeError("Collector exited during refresh")
        session = db.get_session()
        try:
            run = session.query(RunMetadata).filter_by(run_id=run_id).first()
            latest_candle = session.query(func.max(Candle.start_time)).scalar()
            latest_book = session.query(func.max(OrderbookSnapshot.ts)).scalar()
        finally:
            session.close()
        now_ms = int(time.time() * 1000)
        expected_service_ready = (
            expected_pid is None
            or bool(
                info
                and info.get("run_id") == run_id
                and info.get("pid") == expected_pid
                and _process_alive(expected_pid)
            )
        )
        if (
            run and run.collector_heartbeat_at
            and expected_service_ready
            and latest_candle
            and now_ms - int(latest_candle) < max(1, max_candle_age_minutes) * 60 * 1000
            and latest_book and now_ms - int(latest_book) < 2 * 60 * 1000
        ):
            return
        time.sleep(2)
    raise RuntimeError("Market data did not become fresh before timeout")


def _spawn_detached_supervisor() -> subprocess.Popen:
    """Start the same foreground supervisor used by Railway as a local daemon."""
    env = os.environ.copy()
    env.update({"BYBIT_TESTNET": "true", "TRADING_ENABLED": "true", "PYTHONUNBUFFERED": "1"})
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    stream = (log_dir / "supervisor.log").open("a", encoding="utf-8")
    try:
        return subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "live_run.py"), "run"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
        )
    finally:
        stream.close()


def cmd_start(_args) -> int:
    """Local detached mode, always backed by the restart-capable supervisor."""
    _load_env_file()
    _reload_config()
    cfg = BybitConfig()
    _validate_runtime_config(cfg)
    if os.getenv("BYBIT_TESTNET", "").lower() != "true" or not cfg.testnet:
        raise RuntimeError("Testnet safety gate failed")
    for service in ("supervisor", "collector", "trader"):
        info = _service_info(service)
        if _process_alive(info.get("pid")):
            raise RuntimeError(f"Duplicate {service} process is already alive")

    supervisor = _spawn_detached_supervisor()
    deadline = time.time() + 300
    while time.time() < deadline:
        code = supervisor.poll()
        if code is not None:
            raise RuntimeError(
                f"Supervisor exited during startup with status {code}; "
                "inspect logs/supervisor.log"
            )
        supervisor_info = _service_info("supervisor")
        collector_info = _service_info("collector")
        trader_info = _service_info("trader")
        if (
            supervisor_info.get("pid") == supervisor.pid
            and _process_alive(collector_info.get("pid"))
            and _process_alive(trader_info.get("pid"))
        ):
            print(f"supervisor_pid={supervisor.pid}", flush=True)
            print(f"collector_pid={collector_info['pid']}", flush=True)
            print(f"trader_pid={trader_info['pid']}", flush=True)
            print(f"run_id={trader_info.get('run_id')}", flush=True)
            return 0
        time.sleep(1)
    supervisor.terminate()
    raise RuntimeError("Supervised local startup did not become ready within 300 seconds")


def _terminate_children(children: list[subprocess.Popen]) -> None:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
    deadline = time.time() + 15
    for child in reversed(children):
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _record_supervisor_health(telemetry, event_type, severity, status, **details) -> None:
    try:
        telemetry.record_health(
            "process_supervisor", event_type, severity, status, details=details,
        )
    except Exception:
        logger.exception("Unable to persist supervisor health event %s", event_type)


def _start_collector_with_recovery(db, cfg, run_id, sha, telemetry, policy):
    """Recreate collector until it is alive and fresh; never stop the trader."""
    while True:
        collector = _spawn_process("main.py", run_id, sha)
        policy.started()
        try:
            _wait_for_collector(
                db, run_id, expected_pid=collector.pid,
                max_candle_age_minutes=cfg.max_candle_age_minutes,
            )
            _record_supervisor_health(
                telemetry, "collector_process_recovered", "info", "recovered",
                collector_pid=collector.pid, restart_failures=policy.failures,
            )
            return collector
        except Exception as exc:
            if collector.poll() is None:
                collector.terminate()
                try:
                    collector.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    collector.kill()
                    collector.wait(timeout=5)
            delay = policy.failure_delay()
            _record_supervisor_health(
                telemetry, "collector_process_restart_failed", "error", "retrying",
                collector_pid=collector.pid, exit_code=collector.poll(),
                retry_in_seconds=delay, error=repr(exc),
            )
            print(
                f"collector restart failed; retrying in {delay:.1f}s: {exc}",
                flush=True,
            )
            time.sleep(delay)


def cmd_run(_args) -> int:
    """Railway foreground supervisor: keep children attached and observable."""
    run_id, _cfg, db, supervisor_lock = _prepare(acquire_supervisor_lock=True)
    sha = _commit_sha()
    children: list[subprocess.Popen] = []
    telemetry = TelemetryStore(db, _cfg)
    restart_policy = CollectorRestartPolicy(
        initial_seconds=_cfg.collector_restart_initial_seconds,
        maximum_seconds=_cfg.collector_restart_max_seconds,
        stable_reset_seconds=_cfg.collector_restart_stable_reset_seconds,
    )
    _write_supervisor_info(run_id)
    operator_monitor = OperatorMonitor(db, _cfg, run_id)
    operator_monitor.start()

    def stop_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    try:
        collector = _start_collector_with_recovery(
            db, _cfg, run_id, sha, telemetry, restart_policy,
        )
        children.append(collector)
        print(f"collector_pid={collector.pid}", flush=True)

        trader = _spawn_process("trading_main.py", run_id, sha)
        children.append(trader)
        print(f"trader_pid={trader.pid}", flush=True)
        print(f"run_id={run_id}", flush=True)

        while True:
            trader_code = trader.poll()
            if trader_code is not None:
                raise RuntimeError(
                    f"trader process exited unexpectedly with status {trader_code}"
                )
            collector_code = collector.poll()
            if collector_code is not None:
                delay = restart_policy.failure_delay()
                _record_supervisor_health(
                    telemetry, "collector_process_exit", "critical", "restarting",
                    exit_code=collector_code, retry_in_seconds=delay,
                    existing_position_management_continues=True,
                    new_entries_fail_closed_on_stale_data=True,
                )
                print(
                    f"collector exited with status {collector_code}; "
                    f"restarting in {delay:.1f}s",
                    flush=True,
                )
                time.sleep(delay)
                collector = _start_collector_with_recovery(
                    db, _cfg, run_id, sha, telemetry, restart_policy,
                )
                children.append(collector)
                print(f"collector_restarted_pid={collector.pid}", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("Railway supervisor stopping children", flush=True)
        return 0
    finally:
        _terminate_children(children)
        operator_monitor.stop()
        if supervisor_lock is not None:
            supervisor_lock.stop()
        _clear_own_supervisor_info()


def cmd_status(_args) -> int:
    _load_env_file()
    _reload_config()
    cfg = BybitConfig()
    _validate_runtime_config(cfg)
    db = Database(cfg)
    current = None
    if cfg.runtime_mode == "railway":
        row = _find_resumable_run(db, _commit_sha())
        if row is not None:
            current = {"run_id": row.run_id, "commit_sha": row.commit_sha}
    elif CURRENT_RUN.exists():
        current = json.loads(CURRENT_RUN.read_text(encoding="utf-8"))
    if current is None:
        print("No current run")
        return 1
    session = db.get_session()
    try:
        row = session.query(RunMetadata).filter_by(run_id=current["run_id"]).first()
        if row is None:
            print("Current run is missing from the database")
            return 1
        print(f"run_id={row.run_id}")
        print(f"commit_sha={row.commit_sha}")
        print(f"status={row.status}")
        for service in ("collector", "trader"):
            info = _service_info(service)
            heartbeat = getattr(row, f"{service}_heartbeat_at")
            print(
                f"{service}_pid={info.get('pid')} "
                f"alive={_process_alive(info.get('pid'))} heartbeat={heartbeat}"
            )
        supervisor = _service_info("supervisor")
        print(
            f"supervisor_pid={supervisor.get('pid')} "
            f"alive={_process_alive(supervisor.get('pid'))}"
        )
    finally:
        session.close()
    return 0


def cmd_stop(_args) -> int:
    _load_env_file()
    _reload_config()
    cfg = BybitConfig()
    supervisor = _service_info("supervisor")
    supervisor_pid = supervisor.get("pid")
    if _process_alive(supervisor_pid):
        command = subprocess.check_output(
            ["ps", "-p", str(supervisor_pid), "-o", "command="], text=True
        )
        if "live_run.py run" not in command:
            raise RuntimeError(
                f"PID {supervisor_pid} does not match live_run.py run; refusing to signal"
            )
        os.kill(int(supervisor_pid), signal.SIGTERM)
        print(f"stopping_supervisor_pid={supervisor_pid}")
        deadline = time.time() + 20
        while _process_alive(supervisor_pid) and time.time() < deadline:
            time.sleep(0.2)

    # Backward-compatible cleanup for old unsupervised local launches and for
    # a supervisor that did not terminate its children within the grace time.
    for service in ("trader", "collector"):
        info = _service_info(service)
        pid = info.get("pid")
        if not _process_alive(pid):
            continue
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="], text=True
        )
        expected = "trading_main.py" if service == "trader" else "main.py"
        if expected not in command:
            raise RuntimeError(f"PID {pid} does not match {expected}; refusing to signal")
        os.kill(int(pid), signal.SIGTERM)
        print(f"stopping_{service}_pid={pid}")
    if CURRENT_RUN.exists():
        current = json.loads(CURRENT_RUN.read_text(encoding="utf-8"))
        cfg.run_id = current.get("run_id", "")
        cfg.commit_sha = current.get("commit_sha", "")
        if cfg.run_id:
            db = Database(cfg)
            TelemetryStore(db, cfg).finish_run()
            session = db.get_session()
            try:
                row = session.query(RunMetadata).filter_by(run_id=cfg.run_id).first()
                if row is not None and row.stopped_at is None:
                    row.status = "stopped"
                    row.stopped_at = utcnow()
                    session.commit()
            finally:
                session.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()
    handlers = {
        "start": cmd_start,
        "run": cmd_run,
        "status": cmd_status,
        "stop": cmd_stop,
    }
    raise SystemExit(handlers[args.command](args))


if __name__ == "__main__":
    main()
