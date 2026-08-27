"""Small Unix process lock and database heartbeat for long-running services."""

import atexit
import fcntl
import json
import logging
import os
import threading
from pathlib import Path

from sqlalchemy import text

from storage.models import RunMetadata
from timeutils import utcnow

logger = logging.getLogger(__name__)


RUNTIME_DIR = Path(__file__).resolve().parent / ".runtime"
_DATABASE_LOCK_IDS = {
    "supervisor": 4_259_001_001,
    "collector": 4_259_001_002,
    "trader": 4_259_001_003,
}


class DuplicateProcessError(RuntimeError):
    """Raised when another container already owns a singleton service lock."""


class DatabaseProcessLock:
    """PostgreSQL advisory lock that prevents duplicates across containers."""

    def __init__(self, db, service: str):
        if service not in _DATABASE_LOCK_IDS:
            raise ValueError(f"Unknown lock service: {service}")
        self.db = db
        self.service = service
        self._connection = None

    def start(self) -> None:
        if self.db.engine.dialect.name != "postgresql":
            return
        connection = self.db.engine.connect()
        try:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _DATABASE_LOCK_IDS[self.service]},
            ).scalar()
            if not acquired:
                raise DuplicateProcessError(
                    f"Duplicate {self.service} process is already running"
                )
            self._connection = connection
        except Exception:
            connection.close()
            raise

    def stop(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _DATABASE_LOCK_IDS[self.service]},
            )
        finally:
            self._connection.close()
            self._connection = None


class RuntimeService:
    def __init__(
        self, db, run_id: str, service: str, interval_sec: int = 10,
        health_callback=None,
    ):
        if service not in ("collector", "trader"):
            raise ValueError(f"Unknown runtime service: {service}")
        if not run_id:
            raise RuntimeError("RUN_ID is required for supervised live services")
        self.db = db
        self.run_id = run_id
        self.service = service
        self.interval_sec = interval_sec
        self.health_callback = health_callback
        self._heartbeat_failed = False
        self._lock_file = None
        self._database_lock = DatabaseProcessLock(db, service)
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        RUNTIME_DIR.mkdir(exist_ok=True)
        lock_path = RUNTIME_DIR / f"{self.service}.lock"
        self._lock_file = lock_path.open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Duplicate {self.service} process is already running") from exc

        try:
            self._database_lock.start()
            info = {
                "service": self.service,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "started_at": utcnow().isoformat(),
            }
            (RUNTIME_DIR / f"{self.service}.json").write_text(
                json.dumps(info, indent=2), encoding="utf-8"
            )
            self._heartbeat()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"{self.service}-heartbeat",
                daemon=True,
            )
            self._thread.start()
            atexit.register(self.stop)
        except Exception:
            self._database_lock.stop()
            self._lock_file.close()
            self._lock_file = None
            raise

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                self._heartbeat()
                if self._heartbeat_failed and self.health_callback:
                    self.health_callback(
                        f"{self.service}_heartbeat_recovered", "info", "recovered", None
                    )
                self._heartbeat_failed = False
            except Exception as exc:
                self._heartbeat_failed = True
                logger.exception("%s heartbeat write failed; retrying", self.service)
                if self.health_callback:
                    self.health_callback(
                        f"{self.service}_heartbeat_failure", "error", "failed", exc
                    )

    def _heartbeat(self) -> None:
        session = self.db.get_session()
        try:
            row = session.query(RunMetadata).filter(
                RunMetadata.run_id == self.run_id
            ).first()
            if row is None:
                raise RuntimeError(f"Unknown RUN_ID: {self.run_id}")
            now = utcnow()
            if self.service == "collector":
                row.collector_pid = os.getpid()
                row.collector_heartbeat_at = now
            else:
                row.trader_pid = os.getpid()
                row.trader_heartbeat_at = now
            row.status = "running"
            session.commit()
        finally:
            session.close()

    def stop(self) -> None:
        if self._lock_file is None:
            return
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
        finally:
            self._lock_file = None
            self._database_lock.stop()
