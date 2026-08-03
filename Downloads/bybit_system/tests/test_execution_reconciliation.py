import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution.reconciliation import (
    AMBIGUOUS, MATCHED, NOT_FOUND, plan_closed_pnl_reconciliation,
)
from storage.journal import TradeJournal
from storage.models import Base, TradeClosure, TradeExchangeOrder, TradeLog
from strategy.engine import StrategyEngine
from timeutils import utcnow


def trade(oid, entry="100", qty="1", opened=1_000):
    return {
        "order_link_id": oid,
        "symbol": "SOLUSDT",
        "action": "open_long",
        "entry_price": float(entry),
        "entry_filled_qty": float(qty),
        "size_usdt": float(entry) * float(qty),
        "opened_at_ms": opened,
    }


def closed(oid, entry="100", exit_price="90", qty="1", updated="2000", pnl="-10"):
    return {
        "symbol": "SOLUSDT", "orderId": oid, "side": "Sell",
        "avgEntryPrice": entry, "avgExitPrice": exit_price,
        "closedSize": qty, "qty": qty, "closedPnl": pnl,
        "openFee": "0.05", "closeFee": "0.04", "fillCount": "1",
        "createdTime": updated, "updatedTime": updated,
    }


class DeterministicMatchingTest(unittest.TestCase):
    def test_similar_same_side_trades_and_identical_exit_prices_use_parent_ids(self):
        trades = [trade("entry-a", "100"), trade("entry-b", "100.01")]
        records = [
            closed("exit-a", "100", "90"),
            closed("exit-b", "100.01", "90", updated="3000"),
        ]
        orders = [
            {"orderId": "exit-a", "parentOrderLinkId": "entry-a"},
            {"orderId": "exit-b", "parentOrderLinkId": "entry-b"},
        ]
        plan = plan_closed_pnl_reconciliation(trades, records, orders)
        self.assertEqual([item["status"] for item in plan], [MATCHED, MATCHED])
        self.assertEqual(plan[0]["record"]["orderIds"], ["exit-a"])
        self.assertEqual(plan[1]["record"]["orderIds"], ["exit-b"])

    def test_delayed_second_partial_close_waits_for_full_quantity(self):
        first = closed("exit-1", qty="0.4", pnl="-4")
        self.assertEqual(
            plan_closed_pnl_reconciliation([trade("entry")], [first])[0]["status"],
            AMBIGUOUS,
        )
        second = closed("exit-2", qty="0.6", pnl="-6", updated="2001")
        item = plan_closed_pnl_reconciliation([trade("entry")], [first, second])[0]
        self.assertEqual(item["status"], MATCHED)
        self.assertEqual(item["record"]["orderIds"], ["exit-1", "exit-2"])
        self.assertEqual(item["record"]["closedPnl"], "-10")

    def test_duplicate_api_payload_is_collapsed(self):
        record = closed("exit-1")
        item = plan_closed_pnl_reconciliation([trade("entry")], [record, dict(record)])[0]
        self.assertEqual(item["status"], MATCHED)
        self.assertEqual(item["record"]["orderIds"], ["exit-1"])

    def test_conflicting_duplicate_payload_remains_unresolved(self):
        a = closed("exit-1")
        b = dict(a, closedPnl="-99")
        self.assertEqual(
            plan_closed_pnl_reconciliation([trade("entry")], [a, b])[0]["status"],
            AMBIGUOUS,
        )

    def test_stale_protection_replacement_uses_filled_replacement_only(self):
        orders = [
            {"orderId": "old-sl", "parentOrderLinkId": "entry", "orderStatus": "Cancelled",
             "stopOrderType": "StopLoss", "triggerPrice": "99"},
            {"orderId": "new-sl", "parentOrderLinkId": "", "orderStatus": "Filled",
             "stopOrderType": "StopLoss", "triggerPrice": "99.5"},
        ]
        item = plan_closed_pnl_reconciliation(
            [trade("entry")], [closed("new-sl")], orders
        )[0]
        self.assertEqual(item["status"], MATCHED)
        self.assertEqual(item["record"]["orderIds"], ["new-sl"])

    def test_unmatched_record_is_never_attached_by_nearest_price(self):
        record = closed("foreign", entry="105")
        self.assertEqual(
            plan_closed_pnl_reconciliation([trade("entry")], [record])[0]["status"],
            NOT_FOUND,
        )


class _Db:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


class DurableClosureTest(unittest.TestCase):
    def setUp(self):
        self.db = _Db()
        self.journal = TradeJournal(self.db)
        self.journal.log_entry(
            "SOLUSDT", "open_long", "test", "test", 100, 100, 1, 1, 2,
            "entry", entry_requested_qty=1, entry_filled_qty=1,
        )

    def test_restart_preserves_submitted_exit_identity(self):
        self.assertTrue(self.journal.record_submitted_exit_order("entry", "exit", "close-link"))
        restarted = TradeJournal(self.db)
        row = restarted.get_unresolved_trades("SOLUSDT")[0]
        self.assertEqual(row["submitted_exit_order_id"], "exit")
        self.assertEqual(row["submitted_exit_order_link_id"], "close-link")

    def test_closed_trade_with_transiently_missing_order_history_is_retried(self):
        self.journal.log_exit(
            "entry", 90, -10, closed_at=utcnow(),
            closure_records=[closed("exit-1")], closure_executions={"exit-1": []},
        )
        missing = self.journal.get_recent_trades_missing_exchange_order_evidence(
            "SOLUSDT", current_run_id=None,
        )
        self.assertEqual([item["order_link_id"] for item in missing], ["entry"])
        self.assertEqual(missing[0]["known_exchange_exit_order_ids"], ["exit-1"])

        self.journal.upsert_exchange_order_evidence("entry", [
            {"role": "entry", "order": {"orderId": "open-1", "orderLinkId": "entry"}},
            {"role": "protective_exit", "order": {
                "orderId": "exit-1", "parentOrderLinkId": "entry",
                "stopOrderType": "StopLoss", "orderStatus": "Filled",
            }},
        ])
        self.assertEqual(
            self.journal.get_recent_trades_missing_exchange_order_evidence(
                "SOLUSDT", current_run_id=None,
            ),
            [],
        )
        session = self.db.get_session()
        try:
            self.assertEqual(session.query(TradeExchangeOrder).count(), 2)
        finally:
            session.close()


class ExchangeEvidenceBackfillTest(unittest.TestCase):
    def setUp(self):
        self.db = _Db()
        self.journal = TradeJournal(self.db)
        self.journal.log_entry(
            "SOLUSDT", "open_long", "test", "test", 100, 100, 1, 1, 2,
            "entry", entry_requested_qty=1, entry_filled_qty=1,
        )

    def test_active_protection_is_combined_with_history_and_deduplicated(self):
        class Journal:
            written = []

            def get_recent_trades_missing_exchange_order_evidence(self, symbol, current_run_id=None):
                return [{
                    "order_link_id": "entry-link", "opened_at_ms": 1,
                    "status": "open", "known_exchange_exit_order_ids": [],
                }]

            def upsert_exchange_order_evidence(self, order_link_id, evidence):
                self.written.append((order_link_id, evidence))

        class Execution:
            def get_all_order_history_since(self, symbol, start_time_ms=None):
                return [{"orderId": "entry-id", "orderLinkId": "entry-link"}]

            def get_active_protective_orders(self, symbol):
                return [
                    {"orderId": "sl-id", "parentOrderLinkId": "entry-link",
                     "stopOrderType": "StopLoss"},
                    {"orderId": "tp-id", "parentOrderLinkId": "entry-link",
                     "stopOrderType": "TakeProfit"},
                ]

        engine = object.__new__(StrategyEngine)
        engine.cfg = type("Cfg", (), {"run_id": "run-new"})()
        engine.execution = Execution()
        engine.journal = Journal()
        engine._backfill_exchange_order_evidence("SOLUSDT")
        evidence = engine.journal.written[0][1]
        self.assertEqual([item["role"] for item in evidence], ["entry", "protective", "protective"])

    def test_one_position_multiple_orders_and_executions_aggregate_once(self):
        close_ms = str(int(utcnow().timestamp() * 1000) + 1_000)
        records = [
            closed("exit-1", exit_price="91", qty="0.4", pnl="-3.7", updated=close_ms),
            closed("exit-2", exit_price="89", qty="0.6", pnl="-6.7", updated=close_ms),
        ]
        executions = {
            "exit-1": [{"execId": "fill-1", "execQty": "0.4"}],
            "exit-2": [
                {"execId": "fill-2", "execQty": "0.2"},
                {"execId": "fill-3", "execQty": "0.4"},
            ],
        }
        result = self.journal.log_exit(
            "entry", 0, 0, exit_reason="SL", closed_at=utcnow(),
            closure_records=records, closure_executions=executions,
        )
        self.assertTrue(result.recorded)
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter_by(order_link_id="entry").one()
            closures = session.query(TradeClosure).filter_by(trade_log_id=row.id).all()
            self.assertEqual(len(closures), 2)
            self.assertAlmostEqual(float(row.exit_price), 89.8)
            self.assertAlmostEqual(float(row.pnl_usdt), -10.4)
            self.assertEqual(row.exchange_exit_order_ids, ["exit-1", "exit-2"])
            self.assertEqual(sum(len(c.executions) for c in closures), 3)
        finally:
            session.close()

        repeated = self.journal.log_exit(
            "entry", 0, 0, closure_records=records, closure_executions=executions
        )
        self.assertFalse(repeated.recorded)
        self.assertTrue(repeated.already_closed)


if __name__ == "__main__":
    unittest.main()
