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
