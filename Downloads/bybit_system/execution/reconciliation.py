"""Pure, conservative linkage of internal trades to Bybit closed-PnL rows.

The matcher deliberately refuses ambiguous price/time matches.  Stable exchange
identifiers win; price, side, time, and quantity are only eligibility checks.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional


MATCHED = "MATCHED"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"


def _decimal(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def closed_qty(record: dict) -> Optional[Decimal]:
    """Actual closed quantity; ``qty`` may be the original order quantity."""
    return _decimal(record.get("closedSize") or record.get("qty"))


def _expected_close_side(action: Optional[str]) -> Optional[str]:
    if action == "open_long":
        return "Sell"
    if action == "open_short":
        return "Buy"
    return None


def _dedupe_records(records: Iterable[dict]):
    by_id = {}
    conflicts = set()
    anonymous = []
    for record in records:
        oid = record.get("orderId")
        if not oid:
            anonymous.append(record)
            continue
        previous = by_id.get(oid)
        if previous is None:
            by_id[oid] = record
        elif previous != record:
            conflicts.add(oid)
    return list(by_id.values()) + anonymous, conflicts


def _record_matches_trade(trade: dict, record: dict, tolerance_pct: Decimal) -> bool:
    if record.get("symbol") and record.get("symbol") != trade.get("symbol"):
        return False
    expected_side = _expected_close_side(trade.get("action"))
    if expected_side and record.get("side") and record.get("side") != expected_side:
        return False

    entry = _decimal(trade.get("entry_price"))
    avg_entry = _decimal(record.get("avgEntryPrice"))
    if not entry or not avg_entry or entry <= 0:
        return False
    if abs(avg_entry - entry) / entry * 100 > tolerance_pct:
        return False

    try:
        closed_ms = int(record.get("updatedTime") or record.get("createdTime"))
    except (TypeError, ValueError):
        return False
    opened_ms = trade.get("opened_at_ms")
    if opened_ms is not None and closed_ms < int(opened_ms):
        return False
    return True


def _expected_qty(trade: dict) -> Optional[Decimal]:
    exact = _decimal(trade.get("entry_filled_qty"))
    if exact and exact > 0:
        return exact
    size = _decimal(trade.get("size_usdt"))
    entry = _decimal(trade.get("entry_price"))
    if size and entry and entry > 0:
        return size / entry
    return None


def _order_index(order_history: Iterable[dict]) -> dict:
    result = {}
    for order in order_history:
        oid = order.get("orderId")
        if oid:
            result[oid] = order
    return result


def _strong_owner(trade: dict, record: dict, order_by_id: dict) -> bool:
    oid = record.get("orderId")
    if oid and oid == trade.get("submitted_exit_order_id"):
        return True
    known = set(trade.get("known_exchange_exit_order_ids") or [])
    if oid and oid in known:
        return True
    order = order_by_id.get(oid) or {}
    return bool(
        order.get("parentOrderLinkId")
        and order.get("parentOrderLinkId") == trade.get("order_link_id")
    )


def _aggregate(records: list[dict]) -> dict:
    qty_total = sum((closed_qty(r) or Decimal("0")) for r in records)
    weighted_exit = sum(
        (closed_qty(r) or Decimal("0")) * (_decimal(r.get("avgExitPrice")) or Decimal("0"))
        for r in records
    )
    result = dict(records[0])
    result["records"] = records
    result["orderIds"] = [r.get("orderId") for r in records if r.get("orderId")]
    if qty_total:
        result["closedSize"] = str(qty_total)
        result["qty"] = str(qty_total)
        result["avgExitPrice"] = str(weighted_exit / qty_total)
    for field in ("closedPnl", "openFee", "closeFee"):
        values = [_decimal(r.get(field)) for r in records]
        result[field] = str(sum(v for v in values if v is not None))
    result["fillCount"] = str(sum(int(r.get("fillCount") or 0) for r in records))
    result["updatedTime"] = str(max(int(r.get("updatedTime") or r.get("createdTime") or 0) for r in records))
    return result


def plan_closed_pnl_reconciliation(
    trades: list[dict],
    records: list[dict],
    order_history: Optional[list[dict]] = None,
    tolerance_pct: float = 0.5,
    qty_tolerance_pct: float = 0.5,
) -> list[dict]:
    """Return one-to-one/one-to-many matches without mutating inputs.

    Multiple closed-PnL rows are accepted only when their actual ``closedSize``
    sums to the filled entry quantity.  A record that is eligible for more than
    one trade remains unresolved unless a stable order identifier gives it one
    strong owner.
    """
    unique_records, duplicate_conflicts = _dedupe_records(records)
    order_by_id = _order_index(order_history or [])
    price_tolerance = Decimal(str(tolerance_pct))
    qty_tolerance = Decimal(str(qty_tolerance_pct))

    candidates = defaultdict(list)
    owners = defaultdict(list)
    strong_owners = defaultdict(list)
    for ti, trade in enumerate(trades):
        for ri, record in enumerate(unique_records):
            if not _record_matches_trade(trade, record, price_tolerance):
                continue
            candidates[ti].append(ri)
            owners[ri].append(ti)
            if _strong_owner(trade, record, order_by_id):
                strong_owners[ri].append(ti)

    plan = []
    for ti, trade in enumerate(trades):
        eligible = candidates[ti]
        selected = []
        contested = []
        for ri in eligible:
            record = unique_records[ri]
            oid = record.get("orderId")
            if oid in duplicate_conflicts:
                contested.append(ri)
                continue
            strong = strong_owners[ri]
            if strong:
                if strong == [ti]:
                    selected.append(ri)
                else:
                    contested.append(ri)
            elif owners[ri] == [ti]:
                selected.append(ri)
            else:
                contested.append(ri)

        if not selected:
            status = AMBIGUOUS if eligible else NOT_FOUND
            plan.append({"trade": trade, "status": status, "record": None,
                         "note": "no uniquely owned closed-PnL record"})
            continue

        expected = _expected_qty(trade)
        selected_quantities = [closed_qty(unique_records[i]) for i in selected]
        if any(q is None for q in selected_quantities):
            if len(selected) != 1 or contested:
                plan.append({"trade": trade, "status": AMBIGUOUS, "record": None,
                             "note": "closed quantity unavailable for multi-record match"})
                continue
            selected_records = [unique_records[selected[0]]]
            plan.append({"trade": trade, "status": MATCHED,
                         "record": _aggregate(selected_records), "note": "quantity unavailable"})
            continue
        selected_qty = sum(selected_quantities, Decimal("0"))
        if expected and expected > 0:
            qty_diff_pct = abs(selected_qty - expected) / expected * 100
            if qty_diff_pct > qty_tolerance:
                plan.append({
                    "trade": trade, "status": AMBIGUOUS, "record": None,
                    "note": f"unique closed quantity {selected_qty} != filled quantity {expected}",
                })
                continue
        elif len(selected) != 1:
            plan.append({"trade": trade, "status": AMBIGUOUS, "record": None,
                         "note": "multiple records but filled entry quantity is unavailable"})
            continue

        if contested:
            # A full exact quantity is sufficient: contested rows cannot also
            # belong to this trade without exceeding its exchange fill.
            if not expected or abs(selected_qty - expected) / expected * 100 > qty_tolerance:
                plan.append({"trade": trade, "status": AMBIGUOUS, "record": None,
                             "note": "additional contested closed-PnL records"})
                continue
        selected_records = sorted(
            (unique_records[i] for i in selected),
            key=lambda r: int(r.get("updatedTime") or r.get("createdTime") or 0),
        )
        plan.append({"trade": trade, "status": MATCHED,
                     "record": _aggregate(selected_records), "note": ""})
    return plan
