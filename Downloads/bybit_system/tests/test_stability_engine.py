import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics.repository import AnalyticsRepository
from analytics.stability import StabilityEngine, stability_profile
from stability_report import render_stability_text
from storage.migrations import run_safe_migrations
from storage.models import Base, TradeExpertVote, TradeLog


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        run_safe_migrations(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


class StabilityEngineTest(unittest.TestCase):
    def test_stable_expert_gets_high_score(self):
        profile = stability_profile(
            "expert_source",
            "expert:stable",
            self._synthetic_trades([1.0] * 220),
        )
        self.assertGreaterEqual(profile["stability_score"], 85)
        self.assertEqual(profile["trend"], "stable")
        self.assertEqual(profile["confidence"], "high")
        self.assertEqual(profile["degradation_flags"], [])

    def test_degrading_expert_is_detected(self):
        profile = stability_profile(
            "expert_source",
            "expert:degrading",
            self._synthetic_trades([1.0] * 180 + [-1.0] * 40),
        )
        self.assertEqual(profile["trend"], "weakening")
        self.assertIn("profit_factor_drop", profile["degradation_flags"])
        self.assertIn("expectancy_negative", profile["degradation_flags"])

    def test_small_sample_is_not_stable(self):
        profile = stability_profile(
            "symbol",
            "TINYUSDT",
            self._synthetic_trades([1.0] * 8),
        )
        self.assertEqual(profile["trend"], "unstable")
        self.assertEqual(profile["confidence"], "very_low")
        self.assertLessEqual(profile["stability_score"], 45)

    def test_report_handles_empty_database(self):
        db = SessionBackedDb()
        session = db.get_session()
        try:
            report = StabilityEngine(AnalyticsRepository(session)).build_report()
            self.assertEqual(report["summary"]["closed_trades"], 0)
            text = render_stability_text(report)
            self.assertIn("Closed trades: 0", text)
        finally:
            session.close()

    def test_old_trades_without_votes_are_supported(self):
        db = SessionBackedDb()
        session = db.get_session()
        try:
            session.add(self._trade_row("legacy-1", "LEGACYUSDT", 1.0, 0))
            session.commit()
            report = StabilityEngine(AnalyticsRepository(session)).build_report()
            self.assertIn("LEGACYUSDT", report["symbols"])
            self.assertEqual(report["expert_sources"], {})
        finally:
            session.close()

    def test_engine_groups_sources_families_symbols_regimes_and_combinations(self):
        db = SessionBackedDb()
        session = db.get_session()
        try:
            for i, pnl in enumerate([1.0] * 25):
                trade = self._trade_row(f"t-{i}", "ETHUSDT", pnl, i, regime="TREND", families="trend,orderflow")
                session.add(trade)
                session.flush()
                session.add(TradeExpertVote(
                    trade_log_id=trade.id,
                    order_link_id=trade.order_link_id,
                    symbol=trade.symbol,
                    source="expert:ema",
                    family="trend",
                    action=trade.action,
                    confidence=0.7,
                    contributed_to_final_decision=True,
                ))
            session.commit()

            report = StabilityEngine(AnalyticsRepository(session)).build_report()
            self.assertIn("expert:ema", report["expert_sources"])
            self.assertIn("trend", report["expert_families"])
            self.assertIn("ETHUSDT", report["symbols"])
            self.assertIn("TREND", report["regimes"])
            self.assertIn("orderflow,trend", report["confirmation_combinations"])
        finally:
            session.close()

    @staticmethod
    def _synthetic_trades(pnls):
        start = datetime.now(timezone.utc) - timedelta(days=10)
        return [
            {
                "order_link_id": f"synthetic-{i}",
                "symbol": "TESTUSDT",
                "regime": "TREND",
                "confirmation_families": "trend",
                "pnl_usdt": pnl,
                "pnl_pct": pnl,
                "holding_seconds": 1800,
                "closed_at": start + timedelta(minutes=i),
                "opened_at": start + timedelta(minutes=i - 30),
                "expert_votes": [{"source": "expert:test", "family": "trend"}],
            }
            for i, pnl in enumerate(pnls)
        ]

    def _trade_row(self, order_id, symbol, pnl, index, regime="TREND", families="trend"):
        opened = datetime.now(timezone.utc) - timedelta(hours=2, minutes=index)
        return TradeLog(
            symbol=symbol,
            action="open_long",
            source="decision:test",
            reason="test",
            order_link_id=order_id,
            entry_price=100,
            exit_price=101,
            size_usdt=100,
            leverage=1,
            pnl_usdt=pnl,
            pnl_pct=pnl,
            holding_seconds=1800,
            status="closed",
            opened_at=opened,
            closed_at=opened + timedelta(minutes=30),
            regime=regime,
            trend="UP",
            exit_type="take_profit" if pnl > 0 else "stop_loss",
            confirmation_families=families,
            confirmation_count=len(families.split(",")),
            entry_snapshot={
                "basic": {"primary_interval": "15"},
                "market_context": {"regime": regime, "trend": "UP"},
            },
        )


if __name__ == "__main__":
    unittest.main()
