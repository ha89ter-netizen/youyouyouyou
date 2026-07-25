import tempfile
import unittest
from pathlib import Path

from config.settings import BybitConfig
from runtime_control import RuntimeService
import runtime_control
from storage.db import Database
from storage.journal import TradeJournal
from storage.legacy_orphans import LEGACY_ORPHAN_IDS, classify_known_legacy_orphans
from storage.models import Base, RunMetadata, TradeLog
from strategy.engine import StrategyEngine
from timeutils import utcnow


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
        session = self.db.get_session()
        try:
            session.add(RunMetadata(
                run_id=self.cfg.run_id,
                commit_sha=self.cfg.commit_sha,
                environment_summary={"testnet": True},
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
                self.assertIsNotNone(row.collector_pid)
            finally:
                session.close()
        finally:
            second.stop()
            first.stop()
            runtime_control.RUNTIME_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
