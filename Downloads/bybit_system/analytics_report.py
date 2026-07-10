import argparse
import csv
import json
import math
import sys
from pathlib import Path

from analytics.engine import AnalyticsEngine
from analytics.repository import AnalyticsFilters, AnalyticsRepository
from analytics.reliability import ReliabilityThresholds
from config.settings import BybitConfig
from storage.db import Database


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline analytics report for closed Bybit bot trades.")
    parser.add_argument("--window", choices=["all"], default="all")
    parser.add_argument("--days", type=int)
    parser.add_argument("--last-trades", type=int)
    parser.add_argument("--symbol")
    parser.add_argument("--direction", choices=["long", "short"])
    parser.add_argument("--regime")
    parser.add_argument("--trend")
    parser.add_argument("--expert")
    parser.add_argument("--family")
    parser.add_argument("--exit-type")
    parser.add_argument("--min-sample", type=int, default=20)
    parser.add_argument("--attribution", choices=["full", "fractional"], default="full")
    parser.add_argument("--format", choices=["text", "json", "csv"], default="text")
    parser.add_argument("--output")
    parser.add_argument("--insufficient-threshold", type=int, default=20)
    parser.add_argument("--preliminary-threshold", type=int, default=50)
    parser.add_argument("--usable-threshold", type=int, default=200)
    return parser


def main():
    args = build_arg_parser().parse_args()
    cfg = BybitConfig()
    db = Database(cfg)
    session = db.get_session()
    try:
        thresholds = ReliabilityThresholds(
            insufficient=args.insufficient_threshold,
            preliminary=args.preliminary_threshold,
            usable=args.usable_threshold,
        )
        filters = AnalyticsFilters(
            days=args.days,
            last_trades=args.last_trades,
            symbol=args.symbol,
            direction=args.direction,
            regime=args.regime,
            trend=args.trend,
            expert=args.expert,
            family=args.family,
            exit_type=args.exit_type,
        )
        engine = AnalyticsEngine(AnalyticsRepository(session), thresholds)
        report = engine.build_report(filters, min_sample=args.min_sample, attribution=args.attribution)
    finally:
        session.close()

    if args.format == "json":
        content = json.dumps(_json_safe(report), ensure_ascii=False, indent=2, sort_keys=True)
        _write_or_print(content, args.output)
    elif args.format == "csv":
        content = _report_csv(report)
        _write_or_print(content, args.output)
    else:
        _write_or_print(_report_text(report), args.output)


def _report_text(report: dict) -> str:
    lines = [
        "BYBIT BOT ANALYTICS REPORT",
        "=" * 80,
        f"Attribution mode: {report['attribution']}",
        "Full attribution gives every participating expert the full trade result; "
        "fractional attribution splits PnL between contributed experts only.",
        "",
        "1. Strategy summary",
    ]
    lines.extend(_metrics_lines(report["strategy"]))
    lines.append("")
    lines.append("2. Data-quality summary")
    for key, value in report["data_quality"].items():
        lines.append(f"  {key}: {value}")
    lines.append("")

    section_map = [
        ("3. LONG vs SHORT", "direction"),
        ("4. Best/worst symbols", "symbol"),
        ("5. Regime breakdown", "regime"),
        ("6. Trend breakdown", "trend"),
        ("7. Exit-type breakdown", "exit_type"),
    ]
    for title, key in section_map:
        lines.append(title)
        lines.extend(_group_lines(report["breakdowns"].get(key, {}), limit=12))
        lines.append("")

    lines.append("8. Expert source statistics")
    lines.extend(_group_lines(report["experts"]["source"], limit=20))
    lines.append("")
    lines.append("9. Expert family statistics")
    lines.extend(_group_lines(report["experts"]["family"], limit=20))
    lines.append("")
    lines.append("10. Confirmation combinations")
    lines.extend(_group_lines(report["confirmation_combinations"], limit=20))
    lines.append("")
    lines.append("11. Best groups with sufficient sample")
    for row in report["best_worst_groups"]["best"][:10]:
        lines.append(_rank_line(row))
    lines.append("Worst groups with sufficient sample")
    for row in report["best_worst_groups"]["worst"][:10]:
        lines.append(_rank_line(row))
    lines.append("")
    lines.append("12. Recent comparison")
    for name, metrics in report["recent_comparison"].items():
        if isinstance(metrics, dict):
            lines.append(
                f"  {name}: trades={metrics['sample_size']} PF={_fmt(metrics['profit_factor'])} "
                f"net={_fmt(metrics['net_pnl_usdt'])} reliability={metrics['reliability']}"
            )
    lines.append("")
    lines.append("Warnings")
    if report["warnings"]:
        lines.extend(f"  - {warning}" for warning in report["warnings"])
    else:
        lines.append("  none")
    lines.append("")
    lines.append("OpenAI audit")
    for key, value in report["openai_audit"].items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _metrics_lines(metrics: dict) -> list[str]:
    keys = [
        "total_closed_trades", "wins", "losses", "breakeven", "win_rate",
        "gross_profit_usdt", "gross_loss_usdt", "net_pnl_usdt", "average_pnl_usdt",
        "median_pnl_usdt", "average_win_usdt", "average_loss_usdt", "payoff_ratio",
        "profit_factor", "expectancy_usdt", "expectancy_pct", "average_holding_seconds",
        "median_holding_seconds", "maximum_consecutive_wins", "maximum_consecutive_losses",
        "maximum_drawdown_usdt", "recovery_factor", "top_10pct_profit_share",
        "bottom_10pct_loss_share", "reliability",
    ]
    lines = [f"  {key}: {_fmt(metrics.get(key))}" for key in keys]
    best = metrics.get("best_trade")
    worst = metrics.get("worst_trade")
    if best:
        lines.append(f"  best_trade: {best.get('symbol')} {best.get('order_link_id')} pnl={_fmt(best.get('pnl_usdt'))}")
    if worst:
        lines.append(f"  worst_trade: {worst.get('symbol')} {worst.get('order_link_id')} pnl={_fmt(worst.get('pnl_usdt'))}")
    return lines


def _group_lines(groups: dict, limit: int) -> list[str]:
    if not groups:
        return ["  no closed trades"]
    ranked = sorted(groups.items(), key=lambda item: item[1].get("net_pnl_usdt") or 0, reverse=True)
    return [
        f"  {key}: trades={m['sample_size']} wins={m['wins']} losses={m['losses']} "
        f"win_rate={_fmt(m['win_rate'])} net={_fmt(m['net_pnl_usdt'])} "
        f"avg={_fmt(m['average_pnl_usdt'])} PF={_fmt(m['profit_factor'])} "
        f"expectancy={_fmt(m['expectancy_usdt'])} avg_hold={_fmt(m['average_holding_seconds'])} "
        f"max_dd={_fmt(m['maximum_drawdown_usdt'])} reliability={m['reliability']}"
        for key, m in ranked[:limit]
    ]


def _rank_line(row: dict) -> str:
    return (
        f"  {row['group']}={row['key']}: trades={row['sample_size']} "
        f"net={_fmt(row['net_pnl_usdt'])} PF={_fmt(row['profit_factor'])} "
        f"reliability={row['reliability']}"
    )


def _report_csv(report: dict) -> str:
    import io

    f = io.StringIO()
    writer = csv.DictWriter(
        f,
        fieldnames=["section", "group", "sample_size", "wins", "losses", "win_rate", "net_pnl_usdt", "profit_factor", "reliability"],
    )
    writer.writeheader()
    writer.writerow(_metrics_csv_row("strategy", "all", report["strategy"]))
    for section, groups in report["breakdowns"].items():
        for key, metrics in groups.items():
            writer.writerow(_metrics_csv_row(section, key, metrics))
    for section, groups in report["experts"].items():
        for key, metrics in groups.items():
            writer.writerow(_metrics_csv_row(f"expert_{section}", key, metrics))
    return f.getvalue()


def _metrics_csv_row(section: str, group: str, metrics: dict) -> dict:
    return {
        "section": section,
        "group": group,
        "sample_size": metrics.get("sample_size"),
        "wins": metrics.get("wins"),
        "losses": metrics.get("losses"),
        "win_rate": metrics.get("win_rate"),
        "net_pnl_usdt": metrics.get("net_pnl_usdt"),
        "profit_factor": _fmt(metrics.get("profit_factor")),
        "reliability": metrics.get("reliability"),
    }


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return value


def _write_or_print(content: str, output: str = None):
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content + "\n")


if __name__ == "__main__":
    main()
