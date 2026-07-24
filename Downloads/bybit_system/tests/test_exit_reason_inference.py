"""
Тесты определения причины закрытия сделки (exit_reason).

Регрессия: /v5/position/closed-pnl НЕ содержит stopOrderType — только
orderType ("Market"/"Limit") и execType, одинаковые для любого закрытия.
_infer_exit_reason раньше искал "takeprofit"/"trailing"/"stoploss" именно в
этих полях и поэтому НИКОГДА не находил совпадения: в реальном прогоне на
testnet 53 из 59 (90%) закрытых сделок получили "manual/unknown".

Реальная причина берётся из /v5/execution/list (там stopOrderType есть),
сматченного по orderId закрывающего ордера. Собственные программные закрытия
(Exit Manager, self_check) различаются по префиксу orderLinkId — надёжнее
прежней схемы через engine._pending_exit_reasons, которая создавала риск
приписать причину не той сделке при реконсиляции нескольких записей по
одному символу за один цикл.

Сеть не задействована.
"""

import sys
import time
import unittest
from datetime import datetime, timezone
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
from strategy.signal import Action


def _cfg() -> BybitConfig:
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.symbols = ["ETHUSDT"]
    cfg.trading_enabled = False
    return cfg


# ======================================================================
# 1. _infer_exit_reason — чистая функция
# ======================================================================

class InferExitReasonTest(unittest.TestCase):
    def test_no_execution_record_is_honestly_unknown(self):
        """Раньше здесь угадывали по closed_pnl-полям, которых физически нет."""
        closed_pnl = {"orderType": "Market", "execType": "Trade"}
        self.assertEqual(StrategyEngine._infer_exit_reason(closed_pnl, None), "manual/unknown")

    def test_take_profit(self):
        for stop_type in ("TakeProfit", "PartialTakeProfit"):
            got = StrategyEngine._infer_exit_reason({}, {"stopOrderType": stop_type})
            self.assertEqual(got, "TP", stop_type)

    def test_stop_loss(self):
        for stop_type in ("StopLoss", "PartialStopLoss", "Stop"):
            got = StrategyEngine._infer_exit_reason({}, {"stopOrderType": stop_type})
            self.assertEqual(got, "SL", stop_type)

    def test_trailing_stop(self):
        got = StrategyEngine._infer_exit_reason({}, {"stopOrderType": "TrailingStop"})
        self.assertEqual(got, "trailing")

    def test_mm_rate_close_is_manual(self):
        got = StrategyEngine._infer_exit_reason({}, {"stopOrderType": "MmRateClose"})
        self.assertEqual(got, "manual (MMR)")

    def test_unknown_stop_order_type_falls_back(self):
        for stop_type in ("", "UNKNOWN", "SomethingNewBybitAdds"):
            got = StrategyEngine._infer_exit_reason({}, {"stopOrderType": stop_type})
            self.assertEqual(got, "manual/unknown", stop_type)

    def test_our_exit_manager_close_detected_by_order_link_id(self):
        """
        source="exit_manager" в close_position() обрезается до 10 символов
        ("exit_manag") при формировании orderLinkId — см. execution_engine.py.
        """
        execution_record = {"orderLinkId": "exit_manag-close-abc123def456"}
        got = StrategyEngine._infer_exit_reason({}, execution_record)
        self.assertEqual(got, "exit_manager")

    def test_self_check_close_detected_by_order_link_id(self):
        execution_record = {"orderLinkId": "self_check-close-abc123"}
        got = StrategyEngine._infer_exit_reason({}, execution_record)
        self.assertEqual(got, "self_check_manual")

    def test_own_order_link_id_takes_priority_over_stop_order_type(self):
        """
        Наш orderLinkId — более специфичный и достоверный сигнал, чем
        stopOrderType биржи. На практике не пересекаются (наши reduceOnly
        ордера не являются биржевыми conditional-ордерами), но приоритет
        должен быть однозначным.
        """
        execution_record = {"orderLinkId": "exit_manag-close-x", "stopOrderType": "StopLoss"}
        got = StrategyEngine._infer_exit_reason({}, execution_record)
        self.assertEqual(got, "exit_manager")

    def test_third_party_order_link_id_without_stop_type_is_unknown(self):
        """orderLinkId есть, но не наш префикс и не биржевой стоп -> manual/unknown."""
        execution_record = {"orderLinkId": "some-other-tool-xyz"}
        got = StrategyEngine._infer_exit_reason({}, execution_record)
        self.assertEqual(got, "manual/unknown")

    def test_missing_fields_do_not_crash(self):
        self.assertEqual(StrategyEngine._infer_exit_reason({}, {}), "manual/unknown")
        self.assertEqual(StrategyEngine._infer_exit_reason({}, {"stopOrderType": None}), "manual/unknown")


# ======================================================================
# 2. _index_executions_by_order_id
# ======================================================================

class IndexExecutionsByOrderIdTest(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(StrategyEngine._index_executions_by_order_id([]), {})

    def test_indexes_by_order_id(self):
        execs = [
            {"orderId": "A", "stopOrderType": "TakeProfit", "execQty": "1"},
            {"orderId": "B", "stopOrderType": "StopLoss", "execQty": "2"},
        ]
        idx = StrategyEngine._index_executions_by_order_id(execs)
        self.assertEqual(idx["A"]["stopOrderType"], "TakeProfit")
        self.assertEqual(idx["B"]["stopOrderType"], "StopLoss")

    def test_multiple_fills_of_same_order_keep_first(self):
        """stopOrderType/orderLinkId одинаковы у всех fill'ов одного ордера."""
        execs = [
            {"orderId": "A", "execQty": "0.5", "execTime": "1"},
            {"orderId": "A", "execQty": "0.5", "execTime": "2"},
        ]
        idx = StrategyEngine._index_executions_by_order_id(execs)
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx["A"]["execTime"], "1")

    def test_executions_without_order_id_are_skipped(self):
        idx = StrategyEngine._index_executions_by_order_id([{"execQty": "1"}])
        self.assertEqual(idx, {})


# ======================================================================
# 3. Сквозной сценарий через реальный журнал: _sync_closed_trades пишет
#    правильный exit_reason, сматчив closed_pnl.orderId с executions
# ======================================================================

class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


class FakeExecution:
    def __init__(self):
        self.closed_pnl = {}
        self.executions = {}
        self.get_executions_calls = []
        self.fail_executions = False

    def get_closed_pnl_since(self, symbol, start_time_ms=None, max_pages=5):
        return self.closed_pnl.get(symbol, [])

    def get_executions(self, symbol, order_link_id=None, start_time_ms=None, max_pages=5):
        self.get_executions_calls.append(symbol)
        if self.fail_executions:
            raise RuntimeError("simulated API failure")
        return self.executions.get(symbol, [])


def _bare_engine(cfg, execution, journal, risk_manager) -> StrategyEngine:
    engine = object.__new__(StrategyEngine)
    engine.cfg = cfg
    engine.execution = execution
    engine.journal = journal
    engine.risk_manager = risk_manager
    engine._orphan_attempts = {}
    return engine


class EndToEndExitReasonTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.db = SessionBackedDb()
        self.journal = TradeJournal(self.db)
        self.risk = RiskManager(self.cfg)
        self.execution = FakeExecution()
        self.engine = _bare_engine(self.cfg, self.execution, self.journal, self.risk)
        self.engine._build_exit_snapshot = lambda symbol, match: {}

        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "oid-1",
        )
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-1").one()
            row.opened_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

    def _closed_pnl_record(self, order_id="close-order-1", pnl="2.5"):
        now_ms = int(time.time() * 1000)
        return {
            "symbol": "ETHUSDT", "orderId": order_id, "side": "Sell",
            "avgEntryPrice": "100.0", "createdTime": str(now_ms), "updatedTime": str(now_ms),
            "avgExitPrice": "103.0", "closedPnl": pnl,
        }

    def _row(self):
        session = self.db.get_session()
        try:
            return session.query(TradeLog).filter(TradeLog.order_link_id == "oid-1").one()
        finally:
            session.close()

    def test_take_profit_closure_recorded_with_correct_reason(self):
        self.execution.closed_pnl["ETHUSDT"] = [self._closed_pnl_record()]
        self.execution.executions["ETHUSDT"] = [
            {"orderId": "close-order-1", "stopOrderType": "TakeProfit", "orderLinkId": ""},
        ]
        self.engine._sync_closed_trades([])
        row = self._row()
        self.assertEqual(row.status, "closed")
        self.assertEqual(row.exit_reason, "TP")
        self.assertEqual(row.exit_type, "take_profit")

    def test_stop_loss_closure_recorded_with_correct_reason(self):
        self.execution.closed_pnl["ETHUSDT"] = [self._closed_pnl_record(pnl="-1.5")]
        self.execution.executions["ETHUSDT"] = [
            {"orderId": "close-order-1", "stopOrderType": "StopLoss", "orderLinkId": ""},
        ]
        self.engine._sync_closed_trades([])
        row = self._row()
        self.assertEqual(row.exit_reason, "SL")
        self.assertEqual(row.exit_type, "stop_loss")

    def test_our_exit_manager_close_recorded_with_correct_reason(self):
        self.execution.closed_pnl["ETHUSDT"] = [self._closed_pnl_record(pnl="0.3")]
        self.execution.executions["ETHUSDT"] = [
            {"orderId": "close-order-1", "stopOrderType": "", "orderLinkId": "exit_manag-close-deadbeef"},
        ]
        self.engine._sync_closed_trades([])
        row = self._row()
        self.assertEqual(row.exit_reason, "exit_manager")
        self.assertEqual(row.exit_type, "exit_manager")

    def test_wrong_order_id_does_not_match_execution(self):
        """orderId в closed_pnl не совпадает ни с одним execution — фолбэк."""
        self.execution.closed_pnl["ETHUSDT"] = [self._closed_pnl_record(order_id="close-order-1")]
        self.execution.executions["ETHUSDT"] = [
            {"orderId": "totally-different-order", "stopOrderType": "TakeProfit"},
        ]
        self.engine._sync_closed_trades([])
        row = self._row()
        self.assertEqual(row.exit_reason, "manual/unknown")
        # PnL при этом всё равно должен быть учтён -- неточный exit_reason не
        # должен блокировать сам факт закрытия сделки.
        self.assertAlmostEqual(float(row.pnl_usdt), 2.5)

    def test_executions_api_failure_does_not_block_pnl_reconciliation(self):
        """
        Асимметрия: ошибка closed_pnl блокирует весь символ (мы не знаем,
        закрыта ли сделка вообще). Ошибка executions — это только потеря
        точности exit_reason, сама сделка обязана закрыться как обычно.
        """
        self.execution.closed_pnl["ETHUSDT"] = [self._closed_pnl_record()]
        self.execution.fail_executions = True
        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            self.engine._sync_closed_trades([])
        self.assertTrue(any("executions" in m for m in logs.output))
        row = self._row()
        self.assertEqual(row.status, "closed")
        self.assertAlmostEqual(float(row.pnl_usdt), 2.5)
        self.assertEqual(row.exit_reason, "manual/unknown")

    def test_executions_are_fetched_for_symbol_with_unresolved_trade(self):
        """setUp уже создал открытую сделку по ETHUSDT -- есть что сверять."""
        self.engine._sync_closed_trades([])
        self.assertIn("ETHUSDT", self.execution.get_executions_calls)

    def test_executions_not_fetched_when_nothing_unresolved(self):
        """
        Экономим API-вызовы: get_executions запрашивается только для символов,
        по которым в журнале есть открытые/orphaned сделки -- не для всех
        символов конфигурации подряд.
        """
        cfg = _cfg()
        cfg.symbols = ["ETHUSDT", "SOLUSDT"]  # по SOLUSDT в журнале ничего нет
        engine = _bare_engine(cfg, self.execution, self.journal, self.risk)
        self.execution.closed_pnl["ETHUSDT"] = [self._closed_pnl_record()]
        self.execution.executions["ETHUSDT"] = [
            {"orderId": "close-order-1", "stopOrderType": "TakeProfit"},
        ]
        engine._build_exit_snapshot = lambda symbol, match: {}

        engine._sync_closed_trades([])

        self.assertIn("ETHUSDT", self.execution.get_executions_calls)
        self.assertNotIn("SOLUSDT", self.execution.get_executions_calls)


if __name__ == "__main__":
    unittest.main()
