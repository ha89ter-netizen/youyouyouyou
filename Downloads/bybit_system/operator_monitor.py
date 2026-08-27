"""Read-only runtime health endpoint and owner-facing Telegram control surface.

The monitor never calls Bybit and never mutates trading state. It evaluates the
canonical operational model (``operational_status``), renders owner-facing text
(``operator_messages``) and turns owner requests into durable control rows
(``operator_control``) that the *trading* process validates and applies.

Durable cursors prevent a container restart from replaying old alerts.
Telegram credentials are read only from process environment and are never
written to PostgreSQL, run metadata, or logs.

Telegram is a visibility and control surface only: if it is unavailable the
trading engine keeps running, keeps managing protection and keeps its own
fail-closed behaviour. Nothing in the trading path waits on this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from sqlalchemy import or_, text

import operational_status as ops
import operator_messages as render
from operational_status import OperationalStatusEvaluator
from operator_control import PAUSE, RESUME, OperatorControlStore
from reporting import DEFAULT_PERIOD, PERIODS, TradingReportBuilder
from storage.durability import StorageGuard
from storage.models import OperatorMonitorState, TradeLog
from timeutils import ensure_aware_utc, utcnow

logger = logging.getLogger(__name__)

# Owner-facing commands. Everything here is read-only except pause/resume,
# which only ever *request* a change the trading process re-validates.
READ_COMMANDS = ("/start", "/status", "/report", "/positions", "/health", "/help")
CONTROL_COMMANDS = {"/pause": PAUSE, "/resume": RESUME}
CALLBACK_ACTIONS = {"resume": RESUME, "pause": PAUSE}

_STORAGE_LEVEL_ORDER = {"normal": 0, "warning": 1, "critical": 2, "emergency": 3}


def _parse_stamp(value):
    """Durable cursors are JSON, so timestamps come back as ISO strings."""
    if isinstance(value, datetime):
        return ensure_aware_utc(value)
    if not value:
        return None
    try:
        return ensure_aware_utc(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


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

    def _post(self, method: str, fields: dict) -> Optional[dict]:
        payload = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=payload, method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            if not 200 <= response.status < 300:
                return None
            return json.loads(response.read().decode("utf-8"))

    def send(self, message: str, buttons: Optional[list[tuple[str, str]]] = None) -> bool:
        fields = {
            "chat_id": self._chat_id, "text": message[:4000],
            "disable_web_page_preview": "true",
        }
        if buttons:
            fields["reply_markup"] = json.dumps({"inline_keyboard": [[
                {"text": label, "callback_data": data} for label, data in buttons
            ]]})
        try:
            return self._post("sendMessage", fields) is not None
        except Exception as exc:
            # Never stringify the exception: urllib errors may include the URL,
            # and the Telegram token is part of that URL.
            logger.error("Telegram notification failed (%s)", type(exc).__name__)
            return False

    def get_updates(self, offset: int) -> list[dict]:
        try:
            document = self._post("getUpdates", {
                "offset": str(max(0, int(offset))), "timeout": "0",
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }) or {}
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

    def answer_callback(self, callback_id: str, text: str = "") -> bool:
        try:
            return self._post("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": text[:200],
            }) is not None
        except Exception as exc:
            logger.error("Telegram callback ack failed (%s)", type(exc).__name__)
            return False


class OperatorMonitor:
    def __init__(
        self, db, cfg, run_id: str,
        sender: Optional[Callable[..., bool]] = None,
        updates_fetcher: Optional[Callable[[int], list[dict]]] = None,
        authorized_chat_id: Optional[str] = None,
        authorized_user_id: Optional[str] = None,
        callback_ack: Optional[Callable[[str, str], bool]] = None,
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
            "run_id": run_id, "status": "starting", "state": "STOPPED",
            "testnet": bool(cfg.testnet),
        }
        self._sender = sender
        self._updates_fetcher = updates_fetcher
        self._callback_ack = callback_ack
        self._authorized_chat_id = (
            str(authorized_chat_id) if authorized_chat_id is not None else None
        )
        self._authorized_user_id = (
            str(authorized_user_id) if authorized_user_id is not None else None
        )
        self.evaluator = OperationalStatusEvaluator(
            db, cfg, run_id, heartbeat_limit_seconds=max(90, self.interval * 4)
        )
        self.reports = TradingReportBuilder(db, cfg, run_id)
        self.control = OperatorControlStore(db)
        self.report_period = (
            cfg.telegram_report_period
            if getattr(cfg, "telegram_report_period", DEFAULT_PERIOD) in PERIODS
            else DEFAULT_PERIOD
        )
        if self._sender is None and getattr(cfg, "telegram_alerts_enabled", False):
            self._configure_telegram_from_environment()

    def _configure_telegram_from_environment(self) -> None:
        """Credentials live in the environment only, and control fails closed.

        Alerts can be delivered to a chat without an owner id, but no command
        or button is accepted until ``TELEGRAM_USER_ID`` names exactly who the
        owner is. A missing owner id must never mean "anyone in the chat".
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        owner_id = os.getenv("TELEGRAM_USER_ID", "").strip()
        if not token or not chat_id:
            logger.error(
                "TELEGRAM_ALERTS_ENABLED=true, but TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is missing; alerts are disabled"
            )
            return
        client = TelegramClient(token, chat_id)
        self._sender = client.send
        self._authorized_chat_id = str(chat_id)
        if owner_id:
            self._authorized_user_id = str(owner_id)
            self._updates_fetcher = client.get_updates
            self._callback_ack = client.answer_callback
        else:
            logger.error(
                "TELEGRAM_USER_ID is not configured; Telegram commands and "
                "buttons are disabled. Alerts are still delivered."
            )

    @property
    def commands_enabled(self) -> bool:
        return bool(
            self._updates_fetcher is not None
            and self._authorized_chat_id is not None
            and self._authorized_user_id is not None
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

    # -- durable cursor ----------------------------------------------------

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
            self._save_state({
                "trade_states": {str(row.id): row.status for row in trades},
                "alert_state": None,
                "alert_since": None,
                "alert_notified_state": None,
                "alert_notified_at": None,
                "storage_level": "normal",
                "last_report_at": None,
                "startup_sent": False,
                "telegram_update_offset": 0,
                "last_control_outcome_id": 0,
                "pending_messages": [],
            })
        finally:
            session.close()

    def _notify(self, message: str, buttons=None) -> bool:
        if not self._sender:
            return False
        try:
            return bool(self._sender(message, buttons) if buttons else self._sender(message))
        except TypeError:
            # Tolerate simple one-argument senders used by tests and embedders.
            return bool(self._sender(message))

    # -- main cycle --------------------------------------------------------

    def poll_once(self) -> dict:
        now = utcnow()
        state = self._load_state()
        storage = StorageGuard(self.db, self.cfg).status()
        status = self.evaluator.evaluate(storage)
        messages: list[tuple[str, Optional[list]]] = []

        self._trade_lifecycle_messages(state, messages)
        self._operational_alert(state, status, now, messages)
        self._storage_alert(state, status, now, messages)
        self._hourly_report(state, status, now, messages)
        self._control_outcomes(state, messages)

        snapshot = status.as_dict()
        snapshot["status"] = status.state.lower()
        snapshot["observed_at"] = now
        snapshot["commands_enabled"] = self.commands_enabled

        if not state.get("startup_sent"):
            messages.insert(0, (
                "🤖 Bot monitor started\n\n" + render.render_status(status), None
            ))
            state["startup_sent"] = True

        self._deliver(state, messages)
        self._poll_telegram_updates(state, status)
        self._save_state(state)
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot

    def _deliver(self, state: dict, messages: list) -> None:
        """Queue durably, then drain. Telegram being down loses nothing."""
        pending = list(state.get("pending_messages") or [])
        known = {item.get("key") for item in pending if isinstance(item, dict)}
        for message, buttons in messages[:10]:
            key = hashlib.sha256(message.encode("utf-8")).hexdigest()
            if key not in known:
                pending.append({"key": key, "message": message, "buttons": buttons})
                known.add(key)
        remaining = []
        for item in pending[:100]:
            if not self._notify(str(item.get("message", "")), item.get("buttons")):
                remaining.append(item)
        # Bound an unavailable Telegram destination without losing the latest
        # operational events forever or growing PostgreSQL without limit.
        state["pending_messages"] = (remaining + pending[100:])[-100:]

    # -- owner-facing events ----------------------------------------------

    def _trade_lifecycle_messages(self, state: dict, messages: list) -> None:
        """Opened/closed trades are owner-facing facts, not engineering noise."""
        session = self.db.get_session()
        try:
            tracked = [
                int(value) for value in (state.get("trade_states") or {})
                if str(value).isdigit()
            ]
            ownership = or_(TradeLog.run_id == self.run_id, TradeLog.status == "open")
            if tracked:
                ownership = or_(ownership, TradeLog.id.in_(tracked))
            trades = session.query(TradeLog).filter(ownership).order_by(
                TradeLog.id.asc()
            ).all()
            previous = dict(state.get("trade_states") or {})
            for row in trades:
                old = previous.get(str(row.id))
                direction = "LONG" if row.action == "open_long" else "SHORT"
                if old is None and row.status == "open":
                    messages.append((
                        f"📈 Opened {row.symbol} {direction}\n"
                        f"entry={float(row.entry_price):.8g}", None,
                    ))
                elif old is not None and old != "closed" and row.status == "closed":
                    messages.append((
                        f"📊 Closed {row.symbol} {direction}\n"
                        f"P&L {float(row.pnl_usdt or 0):+.2f} USDT · "
                        f"{row.exit_reason or 'unknown reason'}", None,
                    ))
            state["trade_states"] = {str(row.id): row.status for row in trades}
        finally:
            session.close()

    def _operational_alert(self, state, status, now, messages) -> None:
        """Lifecycle communication: problem -> recovery, never per-event spam.

        A transient WebSocket disconnect never reaches this point: it has to
        make the canonical state non-HEALTHY *and* stay that way for the
        escalation window before the owner is told anything.
        """
        escalation = max(0, int(getattr(self.cfg, "telegram_alert_escalation_seconds", 180)))
        reminder = max(60, int(getattr(self.cfg, "telegram_alert_reminder_seconds", 3600)))
        previous = state.get("alert_state")
        notified = state.get("alert_notified_state")

        if status.state != previous:
            state["alert_state"] = status.state
            state["alert_since"] = now.isoformat()
        since = _parse_stamp(state.get("alert_since")) or now
        sustained = (now - since).total_seconds() >= escalation

        if status.state == ops.HEALTHY:
            if notified and notified != ops.HEALTHY:
                messages.append((
                    render.render_recovered(
                        status, resume_offered=status.operator_paused
                    ),
                    [("Resume trading", "resume"), ("Keep paused", "pause")]
                    if status.operator_paused else None,
                ))
            state["alert_notified_state"] = ops.HEALTHY
            state["alert_notified_at"] = now.isoformat()
            return

        if not sustained:
            return
        worsened = notified is None or ops.is_worse_than(status.state, notified)
        last_notified = _parse_stamp(state.get("alert_notified_at"))
        due = (
            last_notified is None or (now - last_notified).total_seconds() >= reminder
        )
        if notified == status.state and not due:
            return
        if not worsened and not due:
            return
        if not status.database_available:
            messages.append((render.render_storage_failure(status), None))
        else:
            messages.append((render.render_problem(status, status.reasons), None))
        state["alert_notified_state"] = status.state
        state["alert_notified_at"] = now.isoformat()

    def _storage_alert(self, state, status, now, messages) -> None:
        """Threshold crossing only: an unchanged level never repeats."""
        ratio = status.database_usage_ratio
        if ratio is None:
            return
        block = min(1.0, max(0.01, float(
            getattr(self.cfg, "storage_entry_block_ratio", 0.85)
        )))
        level = (
            "emergency" if ratio >= min(0.98, block + 0.20)
            else "critical" if ratio >= block
            else "warning" if ratio >= max(0.0, block - 0.15)
            else "normal"
        )
        previous = str(state.get("storage_level") or "normal")
        state["storage_level"] = level
        if level == previous:
            return
        if _STORAGE_LEVEL_ORDER[level] < _STORAGE_LEVEL_ORDER[previous]:
            if level == "normal":
                messages.append((
                    f"✅ PostgreSQL storage back to normal ({ratio:.1%}).", None
                ))
            return
        messages.append((
            render.render_storage_alert(status, level, self._largest_growth_source()),
            None,
        ))

    def _largest_growth_source(self) -> Optional[str]:
        """Name the biggest relation, or say nothing rather than guess."""
        if self.db.engine.dialect.name != "postgresql":
            return None
        session = self.db.get_session()
        try:
            result = session.execute(text(
                "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname = 'public' "
                "ORDER BY 2 DESC LIMIT 1"
            )).first()
            if not result:
                return None
            return f"{result[0]} ({result[1] / 1e6:.0f} MB)"
        except Exception:
            session.rollback()
            return None
        finally:
            session.close()

    def _hourly_report(self, state, status, now, messages) -> None:
        interval = max(1, int(getattr(self.cfg, "telegram_report_interval_minutes", 60)))
        last = _parse_stamp(state.get("last_report_at"))
        if last is not None and (now - last) < timedelta(minutes=interval):
            return
        report = self.reports.build(self.report_period)
        messages.append((render.render_report(report, status), None))
        state["last_report_at"] = now.isoformat()

    def _control_outcomes(self, state, messages) -> None:
        outcomes, highest = self.control.drain_processed(
            int(state.get("last_control_outcome_id") or 0)
        )
        for outcome in outcomes:
            icon = "✅" if outcome.state == "applied" else "⛔"
            messages.append((f"{icon} {outcome.outcome}", None))
        state["last_control_outcome_id"] = highest

    # -- Telegram input ----------------------------------------------------

    def _poll_telegram_updates(self, state: dict, status) -> None:
        if not self.commands_enabled:
            return
        offset = int(state.get("telegram_update_offset") or 0)
        updates = self._updates_fetcher(offset)
        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0))):
            update_id = int(update.get("update_id", 0))
            try:
                if not self._handle_update(update, status):
                    # Delivery failed; retry this update on the next poll
                    # instead of advancing past an unanswered owner request.
                    break
            except Exception:
                logger.exception("Telegram update handling failed")
            offset = max(offset, update_id + 1)
        state["telegram_update_offset"] = offset

    def _authorized(self, chat_id: Any, user_id: Any) -> bool:
        """Both the chat and the human must match the configured owner."""
        return (
            self._authorized_chat_id is not None
            and self._authorized_user_id is not None
            and str(chat_id) == self._authorized_chat_id
            and str(user_id) == self._authorized_user_id
        )

    def _handle_update(self, update: dict, status) -> bool:
        callback = update.get("callback_query")
        if callback:
            return self._handle_callback(callback, status)
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if not self._authorized(chat_id, user_id):
            logger.warning("Ignoring Telegram message from an unauthorized sender")
            return True
        text = str(message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if command in CONTROL_COMMANDS:
            return self._notify(self._request_control(
                CONTROL_COMMANDS[command], user_id
            ))
        if command not in READ_COMMANDS:
            return True
        return self._notify(*self._read_response(command, status))

    def _handle_callback(self, callback: dict, status) -> bool:
        callback_id = str(callback.get("id") or "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (callback.get("from") or {}).get("id")
        if not self._authorized(chat_id, user_id):
            # Authorization applies to buttons exactly as it does to commands:
            # a forwarded message must not become a remote control.
            logger.warning("Ignoring Telegram callback from an unauthorized sender")
            if self._callback_ack and callback_id:
                self._callback_ack(callback_id, "Not authorized")
            return True
        action = CALLBACK_ACTIONS.get(str(callback.get("data") or "").strip())
        if self._callback_ack and callback_id:
            self._callback_ack(callback_id, "")
        if action is None:
            return True
        if action == PAUSE and not status.operator_paused:
            return self._notify(self._request_control(PAUSE, user_id))
        if action == PAUSE:
            return self._notify("Trading stays paused.")
        return self._notify(self._request_control(action, user_id))

    def _request_control(self, command: str, user_id: Any) -> str:
        command_id = self.control.request(
            command, requested_by=str(user_id), run_id=self.run_id,
        )
        if command_id is None:
            return (
                "⛔ The request could not be stored, so it was NOT applied. "
                "Trading state is unchanged."
            )
        if command == RESUME:
            return (
                "⏳ Resume requested. The trading process will re-check market "
                "data, protection and durability before allowing new entries."
            )
        return "⏳ Pause requested. New entries stop as soon as it is applied."

    def _read_response(self, command: str, status) -> tuple[str, Optional[list]]:
        if command == "/help":
            return render.render_help(), None
        if command == "/health":
            return render.render_health(status), None
        if command == "/positions":
            return render.render_positions(self.reports.build(self.report_period)), None
        if command == "/report":
            return render.render_report(
                self.reports.build(self.report_period), status
            ), None
        text = render.render_status(status)
        buttons = None
        if status.operator_paused:
            buttons = [("Resume trading", "resume"), ("Keep paused", "pause")]
        elif status.state in (ops.HEALTHY, ops.DEGRADED):
            buttons = [("Pause trading", "pause")]
        if command == "/start":
            text += "\n\n" + render.render_help()
        return text, buttons

    # -- health endpoint ---------------------------------------------------

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
                code = 200 if payload.get("database_available") else 503
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
