from types import SimpleNamespace
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import BybitConfig
from storage.journal import TradeJournal
from storage.models import (
    Base, TradeExchangeOrder, TradeExitEvent, TradeLog, TradeProtectionEvent,
)
from storage.migrations import TRADE_EXIT_SLIPPAGE_COLUMNS
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


def config():
    cfg = BybitConfig(api_key="x", api_secret="y")
    cfg.run_id = "run"; cfg.commit_sha = "c" * 40
    cfg.telemetry_retry_attempts = 1; cfg.telemetry_retry_base_seconds = .001
    cfg.slippage_elevated_pct = .25; cfg.slippage_anomalous_pct = 1.0
    cfg.slippage_elevated_r = .25; cfg.slippage_anomalous_r = .75
    cfg.protective_trigger_by = "LastPrice"
    cfg.protective_quarantine_seconds = 3600
    cfg.protective_anomaly_sticky_count = 2
    return cfg


def test_trigger_evidence_quality_schema_accepts_full_structured_label():
    label = "static_trigger_price_confirmed_timestamp_unavailable"
    assert TradeExitEvent.__table__.c.trigger_evidence_quality.type.length >= len(label)
    assert TRADE_EXIT_SLIPPAGE_COLUMNS["trigger_evidence_quality"] == "VARCHAR(100)"


def finalized_exit(fill, trigger=98, stop_type="StopLoss"):
    db = Db(); cfg = config(); journal = TradeJournal(db); store = TelemetryStore(db, cfg)
    journal.log_entry(
        "TESTUSDT", "open_long", "test", "entry", 100, 100, 1, 2, 4,
        "entry-link", run_id=cfg.run_id, entry_filled_qty=1,
        stop_loss_price=98, take_profit_price=104,
    )
    session = db.get_session(); trade = session.query(TradeLog).one()
    session.add(TradeExchangeOrder(
        trade_log_id=trade.id, internal_order_link_id="entry-link",
        exchange_order_id="close-order", role="protective",
        order_status="Untriggered", stop_order_type=stop_type,
        trigger_price=trigger, raw_payload={"triggerBy": "LastPrice"},
    )); session.commit(); session.close()
    timestamp = str(to_epoch_ms(utcnow()))
    assert store.finalize_trade(
        "entry-link", actual_exit_reason="SL",
        records=[{"orderId": "close-order", "avgExitPrice": str(fill),
                  "updatedTime": timestamp}],
        executions_by_order={"close-order": [{
            "execId": "close-exec", "orderId": "close-order",
            "execTime": timestamp, "execPrice": str(fill), "execQty": "1",
            "stopOrderType": stop_type,
        }]}, realized_pnl=fill - 100, fees=.1,
    )
    return db, store


@pytest.mark.parametrize("fill,expected", [
    (98.0, "normal"), (97.4, "elevated"), (95.5, "anomalous"),
])
def test_trigger_to_fill_slippage_classification(fill, expected):
    db, _ = finalized_exit(fill)
    session = db.get_session(); event = session.query(TradeExitEvent).one()
    assert event.slippage_classification == expected
    assert float(event.intended_trigger_price) == 98
    assert float(event.actual_fill_price) == fill
    assert event.trigger_source == "LastPrice"
    assert event.trigger_evidence_quality == "static_trigger_price_confirmed_timestamp_unavailable"
    assert event.protective_execution_id == "close-exec"
    session.close()


def test_protection_lifecycle_reaches_terminal_states_once_and_is_idempotent():
    db, store = finalized_exit(97.5)
    # Repeated reconciliation is rejected by the immutable final-exit key.
    timestamp = str(to_epoch_ms(utcnow()))
    assert not store.finalize_trade(
        "entry-link", actual_exit_reason="SL",
        records=[{"orderId": "close-order", "avgExitPrice": "97.5",
                  "updatedTime": timestamp}],
        executions_by_order={"close-order": []}, realized_pnl=-2.5, fees=.1,
    )
    session = db.get_session()
    assert session.query(TradeExitEvent).count() == 1
    events = [row.event_type for row in session.query(TradeProtectionEvent).all()]
    assert sorted(events) == sorted(["triggered", "filled", "position_closed", "reconciled"])
    order = session.query(TradeExchangeOrder).one()
    assert order.order_status == "Filled"
    assert order.role == "protective_exit"
    session.close()


@pytest.mark.parametrize("classification,tripped", [
    ("normal", False), ("elevated", False), ("anomalous", True),
])
def test_slippage_breaker_only_blocks_new_entries_for_anomalous_fill(classification, tripped):
    engine = object.__new__(StrategyEngine)
    health = []; breaker = []
    engine.cfg = SimpleNamespace(max_realized_loss_r=1.5)
    engine.telemetry = SimpleNamespace(
        get_exit_slippage=lambda _link: {
            "classification": classification, "slippage_pct": 2,
            "slippage_r": 1, "realized_r": 0.25,
            "exchange_order_id": "order",
        },
        record_health=lambda *args, **kwargs: health.append((args, kwargs)),
    )
    engine.risk_manager = SimpleNamespace(
        trip_circuit_breaker=lambda *args, **kwargs: breaker.append((args, kwargs))
    )
    assert engine._apply_protective_slippage_breaker("link", "TESTUSDT") is tripped
    assert bool(breaker) is tripped
    if tripped:
        assert breaker[0][1]["sticky"] is False
        assert breaker[0][1]["expires_at"] is not None
        assert breaker[0][1]["cause"] == "protective_slippage:link"
        assert health[0][1]["details"]["existing_position_management_continues"] is True


@pytest.mark.parametrize("classification,realized_r,tripped", [
    ("unavailable", -1.49, False),
    ("normal", -1.50, True),
    ("unavailable", -2.73, True),
])
def test_realized_loss_envelope_is_independent_of_exit_type(
    classification, realized_r, tripped,
):
    engine = object.__new__(StrategyEngine)
    health = []; breaker = []
    engine.cfg = SimpleNamespace(max_realized_loss_r=1.5)
    engine.telemetry = SimpleNamespace(
        get_exit_slippage=lambda _link: {
            "classification": classification,
            "realized_r": realized_r,
            "realized_pnl": -3,
            "actual_exit_reason": "exit_manager",
        },
        record_health=lambda *args, **kwargs: health.append((args, kwargs)),
    )
    engine.risk_manager = SimpleNamespace(
        trip_circuit_breaker=lambda *args, **kwargs: breaker.append((args, kwargs))
    )
    assert engine._apply_protective_slippage_breaker("link", "TESTUSDT") is tripped
    assert bool(breaker) is tripped
    if tripped:
        assert breaker[0][1]["cause"] == "exit_risk_envelope:link"
        assert breaker[0][1]["sticky"] is True
        assert health[0][0][1] == "realized_loss_risk_envelope_breach"
        assert health[0][1]["details"]["existing_position_management_continues"] is True


def test_second_active_execution_anomaly_escalates_to_sticky_breaker():
    engine = object.__new__(StrategyEngine); breaker = []
    engine.cfg = SimpleNamespace(
        max_realized_loss_r=1.5, protective_quarantine_seconds=3600,
        protective_anomaly_sticky_count=2,
    )
    engine.telemetry = SimpleNamespace(
        get_exit_slippage=lambda _link: {
            "classification": "anomalous", "slippage_pct": 2,
            "slippage_r": 1, "realized_r": .2,
        },
        record_health=lambda *args, **kwargs: True,
    )
    engine.risk_manager = SimpleNamespace(
        breaker_causes=lambda: {"first": {
            "category": "protective_execution_anomaly", "sticky": False,
        }},
        trip_circuit_breaker=lambda *args, **kwargs: breaker.append(kwargs),
    )
    assert engine._apply_protective_slippage_breaker("second", "TESTUSDT")
    assert breaker[0]["sticky"] is True
    assert breaker[0]["expires_at"] is None


def test_trailing_activation_price_is_not_misclassified_as_fill_slippage():
    db, store = finalized_exit(105, trigger=98, stop_type="TrailingStop")
    session = db.get_session(); event = session.query(TradeExitEvent).one()
    assert event.slippage_classification == "unavailable"
    assert event.slippage_absolute is None
    assert event.slippage_r is None
    assert event.trigger_evidence_quality == "trailing_dynamic_trigger_unavailable"
    session.close()
    evidence = store.get_exit_slippage("entry-link")
    assert evidence["classification"] == "unavailable"
    assert evidence["exchange_exit_mechanism"] == "TrailingStop"


def test_legacy_profitable_trailing_false_positive_is_safely_reclassified():
    engine = object.__new__(StrategyEngine); resolved = []; health = []; tripped = []
    engine.cfg = SimpleNamespace(max_realized_loss_r=1.5, protective_quarantine_seconds=3600)
    engine.risk_manager = SimpleNamespace(
        breaker_causes=lambda: {"protective_slippage:legacy": {
            "reason": "old classifier", "sticky": True, "category": None,
        }},
        resolve_breaker_cause=lambda cause: resolved.append(cause) or True,
        trip_circuit_breaker=lambda *args, **kwargs: tripped.append(kwargs),
    )
    engine.telemetry = SimpleNamespace(
        get_exit_slippage=lambda _link: {
            "exchange_exit_mechanism": "TrailingStop", "realized_r": 1.03,
            "fill_at": utcnow() - timedelta(hours=2),
        },
        record_health=lambda *args, **kwargs: health.append((args, kwargs)),
    )
    assert engine._reclassify_legacy_trailing_slippage_breakers() == 1
    assert resolved == ["protective_slippage:legacy"]
    assert tripped == []  # historical quarantine already elapsed
    assert health[0][0][1] == "legacy_trailing_breaker_reclassified"


def test_legacy_trailing_breaker_is_not_cleared_if_realized_loss_breached_envelope():
    engine = object.__new__(StrategyEngine); resolved = []
    engine.cfg = SimpleNamespace(max_realized_loss_r=1.5, protective_quarantine_seconds=3600)
    engine.risk_manager = SimpleNamespace(
        breaker_causes=lambda: {"protective_slippage:legacy": {
            "reason": "old classifier", "sticky": True, "category": None,
        }},
        resolve_breaker_cause=lambda cause: resolved.append(cause) or True,
    )
    engine.telemetry = SimpleNamespace(get_exit_slippage=lambda _link: {
        "exchange_exit_mechanism": "TrailingStop", "realized_r": -2.0,
        "fill_at": utcnow() - timedelta(hours=2),
    })
    assert engine._reclassify_legacy_trailing_slippage_breakers() == 0
    assert resolved == []


def test_exit_evidence_exposes_realized_r_for_risk_envelope():
    db, store = finalized_exit(95.5)
    evidence = store.get_exit_slippage("entry-link")
    assert evidence["realized_r"] is not None
    assert evidence["realized_r"] < -1.5
    assert evidence["actual_exit_reason"] == "SL"


def test_closed_position_race_does_not_emit_missing_protection_alarm():
    engine = object.__new__(StrategyEngine)
    events = []
    engine.cfg = SimpleNamespace(protective_trigger_by="LastPrice")
    engine.execution = SimpleNamespace(
        get_active_protective_orders=lambda _symbol: [],
        get_open_positions=lambda: [],
    )
    engine.journal = SimpleNamespace(get_open_trades=lambda _symbol: [{
        "symbol": "TESTUSDT", "order_link_id": "link", "run_id": "run",
    }])
    engine.telemetry = SimpleNamespace(
        record_health=lambda *args, **kwargs: True,
        record_protection_event=lambda *args, **kwargs: events.append(args[1]) or True,
    )
    engine._protection_entry_halt = None
    engine._watch_protection([{
        "symbol": "TESTUSDT", "size": "1", "side": "Buy",
        "stopLoss": "98", "takeProfit": "104",
    }])
    assert "position_closed" in events
    assert "missing_protection_detected" not in events
    assert engine._protection_entry_halt is None


def test_post_entry_readback_verifies_configured_trigger_source():
    for source in ("LastPrice", "MarkPrice"):
        engine = object.__new__(StrategyEngine); events = []
        engine.cfg = SimpleNamespace(run_id="run", protective_trigger_by=source)
        engine.execution = SimpleNamespace(
            get_open_positions=lambda: [{
                "symbol": "TESTUSDT", "size": "1", "stopLoss": "98", "takeProfit": "104",
            }],
            get_active_protective_orders=lambda _symbol: [
                {"orderId": "sl", "stopOrderType": "StopLoss",
                 "orderStatus": "Untriggered", "triggerBy": source},
                {"orderId": "tp", "stopOrderType": "TakeProfit",
                 "orderStatus": "Untriggered", "triggerBy": source},
            ],
        )
        engine.telemetry = SimpleNamespace(
            record_protection_event=lambda *args, **kwargs: events.append((args, kwargs)) or True
        )
        engine._protection_entry_halt = None
        assert engine._record_initial_protection_readback("TESTUSDT", "link", 98, 104)
        assert events[-1][0][1] == "verified_active"
