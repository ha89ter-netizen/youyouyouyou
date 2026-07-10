from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from config.settings import BybitConfig
from storage.db import Database
from storage.models import TradeLog


def _dec(value) -> Decimal:
    return Decimal(str(value or 0))


def _avg(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")


def _side(action: str) -> str:
    return "LONG" if action == "open_long" else "SHORT" if action == "open_short" else action


def compute_trade_report_stats(rows: Iterable[TradeLog]) -> dict:
    rows = list(rows)
    closed = [r for r in rows if r.status == "closed"]
    open_rows = [r for r in rows if r.status == "open"]

    pnls = [_dec(r.pnl_usdt) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls, Decimal("0"))
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    profit_factor = None if gross_loss == 0 else gross_profit / gross_loss

    best_trade = max(closed, key=lambda r: _dec(r.pnl_usdt), default=None)
    worst_trade = min(closed, key=lambda r: _dec(r.pnl_usdt), default=None)
    holding_values = [
        Decimal(int(r.holding_seconds))
        for r in closed
        if getattr(r, "holding_seconds", None) is not None
    ]

    by_side = _group_stats(closed, lambda r: _side(r.action))
    by_symbol = _group_stats(closed, lambda r: r.symbol)
    by_confirmation = _group_stats(
        closed,
        lambda r: (getattr(r, "confirmation_families", None) or getattr(r, "source", None) or "unknown"),
    )

    return {
        "total_trades": len(rows),
        "open_count": len(open_rows),
        "closed_count": len(closed),
        "total_pnl": total_pnl,
        "win_rate": (Decimal(len(wins)) / Decimal(len(closed)) * Decimal("100")) if closed else Decimal("0"),
        "average_win": _avg(wins),
        "average_loss": _avg(losses),
        "profit_factor": profit_factor,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "average_holding_seconds": _avg(holding_values),
        "by_side": by_side,
        "by_symbol": by_symbol,
        "by_confirmation": by_confirmation,
    }


def _group_stats(rows: list[TradeLog], key_fn) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)

    result = {}
    for key, group in sorted(grouped.items(), key=lambda item: str(item[0])):
        pnls = [_dec(r.pnl_usdt) for r in group]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_loss = abs(sum(losses, Decimal("0")))
        result[key] = {
            "count": len(group),
            "pnl": sum(pnls, Decimal("0")),
            "win_rate": (Decimal(len(wins)) / Decimal(len(group)) * Decimal("100")) if group else Decimal("0"),
            "profit_factor": None if gross_loss == 0 else sum(wins, Decimal("0")) / gross_loss,
        }
    return result


def _fmt_money(value: Decimal) -> str:
    return f"{value:.4f}"


def _fmt_pct(value: Decimal) -> str:
    return f"{value:.2f}%"


def _fmt_pf(value) -> str:
    return "inf" if value is None else f"{value:.2f}"


def _fmt_seconds(value: Decimal) -> str:
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"


def build_report_text(rows: Iterable[TradeLog], limit: int = 20) -> str:
    rows = list(rows)
    stats = compute_trade_report_stats(rows)
    lines = [
        "=" * 80,
        "BYBIT BOT TRADE REPORT",
        "=" * 80,
    ]

    if not rows:
        lines.append("Журнал пустой. Сделок пока не было.")
        return "\n".join(lines)

    lines.extend([
        f"Всего сделок       : {stats['total_trades']}",
        f"Открытых           : {stats['open_count']}",
        f"Закрытых           : {stats['closed_count']}",
        f"Общий PnL          : {_fmt_money(stats['total_pnl'])} USDT",
        f"Win rate           : {_fmt_pct(stats['win_rate'])}",
        f"Средняя прибыль    : {_fmt_money(stats['average_win'])} USDT",
        f"Средний убыток     : {_fmt_money(stats['average_loss'])} USDT",
        f"Profit factor      : {_fmt_pf(stats['profit_factor'])}",
        f"Среднее удержание  : {_fmt_seconds(stats['average_holding_seconds'])}",
    ])

    if stats["best_trade"]:
        best = stats["best_trade"]
        lines.append(f"Лучший трейд       : {best.symbol} {best.action} pnl={_fmt_money(_dec(best.pnl_usdt))} USDT")
    if stats["worst_trade"]:
        worst = stats["worst_trade"]
        lines.append(f"Худший трейд       : {worst.symbol} {worst.action} pnl={_fmt_money(_dec(worst.pnl_usdt))} USDT")

    lines.append("-" * 80)
    lines.append("LONG vs SHORT:")
    lines.extend(_format_group_stats(stats["by_side"]))

    lines.append("-" * 80)
    lines.append("По символам:")
    lines.extend(_format_group_stats(stats["by_symbol"]))

    lines.append("-" * 80)
    lines.append("По confirmation families / source:")
    lines.extend(_format_group_stats(stats["by_confirmation"]))

    lines.append("-" * 80)
    lines.append(f"Последние {min(limit, len(rows))} сделок:")
    for r in rows[:limit]:
        pnl = _dec(r.pnl_usdt)
        lines.append(
            f"{r.opened_at} | {r.symbol} | {r.action} | entry={r.entry_price} | "
            f"exit={r.exit_price} | size={r.size_usdt} | pnl={_fmt_money(pnl)} | "
            f"status={r.status} | exit_reason={getattr(r, 'exit_reason', None)} | "
            f"holding={getattr(r, 'holding_seconds', None)}"
        )

    return "\n".join(lines)


def _format_group_stats(groups: dict) -> list[str]:
    if not groups:
        return ["  нет закрытых сделок"]
    return [
        f"  {key}: count={value['count']}, pnl={_fmt_money(value['pnl'])} USDT, "
        f"win_rate={_fmt_pct(value['win_rate'])}, profit_factor={_fmt_pf(value['profit_factor'])}"
        for key, value in groups.items()
    ]


def main():
    cfg = BybitConfig()
    db = Database(cfg)
    session = db.get_session()

    try:
        rows = (
            session.query(TradeLog)
            .order_by(TradeLog.opened_at.desc())
            .all()
        )
        print(build_report_text(rows))
    finally:
        session.close()


if __name__ == "__main__":
    main()
