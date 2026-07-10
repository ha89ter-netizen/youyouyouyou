from datetime import datetime, timedelta, timezone
from typing import Optional

from analytics.attribution import (
    fractional_attribution_rows,
    full_attribution_rows,
    normalize_families,
)
from analytics.metrics import group_by, result_metrics, safe_float
from analytics.reliability import ReliabilityThresholds, reliability_status
from analytics.repository import AnalyticsFilters, AnalyticsRepository


class AnalyticsEngine:
    def __init__(
        self,
        repository: AnalyticsRepository,
        thresholds: ReliabilityThresholds = ReliabilityThresholds(),
    ):
        self.repository = repository
        self.thresholds = thresholds

    def build_report(self, filters: AnalyticsFilters, min_sample: int = 20, attribution: str = "full") -> dict:
        trades = self.repository.load_closed_trades(filters)
        all_trades = self.repository.load_all_closed_trades()
        attributed = fractional_attribution_rows(trades) if attribution == "fractional" else full_attribution_rows(trades)

        report = {
            "filters": filters.__dict__,
            "attribution": attribution,
            "strategy": result_metrics(trades, thresholds=self.thresholds),
            "data_quality": data_quality_summary(trades),
            "breakdowns": self._breakdowns(trades),
            "experts": {
                "source": group_by(attributed, lambda r: r.get("expert_source"), self.thresholds, pnl_key="attributed_pnl_usdt"),
                "family": group_by(attributed, lambda r: r.get("expert_family"), self.thresholds, pnl_key="attributed_pnl_usdt"),
            },
            "expert_details": self._expert_details(attributed),
            "confirmation_combinations": self._confirmation_combinations(trades),
            "best_worst_groups": self._best_worst_groups(trades, min_sample),
            "recent_comparison": self._recent_comparison(all_trades, filters.last_trades or 100),
            "exit_type_analysis": group_by(trades, lambda r: r.get("exit_type") or "unknown", self.thresholds),
            "warnings": self._warnings(trades, min_sample),
            "openai_audit": openai_audit(),
        }
        return report

    def _breakdowns(self, trades: list[dict]) -> dict:
        return {
            "direction": group_by(trades, lambda r: r.get("direction"), self.thresholds),
            "symbol": group_by(trades, lambda r: r.get("symbol"), self.thresholds),
            "regime": group_by(trades, lambda r: r.get("regime"), self.thresholds),
            "trend": group_by(trades, lambda r: r.get("trend"), self.thresholds),
            "volatility_state": group_by(trades, lambda r: r.get("volatility_state"), self.thresholds),
            "liquidity_state": group_by(trades, lambda r: r.get("liquidity_state"), self.thresholds),
            "volume_state": group_by(trades, lambda r: r.get("volume_state"), self.thresholds),
            "funding_state": group_by(trades, lambda r: r.get("funding_state"), self.thresholds),
            "open_interest_state": group_by(trades, lambda r: r.get("open_interest_state"), self.thresholds),
            "primary_interval": group_by(trades, lambda r: r.get("primary_interval"), self.thresholds),
            "exit_type": group_by(trades, lambda r: r.get("exit_type") or "unknown", self.thresholds),
            "confirmation_count": group_by(trades, lambda r: r.get("confirmation_count"), self.thresholds),
            "confirmation_families": group_by(trades, lambda r: normalize_families(r.get("confirmation_families")), self.thresholds),
            "decision_confidence_bucket": group_by(trades, lambda r: bucket(safe_float(r.get("decision_confidence")), [0.5, 0.6, 0.7, 0.8, 0.9]), self.thresholds),
            "expected_rr_bucket": group_by(trades, lambda r: bucket(safe_float(r.get("expected_rr")), [1.0, 1.5, 2.0, 2.5, 3.0]), self.thresholds),
            "holding_time_bucket": group_by(trades, lambda r: holding_bucket(r.get("holding_seconds")), self.thresholds),
            "hour_utc": group_by(trades, lambda r: r.get("closed_at").hour if r.get("closed_at") else None, self.thresholds),
            "weekday_utc": group_by(trades, lambda r: r.get("closed_at").strftime("%A") if r.get("closed_at") else None, self.thresholds),
        }

    def _expert_details(self, attributed: list[dict]) -> dict:
        details = {}
        for source, rows in _group_records(attributed, lambda r: r.get("expert_source")).items():
            details[source] = {
                "metrics": result_metrics(rows, pnl_key="attributed_pnl_usdt", thresholds=self.thresholds),
                "average_confidence": _mean([safe_float(r.get("expert_confidence")) for r in rows if safe_float(r.get("expert_confidence")) is not None]),
                "median_confidence": _median([safe_float(r.get("expert_confidence")) for r in rows if safe_float(r.get("expert_confidence")) is not None]),
                "contributed_count": sum(1 for r in rows if r.get("contributed_to_final_decision")),
                "direction": group_by(rows, lambda r: r.get("direction"), self.thresholds, pnl_key="attributed_pnl_usdt"),
                "regime": group_by(rows, lambda r: r.get("regime"), self.thresholds, pnl_key="attributed_pnl_usdt"),
                "symbol": group_by(rows, lambda r: r.get("symbol"), self.thresholds, pnl_key="attributed_pnl_usdt"),
                "exit_type": group_by(rows, lambda r: r.get("exit_type"), self.thresholds, pnl_key="attributed_pnl_usdt"),
            }
        return details

    def _confirmation_combinations(self, trades: list[dict]) -> dict:
        return group_by(trades, lambda r: normalize_families(r.get("confirmation_families")), self.thresholds)

    def _best_worst_groups(self, trades: list[dict], min_sample: int) -> dict:
        candidates = []
        for group_name, groups in self._breakdowns(trades).items():
            for key, metrics in groups.items():
                if metrics["sample_size"] >= min_sample:
                    candidates.append({
                        "group": group_name,
                        "key": key,
                        "sample_size": metrics["sample_size"],
                        "net_pnl_usdt": metrics["net_pnl_usdt"],
                        "profit_factor": metrics["profit_factor"],
                        "reliability": metrics["reliability"],
                    })
        candidates.sort(key=lambda r: r["net_pnl_usdt"])
        return {"worst": candidates[:10], "best": list(reversed(candidates[-10:]))}

    def _recent_comparison(self, all_trades: list[dict], last_n: int) -> dict:
        return {
            "all_time": result_metrics(all_trades, thresholds=self.thresholds),
            "last_7_days": result_metrics(_by_days(all_trades, 7), thresholds=self.thresholds),
            "last_30_days": result_metrics(_by_days(all_trades, 30), thresholds=self.thresholds),
            "last_n_trades": result_metrics(all_trades[-last_n:] if last_n else [], thresholds=self.thresholds),
            "last_n": last_n,
        }

    def _warnings(self, trades: list[dict], min_sample: int) -> list[str]:
        warnings = []
        total = len(trades)
        if total < min_sample:
            warnings.append(f"sample_size={total} below min_sample={min_sample}; conclusions are not reliable")
        unknown_exit = sum(1 for r in trades if (r.get("exit_type") or "unknown") == "unknown")
        if total and unknown_exit / total >= 0.30:
            warnings.append(f"unknown exit_type is high: {unknown_exit}/{total}")
        for name, metrics in self._breakdowns(trades).items():
            small = [key for key, value in metrics.items() if value["reliability"] == "insufficient"]
            if small:
                warnings.append(f"{name}: {len(small)} groups have insufficient data")
        return warnings


def data_quality_summary(trades: list[dict]) -> dict:
    def missing(key):
        return sum(1 for r in trades if not r.get(key))

    suspicious_holding = 0
    old_incompatible = 0
    for row in trades:
        holding = row.get("holding_seconds")
        if holding is not None and holding < 0:
            suspicious_holding += 1
        if not row.get("entry_snapshot") or not row.get("expert_votes"):
            old_incompatible += 1
        if (
            row.get("opened_at")
            and row.get("closed_at")
            and _as_aware_utc(row["opened_at"]) > _as_aware_utc(row["closed_at"])
        ):
            suspicious_holding += 1

    return {
        "closed_trades": len(trades),
        "missing_entry_snapshot": missing("entry_snapshot"),
        "missing_exit_snapshot": missing("exit_snapshot"),
        "missing_expert_votes": sum(1 for r in trades if not r.get("expert_votes")),
        "missing_confirmation_families": missing("confirmation_families"),
        "unknown_exit_type": sum(1 for r in trades if (r.get("exit_type") or "unknown") == "unknown"),
        "null_pnl_pct": sum(1 for r in trades if r.get("pnl_pct") is None),
        "suspicious_holding_seconds": suspicious_holding,
        "empty_order_link_id": sum(1 for r in trades if not r.get("order_link_id")),
        "legacy_partial_rows": old_incompatible,
    }


def openai_audit() -> dict:
    return {
        "ai_market_analyst": "deterministic; no HTTP/OpenAI call",
        "strategy_ai_strategy": "contains OpenAI Chat Completions HTTP call",
        "live_strategy_engine_uses": "AIMarketAnalyst",
        "openai_strategy_class_used_by_live_engine": False,
        "current_live_openai_calls_per_hour": 0,
    }


def bucket(value: Optional[float], edges: list[float]) -> str:
    if value is None:
        return "unknown"
    previous = None
    for edge in edges:
        if value < edge:
            return f"<{edge}" if previous is None else f"{previous}-{edge}"
        previous = edge
    return f">={edges[-1]}"


def holding_bucket(seconds) -> str:
    value = safe_float(seconds)
    if value is None:
        return "unknown"
    if value < 3600:
        return "<1h"
    if value < 4 * 3600:
        return "1-4h"
    if value < 24 * 3600:
        return "4-24h"
    return ">=24h"


def _by_days(trades: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        r for r in trades
        if r.get("closed_at") and _as_aware_utc(r["closed_at"]) >= cutoff
    ]


def _as_aware_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _group_records(records: list[dict], key_fn) -> dict:
    grouped = {}
    for row in records:
        key = key_fn(row) or "unknown"
        grouped.setdefault(str(key), []).append(row)
    return grouped


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2
