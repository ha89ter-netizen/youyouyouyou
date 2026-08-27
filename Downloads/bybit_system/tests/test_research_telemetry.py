import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import BybitConfig
from storage.migrations import run_safe_migrations
from storage.models import (
    AccountSnapshot, Base, DecisionEvent, OperationalHealthEvent, PositionSnapshot,
    RejectionEvent, RunPolicyEpoch, Trade, TradeExcursion, TradeExitEvent, TradeLog,
    TradeProtectionEvent, TradingRun,
)
from storage.telemetry import TelemetryStore, config_hash, effective_config_document
from strategy.engine import StrategyEngine
from timeutils import to_epoch_ms, utcnow


class Db:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def cfg(run_id="telemetry-test"):
    value = BybitConfig()
    value.run_id = run_id
    value.commit_sha = "a" * 40
    value.api_key = "API_KEY_MUST_NOT_PERSIST"
    value.api_secret = "API_SECRET_MUST_NOT_PERSIST"
    value.openai_api_key = "OPENAI_SECRET_MUST_NOT_PERSIST"
    value.symbols = ["SOLUSDT"]
    value.telemetry_account_interval_sec = 60
    value.telemetry_position_interval_sec = 30
    return value


class ResearchTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.db = Db()
        self.cfg = cfg()
        self.root_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.root_tmp.name)
        (self.root / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
        self.store = TelemetryStore(self.db, self.cfg)
        self.started = utcnow()
        self.store.ensure_run(
            root=self.root, started_at=self.started,
            startup_account_snapshot={"wallet_balance": 1000},
        )

    def tearDown(self):
        self.root_tmp.cleanup()

    def _trade(self, action="open_long", order_link="entry-1", entry=100, qty=1):
        session = self.db.get_session()
        row = TradeLog(
            symbol="SOLUSDT", action=action, source="test", reason="test",
            order_link_id=order_link, run_id=self.cfg.run_id,
            entry_price=entry, size_usdt=entry * qty, leverage=1,
            stop_loss_price=98 if action == "open_long" else 102,
            take_profit_price=104 if action == "open_long" else 96,
            entry_filled_qty=qty, status="open", opened_at=self.started,
        )
        session.add(row)
        session.commit()
        session.expunge(row)
        session.close()
        return row

    def _market(self, prices):
        session = self.db.get_session()
        base = to_epoch_ms(self.started)
        for index, price in enumerate(prices, 1):
            session.add(Trade(
                symbol="SOLUSDT", trade_id=f"t-{index}", ts=base + index * 1000,
                side="Buy", price=price, size=1,
            ))
        session.commit()
        session.close()
        return base + (len(prices) + 1) * 1000

    def test_immutable_run_and_policy_epoch(self):
        original = self.db.get_session().query(TradingRun).one()
        original_confidence = original.strategy_config["min_open_confidence"]
        original_hash = original.config_hash
        self.cfg.min_open_confidence += 0.01
        self.store.ensure_run(
            root=self.root, started_at=self.started,
            startup_account_snapshot={"wallet_balance": 999},
        )
        session = self.db.get_session()
        run = session.query(TradingRun).one()
        epochs = session.query(RunPolicyEpoch).order_by(RunPolicyEpoch.epoch).all()
        self.assertEqual(run.config_hash, original_hash)
        self.assertEqual(run.strategy_config["min_open_confidence"], original_confidence)
        self.assertEqual([row.epoch for row in epochs], [0, 1])
        self.assertIn("resolved.min_open_confidence", epochs[1].config_diff)
        session.close()

        # Returning to the original configuration is another immutable policy
        # transition; it must not disappear merely because it matches epoch 0.
        self.cfg.min_open_confidence = original_confidence
        self.store.ensure_run(
            root=self.root, started_at=self.started,
            startup_account_snapshot={"wallet_balance": 998},
        )
        session = self.db.get_session()
        run = session.query(TradingRun).one()
        epochs = session.query(RunPolicyEpoch).order_by(RunPolicyEpoch.epoch).all()
        self.assertEqual(run.config_hash, original_hash)
        self.assertEqual([row.epoch for row in epochs], [0, 1, 2])
        self.assertEqual(epochs[2].config_hash, original_hash)
        self.assertIn("resolved.min_open_confidence", epochs[2].config_diff)
        session.close()

    def test_config_hash_stability_and_secrets_are_redacted(self):
        self.assertEqual(config_hash(self.cfg), config_hash(self.cfg))
        rendered = json.dumps(effective_config_document(self.cfg), sort_keys=True)
        self.assertNotIn("API_KEY_MUST_NOT_PERSIST", rendered)
        self.assertNotIn("API_SECRET_MUST_NOT_PERSIST", rendered)
        self.assertNotIn("OPENAI_SECRET_MUST_NOT_PERSIST", rendered)
        session = self.db.get_session()
        persisted = json.dumps(session.query(TradingRun).one().effective_config, sort_keys=True)
        session.close()
        self.assertNotIn("API_KEY_MUST_NOT_PERSIST", persisted)
        self.assertNotIn("API_SECRET_MUST_NOT_PERSIST", persisted)
        self.assertNotIn("OPENAI_SECRET_MUST_NOT_PERSIST", persisted)

    def test_decision_payload_avoids_normalized_json_duplication(self):
        self.store.record_decision({
            "evaluation_id": "evaluation-1",
            "phase": "candidate",
            "symbol": "SOLUSDT",
            "side": "open_long",
            "signal_outputs": [{"source": "ema", "action": "open_long"}],
            "confirmation_families": ["trend"],
            "filter_results": {"fresh": True},
            "final_decision": "hold",
            "decision_reason": "risk gate",
            "accepted": False,
            "rejections": [{
                "stage": "risk", "code": "blocked", "reason": "risk gate",
                "context": {"breaker": True},
            }],
            "future_extension": {"kept": True},
        })
        session = self.db.get_session()
        decision = session.query(DecisionEvent).one()
        rejection = session.query(RejectionEvent).one()
        self.assertEqual(decision.signal_outputs[0]["source"], "ema")
        self.assertEqual(decision.structured_payload, {
            "schema": "normalized-v1",
            "extra": {"future_extension": {"kept": True}},
        })
        self.assertEqual(rejection.structured_context, {"breaker": True})
        session.close()

    def test_source_change_refuses_same_immutable_run(self):
        (self.root / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source tree fingerprint"):
            self.store.ensure_run(
                root=self.root, started_at=self.started, startup_account_snapshot=None
            )

    def test_long_mfe_mae_and_partial_fill_quantity(self):
        trade = self._trade(qty=0.4)
        observed_ms = self._market([101, 105, 97, 102])
        inserted = self.store.persist_position_snapshots([{
            "symbol": "SOLUSDT", "side": "Buy", "size": "0.4", "avgPrice": "100",
            "markPrice": "102", "lastPrice": "102", "unrealisedPnl": "0.8",
            "stopLoss": "98", "takeProfit": "104",
        }], observed_at=__import__("timeutils").from_epoch_ms(observed_ms))
        self.assertEqual(inserted, 1)
        session = self.db.get_session()
        row = session.query(TradeExcursion).filter_by(trade_log_id=trade.id).one()
        self.assertAlmostEqual(float(row.mfe_price_distance), 5)
        self.assertAlmostEqual(float(row.mae_price_distance), 3)
        self.assertAlmostEqual(float(row.mfe_usdt), 2.0)
        self.assertAlmostEqual(float(row.mae_usdt), 1.2)
        self.assertAlmostEqual(float(row.mfe_r), 2.5)
        self.assertTrue(row.tp_reached_intrabar)
        self.assertTrue(row.sl_reached_intrabar)
        session.close()

    def test_short_mfe_mae(self):
        self._trade(action="open_short")
        observed_ms = self._market([99, 94, 103, 98])
        self.store.persist_position_snapshots([{
            "symbol": "SOLUSDT", "side": "Sell", "size": "1", "avgPrice": "100",
            "markPrice": "98", "lastPrice": "98", "stopLoss": "102", "takeProfit": "96",
        }], observed_at=__import__("timeutils").from_epoch_ms(observed_ms))
        session = self.db.get_session()
        row = session.query(TradeExcursion).one()
        self.assertAlmostEqual(float(row.mfe_price_distance), 6)
        self.assertAlmostEqual(float(row.mae_price_distance), 3)
        self.assertAlmostEqual(float(row.mfe_r), 3)
        session.close()

    def test_restart_recovery_and_repeated_snapshot_are_idempotent(self):
        self._trade()
        observed_ms = self._market([101])
        observed = __import__("timeutils").from_epoch_ms(observed_ms)
        position = {
            "symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
            "markPrice": "101", "stopLoss": "98", "takeProfit": "104",
        }
        self.assertEqual(self.store.persist_position_snapshots([position], observed_at=observed), 1)
        restarted = TelemetryStore(self.db, self.cfg)
        self.assertEqual(restarted.persist_position_snapshots([position], observed_at=observed), 0)
        session = self.db.get_session()
        self.assertEqual(session.query(PositionSnapshot).count(), 1)
        self.assertEqual(session.query(TradeExcursion).count(), 1)
        session.close()

    def test_account_snapshot_idempotency_and_stale_marking(self):
        account = {"wallet_balance": 100, "equity": 101, "fetch_status": "ok"}
        self.assertTrue(self.store.persist_account_snapshot(account, [], observed_at=self.started))
        self.assertFalse(self.store.persist_account_snapshot(account, [], observed_at=self.started))
        self._trade()
        old = self.started
        self.store.persist_position_snapshots([{
            "symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
            "markPrice": "100", "stopLoss": "98", "takeProfit": "104",
        }], observed_at=old)
        session = self.db.get_session()
        self.assertEqual(session.query(AccountSnapshot).count(), 1)
        self.assertTrue(session.query(PositionSnapshot).one().is_stale)
        session.close()

    def test_duplicate_protection_event_and_lifecycle(self):
        trade = self._trade()
        when = utcnow()
        args = dict(
            reason="test tightening", source_module="test", success=True,
            observed_at=when, exchange_order_id="sl-2",
        )
        self.assertTrue(self.store.record_protection_event(
            trade, "protection_tightened", {"sl": 98}, {"sl": 99}, **args
        ))
        self.assertFalse(self.store.record_protection_event(
            trade, "protection_tightened", {"sl": 98}, {"sl": 99}, **args
        ))
        session = self.db.get_session()
        self.assertEqual(session.query(TradeProtectionEvent).count(), 1)
        session.close()

    def test_protection_acknowledgement_links_trade_and_deduplicates_state(self):
        trade = self._trade()
        position = {
            "symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
            "markPrice": "101", "stopLoss": "98", "takeProfit": "104",
        }
        orders = {"SOLUSDT": [{
            "orderId": "native-sl-1", "orderLinkId": "", "orderStatus": "Untriggered",
            "stopOrderType": "StopLoss", "triggerPrice": "98",
        }, {
            "orderId": "native-tp-1", "orderLinkId": "", "orderStatus": "Untriggered",
            "stopOrderType": "TakeProfit", "triggerPrice": "104",
        }]}
        self.assertEqual(self.store.persist_position_snapshots(
            [position], protective_orders=orders, observed_at=self.started
        ), 1)
        later = self.started + __import__("datetime").timedelta(seconds=31)
        self.assertEqual(self.store.persist_position_snapshots(
            [position], protective_orders=orders, observed_at=later
        ), 1)
        session = self.db.get_session()
        events = session.query(TradeProtectionEvent).filter_by(
            event_type="exchange_protection_acknowledged"
        ).all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].trade_log_id, trade.id)
        self.assertEqual(
            sorted(item["order_id"] for item in events[0].new_value["orders"]),
            ["native-sl-1", "native-tp-1"],
        )
        session.close()

    def test_inherited_position_keeps_owner_run_and_records_processing_run(self):
        owner_cfg = cfg("owner-run")
        owner_store = TelemetryStore(self.db, owner_cfg)
        owner_store.ensure_run(
            root=self.root, started_at=self.started,
            startup_account_snapshot={"wallet_balance": 1000},
        )
        session = self.db.get_session()
        trade = TradeLog(
            symbol="ETHUSDT", action="open_short", source="test", reason="inherited",
            order_link_id="inherited-entry", run_id="owner-run", entry_price=100,
            size_usdt=100, leverage=1, stop_loss_price=102, take_profit_price=96,
            entry_filled_qty=1, status="open", opened_at=self.started,
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id
        session.close()
        position = {
            "symbol": "ETHUSDT", "side": "Sell", "size": "1", "avgPrice": "100",
            "markPrice": "99", "stopLoss": "102", "takeProfit": "96",
        }
        orders = {"ETHUSDT": [{
            "orderId": "owner-sl", "orderStatus": "Untriggered",
            "stopOrderType": "StopLoss", "triggerPrice": "102",
        }, {
            "orderId": "owner-tp", "orderStatus": "Untriggered",
            "stopOrderType": "TakeProfit", "triggerPrice": "96",
        }]}
        self.assertEqual(self.store.persist_position_snapshots(
            [position], protective_orders=orders, observed_at=self.started
        ), 1)
        session = self.db.get_session()
        snapshot = session.query(PositionSnapshot).filter_by(trade_log_id=trade_id).one()
        event = session.query(TradeProtectionEvent).filter_by(
            trade_log_id=trade_id,
            event_type="exchange_protection_acknowledged",
        ).one()
        excursion = session.query(TradeExcursion).filter_by(trade_log_id=trade_id).one()
        self.assertEqual(snapshot.run_id, "owner-run")
        self.assertEqual(snapshot.processing_run_id, self.cfg.run_id)
        self.assertEqual(event.run_id, "owner-run")
        self.assertEqual(event.processing_run_id, self.cfg.run_id)
        self.assertEqual(excursion.run_id, "owner-run")
        self.assertEqual(excursion.last_processing_run_id, self.cfg.run_id)
        session.close()

    def test_inherited_exit_is_owner_attributed_and_idempotent(self):
        owner_cfg = cfg("exit-owner-run")
        TelemetryStore(self.db, owner_cfg).ensure_run(
            root=self.root, started_at=self.started,
            startup_account_snapshot={"wallet_balance": 1000},
        )
        session = self.db.get_session()
        trade = TradeLog(
            symbol="ETHUSDT", action="open_long", source="test", reason="inherited",
            order_link_id="inherited-exit", run_id="exit-owner-run", entry_price=100,
            size_usdt=100, leverage=1, stop_loss_price=98, take_profit_price=104,
            entry_filled_qty=1, status="closed", opened_at=self.started,
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id
        session.close()
        record = {
            "orderId": "close-owner-1", "avgExitPrice": "104", "closedSize": "1",
            "closedPnl": "3.8", "updatedTime": to_epoch_ms(self.started) + 1000,
        }
        executions = {"close-owner-1": [{
            "execId": "exec-owner-1", "orderId": "close-owner-1",
            "execPrice": "104", "execQty": "1", "execTime": to_epoch_ms(self.started) + 1000,
            "stopOrderType": "TakeProfit",
        }]}
        self.assertTrue(self.store.finalize_trade(
            "inherited-exit", actual_exit_reason="TP", records=[record],
            executions_by_order=executions, realized_pnl=3.8, fees=0.2,
        ))
        self.assertFalse(self.store.finalize_trade(
            "inherited-exit", actual_exit_reason="TP", records=[record],
            executions_by_order=executions, realized_pnl=3.8, fees=0.2,
        ))
        session = self.db.get_session()
        event = session.query(TradeExitEvent).filter_by(trade_log_id=trade_id).one()
        excursion = session.query(TradeExcursion).filter_by(trade_log_id=trade_id).one()
        self.assertEqual(event.run_id, "exit-owner-run")
        self.assertEqual(event.processing_run_id, self.cfg.run_id)
        self.assertEqual(event.closing_execution_ids, ["exec-owner-1"])
        self.assertEqual(excursion.finalized_by_run_id, self.cfg.run_id)
        self.assertEqual(session.query(TradeExitEvent).filter_by(trade_log_id=trade_id).count(), 1)
        session.close()

    def test_database_failure_is_buffered_and_later_persisted(self):
        original = self.db.get_session
        attempts = {"count": 0}
        def failing():
            attempts["count"] += 1
            raise RuntimeError("database offline")
        self.db.get_session = failing
        self.assertFalse(self.store.record_health("db", "probe", "error", "failed"))
        self.assertLessEqual(len(self.store._pending_health), 100)
        self.db.get_session = original
        self.assertTrue(self.store.record_health("db", "recovered", "info", "ok"))
        session = original()
        kinds = {row.event_type for row in session.query(OperationalHealthEvent).all()}
        self.assertIn("database_write_failure", kinds)
        self.assertIn("recovered", kinds)
        session.close()

    def test_migrations_are_idempotent_and_preserve_historical_rows(self):
        session = self.db.get_session()
        session.add(TradeLog(
            symbol="OLDUSDT", action="open_long", source="legacy", reason="legacy",
            order_link_id="legacy", entry_price=1, size_usdt=1, leverage=1,
            status="closed", opened_at=self.started, closed_at=self.started,
        ))
        session.commit()
        session.close()
        run_safe_migrations(self.db.engine)
        run_safe_migrations(self.db.engine)
        session = self.db.get_session()
        self.assertEqual(session.query(TradeLog).filter_by(order_link_id="legacy").count(), 1)
        session.close()


class OpenPositionManagementFailureTest(unittest.TestCase):
    def test_account_api_failure_does_not_skip_open_position_management(self):
        engine = StrategyEngine.__new__(StrategyEngine)
        engine.cfg = cfg("")
        engine.cfg.symbols = ["SOLUSDT", "ETHUSDT"]
        position = {"symbol": "SOLUSDT", "size": "1", "side": "Buy"}
        engine.execution = SimpleNamespace(
            get_open_positions=lambda: [position],
            get_account_state=lambda: (_ for _ in ()).throw(RuntimeError("wallet down")),
        )
        calls = []
        engine._resolve_pending_entries = lambda positions: calls.append("pending")
        engine._manage_time_range_tightening = lambda positions: calls.append("tighten")
        engine._manage_trailing_stops = lambda positions: calls.append("trailing")
        engine._sync_closed_trades = lambda positions: calls.append("reconcile")
        engine._process_symbol = lambda symbol, balance, positions, execute=False: calls.append(symbol) or False
        engine._execute_ranked_candidates = lambda *args: calls.append("execute")
        engine.run_once()
        self.assertEqual(calls[:4], ["pending", "tighten", "trailing", "reconcile"])
        self.assertIn("SOLUSDT", calls)
        self.assertNotIn("ETHUSDT", calls)
        self.assertNotIn("execute", calls)


if __name__ == "__main__":
    unittest.main()
