"""Small Unix process lock and database heartbeat for long-running services."""

import atexit
import fcntl
import json
import os
import threading
from pathlib import Path

from storage.models import RunMetadata
from timeutils import utcnow


RUNTIME_DIR = Path(__file__).resolve().parent / ".runtime"


class RuntimeService:
    def __init__(self, db, run_id: str, service: str, interval_sec: int = 10):
        if service not in ("collector", "trader"):
            raise ValueError(f"Unknown runtime service: {service}")
        if not run_id:
            raise RuntimeError("RUN_ID is required for supervised live services")
        self.db = db
        self.run_id = run_id
        self.service = service
        self.interval_sec = interval_sec
        self._lock_file = None
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

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self._heartbeat()

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
