"""Prepare, launch, inspect, and safely stop one isolated Testnet run."""

import argparse
import hashlib
import importlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import func

from config.settings import BybitConfig
from execution.execution_engine import ExecutionEngine
from runtime_control import RUNTIME_DIR
from storage.db import Database
from storage.journal import TradeJournal
from storage.migrations import run_safe_migrations
from storage.models import Candle, OrderbookSnapshot, RunMetadata
from timeutils import utcnow

ROOT = Path(__file__).resolve().parent
CURRENT_RUN = RUNTIME_DIR / "current_run.json"


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
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _assert_clean_exchange(cfg: BybitConfig) -> None:
    execution = ExecutionEngine(cfg)
    positions = [
        p for p in execution.get_open_positions()
        if float(p.get("size") or 0) > 0
    ]
    if positions:
        raise RuntimeError(f"Testnet has {len(positions)} live position(s)")
    active_orders = []
    for symbol in cfg.symbols:
        response = execution.session.get_open_orders(
            category=cfg.category, symbol=symbol, openOnly=0, limit=50
        )
        active_orders.extend(
            order for order in response["result"]["list"]
            if order.get("orderStatus") in ("New", "PartiallyFilled", "Untriggered")
        )
    if active_orders:
        raise RuntimeError(f"Testnet has {len(active_orders)} active order(s)")


def _environment_summary(cfg: BybitConfig) -> dict:
    source_files = sorted(
        path for path in ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and ".runtime" not in path.parts
    )
    digest = hashlib.sha256()
    for path in source_files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--", str(ROOT)],
        cwd=ROOT,
        text=True,
    ).strip())
    return {
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


def _prepare() -> tuple[str, BybitConfig, Database]:
    _load_env_file()
    os.environ["BYBIT_TESTNET"] = "true"
    os.environ["TRADING_ENABLED"] = "true"
    _reload_config()
    cfg = BybitConfig()
    if not cfg.testnet:
        raise RuntimeError("Testnet safety gate failed")
    if not (cfg.api_key and cfg.api_secret):
        raise RuntimeError("Testnet API credentials are missing")
    for service in ("collector", "trader"):
        info = _service_info(service)
        if _process_alive(info.get("pid")):
            raise RuntimeError(f"Duplicate {service} process is already alive")

    db = Database(cfg)
    if not db.check_connection():
        raise RuntimeError("Database is unavailable")
    run_safe_migrations(db.engine)
    journal = TradeJournal(db)
    remaining = journal.get_orphaned_trades()
    if remaining:
        raise RuntimeError(f"{len(remaining)} active orphaned trade(s) remain")
    _assert_clean_exchange(cfg)

    sha = _commit_sha()
    run_id = (
        f"testnet-{utcnow():%Y%m%dT%H%M%SZ}-"
        f"{sha[:7]}-{secrets.token_hex(3)}"
    )
    session = db.get_session()
    try:
        session.add(RunMetadata(
            run_id=run_id,
            commit_sha=sha,
            started_at=utcnow(),
            environment_summary=_environment_summary(cfg),
            status="starting",
        ))
        session.commit()
    finally:
        session.close()
    RUNTIME_DIR.mkdir(exist_ok=True)
    CURRENT_RUN.write_text(
        json.dumps({"run_id": run_id, "commit_sha": sha}, indent=2),
        encoding="utf-8",
    )
    return run_id, cfg, db


def _spawn(script: str, run_id: str, sha: str) -> int:
    env = os.environ.copy()
    env.update({
        "BYBIT_TESTNET": "true",
        "TRADING_ENABLED": "true",
        "RUN_ID": run_id,
        "COMMIT_SHA": sha,
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / script)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _wait_for_collector(db: Database, run_id: str, timeout: int = 180) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _service_info("collector")
        if info and not _process_alive(info.get("pid")):
            raise RuntimeError("Collector exited during refresh")
        session = db.get_session()
        try:
            run = session.query(RunMetadata).filter_by(run_id=run_id).first()
            latest_candle = session.query(func.max(Candle.start_time)).scalar()
            latest_book = session.query(func.max(OrderbookSnapshot.ts)).scalar()
        finally:
            session.close()
        now_ms = int(time.time() * 1000)
        if (
            run and run.collector_heartbeat_at
            and latest_candle and now_ms - int(latest_candle) < 20 * 60 * 1000
            and latest_book and now_ms - int(latest_book) < 2 * 60 * 1000
        ):
            return
        time.sleep(2)
    raise RuntimeError("Market data did not become fresh before timeout")


def cmd_start(_args) -> int:
    run_id, _cfg, db = _prepare()
    sha = _commit_sha()
    collector_pid = _spawn("main.py", run_id, sha)
    print(f"collector_pid={collector_pid}")
    _wait_for_collector(db, run_id)
    trader_pid = _spawn("trading_main.py", run_id, sha)
    print(f"trader_pid={trader_pid}")
    print(f"run_id={run_id}")
    return 0


def cmd_status(_args) -> int:
    _load_env_file()
    _reload_config()
    if not CURRENT_RUN.exists():
        print("No current run")
        return 1
    current = json.loads(CURRENT_RUN.read_text(encoding="utf-8"))
    cfg = BybitConfig()
    db = Database(cfg)
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
    finally:
        session.close()
    return 0


def cmd_stop(_args) -> int:
    _load_env_file()
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
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()
    handlers = {"start": cmd_start, "status": cmd_status, "stop": cmd_stop}
    raise SystemExit(handlers[args.command](args))


if __name__ == "__main__":
    main()
