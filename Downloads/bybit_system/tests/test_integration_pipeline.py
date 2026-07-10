import math
import sys
import time
import unittest
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import BybitConfig
from decision_engine import DecisionEngine
from execution.execution_engine import ExecutionEngine
from market_context import MarketContext, MarketContextEngine
from meta_strategy import MetaStrategyDecision, MetaStrategyManager
from portfolio_risk import PortfolioRiskEngine
from risk.risk_manager import RiskManager
from storage.journal import TradeJournal
from storage.models import Base, TradeLog
from strategy.engine import StrategyEngine
from strategy.signal import Action, Signal


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


class FakeExecution:
    def __init__(self):
        self.positions = []
        self.opened = []
        self.closed_pnl = {}
        self.trailing = []
        self.closed_orders = []

    def get_account_balance_usdt(self):
        return 1000.0

    def get_open_positions(self):
        return list(self.positions)

    def open_position(self, **kwargs):
        self.opened.append(kwargs)
        side = "Buy" if kwargs["action"] == Action.OPEN_LONG else "Sell"
        self.positions.append({
            "symbol": kwargs["symbol"],
            "size": "1",
            "side": side,
            "avgPrice": str(kwargs["last_price"]),
            "markPrice": str(kwargs["last_price"]),
        })
        return {
            "retCode": 0,
            "retMsg": "OK",
            "local_order_link_id": f"local-{kwargs['symbol']}",
        }

    def close_position(self, symbol, side_to_close, qty, source="unknown"):
        self.closed_orders.append((symbol, side_to_close, qty, source))
        self.positions = [p for p in self.positions if p["symbol"] != symbol]
        return {"retCode": 0, "retMsg": "OK", "local_order_link_id": f"close-{symbol}"}

    def set_trailing_stop(self, symbol, mark_price, distance_pct):
        self.trailing.append((symbol, mark_price, distance_pct))
        return {"retCode": 0}

    def get_closed_pnl(self, symbol, limit=50):
        return self.closed_pnl.get(symbol, [])


class FakeJournal:
    def __init__(self):
        self.entries = []
        self.open = []
        self.exits = []

    def log_entry(self, **kwargs):
        if not kwargs["order_link_id"]:
            return False
        self.entries.append(kwargs)
        self.open.append({
            "order_link_id": kwargs["order_link_id"],
            "symbol": kwargs["symbol"],
            "action": kwargs["action"].value,
            "entry_price": kwargs["entry_price"],
            "opened_at_ms": int(time.time() * 1000) - 1000,
        })
        return True

    def get_open_trades(self, symbol=None):
        return [t for t in self.open if symbol is None or t["symbol"] == symbol]

    def log_exit(self, order_link_id, exit_price, pnl_usdt, exit_reason="manual/unknown", **kwargs):
        for trade in list(self.open):
            if trade["order_link_id"] == order_link_id:
                self.open.remove(trade)
                self.exits.append((order_link_id, exit_price, pnl_usdt, exit_reason))
                return True
        return False


class FixedTrendExperts:
    def collect(self, symbol, candles_df, funding_rate, market_snapshot):
        return [
            Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:ema",
                confidence=0.70,
                reason="synthetic trend state",
                stop_loss_pct=1.5,
                take_profit_pct=3.2,
            ),
            Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:vwap",
                confidence=0.64,
                reason="synthetic price-location confirmation",
                stop_loss_pct=1.4,
                take_profit_pct=3.0,
            ),
        ]


class RankedExperts:
    def collect(self, symbol, candles_df, funding_rate, market_snapshot):
        base = {
            "ETHUSDT": 0.62,
            "SOLUSDT": 0.76,
            "BNBUSDT": 0.70,
        }[symbol]
        return [
            Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:ema",
                confidence=base,
                reason=f"ranked trend state {base}",
                stop_loss_pct=1.5,
                take_profit_pct=3.2,
            ),
            Signal(
                symbol=symbol,
                action=Action.OPEN_LONG,
                source="expert:vwap",
                confidence=base - 0.04,
                reason=f"ranked vwap confirmation {base}",
                stop_loss_pct=1.4,
                take_profit_pct=3.0,
            ),
        ]


class IntegrationPipelineTest(unittest.TestCase):
    def test_decision_requires_independent_confirmation_for_open(self):
        ctx = MarketContext(
            symbol="ETHUSDT",
            regime="TREND",
            trend="UP",
            liquidity="GOOD",
            confidence=0.75,
            risk_score=0.20,
        )
        meta = MetaStrategyDecision(
            allowed_sources={"expert:ema", "expert:vwap", "expert:momentum"}
        )
        decision_engine = DecisionEngine(min_confirming_families=2)

        single = decision_engine.decide(
            "ETHUSDT",
            ctx,
            meta,
            [Signal(
                "ETHUSDT", Action.OPEN_LONG, "expert:ema", 0.80,
                "single trend expert", stop_loss_pct=1.5, take_profit_pct=3.0,
            )],
        )
        self.assertEqual(single.final_signal.action, Action.HOLD)
        self.assertIn("Недостаточно независимых подтверждений", single.final_signal.reason)

        confirmed = decision_engine.decide(
            "ETHUSDT",
            ctx,
            meta,
            [
                Signal(
                    "ETHUSDT", Action.OPEN_LONG, "expert:ema", 0.80,
                    "trend expert", stop_loss_pct=1.5, take_profit_pct=3.0,
                ),
                Signal(
                    "ETHUSDT", Action.OPEN_LONG, "expert:vwap", 0.64,
                    "price-location confirmation", stop_loss_pct=1.4, take_profit_pct=3.0,
                ),
            ],
        )
        self.assertEqual(confirmed.final_signal.action, Action.OPEN_LONG)
        self.assertEqual(confirmed.confirmation_count, 2)
        self.assertEqual(confirmed.confirmation_families, ["trend", "price_location"])

    def test_journal_requires_order_link_id_and_exit_is_idempotent(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)

        self.assertFalse(journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "missing id",
            100, 50, 1, 1.5, 3.0, "",
        ))
        self.assertTrue(journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100, 50, 1, 1.5, 3.0, "oid-1",
        ))
        self.assertTrue(journal.log_exit("oid-1", 103, 1.5))
        self.assertFalse(journal.log_exit("oid-1", 103, 1.5))

        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-1").one()
            self.assertEqual(row.status, "closed")
            self.assertEqual(float(row.pnl_usdt), 1.5)
        finally:
            session.close()

    def test_execution_returns_local_order_link_id(self):
        class FakeSession:
            def set_leverage(self, **kwargs):
                return {"retCode": 0}

            def get_instruments_info(self, **kwargs):
                return {"result": {"list": [{
                    "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}
                }]}}

            def place_order(self, **kwargs):
                self.last_order_link_id = kwargs["orderLinkId"]
                return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "exchange-order"}}

        execution = object.__new__(ExecutionEngine)
        execution.cfg = BybitConfig(api_key="x", api_secret="y")
        execution.session = FakeSession()
        execution._lot_size_cache = {}

        resp = execution.open_position(
            "ETHUSDT", Action.OPEN_LONG, 100, 1, 100, 1.5, 3.0, "decision:ema",
        )
        self.assertTrue(resp["local_order_link_id"].startswith("decision_e-"))

        close_resp = execution.close_position("ETHUSDT", "Buy", 0.01, "exit:manager")
        self.assertTrue(close_resp["local_order_link_id"].startswith("exit_manag-close-"))

    def test_full_signal_to_sync_path_counts_pnl_once(self):
        engine, now = self._build_strategy_engine()

        changed = engine._process_symbol("ETHUSDT", 1000.0, [])
        self.assertTrue(changed)
        self.assertEqual(len(engine.execution.opened), 1)
        self.assertEqual(len(engine.journal.entries), 1)
        self.assertEqual(engine.journal.entries[0]["order_link_id"], "local-ETHUSDT")

        changed_again = engine._process_symbol("ETHUSDT", 1000.0, engine.execution.get_open_positions())
        self.assertFalse(changed_again)
        self.assertEqual(len(engine.execution.opened), 1)

        entry_price = engine.journal.open[0]["entry_price"]
        engine.execution.positions = []
        engine.execution.closed_pnl["ETHUSDT"] = [{
            "avgEntryPrice": str(entry_price),
            "createdTime": str(now + 1000),
            "avgExitPrice": "105",
            "closedPnl": "2.5",
        }]

        engine._sync_closed_trades([])
        self.assertEqual(engine.journal.open, [])
        self.assertEqual(engine.journal.exits, [("local-ETHUSDT", 105.0, 2.5, "manual/unknown")])
        self.assertEqual(engine.risk_manager._daily_pnl_usdt, 2.5)

        engine._sync_closed_trades([])
        self.assertEqual(engine.risk_manager._daily_pnl_usdt, 2.5)

    def test_sync_warns_when_journal_open_but_live_position_missing_and_no_closed_pnl_match(self):
        engine, _ = self._build_strategy_engine()
        engine.journal.open.append({
            "order_link_id": "lost-1",
            "symbol": "ETHUSDT",
            "action": Action.OPEN_LONG.value,
            "entry_price": 100.0,
            "opened_at_ms": int(time.time() * 1000) - 1000,
        })
        engine.execution.closed_pnl["ETHUSDT"] = []

        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            engine._sync_closed_trades([])

        self.assertTrue(any("live-позиции нет" in msg and "closed_pnl не найден" in msg for msg in logs.output))
        self.assertEqual(len(engine.journal.open), 1)
        self.assertEqual(engine.risk_manager._daily_pnl_usdt, 0.0)

    def test_strategy_loop_continues_after_one_symbol_exception(self):
        engine, _ = self._build_strategy_engine()
        processed = []

        def process(symbol, balance, positions, execute=True):
            processed.append(symbol)
            if symbol == "BADUSDT":
                raise RuntimeError("synthetic symbol failure")
            return False

        engine.cfg.symbols = ["BADUSDT", "SOLUSDT"]
        engine._process_symbol = process
        engine.execution.get_account_balance_usdt = lambda: 1000.0
        engine.execution.get_open_positions = lambda: []
        engine._manage_trailing_stops = lambda positions: None
        engine._sync_closed_trades = lambda positions=None: None

        with self.assertLogs("strategy.engine", level="ERROR") as logs:
            engine.run_once()
        self.assertEqual(processed, ["BADUSDT", "SOLUSDT"])
        self.assertTrue(any("Ошибка обработки символа BADUSDT" in msg for msg in logs.output))

    def test_strategy_ranks_candidates_before_execution_and_caps_new_entries(self):
        engine, _ = self._build_strategy_engine()
        engine.cfg.symbols = ["ETHUSDT", "SOLUSDT", "BNBUSDT"]
        engine.cfg.max_new_positions_per_cycle = 2
        engine.cfg.min_seconds_between_entries = 0
        engine.cfg.max_same_direction_per_group = 3
        engine.experts = RankedExperts()

        engine.run_once()

        opened_symbols = [order["symbol"] for order in engine.execution.opened]
        self.assertEqual(opened_symbols, ["SOLUSDT", "BNBUSDT"])
        self.assertEqual(len(engine.journal.entries), 2)

    def test_strategy_spacing_gate_blocks_second_ranked_entry_in_same_burst(self):
        engine, _ = self._build_strategy_engine()
        engine.cfg.symbols = ["ETHUSDT", "SOLUSDT", "BNBUSDT"]
        engine.cfg.max_new_positions_per_cycle = 3
        engine.cfg.min_seconds_between_entries = 60
        engine.cfg.max_same_direction_per_group = 3
        engine.experts = RankedExperts()

        engine.run_once()

        opened_symbols = [order["symbol"] for order in engine.execution.opened]
        self.assertEqual(opened_symbols, ["SOLUSDT"])

    def _build_strategy_engine(self):
        now = int(time.time() * 1000)
        candles = self._trend_candles(now)
        cfg = BybitConfig(api_key="x", api_secret="y")
        cfg.symbols = ["ETHUSDT"]
        cfg.trend_filter_enabled = True
        cfg.min_open_confidence = 0.45
        cfg.max_open_positions = 5
        cfg.max_new_positions_per_cycle = 2
        cfg.min_seconds_between_entries = 0
        cfg.min_confirming_families = 2
        cfg.max_candle_age_minutes = 60

        engine = object.__new__(StrategyEngine)
        engine.cfg = cfg
        engine.db = None
        engine.rule_strategy = None
        engine.experts = FixedTrendExperts()
        engine.market_context_engine = MarketContextEngine()
        engine.meta_strategy = MetaStrategyManager()
        engine.decision_engine = DecisionEngine(
            cfg.min_open_confidence,
            cfg.min_decision_margin,
            cfg.min_rr,
            cfg.default_stop_loss_pct,
            cfg.default_take_profit_rr,
            cfg.min_confirming_families,
        )
        engine.portfolio_risk = PortfolioRiskEngine(cfg)
        engine.ai_market_analyst = type(
            "AI",
            (),
            {"analyze": lambda self, symbol, snapshot, context: type("A", (), {"conclusion": "synthetic"})()},
        )()
        engine.risk_manager = RiskManager(cfg)
        engine.execution = FakeExecution()
        engine.journal = FakeJournal()
        engine._trailing_activated = set()
        engine._pending_exit_reasons = {}

        engine._load_recent_candles = lambda symbol, limit=210: candles.tail(limit).copy()
        engine._load_latest_funding = lambda symbol: {"rate": 0.0, "ts": now}
        engine._load_funding_trend = lambda symbol, limit=8: {
            "recent_values": [0.0] * 8, "trend": "стабилен", "latest_ts": now,
        }
        engine._load_oi_trend = lambda symbol, limit=20: {
            "current": 1000.0, "change_pct": 1.5, "latest_ts": now,
        }
        engine._load_latest_orderbook = lambda symbol: {
            "ts": now, "spread_pct": 0.02, "bid_ask_imbalance": 0.1,
        }
        engine._load_trade_flow = lambda symbol, minutes=15: {
            "buy_volume": 100, "sell_volume": 80, "imbalance": 0.11,
            "window_minutes": minutes, "latest_ts": now,
        }
        engine._load_recent_liquidations = lambda symbol, minutes=60: {
            "count": 0, "total_volume": 0, "window_minutes": minutes,
        }
        return engine, now

    @staticmethod
    def _trend_candles(now):
        rows = []
        for i in range(220):
            price = 100 + i * 0.04 + math.sin(i / 3) * 0.4
            rows.append({
                "start_time": now - (219 - i) * 15 * 60_000,
                "open": price - 0.1,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 1000 + (i % 10),
            })
        return pd.DataFrame(rows)


if __name__ == "__main__":
    unittest.main()
