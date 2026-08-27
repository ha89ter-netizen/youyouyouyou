"""Owner-facing Telegram surface: authorization, alert lifecycle and reports.

These tests pin the operational invariants, not the wording. The trading path
is deliberately absent: the monitor must never call Bybit or mutate risk state.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import operational_status as ops
from operator_control import (
    APPLIED, PAUSE, REJECTED, RESUME, OperatorControlApplier,
    OperatorControlStore, resume_preconditions,
)
from operator_monitor import OperatorMonitor
from reporting import TradingReportBuilder
from storage.models import (
    AccountSnapshot, Base, OperatorControlCommand, OperatorMonitorState,
    PositionSnapshot, RiskState, RunMetadata, Trade, TradeLog,
)
from timeutils import to_epoch_ms, utcnow

OWNER_CHAT = "42"
OWNER_USER = "777"


class Db:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def cfg(**overrides):
    base = dict(
        testnet=True, operator_monitor_interval_seconds=30,
        telegram_alerts_enabled=False, health_http_enabled=False,
        storage_max_database_bytes=0, storage_entry_block_ratio=.85,
        telegram_report_interval_minutes=60, telegram_report_period="24h",
        telegram_alert_escalation_seconds=0, telegram_alert_reminder_seconds=3600,
        max_orderbook_age_seconds=90, trading_enabled=True, run_id="run",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def seed(db, run_id="run", *, causes=None, fresh_market_data=True):
    session = db.get_session(); now = utcnow()
    session.add(RunMetadata(
        run_id=run_id, commit_sha="c" * 40, started_at=now - timedelta(hours=2),
        environment_summary={}, status="running",
        collector_heartbeat_at=now, trader_heartbeat_at=now,
    ))
    session.add(RiskState(
        id=1, day_utc=now.date().isoformat(), daily_pnl_usdt=0,
        daily_trade_count=0, circuit_breaker_tripped=bool(causes),
        circuit_breaker_sticky=False, circuit_breaker_causes=causes or {},
    ))
    if fresh_market_data:
        session.add(Trade(
            symbol="BTCUSDT", trade_id="t1", ts=to_epoch_ms(now),
            price=100, size=1, side="Buy",
        ))
    session.commit(); session.close()


def monitor_for(db, config=None, *, sent=None, updates=None, acks=None, owner=OWNER_USER):
    sent = sent if sent is not None else []

    def sender(text, buttons=None):
        sent.append((text, buttons))
        return True

    return OperatorMonitor(
        db, config or cfg(), "run", sender=sender,
        updates_fetcher=(lambda _offset: list(updates or [])) if updates is not None else None,
        authorized_chat_id=OWNER_CHAT, authorized_user_id=owner,
        callback_ack=(lambda cid, text: acks.append((cid, text)) or True)
        if acks is not None else None,
    )


def texts(sent):
    return [item[0] for item in sent]


def message_update(text, *, update_id=1, chat=OWNER_CHAT, user=OWNER_USER):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat}, "from": {"id": user}, "text": text},
    }


def callback_update(data, *, update_id=1, chat=OWNER_CHAT, user=OWNER_USER):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb1", "data": data, "from": {"id": user},
            "message": {"chat": {"id": chat}},
        },
    }


def close_trade(db, symbol, pnl, *, when=None, fee=0.1, run_id="run"):
    session = db.get_session()
    session.add(TradeLog(
        symbol=symbol, action="open_long", source="test", reason="test",
        order_link_id=f"link-{symbol}-{pnl}", run_id=run_id, entry_price=100,
        size_usdt=100, leverage=1, status="closed", pnl_usdt=pnl,
        total_fee_usdt=fee, closed_at=when or utcnow(),
    ))
    session.commit(); session.close()


def open_trade(db, symbol="ETHUSDT", *, unrealized=None, run_id="run"):
    session = db.get_session()
    trade = TradeLog(
        symbol=symbol, action="open_long", source="test", reason="test",
        order_link_id=f"open-{symbol}", run_id=run_id, entry_price=100,
        size_usdt=100, leverage=1, status="open",
    )
    session.add(trade); session.commit()
    if unrealized is not None:
        session.add(PositionSnapshot(
            run_id=run_id, trade_log_id=trade.id, order_link_id=trade.order_link_id,
            observed_at=utcnow(), snapshot_bucket=to_epoch_ms(utcnow()),
            symbol=symbol, side="Buy", quantity=1, average_entry=100,
            protection_status="confirmed", unrealized_pnl=unrealized,
            source="test", fetch_status="ok",
        ))
        session.commit()
    trade_id = trade.id
    session.close()
    return trade_id


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------

def test_unauthorized_user_in_the_owner_chat_is_rejected():
    """A correct chat id is not enough: the human must be the owner too."""
    db = Db(); seed(db); sent = []; updates = []
    monitor = monitor_for(db, sent=sent, updates=updates)
    monitor._initialize_durable_cursors()
    monitor.poll_once()  # drain startup and the first report
    sent.clear()
    updates.append(message_update("/status", user="999"))
    monitor.poll_once()
    assert sent == []


def test_unauthorized_callback_is_rejected_and_changes_nothing():
    db = Db(); seed(db); sent = []; acks = []
    monitor = monitor_for(
        db, sent=sent, acks=acks, updates=[callback_update("pause", user="999")]
    )
    monitor._initialize_durable_cursors(); sent.clear()
    monitor.poll_once()
    assert acks == [("cb1", "Not authorized")]
    assert monitor.control.pending() == []


def test_authorized_owner_command_is_accepted():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent, updates=[message_update("/status")])
    monitor._initialize_durable_cursors(); sent.clear()
    monitor.poll_once()
    assert any("HEALTHY" in text for text in texts(sent))


def test_missing_owner_configuration_disables_control_but_keeps_alerts(monkeypatch):
    """No TELEGRAM_USER_ID must mean "nobody", never "anyone in the chat"."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", OWNER_CHAT)
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)
    db = Db(); seed(db)
    monitor = OperatorMonitor(db, cfg(telegram_alerts_enabled=True), "run")
    assert monitor.commands_enabled is False
    assert monitor._sender is not None
    assert monitor._authorized_user_id is None


def test_configured_owner_enables_control():
    db = Db(); seed(db)
    monitor = monitor_for(db, updates=[])
    assert monitor.commands_enabled is True


# --------------------------------------------------------------------------
# Report metrics
# --------------------------------------------------------------------------

@pytest.fixture
def report_db():
    db = Db(); seed(db)
    close_trade(db, "UNIUSDT", 4.0)
    close_trade(db, "BTCUSDT", 2.0)
    close_trade(db, "ADAUSDT", -3.0)
    close_trade(db, "ETHUSDT", -1.0)
    # Outside the rolling 24h window: must not contaminate the period.
    close_trade(db, "OLDUSDT", 99.0, when=utcnow() - timedelta(days=2))
    return db


def test_report_metrics_use_one_source_and_the_declared_period(report_db):
    report = TradingReportBuilder(report_db, cfg(), "run").build("24h")
    assert report.period_label == "rolling 24h"
    assert report.closed_trades == 4
    assert report.realized_pnl == pytest.approx(2.0)
    assert report.gross_profit == pytest.approx(6.0)
    assert report.gross_loss == pytest.approx(-4.0)
    assert report.wins == 2
    assert report.win_rate == pytest.approx(50.0)
    assert report.fees == pytest.approx(0.4)


def test_best_and_worst_symbol_come_from_the_same_qualifying_set(report_db):
    report = TradingReportBuilder(report_db, cfg(), "run").build("24h")
    assert report.best_symbol.symbol == "UNIUSDT"
    assert report.best_symbol.realized_pnl == pytest.approx(4.0)
    assert report.worst_symbol.symbol == "ADAUSDT"
    assert report.worst_symbol.realized_pnl == pytest.approx(-3.0)


def test_zero_closed_trades_reports_no_win_rate_rather_than_zero_percent():
    db = Db(); seed(db)
    report = TradingReportBuilder(db, cfg(), "run").build("24h")
    assert report.closed_trades == 0
    assert report.win_rate is None
    assert report.realized_pnl == 0.0


def test_exactly_zero_pnl_is_not_counted_as_a_win():
    db = Db(); seed(db); close_trade(db, "BTCUSDT", 0.0)
    report = TradingReportBuilder(db, cfg(), "run").build("24h")
    assert report.closed_trades == 1
    assert report.wins == 0
    assert report.win_rate == pytest.approx(0.0)


def test_orphaned_trades_are_excluded_and_counted_separately():
    """An unknown result must never be averaged in as a flat trade."""
    db = Db(); seed(db); close_trade(db, "BTCUSDT", 1.0)
    session = db.get_session()
    session.add(TradeLog(
        symbol="ETHUSDT", action="open_long", source="test", reason="test",
        order_link_id="orphan", run_id="run", entry_price=100, size_usdt=100,
        leverage=1, status="orphaned",
    ))
    session.commit(); session.close()
    report = TradingReportBuilder(db, cfg(), "run").build("24h")
    assert report.closed_trades == 1
    assert report.unresolved_trades == 1


def test_active_positions_render_symbol_side_and_pnl():
    db = Db(); seed(db)
    open_trade(db, "BTCUSDT", unrealized=1.2)
    report = TradingReportBuilder(db, cfg(), "run").build("24h")
    assert report.open_position_count == 1
    position = report.open_positions[0]
    assert (position.symbol, position.side) == ("BTCUSDT", "LONG")
    assert position.unrealized_pnl == pytest.approx(1.2)
    assert report.unrealized_pnl == pytest.approx(1.2)


def test_no_active_positions_reports_zero_unrealized_not_unknown():
    db = Db(); seed(db)
    report = TradingReportBuilder(db, cfg(), "run").build("24h")
    assert report.open_position_count == 0
    assert report.unrealized_pnl == 0.0


def test_position_without_valuation_makes_unrealized_unknown_not_partial():
    """A partial sum would understate exposure, so no number is shown."""
    db = Db(); seed(db)
    open_trade(db, "BTCUSDT", unrealized=1.2)
    open_trade(db, "ETHUSDT", unrealized=None)
    report = TradingReportBuilder(db, cfg(), "run").build("24h")
    assert report.open_position_count == 2
    assert report.unrealized_pnl is None
    assert report.unrealized_unavailable_count == 1


def test_unavailable_position_state_is_not_reported_as_zero_positions():
    db = Db(); seed(db)
    builder = TradingReportBuilder(db, cfg(), "run")

    class Boom:
        def get_session(self):
            raise RuntimeError("connection reset")

    builder.db = Boom()
    report = builder.build("24h")
    assert report.position_state_available is False
    assert report.open_position_count is None
    assert "could not be read" in (report.position_state_reason or "")


# --------------------------------------------------------------------------
# Alert lifecycle
# --------------------------------------------------------------------------

def test_transient_disconnect_does_not_immediately_alert():
    """The escalation window must absorb a blip that recovers on its own."""
    db = Db(); seed(db); sent = []
    config = cfg(telegram_alert_escalation_seconds=600)
    monitor = monitor_for(db, config, sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once(); sent.clear()

    session = db.get_session()
    run = session.query(RunMetadata).one()
    run.collector_heartbeat_at = utcnow() - timedelta(hours=1)
    session.commit(); session.close()

    monitor.poll_once()
    assert not any("BOT WARNING" in text for text in texts(sent))


def test_persistent_failure_escalates_once_and_does_not_repeat():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)  # escalation window 0
    monitor._initialize_durable_cursors(); monitor.poll_once(); sent.clear()

    session = db.get_session()
    run = session.query(RunMetadata).one()
    run.collector_heartbeat_at = utcnow() - timedelta(hours=1)
    session.commit(); session.close()

    monitor.poll_once()
    monitor.poll_once()
    monitor.poll_once()
    warnings = [text for text in texts(sent) if "BOT WARNING" in text]
    assert len(warnings) == 1
    assert "collector" in warnings[0]


def test_recovery_emits_exactly_one_useful_message():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once()

    session = db.get_session()
    run = session.query(RunMetadata).one()
    run.collector_heartbeat_at = utcnow() - timedelta(hours=1)
    session.commit(); session.close()
    monitor.poll_once()
    sent.clear()

    session = db.get_session()
    run = session.query(RunMetadata).one()
    run.collector_heartbeat_at = utcnow()
    session.commit(); session.close()
    monitor.poll_once()
    monitor.poll_once()
    recovered = [text for text in texts(sent) if "BOT RECOVERED" in text]
    assert len(recovered) == 1


def test_raw_websocket_telemetry_never_reaches_telegram():
    """Engineering events stay in operational_health_events, not in the chat."""
    from storage.models import OperationalHealthEvent
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once(); sent.clear()
    session = db.get_session()
    session.add(OperationalHealthEvent(
        event_key="k1", run_id="run", observed_at=utcnow(),
        component="market_collector", event_type="websocket_disconnect",
        severity="error", status="disconnected", details={}, policy_epoch=0,
    ))
    session.commit(); session.close()
    monitor.poll_once()
    assert not any("websocket_disconnect" in text for text in texts(sent))


def test_storage_alert_fires_on_threshold_crossing_only(monkeypatch):
    db = Db(); seed(db); sent = []
    usage = {"ratio": .60}
    monkeypatch.setattr(
        "operator_monitor.StorageGuard.status",
        lambda _self: {
            "available": True, "database_bytes": 3_000_000_000,
            "maximum_bytes": 5_000_000_000, "usage_ratio": usage["ratio"],
            "entry_allowed": usage["ratio"] < .85, "reason": None,
        },
    )
    monitor = monitor_for(db, cfg(storage_entry_block_ratio=.70), sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert sum("DATABASE WARNING" in text for text in texts(sent)) == 1
    sent.clear()
    monitor.poll_once(); monitor.poll_once()
    assert not any("DATABASE" in text for text in texts(sent))
    usage["ratio"] = .75
    monitor.poll_once()
    assert sum("DATABASE CRITICAL" in text for text in texts(sent)) == 1


def test_unwritable_database_produces_a_plain_language_pause_message(monkeypatch):
    monkeypatch.setattr(
        "operator_monitor.StorageGuard.status",
        lambda _self: {
            "available": False, "database_bytes": None, "maximum_bytes": None,
            "usage_ratio": None, "entry_allowed": False,
            "reason": "durable database unavailable: OperationalError",
        },
    )
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)
    monitor._initialize_durable_cursors(); sent.clear()
    monitor.poll_once()
    joined = "\n".join(texts(sent))
    assert "TRADING PAUSED" in joined
    assert "cannot safely persist" in joined
    assert "still being monitored" in joined


def test_trade_open_and_close_are_reported_once_and_survive_restart():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once(); sent.clear()
    trade_id = open_trade(db, "BTCUSDT")
    monitor.poll_once(); monitor.poll_once()
    assert sum("Opened BTCUSDT" in text for text in texts(sent)) == 1

    session = db.get_session()
    trade = session.query(TradeLog).filter_by(id=trade_id).one()
    trade.status = "closed"; trade.pnl_usdt = 1.5; trade.exit_reason = "TP"
    trade.closed_at = utcnow()
    session.commit(); session.close()
    sent.clear()
    monitor.poll_once()
    # A fresh monitor object must not replay the same event from the cursor.
    monitor_for(db, sent=sent).poll_once()
    assert sum("Closed BTCUSDT" in text for text in texts(sent)) == 1


def test_failed_telegram_delivery_is_retried_from_durable_state():
    db = Db(); seed(db); delivered = []
    online = {"value": False}

    def flaky(text, buttons=None):
        if not online["value"]:
            return False
        delivered.append(text)
        return True

    monitor = OperatorMonitor(
        db, cfg(), "run", sender=flaky,
        authorized_chat_id=OWNER_CHAT, authorized_user_id=OWNER_USER,
    )
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert delivered == []
    session = db.get_session()
    state = session.query(OperatorMonitorState).one()
    assert state.state_value["pending_messages"]
    session.close()
    online["value"] = True
    monitor.poll_once()
    assert any("monitor started" in text for text in delivered)


def test_hourly_report_is_sent_once_per_interval():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert sum("TRADING REPORT" in text for text in texts(sent)) == 1
    sent.clear()
    monitor.poll_once(); monitor.poll_once()
    assert not any("TRADING REPORT" in text for text in texts(sent))


# --------------------------------------------------------------------------
# Control: pause / resume
# --------------------------------------------------------------------------

class FakeRisk:
    def __init__(self, causes=None):
        self.causes = dict(causes or {})
        self.tripped = []

    def trip_circuit_breaker(self, reason, sticky=False, cause="unspecified", **kwargs):
        self.causes[cause] = {"reason": reason, "sticky": sticky}
        self.tripped.append(cause)

    def resolve_breaker_cause(self, cause):
        return self.causes.pop(cause, None) is not None


def healthy_status(**overrides):
    base = dict(
        state=ops.HEALTHY, database_available=True, trader_healthy=True,
        collector_healthy=True, market_data_age_seconds=5.0,
        position_state_available=True, outbox={}, breaker_causes={},
    )
    base.update(overrides)
    return ops.OperationalStatus(**base)


def test_telegram_pause_is_a_request_and_never_mutates_risk_state_directly():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent, updates=[message_update("/pause")])
    monitor._initialize_durable_cursors(); sent.clear()
    monitor.poll_once()

    session = db.get_session()
    assert session.query(RiskState).one().circuit_breaker_causes == {}
    command = session.query(OperatorControlCommand).one()
    assert (command.command, command.state) == (PAUSE, "pending")
    assert command.requested_by == OWNER_USER
    session.close()
    assert any("Pause requested" in text for text in texts(sent))


def test_pause_applied_by_the_trading_process_blocks_entries():
    db = Db(); seed(db)
    store = OperatorControlStore(db); risk = FakeRisk()
    store.request(PAUSE, requested_by=OWNER_USER)
    applier = OperatorControlApplier(store, risk, healthy_status)
    outcomes = applier.apply_pending()
    assert [item.state for item in outcomes] == [APPLIED]
    assert ops.OPERATOR_PAUSE_CAUSE in risk.causes
    assert risk.causes[ops.OPERATOR_PAUSE_CAUSE]["sticky"] is True
    assert applier.apply_pending() == []  # never applied twice


def test_resume_cannot_bypass_health_checks():
    db = Db(); seed(db)
    store = OperatorControlStore(db)
    risk = FakeRisk({ops.OPERATOR_PAUSE_CAUSE: {"reason": "paused"}})
    store.request(RESUME, requested_by=OWNER_USER)
    stale = healthy_status(state=ops.DEGRADED, market_data_age_seconds=4000.0)
    outcome = OperatorControlApplier(store, risk, lambda: stale).apply_pending()[0]
    assert outcome.state == REJECTED
    assert "market data is stale" in outcome.outcome
    assert ops.OPERATOR_PAUSE_CAUSE in risk.causes  # still paused


def test_resume_never_clears_an_unrelated_circuit_breaker_cause():
    """An owner pause must not double as an amnesty for a real safety cause."""
    failures = resume_preconditions(healthy_status(breaker_causes={
        ops.OPERATOR_PAUSE_CAUSE: {"reason": "paused"},
        "orphan:abc": {"reason": "unknown result"},
    }))
    assert any("orphan:abc" in item for item in failures)


def test_resume_is_applied_when_every_precondition_passes():
    db = Db(); seed(db)
    store = OperatorControlStore(db)
    risk = FakeRisk({ops.OPERATOR_PAUSE_CAUSE: {"reason": "paused"}})
    store.request(RESUME, requested_by=OWNER_USER)
    status = healthy_status(breaker_causes={
        ops.OPERATOR_PAUSE_CAUSE: {"reason": "paused"}
    })
    outcome = OperatorControlApplier(store, risk, lambda: status).apply_pending()[0]
    assert outcome.state == APPLIED
    assert risk.causes == {}


def test_resume_outcome_is_reported_back_to_the_owner():
    db = Db(); seed(db); sent = []
    monitor = monitor_for(db, sent=sent)
    monitor._initialize_durable_cursors(); monitor.poll_once(); sent.clear()
    store = OperatorControlStore(db)
    command_id = store.request(RESUME, requested_by=OWNER_USER)
    store.finish(command_id, REJECTED, "Resume refused — safety checks did not pass")
    monitor.poll_once()
    assert any("Resume refused" in text for text in texts(sent))
    sent.clear()
    monitor.poll_once()
    assert not any("Resume refused" in text for text in texts(sent))


def test_resume_button_from_the_owner_is_accepted():
    db = Db(); seed(db); sent = []; acks = []
    monitor = monitor_for(
        db, sent=sent, acks=acks, updates=[callback_update("resume")]
    )
    monitor._initialize_durable_cursors(); sent.clear()
    monitor.poll_once()
    pending = monitor.control.pending()
    assert [row.command for row in pending] == [RESUME]
    assert acks == [("cb1", "")]


def test_duplicate_request_is_not_queued_twice():
    db = Db(); seed(db)
    store = OperatorControlStore(db)
    first = store.request(PAUSE, requested_by=OWNER_USER)
    second = store.request(PAUSE, requested_by=OWNER_USER)
    assert first == second
    assert len(store.pending()) == 1


# --------------------------------------------------------------------------
# Health model
# --------------------------------------------------------------------------

def test_health_snapshot_reports_breaker_without_calling_exchange():
    db = Db(); seed(db, causes={"daily_loss": {"reason": "limit"}})
    monitor = monitor_for(db)
    monitor._initialize_durable_cursors()
    snapshot = monitor.poll_once()
    assert snapshot["state"] == ops.PAUSED
    assert snapshot["entries_allowed"] is False
    assert snapshot["collector_healthy"] is True
    assert snapshot["trader_healthy"] is True


def test_stale_trader_heartbeat_is_reported_as_stopped():
    db = Db(); seed(db)
    session = db.get_session()
    run = session.query(RunMetadata).one()
    run.trader_heartbeat_at = utcnow() - timedelta(hours=1)
    session.commit(); session.close()
    snapshot = monitor_for(db).poll_once()
    assert snapshot["state"] == ops.STOPPED


def test_stale_market_data_is_degraded_not_stopped():
    db = Db(); seed(db, fresh_market_data=False)
    session = db.get_session()
    session.add(Trade(
        symbol="BTCUSDT", trade_id="old", price=100, size=1, side="Buy",
        ts=to_epoch_ms(utcnow() - timedelta(hours=1)),
    ))
    session.commit(); session.close()
    snapshot = monitor_for(db).poll_once()
    assert snapshot["state"] == ops.DEGRADED
    assert any("stopped updating" in reason for reason in snapshot["reasons"])


def test_commands_report_status_positions_and_health():
    db = Db(); seed(db)
    open_trade(db, "ETHUSDT", unrealized=-2.93)
    sent = []
    monitor = monitor_for(db, sent=sent, updates=[
        message_update("/status", update_id=1),
        message_update("/positions", update_id=2),
        message_update("/health", update_id=3),
        message_update("/report", update_id=4),
    ])
    monitor._initialize_durable_cursors(); sent.clear()
    monitor.poll_once()
    joined = "\n".join(texts(sent))
    assert "HEALTHY" in joined
    assert "ETHUSDT" in joined and "-2.93 USDT" in joined
    assert "Trader heartbeat" in joined
    assert "TRADING REPORT" in joined
