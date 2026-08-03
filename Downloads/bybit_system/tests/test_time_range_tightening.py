import sys
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import BybitConfig
from storage.journal import TradeJournal
from storage.models import Base, TradeLog
from strategy.engine import StrategyEngine
from strategy.signal import Action
from timeutils import utcnow


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def get_session(self):
        return self.SessionLocal()


class RecordingExecution:
    def __init__(self):
        self.protection = []
        self.trailing = []

    def set_position_protection(self, symbol, side, mark, stop_loss, take_profit):
        self.protection.append((symbol, side, mark, stop_loss, take_profit))
        return {
            "retCode": 0,
            "local_stop_loss_price": stop_loss,
            "local_take_profit_price": take_profit,
        }

    def set_trailing_stop(self, symbol, mark_price, distance_pct):
        self.trailing.append((symbol, mark_price, distance_pct))
        return {"retCode": 0}


def _cfg(trading_enabled=True):
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.trading_enabled = trading_enabled
    cfg.time_range_tightening_enabled = True
    cfg.time_range_tightening_after_seconds = 3600
    cfg.time_range_tightening_factor = 0.5
    cfg.time_range_second_tightening_after_seconds = 18000
    cfg.time_range_second_tightening_factor = 0.5
    return cfg


class TimeRangeTighteningTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionBackedDb()
        self.journal = TradeJournal(self.db)
        self.execution = RecordingExecution()
        self.engine = object.__new__(StrategyEngine)
        self.engine.cfg = _cfg()
        self.engine.journal = self.journal
        self.engine.execution = self.execution

    def _entry(
        self, side, age_seconds=3601, sl=98.0, tp=104.0, oid="oid-1", run_id=None,
    ):
        action = Action.OPEN_LONG if side == "Buy" else Action.OPEN_SHORT
        self.assertTrue(self.journal.log_entry(
            "ETHUSDT", action, "test", "test", 100.0, 50.0, 1,
            2.0, 4.0, oid, stop_loss_price=sl, take_profit_price=tp, run_id=run_id,
        ))
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter_by(order_link_id=oid).one()
            row.opened_at = utcnow() - timedelta(seconds=age_seconds)
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _position(side, mark=100.0, sl=98.0, tp=104.0):
        return {
            "symbol": "ETHUSDT",
            "side": side,
            "size": "1",
            "markPrice": str(mark),
            "stopLoss": str(sl),
            "takeProfit": str(tp),
        }

    def test_long_halves_remaining_distances_once(self):
        self._entry("Buy")
        position = self._position("Buy")
        self.engine._manage_time_range_tightening([position])
        self.assertEqual(
            self.execution.protection[0],
            ("ETHUSDT", "Buy", 100.0, 99.0, 102.0),
        )

        # Повторный вызов и новый экземпляр engine читают durable-флаг из БД.
        restarted = object.__new__(StrategyEngine)
        restarted.cfg = _cfg()
        restarted.journal = TradeJournal(self.db)
        restarted.execution = RecordingExecution()
        restarted._manage_time_range_tightening([position])
        self.assertEqual(restarted.execution.protection, [])
        trade = restarted.journal.get_open_trades("ETHUSDT")[0]
        self.assertIsNotNone(trade["range_tightened_at"])
        self.assertEqual(trade["tightened_stop_loss_price"], 99.0)
        self.assertEqual(trade["tightened_take_profit_price"], 102.0)

    def test_short_halves_remaining_distances(self):
        self._entry("Sell", sl=104.0, tp=98.0)
        self.engine._manage_time_range_tightening([
            self._position("Sell", sl=104.0, tp=98.0)
        ])
        self.assertEqual(
            self.execution.protection[0],
            ("ETHUSDT", "Sell", 100.0, 102.0, 99.0),
        )

    def test_second_stage_halves_remaining_distances_after_five_hours_once(self):
        self._entry("Buy", age_seconds=18001)
        self.engine._manage_time_range_tightening([self._position("Buy")])
        self.engine._manage_time_range_tightening([
            self._position("Buy", sl=99.0, tp=102.0)
        ])
        self.assertEqual(
            self.execution.protection,
            [
                ("ETHUSDT", "Buy", 100.0, 99.0, 102.0),
                ("ETHUSDT", "Buy", 100.0, 99.5, 101.0),
            ],
        )

        restarted = object.__new__(StrategyEngine)
        restarted.cfg = _cfg()
        restarted.journal = TradeJournal(self.db)
        restarted.execution = RecordingExecution()
        restarted._manage_time_range_tightening([
            self._position("Buy", sl=99.5, tp=101.0)
        ])
        self.assertEqual(restarted.execution.protection, [])
        trade = restarted.journal.get_open_trades("ETHUSDT")[0]
        self.assertIsNotNone(trade["range_second_tightened_at"])
        self.assertEqual(trade["second_tightened_stop_loss_price"], 99.5)
        self.assertEqual(trade["second_tightened_take_profit_price"], 101.0)

    def test_second_stage_waits_until_five_hours(self):
        self._entry("Buy", age_seconds=4 * 3600)
        self.engine._manage_time_range_tightening([self._position("Buy")])
        self.engine._manage_time_range_tightening([
            self._position("Buy", sl=99.0, tp=102.0)
        ])
        self.assertEqual(len(self.execution.protection), 1)
        trade = self.journal.get_open_trades("ETHUSDT")[0]
        self.assertIsNone(trade["range_second_tightened_at"])

    def test_younger_position_is_untouched(self):
        self._entry("Buy", age_seconds=3599)
        self.engine._manage_time_range_tightening([self._position("Buy")])
        self.assertEqual(self.execution.protection, [])

    def test_missing_or_invalid_protection_is_untouched(self):
        self._entry("Buy")
        self.engine._manage_time_range_tightening([
            self._position("Buy", sl=0),
            self._position("Buy", sl=101),
        ])
        self.assertEqual(self.execution.protection, [])

    def test_safe_mode_does_not_mutate_exchange_or_journal(self):
        self._entry("Buy")
        self.engine.cfg.trading_enabled = False
        self.engine._manage_time_range_tightening([self._position("Buy")])
        self.assertEqual(self.execution.protection, [])
        self.assertIsNone(
            self.journal.get_open_trades("ETHUSDT")[0]["range_tightened_at"]
        )

    def test_inherited_trade_is_observed_but_never_mutated(self):
        self.engine.cfg.run_id = "new-run"
        self.engine.cfg.trailing_stop_enabled = True
        self.engine.cfg.trailing_activation_pct = 1.0
        self._entry("Buy", run_id="old-run")
        position = self._position("Buy")
        position["avgPrice"] = "95"
        position["trailingStop"] = "0"
        self.engine._manage_time_range_tightening([position])
        self.engine._manage_trailing_stops([position])
        self.assertEqual(self.execution.protection, [])
        self.assertEqual(self.execution.trailing, [])
        self.assertIsNone(self.journal.get_open_trades("ETHUSDT")[0]["range_tightened_at"])

    def test_already_tighter_exchange_state_recovers_flag_without_repeating(self):
        self._entry("Buy", sl=98.0, tp=104.0)
        self.engine._manage_time_range_tightening([
            self._position("Buy", sl=99.0, tp=102.0)
        ])
        self.assertEqual(self.execution.protection, [])
        trade = self.journal.get_open_trades("ETHUSDT")[0]
        self.assertIsNotNone(trade["range_tightened_at"])
        self.assertEqual(trade["tightened_stop_loss_price"], 99.0)
        self.assertEqual(trade["tightened_take_profit_price"], 102.0)


if __name__ == "__main__":
    unittest.main()
