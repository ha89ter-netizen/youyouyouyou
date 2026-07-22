"""
Тесты двух блокеров перед 24-часовым прогоном:
1) TRADING_ENABLED=false — централизованный safe mode: ни один мутирующий
   запрос не уходит на биржу, чтение и анализ продолжаются;
2) risk_admin reconcile-open — строгий матчер зависших open-сделок:
   dry-run ничего не меняет, одна запись closed PnL не используется дважды,
   неоднозначность не применяется, повторный --apply не удваивает PnL.

Реальных обращений к бирже нет: session/execution везде подменены.
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
from execution.execution_engine import ExecutionEngine, SAFE_MODE_RET_CODE
from risk.risk_manager import RiskManager
from risk_admin import (
    AMBIGUOUS,
    MATCHED,
    NOT_FOUND,
    apply_reconciliation_plan,
    plan_open_reconciliation,
)
from storage.journal import TradeJournal
from storage.models import Base, TradeLog
from strategy.engine import StrategyEngine
from strategy.signal import Action, Signal
from timeutils import utc_today


class SessionBackedDb:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def _cfg(trading_enabled: bool) -> BybitConfig:
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.symbols = ["ETHUSDT"]
    cfg.trading_enabled = trading_enabled
    return cfg


class ExplodingSession:
    """Любой мутирующий вызов — провал теста; чтение отвечает данными."""

    def place_order(self, **kwargs):
        raise AssertionError("SAFE MODE ПРОБИТ: place_order ушёл на биржу")

    def set_trading_stop(self, **kwargs):
        raise AssertionError("SAFE MODE ПРОБИТ: set_trading_stop ушёл на биржу")

    def set_leverage(self, **kwargs):
        raise AssertionError("SAFE MODE ПРОБИТ: set_leverage ушёл на биржу")

    def get_positions(self, **kwargs):
        return {"result": {"list": [{"symbol": "ETHUSDT", "size": "1", "side": "Buy"}]}}

    def get_wallet_balance(self, **kwargs):
        return {"result": {"list": [{"coin": [{"coin": "USDT", "walletBalance": "1000"}]}]}}

    def get_closed_pnl(self, **kwargs):
        return {"result": {"list": [{"closedPnl": "1.5"}]}}


def _safe_execution() -> ExecutionEngine:
    execution = object.__new__(ExecutionEngine)
    execution.cfg = _cfg(trading_enabled=False)
    execution.session = ExplodingSession()
    execution._lot_size_cache = {}
    return execution


# ======================================================================
# 1. Централизованный guard: все мутирующие операции заблокированы
# ======================================================================

class SafeModeGuardTest(unittest.TestCase):
    def setUp(self):
        self.execution = _safe_execution()

    def test_open_position_blocked(self):
        with self.assertLogs("execution.execution_engine", level="WARNING") as logs:
            resp = self.execution.open_position(
                "ETHUSDT", Action.OPEN_LONG, 100, 1, 100, 1.5, 3.0, "test",
            )
        self.assertEqual(resp["retCode"], SAFE_MODE_RET_CODE)
        self.assertTrue(resp["safe_mode_blocked"])
        self.assertNotEqual(resp["retCode"], 0)  # движок не примет за успех
        self.assertTrue(any("SAFE MODE" in m for m in logs.output))

    def test_close_position_blocked(self):
        resp = self.execution.close_position("ETHUSDT", "Buy", 1.0, "exit_manager")
        self.assertEqual(resp["retCode"], SAFE_MODE_RET_CODE)

    def test_set_trailing_stop_blocked(self):
        resp = self.execution.set_trailing_stop("ETHUSDT", 100.0, 0.8)
        self.assertEqual(resp["retCode"], SAFE_MODE_RET_CODE)

    def test_set_leverage_blocked(self):
        self.assertIsNone(self.execution.set_leverage("ETHUSDT", 3))  # и не взорвался

    def test_reads_still_allowed(self):
        """Чтение баланса, позиций и closed PnL в safe mode работает."""
        self.assertEqual(self.execution.get_account_balance_usdt(), 1000.0)
        positions = self.execution.get_open_positions()
        self.assertEqual(positions[0]["symbol"], "ETHUSDT")
        self.assertEqual(self.execution.get_closed_pnl("ETHUSDT")[0]["closedPnl"], "1.5")

    def test_trading_enabled_true_does_not_block(self):
        execution = _safe_execution()
        execution.cfg = _cfg(trading_enabled=True)
        # Guard пропускает — и вызов честно доходит до session (взрывается)
        with self.assertRaises(AssertionError):
            execution.close_position("ETHUSDT", "Buy", 1.0)


# ======================================================================
# 2. Движок в safe mode: решения логируются, действия не выполняются
# ======================================================================

class _RecordingExecution:
    def __init__(self):
        self.trailing = []
        self.closed_orders = []

    def set_trailing_stop(self, symbol, price, distance):
        self.trailing.append(symbol)

    def close_position(self, symbol, side, size, source="unknown"):
        self.closed_orders.append(symbol)
        return {"retCode": 0}


def _bare_engine(cfg) -> StrategyEngine:
    engine = object.__new__(StrategyEngine)
    engine.cfg = cfg
    engine.execution = _RecordingExecution()
    engine.risk_manager = RiskManager(cfg)
    engine._pending_exit_reasons = {}
    engine._orphan_attempts = {}
    return engine


class EngineSafeModeTest(unittest.TestCase):
    PROFITABLE_LONG = {
        "symbol": "ETHUSDT", "size": "1", "side": "Buy",
        "avgPrice": "100", "markPrice": "105", "trailingStop": "0",
    }

    def test_trailing_stop_not_sent_but_decision_logged(self):
        engine = _bare_engine(_cfg(trading_enabled=False))
        engine.cfg.trailing_stop_enabled = True
        with self.assertLogs("strategy.engine", level="INFO") as logs:
            engine._manage_trailing_stops([dict(self.PROFITABLE_LONG)])
        self.assertEqual(engine.execution.trailing, [])
        self.assertTrue(any("SAFE MODE" in m and "trailing" in m for m in logs.output))

    def test_exit_manager_does_not_close_but_decision_logged(self):
        engine = _bare_engine(_cfg(trading_enabled=False))
        reversal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.8, "разворот")
        with self.assertLogs("strategy.engine", level="INFO") as logs:
            changed = engine._manage_exit("ETHUSDT", dict(self.PROFITABLE_LONG), reversal, "short")
        self.assertFalse(changed)
        self.assertEqual(engine.execution.closed_orders, [])
        self.assertEqual(engine._pending_exit_reasons, {})  # причина не взводится
        self.assertTrue(any("SAFE MODE" in m and "НЕ закрыта" in m for m in logs.output))

    def test_enabled_mode_still_closes(self):
        engine = _bare_engine(_cfg(trading_enabled=True))
        reversal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.8, "разворот")
        changed = engine._manage_exit("ETHUSDT", dict(self.PROFITABLE_LONG), reversal, "short")
        self.assertTrue(changed)
        self.assertEqual(engine.execution.closed_orders, ["ETHUSDT"])


class _RejectingExecution(_RecordingExecution):
    """close_position, который биржа отклоняет (retCode != 0)."""

    def close_position(self, symbol, side, size, source="unknown"):
        self.closed_orders.append(symbol)
        return {"retCode": 30208, "retMsg": "reduce-only order failed"}


class ExitManagerRejectedCloseTest(unittest.TestCase):
    """
    Регрессия: _manage_exit раньше игнорировал retCode ответа close_position
    и считал закрытие успешным, даже если биржа его отклонила. Из-за этого
    _pending_exit_reasons получал "exit manager" для позиции, которая на самом
    деле осталась открытой — и следующее реальное закрытие (например, по SL)
    записало бы в журнал неверную причину.
    """

    def test_rejected_close_is_not_treated_as_success(self):
        engine = _bare_engine(_cfg(trading_enabled=True))
        engine.execution = _RejectingExecution()
        reversal = Signal("ETHUSDT", Action.OPEN_SHORT, "decision:test", 0.8, "разворот")
        position = {
            "symbol": "ETHUSDT", "size": "1", "side": "Buy",
            "avgPrice": "100", "markPrice": "105", "trailingStop": "0",
        }

        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            changed = engine._manage_exit("ETHUSDT", position, reversal, "short")

        self.assertFalse(changed)
        # Ордер отправлен (биржа его увидела), но отклонён -- pending reason
        # не должен взводиться, иначе он приклеится к следующему, никак не
        # связанному закрытию этой позиции.
        self.assertEqual(engine._pending_exit_reasons, {})
        self.assertTrue(any("отклонено биржей" in m for m in logs.output))


# ======================================================================
# 3. Строгий матчер reconcile-open
# ======================================================================

def _trade(oid, entry=100.0, action="open_long", opened_ms=1_000_000, size=50.0, symbol="ETHUSDT"):
    return {
        "order_link_id": oid, "symbol": symbol, "action": action,
        "entry_price": entry, "size_usdt": size, "opened_at_ms": opened_ms,
        "status": "open",
    }


def _record(entry="100.0", pnl="2.0", side="Sell", created="2000000", qty="0.5", exit_price="104"):
    # qty=0.5 * entry=100 -> номинал 50 USDT, совпадает с size_usdt сделок
    return {
        "symbol": "ETHUSDT", "avgEntryPrice": entry, "side": side,
        "createdTime": created, "updatedTime": created,
        "avgExitPrice": exit_price, "closedPnl": pnl, "qty": qty,
    }


class ReconcilePlanTest(unittest.TestCase):
    def test_unique_match(self):
        plan = plan_open_reconciliation([_trade("a")], [_record()])
        self.assertEqual(plan[0]["status"], MATCHED)

    def test_one_record_two_trades_is_ambiguous_for_both(self):
        """Одна запись closed PnL никогда не достаётся двум сделкам."""
        trades = [_trade("a", entry=100.0), _trade("b", entry=100.05)]
        plan = plan_open_reconciliation(trades, [_record()])
        self.assertEqual([p["status"] for p in plan], [AMBIGUOUS, AMBIGUOUS])

    def test_two_records_one_trade_is_ambiguous(self):
        plan = plan_open_reconciliation(
            [_trade("a")], [_record(pnl="1"), _record(pnl="2", created="2500000")],
        )
        self.assertEqual(plan[0]["status"], AMBIGUOUS)

    def test_contested_record_blocks_even_single_candidate_trade(self):
        """
        У сделки a один кандидат, но он же — кандидат сделки b: обе AMBIGUOUS.
        Запись не может быть использована дважды даже частично.
        """
        rec_shared = _record(pnl="1")
        rec_b_only = _record(entry="100.3", pnl="2", created="2500000")
        trades = [_trade("a", entry=100.0), _trade("b", entry=100.2)]
        plan = plan_open_reconciliation(trades, [rec_shared, rec_b_only])
        by_oid = {p["trade"]["order_link_id"]: p["status"] for p in plan}
        self.assertEqual(by_oid["a"], AMBIGUOUS)

    def test_distinct_matches_use_distinct_records(self):
        trades = [_trade("a", entry=100.0), _trade("b", entry=200.0)]
        records = [_record(entry="100.0", pnl="1"), _record(entry="200.0", pnl="2", qty="0.25")]
        plan = plan_open_reconciliation(trades, records)
        matched = {p["trade"]["order_link_id"]: p["record"] for p in plan if p["status"] == MATCHED}
        self.assertEqual(len(matched), 2)
        self.assertIsNot(matched["a"], matched["b"])

    def test_direction_time_and_size_filters(self):
        # Направление: закрытие шорта (Buy) не подходит лонгу
        self.assertEqual(
            plan_open_reconciliation([_trade("a")], [_record(side="Buy")])[0]["status"],
            NOT_FOUND,
        )
        # Время: закрытие раньше открытия
        self.assertEqual(
            plan_open_reconciliation([_trade("a", opened_ms=3_000_000)], [_record()])[0]["status"],
            NOT_FOUND,
        )
        # Размер: номинал записи 500 USDT против size_usdt=50 — отсекается
        self.assertEqual(
            plan_open_reconciliation([_trade("a")], [_record(qty="5")])[0]["status"],
            NOT_FOUND,
        )

    def test_plan_is_pure_and_changes_nothing(self):
        trades = [_trade("a")]
        records = [_record()]
        before_t, before_r = [dict(t) for t in trades], [dict(r) for r in records]
        plan_open_reconciliation(trades, records)
        self.assertEqual(trades, before_t)
        self.assertEqual(records, before_r)


# ======================================================================
# 4. Dry-run и --apply на настоящем журнале
# ======================================================================

class ReconcileApplyTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionBackedDb()
        self.journal = TradeJournal(self.db)
        self.risk = RiskManager(_cfg(trading_enabled=False))
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.0, 50.0, 1, 1.5, 3.0, "stale-1",
        )

    def _open_trades(self):
        return self.journal.get_open_trades()

    def _fresh_record(self, pnl="-2.0"):
        now_ms = int(time.time() * 1000)
        return _record(pnl=pnl, created=str(now_ms), exit_price="96.0")

    def test_dry_run_changes_nothing_in_db(self):
        """План построен, но без apply журнал и дневной PnL нетронуты."""
        trades = self._open_trades()
        plan = plan_open_reconciliation(trades, [self._fresh_record()])
        self.assertEqual(plan[0]["status"], MATCHED)

        # dry-run = план есть, apply не вызывался
        self.assertEqual(len(self._open_trades()), 1)          # сделка всё ещё open
        self.assertEqual(self.risk._daily_pnl_usdt, 0.0)       # PnL не тронут
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "stale-1").one()
            self.assertEqual(row.status, "open")
            self.assertIsNone(row.pnl_usdt)
        finally:
            session.close()

    def test_apply_closes_and_counts_once(self):
        plan = plan_open_reconciliation(self._open_trades(), [self._fresh_record()])
        applied = apply_reconciliation_plan(plan, self.journal, self.risk)

        self.assertTrue(applied[0]["changed"])
        self.assertEqual(self._open_trades(), [])
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, -2.0)
        total, count = self.journal.sum_closed_pnl_for_utc_day(utc_today())
        self.assertAlmostEqual(total, -2.0)
        self.assertEqual(count, 1)

    def test_repeated_apply_does_not_double_pnl(self):
        plan = plan_open_reconciliation(self._open_trades(), [self._fresh_record()])
        apply_reconciliation_plan(plan, self.journal, self.risk)
        pnl_first = self.risk._daily_pnl_usdt

        # Тот же план применяем ещё дважды — log_exit вернёт recorded=False
        for _ in range(2):
            applied = apply_reconciliation_plan(plan, self.journal, self.risk)
            self.assertFalse(applied[0]["changed"])
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, pnl_first)

        # И повторная полная процедура: сделка больше не open — плана нет
        self.assertEqual(plan_open_reconciliation(self._open_trades(), [self._fresh_record()]), [])

    def test_ambiguous_is_never_applied_or_orphaned(self):
        self.journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100.02, 50.0, 1, 1.5, 3.0, "stale-2",
        )
        plan = plan_open_reconciliation(self._open_trades(), [self._fresh_record()])
        self.assertEqual({p["status"] for p in plan}, {AMBIGUOUS})

        applied = apply_reconciliation_plan(plan, self.journal, self.risk)
        self.assertEqual(applied, [])
        self.assertEqual(len(self._open_trades()), 2)           # обе остались open
        self.assertEqual(self.journal.count_orphaned(), 0)      # в orphaned не переведены
        self.assertEqual(self.risk._daily_pnl_usdt, 0.0)

    def test_old_closure_recorded_but_not_in_daily_limit(self):
        yesterday_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
        # Сделка должна быть старше закрытия
        session = self.db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "stale-1").one()
            row.opened_at = datetime.now(timezone.utc) - timedelta(days=2)
            session.commit()
        finally:
            session.close()

        plan = plan_open_reconciliation(
            self._open_trades(), [_record(pnl="-5.0", created=str(yesterday_ms), exit_price="96")],
        )
        applied = apply_reconciliation_plan(plan, self.journal, self.risk)
        self.assertTrue(applied[0]["changed"])
        self.assertFalse(applied[0]["counted_in_daily"])
        self.assertEqual(self.risk._daily_pnl_usdt, 0.0)        # вчерашний PnL не в сегодняшнем лимите
        self.assertEqual(self._open_trades(), [])               # но сделка закрыта


if __name__ == "__main__":
    unittest.main()
