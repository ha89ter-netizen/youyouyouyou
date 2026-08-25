"""Regression coverage for forensic execution/infrastructure defects.

These tests deliberately avoid strategy scoring and exchange mutation.
"""

from datetime import timedelta
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import BybitConfig
from execution.execution_engine import FillStatus, OrderConfirmation
from execution.reconciliation import MATCHED, NOT_FOUND, plan_closed_pnl_reconciliation
from storage.durability import (
    DurableOutbox, EntryIntentStore, StorageGuard, apply_high_frequency_retention,
)
from storage.journal import TradeJournal
from storage.models import (
    Base, EntryIntent, FundingRate, FundingRateMinuteRollup,
    NormalizedExecution, OpenInterest, OpenInterestMinuteRollup,
    OperationalHealthEvent, ReconciliationAnomaly, TelemetryOutbox, Trade,
    TradeExitEvent, TradeLog,
    TradeExchangeOrder, TradeProtectionEvent,
)
from storage.telemetry import TelemetryStore
from strategy.engine import StrategyEngine
from timeutils import to_epoch_ms, utcnow


class Db:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def config(run_id="durability-run"):
    value = BybitConfig(api_key="x", api_secret="y")
    value.run_id = run_id
    value.commit_sha = "c" * 40
    value.telemetry_retry_attempts = 1
    value.telemetry_retry_base_seconds = 0.001
    value.telemetry_outbox_max_attempts = 3
    value.storage_max_database_bytes = 0
    value.raw_trades_retention_hours = 168
    value.orderbook_retention_hours = 168
    value.liquidations_retention_hours = 720
    value.funding_raw_retention_hours = 24
    value.open_interest_raw_retention_hours = 24
    return value


def prepare(store, evaluation="evaluation-1"):
    return store.prepare(
        evaluation_id=evaluation, symbol="SOLUSDT", side="open_short",
        requested_quantity=1.5, requested_notional=100,
        proposed_entry=75, proposed_stop_loss=76, proposed_take_profit=73,
        policy_epoch=0, config_hash="hash",
        structured_payload={"decision_id": evaluation},
    )


VALID_PREFIXES = [
    ("submitted", ["submitted"]),
    ("accepted", ["submitted", "accepted"]),
    ("partially_filled", ["submitted", "accepted", "partially_filled"]),
    ("filled", ["submitted", "accepted", "filled"]),
    ("journaled", ["submitted", "accepted", "filled", "journaled"]),
    ("closed", ["submitted", "accepted", "filled", "journaled", "closed"]),
    ("reconciled", ["submitted", "accepted", "filled", "journaled", "closed", "reconciled"]),
    ("rejected", ["submitted", "rejected"]),
]


@pytest.mark.parametrize("expected,states", VALID_PREFIXES)
def test_entry_intent_valid_state_transitions(expected, states):
    db = Db()
    store = EntryIntentStore(db, config())
    intent = prepare(store)
    for state in states:
        store.transition(intent.intent_id, state)
    session = db.get_session()
    assert session.query(EntryIntent).one().state == expected
    session.close()


@pytest.mark.parametrize("start,invalid", [
    ("prepared", "accepted"), ("prepared", "filled"), ("submitted", "filled"),
    ("submitted", "journaled"), ("accepted", "closed"),
    ("partially_filled", "closed"), ("filled", "closed"),
    ("journaled", "reconciled"), ("closed", "filled"),
    ("reconciled", "journaled"), ("rejected", "submitted"),
])
def test_entry_intent_rejects_invalid_transitions(start, invalid):
    db = Db()
    store = EntryIntentStore(db, config())
    intent = prepare(store)
    path = [] if start == "prepared" else dict(VALID_PREFIXES)[start]
    for state in path:
        store.transition(intent.intent_id, state)
    with pytest.raises(RuntimeError, match="invalid entry intent transition"):
        store.transition(intent.intent_id, invalid)


def test_entry_intent_is_durable_and_duplicate_prepare_never_resubmits():
    db = Db()
    first = EntryIntentStore(db, config())
    created = prepare(first)
    restarted = EntryIntentStore(db, config())
    duplicate = prepare(restarted)
    assert created.created is True
    assert duplicate.created is False
    assert duplicate.order_link_id == created.order_link_id
    assert duplicate.state == "prepared"


def test_definitively_rejected_intent_is_terminal_but_not_a_restart_blocker():
    db = Db(); store = EntryIntentStore(db, config()); intent = prepare(store)
    store.transition(intent.intent_id, "submitted")
    store.transition(intent.intent_id, "rejected", last_error="Bybit rejected")
    assert store.blocking_intent("SOLUSDT") is None
    assert store.unresolved() == []


def test_repeated_partial_fill_updates_quantity_without_duplicate_intent():
    db = Db(); store = EntryIntentStore(db, config()); intent = prepare(store)
    store.transition(intent.intent_id, "submitted")
    store.transition(intent.intent_id, "accepted")
    store.transition(intent.intent_id, "partially_filled", filled_quantity=0.5,
                     weighted_entry=75)
    assert store.transition(intent.intent_id, "partially_filled", filled_quantity=1.0,
                            weighted_entry=75.1) is True
    session = db.get_session(); row = session.query(EntryIntent).one()
    assert float(row.filled_quantity) == 1.0
    assert float(row.weighted_entry) == 75.1
    session.close()


def test_restart_recovers_filled_prejournal_intent_from_exchange_evidence():
    db = Db(); cfg = config(); intents = EntryIntentStore(db, cfg)
    intent = prepare(intents); intents.transition(intent.intent_id, "submitted")
    engine = object.__new__(StrategyEngine)
    engine.cfg = cfg; engine.entry_intents = intents; engine.journal = TradeJournal(db)
    engine.telemetry = SimpleNamespace(record_health=lambda *args, **kwargs: True)
    fill_time = str(to_epoch_ms(utcnow()))
    engine.execution = SimpleNamespace(
        confirm_order=lambda symbol, link: OrderConfirmation(
            status=FillStatus.FILLED, filled_qty=1.5, avg_price=75,
            detail="filled", raw={"orderId": "entry-order"},
        ),
        get_executions=lambda symbol, order_link_id: [{
            "execId": "entry-exec", "orderId": "entry-order", "execTime": fill_time,
            "execPrice": "75", "execQty": "1.5", "side": "Sell",
        }],
        get_order_fee_usdt=lambda symbol, link: 0.02,
    )
    readbacks = []
    engine._record_initial_protection_readback = lambda *args, **kwargs: readbacks.append(
        (args, kwargs)
    ) or True
    assert engine._recover_unjournaled_entry_intents() == 1
    assert engine._recover_unjournaled_entry_intents() == 0
    session = db.get_session(); trade = session.query(TradeLog).one()
    assert trade.run_id == cfg.run_id
    assert trade.order_link_id == intent.order_link_id
    assert trade.exchange_entry_order_id == "entry-order"
    assert float(trade.entry_filled_qty) == 1.5
    assert session.query(NormalizedExecution).count() == 1
    stored_intent = session.query(EntryIntent).one()
    assert stored_intent.state == "journaled"
    assert stored_intent.trade_log_id == trade.id
    assert readbacks[0][1]["owner_run_id"] == cfg.run_id
    session.close()


def test_submitted_entry_intent_blocks_same_symbol_after_restart():
    db = Db(); first = EntryIntentStore(db, config())
    intent = prepare(first); first.transition(intent.intent_id, "submitted")
    restarted = EntryIntentStore(db, config())
    blocked = restarted.blocking_intent("SOLUSDT")
    assert blocked["order_link_id"] == intent.order_link_id
    assert blocked["state"] == "submitted"
    assert restarted.blocking_intent("ETHUSDT") is None


def test_entry_intent_order_link_is_bybit_safe_and_deterministic():
    first = prepare(EntryIntentStore(Db(), config()))
    second = prepare(EntryIntentStore(Db(), config()))
    assert first.order_link_id == second.order_link_id
    assert len(first.order_link_id) <= 36
    assert first.order_link_id.startswith("intent-")


@pytest.mark.parametrize("event_type", ["decision", "protection", "health", "exit"])
def test_outbox_enqueue_is_idempotent_for_critical_event_types(event_type):
    db = Db()
    outbox = DurableOutbox(db, "run", max_attempts=3, base_backoff_seconds=0.001)
    key = outbox.enqueue(event_type, {"event": event_type}, event_key=f"key-{event_type}")
    outbox.enqueue(event_type, {"event": event_type}, event_key=key)
    session = db.get_session()
    assert session.query(TelemetryOutbox).filter_by(event_key=key).count() == 1
    session.close()


def test_outbox_replay_survives_new_store_instance():
    db = Db()
    DurableOutbox(db, "run").enqueue("health", {"x": 1}, event_key="event")
    restarted = DurableOutbox(db, "run")
    row = restarted.pending()[0]
    delivered = []
    assert restarted.deliver(row.id, lambda _s, kind, payload, key: delivered.append((kind, payload, key)))
    assert delivered == [("health", {"x": 1}, "event")]
    assert restarted.status("event") == "delivered"


def test_outbox_failure_uses_bounded_exponential_retry_then_dead_letter():
    db = Db()
    outbox = DurableOutbox(db, "run", max_attempts=2, base_backoff_seconds=0.001)
    outbox.enqueue("health", {"x": 1}, event_key="event")
    row = outbox.pending()[0]
    assert not outbox.deliver(row.id, lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    session = db.get_session()
    queued = session.query(TelemetryOutbox).one()
    queued.next_attempt_at = utcnow() - timedelta(seconds=1)
    session.commit()
    session.close()
    row = outbox.pending()[0]
    assert not outbox.deliver(row.id, lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    assert outbox.status("event") == "dead_letter"


def test_telemetry_failure_breadcrumb_buffer_is_hard_bounded():
    store = TelemetryStore(Db(), config())
    for index in range(250):
        store._pending_health.append({"observed_at": utcnow(), "error": str(index)})
    assert len(store._pending_health) == 100
    assert store._pending_health[-1]["error"] == "249"


def test_health_outbox_delivers_exactly_once_after_restart():
    db = Db()
    first = TelemetryStore(db, config())
    assert first.record_health("db", "recovered", "info", "ok")
    restarted = TelemetryStore(db, config())
    restarted.replay_outbox()
    session = db.get_session()
    assert session.query(OperationalHealthEvent).count() == 1
    assert session.query(TelemetryOutbox).filter_by(status="delivered").count() == 1
    session.close()


def test_health_events_are_deduplicated_and_report_suppressed_count():
    db = Db(); cfg = config(); cfg.health_event_dedup_window_seconds = 60
    store = TelemetryStore(db, cfg); clock = [0.0]; store._monotonic = lambda: clock[0]
    error = RuntimeError("same transport failure")
    assert store.record_health("collector", "reconnect", "error", "failed", error=error)
    for _ in range(10):
        assert not store.record_health("collector", "reconnect", "error", "failed", error=error)
    clock[0] = 61
    assert store.record_health("collector", "reconnect", "error", "failed", error=error)
    session = db.get_session(); rows = session.query(OperationalHealthEvent).order_by(
        OperationalHealthEvent.id
    ).all()
    assert len(rows) == 2
    assert rows[-1].details["suppressed_identical_events"] == 10
    session.close()


def test_health_condition_persists_enter_reminder_and_recovery_only():
    db = Db(); cfg = config(); cfg.health_condition_reminder_seconds = 900
    store = TelemetryStore(db, cfg); clock = [0.0]; store._monotonic = lambda: clock[0]
    started = utcnow()
    assert store.record_health_condition(
        "market_data", "stale_orderbook", active=True, symbol="SOLUSDT",
        observed_at=started,
    )
    for second in range(1, 101):
        clock[0] = second
        assert not store.record_health_condition(
            "market_data", "stale_orderbook", active=True, symbol="SOLUSDT",
            observed_at=started + timedelta(seconds=second),
        )
    clock[0] = 901
    assert store.record_health_condition(
        "market_data", "stale_orderbook", active=True, symbol="SOLUSDT",
        observed_at=started + timedelta(seconds=901),
    )
    clock[0] = 902
    assert store.record_health_condition(
        "market_data", "stale_orderbook", active=False, symbol="SOLUSDT",
        observed_at=started + timedelta(seconds=902),
    )
    session = db.get_session()
    rows = session.query(OperationalHealthEvent).order_by(OperationalHealthEvent.id).all()
    assert [row.event_type for row in rows] == [
        "stale_orderbook", "stale_orderbook", "stale_orderbook_recovered",
    ]
    assert rows[1].details["suppressed_identical_events"] == 100
    assert rows[2].details["condition_transition"] == "recovered"
    session.close()

    clock[0] = 903
    assert store.record_health_condition(
        "market_data", "stale_orderbook", active=True, symbol="SOLUSDT",
        observed_at=started + timedelta(seconds=903),
    )
    session = db.get_session()
    assert session.query(OperationalHealthEvent).count() == 4
    session.close()


def test_outbox_cleanup_deletes_only_old_confirmed_deliveries_in_batches():
    db = Db(); outbox = DurableOutbox(db, "run")
    session = db.get_session(); old = utcnow() - timedelta(hours=48)
    session.add_all([
        TelemetryOutbox(event_key="old-1", run_id="run", event_type="health", payload={},
                        status="delivered", attempts=0, next_attempt_at=old,
                        created_at=old, delivered_at=old),
        TelemetryOutbox(event_key="old-2", run_id="run", event_type="health", payload={},
                        status="delivered", attempts=0, next_attempt_at=old,
                        created_at=old, delivered_at=old),
        TelemetryOutbox(event_key="pending", run_id="run", event_type="health", payload={},
                        status="pending", attempts=0, next_attempt_at=old, created_at=old),
        TelemetryOutbox(event_key="failed", run_id="run", event_type="health", payload={},
                        status="dead_letter", attempts=8, next_attempt_at=old, created_at=old),
    ]); session.commit(); session.close()
    assert outbox.cleanup_delivered(retention_hours=24, batch_size=1)["deleted"] == 1
    restarted = DurableOutbox(db, "run")
    metrics = restarted.metrics()
    assert metrics == pytest.approx({
        "pending": 1, "delivered": 1, "failed": 1,
        "oldest_pending_age_seconds": metrics["oldest_pending_age_seconds"],
    })
    assert restarted.cleanup_delivered(retention_hours=24, batch_size=100)["deleted"] == 1
    session = db.get_session()
    assert {row.event_key for row in session.query(TelemetryOutbox).all()} == {"pending", "failed"}
    session.close()


def test_outbox_cleanup_is_scheduled_once_off_the_trading_thread():
    store = TelemetryStore(Db(), config())
    entered = threading.Event(); release = threading.Event()
    def slow_batch():
        entered.set(); release.wait(timeout=2); return {}
    store.maintain_outbox = slow_batch
    store.record_health = lambda *args, **kwargs: True
    assert store.schedule_outbox_maintenance() is True
    assert entered.wait(timeout=1)
    assert store.schedule_outbox_maintenance() is False
    release.set()
    store._outbox_maintenance_thread.join(timeout=1)
    assert not store._outbox_maintenance_thread.is_alive()


def test_outbox_maintenance_catches_up_multiple_bounded_batches():
    db = Db(); cfg = config()
    cfg.telemetry_outbox_delivered_retention_hours = 24
    cfg.telemetry_outbox_cleanup_batch_size = 2
    cfg.telemetry_outbox_cleanup_max_batches = 3
    old = utcnow() - timedelta(hours=48)
    session = db.get_session()
    session.add_all([
        TelemetryOutbox(
            event_key=f"old-{index}", run_id=cfg.run_id, event_type="health",
            payload={}, status="delivered", attempts=0, next_attempt_at=old,
            created_at=old, delivered_at=old,
        )
        for index in range(5)
    ])
    session.commit(); session.close()
    result = TelemetryStore(db, cfg).maintain_outbox()
    assert result["cleanup"]["deleted"] == 5
    assert result["cleanup"]["batches"] == 3
    assert result["after"]["delivered"] == 0


def test_recent_closed_position_visibility_lag_is_not_unresolved_or_attached():
    db = Db(); cfg = config(); cfg.position_close_visibility_grace_seconds = 120
    journal = TradeJournal(db)
    journal.log_entry(
        "SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
        "entry", run_id=cfg.run_id, entry_filled_qty=1,
        stop_loss_price=98, take_profit_price=104,
    )
    closed_at = utcnow()
    journal.log_exit("entry", 101, 1, closed_at=closed_at)
    store = TelemetryStore(db, cfg)
    assert store.persist_position_snapshots([{
        "symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
        "markPrice": "101", "stopLoss": "98", "takeProfit": "104",
    }], observed_at=closed_at + timedelta(seconds=5)) == 0
    session = db.get_session()
    events = session.query(OperationalHealthEvent).all()
    assert [event.event_type for event in events] == ["position_close_visibility_lag"]
    assert events[0].details["trade_log_id"] == 1
    session.close()


def test_storage_guard_allows_healthy_database():
    status = StorageGuard(Db(), config()).status()
    assert status["available"] is True
    assert status["entry_allowed"] is True


def test_storage_guard_fails_closed_when_session_creation_fails():
    db = Db()
    db.get_session = lambda: (_ for _ in ()).throw(RuntimeError("recovery mode"))
    status = StorageGuard(db, config()).status()
    assert status["available"] is False
    assert status["entry_allowed"] is False
    assert "unavailable" in status["reason"]


@pytest.mark.parametrize("table", [
    "trades", "orderbook_snapshots", "liquidations", "funding_rate", "open_interest",
])
def test_retention_policy_scope_excludes_audit_tables(table):
    # Policy declaration itself is intentionally allowlisted; this catches a
    # future broad "delete all old rows" implementation.
    from inspect import getsource
    source = getsource(apply_high_frequency_retention)
    assert f'"{table}"' in source
    for protected in ("trade_log", "trade_exit_events", "entry_intents", "telemetry_outbox"):
        assert f'"{protected}"' not in source


def test_retention_deletes_old_public_trades_but_preserves_recent_rows():
    db = Db()
    now_ms = to_epoch_ms(utcnow())
    session = db.get_session()
    session.add_all([
        Trade(symbol="SOLUSDT", trade_id="old", ts=now_ms - 8 * 24 * 3600_000,
              side="Buy", price=1, size=1),
        Trade(symbol="SOLUSDT", trade_id="new", ts=now_ms, side="Buy", price=1, size=1),
    ])
    session.commit(); session.close()
    apply_high_frequency_retention(db.engine, config())
    session = db.get_session()
    assert [row.trade_id for row in session.query(Trade).all()] == ["new"]
    session.close()


def test_retention_preserves_raw_ticks_needed_by_old_open_trade():
    db = Db(); cfg = config(); now = utcnow(); now_ms = to_epoch_ms(now)
    session = db.get_session()
    session.add(TradeLog(
        symbol="SOLUSDT", action="open_long", source="test", reason="test",
        order_link_id="old-open", run_id=cfg.run_id, entry_price=100,
        size_usdt=100, leverage=1, status="open", opened_at=now - timedelta(days=10),
    ))
    session.add(Trade(symbol="SOLUSDT", trade_id="during-open",
                      ts=now_ms - 9 * 24 * 3600_000, side="Buy", price=100, size=1))
    session.commit(); session.close()
    apply_high_frequency_retention(db.engine, cfg)
    session = db.get_session()
    assert session.query(Trade).filter_by(trade_id="during-open").count() == 1
    session.close()


def test_retention_bounds_ticker_tables_without_deleting_recent_research_data():
    db = Db(); cfg = config(); now_ms = to_epoch_ms(utcnow())
    session = db.get_session()
    session.add_all([
        FundingRate(symbol="SOLUSDT", funding_ts=now_ms - 25 * 3600_000,
                    funding_rate=.0001),
        FundingRate(symbol="SOLUSDT", funding_ts=now_ms, funding_rate=.0002),
        OpenInterest(symbol="SOLUSDT", ts=now_ms - 25 * 3600_000,
                     open_interest=100),
        OpenInterest(symbol="SOLUSDT", ts=now_ms, open_interest=101),
    ])
    session.commit(); session.close()
    deleted = apply_high_frequency_retention(db.engine, cfg)
    assert deleted["funding_rate"] == 1
    assert deleted["open_interest"] == 1
    session = db.get_session()
    assert session.query(FundingRate).count() == 1
    assert session.query(OpenInterest).count() == 1
    funding_archive = session.query(FundingRateMinuteRollup).one()
    oi_archive = session.query(OpenInterestMinuteRollup).one()
    assert funding_archive.sample_count == 1
    assert oi_archive.sample_count == 1
    session.close()


def test_long_requested_exit_reason_and_structured_payload_are_not_truncated():
    db = Db(); cfg = config(); store = TelemetryStore(db, cfg)
    journal = TradeJournal(db)
    journal.log_entry("SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
                      "entry-link", run_id=cfg.run_id, entry_filled_qty=1,
                      stop_loss_price=99, take_profit_price=102)
    session = db.get_session()
    trade = session.query(TradeLog).one()
    reason = "structured-exit:" + "x" * 150_000
    trade.exit_trigger = {"reason": reason, "families": ["trend", "flow"]}
    session.commit(); session.close()
    record = {"orderId": "exit", "avgExitPrice": "101", "updatedTime": str(to_epoch_ms(utcnow()))}
    assert store.finalize_trade(
        "entry-link", actual_exit_reason="exit_manager", records=[record],
        executions_by_order={"exit": []}, realized_pnl=1, fees=0.1,
    )
    session = db.get_session()
    event = session.query(TradeExitEvent).one()
    assert event.requested_exit_reason == reason
    assert event.exit_manager_signal["families"] == ["trend", "flow"]
    assert event.raw_payload["requested_exit_reason"] == reason
    session.close()


def _trade(link="entry", entry=100, qty=1):
    return {"order_link_id": link, "symbol": "SOLUSDT", "action": "open_long",
            "entry_price": entry, "entry_filled_qty": qty,
            "size_usdt": entry * qty, "opened_at_ms": 1000}


def _closed(entry=100, qty=1):
    return {"symbol": "SOLUSDT", "orderId": "exit", "side": "Sell",
            "avgEntryPrice": str(entry), "avgExitPrice": "90", "closedSize": str(qty),
            "closedPnl": "-10", "createdTime": "1500", "updatedTime": "2000"}


@pytest.mark.parametrize("field,value,anomaly", [
    ("avgEntryPrice", "130", "entry_price_mismatch"),
    ("symbol", "ETHUSDT", "symbol_mismatch"),
    ("side", "Buy", "side_mismatch"),
    ("updatedTime", "500", "close_before_internal_open"),
])
def test_strong_parent_id_owns_record_but_validation_mismatch_is_retained(field, value, anomaly):
    record = _closed(); record[field] = value
    plan = plan_closed_pnl_reconciliation(
        [_trade()], [record], [{"orderId": "exit", "parentOrderLinkId": "entry"}]
    )[0]
    assert plan["status"] == MATCHED
    assert anomaly in {item["type"] for item in plan["validation_anomalies"]}


def test_strong_parent_id_quantity_mismatch_is_matched_and_flagged():
    plan = plan_closed_pnl_reconciliation(
        [_trade(qty=2)], [_closed(qty=1)],
        [{"orderId": "exit", "parentOrderLinkId": "entry"}],
    )[0]
    assert plan["status"] == MATCHED
    assert "quantity_mismatch" in {item["type"] for item in plan["validation_anomalies"]}


def test_price_mismatch_without_strong_id_remains_unmatched():
    assert plan_closed_pnl_reconciliation([_trade()], [_closed(entry=130)])[0]["status"] == NOT_FOUND


@pytest.mark.parametrize("maker,expected", [(True, "maker"), ("true", "maker"),
                                              (False, "taker"), (None, None)])
def test_normalized_execution_persists_maker_taker_and_deduplicates(maker, expected):
    db = Db(); journal = TradeJournal(db)
    journal.log_entry("SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
                      "entry", run_id="run", entry_filled_qty=1)
    execution = {"execId": "exec", "orderId": "order", "execTime": str(to_epoch_ms(utcnow())),
                 "execPrice": "100", "execQty": "1", "isMaker": maker, "side": "Buy"}
    journal.persist_normalized_executions("entry", [execution, dict(execution)], role="entry")
    session = db.get_session(); row = session.query(NormalizedExecution).one()
    assert row.maker_taker == expected
    session.close()


def test_execution_id_conflict_remains_with_original_owner_and_records_anomaly():
    db = Db(); journal = TradeJournal(db); now_ms = str(to_epoch_ms(utcnow()))
    for link in ("entry-a", "entry-b"):
        journal.log_entry("SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
                          link, run_id="run", entry_filled_qty=1)
    execution = {"execId": "shared-exec", "orderId": "order-a", "execTime": now_ms,
                 "execPrice": "100", "execQty": "1", "side": "Buy"}
    assert journal.persist_normalized_executions("entry-a", [execution], role="entry")
    assert journal.persist_normalized_executions("entry-b", [execution], role="entry") == []
    session = db.get_session()
    evidence = session.query(NormalizedExecution).one()
    owner = session.query(TradeLog).filter_by(order_link_id="entry-a").one()
    assert evidence.trade_log_id == owner.id
    anomaly = session.query(ReconciliationAnomaly).one()
    assert anomaly.anomaly_type == "execution_owner_conflict"
    assert anomaly.severity == "critical"
    session.close()


def test_mfe_sampling_starts_at_first_entry_execution_not_internal_creation():
    db = Db(); cfg = config(); journal = TradeJournal(db); store = TelemetryStore(db, cfg)
    created = utcnow() - timedelta(minutes=10)
    first_fill = created + timedelta(minutes=5)
    journal.log_entry("SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
                      "entry", run_id=cfg.run_id, entry_filled_qty=1,
                      stop_loss_price=98, take_profit_price=104)
    session = db.get_session(); trade = session.query(TradeLog).one(); trade.opened_at = created
    session.add_all([
        Trade(symbol="SOLUSDT", trade_id="pre-fill", ts=to_epoch_ms(first_fill) - 1000,
              side="Buy", price=150, size=1),
        Trade(symbol="SOLUSDT", trade_id="post-fill", ts=to_epoch_ms(first_fill) + 1000,
              side="Buy", price=105, size=1),
    ])
    session.commit(); session.close()
    journal.persist_normalized_executions("entry", [{
        "execId": "entry-exec", "orderId": "entry-order",
        "execTime": str(to_epoch_ms(first_fill)), "execPrice": "100",
        "execQty": "1", "side": "Buy",
    }], role="entry")
    store.persist_position_snapshots([{
        "symbol": "SOLUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
        "markPrice": "105", "lastPrice": "105", "stopLoss": "98", "takeProfit": "104",
    }], observed_at=first_fill + timedelta(seconds=2))
    session = db.get_session()
    from storage.models import TradeExcursion
    excursion = session.query(TradeExcursion).one()
    assert float(excursion.mfe_price) == 105
    assert excursion.time_to_mfe_seconds == 1
    session.close()


@pytest.mark.parametrize("closed_latency_ms", [3, 50, 500, 1000])
def test_holding_duration_uses_entry_to_exit_execution_not_closed_pnl_latency(closed_latency_ms):
    db = Db(); journal = TradeJournal(db)
    opened = utcnow() - timedelta(hours=2)
    journal.log_entry("SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
                      "entry", run_id="run", entry_filled_qty=1)
    session = db.get_session(); trade = session.query(TradeLog).one(); trade.opened_at = opened
    session.commit(); session.close()
    first_fill = opened + timedelta(seconds=10)
    final_fill = first_fill + timedelta(minutes=37, seconds=5)
    closed_created = final_fill - timedelta(milliseconds=closed_latency_ms)
    journal.log_exit(
        "entry", 99, -1, closed_at=closed_created,
        entry_execution_at=first_fill, final_exit_execution_at=final_fill,
    )
    session = db.get_session(); trade = session.query(TradeLog).one()
    assert trade.holding_seconds == 37 * 60 + 5
    assert trade.closed_at == final_fill.replace(tzinfo=None) or trade.closed_at == final_fill
    session.close()


def test_reconciliation_anomaly_persistence_is_idempotent():
    db = Db(); journal = TradeJournal(db)
    journal.log_entry("SOLUSDT", "open_long", "test", "entry", 100, 100, 1, 1, 2,
                      "entry", run_id="run", entry_filled_qty=1)
    anomalies = [{"type": "entry_price_mismatch", "difference_pct": "30"}]
    assert journal.record_reconciliation_anomalies("entry", "exit", anomalies) == 1
    assert journal.record_reconciliation_anomalies("entry", "exit", anomalies) == 0
    session = db.get_session(); assert session.query(ReconciliationAnomaly).count() == 1
    session.close()


class ProtectionTelemetry:
    def __init__(self): self.events = []; self.health = []
    def record_protection_event(self, *args, **kwargs): self.events.append((args, kwargs)); return True
    def record_health(self, *args, **kwargs): self.health.append((args, kwargs)); return True


class ProtectionExecution:
    def __init__(self, orders=None, error=None): self.orders = orders or []; self.error = error
    def get_active_protective_orders(self, _symbol):
        if self.error: raise self.error
        return list(self.orders)


@pytest.mark.parametrize("stop,tp,orders,halted", [
    (99, 102, [
        {"orderId": "sl", "stopOrderType": "StopLoss", "orderStatus": "Untriggered"},
        {"orderId": "tp", "stopOrderType": "TakeProfit", "orderStatus": "Untriggered"},
    ], False),
    (0, 102, [{"orderId": "tp", "stopOrderType": "TakeProfit", "orderStatus": "Untriggered"}], True),
    (99, 0, [{"orderId": "sl", "stopOrderType": "StopLoss", "orderStatus": "Untriggered"}], True),
    (99, 102, [], True),
])
def test_read_only_protection_watchdog_detects_without_mutating(stop, tp, orders, halted):
    engine = object.__new__(StrategyEngine)
    engine.execution = ProtectionExecution(orders)
    engine.telemetry = ProtectionTelemetry()
    engine.journal = SimpleNamespace(get_open_trades=lambda _s: [{
        "symbol": "SOLUSDT", "order_link_id": "entry", "run_id": "run",
    }])
    engine._protection_entry_halt = None
    result = engine._watch_protection([{
        "symbol": "SOLUSDT", "size": "1", "side": "Buy",
        "stopLoss": str(stop), "takeProfit": str(tp),
    }])
    assert result["SOLUSDT"] == orders
    assert bool(engine._protection_entry_halt) is halted
    assert not hasattr(engine.execution, "place_order")


def test_protection_watchdog_api_failure_blocks_only_new_entries():
    engine = object.__new__(StrategyEngine)
    engine.execution = ProtectionExecution(error=RuntimeError("temporary"))
    engine.telemetry = ProtectionTelemetry()
    engine.journal = SimpleNamespace(get_open_trades=lambda _s: [])
    engine._protection_entry_halt = None
    assert engine._watch_protection([{"symbol": "SOLUSDT", "size": "1"}]) == {}
    assert "read-back unavailable" in engine._protection_entry_halt
    assert engine.telemetry.health[0][0][1] == "protective_order_fetch_failure"


def test_reconciliation_failure_does_not_abort_rest_of_open_position_cycle():
    engine = object.__new__(StrategyEngine)
    engine.cfg = SimpleNamespace(
        symbols=[], decision_interval_sec=30, storage_monitor_interval_sec=300,
    )
    engine.storage_guard = None
    events = []
    engine.telemetry = SimpleNamespace(
        replay_outbox=lambda limit=100: {},
        record_health=lambda *args, **kwargs: events.append((args, kwargs)) or True,
        position_snapshot_due=lambda: False,
        account_snapshot_due=lambda: False,
    )
    engine.execution = SimpleNamespace(
        get_open_positions=lambda: [{"symbol": "SOLUSDT", "size": "1"}],
        get_account_state=lambda: {"wallet_balance": 1000},
    )
    managed = []
    engine._watch_protection = lambda positions: managed.append("watchdog") or {}
    engine._resolve_pending_entries = lambda positions: (_ for _ in ()).throw(
        RuntimeError("risk state db down")
    )
    engine._manage_time_range_tightening = lambda positions: managed.append("tightening")
    engine._manage_trailing_stops = lambda positions: managed.append("trailing")
    engine._sync_closed_trades = lambda positions: (_ for _ in ()).throw(RuntimeError("db down"))
    engine.risk_manager = SimpleNamespace(ensure_daily_reset=lambda balance: managed.append("account"))
    engine._execute_ranked_candidates = lambda *args: None
    engine.run_once()
    assert managed == ["watchdog", "tightening", "trailing", "account"]
    assert any(args[1] == "pending_entry_recovery_failure" for args, _ in events)
    assert any(args[1] == "reconciliation_database_failure" for args, _ in events)
