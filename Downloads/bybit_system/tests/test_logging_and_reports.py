import logging
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logging_config import configure_app_logging
from export_trade_dataset import export_trade_dataset
from storage.journal import TradeJournal
from storage.migrations import run_safe_migrations
from storage.models import Base, TradeExpertVote, TradeLog
from trade_report import compute_trade_report_stats
from strategy.signal import Action


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


class LoggingAndReportTest(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_bybit_managed_handler", False):
                root.removeHandler(handler)
                handler.close()

    def test_logging_creates_file_without_duplicate_handlers_and_masks_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["BYBIT_API_SECRET"] = "super-secret-value"
            log_path = configure_app_logging("trading-test", "trading.log", log_dir=Path(tmpdir))
            configure_app_logging("trading-test", "trading.log", log_dir=Path(tmpdir))

            managed = [
                h for h in logging.getLogger().handlers
                if getattr(h, "_bybit_managed_handler", False)
            ]
            self.assertEqual(len(managed), 2)

            logger = logging.getLogger("test.logging")
            logger.info("secret=%s visible=ok", "super-secret-value")
            for handler in managed:
                handler.flush()

            content = log_path.read_text(encoding="utf-8")
            self.assertIn("visible=ok", content)
            self.assertNotIn("super-secret-value", content)
            self.assertIn("***", content)

    def test_trade_report_stats_ignore_open_trades_for_win_rate(self):
        rows = [
            SimpleNamespace(
                status="closed", pnl_usdt=10, holding_seconds=60, action="open_long",
                symbol="ETHUSDT", confirmation_families="trend, price_location", source="decision:ema+vwap",
            ),
            SimpleNamespace(
                status="closed", pnl_usdt=-5, holding_seconds=120, action="open_short",
                symbol="SOLUSDT", confirmation_families="trend, trade_flow", source="decision:ema+momentum",
            ),
            SimpleNamespace(
                status="open", pnl_usdt=None, holding_seconds=None, action="open_long",
                symbol="BNBUSDT", confirmation_families="trend, price_location", source="decision:ema+vwap",
            ),
        ]

        stats = compute_trade_report_stats(rows)

        self.assertEqual(stats["closed_count"], 2)
        self.assertEqual(stats["open_count"], 1)
        self.assertEqual(float(stats["win_rate"]), 50.0)
        self.assertEqual(float(stats["average_win"]), 10.0)
        self.assertEqual(float(stats["average_loss"]), -5.0)
        self.assertEqual(float(stats["profit_factor"]), 2.0)
        self.assertEqual(int(stats["average_holding_seconds"]), 90)

    def test_journal_exit_records_reason_and_holding_time(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)
        self.assertTrue(journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "decision:ema+vwap", "entry",
            100, 50, 1, 1.5, 3.0, "oid-analytics",
            market_context="Trend: UP", regime="TREND", trend="UP",
            decision_confidence=0.71, expected_rr=2.1,
            confirmation_count=2, confirmation_families="trend, price_location",
            entry_reason="confirmed entry",
            entry_snapshot={
                "basic": {"symbol": "ETHUSDT", "api_key": os.getenv("BYBIT_API_KEY")},
                "decision": {"confidence": 0.71},
            },
            expert_votes=[
                {
                    "source": "expert:ema",
                    "family": "trend",
                    "action": "open_long",
                    "confidence": 0.70,
                    "reason": "ema state",
                    "contributed_to_final_decision": True,
                },
                {
                    "source": "expert:vwap",
                    "family": "price_location",
                    "action": "open_long",
                    "confidence": 0.64,
                    "reason": "vwap state",
                    "contributed_to_final_decision": True,
                },
            ],
        ))

        self.assertFalse(journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "decision:ema+vwap", "duplicate",
            100, 50, 1, 1.5, 3.0, "oid-analytics",
        ))

        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-analytics").one()
            self.assertEqual(row.entry_snapshot["basic"]["symbol"], "ETHUSDT")
            votes = session.query(TradeExpertVote).filter(
                TradeExpertVote.order_link_id == "oid-analytics"
            ).all()
            self.assertEqual(len(votes), 2)
            self.assertTrue(all(v.contributed_to_final_decision for v in votes))
            row.opened_at = datetime.now(timezone.utc) - timedelta(seconds=90)
            session.commit()
        finally:
            session.close()

        time.sleep(0.01)
        self.assertTrue(journal.log_exit(
            "oid-analytics", 103, 1.5, exit_reason="TP",
            exit_snapshot={"regime": "TREND", "secret": "do-not-store"},
        ))
        self.assertFalse(journal.log_exit("oid-analytics", 103, 1.5, exit_reason="TP"))

        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-analytics").one()
            self.assertEqual(row.exit_reason, "TP")
            self.assertEqual(row.exit_type, "take_profit")
            self.assertAlmostEqual(float(row.pnl_pct), 3.0)
            self.assertGreaterEqual(row.holding_seconds, 89)
            self.assertEqual(row.entry_reason, "confirmed entry")
            self.assertEqual(row.regime, "TREND")
            self.assertEqual(row.confirmation_count, 2)
            self.assertEqual(row.exit_snapshot["secret"], "***")
        finally:
            session.close()

    def test_missing_optional_snapshot_data_does_not_break_entry(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)
        self.assertTrue(journal.log_entry(
            "SOLUSDT", Action.OPEN_SHORT, "decision:test", "entry",
            50, 25, 1, None, None, "oid-missing",
            entry_snapshot={"technical": {"rsi": float("nan"), "atr": float("inf")}},
            expert_votes=[{"source": "expert:ema", "action": "open_short", "confidence": 1.5}],
        ))

        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-missing").one()
            self.assertIsNone(row.entry_snapshot["technical"]["rsi"])
            self.assertIsNone(row.entry_snapshot["technical"]["atr"])
            vote = session.query(TradeExpertVote).filter(TradeExpertVote.order_link_id == "oid-missing").one()
            self.assertEqual(float(vote.confidence), 1.0)
        finally:
            session.close()

    def test_safe_migration_is_idempotent(self):
        db = SessionBackedDb()
        run_safe_migrations(db.engine)
        run_safe_migrations(db.engine)

        session = db.get_session()
        try:
            self.assertEqual(session.query(TradeLog).count(), 0)
            self.assertEqual(session.query(TradeExpertVote).count(), 0)
        finally:
            session.close()

    def test_export_trade_dataset_writes_csv_and_jsonl_row(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)
        self.assertTrue(journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "decision:ema+vwap", "entry",
            100, 50, 1, 1.5, 3.0, "oid-export",
            regime="TREND", trend="UP", decision_confidence=0.70,
            expected_rr=2.0, confirmation_count=2,
            confirmation_families="trend, price_location",
            entry_snapshot={"basic": {"symbol": "ETHUSDT"}},
            expert_votes=[{"source": "expert:ema", "family": "trend", "action": "open_long", "confidence": 0.7}],
        ))
        self.assertTrue(journal.log_exit("oid-export", 102, 1.0, exit_reason="manual/unknown"))

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "dataset.csv"
            jsonl_path = Path(tmpdir) / "dataset.jsonl"
            count, _, _ = export_trade_dataset(csv_path, jsonl_path, db=db)
            self.assertEqual(count, 1)
            csv_text = csv_path.read_text(encoding="utf-8")
            jsonl_text = jsonl_path.read_text(encoding="utf-8")
            self.assertIn("oid-export", csv_text)
            self.assertIn("expert:ema", jsonl_text)


if __name__ == "__main__":
    unittest.main()
