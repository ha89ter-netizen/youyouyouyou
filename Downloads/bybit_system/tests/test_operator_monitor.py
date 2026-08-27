from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import BybitConfig
from operator_monitor import OperatorMonitor
from storage.models import Base, OperatorMonitorState, RiskState, RunMetadata, TradeLog
from storage.telemetry import effective_config_document
from timeutils import utcnow


class Db:
    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def get_session(self):
        return self.SessionLocal()


def cfg():
    return SimpleNamespace(
        testnet=True, operator_monitor_interval_seconds=30,
        telegram_alerts_enabled=False, telegram_daily_summary_utc_hour=23,
        health_http_enabled=False, storage_max_database_bytes=0,
        storage_entry_block_ratio=.85,
    )


def seed(db, run_id="run"):
    session = db.get_session(); now = utcnow()
    session.add(RunMetadata(
        run_id=run_id, commit_sha="c" * 40, started_at=now,
        environment_summary={}, status="running",
        collector_heartbeat_at=now, trader_heartbeat_at=now,
    ))
    session.add(RiskState(
        id=1, day_utc=now.date(), daily_pnl_usdt=0,
        daily_trade_count=0, circuit_breaker_tripped=False,
        circuit_breaker_sticky=False, circuit_breaker_causes={},
    ))
    session.commit(); session.close()


def test_trade_notifications_are_automatic_deduplicated_and_restart_safe():
    db = Db(); seed(db); sent = []
    monitor = OperatorMonitor(db, cfg(), "run", sender=lambda text: sent.append(text) or True)
    monitor._initialize_durable_cursors()
    monitor.poll_once()
    assert sum("monitor started" in item for item in sent) == 1

    session = db.get_session()
    trade = TradeLog(
        symbol="BTCUSDT", action="open_long", source="test", reason="test",
        order_link_id="link", run_id="run", entry_price=100, size_usdt=100,
        leverage=1, status="open",
    )
    session.add(trade); session.commit(); trade_id = trade.id; session.close()
    monitor.poll_once(); monitor.poll_once()
    assert sum("Opened BTCUSDT" in item for item in sent) == 1

    session = db.get_session(); trade = session.get(TradeLog, trade_id)
    trade.status = "closed"; trade.pnl_usdt = 1.25; trade.exit_reason = "TP"
    trade.closed_at = utcnow(); session.commit(); session.close()
    monitor.poll_once(); monitor.poll_once()
    assert sum("Closed BTCUSDT" in item for item in sent) == 1

    restarted = OperatorMonitor(db, cfg(), "run", sender=lambda text: sent.append(text) or True)
    restarted._initialize_durable_cursors(); restarted.poll_once()
    assert sum("Opened BTCUSDT" in item for item in sent) == 1
    assert sum("Closed BTCUSDT" in item for item in sent) == 1


def test_failed_telegram_delivery_remains_durable_for_retry():
    db = Db(); seed(db)
    failed = OperatorMonitor(db, cfg(), "run", sender=lambda _text: False)
    failed._initialize_durable_cursors(); failed.poll_once()
    session = db.get_session()
    row = session.query(OperatorMonitorState).one()
    assert row.state_value["pending_messages"]
    session.close()

    sent = []
    recovered = OperatorMonitor(db, cfg(), "run", sender=lambda text: sent.append(text) or True)
    recovered.poll_once()
    assert any("monitor started" in item for item in sent)
    session = db.get_session(); row = session.query(OperatorMonitorState).one()
    assert row.state_value["pending_messages"] == []
    session.close()


def test_health_snapshot_reports_breaker_without_calling_exchange():
    db = Db(); seed(db)
    session = db.get_session(); risk = session.get(RiskState, 1)
    risk.circuit_breaker_tripped = True
    risk.circuit_breaker_causes = {"protective:x": {
        "reason": "temporary quarantine", "sticky": False,
    }}
    session.commit(); session.close()
    monitor = OperatorMonitor(db, cfg(), "run", sender=lambda _text: True)
    monitor._initialize_durable_cursors(); snapshot = monitor.poll_once()
    assert snapshot["status"] == "healthy"
    assert snapshot["collector_healthy"] is True
    assert snapshot["trader_healthy"] is True
    assert snapshot["circuit_breaker"] is True
    assert snapshot["open_trades"] == 0


def test_telegram_credentials_are_never_captured_in_run_configuration(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN_MUST_NOT_PERSIST")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT_MUST_NOT_PERSIST")
    document = effective_config_document(BybitConfig(api_key="x", api_secret="y"))
    rendered = str(document)
    assert "TOKEN_MUST_NOT_PERSIST" not in rendered
    assert "CHAT_MUST_NOT_PERSIST" not in rendered
    assert "TELEGRAM_BOT_TOKEN" not in document["environment"]
    assert "TELEGRAM_CHAT_ID" not in document["environment"]


def test_read_only_telegram_commands_report_status_positions_and_pnl():
    db = Db(); seed(db); sent = []
    session = db.get_session()
    session.add(TradeLog(
        symbol="ETHUSDT", action="open_long", source="test", reason="test",
        order_link_id="open-link", run_id="old-run", entry_price=100,
        size_usdt=100, leverage=1, stop_loss_price=98,
        take_profit_price=104, status="open",
    ))
    session.add(TradeLog(
        symbol="BTCUSDT", action="open_short", source="test", reason="test",
        order_link_id="closed-link", run_id="run", entry_price=100,
        size_usdt=100, leverage=1, status="closed", pnl_usdt=2.5,
        total_fee_usdt=.2, closed_at=utcnow(),
    ))
    session.commit(); session.close()
    updates = [{
        "update_id": number,
        "message": {"chat": {"id": 42}, "text": command},
    } for number, command in enumerate(
        ("/start", "/status", "/positions", "/pnl"), start=10
    )]
    monitor = OperatorMonitor(
        db, cfg(), "run", sender=lambda text: sent.append(text) or True,
        updates_fetcher=lambda offset: [
            update for update in updates if update["update_id"] >= offset
        ],
        authorized_chat_id="42",
    )
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert any("Команды: /status, /positions, /pnl" in text for text in sent)
    assert any("runtime=healthy" in text for text in sent)
    assert any("ETHUSDT LONG" in text and "SL=98" in text for text in sent)
    assert any("net=+2.5000 USDT" in text for text in sent)
    session = db.get_session(); state = session.query(OperatorMonitorState).one()
    assert state.state_value["telegram_update_offset"] == 14
    session.close()

    before = list(sent)
    restarted = OperatorMonitor(
        db, cfg(), "run", sender=lambda text: sent.append(text) or True,
        updates_fetcher=lambda offset: [
            update for update in updates if update["update_id"] >= offset
        ],
        authorized_chat_id="42",
    )
    restarted.poll_once()
    assert sent == before


def test_telegram_commands_ignore_unauthorized_chat():
    db = Db(); seed(db); sent = []
    updates = [{
        "update_id": 7,
        "message": {"chat": {"id": 999}, "text": "/positions"},
    }]
    monitor = OperatorMonitor(
        db, cfg(), "run", sender=lambda text: sent.append(text) or True,
        updates_fetcher=lambda _offset: updates, authorized_chat_id="42",
    )
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert not any("Открытые позиции" in text for text in sent)
    session = db.get_session(); state = session.query(OperatorMonitorState).one()
    assert state.state_value["telegram_update_offset"] == 8
    session.close()


def test_daily_summary_counts_only_current_utc_day():
    db = Db(); seed(db); sent = []; now = utcnow()
    session = db.get_session()
    session.add_all([
        TradeLog(
            symbol="OLDUSDT", action="open_long", source="test", reason="old",
            order_link_id="old", run_id="run", entry_price=100, size_usdt=100,
            leverage=1, status="closed", pnl_usdt=99,
            closed_at=now - timedelta(days=1),
        ),
        TradeLog(
            symbol="TODAYUSDT", action="open_long", source="test", reason="today",
            order_link_id="today", run_id="run", entry_price=100, size_usdt=100,
            leverage=1, status="closed", pnl_usdt=2, closed_at=now,
        ),
    ])
    session.commit(); session.close()
    config = cfg(); config.telegram_daily_summary_utc_hour = 0
    monitor = OperatorMonitor(
        db, config, "run", sender=lambda text: sent.append(text) or True
    )
    monitor._initialize_durable_cursors(); monitor.poll_once()
    summary = next(text for text in sent if "Daily status" in text)
    assert "closed=1" in summary
    assert "PnL=+2.0000 USDT" in summary
    assert "+101.0000" not in summary


def test_positions_command_handles_missing_protection_prices():
    db = Db(); seed(db); sent = []
    session = db.get_session()
    session.add(TradeLog(
        symbol="ETHUSDT", action="open_long", source="test", reason="test",
        order_link_id="missing-protection", run_id="run", entry_price=100,
        size_usdt=100, leverage=1, stop_loss_price=None,
        take_profit_price=None, status="open",
    ))
    session.commit(); session.close()
    monitor = OperatorMonitor(
        db, cfg(), "run", sender=lambda text: sent.append(text) or True,
        updates_fetcher=lambda _offset: [{
            "update_id": 1,
            "message": {"chat": {"id": 42}, "text": "/positions"},
        }],
        authorized_chat_id="42",
    )
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert any("ETHUSDT LONG" in text and "SL=n/a TP=n/a" in text for text in sent)


def test_initial_storage_warning_uses_configured_safety_threshold(monkeypatch):
    db = Db(); seed(db); sent = []; config = cfg()
    config.storage_entry_block_ratio = .70
    monkeypatch.setattr(
        "operator_monitor.StorageGuard.status",
        lambda _self: {
            "available": True, "database_bytes": 3_000_000_000,
            "maximum_bytes": 5_000_000_000, "usage_ratio": .60,
            "entry_allowed": True, "reason": None,
        },
    )
    monitor = OperatorMonitor(
        db, config, "run", sender=lambda text: sent.append(text) or True
    )
    monitor._initialize_durable_cursors(); monitor.poll_once()
    assert any("PostgreSQL usage is 60.0% (warning)" in text for text in sent)
