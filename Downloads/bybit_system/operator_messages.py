"""Owner-facing message rendering. Engineering detail stays in the logs.

Messages are deliberately plain text: Telegram is called without a
``parse_mode``, so a symbol or an exchange reason can never be interpreted as
markup, and no escaping bug can mangle or truncate an operational alert.

Nothing here decides anything. Rendering is separated from the health model and
the control layer so that a wording change can never alter trading behaviour.
"""

from __future__ import annotations

from typing import Optional

import operational_status as ops
from operational_status import OperationalStatus, humanize_age
from reporting import TradingReport

_STATE_ICON = {
    ops.HEALTHY: "✅", ops.DEGRADED: "⚠️", ops.PAUSED: "⏸", ops.STOPPED: "🛑",
}


def _usdt(value: Optional[float], *, signed: bool = True) -> str:
    if value is None:
        return "unavailable"
    return f"{value:+.2f} USDT" if signed else f"{value:.2f} USDT"


def _bytes(value: Optional[float]) -> str:
    if not value:
        return "unknown"
    return f"{value / 1e9:.1f} GB"


def _database_line(status: OperationalStatus) -> str:
    if not status.database_available:
        return "Database: UNAVAILABLE"
    if status.database_usage_ratio is None:
        return "Database: OK (no quota configured)"
    return f"Database: {status.database_usage_ratio:.1%} used"


def _market_data_line(status: OperationalStatus) -> str:
    if status.market_data_age_seconds is None:
        return "Bybit data: no data stored yet"
    age = humanize_age(status.market_data_age_seconds)
    fresh = "fresh" if status.market_data_age_seconds <= 120 else "STALE"
    source = status.market_data_source or "market data"
    return f"Bybit data: {fresh} ({source} {age})"


def render_status(status: OperationalStatus) -> str:
    """Short answer to "is my bot working?"."""
    icon = _STATE_ICON.get(status.state, "•")
    lines = [
        f"{icon} {status.state} — {status.summary}",
        f"run={status.run_id}" + (" · TESTNET" if status.testnet else ""),
        "",
        f"Collector: {'OK' if status.collector_healthy else 'STALE'}"
        f" · Trader: {'OK' if status.trader_healthy else 'STALE'}",
        _market_data_line(status),
        _database_line(status),
    ]
    if status.uptime_seconds is not None:
        lines.insert(2, f"Uptime: {humanize_age(status.uptime_seconds).removesuffix(' ago')}")
    if status.position_state_available:
        lines.append(f"Open positions: {status.open_positions}")
    else:
        lines.append("Open positions: UNAVAILABLE (state could not be read)")
    lines.append(
        "New entries: allowed" if status.entries_allowed
        else "New entries: BLOCKED — " + ", ".join(status.entry_block_reasons)
    )
    if status.reasons:
        lines.append("")
        lines.extend(f"• {reason}" for reason in status.reasons[:5])
    return "\n".join(lines)


def render_health(status: OperationalStatus) -> str:
    """The same model as /status, with the per-component detail spelled out."""
    lines = [
        f"{_STATE_ICON.get(status.state, '•')} Health: {status.state}",
        "",
        f"Trader heartbeat:    {humanize_age(status.trader_age_seconds)}",
        f"Collector heartbeat: {humanize_age(status.collector_age_seconds)}",
        f"Market data:         {humanize_age(status.market_data_age_seconds)}"
        + (f" ({status.market_data_source})" if status.market_data_source else ""),
        f"Database:            {'available' if status.database_available else 'UNAVAILABLE'}"
        + (
            f", {status.database_usage_ratio:.1%} used"
            if status.database_usage_ratio is not None else ""
        ),
    ]
    outbox = status.outbox or {}
    if outbox:
        lines.append(
            "Telemetry outbox:    "
            + ", ".join(f"{key}={value}" for key, value in sorted(outbox.items()))
        )
    if status.breaker_causes:
        lines.append("")
        lines.append(f"Circuit breaker: ON ({len(status.breaker_causes)} cause(s))")
        for key, value in list(status.breaker_causes.items())[:5]:
            lines.append(f"• {value.get('reason', key)}")
    else:
        lines.append("")
        lines.append("Circuit breaker: off")
    if status.position_state_reason:
        lines.append(f"Note: {status.position_state_reason}")
    return "\n".join(lines)


def render_positions(report: TradingReport) -> str:
    if not report.position_state_available:
        return (
            "⚠️ Position state is UNAVAILABLE.\n"
            f"{report.position_state_reason or 'The trading state could not be read.'}\n"
            "This is not the same as having no positions."
        )
    if not report.open_positions:
        return "📭 No open positions."
    width = max(len(item.symbol) for item in report.open_positions)
    lines = [f"📌 Open positions: {len(report.open_positions)}", ""]
    for item in report.open_positions:
        lines.append(
            f"{item.symbol:<{width}}  {item.side:<5} {_usdt(item.unrealized_pnl):>16}"
        )
    if report.unrealized_unavailable_count:
        lines.append("")
        lines.append(
            f"{report.unrealized_unavailable_count} position(s) have no fresh "
            "valuation yet; their P&L is not included."
        )
    return "\n".join(lines)


def render_report(report: TradingReport, status: OperationalStatus) -> str:
    """The hourly consolidated report."""
    stamp = report.generated_at.strftime("%Y-%m-%d %H:%M UTC") if report.generated_at else ""
    lines = [
        "📊 TRADING REPORT",
        f"{stamp} · {report.period_label}",
        "",
        "STATUS",
        f"Bot: {status.state} — {status.summary}",
    ]
    if status.uptime_seconds is not None:
        lines.append(
            f"Uptime: {humanize_age(status.uptime_seconds).removesuffix(' ago')}"
        )
    lines.append(_market_data_line(status))
    lines.append(_database_line(status))

    lines += ["", "TRADES"]
    lines.append(
        f"Open: {report.open_position_count}" if report.position_state_available
        else "Open: UNAVAILABLE (state could not be read)"
    )
    lines.append(f"Closed ({report.period_label}): {report.closed_trades}")
    if report.unresolved_trades:
        lines.append(
            f"Unresolved: {report.unresolved_trades} (result unknown, excluded)"
        )

    lines += ["", "PNL"]
    lines.append(f"Realized:   {_usdt(report.realized_pnl)}")
    lines.append(f"Unrealized: {_usdt(report.unrealized_pnl)}")

    lines += ["", "PERFORMANCE"]
    if report.closed_trades:
        lines.append(
            f"Win rate:     {report.win_rate:.1f}% "
            f"({report.wins}/{report.closed_trades})"
        )
        lines.append(f"Gross profit: {_usdt(report.gross_profit)}")
        lines.append(f"Gross loss:   {_usdt(report.gross_loss)}")
        lines.append(f"Fees:         {report.fees:.2f} USDT")
    else:
        lines.append(f"No trades closed in this period ({report.period_label}).")

    if report.best_symbol or report.worst_symbol:
        lines += ["", "BEST / WORST"]
        if report.best_symbol:
            lines.append(
                f"Best:  {report.best_symbol.symbol} "
                f"{_usdt(report.best_symbol.realized_pnl)}"
            )
        if report.worst_symbol:
            lines.append(
                f"Worst: {report.worst_symbol.symbol} "
                f"{_usdt(report.worst_symbol.realized_pnl)}"
            )

    lines += ["", "ACTIVE POSITIONS"]
    if not report.position_state_available:
        lines.append("UNAVAILABLE — the trading state could not be read.")
    elif not report.open_positions:
        lines.append("None.")
    else:
        width = max(len(item.symbol) for item in report.open_positions)
        for item in report.open_positions:
            lines.append(
                f"{item.symbol:<{width}}  {item.side:<5} "
                f"{_usdt(item.unrealized_pnl):>16}"
            )
    if not status.entries_allowed:
        lines += ["", "New entries are BLOCKED — " + ", ".join(status.entry_block_reasons)]
    if report.wallet_balance is not None:
        lines += ["", f"Wallet: {report.wallet_balance:.2f} USDT"]
    return "\n".join(lines)


def render_problem(status: OperationalStatus, reasons: list[str]) -> str:
    """Emitted once when the system escalates into a genuinely bad state."""
    protection = (
        "confirmed by the exchange" if status.entries_allowed or status.breaker_causes
        else "being monitored"
    )
    lines = [
        "⚠️ BOT WARNING",
        "",
        f"Status: {status.summary}",
        "Reason: " + (reasons[0] if reasons else "an operational check failed"),
    ]
    for extra in reasons[1:4]:
        lines.append(f"        {extra}")
    lines += [
        "",
        "Automatic recovery is being attempted.",
        "",
        f"Open positions: "
        + (str(status.open_positions) if status.position_state_available else "UNAVAILABLE"),
        f"Protection status: {protection}",
        "New entries: "
        + ("allowed" if status.entries_allowed else "blocked"),
        "",
        f"Last market update: {humanize_age(status.market_data_age_seconds)}",
    ]
    return "\n".join(lines)


def render_recovered(status: OperationalStatus, *, resume_offered: bool) -> str:
    lines = [
        "✅ BOT RECOVERED",
        "",
        "Bybit connection restored.",
        _market_data_line(status),
        "Safety checks passed.",
    ]
    if resume_offered:
        lines += [
            "",
            "Trading remains paused pending your approval.",
            "Send /resume to allow new entries, or /pause to stay paused.",
        ]
    else:
        lines += ["", f"Status: {status.state} — {status.summary}"]
    return "\n".join(lines)


def render_storage_alert(
    status: OperationalStatus, level: str, largest_source: Optional[str],
) -> str:
    ratio = status.database_usage_ratio
    lines = [
        "🗄 DATABASE " + ("EMERGENCY" if level == "emergency" else level.upper()),
        "",
        f"PostgreSQL storage: {ratio:.1%}" if ratio is not None else
        "PostgreSQL storage: usage unknown",
    ]
    if largest_source:
        lines += ["", "Largest source of growth:", largest_source]
    lines += [
        "",
        "Trading remains operational."
        if status.entries_allowed else
        "New entries are blocked until usage falls below the safety threshold.",
    ]
    return "\n".join(lines)


def render_storage_failure(status: OperationalStatus) -> str:
    return "\n".join([
        "🛑 TRADING PAUSED",
        "",
        "The database cannot safely persist new trading state.",
        "New entries have been disabled.",
        "Existing positions are still being monitored.",
        "",
        "Open positions: "
        + (str(status.open_positions) if status.position_state_available
           else "UNAVAILABLE (state could not be read)"),
    ])


def render_help() -> str:
    return "\n".join([
        "Available commands:",
        "",
        "/status    — is the bot working right now",
        "/report    — full trading report",
        "/positions — open positions and their P&L",
        "/health    — per-component health detail",
        "/pause     — block new entries (positions stay managed)",
        "/resume    — request resume; deterministic safety checks must pass",
    ])
