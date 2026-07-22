"""
Инструмент оператора для разбора состояния Risk Manager.

Зачем он нужен: состояние Risk Manager персистентно, и circuit breaker с
причиной "неизвестный финансовый результат" намеренно НЕ снимается ни
перезапуском, ни сменой суток. Без этого инструмента единственным способом
возобновить торговлю была бы правка строк в БД руками.

Ордеров не создаёт. К бирже обращаются reconcile, reconcile-open и
investigate-orphans, и все только ЧИТАЮТ (позиции, история ордеров,
исполнения, closed PnL). Команды с --apply по умолчанию работают в dry-run:
запись в журнал происходит исключительно с явным флагом и только для
однозначных вердиктов.

Примеры:
    python risk_admin.py status
    python risk_admin.py reconcile
    python risk_admin.py reconcile --symbol ETHUSDT
    python risk_admin.py investigate-orphans
    python risk_admin.py investigate-orphans --apply
    python risk_admin.py resolve-cause "orphan:decision_e-ab12cd34"
    python risk_admin.py unblock ETHUSDT
    python risk_admin.py reset-breaker --yes
"""

import argparse
import logging
import sys
from typing import List, Optional

from config.settings import BybitConfig
from logging_config import configure_app_logging
from storage.db import Database
from storage.journal import TradeJournal
from storage.migrations import run_safe_migrations
from storage.risk_state import RiskStateStore
from risk.risk_manager import RiskManager, orphan_cause
from timeutils import utc_today, utcnow

logger = logging.getLogger("risk_admin")

# Допуски строгого матчера reconcile-open. Цена — как в торговом цикле;
# размер — щедрый, потому что старые записи хранят одобренный номинал,
# а не фактический (округление лота занижает реальный размер).
RECONCILE_PRICE_TOLERANCE_PCT = 0.5
RECONCILE_SIZE_TOLERANCE_PCT = 25.0

# Статусы плана реконсиляции
MATCHED = "MATCHED"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"
LIVE = "LIVE"
API_ERROR = "API_ERROR"

# Статусы глубокого разбора orphaned-сделок
CONFIRMED_CLOSED = "CONFIRMED_CLOSED"
NEVER_FILLED = "NEVER_FILLED"
STILL_LIVE = "STILL_LIVE"

# Статусы ордера Bybit, означающие "исполнения не было и уже не будет".
# Проверяются вместе с cumExecQty: частично исполненный и отменённый ордер
# оставляет реальную экспозицию и в эту категорию не попадает.
_DEAD_ORDER_STATUSES = ("Rejected", "Cancelled", "Deactivated")


def _record_matches_trade(trade: dict, record: dict) -> bool:
    """
    Может ли запись closed PnL принадлежать этой сделке. Критерии:
    символ, направление (сторона закрывающего ордера противоположна позиции),
    время (закрытие не раньше открытия), цена входа с допуском и — если данные
    есть — размер позиции.
    """
    from strategy.engine import StrategyEngine

    if record.get("symbol") and record["symbol"] != trade.get("symbol"):
        return False

    expected_side = StrategyEngine._expected_close_side(trade.get("action"))
    side = record.get("side")
    if expected_side and side and side != expected_side:
        return False

    try:
        avg_entry = float(record.get("avgEntryPrice"))
        created_ms = int(record.get("createdTime"))
    except (TypeError, ValueError):
        return False

    opened_ms = trade.get("opened_at_ms")
    if opened_ms is not None and created_ms < opened_ms:
        return False

    entry_price = trade.get("entry_price") or 0
    if entry_price <= 0:
        return False
    if abs(avg_entry - entry_price) / entry_price * 100 > RECONCILE_PRICE_TOLERANCE_PCT:
        return False

    # Размер — только если обе стороны его знают. qty * avgEntryPrice — номинал
    # закрытой позиции; size_usdt в журнале — одобренный номинал при входе.
    qty_raw = record.get("qty") or record.get("closedSize")
    try:
        qty = float(qty_raw) if qty_raw is not None else None
    except (TypeError, ValueError):
        qty = None
    size_usdt = trade.get("size_usdt") or 0
    if qty and size_usdt > 0:
        notional = qty * avg_entry
        if abs(notional - size_usdt) / size_usdt * 100 > RECONCILE_SIZE_TOLERANCE_PCT:
            return False
    return True


def plan_open_reconciliation(trades: List[dict], records: List[dict]) -> List[dict]:
    """
    Строит план сопоставления открытых сделок с записями closed PnL.
    ЧИСТАЯ функция: ничего не читает и не пишет, только считает.

    Правило единственности: сделка получает MATCHED только если у неё РОВНО
    один кандидат, и этот кандидат не подходит больше никому. Любая другая
    комбинация — AMBIGUOUS: связывать одну запись closed PnL с несколькими
    сделками (или гадать между несколькими записями) значит приписать PnL
    не той сделке.
    """
    candidates = {}
    owners = {}  # индекс записи -> [order_link_id, ...]
    for trade in trades:
        oid = trade["order_link_id"]
        idxs = [i for i, r in enumerate(records) if _record_matches_trade(trade, r)]
        candidates[oid] = idxs
        for i in idxs:
            owners.setdefault(i, []).append(oid)

    plan = []
    for trade in sorted(trades, key=lambda t: t.get("opened_at_ms") or 0):
        oid = trade["order_link_id"]
        idxs = candidates[oid]
        if not idxs:
            plan.append({"trade": trade, "status": NOT_FOUND, "record": None,
                         "note": "подходящих записей closed PnL нет"})
        elif len(idxs) > 1:
            plan.append({"trade": trade, "status": AMBIGUOUS, "record": None,
                         "note": f"{len(idxs)} подходящих записей closed PnL — выбор был бы гаданием"})
        elif len(owners[idxs[0]]) > 1:
            others = [o for o in owners[idxs[0]] if o != oid]
            plan.append({"trade": trade, "status": AMBIGUOUS, "record": None,
                         "note": "единственная подходящая запись также подходит: " + ", ".join(others)})
        else:
            plan.append({"trade": trade, "status": MATCHED, "record": records[idxs[0]], "note": ""})
    return plan


def apply_reconciliation_plan(plan: List[dict], journal: TradeJournal, risk: RiskManager) -> List[dict]:
    """
    Применяет ТОЛЬКО MATCHED-элементы плана. Идемпотентность — та же граница,
    что и в торговом цикле: log_exit().recorded. Уже закрытая сделка вернёт
    recorded=False, и её PnL повторно не учитывается.
    """
    from strategy.engine import StrategyEngine

    applied = []
    for item in plan:
        if item["status"] != MATCHED:
            continue
        trade, record = item["trade"], item["record"]
        oid = trade["order_link_id"]
        exit_price = float(record.get("avgExitPrice") or 0)
        pnl_usdt = float(record.get("closedPnl") or 0)
        closed_at = StrategyEngine._closed_at_from_match(record)
        result = journal.log_exit(
            oid, exit_price, pnl_usdt,
            exit_reason=StrategyEngine._infer_exit_reason(record),
            closed_at=closed_at,
        )
        if not result.recorded:
            applied.append({"order_link_id": oid, "changed": False,
                            "note": "уже закрыта — пропущено (идемпотентность)"})
            continue
        effective = result.closed_at or utcnow()
        counted_today = effective.date() == utc_today()
        if counted_today:
            risk.record_closed_pnl(pnl_usdt)
        applied.append({
            "order_link_id": oid, "changed": True, "pnl_usdt": pnl_usdt,
            "closed_at": effective, "counted_in_daily": counted_today,
        })
    return applied


def _build(cfg: BybitConfig):
    db = Database(cfg)
    if not db.check_connection():
        print("БД недоступна. Запустите docker compose up -d")
        sys.exit(1)
    run_safe_migrations(db.engine)
    journal = TradeJournal(db)
    risk = RiskManager(cfg, state_store=RiskStateStore(db))
    return db, journal, risk


def cmd_status(cfg, args) -> int:
    _, journal, risk = _build(cfg)
    journal_pnl, closed_count = journal.sum_closed_pnl_for_utc_day(utc_today())
    orphaned = journal.get_orphaned_trades()

    print(f"UTC-день:            {utc_today()}")
    print(f"Дневной PnL:         {risk._daily_pnl_usdt:.4f} USDT (состояние)")
    print(f"Дневной PnL:         {journal_pnl:.4f} USDT по журналу, {closed_count} закрытых сделок")
    print(f"Стартовый баланс:    {risk._daily_start_balance}")
    print(f"Сделок за день:      {risk._daily_trade_count}")
    print(f"Circuit breaker:     {'ВЗВЕДЁН' if risk.circuit_breaker_tripped else 'снят'}")

    causes = risk.breaker_causes()
    if causes:
        print("\nПричины circuit breaker (снимаются по одной через resolve-cause):")
        for key, value in causes.items():
            flag = "требует устранения причины" if value["sticky"] else "снимется со сменой UTC-дня"
            print(f"  [{key}] {value['reason']}  ({flag})")

    blocked = risk.blocked_symbols()
    if blocked:
        print("\nЗаблокированные символы (снимаются через unblock):")
        for symbol, reason in blocked.items():
            print(f"  {symbol}: {reason}")

    pending = risk.pending_entry_symbols()
    if pending:
        print(f"\nНеподтверждённые ордера: {', '.join(pending)}")

    if orphaned:
        print(f"\nOrphaned-сделки ({len(orphaned)}) — результат неизвестен:")
        for trade in orphaned:
            print(
                f"  {trade['order_link_id']}  {trade['symbol']}  {trade['action']}  "
                f"entry={trade['entry_price']}  opened={trade['opened_at']}"
            )
        print("\nПопробуйте `python risk_admin.py reconcile` — возможно, биржа уже отдала закрытие.")
    return 0


def cmd_reconcile(cfg, args) -> int:
    """
    Ручной повторный поиск результата orphaned-сделок.

    Использует ровно тот же путь, что и автоматическая сверка в торговом цикле,
    поэтому идемпотентен так же: уже закрытая сделка повторно PnL не прибавит.
    """
    from strategy.engine import StrategyEngine

    db, journal, _ = _build(cfg)
    orphaned = journal.get_orphaned_trades(args.symbol)
    if not orphaned:
        print("Orphaned-сделок нет — сверять нечего.")
        return 0

    print(f"Orphaned-сделок к сверке: {len(orphaned)}")
    engine = StrategyEngine(cfg, db)
    symbols = sorted({t["symbol"] for t in orphaned})
    before = {t["order_link_id"] for t in orphaned}

    original_symbols = engine.cfg.symbols
    engine.cfg.symbols = symbols
    try:
        # Живые позиции читаем с биржи (только чтение): если по символу успели
        # открыть новую позицию, сверка не должна принять её за нашу старую.
        engine._sync_closed_trades(engine.execution.get_open_positions())
    finally:
        engine.cfg.symbols = original_symbols

    after = {t["order_link_id"] for t in journal.get_orphaned_trades(args.symbol)}
    recovered = before - after
    if recovered:
        print(f"Восстановлено сделок: {len(recovered)}")
        for oid in sorted(recovered):
            print(f"  {oid}")
        print("Дневной PnL и circuit breaker обновлены автоматически.")
    else:
        print("Восстановить не удалось: биржа не отдала закрытие по этим сделкам.")
        print("Если сделка старше 7 суток, автоматическая сверка невозможна — разбирайте вручную.")
    return 0


def closed_record_identity(record: dict) -> tuple:
    """
    Устойчивый идентификатор записи closed PnL.

    Позиция в списке идентификатором быть НЕ может: улики собираются на каждую
    сделку отдельным запросом со своим startTime, поэтому индекс i в списке
    одной сделки и индекс i в списке другой — разные записи. Сравнение по
    индексу приписало бы одно закрытие двум сделкам (или наоборот — развело бы
    конкурентов), что ломает всё правило единственности.

    orderId закрывающего ордера уникален; составной ключ — запасной вариант
    для ответов, где его нет.
    """
    order_id = record.get("orderId")
    if order_id:
        return ("orderId", str(order_id))
    return (
        "composite",
        str(record.get("symbol") or ""),
        str(record.get("createdTime") or ""),
        str(record.get("avgEntryPrice") or ""),
        str(record.get("avgExitPrice") or ""),
        str(record.get("closedPnl") or ""),
    )


def entry_fill_from_evidence(evidence: dict) -> Optional[dict]:
    """
    Фактическое исполнение входа: средневзвешенная цена и суммарный объём.

    Источник 1 — executions (каждый fill отдельной строкой), самый точный.
    Источник 2 — cumExecQty/avgPrice самого ордера.
    Возвращает None, если исполнения не было вовсе.
    """
    fills = evidence.get("executions") or []
    total_qty = 0.0
    total_notional = 0.0
    for f in fills:
        try:
            qty = float(f.get("execQty") or 0)
            price = float(f.get("execPrice") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and price > 0:
            total_qty += qty
            total_notional += qty * price
    if total_qty > 0:
        return {"price": total_notional / total_qty, "qty": total_qty, "source": "executions"}

    order = evidence.get("order")
    if not order:
        return None
    try:
        qty = float(order.get("cumExecQty") or 0)
        price = float(order.get("avgPrice") or 0)
    except (TypeError, ValueError):
        return None
    if qty > 0 and price > 0:
        return {"price": price, "qty": qty, "source": "order.cumExecQty"}
    return None


def _live_position_for(trade: dict, live_positions: List[dict]) -> Optional[dict]:
    """Живая позиция того же символа И того же направления."""
    from strategy.engine import StrategyEngine

    expected_close = StrategyEngine._expected_close_side(trade.get("action"))
    # Сторона позиции противоположна стороне её закрытия
    expected_side = {"Sell": "Buy", "Buy": "Sell"}.get(expected_close)
    for p in live_positions or []:
        if p.get("symbol") != trade.get("symbol"):
            continue
        try:
            if float(p.get("size", 0) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        if expected_side and p.get("side") and p["side"] != expected_side:
            continue
        return p
    return None


def plan_orphan_investigation(
    trades: List[dict],
    evidence_by_oid: dict,
    live_positions: List[dict],
) -> List[dict]:
    """
    Классифицирует orphaned-сделки по собранным уликам. ЧИСТАЯ функция:
    ничего не читает и не пишет, только рассуждает — поэтому проверяема без сети.

    Порядок проверок задан «убывающей достоверностью» улики:
      API_ERROR        — данных нет, судить не о чем;
      STILL_LIVE       — позиция ЕСТЬ прямо сейчас, это сильнее любой истории;
      NEVER_FILLED     — ордер найден и достоверно не исполнился: сделки не было;
      CONFIRMED_CLOSED — вход исполнен и найдено РОВНО одно закрытие;
      AMBIGUOUS        — кандидатов несколько или закрытие оспаривают две сделки;
      NOT_FOUND        — улик не хватает для вывода.

    Правило единственности: одна запись closed PnL не может обслужить две
    сделки. Оспариваемая запись делает AMBIGUOUS обе — приписать PnL наугад
    хуже, чем не приписать вовсе.
    """
    # Кандидаты closed PnL по каждой сделке, с учётом реальной цены исполнения
    candidates: dict = {}
    owners: dict = {}
    for trade in trades:
        oid = trade["order_link_id"]
        ev = evidence_by_oid.get(oid) or {}
        if ev.get("error"):
            continue
        fill = entry_fill_from_evidence(ev)
        if fill is None:
            continue
        # Матчим по ФАКТИЧЕСКОЙ цене входа с биржи, а не по оценке из журнала
        effective = dict(trade)
        effective["entry_price"] = fill["price"]
        effective["size_usdt"] = fill["qty"] * fill["price"]
        records = ev.get("closed_records") or []
        matches = [r for r in records if _record_matches_trade(effective, r)]
        candidates[oid] = matches
        # Владение считаем по устойчивой идентичности записи, а НЕ по её позиции
        # в списке: у каждой сделки свой список улик (см. closed_record_identity).
        for r in matches:
            owners.setdefault(closed_record_identity(r), []).append(oid)

    plan: List[dict] = []
    for trade in sorted(trades, key=lambda t: t.get("opened_at_ms") or 0):
        oid = trade["order_link_id"]
        ev = evidence_by_oid.get(oid) or {}
        base = {"trade": trade, "evidence": ev, "record": None, "fill": None}

        if ev.get("error"):
            plan.append({**base, "status": API_ERROR, "note": ev["error"]})
            continue

        live = _live_position_for(trade, live_positions)
        if live is not None:
            plan.append({
                **base, "status": STILL_LIVE,
                "note": f"живая позиция size={live.get('size')} side={live.get('side')}",
            })
            continue

        order = ev.get("order")
        fill = entry_fill_from_evidence(ev)
        base["fill"] = fill

        if order is None and fill is None:
            plan.append({
                **base, "status": NOT_FOUND,
                "note": "ордер не найден в истории и исполнений нет — судить не о чем",
            })
            continue

        if fill is None:
            status = str((order or {}).get("orderStatus") or "")
            if status in _DEAD_ORDER_STATUSES:
                plan.append({
                    **base, "status": NEVER_FILLED,
                    "note": f"orderStatus={status}, cumExecQty=0 — позиции никогда не было",
                })
            else:
                plan.append({
                    **base, "status": NOT_FOUND,
                    "note": f"orderStatus={status or 'неизвестен'}, исполнений нет, но статус не терминальный",
                })
            continue

        matches = candidates.get(oid) or []
        records = ev.get("closed_records") or []
        if not matches:
            plan.append({
                **base, "status": NOT_FOUND,
                "note": f"вход исполнен (qty={fill['qty']:g} @ {fill['price']:.6g}), "
                        f"но закрытия среди {len(records)} записей нет",
            })
            continue
        if len(matches) > 1:
            plan.append({
                **base, "status": AMBIGUOUS,
                "note": f"{len(matches)} подходящих закрытий — выбор был бы гаданием",
            })
            continue
        contenders = owners.get(closed_record_identity(matches[0]), [])
        if len(contenders) > 1:
            others = [o for o in contenders if o != oid]
            plan.append({
                **base, "status": AMBIGUOUS,
                "note": "единственное закрытие оспаривает также: " + ", ".join(others),
            })
            continue
        plan.append({**base, "status": CONFIRMED_CLOSED, "record": matches[0], "note": ""})
    return plan


def apply_orphan_findings(plan: List[dict], journal: TradeJournal, risk: RiskManager) -> List[dict]:
    """
    Применяет только три достоверных вердикта. AMBIGUOUS, NOT_FOUND и
    API_ERROR не трогаются никогда.

    Идемпотентность — на уровне журнала: все три перехода применимы только из
    исходного статуса, повторный вызов возвращает False и ничего не меняет.
    Причина circuit breaker снимается только при фактическом изменении.
    """
    from strategy.engine import StrategyEngine

    applied: List[dict] = []
    for item in plan:
        status = item["status"]
        trade = item["trade"]
        oid = trade["order_link_id"]

        if status == CONFIRMED_CLOSED:
            record = item["record"]
            pnl_usdt = float(record.get("closedPnl") or 0)
            closed_at = StrategyEngine._closed_at_from_match(record)
            result = journal.log_exit(
                oid,
                float(record.get("avgExitPrice") or 0),
                pnl_usdt,
                exit_reason=StrategyEngine._infer_exit_reason(record),
                closed_at=closed_at,
            )
            if not result.recorded:
                applied.append({"order_link_id": oid, "status": status, "changed": False,
                                "note": "уже закрыта — пропущено (идемпотентность)"})
                continue
            effective = result.closed_at or utcnow()
            counted = effective.date() == utc_today()
            if counted:
                risk.record_closed_pnl(pnl_usdt)
            risk.resolve_breaker_cause(orphan_cause(oid))
            applied.append({"order_link_id": oid, "status": status, "changed": True,
                            "pnl_usdt": pnl_usdt, "counted_in_daily": counted,
                            "note": f"закрыта, pnl={pnl_usdt:.4f} USDT"})

        elif status == NEVER_FILLED:
            changed = journal.mark_not_filled(oid, item["note"][:100])
            if changed:
                risk.resolve_breaker_cause(orphan_cause(oid))
            applied.append({"order_link_id": oid, "status": status, "changed": changed,
                            "note": "помечена not_filled без PnL" if changed
                                    else "уже не в orphaned — пропущено (идемпотентность)"})

        elif status == STILL_LIVE:
            changed = journal.reopen_orphaned(oid, item["note"][:100])
            if changed:
                risk.resolve_breaker_cause(orphan_cause(oid))
            applied.append({"order_link_id": oid, "status": status, "changed": changed,
                            "note": "возвращена в open" if changed
                                    else "уже не в orphaned — пропущено (идемпотентность)"})
    return applied


def gather_orphan_evidence(execution, trade: dict) -> dict:
    """
    Собирает улики по одной orphaned-сделке. Только READ-запросы.

    Запрос истории ордера идёт по нашему orderLinkId — Bybit обслуживает его
    без ограничения окна в 7 суток, поэтому судьба входа выясняется даже для
    старых сделок, чей closed PnL уже недоступен.
    """
    symbol = trade["symbol"]
    oid = trade["order_link_id"]
    evidence: dict = {
        "symbol_key": symbol, "order": None, "executions": [],
        "closed_records": [], "error": None,
    }
    try:
        orders = execution.get_order_history(symbol, order_link_id=oid)
        evidence["order"] = next(
            (o for o in orders if o.get("orderLinkId") == oid), orders[0] if orders else None
        )
    except Exception as exc:
        evidence["error"] = f"order history недоступна: {type(exc).__name__}"
        return evidence

    try:
        evidence["executions"] = [
            e for e in execution.get_executions(
                symbol, order_link_id=oid, start_time_ms=trade.get("opened_at_ms"),
            )
            if e.get("orderLinkId") == oid
        ]
    except Exception as exc:
        # Не фатально: судьбу входа можно вывести и из самого ордера
        logger.warning("%s: executions недоступны (%s)", oid, type(exc).__name__)

    try:
        evidence["closed_records"] = execution.get_closed_pnl_since(
            symbol, start_time_ms=trade.get("opened_at_ms"),
        )
    except Exception as exc:
        evidence["error"] = f"closed PnL недоступен: {type(exc).__name__}"
    return evidence


def cmd_investigate_orphans(cfg, args) -> int:
    """
    Глубокий разбор orphaned-сделок по всем доступным источникам биржи.
    По умолчанию — dry-run: ничего в БД не меняется.
    """
    from execution.execution_engine import ExecutionEngine

    _, journal, risk = _build(cfg)
    trades = journal.get_orphaned_trades(getattr(args, "symbol", None))
    if not trades:
        print("Orphaned-сделок нет — разбирать нечего.")
        return 0

    execution = ExecutionEngine(cfg)  # используются только read-методы
    try:
        live_positions = execution.get_open_positions()
    except Exception as exc:
        print(f"ВНИМАНИЕ: живые позиции недоступны ({type(exc).__name__}).")
        if args.apply:
            print("--apply отклонён: без списка живых позиций нельзя отличить STILL_LIVE.")
            return 1
        live_positions = []
        print("Продолжаю dry-run; STILL_LIVE определить нельзя.\n")

    evidence_by_oid = {t["order_link_id"]: gather_orphan_evidence(execution, t) for t in trades}
    plan = plan_orphan_investigation(trades, evidence_by_oid, live_positions)

    counts: dict = {}
    for item in plan:
        t, ev, fill = item["trade"], item["evidence"], item.get("fill")
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        order = ev.get("order") or {}
        print(f"\n=== {item['status']}  {t['symbol']}  {t['action']}  {t['order_link_id']}")
        print(f"    журнал:      entry={t['entry_price']:.6g} size={t['size_usdt']:.2f} USDT "
              f"opened={t['opened_at']:%Y-%m-%d %H:%M} UTC")
        print(f"    ордер:       status={order.get('orderStatus') or 'не найден'} "
              f"cumExecQty={order.get('cumExecQty') or '-'} avgPrice={order.get('avgPrice') or '-'}")
        print(f"    исполнения:  {len(ev.get('executions') or [])} fill(ов)"
              + (f", факт: qty={fill['qty']:g} @ {fill['price']:.6g} (из {fill['source']})"
                 if fill else ", фактического исполнения нет"))
        print(f"    closed PnL:  {len(ev.get('closed_records') or [])} записей в окне")
        if item.get("record"):
            r = item["record"]
            print(f"    -> закрытие: exit={r.get('avgExitPrice')} closedPnl={r.get('closedPnl')} "
                  f"side={r.get('side')}")
        if item["note"]:
            print(f"    вывод:       {item['note']}")

    print("\n" + "=" * 60)
    print("Итог:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    actionable = [i for i in plan if i["status"] in (CONFIRMED_CLOSED, NEVER_FILLED, STILL_LIVE)]
    if not actionable:
        print("Достоверных вердиктов нет. Записи НЕ изменены.")
        return 0

    if not args.apply:
        print(f"\nDRY-RUN: {len(actionable)} сделок были бы изменены:")
        for i in actionable:
            action = {
                CONFIRMED_CLOSED: "записать реальный PnL и закрыть",
                NEVER_FILLED: "пометить not_filled (без PnL)",
                STILL_LIVE: "вернуть статус open",
            }[i["status"]]
            print(f"  {i['trade']['order_link_id']}: {action}")
        print("Записи НЕ изменены. Для применения повторите с --apply.")
        return 0

    print(f"\nПрименяю {len(actionable)} вердиктов...")
    for a in apply_orphan_findings(plan, journal, risk):
        print(f"  [{a['status']}] {a['order_link_id']}: {a['note']}")
    remaining = risk.breaker_causes()
    print("\nПричины circuit breaker после применения:", ", ".join(remaining) or "нет — breaker снят")
    return 0


def cmd_list_open(cfg, args) -> int:
    """Зависшие open-сделки: возраст, символ, направление, цена входа, id."""
    _, journal, _ = _build(cfg)
    trades = sorted(
        journal.get_open_trades(getattr(args, "symbol", None)),
        key=lambda t: t.get("opened_at_ms") or 0,
    )
    if not trades:
        print("Открытых сделок в журнале нет.")
        return 0

    now = utcnow()
    print(f"Открытых сделок в журнале: {len(trades)}\n")
    print(f"{'age_days':>8}  {'symbol':<13} {'action':<11} {'entry_price':>12} {'size_usdt':>10}  order_link_id")
    for t in trades:
        age = (now - t["opened_at"]).total_seconds() / 86400 if t.get("opened_at") else float("nan")
        print(
            f"{age:>8.1f}  {t['symbol']:<13} {t['action']:<11} "
            f"{t['entry_price']:>12.6g} {t['size_usdt']:>10.2f}  {t['order_link_id']}"
        )
    print(
        "\nСледующий шаг: python risk_admin.py reconcile-open  (dry-run, ничего не меняет)\n"
        "Применить найденные однозначные совпадения: reconcile-open --apply"
    )
    return 0


def cmd_reconcile_open(cfg, args) -> int:
    """
    Сопоставляет зависшие open-сделки с closed PnL Bybit.

    По умолчанию — DRY-RUN: только показывает, что было бы изменено.
    Запись происходит исключительно с явным --apply, и только для MATCHED.
    AMBIGUOUS и NOT_FOUND не трогаются никогда — и в orphaned отсюда ничего
    не переводится.
    """
    from execution.execution_engine import ExecutionEngine

    _, journal, risk = _build(cfg)
    trades = journal.get_open_trades(getattr(args, "symbol", None))
    if not trades:
        print("Открытых сделок в журнале нет — сверять нечего.")
        return 0

    execution = ExecutionEngine(cfg)  # используются только read-методы

    # Символы с живой позицией пропускаем: их open-сделка, вероятно, настоящая.
    live_symbols = None
    try:
        live_symbols = {
            p.get("symbol") for p in execution.get_open_positions()
            if float(p.get("size", 0) or 0) > 0
        }
    except Exception as exc:
        print(f"ВНИМАНИЕ: не удалось прочитать живые позиции ({type(exc).__name__}).")
        if args.apply:
            print("--apply отклонён: без списка живых позиций закрывать записи небезопасно.")
            return 1
        print("Продолжаю dry-run без фильтра живых позиций.\n")

    plan: List[dict] = []
    by_symbol: dict = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(t)

    for symbol in sorted(by_symbol):
        sym_trades = by_symbol[symbol]
        if live_symbols is not None and symbol in live_symbols:
            for t in sym_trades:
                plan.append({"trade": t, "status": LIVE, "record": None,
                             "note": "на бирже есть живая позиция — сделка, вероятно, настоящая"})
            continue
        oldest_ms = min(
            (t["opened_at_ms"] for t in sym_trades if t.get("opened_at_ms") is not None),
            default=None,
        )
        try:
            records = execution.get_closed_pnl_since(symbol, start_time_ms=oldest_ms)
        except Exception as exc:
            for t in sym_trades:
                plan.append({"trade": t, "status": API_ERROR, "record": None,
                             "note": f"closed PnL недоступен: {type(exc).__name__}"})
            continue
        plan.extend(plan_open_reconciliation(sym_trades, records))

    counts: dict = {}
    print(f"{'status':<10} {'symbol':<13} {'entry':>12}  order_link_id / примечание")
    for item in plan:
        t = item["trade"]
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        line = f"{item['status']:<10} {t['symbol']:<13} {t['entry_price']:>12.6g}  {t['order_link_id']}"
        if item["status"] == MATCHED:
            r = item["record"]
            line += f"  -> closedPnl={r.get('closedPnl')} exit={r.get('avgExitPrice')}"
        elif item["note"]:
            line += f"  ({item['note']})"
        print(line)
    print("\nИтог:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    matched = [i for i in plan if i["status"] == MATCHED]
    if not matched:
        print("Применять нечего: однозначных совпадений нет. Записи НЕ изменены.")
        return 0

    if not args.apply:
        print(f"\nDRY-RUN: {len(matched)} сделок были бы закрыты. Записи НЕ изменены.")
        print("Для применения повторите с --apply.")
        return 0

    print(f"\nПрименяю {len(matched)} однозначных совпадений...")
    applied = apply_reconciliation_plan(plan, journal, risk)
    for a in applied:
        if a["changed"]:
            print(
                f"  {a['order_link_id']}: закрыта, pnl={a['pnl_usdt']:.4f} USDT, "
                f"closed_at={a['closed_at']:%Y-%m-%d %H:%M} UTC, "
                f"в дневном лимите: {'да' if a['counted_in_daily'] else 'нет (закрыта не сегодня)'}"
            )
        else:
            print(f"  {a['order_link_id']}: {a['note']}")
    return 0


def cmd_resolve_cause(cfg, args) -> int:
    _, _, risk = _build(cfg)
    if risk.resolve_breaker_cause(args.cause):
        remaining = risk.breaker_causes()
        print(f"Причина [{args.cause}] снята.")
        print(
            f"Circuit breaker: {'всё ещё взведён, осталось причин: ' + str(len(remaining)) if remaining else 'СНЯТ'}"
        )
    else:
        print(f"Причина [{args.cause}] не найдена — возможно, уже снята.")
    return 0


def cmd_unblock(cfg, args) -> int:
    _, _, risk = _build(cfg)
    symbol = args.symbol.upper()
    if symbol not in risk.blocked_symbols():
        print(f"{symbol} не заблокирован.")
        return 0
    risk.unblock_symbol(symbol)
    risk.clear_entry_pending(symbol)
    print(f"{symbol} разблокирован. Убедитесь, что позиция по нему сверена с биржей вручную.")
    return 0


def cmd_reset_breaker(cfg, args) -> int:
    _, _, risk = _build(cfg)
    causes = risk.breaker_causes()
    if not causes:
        print("Circuit breaker не взведён.")
        return 0
    print("Будут сняты ВСЕ причины:")
    for key, value in causes.items():
        print(f"  [{key}] {value['reason']}")
    if not args.yes:
        print("\nЭто отключает защиту. Повторите с --yes, если действительно разобрались.")
        return 1
    risk.manual_reset_circuit_breaker()
    print("Circuit breaker снят. Торговля возобновится со следующего цикла.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Разбор состояния Risk Manager")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Показать состояние, причины breaker и orphaned-сделки")

    reconcile = sub.add_parser("reconcile", help="Повторно искать результат orphaned-сделок")
    reconcile.add_argument("--symbol", help="Только по одному символу")

    investigate = sub.add_parser(
        "investigate-orphans",
        help="Глубокий разбор orphaned-сделок по истории биржи (по умолчанию dry-run)",
    )
    investigate.add_argument("--symbol", help="Только по одному символу")
    investigate.add_argument(
        "--apply", action="store_true",
        help="Применить только достоверные вердикты: CONFIRMED_CLOSED, NEVER_FILLED, STILL_LIVE",
    )

    list_open = sub.add_parser("list-open", help="Показать зависшие open-сделки журнала")
    list_open.add_argument("--symbol", help="Только по одному символу")

    reconcile_open = sub.add_parser(
        "reconcile-open",
        help="Сопоставить open-сделки с closed PnL Bybit (по умолчанию dry-run)",
    )
    reconcile_open.add_argument("--symbol", help="Только по одному символу")
    reconcile_open.add_argument(
        "--apply", action="store_true",
        help="Записать однозначные (MATCHED) совпадения. Без флага — только показ.",
    )

    resolve = sub.add_parser("resolve-cause", help="Снять одну причину circuit breaker")
    resolve.add_argument("cause", help='Ключ причины, например "orphan:decision_e-ab12"')

    unblock = sub.add_parser("unblock", help="Снять блокировку символа")
    unblock.add_argument("symbol")

    reset = sub.add_parser("reset-breaker", help="Снять circuit breaker целиком")
    reset.add_argument("--yes", action="store_true", help="Подтверждение")

    args = parser.parse_args()
    configure_app_logging("risk_admin", "risk_admin.log")
    cfg = BybitConfig()

    handlers = {
        "status": cmd_status,
        "reconcile": cmd_reconcile,
        "investigate-orphans": cmd_investigate_orphans,
        "list-open": cmd_list_open,
        "reconcile-open": cmd_reconcile_open,
        "resolve-cause": cmd_resolve_cause,
        "unblock": cmd_unblock,
        "reset-breaker": cmd_reset_breaker,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
