"""
Тесты снимка решения, вызвавшего закрытие через Exit Manager (exit_trigger).

Контекст: в реальном 24-часовом прогоне на testnet 86% сделок (51 из 59)
закрылись ни по SL, ни по TP -- "досрочно". Раньше не было способа узнать,
какой именно разворотный сигнал вызвал каждое такое закрытие: closed_pnl и
execution list с биржи ничего не знают про наш DecisionEngine, а
_pending_exit_reasons (символ-ключевой кэш) был снят как источник риска
неверной атрибуции. exit_trigger решает это иначе -- пишется СРАЗУ в строку
конкретной сделки по order_link_id, не по символу.

Важно: сам факт наличия комитетного барьера у разворотного сигнала (тот же
порог, что и для входа: confidence>=0.45, margin>=0.08, RR>=2.0, >=2 семейства)
уже проверен раньше в decision_engine.py -- эти тесты НЕ проверяют, должен ли
Exit Manager закрывать позицию, только то, что решение при закрытии
записывается корректно и без побочных эффектов при сбоях.
"""

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import BybitConfig
from risk.risk_manager import RiskManager
from storage.journal import TradeJournal
from storage.models import Base, TradeLog
from strategy.engine import StrategyEngine
from strategy.signal import Action, Signal


def _cfg(trading_enabled=True) -> BybitConfig:
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.symbols = ["ETHUSDT"]
    cfg.trading_enabled = trading_enabled
    return cfg


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


class _ClosingExecution:
    """close_position успешно закрывает, ничего больше не делает."""

    def close_position(self, symbol, side, size, source="unknown"):
        return {"retCode": 0, "retMsg": "OK"}


class _ExplodingJournalLookup:
    """journal, у которого get_open_trades всегда падает."""

    def get_open_trades(self, symbol=None):
        raise RuntimeError("db connection lost")


def _bare_engine(cfg, execution=None, journal=None) -> StrategyEngine:
    engine = object.__new__(StrategyEngine)
    engine.cfg = cfg
    engine.execution = execution or _ClosingExecution()
    engine.journal = journal
    engine.risk_manager = RiskManager(cfg)
    engine._orphan_attempts = {}
    return engine


POSITION = {
    "symbol": "ETHUSDT", "size": "1", "side": "Buy",
    "avgPrice": "100", "markPrice": "105", "trailingStop": "0",
}


# ======================================================================
# 1. Сквозной сценарий на настоящем журнале
# ======================================================================

class RecordExitTriggerEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionBackedDb()
        self.journal = TradeJournal(self.db)
        self.cfg = _cfg()
        self.engine = _bare_engine(self.cfg, journal=self.journal)

    def _row(self, order_link_id="oid-1"):
        session = self.db.get_session()
        try:
            return session.query(TradeLog).filter(TradeLog.order_link_id == order_link_id).one()
        finally:
            session.close()

    def test_trigger_written_to_the_correct_row(self):
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "oid-1",
        )
        signal = Signal(
            "ETHUSDT", Action.OPEN_SHORT, "decision:vwap+committee", 0.72,
            "разворот вниз", stop_loss_pct=1.2, take_profit_pct=2.6,
        )
        changed = self.engine._manage_exit("ETHUSDT", dict(POSITION), signal, "short")

        self.assertTrue(changed)
        row = self._row()
        self.assertEqual(row.exit_trigger["action"], "open_short")
        self.assertEqual(row.exit_trigger["source"], "decision:vwap+committee")
        self.assertAlmostEqual(row.exit_trigger["confidence"], 0.72)
        self.assertAlmostEqual(row.exit_trigger["expected_rr"], round(2.6 / 1.2, 3))
        self.assertEqual(row.exit_trigger["reason"], "разворот вниз")
        # Категория закрытия -- отдельная от снимка решения, тут ещё не заполнена
        self.assertIsNone(row.exit_reason)
        self.assertEqual(row.status, "open")  # само закрытие подтвердит сверка с биржей

    def test_missing_rr_when_stop_or_take_profit_absent(self):
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "oid-1",
        )
        signal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.6, "нет SL/TP у сигнала")
        self.engine._manage_exit("ETHUSDT", dict(POSITION), signal, "short")
        self.assertIsNone(self._row().exit_trigger["expected_rr"])

    def test_no_open_trade_does_not_crash_close(self):
        """Позиция есть на бирже, но в журнале почему-то нет открытой записи."""
        signal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.7, "разворот")
        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            changed = self.engine._manage_exit("ETHUSDT", dict(POSITION), signal, "short")
        self.assertTrue(changed)  # закрытие на бирже состоялось, это главное
        self.assertTrue(any("снимок решения записать некуда" in m for m in logs.output))

    def test_multiple_open_rows_for_symbol_picks_oldest_and_warns(self):
        """
        В норме Risk Manager не даёт двух открытых сделок по символу
        одновременно, но проверяем, что при аномалии код не падает и
        выбирает детерминированно (самую старую), а не первую попавшуюся.
        """
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "older",
            100.0, 50.0, 1, 1.5, 3.0, "oid-older",
        )
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "newer",
            101.0, 50.0, 1, 1.5, 3.0, "oid-newer",
        )
        # Обе имеют opened_at_ms из server_default -- зафиксируем порядок явно
        session = self.db.get_session()
        try:
            older = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-older").one()
            newer = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-newer").one()
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            older.opened_at = now - timedelta(hours=1)
            newer.opened_at = now
            session.commit()
        finally:
            session.close()

        signal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.7, "разворот")
        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            self.engine._manage_exit("ETHUSDT", dict(POSITION), signal, "short")

        self.assertTrue(any("2 открытых сделок" in m for m in logs.output))
        self.assertIsNotNone(self._row("oid-older").exit_trigger)
        self.assertIsNone(self._row("oid-newer").exit_trigger)


# ======================================================================
# 2. Отказоустойчивость: сбой записи снимка не должен отменять закрытие
# ======================================================================

class RecordExitTriggerFailureIsolationTest(unittest.TestCase):
    def test_journal_lookup_exception_does_not_block_close(self):
        engine = _bare_engine(_cfg(), journal=_ExplodingJournalLookup())
        signal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.7, "разворот")
        with self.assertLogs("strategy.engine", level="ERROR"):
            changed = engine._manage_exit("ETHUSDT", dict(POSITION), signal, "short")
        self.assertTrue(changed)

    def test_safe_mode_never_reaches_trigger_logic(self):
        """TRADING_ENABLED=false выходит раньше, чем до close_position/journal."""
        engine = _bare_engine(_cfg(trading_enabled=False), journal=_ExplodingJournalLookup())
        signal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.7, "разворот")
        # Если бы код дошёл до journal.get_open_trades, тест упал бы с RuntimeError.
        changed = engine._manage_exit("ETHUSDT", dict(POSITION), signal, "short")
        self.assertFalse(changed)


# ======================================================================
# 3. record_exit_trigger в TradeJournal напрямую
# ======================================================================

class JournalRecordExitTriggerTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionBackedDb()
        self.journal = TradeJournal(self.db)

    def test_unknown_order_link_id_returns_false_without_crash(self):
        self.assertFalse(self.journal.record_exit_trigger("no-such-id", {"action": "open_short"}))

    def test_can_be_called_before_the_trade_closes(self):
        """
        exit_trigger пишется В МОМЕНТ инициирования закрытия -- до того, как
        сверка с биржей подтвердит фактическое закрытие. Статус сделки при
        этом остаётся open.
        """
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "oid-1",
        )
        self.assertTrue(self.journal.record_exit_trigger("oid-1", {"action": "open_short", "confidence": 0.6}))

        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-1").one()
            self.assertEqual(row.status, "open")
            self.assertEqual(row.exit_trigger["confidence"], 0.6)
        finally:
            session.close()

    def test_overwrites_previous_trigger_idempotently(self):
        """Повторный вызов (например, ретрай цикла) просто заменяет снимок, не падает."""
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "oid-1",
        )
        self.journal.record_exit_trigger("oid-1", {"action": "open_short", "confidence": 0.5})
        self.journal.record_exit_trigger("oid-1", {"action": "open_short", "confidence": 0.9})

        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-1").one()
            self.assertAlmostEqual(row.exit_trigger["confidence"], 0.9)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
