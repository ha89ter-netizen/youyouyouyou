"""
Скрипт первичной настройки БД. Запускать один раз при разворачивании:

    python -m storage.init_db

Делает:
1. Создаёт обычные таблицы через SQLAlchemy metadata.
2. Включает расширение TimescaleDB (если ещё не включено).
3. Конвертирует каждую таблицу в hypertable — партиционирование по времени.

Требования: PostgreSQL с установленным расширением TimescaleDB.
Проще всего поднять через Docker (см. README.md, docker-compose.yml).
"""

import logging

from config.settings import BybitConfig
from storage.db import Database
from storage.models import Base, HYPERTABLE_CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("init_db")


def init_database(cfg: BybitConfig = None):
    cfg = cfg or BybitConfig()
    db = Database(cfg)

    if not db.check_connection():
        raise RuntimeError(
            "Нет подключения к БД. Проверьте DATABASE_URL и что Postgres запущен."
        )

    # 1. Обычные таблицы
    logger.info("Создаю таблицы...")
    Base.metadata.create_all(db.engine)

    # 2. Расширение TimescaleDB + 3. hypertables — через raw SQL,
    #    SQLAlchemy ORM этого не умеет.
    with db.engine.connect() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        conn.commit()

        for table, time_col in HYPERTABLE_CONFIG.items():
            logger.info("Превращаю '%s' в hypertable по колонке '%s'...", table, time_col)
            # chunk_time_interval в мс: 86400000 = 1 день на chunk.
            # if_not_exists=TRUE — безопасно перезапускать скрипт.
            conn.exec_driver_sql(f"""
                SELECT create_hypertable(
                    '{table}', '{time_col}',
                    chunk_time_interval => 86400000,
                    if_not_exists => TRUE,
                    migrate_data => TRUE
                );
            """)
            conn.commit()

    logger.info("Готово. База данных настроена.")


if __name__ == "__main__":
    init_database()
