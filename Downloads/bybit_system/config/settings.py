"""
Конфигурация подключения к Bybit.

Все секреты читаются только из переменных окружения (.env).

В этом файле НЕ должно быть API-ключей.
"""

import os
from dataclasses import dataclass, field
from typing import List


def _symbols_from_env() -> List[str]:
    raw = os.getenv("SYMBOLS")
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "TRXUSDT",
        "LINKUSDT",
        "SUIUSDT",
        "HBARUSDT",
        "DOTUSDT",
        "BCHUSDT",
        "UNIUSDT",
        "APTUSDT",
        "1000PEPEUSDT",
        "HYPEUSDT",
        "XMRUSDT",
        "XLMUSDT",
        "SHIB1000USDT",
        "CROUSDT",
        "NEARUSDT",
        "TAOUSDT",
        "ONDOUSDT",
        "AAVEUSDT",
        "MNTUSDT",
    ]


@dataclass
class BybitConfig:
    run_id: str = os.getenv("RUN_ID", "")
    commit_sha: str = os.getenv("COMMIT_SHA", "")
    runtime_mode: str = os.getenv("RUNTIME_MODE", "local").strip().lower()

    # ==========================================================
    # API
    # ==========================================================

    api_key: str = os.getenv("BYBIT_API_KEY", "")
    api_secret: str = os.getenv("BYBIT_API_SECRET", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")

    # ==========================================================
    # РЕЖИМ РАБОТЫ
    # ==========================================================

    testnet: bool = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

    trading_enabled: bool = (
        os.getenv("TRADING_ENABLED", "true").lower() == "true"
    )

    paper_trading: bool = (
        os.getenv("PAPER_TRADING", "false").lower() == "true"
    )

    category: str = "linear"

    # ==========================================================
    # ТОРГУЕМЫЕ ИНСТРУМЕНТЫ
    # ==========================================================

    symbols: List[str] = field(default_factory=_symbols_from_env)

    # ==========================================================
    # БАЗА ДАННЫХ
    # ==========================================================

    db_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/bybit"
    )

    # ==========================================================
    # WEBSOCKET
    # ==========================================================

    ws_channel_type: str = "linear"
    ping_interval: int = 20

    # Collector transport recovery. These values affect process supervision
    # and market-data availability only; they never relax entry freshness
    # checks or alter a trading decision.
    ws_reconnect_initial_seconds: float = float(
        os.getenv("WS_RECONNECT_INITIAL_SECONDS", "5")
    )
    ws_reconnect_max_seconds: float = float(
        os.getenv("WS_RECONNECT_MAX_SECONDS", "60")
    )
    ws_reconnect_jitter_ratio: float = float(
        os.getenv("WS_RECONNECT_JITTER_RATIO", "0.20")
    )
    ws_reconnect_stable_reset_seconds: float = float(
        os.getenv("WS_RECONNECT_STABLE_RESET_SECONDS", "120")
    )
    ws_reconnect_restart_after_seconds: float = float(
        os.getenv("WS_RECONNECT_RESTART_AFTER_SECONDS", "900")
    )

    # ==========================================================
    # ТАЙМФРЕЙМЫ
    # ==========================================================

    primary_interval: str = os.getenv("PRIMARY_INTERVAL", "15")
    confirmation_interval: str = os.getenv("CONFIRMATION_INTERVAL", "1")
    higher_interval: str = os.getenv("HIGHER_INTERVAL", "60")

    # ==========================================================
    # DECISION ENGINE
    # ==========================================================

    decision_interval_sec: int = int(
        os.getenv("DECISION_INTERVAL_SEC", "30")
    )

    min_open_confidence: float = float(
        os.getenv("MIN_OPEN_CONFIDENCE", "0.45")
    )

    min_rr: float = float(
        os.getenv("MIN_RR", "2.0")
    )

    min_decision_margin: float = float(
        os.getenv("MIN_DECISION_MARGIN", "0.08")
    )

    min_confirming_families: int = int(
        os.getenv("MIN_CONFIRMING_FAMILIES", "2")
    )

    max_new_positions_per_cycle: int = int(
        os.getenv("MAX_NEW_POSITIONS_PER_CYCLE", "2")
    )

    min_seconds_between_entries: int = int(
        os.getenv("MIN_SECONDS_BETWEEN_ENTRIES", "20")
    )

    # ==========================================================
    # RISK MANAGER
    # ==========================================================

    risk_per_trade_pct: float = float(
        os.getenv("RISK_PER_TRADE_PCT", "1.0")
    )

    max_position_usdt: float = float(
        os.getenv("MAX_POSITION_USDT", "250")
    )

    max_leverage: int = int(
        os.getenv("MAX_LEVERAGE", "3")
    )

    max_daily_loss_pct: float = float(
        os.getenv("MAX_DAILY_LOSS_PCT", "3.0")
    )

    max_open_positions: int = int(
        os.getenv("MAX_OPEN_POSITIONS", "10")
    )

    max_daily_trades: int = int(
        os.getenv("MAX_DAILY_TRADES", "200")
    )

    max_trades_per_symbol: int = int(
        os.getenv("MAX_TRADES_PER_SYMBOL", "5")
    )

    cooldown_minutes: int = int(
        os.getenv("COOLDOWN_MINUTES", "5")
    )

    # ==========================================================
    # STOP LOSS / TAKE PROFIT
    # ==========================================================

    default_stop_loss_pct: float = float(
        os.getenv("DEFAULT_STOP_LOSS_PCT", "1.5")
    )

    default_take_profit_rr: float = float(
        os.getenv("DEFAULT_TP_RR", "2.0")
    )

    # ==========================================================
    # VOLATILITY
    # ==========================================================

    max_volatility_atr_pct: float = float(
        os.getenv("MAX_VOLATILITY_ATR_PCT", "5.0")
    )

    max_spread_pct: float = float(
        os.getenv("MAX_SPREAD_PCT", "0.8")
    )

    max_long_funding_rate: float = float(
        os.getenv("MAX_LONG_FUNDING_RATE", "0.001")
    )

    max_short_funding_rate_abs: float = float(
        os.getenv("MAX_SHORT_FUNDING_RATE_ABS", "0.001")
    )

    # ==========================================================
    # TREND FILTER
    # ==========================================================

    trend_filter_enabled: bool = (
        os.getenv("TREND_FILTER_ENABLED", "true").lower() == "true"
    )

    trend_filter_reversal_confidence: float = float(
        os.getenv("TREND_FILTER_REVERSAL_CONFIDENCE", "0.68")
    )

    # ==========================================================
    # СВЕЖЕСТЬ ДАННЫХ
    # ==========================================================

    max_candle_age_minutes: int = int(
        os.getenv("MAX_CANDLE_AGE_MINUTES", "45")
    )

    max_orderbook_age_seconds: int = int(
        os.getenv("MAX_ORDERBOOK_AGE_SECONDS", "90")
    )

    max_funding_age_minutes: int = int(
        os.getenv("MAX_FUNDING_AGE_MINUTES", "90")
    )

    max_open_interest_age_minutes: int = int(
        os.getenv("MAX_OPEN_INTEREST_AGE_MINUTES", "90")
    )

    max_trade_flow_age_seconds: int = int(
        os.getenv("MAX_TRADE_FLOW_AGE_SECONDS", "120")
    )

    # ==========================================================
    # PORTFOLIO RISK
    # ==========================================================

    max_same_direction_per_group: int = int(
        os.getenv("MAX_SAME_DIRECTION_PER_GROUP", "2")
    )

    # ==========================================================
    # TRAILING STOP
    # ==========================================================

    trailing_stop_enabled: bool = (
        os.getenv("TRAILING_STOP_ENABLED", "true").lower() == "true"
    )

    trailing_activation_pct: float = float(
        os.getenv("TRAILING_ACTIVATION_PCT", "1.0")
    )

    trailing_distance_pct: float = float(
        os.getenv("TRAILING_DISTANCE_PCT", "0.8")
    )

    # Два последовательных сужения оставшегося диапазона защиты для
    # затянувшейся позиции. Каждый этап хранится в БД и применяется ровно раз.
    time_range_tightening_enabled: bool = (
        os.getenv("TIME_RANGE_TIGHTENING_ENABLED", "true").lower() == "true"
    )

    time_range_tightening_after_seconds: int = int(
        os.getenv("TIME_RANGE_TIGHTENING_AFTER_SECONDS", "3600")
    )

    time_range_tightening_factor: float = float(
        os.getenv("TIME_RANGE_TIGHTENING_FACTOR", "0.5")
    )

    time_range_second_tightening_after_seconds: int = int(
        os.getenv("TIME_RANGE_SECOND_TIGHTENING_AFTER_SECONDS", "18000")
    )

    time_range_second_tightening_factor: float = float(
        os.getenv("TIME_RANGE_SECOND_TIGHTENING_FACTOR", "0.5")
    )

    # ==========================================================
    # ЛОГИРОВАНИЕ
    # ==========================================================

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Research telemetry cadence. These affect persistence/API observation
    # frequency only; they do not affect signals, orders, or risk policy.
    telemetry_account_interval_sec: int = int(
        os.getenv("TELEMETRY_ACCOUNT_INTERVAL_SEC", "60")
    )
    telemetry_position_interval_sec: int = int(
        os.getenv("TELEMETRY_POSITION_INTERVAL_SEC", "30")
    )

    # Infrastructure-only durability/capacity controls.  These never alter
    # signal calculations, position sizing, SL/TP, or exit policy.
    telemetry_retry_attempts: int = int(os.getenv("TELEMETRY_RETRY_ATTEMPTS", "3"))
    telemetry_retry_base_seconds: float = float(
        os.getenv("TELEMETRY_RETRY_BASE_SECONDS", "0.25")
    )
    telemetry_outbox_max_attempts: int = int(
        os.getenv("TELEMETRY_OUTBOX_MAX_ATTEMPTS", "8")
    )
    telemetry_outbox_delivered_retention_hours: int = int(
        os.getenv("TELEMETRY_OUTBOX_DELIVERED_RETENTION_HOURS", "24")
    )
    telemetry_outbox_cleanup_batch_size: int = int(
        os.getenv("TELEMETRY_OUTBOX_CLEANUP_BATCH_SIZE", "1000")
    )
    telemetry_outbox_cleanup_max_batches: int = int(
        os.getenv("TELEMETRY_OUTBOX_CLEANUP_MAX_BATCHES", "10")
    )
    health_event_dedup_window_seconds: int = int(
        os.getenv("HEALTH_EVENT_DEDUP_WINDOW_SECONDS", "60")
    )
    health_condition_reminder_seconds: int = int(
        os.getenv("HEALTH_CONDITION_REMINDER_SECONDS", "900")
    )
    position_close_visibility_grace_seconds: int = int(
        os.getenv("POSITION_CLOSE_VISIBILITY_GRACE_SECONDS", "120")
    )
    storage_max_database_bytes: int = int(
        os.getenv("STORAGE_MAX_DATABASE_BYTES", "0")
    )
    storage_entry_block_ratio: float = float(
        os.getenv("STORAGE_ENTRY_BLOCK_RATIO", "0.70")
    )
    storage_monitor_interval_sec: int = int(
        os.getenv("STORAGE_MONITOR_INTERVAL_SEC", "300")
    )
    raw_trades_retention_hours: int = int(
        os.getenv("RAW_TRADES_RETENTION_HOURS", "168")
    )
    orderbook_retention_hours: int = int(
        os.getenv("ORDERBOOK_RETENTION_HOURS", "168")
    )
    liquidations_retention_hours: int = int(
        os.getenv("LIQUIDATIONS_RETENTION_HOURS", "720")
    )
    funding_raw_retention_hours: int = int(
        os.getenv("FUNDING_RAW_RETENTION_HOURS", "6")
    )
    open_interest_raw_retention_hours: int = int(
        os.getenv("OPEN_INTEREST_RAW_RETENTION_HOURS", "6")
    )
    high_frequency_retention_interval_seconds: int = int(
        os.getenv("HIGH_FREQUENCY_RETENTION_INTERVAL_SECONDS", "1800")
    )
    retention_delete_batch_size: int = int(
        os.getenv("RETENTION_DELETE_BATCH_SIZE", "10000")
    )
    # Measured on the Railway database: the two ticker tables ingest roughly
    # 190k rows/hour combined, so a 100k per-table budget could not drain a
    # backlog while also keeping up with new writes. Deletion stays batched and
    # only ever touches rows already past their policy window.
    retention_max_rows_per_run: int = int(
        os.getenv("RETENTION_MAX_ROWS_PER_RUN", "400000")
    )

    # Protective orders keep LastPrice by default for backward-compatible
    # production/Testnet behaviour. MarkPrice is opt-in for a separately
    # approved smoke test.
    protective_trigger_by: str = os.getenv(
        "PROTECTIVE_TRIGGER_BY", "LastPrice"
    ).strip()
    slippage_elevated_pct: float = float(
        os.getenv("SLIPPAGE_ELEVATED_PCT", "0.25")
    )
    slippage_anomalous_pct: float = float(
        os.getenv("SLIPPAGE_ANOMALOUS_PCT", "1.0")
    )
    slippage_elevated_r: float = float(
        os.getenv("SLIPPAGE_ELEVATED_R", "0.25")
    )
    slippage_anomalous_r: float = float(
        os.getenv("SLIPPAGE_ANOMALOUS_R", "0.75")
    )
    # Independent of the exchange trigger/fill classification, a single
    # realized loss outside this envelope is evidence that actual execution
    # no longer matches the risk model.  It blocks only future entries; open
    # positions continue to be managed and retain their native protection.
    max_realized_loss_r: float = float(
        os.getenv("MAX_REALIZED_LOSS_R", "1.5")
    )
    protective_quarantine_seconds: int = int(
        os.getenv("PROTECTIVE_QUARANTINE_SECONDS", "3600")
    )
    protective_anomaly_sticky_count: int = int(
        os.getenv("PROTECTIVE_ANOMALY_STICKY_COUNT", "2")
    )

    # Operator observability. Telegram credentials are intentionally read
    # directly from the environment by the notifier and are never captured
    # in immutable run metadata.
    operator_monitor_interval_seconds: int = int(
        os.getenv("OPERATOR_MONITOR_INTERVAL_SECONDS", "30")
    )
    health_http_enabled: bool = os.getenv("HEALTH_HTTP_ENABLED", "true").lower() == "true"
    telegram_alerts_enabled: bool = os.getenv(
        "TELEGRAM_ALERTS_ENABLED", "false"
    ).lower() == "true"
    # Consolidated owner report cadence. This replaces the former once-a-day
    # summary: an hourly report answers "how is it going?" without the owner
    # having to wait until a fixed hour.
    telegram_report_interval_minutes: int = int(
        os.getenv("TELEGRAM_REPORT_INTERVAL_MINUTES", "60")
    )
    # Reporting period for that report: "24h", "utc_day" or "run". Printed
    # verbatim in the message so a number is never period-ambiguous.
    telegram_report_period: str = os.getenv("TELEGRAM_REPORT_PERIOD", "24h")
    # A problem must persist this long before the owner is told, so a
    # WebSocket blip that reconnects on its own never reaches Telegram.
    telegram_alert_escalation_seconds: int = int(
        os.getenv("TELEGRAM_ALERT_ESCALATION_SECONDS", "180")
    )
    # An unchanged, still-active problem is repeated at most this often.
    telegram_alert_reminder_seconds: int = int(
        os.getenv("TELEGRAM_ALERT_REMINDER_SECONDS", "3600")
    )

    # Process-supervisor policy.  This is deliberately separate from the
    # WebSocket reconnect backoff inside the collector: once that recovery
    # budget is exhausted, the collector exits and the supervisor recreates
    # the whole process with fresh sockets and resolver state.
    collector_restart_initial_seconds: float = float(
        os.getenv("COLLECTOR_RESTART_INITIAL_SECONDS", "5")
    )
    collector_restart_max_seconds: float = float(
        os.getenv("COLLECTOR_RESTART_MAX_SECONDS", "60")
    )
    collector_restart_stable_reset_seconds: float = float(
        os.getenv("COLLECTOR_RESTART_STABLE_RESET_SECONDS", "300")
    )
