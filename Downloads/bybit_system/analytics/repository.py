from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from analytics.attribution import normalize_families
from analytics.metrics import safe_float
from storage.models import TradeExpertVote, TradeLog


@dataclass
class AnalyticsFilters:
    days: Optional[int] = None
    last_trades: Optional[int] = None
    symbol: Optional[str] = None
    direction: Optional[str] = None
    regime: Optional[str] = None
    trend: Optional[str] = None
    expert: Optional[str] = None
    family: Optional[str] = None
    exit_type: Optional[str] = None


class AnalyticsRepository:
    def __init__(self, session: Session):
        self.session = session

    def load_closed_trades(self, filters: AnalyticsFilters = AnalyticsFilters()) -> list[dict]:
        query = self.session.query(TradeLog).filter(TradeLog.status == "closed")
        if filters.days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=filters.days)
            query = query.filter(TradeLog.closed_at >= cutoff)
        if filters.symbol:
            query = query.filter(TradeLog.symbol == filters.symbol.upper())
        if filters.direction:
            action = "open_long" if filters.direction.lower() == "long" else "open_short"
            query = query.filter(TradeLog.action == action)
        if filters.regime:
            query = query.filter(TradeLog.regime == filters.regime)
        if filters.trend:
            query = query.filter(TradeLog.trend == filters.trend)
        if filters.exit_type:
            query = query.filter(TradeLog.exit_type == filters.exit_type)

        rows = query.order_by(TradeLog.closed_at.asc(), TradeLog.id.asc()).all()
        if filters.last_trades:
            rows = rows[-filters.last_trades:]

        deduped = []
        seen = set()
        for row in rows:
            key = row.order_link_id or f"missing-id-{row.id}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        vote_map = self._load_votes([row.order_link_id for row in deduped if row.order_link_id])
        trades = [self._row_to_dict(row, vote_map.get(row.order_link_id, [])) for row in deduped]

        if filters.expert:
            trades = [
                trade for trade in trades
                if any(v.get("source") == filters.expert for v in trade.get("expert_votes", []))
            ]
        if filters.family:
            trades = [
                trade for trade in trades
                if any(v.get("family") == filters.family for v in trade.get("expert_votes", []))
            ]
        return trades

    def load_all_closed_trades(self) -> list[dict]:
        return self.load_closed_trades(AnalyticsFilters())

    def _load_votes(self, order_ids: list[str]) -> dict[str, list[dict]]:
        if not order_ids:
            return {}
        rows = (
            self.session.query(TradeExpertVote)
            .filter(TradeExpertVote.order_link_id.in_(order_ids))
            .order_by(TradeExpertVote.order_link_id.asc(), TradeExpertVote.source.asc())
            .all()
        )
        vote_map: dict[str, list[dict]] = {}
        seen = set()
        for vote in rows:
            key = (vote.order_link_id, vote.source)
            if key in seen:
                continue
            seen.add(key)
            vote_map.setdefault(vote.order_link_id, []).append({
                "source": vote.source,
                "family": vote.family,
                "action": vote.action,
                "confidence": safe_float(vote.confidence),
                "reason": vote.reason,
                "weight": safe_float(vote.weight),
                "contributed_to_final_decision": bool(vote.contributed_to_final_decision),
            })
        return vote_map

    @staticmethod
    def _row_to_dict(row: TradeLog, votes: list[dict]) -> dict:
        entry_snapshot = row.entry_snapshot or {}
        exit_snapshot = row.exit_snapshot or {}
        market_context = entry_snapshot.get("market_context") or {}
        basic = entry_snapshot.get("basic") or {}
        return {
            "id": row.id,
            "order_link_id": row.order_link_id,
            "symbol": row.symbol,
            "direction": "long" if row.action == "open_long" else "short" if row.action == "open_short" else row.action,
            "action": row.action,
            "source": row.source,
            "entry_price": safe_float(row.entry_price),
            "exit_price": safe_float(row.exit_price),
            "size_usdt": safe_float(row.size_usdt),
            "leverage": row.leverage,
            "pnl_usdt": safe_float(row.pnl_usdt),
            "pnl_pct": safe_float(row.pnl_pct),
            "holding_seconds": row.holding_seconds,
            "opened_at": row.opened_at,
            "closed_at": row.closed_at,
            "exit_reason": row.exit_reason,
            "exit_type": row.exit_type or "unknown",
            "regime": row.regime or market_context.get("regime"),
            "trend": row.trend or market_context.get("trend"),
            "volatility_state": market_context.get("volatility_state"),
            "liquidity_state": market_context.get("liquidity_state"),
            "volume_state": market_context.get("volume_state"),
            "funding_state": market_context.get("funding_state"),
            "open_interest_state": market_context.get("open_interest_state"),
            "primary_interval": basic.get("primary_interval"),
            "decision_confidence": safe_float(row.decision_confidence),
            "expected_rr": safe_float(row.expected_rr),
            "confirmation_count": row.confirmation_count,
            "confirmation_families": row.confirmation_families,
            "confirmation_combo": normalize_families(row.confirmation_families),
            "entry_snapshot": entry_snapshot,
            "exit_snapshot": exit_snapshot,
            "expert_votes": votes,
        }
