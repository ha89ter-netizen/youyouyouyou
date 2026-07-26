import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config.settings import BybitConfig
import live_run
from risk.risk_manager import RiskManager
from runtime_control import DatabaseProcessLock, RuntimeService
import runtime_control
from storage.db import Database
from storage.journal import TradeJournal
from storage.legacy_orphans import LEGACY_ORPHAN_IDS, classify_known_legacy_orphans
from storage.models import Base, RunMetadata, TradeLog
from storage.risk_state import RiskStateStore
from strategy.engine import StrategyEngine
from timeutils import utc_day_str, utcnow


class LiveRunInfrastructureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "test.db"
        self.cfg = BybitConfig(
            api_key="test",
            api_secret="test",
            testnet=True,
            db_url=f"sqlite:///{db_path}",
            run_id="testnet-test-run",
            commit_sha="abc123",
        )
        self.db = Database(self.cfg)
        Base.metadata.create_all(self.db.engine)

    def tearDown(self):
        self.tmp.cleanup()

    def _trade(self, oid, status="orphaned"):
        return TradeLog(
            symbol="ETHUSDT",
            action="open_long",
            source="test",
            reason="test",
            order_link_id=oid,
            entry_price=100,
            size_usdt=10,
            leverage=1,
            status=status,
            opened_at=utcnow(),
        )

    def test_only_known_legacy_ids_are_classified(self):
        future = "future-orphan-must-stay-blocking"
        session = self.db.get_session()
        try:
            session.add_all([self._trade(oid) for oid in LEGACY_ORPHAN_IDS])
            session.add(self._trade(future))
            session.commit()
        finally:
            session.close()

        self.assertEqual(classify_known_legacy_orphans(self.db.engine), 7)
        self.assertEqual(classify_known_legacy_orphans(self.db.engine), 0)

        session = self.db.get_session()
        try:
            legacy = session.query(TradeLog).filter(
                TradeLog.status == "historical_orphan"
            ).all()
            future_row = session.query(TradeLog).filter_by(order_link_id=future).one()
            self.assertEqual(len(legacy), 7)
            self.assertTrue(all(row.legacy_orphan_reason for row in legacy))
            self.assertTrue(all(row.legacy_classified_at for row in legacy))
            self.assertEqual(future_row.status, "orphaned")
        finally:
            session.close()

    def test_historical_orphan_does_not_arm_breaker_but_future_orphan_does(self):
        session = self.db.get_session()
        try:
            historical = self._trade(LEGACY_ORPHAN_IDS[0], "historical_orphan")
            historical.legacy_orphan_reason = "retention expired"
            historical.legacy_classified_at = utcnow()
            session.add(historical)
            session.commit()
        finally:
            session.close()

        engine = StrategyEngine(self.cfg, self.db)
        self.assertFalse(engine.risk_manager.circuit_breaker_tripped)

        session = self.db.get_session()
        try:
            session.add(self._trade("future-orphan"))
            session.commit()
        finally:
            session.close()
        engine = StrategyEngine(self.cfg, self.db)
        self.assertTrue(engine.risk_manager.circuit_breaker_tripped)
        self.assertIn("orphan:future-orphan", engine.risk_manager.breaker_causes())
        self.assertNotIn(
            f"orphan:{LEGACY_ORPHAN_IDS[0]}",
            engine.risk_manager.breaker_causes(),
        )

    def test_new_trade_records_run_and_execution_observability(self):
        journal = TradeJournal(self.db)
        self.assertTrue(journal.log_entry(
            symbol="ETHUSDT",
            action="open_long",
            source="test",
            reason="test",
            entry_price=100,
            size_usdt=10,
            leverage=1,
            stop_loss_pct=1.5,
            take_profit_pct=3,
            order_link_id="run-observability-order",
            run_id=self.cfg.run_id,
            exchange_entry_order_id="exchange-entry",
            stop_loss_price=98.5,
            take_profit_price=103,
            entry_fee_usdt=0.01,
        ))
        result = journal.log_exit(
            "run-observability-order",
            103,
            0.28,
            exchange_exit_order_id="exchange-exit",
            exit_fee_usdt=0.01,
            total_fee_usdt=0.02,
        )
        self.assertTrue(result.recorded)
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter_by(
                order_link_id="run-observability-order"
            ).one()
            self.assertEqual(row.run_id, self.cfg.run_id)
            self.assertEqual(row.exchange_entry_order_id, "exchange-entry")
            self.assertEqual(row.exchange_exit_order_id, "exchange-exit")
            self.assertEqual(float(row.total_fee_usdt), 0.02)
        finally:
            session.close()

    def test_pid_lock_rejects_duplicate_and_heartbeat_records_run(self):
        old_dir = runtime_control.RUNTIME_DIR
        runtime_control.RUNTIME_DIR = Path(self.tmp.name) / "runtime"
        runtime_control.RUNTIME_DIR.mkdir()
        (runtime_control.RUNTIME_DIR / "collector.json").write_text(
            json.dumps({"pid": os.getpid(), "run_id": "stale-run"}),
            encoding="utf-8",
        )
        session = self.db.get_session()
        try:
            session.add(RunMetadata(
                run_id=self.cfg.run_id,
                commit_sha=self.cfg.commit_sha,
                environment_summary={"testnet": True},
                collector_pid=999999,
                collector_heartbeat_at=utcnow(),
            ))
            session.commit()
        finally:
            session.close()
        first = RuntimeService(self.db, self.cfg.run_id, "collector", interval_sec=60)
        second = RuntimeService(self.db, self.cfg.run_id, "collector", interval_sec=60)
        try:
            first.start()
            with self.assertRaisesRegex(RuntimeError, "Duplicate collector"):
                second.start()
            session = self.db.get_session()
            try:
                row = session.query(RunMetadata).filter_by(run_id=self.cfg.run_id).one()
                self.assertIsNotNone(row.collector_heartbeat_at)
                self.assertEqual(row.collector_pid, os.getpid())
            finally:
                session.close()
        finally:
            second.stop()
            first.stop()
            runtime_control.RUNTIME_DIR = old_dir

    def test_railway_subprocess_inherits_output_and_is_unbuffered(self):
        process = Mock(pid=1234)
        with patch.dict(os.environ, {"RUNTIME_MODE": "railway"}, clear=False):
            with patch.object(live_run.subprocess, "Popen", return_value=process) as popen:
                result = live_run._spawn_process("main.py", "run-1", "sha-1")

        self.assertIs(result, process)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][1], "-u")
        self.assertIsNone(kwargs["stdout"])
        self.assertIsNone(kwargs["stderr"])
        self.assertEqual(kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(kwargs["env"]["RUN_ID"], "run-1")

    def test_collector_wait_ignores_stale_pid_from_previous_run(self):
        now = 1_800_000_000.0
        session = Mock()
        run_query = Mock()
        run_query.filter_by.return_value.first.return_value = Mock(
            collector_heartbeat_at=utcnow()
        )
        candle_query = Mock()
        candle_query.scalar.return_value = int(now * 1000)
        book_query = Mock()
        book_query.scalar.return_value = int(now * 1000)
        session.query.side_effect = [run_query, candle_query, book_query]
        db = Mock()
        db.get_session.return_value = session

        with patch.object(
            live_run,
            "_service_info",
            return_value={"run_id": "previous-run", "pid": 999999},
        ), patch.object(live_run, "_process_alive", return_value=False), patch.object(
            live_run.time, "time", return_value=now
        ):
            live_run._wait_for_collector(db, "current-run", timeout=1)

        session.close.assert_called_once()

    def test_postgresql_advisory_lock_rejects_duplicate_container(self):
        connection = Mock()
        connection.execute.return_value.scalar.return_value = False
        db = Mock()
        db.engine.dialect.name = "postgresql"
        db.engine.connect.return_value = connection

        lock = DatabaseProcessLock(db, "trader")
        with self.assertRaisesRegex(RuntimeError, "Duplicate trader"):
            lock.start()
        connection.close.assert_called_once()

    def test_railway_rejects_missing_or_localhost_database_url(self):
        missing = BybitConfig(runtime_mode="railway", db_url="")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is required"):
                live_run._validate_runtime_config(missing)

        localhost = BybitConfig(
            runtime_mode="railway",
            db_url="postgresql://user:pass@localhost:5432/bybit",
        )
        with patch.dict(
            os.environ,
            {"DATABASE_URL": localhost.db_url},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "localhost"):
                live_run._validate_runtime_config(localhost)

    def test_restart_recovers_run_identity_and_risk_state_from_database(self):
        session = self.db.get_session()
        try:
            session.add(RunMetadata(
                run_id="durable-railway-run",
                commit_sha="railway-sha",
                environment_summary={"testnet": True, "runtime_mode": "railway"},
                status="running",
                collector_pid=111,
                trader_pid=222,
                collector_heartbeat_at=utcnow(),
                trader_heartbeat_at=utcnow(),
            ))
            session.commit()
        finally:
            session.close()

        durable_run = live_run._find_resumable_run(self.db, "railway-sha")
        self.assertIsNotNone(durable_run)
        self.assertEqual(durable_run.run_id, "durable-railway-run")

        store = RiskStateStore(self.db)
        self.assertTrue(store.save({
            "day_utc": utc_day_str(),
            "daily_start_balance": 1000,
            "daily_pnl_usdt": -12.5,
            "daily_trade_count": 3,
            "symbol_trade_counts": {"ETHUSDT": 2},
            "last_entry_ts_by_symbol": {"ETHUSDT": 100.0},
            "pending_entries": {"SOLUSDT": 200.0},
            "blocked_symbols": {"XRPUSDT": "manual review"},
            "circuit_breaker_causes": {
                "orphan:test": {"reason": "unknown result", "sticky": True}
            },
            "circuit_breaker_tripped": True,
            "circuit_breaker_reason": "unknown result",
            "circuit_breaker_sticky": True,
        }))
        restored = RiskManager(self.cfg, state_store=RiskStateStore(self.db))
        self.assertEqual(restored._daily_pnl_usdt, -12.5)
        self.assertEqual(restored._daily_trade_count, 3)
        self.assertEqual(restored.pending_entry_symbols(), ["SOLUSDT"])
        self.assertEqual(restored.blocked_symbols()["XRPUSDT"], "manual review")
        self.assertIn("orphan:test", restored.breaker_causes())


if __name__ == "__main__":
    unittest.main()
