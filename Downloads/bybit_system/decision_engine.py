"""
Decision Engine.

Собирает мнения независимых экспертов и выпускает итоговый Signal плюс
TradeDecisionReport: почему победил LONG/SHORT/HOLD, кто голосовал, кто был
проигнорирован и какой риск у решения.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from market_context import MarketContext
from meta_strategy import MetaStrategyDecision
from strategy.signal import Action, Signal


@dataclass
class ExpertVote:
    source: str
    action: Action
    confidence: float
    reason: str
    expected_rr: Optional[float] = None
    ignored: bool = False
    ignored_reason: str = ""


@dataclass
class TradeDecisionReport:
    symbol: str
    market_context: MarketContext
    votes: List[ExpertVote]
    final_signal: Signal
    winning_action: Action
    rejected_actions: Dict[str, str] = field(default_factory=dict)
    risk_score: float = 0.0
    expected_rr: Optional[float] = None
    confidence: float = 0.0
    confirmation_count: int = 0
    confirmation_families: List[str] = field(default_factory=list)
    meta_notes: List[str] = field(default_factory=list)
    ai_analysis: Optional[str] = None

    def as_log_text(self) -> str:
        active_votes = [
            f"{v.source}={v.action.value}({v.confidence:.2f})"
            for v in self.votes if not v.ignored
        ]
        hold_reasons = [
            f"{v.source}: {v.reason}" for v in self.votes
            if not v.ignored and v.action == Action.HOLD and v.reason
        ]
        ignored_votes = [
            f"{v.source}: {v.ignored_reason}" for v in self.votes if v.ignored
        ]
        chunks = [
            f"Market Context: {self.market_context.summary()}",
            "Голоса: " + (", ".join(active_votes) if active_votes else "нет активных голосов"),
            f"Итог: {self.winning_action.value}, confidence={self.confidence:.2f}, "
            f"risk_score={self.risk_score:.2f}, expected_rr={self.expected_rr}",
            "Подтверждения: "
            + (
                f"{self.confirmation_count} независимых семейств ({', '.join(self.confirmation_families)})"
                if self.confirmation_families else "нет"
            ),
        ]
        if self.rejected_actions:
            chunks.append(
                "Отклонено: "
                + "; ".join(f"{action}: {reason}" for action, reason in self.rejected_actions.items())
            )
        if hold_reasons:
            chunks.append("Причины HOLD экспертов: " + "; ".join(hold_reasons[:4]))
        if ignored_votes:
            chunks.append("Проигнорированы: " + "; ".join(ignored_votes))
        if self.meta_notes:
            chunks.append("Meta Strategy: " + "; ".join(self.meta_notes))
        if self.ai_analysis:
            chunks.append("AI Analyst: " + self.ai_analysis)
        return " | ".join(chunks)

    def journal_reason(self, limit: int = 1000) -> str:
        return self.as_log_text()[:limit]


class DecisionEngine:
    def __init__(
        self,
        min_open_confidence: float = 0.45,
        min_margin: float = 0.08,
        min_rr: float = 2.0,
        default_stop_loss_pct: float = 1.5,
        default_take_profit_rr: float = 2.0,
        min_confirming_families: int = 2,
    ):
        self.min_open_confidence = min_open_confidence
        self.min_margin = min_margin
        self.min_rr = min_rr
        self.default_stop_loss_pct = default_stop_loss_pct
        self.default_take_profit_rr = default_take_profit_rr
        self.min_confirming_families = min_confirming_families

    def decide(
        self,
        symbol: str,
        context: MarketContext,
        meta: MetaStrategyDecision,
        expert_signals: List[Signal],
        ai_analysis: Optional[str] = None,
    ) -> TradeDecisionReport:
        votes = self._build_votes(expert_signals, meta)
        scores = self._score_votes(votes, context)
        winning_action = max(scores, key=scores.get)

        long_score = scores[Action.OPEN_LONG]
        short_score = scores[Action.OPEN_SHORT]
        hold_score = scores[Action.HOLD]
        best_open = Action.OPEN_LONG if long_score >= short_score else Action.OPEN_SHORT
        best_open_score = max(long_score, short_score)
        opposite_score = min(long_score, short_score)

        rejected: Dict[str, str] = {}
        base_signal = self._select_base_signal(expert_signals, best_open)
        open_votes = [
            v for v in votes if not v.ignored and v.action == best_open
        ]
        confirmation_families = self._confirmation_families(open_votes)
        expected_rr = self._expected_rr(open_votes, base_signal)

        if best_open_score < self.min_open_confidence:
            winning_action = Action.HOLD
            rejected[best_open.value] = (
                f"Суммарная уверенность {best_open_score:.2f} ниже порога {self.min_open_confidence:.2f}"
            )
        elif len(confirmation_families) < self.min_confirming_families:
            winning_action = Action.HOLD
            rejected[best_open.value] = (
                f"Недостаточно независимых подтверждений: "
                f"{len(confirmation_families)}/{self.min_confirming_families} "
                f"({', '.join(confirmation_families) if confirmation_families else 'нет активных семейств'})"
            )
        elif best_open_score - opposite_score < self.min_margin and opposite_score > 0:
            winning_action = Action.HOLD
            rejected[best_open.value] = (
                f"Недостаточный перевес над противоположным сценарием "
                f"({best_open_score:.2f} vs {opposite_score:.2f})"
            )
        elif expected_rr is not None and expected_rr < self.min_rr:
            winning_action = Action.HOLD
            rejected[best_open.value] = f"RR {expected_rr:.2f} ниже минимального {self.min_rr:.2f}"
        elif hold_score > best_open_score and best_open_score < self.min_open_confidence + 0.03:
            winning_action = Action.HOLD
            rejected[best_open.value] = f"HOLD-сценарий сильнее ({hold_score:.2f} vs {best_open_score:.2f})"
        else:
            winning_action = best_open
            if best_open == Action.OPEN_LONG and short_score > 0:
                rejected[Action.OPEN_SHORT.value] = f"LONG набрал больше веса ({long_score:.2f} vs {short_score:.2f})"
            if best_open == Action.OPEN_SHORT and long_score > 0:
                rejected[Action.OPEN_LONG.value] = f"SHORT набрал больше веса ({short_score:.2f} vs {long_score:.2f})"
            if hold_score > 0:
                rejected[Action.HOLD.value] = f"Активный сценарий сильнее HOLD ({best_open_score:.2f} vs {hold_score:.2f})"

        winning_votes = [
            v for v in votes if not v.ignored and v.action == winning_action and winning_action != Action.HOLD
        ]
        base_signal = self._select_base_signal(expert_signals, winning_action)
        confidence = self._final_confidence(scores, winning_action, context)
        if winning_action != Action.HOLD:
            expected_rr = self._expected_rr(winning_votes, base_signal)
            confirmation_families = self._confirmation_families(winning_votes)
        else:
            confirmation_families = []

        if winning_action == Action.HOLD:
            final_signal = Signal(
                symbol=symbol,
                action=Action.HOLD,
                source="decision:committee",
                confidence=confidence,
                reason=self._hold_reason(rejected, scores),
            )
        else:
            decision_source = self._decision_source(winning_votes)
            stop_loss_pct = (base_signal.stop_loss_pct if base_signal else None) or self.default_stop_loss_pct
            take_profit_pct = (
                (base_signal.take_profit_pct if base_signal else None)
                or stop_loss_pct * self.default_take_profit_rr
            )
            final_signal = Signal(
                symbol=symbol,
                action=winning_action,
                source=decision_source,
                confidence=confidence,
                reason=self._decision_reason(winning_action, winning_votes, context),
                suggested_size_usdt=base_signal.suggested_size_usdt if base_signal else None,
                suggested_leverage=base_signal.suggested_leverage if base_signal else None,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )

        report = TradeDecisionReport(
            symbol=symbol,
            market_context=context,
            votes=votes,
            final_signal=final_signal,
            winning_action=winning_action,
            rejected_actions=rejected,
            risk_score=context.risk_score,
            expected_rr=expected_rr,
            confidence=confidence,
            confirmation_count=len(confirmation_families),
            confirmation_families=confirmation_families,
            meta_notes=meta.notes,
            ai_analysis=ai_analysis,
        )
        final_signal.reason = report.journal_reason()
        return report

    @staticmethod
    def _build_votes(signals: List[Signal], meta: MetaStrategyDecision) -> List[ExpertVote]:
        votes: List[ExpertVote] = []
        for signal in signals:
            ignored = not meta.is_allowed(signal.source)
            permission = meta.permissions.get(signal.source)
            ignored_reason = (
                permission.reason if permission is not None
                else "Meta Strategy Manager не разрешил этому эксперту голосовать"
            )
            votes.append(ExpertVote(
                source=signal.source,
                action=signal.action,
                confidence=signal.confidence,
                reason=signal.reason,
                expected_rr=DecisionEngine._rr_from_signal(signal),
                ignored=ignored,
                ignored_reason=ignored_reason if ignored else "",
            ))
        return votes

    @staticmethod
    def _score_votes(votes: List[ExpertVote], context: MarketContext) -> Dict[Action, float]:
        grouped = {Action.OPEN_LONG: [], Action.OPEN_SHORT: [], Action.HOLD: []}
        for vote in votes:
            if vote.ignored:
                continue
            weight = vote.confidence
            if vote.action == Action.HOLD:
                # HOLD-голос эксперта — это диагностическая осторожность, а не
                # самостоятельное veto против всех OPEN-сценариев.
                weight *= 0.45
            if vote.action == Action.OPEN_LONG and context.trend == "UP":
                weight *= 1.12
            elif vote.action == Action.OPEN_SHORT and context.trend == "DOWN":
                weight *= 1.12
            elif vote.action in (Action.OPEN_LONG, Action.OPEN_SHORT) and context.trend != "NEUTRAL":
                expected_trend = "UP" if vote.action == Action.OPEN_LONG else "DOWN"
                if context.trend != expected_trend:
                    weight *= 0.65
            if context.liquidity == "LOW" and vote.action != Action.HOLD:
                weight *= 0.75
            if vote.action in grouped:
                grouped[vote.action].append(weight)

        scores = {}
        for action, weights in grouped.items():
            if not weights:
                scores[action] = 0.0
                continue
            # Сравниваем силу каждого сценария отдельно. Несколько согласных
            # экспертов дают небольшой бонус, но один уверенный эксперт не
            # исчезает просто потому, что остальные честно сказали HOLD.
            average = sum(weights) / len(weights)
            support_bonus = min(len(weights) - 1, 3) * (0.025 if action == Action.HOLD else 0.05)
            cap = 0.45 if action == Action.HOLD else 0.98
            scores[action] = round(min(average + support_bonus, cap), 4)
        return scores

    @staticmethod
    def _source_family(source: str) -> str:
        name = source.split(":", 1)[-1]
        mapping = {
            "ema": "trend",
            "vwap": "price_location",
            "momentum": "trade_flow",
            "orderbook": "microstructure",
            "funding": "positioning",
            "rsi": "mean_reversion",
            "committee": "multi_indicator",
        }
        return mapping.get(name, name)

    @classmethod
    def _confirmation_families(cls, votes: List[ExpertVote]) -> List[str]:
        families: List[str] = []
        for vote in sorted(votes, key=lambda v: v.confidence, reverse=True):
            family = cls._source_family(vote.source)
            if family not in families:
                families.append(family)
        return families

    @staticmethod
    def _select_base_signal(signals: List[Signal], action: Action) -> Optional[Signal]:
        candidates = [s for s in signals if s.action == action]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.confidence)

    @staticmethod
    def _rr_from_signal(signal: Signal) -> Optional[float]:
        if not signal.stop_loss_pct or not signal.take_profit_pct:
            return None
        if signal.stop_loss_pct <= 0:
            return None
        return round(signal.take_profit_pct / signal.stop_loss_pct, 2)

    @staticmethod
    def _expected_rr(votes: List[ExpertVote], base_signal: Optional[Signal]) -> Optional[float]:
        rrs = [v.expected_rr for v in votes if v.expected_rr is not None]
        if rrs:
            return round(sum(rrs) / len(rrs), 2)
        return DecisionEngine._rr_from_signal(base_signal) if base_signal else None

    @staticmethod
    def _final_confidence(scores: Dict[Action, float], action: Action, context: MarketContext) -> float:
        base = scores.get(action, 0.0)
        if action == Action.HOLD:
            base = max(scores[Action.HOLD], 1.0 - max(scores[Action.OPEN_LONG], scores[Action.OPEN_SHORT]))
        confidence = 0.60 * base + 0.40 * context.confidence
        confidence *= 1.0 - min(context.risk_score, 0.8) * 0.25
        return round(max(0.0, min(confidence, 0.95)), 3)

    @staticmethod
    def _decision_reason(action: Action, votes: List[ExpertVote], context: MarketContext) -> str:
        supporters = ", ".join(v.source for v in votes) or "нет явных сторонников"
        families = ", ".join(DecisionEngine._confirmation_families(votes)) or "нет"
        direction = "LONG" if action == Action.OPEN_LONG else "SHORT"
        return f"{direction} победил: {supporters}. Независимые подтверждения: {families}. Контекст: {context.summary()}"

    @staticmethod
    def _decision_source(votes: List[ExpertVote]) -> str:
        if not votes:
            return "decision:committee"
        short_names = []
        for vote in votes:
            name = vote.source.split(":", 1)[-1]
            if name not in short_names:
                short_names.append(name)
        source = "decision:" + "+".join(short_names)
        return source[:50]

    @staticmethod
    def _hold_reason(rejected: Dict[str, str], scores: Dict[Action, float]) -> str:
        if rejected:
            return "HOLD: " + "; ".join(f"{k}: {v}" for k, v in rejected.items())
        return (
            f"HOLD: нет достаточного преимущества. "
            f"long={scores[Action.OPEN_LONG]:.2f}, short={scores[Action.OPEN_SHORT]:.2f}, "
            f"hold={scores[Action.HOLD]:.2f}"
        )
