"""
Инициализация SQLAlchemy engine и фабрики сессий.
Используем connection pool, т.к. WS-колбэки будут писать часто и из разных потоков.
"""

import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import BybitConfig

logger = logging.getLogger(__name__)


def make_engine(cfg: BybitConfig):
    engine = create_engine(
        cfg.db_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,  # проверяет соединение перед использованием — не ловим stale connection
        future=True,
    )
    return engine


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Database:
    """Обёртка: engine + session factory + удобный контекст-менеджер."""

    def __init__(self, cfg: BybitConfig):
        self.cfg = cfg
        self.engine = make_engine(cfg)
        self.SessionLocal = make_session_factory(self.engine)
        logger.info("Database engine создан для %s", self._safe_url(cfg.db_url))

    @staticmethod
    def _safe_url(url: str) -> str:
        """Скрываем пароль в логах."""
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            creds_host = rest.split("@", 1)
            if len(creds_host) == 2:
                return f"{scheme}://***@{creds_host[1]}"
        return url

    def get_session(self) -> Session:
        return self.SessionLocal()

    def check_connection(self) -> bool:
        """Проверка, что БД доступна. Вызывайте при старте приложения."""
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            logger.exception("Не удалось подключиться к БД: %s", self._safe_url(self.cfg.db_url))
            return False
