import math
from datetime import timezone
from dataclasses import dataclass
from typing import Callable, Optional

from analytics.attribution import normalize_families
from analytics.metrics import result_metrics, safe_float
from analytics.repository import AnalyticsFilters, AnalyticsRepository
from analytics.reliability import ReliabilityThresholds, reliability_status


WINDOWS = ("all", 200, 100, 50, 20, 10)


@dataclass(frozen=True)
class StabilityConfig:
    min_stable_sample: int = 20
    strong_sample: int = 200
    pf_drop_threshold: float = 0.35
    win_rate_drop_threshold: float = 0.15
    expectancy_drop_threshold: float = 0.50


class StabilityEngine:
    def __init__(
        self,
        repository: AnalyticsRepository,
        thresholds: ReliabilityThresholds = ReliabilityThresholds(),
        config: StabilityConfig = StabilityConfig(),
    ):
        self.repository = repository
        self.thresholds = thresholds
        self.config = config

    def build_report(self) -> dict:
        trades = self.repository.load_closed_trades(AnalyticsFilters())
        return {
            "formula": stability_formula_description(),
            "expert_sources": self._analyze_dimension("expert_source", self._expert_source_groups(trades)),
            "expert_families": self._analyze_dimension("expert_family", self._expert_family_groups(trades)),
            "symbols": self._analyze_dimension("symbol", _group_by(trades, lambda r: r.get("symbol"))),
            "regimes": self._analyze_dimension("regime", _group_by(trades, lambda r: r.get("regime"))),
            "confirmation_combinations": self._analyze_dimension(
                "confirmation_combination",
                _group_by(trades, lambda r: normalize_families(r.get("confirmation_families"))),
            ),
            "summary": {
                "closed_trades": len(trades),
                "windows": list(WINDOWS),
                "min_stable_sample": self.config.min_stable_sample,
            },
        }

    def _analyze_dimension(self, dimension: str, groups: dict[str, list[dict]]) -> dict[str, dict]:
        return {
            key: stability_profile(dimension, key, rows, self.thresholds, self.config)
            for key, rows in sorted(groups.items(), key=lambda item: item[0])
        }

    @staticmethod
    def _expert_source_groups(trades: list[dict]) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        seen = set()
        for trade in trades:
            for vote in trade.get("expert_votes", []):
                source = vote.get("source") or "unknown"
                key = (trade.get("order_link_id"), source)
                if key in seen:
                    continue
                seen.add(key)
                groups.setdefault(source, []).append(trade)
        return groups

    @staticmethod
    def _expert_family_groups(trades: list[dict]) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        seen = set()
        for trade in trades:
            for vote in trade.get("expert_votes", []):
                family = vote.get("family") or "unknown"
                key = (trade.get("order_link_id"), family)
                if key in seen:
                    continue
                seen.add(key)
                groups.setdefault(family, []).append(trade)
        return groups


def stability_profile(
    dimension: str,
    key: str,
    trades: list[dict],
    thresholds: ReliabilityThresholds = ReliabilityThresholds(),
    config: StabilityConfig = StabilityConfig(),
) -> dict:
    ordered = sorted(trades, key=_time_sort_key)
    window_metrics = {str(window): _window_metrics(ordered, window, thresholds) for window in WINDOWS}
    score_parts = _score_parts(ordered, window_metrics, config)
    degradation = degradation_flags(window_metrics, config)
    trend = performance_trend(window_metrics, degradation, config)
    confidence = trend_confidence(window_metrics, config)
    score = round(100 * sum(score_parts.values()) / len(score_parts), 2)
    if window_metrics["all"]["sample_size"] < config.min_stable_sample:
        score = min(score, 45.0)
    return {
        "dimension": dimension,
        "key": key,
        "sample_size": window_metrics["all"]["sample_size"],
        "reliability": reliability_status(window_metrics["all"]["sample_size"], thresholds),
        "trend": trend,
        "confidence": confidence,
        "stability_score": score,
        "score_parts": {k: round(v, 4) for k, v in score_parts.items()},
        "degradation_flags": degradation,
        "windows": window_metrics,
    }


def _window_metrics(ordered: list[dict], window, thresholds: ReliabilityThresholds) -> dict:
    rows = ordered if window == "all" else ordered[-int(window):]
    metrics = result_metrics(rows, thresholds=thresholds)
    return {
        "sample_size": metrics["sample_size"],
        "profit_factor": metrics["profit_factor"],
        "expectancy_usdt": metrics["expectancy_usdt"],
        "win_rate": metrics["win_rate"],
        "average_pnl_usdt": metrics["average_pnl_usdt"],
        "average_holding_seconds": metrics["average_holding_seconds"],
        "reliability": metrics["reliability"],
    }


def _score_parts(ordered: list[dict], windows: dict[str, dict], config: StabilityConfig) -> dict[str, float]:
    all_n = windows["all"]["sample_size"]
    sample_score = min(all_n / config.strong_sample, 1.0)
    pf_values = [_bounded_pf(windows[str(w)]["profit_factor"]) for w in WINDOWS if windows[str(w)]["sample_size"] >= min(config.min_stable_sample, all_n)]
    wr_values = [_metric_value(windows[str(w)]["win_rate"]) for w in WINDOWS if windows[str(w)]["sample_size"] >= min(config.min_stable_sample, all_n)]
    exp_values = [_metric_value(windows[str(w)]["expectancy_usdt"]) for w in WINDOWS if windows[str(w)]["sample_size"] >= min(config.min_stable_sample, all_n)]
    pnl_values = [safe_float(r.get("pnl_usdt")) for r in ordered if safe_float(r.get("pnl_usdt")) is not None]
    return {
        "sample_size": sample_score,
        "profit_factor_stability": _stability_from_values(pf_values),
        "win_rate_stability": _range_stability(wr_values, scale=1.0),
        "degradation_resilience": _degradation_resilience(windows, config),
        "dispersion_control": _pnl_dispersion_score(pnl_values),
    }


def degradation_flags(windows: dict[str, dict], config: StabilityConfig = StabilityConfig()) -> list[str]:
    flags = []
    all_m = windows["all"]
    recent = _preferred_recent_window(windows, config)
    if recent is None:
        return ["insufficient_recent_sample"]

    all_pf = _bounded_pf(all_m["profit_factor"])
    recent_pf = _bounded_pf(recent["profit_factor"])
    if all_pf > 0 and recent_pf < all_pf * (1 - config.pf_drop_threshold):
        flags.append("profit_factor_drop")

    all_wr = all_m.get("win_rate")
    recent_wr = recent.get("win_rate")
    if all_wr is not None and recent_wr is not None and recent_wr < all_wr - config.win_rate_drop_threshold:
        flags.append("win_rate_drop")

    all_exp = all_m.get("expectancy_usdt")
    recent_exp = recent.get("expectancy_usdt")
    if all_exp is not None and recent_exp is not None:
        if all_exp > 0 and recent_exp < 0:
            flags.append("expectancy_negative")
        elif all_exp > 0 and recent_exp < all_exp * (1 - config.expectancy_drop_threshold):
            flags.append("expectancy_drop")

    all_avg = all_m.get("average_pnl_usdt")
    recent_avg = recent.get("average_pnl_usdt")
    if all_avg is not None and recent_avg is not None and all_avg > 0 and recent_avg < all_avg * 0.5:
        flags.append("recent_average_pnl_much_worse")
    return flags


def performance_trend(windows: dict[str, dict], degradation: list[str], config: StabilityConfig = StabilityConfig()) -> str:
    if windows["all"]["sample_size"] < config.min_stable_sample:
        return "unstable"
    if "insufficient_recent_sample" in degradation:
        return "unstable"
    if len([flag for flag in degradation if flag != "insufficient_recent_sample"]) >= 2:
        return "weakening"

    all_m = windows["all"]
    recent = _preferred_recent_window(windows, config)
    if recent is None:
        return "unstable"

    improving = _recent_better(recent, all_m)
    weakening = bool(degradation) or _recent_worse(recent, all_m)
    dispersion = _metric_dispersion([
        _bounded_pf(windows[str(w)]["profit_factor"])
        for w in (100, 50, 20)
        if windows[str(w)]["sample_size"] >= config.min_stable_sample
    ])
    if dispersion is not None and dispersion > 0.45:
        return "unstable"
    if improving and not weakening:
        return "improving"
    if weakening:
        return "weakening"
    return "stable"


def trend_confidence(windows: dict[str, dict], config: StabilityConfig = StabilityConfig()) -> str:
    n = windows["all"]["sample_size"]
    has_50 = windows["50"]["sample_size"] >= 50
    has_20 = windows["20"]["sample_size"] >= 20
    if n >= 200 and has_50:
        return "high"
    if n >= 50 and has_20:
        return "medium"
    if n >= config.min_stable_sample:
        return "low"
    return "very_low"


def stability_formula_description() -> str:
    return (
        "Stability Score = 100 * average of five normalized components: "
        "sample_size=min(n/200,1), profit_factor_stability=1/(1+coefficient_of_variation(PF windows)), "
        "win_rate_stability=1-range(win_rate windows), degradation_resilience=1-flagged_degradations/4, "
        "dispersion_control=1/(1+std(PnL)/mean(abs(PnL))). "
        "If sample_size < 20, final score is capped at 45. Windows: all, 200, 100, 50, 20, 10."
    )


def _group_by(records: list[dict], key_fn: Callable[[dict], Optional[str]]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in records:
        key = key_fn(row) or "unknown"
        groups.setdefault(str(key), []).append(row)
    return groups


def _time_sort_key(row: dict):
    value = row.get("closed_at") or row.get("opened_at")
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _preferred_recent_window(windows: dict[str, dict], config: StabilityConfig) -> Optional[dict]:
    for name in ("20", "50", "100", "10"):
        if windows[name]["sample_size"] >= min(config.min_stable_sample, windows["all"]["sample_size"]):
            return windows[name]
    return None


def _recent_better(recent: dict, base: dict) -> bool:
    pf_ok = _bounded_pf(recent["profit_factor"]) >= _bounded_pf(base["profit_factor"]) * 1.10
    wr_ok = (recent.get("win_rate") is not None and base.get("win_rate") is not None and recent["win_rate"] >= base["win_rate"] + 0.05)
    exp_ok = (recent.get("expectancy_usdt") is not None and base.get("expectancy_usdt") is not None and recent["expectancy_usdt"] > base["expectancy_usdt"])
    return sum([pf_ok, wr_ok, exp_ok]) >= 2


def _recent_worse(recent: dict, base: dict) -> bool:
    pf_bad = _bounded_pf(recent["profit_factor"]) < _bounded_pf(base["profit_factor"]) * 0.80
    wr_bad = (recent.get("win_rate") is not None and base.get("win_rate") is not None and recent["win_rate"] < base["win_rate"] - 0.10)
    exp_bad = (recent.get("expectancy_usdt") is not None and base.get("expectancy_usdt") is not None and recent["expectancy_usdt"] < base["expectancy_usdt"])
    return sum([pf_bad, wr_bad, exp_bad]) >= 2


def _bounded_pf(value) -> float:
    if value is None:
        return 1.0
    if isinstance(value, float) and math.isinf(value):
        return 10.0
    return max(0.0, min(float(value), 10.0))


def _metric_value(value) -> float:
    if value is None:
        return 0.0
    return float(value)


def _stability_from_values(values: list[float]) -> float:
    dispersion = _metric_dispersion(values)
    if dispersion is None:
        return 0.0
    return 1.0 / (1.0 + dispersion)


def _range_stability(values: list[float], scale: float) -> float:
    if not values:
        return 0.0
    return max(0.0, 1.0 - (max(values) - min(values)) / scale)


def _metric_dispersion(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if abs(mean) < 1e-12:
        return 1.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance) / abs(mean)


def _degradation_resilience(windows: dict[str, dict], config: StabilityConfig) -> float:
    flags = [flag for flag in degradation_flags(windows, config) if flag != "insufficient_recent_sample"]
    if "insufficient_recent_sample" in degradation_flags(windows, config):
        return 0.25
    return max(0.0, 1.0 - min(len(flags), 4) / 4)


def _pnl_dispersion_score(pnls: list[float]) -> float:
    if len(pnls) < 2:
        return 0.0
    avg_abs = sum(abs(p) for p in pnls) / len(pnls)
    if avg_abs < 1e-12:
        return 1.0
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
    return 1.0 / (1.0 + math.sqrt(variance) / avg_abs)
