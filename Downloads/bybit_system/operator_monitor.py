"""Read-only runtime health endpoint and optional automatic Telegram alerts.

The monitor never calls Bybit and never mutates trading state. Its durable
cursors prevent a container restart from replaying old trade/health alerts.
Telegram credentials are read only from process environment and are never
written to PostgreSQL, run metadata, or logs.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import urllib.parse
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from sqlalchemy import func, or_

from storage.durability import StorageGuard
from storage.models import (
    AccountSnapshot, OperationalHealthEvent, OperatorMonitorState,
    PositionSnapshot, RunMetadata, RiskState, TelemetryOutbox, TradeLog,
)
from timeutils import ensure_aware_utc, utcnow

logger = logging.getLogger(__name__)


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: float = 10.0):
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout
        self._poll_error_active = False

    def send(self, message: str) -> bool:
        payload = urllib.parse.urlencode({
            "chat_id": self._chat_id, "text": message[:4000],
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=payload, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return 200 <= response.status < 300
        except Exception as exc:
            # Never stringify the exception: urllib errors may include the URL,
            # and the Telegram token is part of that URL.
            logger.error("Telegram notification failed (%s)", type(exc).__name__)
            return False

    def get_updates(self, offset: int) -> list[dict]:
        payload = urllib.parse.urlencode({
            "offset": str(max(0, int(offset))),
            "timeout": "0",
            "allowed_updates": json.dumps(["message"]),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/getUpdates",
            data=payload, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                document = json.loads(response.read().decode("utf-8"))
            if self._poll_error_active:
                logger.info("Telegram command polling recovered")
            self._poll_error_active = False
            result = document.get("result", []) if document.get("ok") else []
            return result if isinstance(result, list) else []
        except Exception as exc:
            if not self._poll_error_active:
                logger.error("Telegram command polling failed (%s)", type(exc).__name__)
            self._poll_error_active = True
            return []


class OperatorMonitor:
    def __init__(
        self, db, cfg, run_id: str,
        sender: Optional[Callable[[str], bool]] = None,
        updates_fetcher: Optional[Callable[[int], list[dict]]] = None,
        authorized_chat_id: Optional[str] = None,
    ):
        self.db = db
        self.cfg = cfg
        self.run_id = run_id
        self.interval = max(5, int(getattr(cfg, "operator_monitor_interval_seconds", 30)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._http: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None
        self._snapshot_lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "run_id": run_id, "status": "starting", "testnet": bool(cfg.testnet)
        }
        self._sender = sender
        self._updates_fetcher = updates_fetcher
        self._authorized_chat_id = (
            str(authorized_chat_id) if authorized_chat_id is not None else None
        )
        if self._sender is None and getattr(cfg, "telegram_alerts_enabled", False):
            token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
            if token and chat_id:
                client = TelegramClient(token, chat_id)
                self._sender = client.send
                self._updates_fetcher = client.get_updates
                self._authorized_chat_id = str(chat_id)
            else:
                logger.error(
                    "TELEGRAM_ALERTS_ENABLED=true, but TELEGRAM_BOT_TOKEN or "
                    "TELEGRAM_CHAT_ID is missing; alerts are disabled"
                )

    def start(self) -> None:
        self._initialize_durable_cursors()
        self.poll_once()
        self._thread = threading.Thread(
            target=self._run, name="operator-monitor", daemon=True
        )
        self._thread.start()
        if getattr(self.cfg, "health_http_enabled", True):
            self._start_http()

    def stop(self) -> None:
        self._stop.set()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)

    def snapshot(self) -> dict:
        with self._snapshot_lock:
            return json.loads(json.dumps(self._snapshot, default=_json_default))

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception:
                logger.exception("Operator monitor polling failed")

    def _load_state(self) -> dict:
        session = self.db.get_session()
        try:
            row = session.query(OperatorMonitorState).filter_by(
                state_key=f"run:{self.run_id}"
            ).first()
            return dict(row.state_value or {}) if row else {}
        finally:
            session.close()

    def _save_state(self, value: dict) -> None:
        session = self.db.get_session()
        try:
            key = f"run:{self.run_id}"
            row = session.query(OperatorMonitorState).filter_by(state_key=key).first()
            if row is None:
                row = OperatorMonitorState(state_key=key, state_value={})
                session.add(row)
            row.state_value = value
            row.updated_at = utcnow()
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Could not persist operator monitor cursor")
        finally:
            session.close()

    def _initialize_durable_cursors(self) -> None:
        state = self._load_state()
        if state:
            return
        session = self.db.get_session()
        try:
            trades = session.query(TradeLog).filter(or_(
                TradeLog.run_id == self.run_id,
                TradeLog.status == "open",
            )).all()
            latest_health = session.query(func.max(OperationalHealthEvent.id)).filter_by(
                run_id=self.run_id
            ).scalar() or 0
            state = {
                "trade_states": {str(row.id): row.status for row in trades},
                "last_health_id": int(latest_health),
                "breaker_fingerprint": None,
                "heartbeat_state": None,
                "storage_level": None,
                "last_daily_summary": None,
                "startup_sent": False,
                "telegram_update_offset": 0,
                "pending_messages": [],
            }
            self._save_state(state)
        finally:
            session.close()

    def _notify(self, message: str) -> bool:
        return bool(self._sender and self._sender(message))

    def poll_once(self) -> dict:
        now = utcnow()
        state = self._load_state()
        storage = StorageGuard(self.db, self.cfg).status()
        session = self.db.get_session()
        messages = []
        try:
            run = session.query(RunMetadata).filter_by(run_id=self.run_id).first()
            risk = session.query(RiskState).filter_by(id=1).first()
            tracked_ids = [
                int(value) for value in (state.get("trade_states") or {})
                if str(value).isdigit()
            ]
            ownership_filter = or_(
                TradeLog.run_id == self.run_id,
                TradeLog.status == "open",
            )
            if tracked_ids:
                ownership_filter = or_(ownership_filter, TradeLog.id.in_(tracked_ids))
            trades = session.query(TradeLog).filter(ownership_filter).order_by(
                TradeLog.id.asc()
            ).all()
            open_trades = [row for row in trades if row.status == "open"]
            closed = [row for row in trades if row.status == "closed"]
            total_pnl = sum(float(row.pnl_usdt or 0) for row in closed)
            wins = sum(1 for row in closed if float(row.pnl_usdt or 0) > 0)
            outbox = dict(session.query(
                TelemetryOutbox.status, func.count(TelemetryOutbox.id)
            ).group_by(TelemetryOutbox.status).all())
            oldest_pending = session.query(func.min(TelemetryOutbox.created_at)).filter(
                TelemetryOutbox.status.in_(("pending", "failed"))
            ).scalar()

            heartbeat_limit = now - timedelta(seconds=max(90, self.interval * 4))
            collector_ok = bool(
                run and ensure_aware_utc(run.collector_heartbeat_at)
                and ensure_aware_utc(run.collector_heartbeat_at) >= heartbeat_limit
            )
            trader_ok = bool(
                run and ensure_aware_utc(run.trader_heartbeat_at)
                and ensure_aware_utc(run.trader_heartbeat_at) >= heartbeat_limit
            )
            started_at = ensure_aware_utc(run.started_at) if run else None
            in_startup_grace = bool(
                started_at and (now - started_at).total_seconds() < 120
            )
            heartbeat_state = (
                "healthy" if collector_ok and trader_ok
                else "starting" if in_startup_grace else "degraded"
            )
            if state.get("heartbeat_state") not in (None, heartbeat_state):
                messages.append(
                    "✅ Runtime heartbeats recovered" if heartbeat_state == "healthy"
                    else "🚨 Runtime heartbeat is stale; check Railway logs"
                )
            state["heartbeat_state"] = heartbeat_state

            causes = dict((risk.circuit_breaker_causes or {}) if risk else {})
            fingerprint = json.dumps(causes, sort_keys=True, default=_json_default)
            if state.get("breaker_fingerprint") not in (None, fingerprint):
                messages.append(
                    "🚨 New entries blocked: " + "; ".join(
                        str(value.get("reason", key)) for key, value in causes.items()
                    ) if causes else "✅ Circuit breaker cleared; entries may resume"
                )
            state["breaker_fingerprint"] = fingerprint

            previous = dict(state.get("trade_states") or {})
            current = {str(row.id): row.status for row in trades}
            for row in trades:
                old = previous.get(str(row.id))
                if old is None:
                    direction = "LONG" if row.action == "open_long" else "SHORT"
                    messages.append(
                        f"📈 Opened {row.symbol} {direction} | trade={row.id} | "
                        f"entry={float(row.entry_price):.8g}"
                    )
                elif old != "closed" and row.status == "closed":
                    messages.append(
                        f"📊 Closed {row.symbol} | trade={row.id} | "
                        f"PnL={float(row.pnl_usdt or 0):+.4f} USDT | "
                        f"reason={row.exit_reason or 'unknown'}"
                    )
            state["trade_states"] = current

            new_health = session.query(OperationalHealthEvent).filter(
                OperationalHealthEvent.run_id == self.run_id,
                OperationalHealthEvent.id > int(state.get("last_health_id") or 0),
                OperationalHealthEvent.severity.in_(("critical", "error")),
            ).order_by(OperationalHealthEvent.id.asc()).all()
            if new_health:
                for event in new_health[-3:]:
                    messages.append(
                        f"⚠️ {event.component}/{event.event_type}: {event.status}"
                    )
            latest_health = session.query(func.max(OperationalHealthEvent.id)).filter_by(
                run_id=self.run_id
            ).scalar() or state.get("last_health_id") or 0
            state["last_health_id"] = int(latest_health)

            ratio = storage.get("usage_ratio")
            level = "critical" if ratio is not None and ratio >= .85 else (
                "warning" if ratio is not None and ratio >= .70 else "normal"
            )
            if state.get("storage_level") not in (None, level) and level != "normal":
                messages.append(f"🚨 PostgreSQL usage is {ratio:.1%} ({level})")
            state["storage_level"] = level

            daily_hour = min(23, max(0, int(getattr(
                self.cfg, "telegram_daily_summary_utc_hour", 12
            ))))
            today = now.date().isoformat()
            if now.hour >= daily_hour and state.get("last_daily_summary") != today:
                win_rate = wins / len(closed) * 100 if closed else 0.0
                summary = (
                    f"🧾 Daily status {today} UTC\nrun={self.run_id}\n"
                    f"closed={len(closed)} open={len(open_trades)} win_rate={win_rate:.1f}%\n"
                    f"PnL={total_pnl:+.4f} USDT breaker={'ON' if causes else 'off'}\n"
                )
                summary += f"DB={ratio:.1%}" if ratio is not None else "DB size unavailable"
                messages.append(summary)
                state["last_daily_summary"] = today

            snapshot = {
                "run_id": self.run_id, "testnet": bool(self.cfg.testnet),
                "status": (
                    heartbeat_state if storage["available"] else "degraded"
                ),
                "collector_heartbeat": run.collector_heartbeat_at if run else None,
                "trader_heartbeat": run.trader_heartbeat_at if run else None,
                "closed_trades": len(closed), "open_trades": len(open_trades),
                "wins": wins, "realized_pnl_usdt": total_pnl,
                "circuit_breaker": bool(causes), "breaker_causes": causes,
                "database": storage, "outbox": outbox,
                "oldest_pending_outbox": oldest_pending, "observed_at": now,
            }
        finally:
            session.close()

        if not state.get("startup_sent"):
            messages.insert(
                0,
                f"🤖 Testnet bot monitor started\nrun={self.run_id}\n"
                f"status={snapshot['status']} | open={snapshot['open_trades']} | "
                f"breaker={'ON' if snapshot['circuit_breaker'] else 'off'}",
            )
            state["startup_sent"] = True
        pending = list(state.get("pending_messages") or [])
        known = {item.get("key") for item in pending if isinstance(item, dict)}
        for message in messages[:10]:
            key = hashlib.sha256(message.encode("utf-8")).hexdigest()
            if key not in known:
                pending.append({"key": key, "message": message})
                known.add(key)
        remaining = []
        for item in pending[:100]:
            if not self._notify(str(item.get("message", ""))):
                remaining.append(item)
        # Bound an unavailable Telegram destination without losing the latest
        # operational events forever or growing PostgreSQL without limit.
        state["pending_messages"] = (remaining + pending[100:])[-100:]
        self._poll_telegram_commands(state, snapshot)
        self._save_state(state)
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot

    def _poll_telegram_commands(self, state: dict, snapshot: dict) -> None:
        if self._updates_fetcher is None or self._authorized_chat_id is None:
            return
        offset = int(state.get("telegram_update_offset") or 0)
        updates = self._updates_fetcher(offset)
        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0))):
            update_id = int(update.get("update_id", 0))
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = str(message.get("text") or "").strip()
            if chat_id != self._authorized_chat_id:
                offset = max(offset, update_id + 1)
                continue
            command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
            if command not in ("/start", "/status", "/positions", "/pnl", "/help"):
                offset = max(offset, update_id + 1)
                continue
            response = self._build_command_response(command, snapshot)
            if not self._notify(response):
                break
            offset = max(offset, update_id + 1)
        state["telegram_update_offset"] = offset

    def _build_command_response(self, command: str, snapshot: dict) -> str:
        session = self.db.get_session()
        try:
            now = utcnow()
            risk = session.query(RiskState).filter_by(id=1).first()
            open_trades = session.query(TradeLog).filter_by(status="open").order_by(
                TradeLog.id.asc()
            ).all()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            closed_today = session.query(TradeLog).filter(
                TradeLog.status == "closed",
                TradeLog.closed_at >= today_start,
            ).all()
            latest_account = session.query(AccountSnapshot).order_by(
                AccountSnapshot.observed_at.desc()
            ).first()

            if command == "/positions":
                if not open_trades:
                    return "📭 Открытых позиций нет."
                lines = [f"📌 Открытые позиции: {len(open_trades)}"]
                for trade in open_trades[:20]:
                    position = session.query(PositionSnapshot).filter_by(
                        trade_log_id=trade.id
                    ).order_by(PositionSnapshot.observed_at.desc()).first()
                    direction = "LONG" if trade.action == "open_long" else "SHORT"
                    unrealized = (
                        f"{float(position.unrealized_pnl):+.3f} USDT"
                        if position and position.unrealized_pnl is not None else "n/a"
                    )
                    lines.append(
                        f"#{trade.id} {trade.symbol} {direction} | entry="
                        f"{float(trade.entry_price):.8g} | SL={float(trade.stop_loss_price):.8g} "
                        f"TP={float(trade.take_profit_price):.8g} | uPnL={unrealized}"
                    )
                return "\n".join(lines)

            gross_profit = sum(max(0.0, float(row.pnl_usdt or 0)) for row in closed_today)
            gross_loss = sum(min(0.0, float(row.pnl_usdt or 0)) for row in closed_today)
            net = gross_profit + gross_loss
            fees = sum(float(row.total_fee_usdt or 0) for row in closed_today)
            wins = sum(1 for row in closed_today if float(row.pnl_usdt or 0) > 0)
            win_rate = wins / len(closed_today) * 100 if closed_today else 0.0
            if command == "/pnl":
                wallet = (
                    f"{float(latest_account.wallet_balance):.2f} USDT"
                    if latest_account and latest_account.wallet_balance is not None else "n/a"
                )
                return (
                    f"💰 P&L за сегодня (UTC)\nclosed={len(closed_today)} | "
                    f"win rate={win_rate:.1f}%\nnet={net:+.4f} USDT | "
                    f"gross profit={gross_profit:+.4f} | gross loss={gross_loss:.4f}\n"
                    f"fees={fees:.4f} USDT | wallet={wallet}"
                )

            if command == "/help":
                return "Команды: /status — состояние, /positions — позиции, /pnl — P&L."

            database = snapshot.get("database") or {}
            usage = database.get("usage_ratio")
            db_text = "OK" if database.get("available") else "ERROR"
            if usage is not None:
                db_text += f" ({float(usage):.1%})"
            causes = dict((risk.circuit_breaker_causes or {}) if risk else {})
            daily_pnl = float(risk.daily_pnl_usdt or 0) if risk else net
            report = (
                f"🤖 Bybit Testnet bot\nrun={self.run_id}\n"
                f"runtime={snapshot.get('status', 'unknown')} | DB={db_text}\n"
                f"collector={'OK' if snapshot.get('collector_heartbeat') else 'unknown'} | "
                f"trader={'OK' if snapshot.get('trader_heartbeat') else 'unknown'}\n"
                f"open={len(open_trades)} | daily P&L={daily_pnl:+.4f} USDT\n"
                f"circuit breaker={'ON' if causes else 'off'} | "
                f"outbox pending={(snapshot.get('outbox') or {}).get('pending', 0)}"
            )
            if command == "/start":
                report += "\n\nКоманды: /status, /positions, /pnl"
            return report
        finally:
            session.close()

    def _start_http(self) -> None:
        monitor = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in ("/healthz", "/status"):
                    self.send_error(404)
                    return
                payload = monitor.snapshot()
                # A circuit breaker or stale child heartbeat is a degraded but
                # observable trading state; the supervisor/Telegram handles
                # it. Restart loops would make diagnosis and recovery worse.
                code = 200 if payload.get("database", {}).get("available") else 503
                body = json.dumps(payload, default=_json_default).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        port = int(os.getenv("PORT", "8080"))
        self._http = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._http_thread = threading.Thread(
            target=self._http.serve_forever, name="health-http", daemon=True
        )
        self._http_thread.start()
        logger.info("Operator health endpoint listening on port %d", port)
