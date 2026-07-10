import logging
import os
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
) -> Path:
    """
    Настраивает root logger для отдельного процесса приложения.

    Повторный вызов с тем же app_name не добавляет новые handlers. Если в том
    же процессе переключили app_name, старые managed handlers заменяются.
    """
    target_dir = Path(log_dir) if log_dir is not None else Path(__file__).resolve().parent / "logs"
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / log_filename

    root = logging.getLogger()
    root.setLevel(level)

    managed = [h for h in root.handlers if getattr(h, "_bybit_managed_handler", False)]
    if managed and all(
        getattr(h, "_bybit_app_name", None) == app_name
        and getattr(h, "_bybit_log_path", str(log_path)) == str(log_path)
        for h in managed
    ):
        return log_path

    for handler in managed:
        root.removeHandler(handler)
        handler.close()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    secret_filter = SensitiveDataFilter(_secrets_from_env())

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(secret_filter)
    stream_handler._bybit_managed_handler = True
    stream_handler._bybit_app_name = app_name
    stream_handler._bybit_log_path = str(log_path)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(secret_filter)
    file_handler._bybit_managed_handler = True
    file_handler._bybit_app_name = app_name
    file_handler._bybit_log_path = str(log_path)

    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    return log_path
