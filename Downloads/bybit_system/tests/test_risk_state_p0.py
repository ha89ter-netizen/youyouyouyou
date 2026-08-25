"""
Тесты P0: устойчивость к перезапуску, защита от повторного входа,
достоверный учёт закрытых сделок и дневного PnL.

Реальных обращений к бирже здесь нет: execution везде подменён фейком.
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

from analytics.metrics import result_metrics
from config.settings import BybitConfig
from execution.execution_engine import ExecutionEngine, FillStatus, OrderConfirmation
from risk.risk_manager import DAILY_LOSS_CAUSE, RiskManager, orphan_cause
from storage.journal import ExitResult, TradeJournal
from timeutils import utcnow
from storage.models import Base, TradeLog
from storage.risk_state import RiskStateStore
from storage.durability import EntryIntentStore
from strategy.engine import _ORPHAN_MAX_ATTEMPTS, StrategyEngine
from strategy.signal import Action, Signal
from timeutils import utc_today


class SessionBackedDb:
    """SQLite in-memory: несколько сессий из одного engine видят одну БД."""

    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def _cfg(**overrides) -> BybitConfig:
    # Значения задаются явно: BybitConfig читает env на этапе импорта, и тесты
    # не должны зависеть от того, что экспортировано в шелле.
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.symbols = ["ETHUSDT"]
    cfg.trading_enabled = True
    cfg.max_daily_loss_pct = 3.0
    cfg.max_trades_per_symbol = 5
    cfg.max_daily_trades = 50
    cfg.cooldown_minutes = 5
    cfg.min_open_confidence = 0.45
    cfg.max_open_positions = 5
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _long_signal(symbol="ETHUSDT", confidence=0.80) -> Signal:
    return Signal(
        symbol=symbol, action=Action.OPEN_LONG, source="decision:test",
        confidence=confidence, reason="p0 test", stop_loss_pct=1.5, take_profit_pct=3.0,
    )


class FakeExecution:
    def __init__(self):
        self.positions = []
        self.opened = []
        self.closed_pnl = {}
        self.executions = {}
        self.confirmation = OrderConfirmation(status=FillStatus.FILLED, filled_qty=1.0)
        self.since_calls = []

    def get_account_balance_usdt(self):
        return 1000.0

    def get_open_positions(self):
        return list(self.positions)

    def open_position(self, **kwargs):
        self.opened.append(kwargs)
        return {"retCode": 0, "retMsg": "OK", "local_order_link_id": f"local-{kwargs['symbol']}"}

    def confirm_order(self, symbol, order_link_id, attempts=3, delay_seconds=0.6):
        return self.confirmation

    def get_closed_pnl(self, symbol, limit=50):
        return self.closed_pnl.get(symbol, [])

    def get_closed_pnl_since(self, symbol, start_time_ms=None, max_pages=5):
        self.since_calls.append((symbol, start_time_ms))
        return self.closed_pnl.get(symbol, [])

    def get_executions(self, symbol, order_link_id=None, start_time_ms=None, max_pages=5):
        # По умолчанию пусто -> exit_reason падает в fallback "manual/unknown",
        # как и раньше. Тесты, которым нужна конкретная причина, кладут сюда
        # свои executions через self.executions[symbol].
        return self.executions.get(symbol, [])


class FakeJournal:
    def __init__(self):
        self.open = []
        self.entries = []
        self.orphaned = []
        self.closed = []
        self.exit_triggers = {}
        self.log_entry_ok = True

    def record_exit_trigger(self, order_link_id, trigger):
        self.exit_triggers[order_link_id] = trigger
        return True

    def log_entry(self, **kwargs):
        if not self.log_entry_ok:
            return False
        self.entries.append(kwargs)
        self.open.append({
            "order_link_id": kwargs["order_link_id"],
            "symbol": kwargs["symbol"],
            "action": kwargs["action"].value,
            "entry_price": kwargs["entry_price"],
            "opened_at_ms": int(time.time() * 1000) - 5000,
            "status": "open",
        })
        return True

    def get_open_trades(self, symbol=None):
        return [t for t in self.open if symbol is None or t["symbol"] == symbol]

    def log_exit(self, order_link_id, exit_price, pnl_usdt, closed_at=None, **kwargs):
        for trade in list(self.open):
            if trade["order_link_id"] == order_link_id:
                recovered = trade.get("status") == "orphaned"
                self.open.remove(trade)
                self.closed.append(order_link_id)
                return ExitResult(
                    recorded=True, recovered_from_orphan=recovered,
                    closed_at=closed_at or utcnow(), symbol=trade["symbol"],
                )
        if order_link_id in self.closed:
            return ExitResult(recorded=False, already_closed=True, reason="уже закрыта")
        return ExitResult(recorded=False, reason="не найдена")

    def get_unresolved_trades(self, symbol=None):
        return [t for t in self.open if symbol is None or t["symbol"] == symbol]

    def get_orphaned_trades(self, symbol=None):
        return [
            t for t in self.open
            if t.get("status") == "orphaned" and (symbol is None or t["symbol"] == symbol)
        ]

    def mark_orphaned(self, order_link_id, reason):
        for trade in self.open:
            if trade["order_link_id"] == order_link_id and trade.get("status", "open") == "open":
                trade["status"] = "orphaned"
                self.orphaned.append((order_link_id, reason))
                return True
        return False

    def count_orphaned(self):
        return len([t for t in self.open if t.get("status") == "orphaned"])

    def sum_closed_pnl_for_utc_day(self, day=None):
        return 0.0, 0


def _bare_engine(cfg, execution, journal, risk_manager) -> StrategyEngine:
    """Движок без __init__: собираем только то, что нужно проверяемым методам."""
    engine = object.__new__(StrategyEngine)
    engine.cfg = cfg
    engine.execution = execution
    engine.journal = journal
    engine.risk_manager = risk_manager
    intent_db = SessionBackedDb()
    cfg.run_id = cfg.run_id or "unit-entry-intent"
    engine.entry_intents = EntryIntentStore(intent_db, cfg)
    engine.telemetry = type("Telemetry", (), {
        "current_policy": lambda self: (0, "unit-config"),
        "record_protection_event": lambda self, *args, **kwargs: True,
        "finalize_trade": lambda self, *args, **kwargs: True,
    })()
    engine._protection_entry_halt = None
    engine._orphan_attempts = {}
    engine._last_entry_ts = None
    return engine


# ======================================================================
# 1. Дневной лимит переживает перезапуск процесса
# ======================================================================

class DailyLimitSurvivesRestartTest(unittest.TestCase):
    def test_temporary_breaker_expiry_survives_restart_and_clears_on_deadline(self):
        db = SessionBackedDb(); store = RiskStateStore(db); cfg = _cfg()
        first = RiskManager(cfg, state_store=store)
        deadline = utcnow() + timedelta(minutes=30)
        first.trip_circuit_breaker(
            "unverified exchange anomaly", cause="protective:x",
            expires_at=deadline, category="protective_execution_anomaly",
        )
        restarted = RiskManager(cfg, state_store=store)
        self.assertTrue(restarted.circuit_breaker_tripped)
        self.assertEqual(restarted.expire_temporary_causes(deadline - timedelta(seconds=1)), [])
        self.assertEqual(restarted.expire_temporary_causes(deadline), ["protective:x"])
        self.assertFalse(restarted.circuit_breaker_tripped)

    def test_utc_day_reset_does_not_shorten_temporary_execution_quarantine(self):
        manager = RiskManager(_cfg())
        deadline = utcnow() + timedelta(minutes=30)
        manager.trip_circuit_breaker(
            "execution quarantine", cause="protective:x", expires_at=deadline,
            category="protective_execution_anomaly",
        )
        manager._daily_reset_date = utc_today() - timedelta(days=1)
        manager.ensure_daily_reset(1000)
        self.assertIn("protective:x", manager.breaker_causes())

    def test_daily_pnl_and_counters_survive_restart(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()

        first = RiskManager(cfg, state_store=store)
        first.ensure_daily_reset(1000.0)
        first.record_closed_pnl(-12.5)
        first.record_open_trade("ETHUSDT")

        # Перезапуск процесса: полностью новый объект, та же БД.
        second = RiskManager(cfg, state_store=store)
        self.assertEqual(second._daily_pnl_usdt, -12.5)
        self.assertEqual(second._daily_trade_count, 1)
        self.assertEqual(second._symbol_trade_counts.get("ETHUSDT"), 1)
        self.assertEqual(second._daily_start_balance, 1000.0)

        # ensure_daily_reset в тот же UTC-день не должен ничего обнулять
        second.ensure_daily_reset(1000.0)
        self.assertEqual(second._daily_pnl_usdt, -12.5)
        self.assertEqual(second._daily_trade_count, 1)

    def test_circuit_breaker_stays_tripped_after_restart(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()

        first = RiskManager(cfg, state_store=store)
        first.ensure_daily_reset(1000.0)
        # Убыток больше 3% от 1000 USDT — лимит превышен
        first.record_closed_pnl(-35.0)
        result = first.evaluate(_long_signal(), [], 1000.0)
        self.assertFalse(result.approved)
        self.assertTrue(first.circuit_breaker_tripped)

        restarted = RiskManager(cfg, state_store=store)
        self.assertTrue(restarted.circuit_breaker_tripped)
        after_restart = restarted.evaluate(_long_signal(), [], 1000.0)
        self.assertFalse(after_restart.approved)
        self.assertIn("Circuit breaker", after_restart.reason)

    def test_restart_does_not_refix_start_balance_on_drawdown(self):
        """
        Ключевой сценарий регрессии: раньше рестарт фиксировал стартовый баланс
        заново по просевшему балансу, и дневной лимит убытка отсчитывался с нуля.
        """
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()

        first = RiskManager(cfg, state_store=store)
        first.ensure_daily_reset(1000.0)
        first.record_closed_pnl(-20.0)

        restarted = RiskManager(cfg, state_store=store)
        restarted.ensure_daily_reset(980.0)  # баланс уже просел
        self.assertEqual(restarted._daily_start_balance, 1000.0)
        self.assertEqual(restarted._daily_pnl_usdt, -20.0)


# ======================================================================
# 2. Восстановление дневного PnL из журнала
# ======================================================================

class DailyPnlRestoreTest(unittest.TestCase):
    def test_journal_sums_only_todays_closed_trades(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)
        now = datetime.now(timezone.utc)

        self._add_trade(db, "today-1", -5.0, now - timedelta(hours=1), "closed")
        self._add_trade(db, "today-2", 2.0, now - timedelta(hours=2), "closed")
        self._add_trade(db, "yesterday", -100.0, now - timedelta(days=1), "closed")
        self._add_trade(db, "still-open", None, None, "open")
        # orphaned не учитывается: результат неизвестен, 0 был бы враньём
        self._add_trade(db, "orphan", None, now, "orphaned")

        total, count = journal.sum_closed_pnl_for_utc_day(utc_today())
        self.assertAlmostEqual(total, -3.0)
        self.assertEqual(count, 2)

    def test_restore_prefers_more_conservative_value(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()

        manager = RiskManager(cfg, state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.record_closed_pnl(-5.0)

        # Журнал знает о большем убытке, чем успело записать состояние
        with self.assertLogs("risk.risk_manager", level="WARNING") as logs:
            manager.restore_daily_pnl_from_journal(-18.0, 3)
        self.assertEqual(manager._daily_pnl_usdt, -18.0)
        self.assertTrue(any("РАСХОЖДЕНИЕ" in m for m in logs.output))

    def test_restore_keeps_state_value_when_state_is_more_conservative(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        manager = RiskManager(_cfg(), state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.record_closed_pnl(-30.0)

        # Журнал оптимистичнее — берём своё, более убыточное значение
        with self.assertLogs("risk.risk_manager", level="WARNING"):
            manager.restore_daily_pnl_from_journal(-2.0, 1)
        self.assertEqual(manager._daily_pnl_usdt, -30.0)

    @staticmethod
    def _add_trade(db, order_link_id, pnl, closed_at, status):
        session = db.get_session()
        try:
            session.add(TradeLog(
                symbol="ETHUSDT", action="open_long", source="test", reason="r",
                order_link_id=order_link_id, entry_price=100, size_usdt=50, leverage=1,
                status=status, pnl_usdt=pnl, closed_at=closed_at,
                opened_at=datetime.now(timezone.utc) - timedelta(hours=3),
            ))
            session.commit()
        finally:
            session.close()


# ======================================================================
# 3. Защита от повторного входа: каждый источник блокирует независимо
# ======================================================================

class DuplicateEntryGuardTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.execution = FakeExecution()
        self.journal = FakeJournal()
        self.risk = RiskManager(self.cfg)
        self.engine = _bare_engine(self.cfg, self.execution, self.journal, self.risk)

    def test_no_block_when_symbol_is_free(self):
        self.assertIsNone(self.engine._entry_block_reason("ETHUSDT", []))

    def test_live_position_blocks(self):
        positions = [{"symbol": "ETHUSDT", "size": "1", "side": "Buy"}]
        reason = self.engine._entry_block_reason("ETHUSDT", positions)
        self.assertIn("живая позиция", reason)

    def test_open_journal_trade_blocks_even_without_live_position(self):
        """Биржа ещё не показывает позицию, но журнал уже знает о входе."""
        self.journal.open.append({
            "order_link_id": "oid-1", "symbol": "ETHUSDT",
            "action": "open_long", "entry_price": 100.0, "opened_at_ms": 0,
        })
        reason = self.engine._entry_block_reason("ETHUSDT", [])
        self.assertIn("журнал считает сделку", reason)

    def test_cooldown_blocks(self):
        self.risk.record_open_trade("ETHUSDT")
        reason = self.engine._entry_block_reason("ETHUSDT", [])
        self.assertIn("Cooldown", reason)

    def test_symbol_trade_limit_blocks(self):
        self.cfg.cooldown_minutes = 0
        self.cfg.max_trades_per_symbol = 2
        self.risk.record_open_trade("ETHUSDT")
        self.risk.record_open_trade("ETHUSDT")
        reason = self.engine._entry_block_reason("ETHUSDT", [])
        self.assertIn("лимит сделок по ETHUSDT", reason)

    def test_pending_unconfirmed_order_blocks(self):
        self.cfg.cooldown_minutes = 0
        self.risk.mark_entry_pending("ETHUSDT")
        reason = self.engine._entry_block_reason("ETHUSDT", [])
        self.assertIn("не подтверждённый ордер", reason)

    def test_blocked_symbol_blocks(self):
        self.risk.block_symbol("ETHUSDT", "ручная блокировка")
        reason = self.engine._entry_block_reason("ETHUSDT", [])
        self.assertIn("заблокирован", reason)

    def test_journal_failure_blocks_entry_instead_of_trading_blind(self):
        def boom(symbol=None):
            raise RuntimeError("db down")

        self.journal.get_open_trades = boom
        with self.assertLogs("strategy.engine", level="ERROR"):
            reason = self.engine._entry_block_reason("ETHUSDT", [])
        self.assertIn("журнал недоступен", reason)


# ======================================================================
# 4-5. Подтверждение исполнения ордера
# ======================================================================

class OrderConfirmationTest(unittest.TestCase):
    def _execution(self, session):
        execution = object.__new__(ExecutionEngine)
        execution.cfg = _cfg()
        execution.session = session
        execution._lot_size_cache = {}
        return execution

    def test_filled_order_is_confirmed(self):
        class Session:
            def get_open_orders(self, **kwargs):
                return {"result": {"list": [{
                    "orderLinkId": "oid-1", "orderStatus": "Filled",
                    "cumExecQty": "0.5", "avgPrice": "100",
                }]}}

            def get_order_history(self, **kwargs):
                return {"result": {"list": []}}

        confirmation = self._execution(Session()).confirm_order("ETHUSDT", "oid-1", delay_seconds=0)
        self.assertEqual(confirmation.status, FillStatus.FILLED)
        self.assertTrue(confirmation.has_exposure)
        self.assertEqual(confirmation.filled_qty, 0.5)

    def test_rejected_order_is_confirmed_as_rejected(self):
        class Session:
            def get_open_orders(self, **kwargs):
                return {"result": {"list": []}}

            def get_order_history(self, **kwargs):
                return {"result": {"list": [{
                    "orderLinkId": "oid-1", "orderStatus": "Rejected", "cumExecQty": "0",
                }]}}

        confirmation = self._execution(Session()).confirm_order("ETHUSDT", "oid-1", delay_seconds=0)
        self.assertEqual(confirmation.status, FillStatus.REJECTED)
        self.assertFalse(confirmation.has_exposure)

    def test_partially_filled_reports_exposure(self):
        class Session:
            def get_open_orders(self, **kwargs):
                return {"result": {"list": [{
                    "orderLinkId": "oid-1", "orderStatus": "PartiallyFilled",
                    "cumExecQty": "0.2", "avgPrice": "100",
                }]}}

            def get_order_history(self, **kwargs):
                return {"result": {"list": []}}

        confirmation = self._execution(Session()).confirm_order(
            "ETHUSDT", "oid-1", attempts=2, delay_seconds=0,
        )
        self.assertEqual(confirmation.status, FillStatus.PARTIALLY_FILLED)
        self.assertTrue(confirmation.has_exposure)

    def test_accepted_but_never_filled_becomes_unknown_with_bounded_retries(self):
        """Ордер принят и висит в 'New': ретраи ограничены, итог — UNKNOWN."""
        class Session:
            def __init__(self):
                self.calls = 0

            def get_open_orders(self, **kwargs):
                self.calls += 1
                return {"result": {"list": [{
                    "orderLinkId": "oid-1", "orderStatus": "New", "cumExecQty": "0",
                }]}}

            def get_order_history(self, **kwargs):
                return {"result": {"list": []}}

            def get_positions(self, **kwargs):
                return {"result": {"list": []}}

        session = Session()
        confirmation = self._execution(session).confirm_order(
            "ETHUSDT", "oid-1", attempts=3, delay_seconds=0,
        )
        self.assertEqual(confirmation.status, FillStatus.UNKNOWN)
        self.assertFalse(confirmation.has_exposure)
        self.assertFalse(confirmation.is_conclusive)
        # Ровно 3 попытки — никакого бесконечного ожидания
        self.assertEqual(session.calls, 3)

    def test_unconfirmed_order_falls_back_to_live_position(self):
        class Session:
            def get_open_orders(self, **kwargs):
                return {"result": {"list": []}}

            def get_order_history(self, **kwargs):
                return {"result": {"list": []}}

            def get_positions(self, **kwargs):
                return {"result": {"list": [{
                    "symbol": "ETHUSDT", "size": "0.3", "avgPrice": "100",
                }]}}

        confirmation = self._execution(Session()).confirm_order(
            "ETHUSDT", "oid-1", attempts=2, delay_seconds=0,
        )
        self.assertEqual(confirmation.status, FillStatus.FILLED)
        self.assertEqual(confirmation.filled_qty, 0.3)

    def test_missing_order_link_id_is_unknown(self):
        confirmation = self._execution(object()).confirm_order("ETHUSDT", "", delay_seconds=0)
        self.assertEqual(confirmation.status, FillStatus.UNKNOWN)

    def test_api_errors_do_not_raise_and_end_as_unknown(self):
        class Session:
            def get_open_orders(self, **kwargs):
                raise RuntimeError("api down")

            def get_order_history(self, **kwargs):
                raise RuntimeError("api down")

            def get_positions(self, **kwargs):
                raise RuntimeError("api down")

        confirmation = self._execution(Session()).confirm_order(
            "ETHUSDT", "oid-1", attempts=2, delay_seconds=0,
        )
        self.assertEqual(confirmation.status, FillStatus.UNKNOWN)


class EngineFillHandlingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.execution = FakeExecution()
        self.journal = FakeJournal()
        self.risk = RiskManager(self.cfg)
        self.engine = _bare_engine(self.cfg, self.execution, self.journal, self.risk)

    def test_unknown_fill_blocks_symbol_and_logs_critical(self):
        self.execution.confirmation = OrderConfirmation(
            status=FillStatus.UNKNOWN, detail="нет ответа биржи",
        )
        with self.assertLogs("strategy.engine", level="CRITICAL") as logs:
            confirmation = self.engine._confirm_entry_fill("ETHUSDT", "oid-1")

        self.assertEqual(confirmation.status, FillStatus.UNKNOWN)
        self.assertIn("ETHUSDT", self.risk.blocked_symbols())
        self.assertTrue(any("НЕ подтверждено" in m for m in logs.output))
        self.assertIn("ETHUSDT", self.engine._entry_block_reason("ETHUSDT", []))

    def test_filled_clears_pending(self):
        self.risk.mark_entry_pending("ETHUSDT")
        self.execution.confirmation = OrderConfirmation(status=FillStatus.FILLED, filled_qty=1.0)
        self.engine._confirm_entry_fill("ETHUSDT", "oid-1")
        self.assertEqual(self.risk.pending_entry_symbols(), [])

    def test_confirmation_exception_blocks_symbol(self):
        def boom(*args, **kwargs):
            raise RuntimeError("network gone")

        self.execution.confirm_order = boom
        with self.assertLogs("strategy.engine", level="CRITICAL"):
            confirmation = self.engine._confirm_entry_fill("ETHUSDT", "oid-1")
        self.assertEqual(confirmation.status, FillStatus.UNKNOWN)
        self.assertIn("ETHUSDT", self.risk.blocked_symbols())


# ======================================================================
# 5. Сбой журнала после принятия ордера не снимает cooldown
# ======================================================================

class JournalFailureDoesNotUnlockEntryTest(unittest.TestCase):
    def _candidate(self, engine):
        from strategy.engine import EntryCandidate
        return EntryCandidate(
            symbol="ETHUSDT",
            final_signal=_long_signal(),
            decision_report=_FakeReport(),
            last_price=100.0,
            risk_check=type("R", (), {"approved_size_usdt": 50.0, "approved_leverage": 1})(),
            atr_pct_of_price=1.0,
            spread_pct=0.02,
            funding_rate=0.0,
            position_size_multiplier=1.0,
            rank_score=0.9,
            entry_snapshot={},
            expert_vote_rows=[],
        )

    def test_cooldown_recorded_even_when_journal_write_fails(self):
        cfg = _cfg()
        execution = FakeExecution()
        journal = FakeJournal()
        journal.log_entry_ok = False  # БД лежит
        risk = RiskManager(cfg)
        engine = _bare_engine(cfg, execution, journal, risk)

        with self.assertLogs("strategy.engine", level="CRITICAL") as logs:
            result = engine._execute_candidate(self._candidate(engine))

        self.assertTrue(result)
        self.assertEqual(len(execution.opened), 1)
        # Журнал пуст, но счётчик и cooldown зафиксированы -> второго входа не будет
        self.assertEqual(journal.entries, [])
        self.assertEqual(risk._symbol_trade_counts.get("ETHUSDT"), 1)
        self.assertEqual(risk._daily_trade_count, 1)
        block = engine._entry_block_reason("ETHUSDT", [])
        self.assertIsNotNone(block)
        self.assertTrue(any("не записан в trade_log" in m for m in logs.output))

    def test_lost_order_link_id_blocks_symbol(self):
        cfg = _cfg()
        execution = FakeExecution()
        execution.open_position = lambda **kwargs: {"retCode": 0, "retMsg": "OK"}
        journal = FakeJournal()
        risk = RiskManager(cfg)
        engine = _bare_engine(cfg, execution, journal, risk)

        with self.assertLogs("strategy.engine", level="CRITICAL"):
            result = engine._execute_candidate(self._candidate(engine))

        self.assertTrue(result)
        self.assertIn("ETHUSDT", risk.blocked_symbols())
        self.assertEqual(risk._symbol_trade_counts.get("ETHUSDT"), 1)

    def test_rejected_after_accept_keeps_cooldown(self):
        cfg = _cfg()
        execution = FakeExecution()
        execution.confirmation = OrderConfirmation(
            status=FillStatus.REJECTED, detail="orderStatus=Rejected",
        )
        journal = FakeJournal()
        risk = RiskManager(cfg)
        engine = _bare_engine(cfg, execution, journal, risk)

        result = engine._execute_candidate(self._candidate(engine))
        self.assertFalse(result)
        self.assertEqual(journal.entries, [])
        # Экспозиции нет, но по символу что-то не так — cooldown остаётся
        self.assertEqual(risk._symbol_trade_counts.get("ETHUSDT"), 1)
        self.assertEqual(risk.pending_entry_symbols(), [])


class _FakeReport:
    confidence = 0.7
    expected_rr = 2.0
    risk_score = 0.2
    confirmation_count = 2
    confirmation_families = ["trend", "price_location"]
    rejected_actions = {}
    votes = []
    winning_action = Action.OPEN_LONG
    ai_analysis = None
    market_context = type("C", (), {
        "summary": lambda self: "synthetic", "regime": "TREND", "trend": "UP",
    })()

    def journal_reason(self, limit=1000):
        return "synthetic reason"


# ======================================================================
# 6. Orphaned-сделка взводит circuit breaker
# ======================================================================

class OrphanReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.execution = FakeExecution()
        self.journal = FakeJournal()
        self.risk = RiskManager(self.cfg)
        self.engine = _bare_engine(self.cfg, self.execution, self.journal, self.risk)
        self.journal.open.append({
            "order_link_id": "lost-1", "symbol": "ETHUSDT", "action": "open_long",
            "entry_price": 100.0, "opened_at_ms": int(time.time() * 1000) - 60_000,
        })
        self.execution.closed_pnl["ETHUSDT"] = []

    def test_orphan_only_after_bounded_attempts_then_trips_breaker(self):
        # Первые попытки: только предупреждение, сделка ещё не orphaned
        for _ in range(2):
            with self.assertLogs("strategy.engine", level="WARNING"):
                self.engine._sync_closed_trades([])
            self.assertEqual(self.journal.orphaned, [])
            self.assertFalse(self.risk.circuit_breaker_tripped)

        # Третья попытка — сдаёмся
        with self.assertLogs("strategy.engine", level="CRITICAL") as logs:
            self.engine._sync_closed_trades([])

        self.assertEqual(len(self.journal.orphaned), 1)
        self.assertEqual(self.journal.orphaned[0][0], "lost-1")
        self.assertTrue(self.risk.circuit_breaker_tripped)
        self.assertTrue(any("ORPHANED" in m for m in logs.output))

        # Circuit breaker реально останавливает торговлю
        result = self.risk.evaluate(_long_signal(), [], 1000.0)
        self.assertFalse(result.approved)
        self.assertIn("Circuit breaker", result.reason)

    def test_api_error_does_not_orphan_a_healthy_trade(self):
        """
        Регрессия review: сбой API возвращал пустой список, неотличимый от
        "закрытий нет", и за 3 цикла помечал живую сделку orphaned.
        Ошибка обязана прерывать цикл сверки, а не считаться отсутствием данных.
        """
        def api_down(symbol, start_time_ms=None, max_pages=5):
            raise RuntimeError("bybit unavailable")

        self.execution.get_closed_pnl_since = api_down
        for _ in range(_ORPHAN_MAX_ATTEMPTS + 2):
            with self.assertLogs("strategy.engine", level="ERROR"):
                self.engine._sync_closed_trades([])

        self.assertEqual(self.journal.orphaned, [])
        self.assertFalse(self.risk.circuit_breaker_tripped)
        self.assertEqual(self.engine._orphan_attempts, {})
        self.assertEqual(len(self.journal.open), 1)

    def test_search_uses_open_time_and_pagination(self):
        self.engine._sync_closed_trades([])
        self.assertEqual(len(self.execution.since_calls), 1)
        symbol, start_ms = self.execution.since_calls[0]
        self.assertEqual(symbol, "ETHUSDT")
        self.assertIsNotNone(start_ms)  # ищем от времени открытия, а не последние 50

    def test_found_closure_resets_orphan_counter(self):
        with self.assertLogs("strategy.engine", level="WARNING"):
            self.engine._sync_closed_trades([])
        self.assertEqual(self.engine._orphan_attempts.get("lost-1"), 1)

        self.execution.closed_pnl["ETHUSDT"] = [{
            "avgEntryPrice": "100.0",
            "createdTime": str(int(time.time() * 1000)),
            "avgExitPrice": "103",
            "closedPnl": "1.5",
        }]
        self.engine._build_exit_snapshot = lambda symbol, match: {}
        self.engine._sync_closed_trades([])

        self.assertNotIn("lost-1", self.engine._orphan_attempts)
        self.assertEqual(self.journal.orphaned, [])
        self.assertEqual(self.risk._daily_pnl_usdt, 1.5)


# ======================================================================
# 6b. Полный жизненный цикл orphaned: обнаружение -> восстановление
# ======================================================================

class OrphanRecoveryLifecycleTest(unittest.TestCase):
    """Orphaned — не конечное состояние. Проверяем весь путь назад."""

    def setUp(self):
        self.cfg = _cfg()
        self.execution = FakeExecution()
        self.journal = FakeJournal()
        self.risk = RiskManager(self.cfg)
        self.engine = _bare_engine(self.cfg, self.execution, self.journal, self.risk)
        self.engine._build_exit_snapshot = lambda symbol, match: {}
        self.opened_ms = int(time.time() * 1000) - 60_000
        self.journal.open.append({
            "order_link_id": "lost-1", "symbol": "ETHUSDT", "action": "open_long",
            "entry_price": 100.0, "opened_at_ms": self.opened_ms, "status": "open",
        })
        self.execution.closed_pnl["ETHUSDT"] = []

    def _drive_to_orphan(self):
        for _ in range(_ORPHAN_MAX_ATTEMPTS):
            self.engine._sync_closed_trades([])
        self.assertEqual(len(self.journal.orphaned), 1)
        self.assertTrue(self.risk.circuit_breaker_tripped)

    def _publish_closure(self, pnl="-3.5", created_ms=None):
        self.execution.closed_pnl["ETHUSDT"] = [{
            "avgEntryPrice": "100.0",
            "side": "Sell",                      # закрытие лонга
            "createdTime": str(created_ms or int(time.time() * 1000)),
            "updatedTime": str(created_ms or int(time.time() * 1000)),
            "avgExitPrice": "96.5",
            "closedPnl": pnl,
        }]

    def test_orphan_is_recovered_when_closure_appears_later(self):
        self._drive_to_orphan()
        self._publish_closure(pnl="-3.5")

        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            self.engine._sync_closed_trades([])

        # Статус orphaned снят, сделка закрыта
        self.assertEqual(self.journal.get_orphaned_trades(), [])
        self.assertEqual(self.journal.count_orphaned(), 0)
        self.assertIn("lost-1", self.journal.closed)
        # Реальный PnL учтён в дневном итоге
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, -3.5)
        # Circuit breaker снят: других причин не было
        self.assertFalse(self.risk.circuit_breaker_tripped)
        self.assertEqual(self.risk.breaker_causes(), {})
        # Событие восстановления записано
        self.assertTrue(any("ORPHAN_RECOVERED" in m for m in logs.output))

    def test_repeated_recovery_is_idempotent(self):
        """Повторная реконсиляция не должна прибавлять PnL и трогать журнал второй раз."""
        self._drive_to_orphan()
        self._publish_closure(pnl="-3.5")
        self.engine._sync_closed_trades([])

        pnl_after_first = self.risk._daily_pnl_usdt
        closed_after_first = list(self.journal.closed)
        self.assertAlmostEqual(pnl_after_first, -3.5)

        # Ещё три прогона подряд — состояние не должно измениться ничем
        for _ in range(3):
            self.engine._sync_closed_trades([])

        self.assertAlmostEqual(self.risk._daily_pnl_usdt, pnl_after_first)
        self.assertEqual(self.journal.closed, closed_after_first)
        self.assertEqual(self.journal.count_orphaned(), 0)
        self.assertFalse(self.risk.circuit_breaker_tripped)

    def test_recovery_does_not_double_count_pnl_across_restart(self):
        """После восстановления сделка закрыта — повторный старт её не пересчитает."""
        self._drive_to_orphan()
        self._publish_closure(pnl="-7.25")
        self.engine._sync_closed_trades([])
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, -7.25)

        # Сделка уже в closed -> log_exit вернёт recorded=False
        result = self.journal.log_exit("lost-1", 96.5, -7.25)
        self.assertFalse(result.recorded)
        self.assertTrue(result.already_closed)
        self.assertAlmostEqual(self.risk._daily_pnl_usdt, -7.25)

    def test_recovery_clears_only_its_own_breaker_cause(self):
        """Дневной лимит убытка должен пережить восстановление orphaned-сделки."""
        self._drive_to_orphan()
        self.risk.trip_circuit_breaker("дневной лимит убытка достигнут", cause=DAILY_LOSS_CAUSE)
        self.assertEqual(len(self.risk.breaker_causes()), 2)

        self._publish_closure(pnl="-3.5")
        self.engine._sync_closed_trades([])

        causes = self.risk.breaker_causes()
        self.assertNotIn(orphan_cause("lost-1"), causes)
        self.assertIn(DAILY_LOSS_CAUSE, causes)
        self.assertTrue(self.risk.circuit_breaker_tripped)  # торговля всё ещё стоит

    def test_resolve_breaker_cause_is_idempotent(self):
        self.risk.trip_circuit_breaker("orphan", sticky=True, cause="orphan:x")
        self.assertTrue(self.risk.resolve_breaker_cause("orphan:x"))
        self.assertFalse(self.risk.resolve_breaker_cause("orphan:x"))
        self.assertFalse(self.risk.circuit_breaker_tripped)

    def test_recovered_trade_closed_yesterday_not_counted_in_today_pnl(self):
        """
        Восстановленная сделка, закрывшаяся вчера, идёт в журнал, но не в
        сегодняшний дневной лимит: лимит считает убыток за текущий день.
        """
        # Сделка открыта позавчера и закрыта вчера, orphaned; восстанавливаем сегодня.
        self.journal.open[0]["opened_at_ms"] = int(
            (datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000
        )
        self._drive_to_orphan()
        yesterday_ms = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp() * 1000)
        self._publish_closure(pnl="-9.0", created_ms=yesterday_ms)

        with self.assertLogs("strategy.engine", level="WARNING") as logs:
            self.engine._sync_closed_trades([])

        self.assertEqual(self.journal.count_orphaned(), 0)      # восстановлена
        self.assertEqual(self.risk._daily_pnl_usdt, 0.0)        # но не в сегодняшнем PnL
        self.assertFalse(self.risk.circuit_breaker_tripped)     # причина устранена
        self.assertTrue(any("не сегодня" in m for m in logs.output))

    def test_orphaned_trade_keeps_being_searched_without_retripping(self):
        """Пока не восстановлена — ищем дальше, но причину повторно не взводим."""
        self._drive_to_orphan()
        causes_before = self.risk.breaker_causes()

        for _ in range(3):
            self.engine._sync_closed_trades([])

        self.assertEqual(self.risk.breaker_causes(), causes_before)
        self.assertEqual(len(self.journal.orphaned), 1)  # помечена один раз, не трижды
        self.assertEqual(self.journal.count_orphaned(), 1)

    def test_real_journal_orphan_roundtrip(self):
        """Тот же цикл на настоящем журнале, а не на фейке."""
        db = SessionBackedDb()
        journal = TradeJournal(db)
        journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100, 50, 1, 1.5, 3.0, "oid-real",
        )
        self.assertTrue(journal.mark_orphaned("oid-real", "закрытие не найдено"))
        # orphaned не занимает символ, но подлежит сверке
        self.assertEqual(journal.get_open_trades("ETHUSDT"), [])
        unresolved = journal.get_unresolved_trades("ETHUSDT")
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["status"], "orphaned")
        self.assertEqual(journal.count_orphaned(), 1)

        # Повторная пометка идемпотентна
        self.assertFalse(journal.mark_orphaned("oid-real", "ещё раз"))

        # Восстановление
        closed_at = datetime.now(timezone.utc)
        result = journal.log_exit("oid-real", 96.0, -4.0, exit_reason="SL", closed_at=closed_at)
        self.assertTrue(result.recorded)
        self.assertTrue(result.recovered_from_orphan)
        self.assertEqual(result.symbol, "ETHUSDT")

        # Повторное восстановление ничего не меняет
        again = journal.log_exit("oid-real", 96.0, -4.0)
        self.assertFalse(again.recorded)
        self.assertTrue(again.already_closed)

        self.assertEqual(journal.count_orphaned(), 0)
        self.assertEqual(journal.get_unresolved_trades("ETHUSDT"), [])
        total, count = journal.sum_closed_pnl_for_utc_day(utc_today())
        self.assertAlmostEqual(total, -4.0)
        self.assertEqual(count, 1)

        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-real").one()
            self.assertEqual(row.status, "closed")
            self.assertEqual(row.exit_type, "stop_loss")   # не "orphaned"
            self.assertAlmostEqual(float(row.pnl_usdt), -4.0)
            self.assertIsNotNone(row.pnl_pct)
        finally:
            session.close()

    def test_breaker_restored_from_journal_when_risk_state_lost(self):
        """
        risk_state — кэш, журнал — источник правды. Если состояние потеряли,
        а orphaned-сделки в журнале есть, бот обязан стартовать с взведённым
        breaker, а не торговать вслепую.
        """
        journal = FakeJournal()
        journal.open.append({
            "order_link_id": "lost-9", "symbol": "ETHUSDT", "action": "open_long",
            "entry_price": 100.0, "opened_at_ms": self.opened_ms, "status": "orphaned",
        })
        risk = RiskManager(_cfg())          # пустое состояние, как после потери risk_state
        self.assertFalse(risk.circuit_breaker_tripped)

        engine = _bare_engine(_cfg(), FakeExecution(), journal, risk)
        with self.assertLogs("strategy.engine", level="CRITICAL"):
            engine._restore_breaker_from_orphaned_trades()

        self.assertTrue(risk.circuit_breaker_tripped)
        self.assertIn(orphan_cause("lost-9"), risk.breaker_causes())
        self.assertFalse(risk.evaluate(_long_signal(), [], 1000.0).approved)

    def test_breaker_restore_from_journal_is_idempotent(self):
        journal = FakeJournal()
        journal.open.append({
            "order_link_id": "lost-9", "symbol": "ETHUSDT", "action": "open_long",
            "entry_price": 100.0, "opened_at_ms": self.opened_ms, "status": "orphaned",
        })
        risk = RiskManager(_cfg())
        engine = _bare_engine(_cfg(), FakeExecution(), journal, risk)

        with self.assertLogs("strategy.engine", level="CRITICAL"):
            engine._restore_breaker_from_orphaned_trades()
        first = risk.breaker_causes()
        # Повторные старты не должны плодить или переписывать причины
        for _ in range(3):
            with self.assertLogs("strategy.engine", level="CRITICAL"):
                engine._restore_breaker_from_orphaned_trades()
        self.assertEqual(risk.breaker_causes(), first)
        self.assertEqual(len(risk.breaker_causes()), 1)

    def test_orphaned_older_than_closed_pnl_window_is_not_rescanned(self):
        """Сделку старше 7 суток биржа не отдаст — не расширяем окно запроса вечно."""
        self.journal.open[0]["status"] = "orphaned"
        self.journal.open[0]["opened_at_ms"] = int(
            (datetime.now(timezone.utc) - timedelta(days=9)).timestamp() * 1000
        )
        self.engine._sync_closed_trades([])
        self.assertEqual(self.execution.since_calls, [])  # запроса к бирже не было


# ======================================================================
# 6c. Matching закрытых сделок
# ======================================================================

class ClosedPnlMatchingTest(unittest.TestCase):
    def _trade(self, action="open_long", entry=100.0):
        return {
            "order_link_id": "oid", "symbol": "ETHUSDT", "action": action,
            "entry_price": entry, "opened_at_ms": 1_000_000, "status": "open",
        }

    def test_direction_mismatch_is_rejected(self):
        """Закрытие шорта не должно матчиться на лонг, открытый по той же цене."""
        rows = [{
            "avgEntryPrice": "100.0", "side": "Buy",   # закрытие ШОРТА
            "createdTime": "2000000", "avgExitPrice": "98", "closedPnl": "5",
        }]
        self.assertIsNone(StrategyEngine._find_matching_closed_pnl(self._trade("open_long"), rows))
        self.assertIsNotNone(StrategyEngine._find_matching_closed_pnl(self._trade("open_short"), rows))

    def test_closest_entry_price_wins_over_earliest(self):
        rows = [
            {"avgEntryPrice": "100.4", "side": "Sell", "createdTime": "2000000",
             "avgExitPrice": "101", "closedPnl": "1"},
            {"avgEntryPrice": "100.0", "side": "Sell", "createdTime": "3000000",
             "avgExitPrice": "102", "closedPnl": "2"},
        ]
        match = StrategyEngine._find_matching_closed_pnl(self._trade(entry=100.0), rows)
        self.assertEqual(match["closedPnl"], "2")  # точная цена, хоть и позже

    def test_closure_before_open_is_rejected(self):
        rows = [{"avgEntryPrice": "100.0", "side": "Sell", "createdTime": "500",
                 "avgExitPrice": "101", "closedPnl": "1"}]
        self.assertIsNone(StrategyEngine._find_matching_closed_pnl(self._trade(), rows))

    def test_updated_time_is_used_when_created_time_precedes_journal_entry(self):
        rows = [{
            "avgEntryPrice": "100.0", "side": "Sell",
            "createdTime": "999500", "updatedTime": "2000000",
            "avgExitPrice": "101", "closedPnl": "1",
        }]
        self.assertIsNotNone(
            StrategyEngine._find_matching_closed_pnl(self._trade(), rows)
        )

    def test_price_outside_tolerance_is_rejected(self):
        rows = [{"avgEntryPrice": "105.0", "side": "Sell", "createdTime": "2000000",
                 "avgExitPrice": "101", "closedPnl": "1"}]
        self.assertIsNone(StrategyEngine._find_matching_closed_pnl(self._trade(), rows))

    def test_missing_side_field_still_matches(self):
        """Старые записи без side не должны переставать матчиться."""
        rows = [{"avgEntryPrice": "100.0", "createdTime": "2000000",
                 "avgExitPrice": "101", "closedPnl": "1"}]
        self.assertIsNotNone(StrategyEngine._find_matching_closed_pnl(self._trade(), rows))


# ======================================================================
# 7. Смешанные naive и aware datetime не ломают аналитику
# ======================================================================

class MixedDatetimeTest(unittest.TestCase):
    def test_result_metrics_handles_mixed_naive_and_aware(self):
        now = datetime.now(timezone.utc)
        rows = [
            # aware (новые записи)
            {"pnl_usdt": 5.0, "closed_at": now, "opened_at": now - timedelta(hours=1)},
            # naive (старые записи из SQLite)
            {"pnl_usdt": -2.0, "closed_at": (now - timedelta(hours=2)).replace(tzinfo=None),
             "opened_at": (now - timedelta(hours=3)).replace(tzinfo=None)},
            # без closed_at — откат на opened_at
            {"pnl_usdt": 1.0, "closed_at": None, "opened_at": now - timedelta(hours=4)},
            # вообще без времени
            {"pnl_usdt": 0.5, "closed_at": None, "opened_at": None},
        ]
        metrics = result_metrics(rows)
        self.assertEqual(metrics["sample_size"], 4)
        self.assertAlmostEqual(metrics["net_pnl_usdt"], 4.5)

    def test_sort_key_orders_mixed_rows_chronologically(self):
        from timeutils import trade_time_sort_key

        now = datetime.now(timezone.utc)
        rows = [
            {"closed_at": now},
            {"closed_at": (now - timedelta(hours=5)).replace(tzinfo=None)},
            {"closed_at": None, "opened_at": None},
        ]
        ordered = sorted(rows, key=trade_time_sort_key)
        self.assertIsNone(ordered[0]["closed_at"])   # без времени — в начало
        self.assertIsNone(ordered[1]["closed_at"].tzinfo)  # naive, 5 часов назад
        self.assertEqual(ordered[2]["closed_at"], now)

    def test_journal_holding_seconds_with_naive_opened_at(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)
        journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100, 50, 1, 1.5, 3.0, "oid-tz",
        )
        # Симулируем старую запись: naive opened_at, как отдаёт SQLite
        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-tz").one()
            row.opened_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=120)
            session.commit()
        finally:
            session.close()

        self.assertTrue(journal.log_exit("oid-tz", 103, 1.5))
        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-tz").one()
            self.assertIsNotNone(row.holding_seconds)
            self.assertGreaterEqual(row.holding_seconds, 119)
        finally:
            session.close()

    def test_get_open_trades_normalizes_naive_opened_at(self):
        db = SessionBackedDb()
        journal = TradeJournal(db)
        journal.log_entry(
            "ETHUSDT", Action.OPEN_LONG, "test", "entry",
            100, 50, 1, 1.5, 3.0, "oid-naive",
        )
        session = db.get_session()
        try:
            row = session.query(TradeLog).filter(TradeLog.order_link_id == "oid-naive").one()
            row.opened_at = datetime(2026, 7, 14, 10, 0, 0)  # naive
            session.commit()
        finally:
            session.close()

        trades = journal.get_open_trades("ETHUSDT")
        self.assertEqual(len(trades), 1)
        self.assertIsNotNone(trades[0]["opened_at"].tzinfo)
        expected_ms = int(datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(trades[0]["opened_at_ms"], expected_ms)


# ======================================================================
# 8. Переход на новый UTC-день
# ======================================================================

class UtcDayRolloverTest(unittest.TestCase):
    def test_new_utc_day_resets_daily_values(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()

        manager = RiskManager(cfg, state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.record_closed_pnl(-40.0)
        manager.record_open_trade("ETHUSDT")
        manager.trip_circuit_breaker("вчерашний лимит убытка")

        # Наступили новые сутки
        manager._daily_reset_date = utc_today() - timedelta(days=1)
        manager.ensure_daily_reset(960.0)

        self.assertEqual(manager._daily_pnl_usdt, 0.0)
        self.assertEqual(manager._daily_trade_count, 0)
        self.assertEqual(manager._symbol_trade_counts, {})
        self.assertEqual(manager._last_entry_ts_by_symbol, {})
        self.assertEqual(manager._daily_start_balance, 960.0)
        self.assertFalse(manager.circuit_breaker_tripped)
        self.assertEqual(manager._daily_reset_date, utc_today())

    def test_sticky_breaker_survives_utc_day_rollover(self):
        """
        Регрессия review: circuit breaker от orphaned-сделки снимался в полночь,
        и бот возобновлял торговлю, не зная реального финансового результата.
        """
        db = SessionBackedDb()
        store = RiskStateStore(db)
        manager = RiskManager(_cfg(), state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.trip_circuit_breaker("orphaned-сделка: результат неизвестен", sticky=True)

        manager._daily_reset_date = utc_today() - timedelta(days=1)
        with self.assertLogs("risk.risk_manager", level="CRITICAL"):
            manager.ensure_daily_reset(1000.0)

        # Дневные значения сброшены, но торговля остаётся остановленной
        self.assertEqual(manager._daily_pnl_usdt, 0.0)
        self.assertTrue(manager.circuit_breaker_tripped)
        result = manager.evaluate(_long_signal(), [], 1000.0)
        self.assertFalse(result.approved)
        self.assertIn("Circuit breaker", result.reason)

    def test_sticky_breaker_survives_restart_across_days(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()
        manager = RiskManager(cfg, state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.trip_circuit_breaker("orphaned-сделка: результат неизвестен", sticky=True)

        stale = manager._snapshot()
        stale["day_utc"] = (utc_today() - timedelta(days=1)).isoformat()
        store.save(stale)

        with self.assertLogs("risk.risk_manager", level="CRITICAL"):
            restarted = RiskManager(cfg, state_store=store)
        self.assertTrue(restarted.circuit_breaker_tripped)
        self.assertEqual(restarted._daily_pnl_usdt, 0.0)  # дневное всё же сброшено

    def test_sticky_breaker_only_clears_manually(self):
        manager = RiskManager(_cfg())
        manager.trip_circuit_breaker("orphaned", sticky=True)
        manager._daily_reset_date = utc_today() - timedelta(days=1)
        with self.assertLogs("risk.risk_manager", level="CRITICAL"):
            manager.ensure_daily_reset(1000.0)
        self.assertTrue(manager.circuit_breaker_tripped)

        manager.manual_reset_circuit_breaker()
        self.assertFalse(manager.circuit_breaker_tripped)
        self.assertTrue(manager.evaluate(_long_signal(), [], 1000.0).approved)

    def test_non_sticky_breaker_is_not_upgraded_by_daily_loss(self):
        """Дневной лимит убытка должен сниматься сменой суток, как и раньше."""
        manager = RiskManager(_cfg())
        manager.ensure_daily_reset(1000.0)
        manager.record_closed_pnl(-35.0)
        manager.evaluate(_long_signal(), [], 1000.0)
        self.assertTrue(manager.circuit_breaker_tripped)

        manager._daily_reset_date = utc_today() - timedelta(days=1)
        manager.ensure_daily_reset(965.0)
        self.assertFalse(manager.circuit_breaker_tripped)

    def test_rollover_keeps_blocked_symbols_and_pending(self):
        """Неизвестная экспозиция не перестаёт быть проблемой из-за смены суток."""
        db = SessionBackedDb()
        store = RiskStateStore(db)
        manager = RiskManager(_cfg(), state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.block_symbol("ETHUSDT", "неизвестное состояние ордера")
        manager.mark_entry_pending("SOLUSDT")

        manager._daily_reset_date = utc_today() - timedelta(days=1)
        manager.ensure_daily_reset(1000.0)

        self.assertIn("ETHUSDT", manager.blocked_symbols())
        self.assertIn("SOLUSDT", manager.pending_entry_symbols())

    def test_state_from_previous_day_does_not_carry_daily_pnl(self):
        db = SessionBackedDb()
        store = RiskStateStore(db)
        cfg = _cfg()

        manager = RiskManager(cfg, state_store=store)
        manager.ensure_daily_reset(1000.0)
        manager.record_closed_pnl(-40.0)
        # Подделываем сохранённое состояние: оно от вчера
        stale = manager._snapshot()
        stale["day_utc"] = (utc_today() - timedelta(days=1)).isoformat()
        store.save(stale)

        fresh = RiskManager(cfg, state_store=store)
        self.assertEqual(fresh._daily_pnl_usdt, 0.0)
        self.assertEqual(fresh._daily_reset_date, utc_today())
        self.assertIsNone(fresh._daily_start_balance)


if __name__ == "__main__":
    unittest.main()
