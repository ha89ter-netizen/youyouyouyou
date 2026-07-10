from analytics.metrics import safe_float


def normalize_families(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value).replace("+", ",").replace("|", ",").split(",")
    families = sorted({str(item).strip().lower() for item in raw if str(item).strip()})
    return ",".join(families) if families else "unknown"


def full_attribution_rows(trades: list[dict]) -> list[dict]:
    rows = []
    for trade in trades:
        for vote in trade.get("expert_votes", []):
            row = dict(trade)
            row["expert_source"] = vote.get("source") or "unknown"
            row["expert_family"] = vote.get("family") or "unknown"
            row["expert_confidence"] = safe_float(vote.get("confidence"))
            row["contributed_to_final_decision"] = bool(vote.get("contributed_to_final_decision"))
            row["attributed_pnl_usdt"] = safe_float(trade.get("pnl_usdt"))
            rows.append(row)
    return rows


def fractional_attribution_rows(trades: list[dict]) -> list[dict]:
    rows = []
    for trade in trades:
        contributors = [
            vote for vote in trade.get("expert_votes", [])
            if vote.get("contributed_to_final_decision")
        ]
        if not contributors:
            continue
        pnl = safe_float(trade.get("pnl_usdt"))
        attributed = pnl / len(contributors) if pnl is not None else None
        for vote in contributors:
            row = dict(trade)
            row["expert_source"] = vote.get("source") or "unknown"
            row["expert_family"] = vote.get("family") or "unknown"
            row["expert_confidence"] = safe_float(vote.get("confidence"))
            row["contributed_to_final_decision"] = True
            row["attributed_pnl_usdt"] = attributed
            rows.append(row)
    return rows
