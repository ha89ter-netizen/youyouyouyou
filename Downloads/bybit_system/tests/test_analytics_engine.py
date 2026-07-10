import csv
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analytics.attribution import fractional_attribution_rows, full_attribution_rows, normalize_families
from analytics.engine import AnalyticsEngine, openai_audit
from analytics.metrics import max_drawdown, result_metrics
from analytics.repository import AnalyticsFilters, AnalyticsRepository
from analytics.reliability import reliability_status
from analytics_report import _json_safe, _report_csv
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


class AnalyticsEngineTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionBackedDb()
        self.session = self.db.get_session()
        self.now = datetime.now(timezone.utc)
        self._seed()

    def tearDown(self):
        self.session.close()

    def test_closed_only_and_basic_metrics(self):
        trades = AnalyticsRepository(self.session).load_closed_trades(AnalyticsFilters())
        self.assertEqual([t["order_link_id"] for t in trades], ["t1", "t2", "t3", "legacy"])

        metrics = result_metrics(trades)
        self.assertEqual(metrics["wins"], 1)
        self.assertEqual(metrics["losses"], 1)
        self.assertEqual(metrics["breakeven"], 2)
        self.assertEqual(metrics["total_closed_trades"], 4)
        self.assertAlmostEqual(metrics["win_rate"], 0.25)
        self.assertAlmostEqual(metrics["profit_factor"], 2.0)
        self.assertAlmostEqual(metrics["expectancy_usdt"], 1.25)
        self.assertAlmostEqual(metrics["payoff_ratio"], 2.0)
        self.assertEqual(metrics["maximum_consecutive_wins"], 1)
        self.assertEqual(metrics["maximum_consecutive_losses"], 1)

    def test_profit_factor_without_losses_is_infinity(self):
        metrics = result_metrics([
            {"pnl_usdt": 1, "closed_at": self.now},
            {"pnl_usdt": 2, "closed_at": self.now},
        ])
        self.assertEqual(metrics["profit_factor"], float("inf"))

    def test_drawdown(self):
        self.assertEqual(max_drawdown([10, -4, -8, 3, -2]), -12.0)

    def test_full_and_fractional_attribution(self):
        trades = AnalyticsRepository(self.session).load_closed_trades(AnalyticsFilters(symbol="ETHUSDT"))
        full = full_attribution_rows(trades)
        fractional = fractional_attribution_rows(trades)
        self.assertEqual(len(full), 2)
        self.assertEqual(len(fractional), 2)
        self.assertAlmostEqual(sum(r["attributed_pnl_usdt"] for r in fractional), 10.0)
        self.assertTrue(all(r["attributed_pnl_usdt"] == 5.0 for r in fractional))

    def test_family_normalization_and_reliability(self):
        self.assertEqual(normalize_families("orderflow, trend,trend"), "orderflow,trend")
        self.assertEqual(normalize_families("trend,orderflow"), "orderflow,trend")
        self.assertEqual(reliability_status(8), "insufficient")
        self.assertEqual(reliability_status(25), "preliminary")
        self.assertEqual(reliability_status(80), "usable")
        self.assertEqual(reliability_status(220), "strong")

    def test_filters_and_report_sections_support_null_legacy_rows(self):
        engine = AnalyticsEngine(AnalyticsRepository(self.session))
        report = engine.build_report(AnalyticsFilters(symbol="ETHUSDT"), min_sample=1)
        self.assertEqual(report["strategy"]["sample_size"], 1)
        self.assertIn("direction", report["breakdowns"])
        self.assertEqual(report["data_quality"]["legacy_partial_rows"], 0)

        legacy_report = engine.build_report(AnalyticsFilters(symbol="XRPUSDT"), min_sample=1)
        self.assertEqual(legacy_report["strategy"]["sample_size"], 1)
        self.assertEqual(legacy_report["data_quality"]["missing_entry_snapshot"], 1)

    def test_confirmation_combinations(self):
        report = AnalyticsEngine(AnalyticsRepository(self.session)).build_report(AnalyticsFilters(), min_sample=1)
        self.assertIn("orderflow,trend", report["confirmation_combinations"])

    def test_cli_json_and_csv_serialization(self):
        report = AnalyticsEngine(AnalyticsRepository(self.session)).build_report(AnalyticsFilters(), min_sample=1)
        json_text = json.dumps(_json_safe(report), sort_keys=True)
        self.assertIn("current_live_openai_calls_per_hour", json_text)

        csv_text = _report_csv(report)
        rows = list(csv.DictReader(StringIO(csv_text)))
        self.assertTrue(any(row["section"] == "strategy" for row in rows))

    def test_empty_database_does_not_crash(self):
        empty = SessionBackedDb()
        session = empty.get_session()
        try:
            report = AnalyticsEngine(AnalyticsRepository(session)).build_report(AnalyticsFilters())
            self.assertEqual(report["strategy"]["sample_size"], 0)
            self.assertEqual(report["data_quality"]["closed_trades"], 0)
        finally:
            session.close()

    def test_openai_audit_has_no_live_calls(self):
        audit = openai_audit()
        self.assertEqual(audit["current_live_openai_calls_per_hour"], 0)
        self.assertFalse(audit["openai_strategy_class_used_by_live_engine"])

    def _seed(self):
        rows = [
            self._trade("t1", "ETHUSDT", "open_long", 10, 20, "TREND", "UP", "take_profit", "trend, orderflow", 0),
            self._trade("t2", "SOLUSDT", "open_short", -5, -10, "RANGE", "DOWN", "stop_loss", "trend", 1),
            self._trade("t3", "BNBUSDT", "open_long", 0, 0, "TREND", "UP", "manual", "mean_reversion", 2),
            self._trade("legacy", "XRPUSDT", "open_long", 0, None, None, None, None, None, 3),
            self._trade("open1", "ADAUSDT", "open_long", 99, 50, "TREND", "UP", None, "trend", 4, status="open"),
        ]
        self.session.add_all(rows)
        self.session.flush()
        self.session.add_all([
            self._vote(rows[0], "expert:ema", "trend", True, 0.7),
            self._vote(rows[0], "expert:momentum", "orderflow", True, 0.65),
            self._vote(rows[1], "expert:ema", "trend", True, 0.6),
            self._vote(rows[2], "expert:rsi", "mean_reversion", False, 0.55),
        ])
        self.session.commit()

    def _trade(
        self, order_id, symbol, action, pnl, pnl_pct, regime, trend,
        exit_type, families, index, status="closed",
    ):
        opened = self.now - timedelta(hours=4 - index)
        closed = opened + timedelta(minutes=30 + index)
        return TradeLog(
            symbol=symbol,
            action=action,
            source="decision:test",
            reason="test",
            order_link_id=order_id,
            entry_price=100,
            exit_price=101,
            size_usdt=100,
            leverage=1,
            pnl_usdt=pnl,
            pnl_pct=pnl_pct,
            holding_seconds=1800 + index * 60,
            status=status,
            opened_at=opened,
            closed_at=closed if status == "closed" else None,
            regime=regime,
            trend=trend,
            exit_type=exit_type,
            confirmation_families=families,
            confirmation_count=len(families.split(",")) if families else None,
            decision_confidence=0.7,
            expected_rr=2.0,
            entry_snapshot={
                "basic": {"primary_interval": "15"},
                "market_context": {
                    "regime": regime,
                    "trend": trend,
                    "volatility_state": "NORMAL",
                    "liquidity_state": "GOOD",
                    "volume_state": "NORMAL",
                    "funding_state": "NEUTRAL",
                    "open_interest_state": "RISING",
                },
            } if regime else None,
            exit_snapshot={"regime": regime} if regime else None,
        )

    @staticmethod
    def _vote(trade, source, family, contributed, confidence):
        return TradeExpertVote(
            trade_log_id=trade.id,
            order_link_id=trade.order_link_id,
            symbol=trade.symbol,
            source=source,
            family=family,
            action=trade.action,
            confidence=confidence,
            reason="test vote",
            contributed_to_final_decision=contributed,
        )


if __name__ == "__main__":
    unittest.main()
