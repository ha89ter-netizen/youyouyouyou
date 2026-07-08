"""
Repository-слой: превращает сырые сообщения Bybit в записи БД.

Важно про производительность:
- Сделки и стакан летят очень часто (десятки-сотни сообщений в секунду
  на ликвидном инструменте). Писать в БД на каждое сообщение — плохая идея.
- Поэтому здесь используется буферизация: сообщения копятся в памяти
  и сбрасываются в БД пачкой (bulk insert) раз в N секунд или по достижении
  размера буфера. Это в разы снижает нагрузку на БД.
- Дубликаты (реконнект WS, повторные сообщения) не страшны: у всех таблиц
  primary key на бизнес-ключ, используется upsert (ON CONFLICT DO NOTHING).
"""

import logging
import threading
import time
from typing import List, Dict, Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from storage.db import Database
from storage.models import (
    Candle, Trade, FundingRate, OpenInterest, Liquidation, OrderbookSnapshot
)
from data.orderbook_state import OrderBookRegistry

logger = logging.getLogger(__name__)


def _upsert(session, model, rows: List[Dict[str, Any]]):
    """INSERT ... ON CONFLICT DO NOTHING — безопасно при дублях/реконнектах."""
    if not rows:
        return
    stmt = pg_insert(model).values(rows)
    pk_cols = [c.name for c in model.__table__.primary_key.columns]
    stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
    session.execute(stmt)
    session.commit()


class BufferedWriter:
    """
    Копит записи одного типа в памяти и сбрасывает в БД по таймеру
    или при переполнении буфера. Потокобезопасно (WS-колбэки из разных потоков).
    """

    def __init__(self, db: Database, model, flush_interval: float = 2.0, max_buffer: int = 500):
        self.db = db
        self.model = model
        self.flush_interval = flush_interval
        self.max_buffer = max_buffer
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def add(self, row: Dict[str, Any]):
        with self._lock:
            self._buffer.append(row)
            should_flush = len(self._buffer) >= self.max_buffer
        if should_flush:
            self.flush()

    def flush(self):
        with self._lock:
            if not self._buffer:
                return
            rows, self._buffer = self._buffer, []
        try:
            session = self.db.get_session()
            try:
                _upsert(session, self.model, rows)
                logger.debug("Записано %d строк в %s", len(rows), self.model.__tablename__)
            finally:
                session.close()
        except Exception:
            logger.exception("Ошибка записи в %s, потеряно %d строк", self.model.__tablename__, len(rows))

    def _run(self):
        while not self._stop.is_set():
            time.sleep(self.flush_interval)
            self.flush()

    def stop(self):
        self._stop.set()
        self.flush()


class MarketDataStore:
    """
    Единая точка входа: колбэки WS/REST вызывают методы этого класса,
    а он сам решает, как и когда писать в БД.
    """

    def __init__(self, db: Database):
        self.db = db
        self.candles = BufferedWriter(db, Candle, flush_interval=2.0, max_buffer=100)
        self.trades = BufferedWriter(db, Trade, flush_interval=1.0, max_buffer=300)
        self.funding = BufferedWriter(db, FundingRate, flush_interval=5.0, max_buffer=50)
        self.open_interest = BufferedWriter(db, OpenInterest, flush_interval=5.0, max_buffer=50)
        self.liquidations = BufferedWriter(db, Liquidation, flush_interval=1.0, max_buffer=50)
        self.orderbook = BufferedWriter(db, OrderbookSnapshot, flush_interval=1.0, max_buffer=200)
        self._orderbook_registry = OrderBookRegistry()

    # --- методы для колбэков WS (сырые сообщения Bybit) ---

    def on_kline_ws(self, msg: Dict[str, Any]):
        for k in msg.get("data", []):
            if not k.get("confirm"):
                continue  # пишем только закрытые свечи, не промежуточные тики
            self.candles.add({
                "symbol": msg.get("topic", "").split(".")[-1],
                "interval": str(k.get("interval")),
                "start_time": int(k.get("start")),
                "open": k.get("open"), "high": k.get("high"),
                "low": k.get("low"), "close": k.get("close"),
                "volume": k.get("volume"), "turnover": k.get("turnover"),
            })

    def on_trade_ws(self, msg: Dict[str, Any]):
        for t in msg.get("data", []):
            self.trades.add({
                "symbol": t.get("s"), "trade_id": t.get("i"), "ts": int(t.get("T")),
                "side": t.get("S"), "price": t.get("p"), "size": t.get("v"),
            })

    def on_liquidation_ws(self, msg: Dict[str, Any]):
        """
        Канал allLiquidation отдаёт данные списком, как в сделках:
        {"T": ts, "s": symbol, "S": side, "v": size, "p": price}
        """
        for d in msg.get("data", []):
            self.liquidations.add({
                "symbol": d.get("s"), "ts": int(d.get("T")),
                "side": d.get("S"), "price": d.get("p"), "size": d.get("v"),
            })

    def on_ticker_ws(self, msg: Dict[str, Any]):
        """Тикер содержит funding_rate и open_interest — раскладываем по двум таблицам."""
        d = msg.get("data", {})
        ts = int(msg.get("ts", time.time() * 1000))
        symbol = d.get("symbol")
        if d.get("fundingRate") is not None:
            self.funding.add({"symbol": symbol, "funding_ts": ts, "funding_rate": d["fundingRate"]})
        if d.get("openInterest") is not None:
            self.open_interest.add({"symbol": symbol, "ts": ts, "open_interest": d["openInterest"]})

    def on_orderbook_ws(self, msg: Dict[str, Any]):
        """
        ВАЖНО: не берём data["b"][0]/["a"][0] напрямую -- на delta-сообщениях
        это могут быть произвольные изменившиеся уровни, а не реальный топ
        стакана. Вместо этого прогоняем сообщение через OrderBookRegistry,
        который держит полное состояние (snapshot + применённые delta) и
        отдаёт настоящий текущий лучший бид/аск.
        """
        top = self._orderbook_registry.apply_message(msg)
        if top is None:
            return  # состояние ещё не готово (ждём snapshot) -- не пишем мусор в БД
        symbol = msg.get("data", {}).get("s")
        self.orderbook.add({
            "symbol": symbol, "ts": int(msg.get("ts", time.time() * 1000)),
            "best_bid_price": top["best_bid_price"], "best_bid_size": top["best_bid_size"],
            "best_ask_price": top["best_ask_price"], "best_ask_size": top["best_ask_size"],
        })

    # --- методы для REST (пачка исторических данных сразу) ---

    def save_historical_klines(self, symbol: str, interval: str, klines: List[Dict[str, Any]]):
        session = self.db.get_session()
        try:
            rows = [{
                "symbol": symbol, "interval": interval, "start_time": int(k["start"]),
                "open": k["open"], "high": k["high"], "low": k["low"],
                "close": k["close"], "volume": k["volume"], "turnover": k["turnover"],
            } for k in klines]
            _upsert(session, Candle, rows)
            logger.info("Сохранено %d исторических свечей для %s", len(rows), symbol)
        finally:
            session.close()

    def stop(self):
        """Сбросить все буферы перед остановкой приложения."""
        for writer in [self.candles, self.trades, self.funding,
                       self.open_interest, self.liquidations, self.orderbook]:
            writer.stop()
