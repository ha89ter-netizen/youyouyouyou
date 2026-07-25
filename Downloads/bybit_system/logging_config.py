import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Optional


SENSITIVE_ENV_NAMES = (
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "OPENAI_API_KEY",
    "DATABASE_URL",
)


class SensitiveDataFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "***")
        record.msg = message
        record.args = ()
        return True


class RuntimeContextFilter(logging.Filter):
    def __init__(self, app_name: str):
        super().__init__()
        self.app_name = app_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.app_name = self.app_name
        record.run_id = os.getenv("RUN_ID") or "-"
        symbol = getattr(record, "symbol", None)
        if not symbol:
            match = re.search(r"\b[A-Z0-9]{2,20}USDT\b", record.getMessage())
            symbol = match.group(0) if match else "-"
        record.symbol = symbol
        return True


class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def _secrets_from_env() -> list[str]:
    secrets = []
    for name in SENSITIVE_ENV_NAMES:
        value = os.getenv(name)
        if value:
            secrets.append(value)
    return secrets


def configure_app_logging(
    app_name: str,
    log_filename: str,
    level: int = logging.INFO,
    log_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Настраивает root logger для отдельного процесса приложения.

    Повторный вызов с тем же app_name не добавляет новые handlers. Если в том
    же процессе переключили app_name, старые managed handlers заменяются.
    """
    runtime_mode = os.getenv("RUNTIME_MODE", "local").strip().lower()
    railway_mode = runtime_mode == "railway"
    log_path = None
    if not railway_mode:
        target_dir = (
            Path(log_dir)
            if log_dir is not None
            else Path(__file__).resolve().parent / "logs"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        log_path = target_dir / log_filename

    root = logging.getLogger()
    root.setLevel(level)

    managed = [h for h in root.handlers if getattr(h, "_bybit_managed_handler", False)]
    if managed and all(
        getattr(h, "_bybit_app_name", None) == app_name
        and getattr(h, "_bybit_runtime_mode", None) == runtime_mode
        and getattr(h, "_bybit_log_path", None) == (
            str(log_path) if log_path is not None else None
        )
        for h in managed
    ):
        return log_path

    for handler in managed:
        root.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter(
        "%(asctime)s process=%(app_name)s level=%(levelname)s "
        "run_id=%(run_id)s symbol=%(symbol)s logger=%(name)s message=%(message)s"
    )
    secret_filter = SensitiveDataFilter(_secrets_from_env())
    context_filter = RuntimeContextFilter(app_name)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(context_filter)
    stream_handler.addFilter(secret_filter)
    stream_handler._bybit_managed_handler = True
    stream_handler._bybit_app_name = app_name
    stream_handler._bybit_runtime_mode = runtime_mode
    stream_handler._bybit_log_path = str(log_path) if log_path is not None else None

    root.addHandler(stream_handler)
    if railway_mode:
        stream_handler.addFilter(MaxLevelFilter(logging.WARNING))
        error_handler = logging.StreamHandler(sys.stderr)
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(fmt)
        error_handler.addFilter(context_filter)
        error_handler.addFilter(secret_filter)
        error_handler._bybit_managed_handler = True
        error_handler._bybit_app_name = app_name
        error_handler._bybit_runtime_mode = runtime_mode
        error_handler._bybit_log_path = None
        root.addHandler(error_handler)
    else:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(context_filter)
        file_handler.addFilter(secret_filter)
        file_handler._bybit_managed_handler = True
        file_handler._bybit_app_name = app_name
        file_handler._bybit_runtime_mode = runtime_mode
        file_handler._bybit_log_path = str(log_path)
        root.addHandler(file_handler)
    return log_path
