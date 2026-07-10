import argparse
import json
import math
import sys
from pathlib import Path

from analytics.stability import StabilityEngine
from analytics.repository import AnalyticsRepository
from analytics.reliability import ReliabilityThresholds
from config.settings import BybitConfig
from storage.db import Database


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline stability report for Bybit bot analytics.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=10)
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
        report = StabilityEngine(AnalyticsRepository(session), thresholds).build_report()
    finally:
        session.close()

    if args.format == "json":
        content = json.dumps(_json_safe(report), ensure_ascii=False, indent=2, sort_keys=True)
    else:
        content = render_stability_text(report, limit=args.limit)
    _write_or_print(content, args.output)


def render_stability_text(report: dict, limit: int = 10) -> str:
    lines = [
        "BYBIT BOT STABILITY REPORT",
        "=" * 80,
        "Stability measures whether historical edge remains present in recent windows.",
        report["formula"],
        "",
        f"Closed trades: {report['summary']['closed_trades']}",
        f"Windows: {', '.join(map(str, report['summary']['windows']))}",
        "",
        "Best experts right now",
    ]
    lines.extend(_profile_lines(_rank(report["expert_sources"], reverse=True), limit))
    lines.append("")
    lines.append("Experts with degradation")
    lines.extend(_profile_lines(_with_degradation(report["expert_sources"]), limit))
    lines.append("")
    lines.append("Experts improving")
    lines.extend(_profile_lines(_with_trend(report["expert_sources"], "improving"), limit))
    lines.append("")
    lines.append("Best symbols")
    lines.extend(_profile_lines(_rank(report["symbols"], reverse=True), limit))
    lines.append("")
    lines.append("Worst symbols")
    lines.extend(_profile_lines(_rank(report["symbols"], reverse=False), limit))
    lines.append("")
    lines.append("Best regime")
    lines.extend(_profile_lines(_rank(report["regimes"], reverse=True), limit))
    lines.append("")
    lines.append("Worst regime")
    lines.extend(_profile_lines(_rank(report["regimes"], reverse=False), limit))
    lines.append("")
    lines.append("Best confirmation combinations")
    lines.extend(_profile_lines(_rank(report["confirmation_combinations"], reverse=True), limit))
    lines.append("")
    lines.append("Worst confirmation combinations")
    lines.extend(_profile_lines(_rank(report["confirmation_combinations"], reverse=False), limit))
    return "\n".join(lines)


def _rank(profiles: dict, reverse: bool) -> list[dict]:
    rows = list(profiles.values())
    return sorted(
        rows,
        key=lambda r: (r["stability_score"], _latest_expectancy(r), r["sample_size"]),
        reverse=reverse,
    )


def _with_degradation(profiles: dict) -> list[dict]:
    return sorted(
        [row for row in profiles.values() if row["degradation_flags"]],
        key=lambda r: (len(r["degradation_flags"]), -r["stability_score"]),
        reverse=True,
    )


def _with_trend(profiles: dict, trend: str) -> list[dict]:
    return sorted(
        [row for row in profiles.values() if row["trend"] == trend],
        key=lambda r: r["stability_score"],
        reverse=True,
    )


def _profile_lines(rows: list[dict], limit: int) -> list[str]:
    if not rows:
        return ["  none"]
    return [
        "  {dimension}:{key} score={score:.1f} trend={trend} confidence={confidence} "
        "reliability={reliability} trades={sample_size} PF20={pf20} Exp20={exp20} flags={flags}".format(
            dimension=row["dimension"],
            key=row["key"],
            score=row["stability_score"],
            trend=row["trend"],
            confidence=row["confidence"],
            reliability=row["reliability"],
            sample_size=row["sample_size"],
            pf20=_fmt(row["windows"]["20"]["profit_factor"]),
            exp20=_fmt(row["windows"]["20"]["expectancy_usdt"]),
            flags=",".join(row["degradation_flags"]) or "none",
        )
        for row in rows[:limit]
    ]


def _latest_expectancy(profile: dict) -> float:
    return profile["windows"]["20"].get("expectancy_usdt") or profile["windows"]["all"].get("expectancy_usdt") or 0.0


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
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
