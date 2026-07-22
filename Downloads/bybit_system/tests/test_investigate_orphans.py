"""
Тесты глубокого разбора orphaned-сделок (risk_admin investigate-orphans).

Проверяются все шесть вердиктов, правило единственности closed-записи,
неприкосновенность AMBIGUOUS/NOT_FOUND и идемпотентность повторного --apply.
Сеть не задействована: execution подменён фейком.
"""

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import BybitConfig
from risk.risk_manager import RiskManager, orphan_cause
from risk_admin import (
    AMBIGUOUS,
    closed_record_identity,
    API_ERROR,
    CONFIRMED_CLOSED,
    NEVER_FILLED,
    NOT_FOUND,
    STILL_LIVE,
    apply_orphan_findings,
    entry_fill_from_evidence,
    gather_orphan_evidence,
    plan_orphan_investigation,
)
from storage.journal import TradeJournal
from storage.models import Base, TradeLog
from strategy.signal import Action
from timeutils import utc_today


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def _cfg() -> BybitConfig:
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.symbols = ["ETHUSDT"]
    cfg.trading_enabled = False
    return cfg


NOW_MS = int(time.time() * 1000)
OPENED_MS = NOW_MS - 3 * 60 * 60 * 1000  # 3 часа назад


def _trade(oid="orph-1", action="open_long", entry=100.0, size=50.0, symbol="ETHUSDT"):
    return {
        "order_link_id": oid, "symbol": symbol, "action": action,
        "entry_price": entry, "size_usdt": size, "opened_at_ms": OPENED_MS,
        "opened_at": datetime.now(timezone.utc) - timedelta(hours=3),
        "status": "orphaned",
    }


def _order(oid="orph-1", status="Filled", cum_qty="0.5", avg_price="100.0"):
    return {"orderLinkId": oid, "orderStatus": status, "cumExecQty": cum_qty, "avgPrice": avg_price}


def _execution(oid="orph-1", qty="0.5", price="100.0"):
    return {"orderLinkId": oid, "execQty": qty, "execPrice": price}


def _closed(entry="100.0", pnl="-2.0", side="Sell", created=None, qty="0.5", exit_price="96.0"):
    return {
        "symbol": "ETHUSDT", "avgEntryPrice": entry, "side": side,
        "createdTime": str(created or NOW_MS), "updatedTime": str(created or NOW_MS),
        "avgExitPrice": exit_price, "closedPnl": pnl, "qty": qty,
    }


def _evidence(order=None, executions=None, closed=None, error=None, symbol="ETHUSDT"):
    return {
        "symbol_key": symbol,
        "order": order,
        "executions": executions or [],
        "closed_records": closed or [],
        "error": error,
    }


# ======================================================================
# Классификация: все шесть вердиктов
# ======================================================================

class OrphanClassificationTest(unittest.TestCase):
    def _status(self, trade, evidence, live=None):
        plan = plan_orphan_investigation([trade], {trade["order_link_id"]: evidence}, live or [])
        return plan[0]["status"], plan[0]

    def test_confirmed_closed(self):
        status, item = self._status(
            _trade(),
            _evidence(order=_order(), executions=[_execution()], closed=[_closed()]),
        )
        self.assertEqual(status, CONFIRMED_CLOSED)
        self.assertEqual(item["record"]["closedPnl"], "-2.0")
        self.assertAlmostEqual(item["fill"]["price"], 100.0)

    def test_never_filled_when_order_rejected_without_fill(self):
        status, item = self._status(
            _trade(),
            _evidence(order=_order(status="Rejected", cum_qty="0", avg_price="0")),
        )
        self.assertEqual(status, NEVER_FILLED)
        self.assertIn("Rejected", item["note"])

    def test_never_filled_for_cancelled_and_deactivated(self):
        for dead in ("Cancelled", "Deactivated"):
            status, _ = self._status(
                _trade(), _evidence(order=_order(status=dead, cum_qty="0", avg_price="0")),
            )
            self.assertEqual(status, NEVER_FILLED, dead)

    def test_partially_filled_then_cancelled_is_not_never_filled(self):
        """Частичное исполнение оставляет экспозицию — это не 'сделки не было'."""
        status, _ = self._status(
            _trade(),
            _evidence(
                order=_order(status="PartiallyFilledCanceled", cum_qty="0.2", avg_price="100.0"),
                executions=[_execution(qty="0.2")],
                closed=[],
            ),
        )
        self.assertNotEqual(status, NEVER_FILLED)
        self.assertEqual(status, NOT_FOUND)

    def test_still_live_beats_history(self):
        """Живая позиция сильнее любой истории: сдались мы преждевременно."""
        live = [{"symbol": "ETHUSDT", "size": "0.5", "side": "Buy"}]
        status, item = self._status(
            _trade(), _evidence(order=_order(), executions=[_execution()], closed=[_closed()]), live,
        )
        self.assertEqual(status, STILL_LIVE)
        self.assertIn("size=0.5", item["note"])

    def test_live_position_of_opposite_direction_is_not_ours(self):
        live = [{"symbol": "ETHUSDT", "size": "0.5", "side": "Sell"}]  # шорт, а у нас лонг
        status, _ = self._status(
            _trade(action="open_long"), _evidence(order=_order(), executions=[_execution()]), live,
        )
        self.assertNotEqual(status, STILL_LIVE)

    def test_ambiguous_when_two_closed_records_fit(self):
        status, _ = self._status(
            _trade(),
            _evidence(
                order=_order(), executions=[_execution()],
                closed=[_closed(pnl="-2.0"), _closed(pnl="-3.0", created=NOW_MS - 1000)],
            ),
        )
        self.assertEqual(status, AMBIGUOUS)

    def test_not_found_when_filled_but_no_closure(self):
        status, item = self._status(
            _trade(), _evidence(order=_order(), executions=[_execution()], closed=[]),
        )
        self.assertEqual(status, NOT_FOUND)
        self.assertIn("закрытия среди 0 записей нет", item["note"])

    def test_not_found_when_order_missing_entirely(self):
        status, _ = self._status(_trade(), _evidence(order=None, executions=[]))
        self.assertEqual(status, NOT_FOUND)

    def test_api_error(self):
        status, item = self._status(_trade(), _evidence(error="closed PnL недоступен: Timeout"))
        self.assertEqual(status, API_ERROR)
        self.assertIn("Timeout", item["note"])

    def test_one_closed_record_never_serves_two_orphans(self):
        """Правило единственности: оспариваемая запись делает AMBIGUOUS обе сделки."""
        t1, t2 = _trade("orph-1", entry=100.0), _trade("orph-2", entry=100.02)
        record = _closed()
        evidence = {
            "orph-1": _evidence(order=_order("orph-1"), executions=[_execution("orph-1")], closed=[record]),
            "orph-2": _evidence(order=_order("orph-2"), executions=[_execution("orph-2")], closed=[record]),
        }
        plan = plan_orphan_investigation([t1, t2], evidence, [])
        self.assertEqual({p["status"] for p in plan}, {AMBIGUOUS})
        self.assertTrue(all(p["record"] is None for p in plan))

    def test_contested_record_detected_across_differently_sized_evidence_lists(self):
        """
        Регрессия: улики собираются на КАЖДУЮ сделку отдельным запросом со
        своим startTime, поэтому у сделок разные списки closed PnL и одна и та
        же запись лежит на разных индексах. Владение обязано считаться по
        идентичности записи, иначе конкуренты не обнаружатся.

        Сценарий из реальных данных: два ETHUSDT-шорта в 0.42% друг от друга,
        то есть внутри допуска 0.5% — оба претендуют на одно закрытие.
        """
        shared = _closed(entry="100.0", pnl="-2.0")
        shared["orderId"] = "close-order-777"
        noise = _closed(entry="500.0", pnl="9.0", created=NOW_MS - 5000)
        noise["orderId"] = "unrelated-1"

        t1 = _trade("orph-1", entry=100.0)
        t2 = _trade("orph-2", entry=100.3)  # 0.3% — в пределах допуска
        evidence = {
            # у orph-1 общая запись на индексе 0
            "orph-1": _evidence(order=_order("orph-1"), executions=[_execution("orph-1")],
                                closed=[shared, noise]),
            # у orph-2 та же запись уже на индексе 1
            "orph-2": _evidence(order=_order("orph-2", avg_price="100.3"),
                                executions=[_execution("orph-2", price="100.3")],
                                closed=[noise, shared]),
        }
        plan = plan_orphan_investigation([t1, t2], evidence, [])
        self.assertEqual({p["status"] for p in plan}, {AMBIGUOUS})
        self.assertTrue(all(p["record"] is None for p in plan))

    def test_distinct_records_still_confirm_independently(self):
        """Разные закрытия — обе сделки подтверждаются, конкуренции нет."""
        r1 = _closed(entry="100.0", pnl="-2.0"); r1["orderId"] = "close-1"
        r2 = _closed(entry="200.0", pnl="5.0", qty="0.25"); r2["orderId"] = "close-2"
        t1 = _trade("orph-1", entry=100.0)
        t2 = _trade("orph-2", entry=200.0, size=50.0)
        evidence = {
            "orph-1": _evidence(order=_order("orph-1"), executions=[_execution("orph-1")],
                                closed=[r1, r2]),
            "orph-2": _evidence(order=_order("orph-2", avg_price="200.0"),
                                executions=[_execution("orph-2", qty="0.25", price="200.0")],
                                closed=[r1, r2]),
        }
        plan = plan_orphan_investigation([t1, t2], evidence, [])
        self.assertEqual({p["status"] for p in plan}, {CONFIRMED_CLOSED})
        used = [closed_record_identity(p["record"]) for p in plan]
        self.assertEqual(len(set(used)), 2)  # каждая запись использована ровно раз

    def test_record_identity_prefers_order_id(self):
        a = _closed(); a["orderId"] = "X"
        b = _closed(pnl="-99.0", exit_price="1"); b["orderId"] = "X"  # тот же ордер
        self.assertEqual(closed_record_identity(a), closed_record_identity(b))
        c = _closed(); c["orderId"] = "Y"
        self.assertNotEqual(closed_record_identity(a), closed_record_identity(c))

    def test_record_identity_falls_back_without_order_id(self):
        a, b = _closed(), _closed()
        self.assertEqual(closed_record_identity(a), closed_record_identity(b))
        self.assertNotEqual(closed_record_identity(a), closed_record_identity(_closed(pnl="-3.0")))

    def test_fill_price_from_executions_used_for_matching(self):
        """
        Журнал хранит оценку (закрытие свечи), а реальный fill был по 100.0.
        Матчинг обязан идти по факту с биржи, иначе закрытие не найдётся.
        """
        trade = _trade(entry=103.0)  # оценка в журнале мимо на 3%
        evidence = _evidence(
            order=_order(avg_price="100.0"),
            executions=[_execution(qty="0.5", price="100.0")],
            closed=[_closed(entry="100.0")],
        )
        status, item = self._status(trade, evidence)
        self.assertEqual(status, CONFIRMED_CLOSED)
        self.assertEqual(item["fill"]["source"], "executions")

    def test_plan_is_pure(self):
        trade, ev = _trade(), _evidence(order=_order(), executions=[_execution()], closed=[_closed()])
        before_trade, before_ev = dict(trade), dict(ev)
        plan_orphan_investigation([trade], {trade["order_link_id"]: ev}, [])
        self.assertEqual(trade, before_trade)
        self.assertEqual(ev["closed_records"], before_ev["closed_records"])


class EntryFillTest(unittest.TestCase):
    def test_weighted_average_across_fills(self):
        fill = entry_fill_from_evidence(_evidence(executions=[
            _execution(qty="1", price="100"), _execution(qty="3", price="104"),
        ]))
        self.assertAlmostEqual(fill["price"], 103.0)   # (100 + 312) / 4
        self.assertAlmostEqual(fill["qty"], 4.0)

    def test_falls_back_to_order_cum_exec_qty(self):
        fill = entry_fill_from_evidence(_evidence(order=_order(cum_qty="2", avg_price="50")))
        self.assertEqual(fill["source"], "order.cumExecQty")
        self.assertAlmostEqual(fill["qty"], 2.0)

    def test_none_when_no_fill(self):
        self.assertIsNone(entry_fill_from_evidence(_evidence(order=_order(cum_qty="0", avg_price="0"))))
        self.assertIsNone(entry_fill_from_evidence(_evidence()))


# ======================================================================
# Применение вердиктов на настоящем журнале
# ======================================================================

class ApplyOrphanFindingsTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionBackedDb()
        self.journal = TradeJournal(self.db)
        self.risk = RiskManager(_cfg())
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "orph-1",
        )
        # Сделка должна быть ОТКРЫТА раньше закрытий из фикстур (_closed()
        # датирован NOW_MS, снятым при импорте модуля). Иначе матчер честно
        # отвергнет закрытие как случившееся раньше открытия.
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "orph-1").one()
            row.opened_at = datetime.now(timezone.utc) - timedelta(hours=3)
            session.commit()
        finally:
            session.close()
        self.journal.mark_orphaned("orph-1", "закрытие не найдено")
        self.risk.trip_circuit_breaker("orphan", sticky=True, cause=orphan_cause("orph-1"))

    def _row(self):
        session = self.db.get_session()
        try:
            return session.query(TradeLog).filter(TradeLog.order_link_id == "orph-1").one()
        finally:
            session.close()

    def _plan(self, evidence, live=None):
        trades = self.journal.get_orphaned_trades()
        return plan_orphan_investigation(trades, {"orph-1": evidence}, live or [])

    def test_confirmed_closed_records_pnl_and_clears_breaker(self):
        plan = self._plan(_evidence(order=_order(), executions=[_execution()], closed=[_closed()]))
        applied = apply_orphan_findings(plan, self.journal, self.risk)

        self.assertTrue(applied[0]["changed"])
        row = self._row()
        self.assertEqual(row.status, "closed")
        self.assertAlmostEqual(float(row.pnl_usdt), -2.0)
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, -2.0)
        self.assertFalse(self.risk.circuit_breaker_tripped)
        self.assertEqual(self.journal.count_orphaned(), 0)

    def test_never_filled_closes_without_pnl(self):
        plan = self._plan(_evidence(order=_order(status="Rejected", cum_qty="0", avg_price="0")))
        applied = apply_orphan_findings(plan, self.journal, self.risk)

        self.assertTrue(applied[0]["changed"])
        row = self._row()
        self.assertEqual(row.status, "not_filled")
        self.assertEqual(row.exit_type, "not_filled")
        # PnL остаётся NULL: нуль исказил бы win rate как безубыточная сделка
        self.assertIsNone(row.pnl_usdt)
        self.assertEqual(self.risk._daily_pnl_usdt, 0.0)
        self.assertFalse(self.risk.circuit_breaker_tripped)
        # Не участвует ни в дневной сумме, ни в orphaned
        total, count = self.journal.sum_closed_pnl_for_utc_day(utc_today())
        self.assertEqual((total, count), (0.0, 0))
        self.assertEqual(self.journal.count_orphaned(), 0)

    def test_still_live_returns_status_to_open(self):
        live = [{"symbol": "ETHUSDT", "size": "0.5", "side": "Buy"}]
        plan = self._plan(_evidence(order=_order(), executions=[_execution()]), live)
        applied = apply_orphan_findings(plan, self.journal, self.risk)

        self.assertTrue(applied[0]["changed"])
        row = self._row()
        self.assertEqual(row.status, "open")
        self.assertIsNone(row.closed_at)
        self.assertIsNone(row.pnl_usdt)
        self.assertFalse(self.risk.circuit_breaker_tripped)
        # Снова занимает символ и подлежит обычной сверке
        self.assertEqual(len(self.journal.get_open_trades("ETHUSDT")), 1)
        self.assertEqual(self.journal.count_orphaned(), 0)

    def test_ambiguous_and_not_found_are_never_applied(self):
        for evidence in (
            _evidence(order=_order(), executions=[_execution()],
                      closed=[_closed(pnl="-2.0"), _closed(pnl="-3.0", created=NOW_MS - 1000)]),
            _evidence(order=_order(), executions=[_execution()], closed=[]),
            _evidence(error="Timeout"),
        ):
            plan = self._plan(evidence)
            applied = apply_orphan_findings(plan, self.journal, self.risk)
            self.assertEqual(applied, [])
            self.assertEqual(self._row().status, "orphaned")     # не тронута
            self.assertTrue(self.risk.circuit_breaker_tripped)   # breaker на месте
            self.assertEqual(self.risk._daily_pnl_usdt, 0.0)

    def test_repeated_apply_does_not_double_pnl(self):
        plan = self._plan(_evidence(order=_order(), executions=[_execution()], closed=[_closed()]))
        apply_orphan_findings(plan, self.journal, self.risk)
        pnl_first = self.risk._daily_pnl_usdt

        for _ in range(3):
            applied = apply_orphan_findings(plan, self.journal, self.risk)
            self.assertFalse(applied[0]["changed"])
            self.assertIn("идемпотентность", applied[0]["note"])
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, pnl_first)
        self.assertEqual(self._row().status, "closed")

    def test_repeated_apply_never_filled_is_idempotent(self):
        plan = self._plan(_evidence(order=_order(status="Rejected", cum_qty="0", avg_price="0")))
        apply_orphan_findings(plan, self.journal, self.risk)
        for _ in range(2):
            applied = apply_orphan_findings(plan, self.journal, self.risk)
            self.assertFalse(applied[0]["changed"])
        self.assertEqual(self._row().status, "not_filled")

    def test_repeated_apply_still_live_is_idempotent(self):
        live = [{"symbol": "ETHUSDT", "size": "0.5", "side": "Buy"}]
        plan = self._plan(_evidence(order=_order(), executions=[_execution()]), live)
        apply_orphan_findings(plan, self.journal, self.risk)
        for _ in range(2):
            applied = apply_orphan_findings(plan, self.journal, self.risk)
            self.assertFalse(applied[0]["changed"])
        self.assertEqual(self._row().status, "open")

    def test_breaker_with_other_causes_stays_tripped(self):
        """Снимается только причина этой сделки, дневной лимит остаётся."""
        self.risk.trip_circuit_breaker("дневной лимит убытка", cause="daily_loss")
        plan = self._plan(_evidence(order=_order(), executions=[_execution()], closed=[_closed()]))
        apply_orphan_findings(plan, self.journal, self.risk)

        self.assertTrue(self.risk.circuit_breaker_tripped)
        self.assertIn("daily_loss", self.risk.breaker_causes())
        self.assertNotIn(orphan_cause("orph-1"), self.risk.breaker_causes())


# ======================================================================
# Сбор улик: только чтение, ошибки не глушатся молча
# ======================================================================

class GatherEvidenceTest(unittest.TestCase):
    class FakeExecution:
        def __init__(self, orders=None, execs=None, closed=None, fail=None):
            self.orders, self.execs, self.closed, self.fail = orders or [], execs or [], closed or [], fail
            self.calls = []

        def get_order_history(self, symbol, order_link_id=None, start_time_ms=None, max_pages=5):
            self.calls.append(("order_history", symbol, order_link_id))
            if self.fail == "order":
                raise RuntimeError("api down")
            return self.orders

        def get_executions(self, symbol, order_link_id=None, start_time_ms=None, max_pages=5):
            self.calls.append(("executions", symbol, order_link_id))
            if self.fail == "exec":
                raise RuntimeError("api down")
            return self.execs

        def get_closed_pnl_since(self, symbol, start_time_ms=None, max_pages=5):
            self.calls.append(("closed_pnl", symbol, None))
            if self.fail == "closed":
                raise RuntimeError("api down")
            return self.closed

    def test_gathers_all_three_sources_by_order_link_id(self):
        ex = self.FakeExecution(orders=[_order()], execs=[_execution()], closed=[_closed()])
        ev = gather_orphan_evidence(ex, _trade())
        self.assertEqual(ev["order"]["orderLinkId"], "orph-1")
        self.assertEqual(len(ev["executions"]), 1)
        self.assertEqual(len(ev["closed_records"]), 1)
        self.assertIsNone(ev["error"])
        # История ордера и исполнения запрошены именно по нашему order_link_id
        self.assertIn(("order_history", "ETHUSDT", "orph-1"), ex.calls)
        self.assertIn(("executions", "ETHUSDT", "orph-1"), ex.calls)

    def test_order_history_error_is_reported_not_swallowed(self):
        ev = gather_orphan_evidence(self.FakeExecution(fail="order"), _trade())
        self.assertIn("order history", ev["error"])

    def test_closed_pnl_error_is_reported(self):
        ev = gather_orphan_evidence(
            self.FakeExecution(orders=[_order()], execs=[_execution()], fail="closed"), _trade(),
        )
        self.assertIn("closed PnL", ev["error"])

    def test_executions_error_is_not_fatal(self):
        """Судьбу входа можно вывести и из самого ордера."""
        ev = gather_orphan_evidence(
            self.FakeExecution(orders=[_order()], closed=[_closed()], fail="exec"), _trade(),
        )
        self.assertIsNone(ev["error"])
        self.assertEqual(ev["executions"], [])
        self.assertIsNotNone(ev["order"])

    def test_foreign_order_link_id_is_filtered_out(self):
        ex = self.FakeExecution(orders=[_order()], execs=[_execution(oid="somebody-else")])
        ev = gather_orphan_evidence(ex, _trade())
        self.assertEqual(ev["executions"], [])


if __name__ == "__main__":
    unittest.main()
