"""
Strategy Engine: главный оркестратор торгового цикла.

Каждые decision_interval_sec секунд, для каждого символа:
1. Достаёт свежие данные из БД (свечи, funding rate).
2. Спрашивает rule-based стратегию (жёсткая схема).
3. Спрашивает AI-стратегию ("мозг", ищет неочевидное).
4. Если оба источника согласны (или хотя бы один даёт уверенный сигнал
   при отсутствии противоречия) — сигнал уходит в Risk Manager.
5. Risk Manager одобряет/режет параметры.
6. Одобренное уходит в Execution Engine.

Логика примирения сигналов (rule vs AI) — намеренно простая и explicit,
чтобы всегда можно было объяснить, ПОЧЕМУ система открыла сделку.
"""

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.settings import BybitConfig
from storage.db import Database
from storage.models import Candle, FundingRate, OpenInterest, Liquidation, Trade, OrderbookSnapshot
from storage.journal import TradeJournal
from strategy.signal import Signal, Action
from strategy.rule_based import TechnicalRuleCommittee
from strategy.experts import ExpertSignalCollector
from strategy.indicators import compute_all_indicators, trend_direction
from market_context import MarketContextEngine
from meta_strategy import MetaStrategyManager
from decision_engine import DecisionEngine
from portfolio_risk import PortfolioRiskEngine
from ai_market_analyst import AIMarketAnalyst
from risk.risk_manager import RiskManager
from execution.execution_engine import ExecutionEngine

logger = logging.getLogger(__name__)


@dataclass
class EntryCandidate:
    symbol: str
    final_signal: Signal
    decision_report: object
    last_price: float
    risk_check: object
    atr_pct_of_price: Optional[float]
    spread_pct: Optional[float]
    funding_rate: Optional[float]
    position_size_multiplier: float
    rank_score: float
    entry_snapshot: Optional[dict] = None
    expert_vote_rows: Optional[list] = None


class StrategyEngine:
    def __init__(self, cfg: BybitConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.rule_strategy = TechnicalRuleCommittee()
        self.experts = ExpertSignalCollector()
        self.market_context_engine = MarketContextEngine()
        self.meta_strategy = MetaStrategyManager()
        self.decision_engine = DecisionEngine(
            min_open_confidence=cfg.min_open_confidence,
            min_margin=cfg.min_decision_margin,
            min_rr=cfg.min_rr,
            default_stop_loss_pct=cfg.default_stop_loss_pct,
            default_take_profit_rr=cfg.default_take_profit_rr,
            min_confirming_families=cfg.min_confirming_families,
        )
        self.portfolio_risk = PortfolioRiskEngine(cfg)
        self.ai_market_analyst = AIMarketAnalyst()
        self.risk_manager = RiskManager(cfg)
        self.execution = ExecutionEngine(cfg)
        self.journal = TradeJournal(db)
        self._trailing_activated: set = set()  # order_link_id, для которых уже включили trailing
        self._last_entry_ts: Optional[float] = None
        self._pending_exit_reasons: dict[str, str] = {}

    def run_forever(self):
        logger.info("Strategy Engine запущен, интервал решений: %d сек", self.cfg.decision_interval_sec)
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("Ошибка в торговом цикле, продолжаю после паузы")
            time.sleep(self.cfg.decision_interval_sec)

    def run_once(self):
        balance = self.execution.get_account_balance_usdt()
        positions = self.execution.get_open_positions()
        logger.info("Баланс: %.2f USDT, открытых позиций: %d", balance, len(positions))

        self.risk_manager.ensure_daily_reset(balance)
        self._manage_trailing_stops(positions)
        self._sync_closed_trades(positions)

        candidates = []
        for symbol in self.cfg.symbols:
            try:
                result = self._process_symbol(symbol, balance, positions, execute=False)
                if isinstance(result, EntryCandidate):
                    candidates.append(result)
                elif result:
                    positions = self.execution.get_open_positions()
                    logger.info("Позиции обновлены после изменения по %s: %d", symbol, len(positions))
            except Exception:
                logger.exception("Ошибка обработки символа %s", symbol)

        self._execute_ranked_candidates(candidates, balance, positions)

    def _manage_trailing_stops(self, positions: list):
        """
        Проверяет открытые позиции: если нереализованная прибыль достигла
        порога активации — включает trailing stop (один раз на позицию).
        """
        if not self.cfg.trailing_stop_enabled:
            return
        for p in positions:
            try:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue
                symbol = p["symbol"]
                entry_price = float(p["avgPrice"])
                mark_price = float(p["markPrice"])
                side = p["side"]  # "Buy" (long) | "Sell" (short)

                pnl_pct = ((mark_price - entry_price) / entry_price * 100
                           if side == "Buy" else (entry_price - mark_price) / entry_price * 100)

                already_trailing = float(p.get("trailingStop", 0) or 0) > 0
                if pnl_pct >= self.cfg.trailing_activation_pct and not already_trailing:
                    self.execution.set_trailing_stop(symbol, mark_price, self.cfg.trailing_distance_pct)
                    logger.info(
                        "%s: активирован trailing stop, прибыль %.2f%% >= порога %.2f%%",
                        symbol, pnl_pct, self.cfg.trailing_activation_pct,
                    )
            except Exception:
                logger.exception("Ошибка управления trailing stop для позиции %s", p.get("symbol"))

    def _sync_closed_trades(self, positions: Optional[list] = None):
        """
        Сверяет журнал с биржей: если сделка помечена у нас как "open", а на бирже
        уже закрыта (по SL, TP, trailing stop или вручную) — подтягивает реальный
        PnL и обновляет запись + Risk Manager.

        Матчинг НЕ по orderLinkId: в ответе get_closed_pnl этого поля нет вообще
        (закрывающий ордер при срабатывании SL/TP создаётся биржей автоматически).
        Вместо этого матчим по символу + цене входа (с допуском на проскальзывание)
        + времени (закрытие должно быть позже открытия). Это надёжно, так как
        Risk Manager не даёт открыть вторую позицию по тому же символу, пока
        текущая не закрыта — на символ в любой момент максимум одна открытая сделка.
        """
        PRICE_TOLERANCE_PCT = 0.5  # допуск на проскальзывание при сверке цены входа
        live_symbols = {
            p.get("symbol")
            for p in (positions or [])
            if float(p.get("size", 0) or 0) > 0
        }

        for symbol in self.cfg.symbols:
            open_trades = self.journal.get_open_trades(symbol)
            if not open_trades:
                continue
            try:
                closed = self.execution.get_closed_pnl(symbol, limit=50)
            except Exception:
                logger.exception("Не удалось получить closed PnL для %s", symbol)
                continue

            for trade in open_trades:
                match = self._find_matching_closed_pnl(trade, closed, PRICE_TOLERANCE_PCT)
                if match is None:
                    if positions is not None and symbol not in live_symbols:
                        logger.warning(
                            "%s: журнал считает сделку %s открытой, но live-позиции нет; "
                            "closed_pnl не найден по цене %.4f. Нужна ручная сверка или расширение окна closed_pnl.",
                            symbol, trade["order_link_id"], trade["entry_price"],
                        )
                    continue
                exit_price = float(match.get("avgExitPrice", 0) or 0)
                pnl_usdt = float(match.get("closedPnl", 0) or 0)
                pending_exit_reasons = getattr(self, "_pending_exit_reasons", {})
                exit_reason = pending_exit_reasons.pop(symbol, None) or self._infer_exit_reason(match)
                exit_snapshot = self._build_exit_snapshot(symbol, match)
                if self.journal.log_exit(
                    trade["order_link_id"],
                    exit_price,
                    pnl_usdt,
                    exit_reason=exit_reason,
                    exit_snapshot=exit_snapshot,
                ):
                    self.risk_manager.record_closed_pnl(pnl_usdt)
                    holding_seconds = self._holding_seconds(trade.get("opened_at_ms"))
                    pnl_pct = self._position_pnl_pct(trade, exit_price)
                    logger.info(
                        "TRADE_CLOSE symbol=%s direction=%s entry=%.6f exit=%.6f pnl_usdt=%.4f "
                        "pnl_pct=%.3f holding_seconds=%s exit_reason=%s orderLinkId=%s",
                        symbol,
                        trade.get("action"),
                        trade["entry_price"],
                        exit_price,
                        pnl_usdt,
                        pnl_pct,
                        holding_seconds,
                        exit_reason,
                        trade["order_link_id"],
                    )

    @staticmethod
    def _find_matching_closed_pnl(trade: dict, closed_pnl_list: list, tolerance_pct: float) -> Optional[dict]:
        """
        Ищет запись closed_pnl, которая соответствует нашей открытой сделке:
        - та же цена входа (avgEntryPrice) с допуском на проскальзывание
        - createdTime записи ПОЗЖЕ, чем наш opened_at (закрытие не может быть раньше открытия)
        Если совпадений несколько — берём ближайшую по времени к нашему opened_at.
        """
        candidates = []
        for c in closed_pnl_list:
            avg_entry = c.get("avgEntryPrice")
            created_time = c.get("createdTime")
            if avg_entry is None or created_time is None:
                continue
            try:
                avg_entry = float(avg_entry)
                created_time_ms = int(created_time)
            except (TypeError, ValueError):
                continue

            if trade["opened_at_ms"] is not None and created_time_ms < trade["opened_at_ms"]:
                continue  # закрытие раньше открытия -- не может относиться к этой сделке

            price_diff_pct = abs(avg_entry - trade["entry_price"]) / trade["entry_price"] * 100
            if price_diff_pct <= tolerance_pct:
                candidates.append((created_time_ms, c))

        if not candidates:
            return None
        # Берём самое РАННЕЕ закрытие после открытия -- это и есть закрытие именно этой сделки
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    # ------------------------------------------------------------------

    def _process_symbol(self, symbol: str, balance: float, positions: list, execute: bool = True):
        candles_df = self._load_recent_candles(symbol, limit=210)
        if candles_df is None or len(candles_df) < 30:
            logger.debug("%s: недостаточно свечей в БД для анализа", symbol)
            return False

        funding_info = self._load_latest_funding(symbol)
        funding_rate = funding_info["rate"] if funding_info else None
        funding_trend = self._load_funding_trend(symbol, limit=8)
        oi_trend = self._load_oi_trend(symbol, limit=20)
        orderbook = self._load_latest_orderbook(symbol)
        trade_flow = self._load_trade_flow(symbol, minutes=15)
        liquidations = self._load_recent_liquidations(symbol, minutes=60)
        freshness = self._check_data_freshness(symbol, candles_df, funding_info, oi_trend, orderbook, trade_flow)
        if freshness["critical"]:
            logger.warning("%s: пропускаю символ из-за устаревших критичных данных: %s", symbol, "; ".join(freshness["warnings"]))
            return False

        indicators = compute_all_indicators(candles_df)
        trend = trend_direction(candles_df)  # "long" | "short" | "neutral" | None (мало данных)

        market_snapshot = self._build_market_snapshot(
            symbol, candles_df, funding_rate, funding_trend, oi_trend,
            orderbook, trade_flow, liquidations, indicators,
        )
        market_snapshot["trend_filter"] = trend
        market_snapshot["data_warnings"] = freshness["warnings"]

        market_context = self.market_context_engine.analyze(symbol, candles_df, market_snapshot)
        meta_decision = self.meta_strategy.evaluate(market_context)
        expert_signals = self.experts.collect(symbol, candles_df, funding_rate or 0.0, market_snapshot)
        ai_analysis = self.ai_market_analyst.analyze(symbol, market_snapshot, market_context)
        decision_report = self.decision_engine.decide(
            symbol=symbol,
            context=market_context,
            meta=meta_decision,
            expert_signals=expert_signals,
            ai_analysis=ai_analysis.conclusion,
        )
        final_signal = self._apply_trend_filter(decision_report.final_signal, trend, market_context, symbol)
        self._log_decision_summary(symbol, decision_report, final_signal, market_context, trend)

        logger.info(
            "%s: context=[%s] experts=%s trend=%s data=%s -> итог=%s (%s)",
            symbol,
            market_context.summary(),
            ", ".join(f"{s.source}:{s.action.value}:{s.confidence:.2f}" for s in expert_signals),
            trend or "недостаточно данных",
            "OK" if not freshness["warnings"] else "; ".join(freshness["warnings"]),
            final_signal.action, final_signal.reason,
        )

        existing_position = self._find_open_position(symbol, positions)
        if existing_position is not None:
            # Позиция уже открыта -- проверяем, не пора ли её ЗАКРЫТЬ по новым
            # данным (разворотный сигнал или смена старшего тренда), вместо
            # того чтобы пассивно ждать, пока цена дойдёт до фиксированного
            # SL/TP. Пока позиция открыта, новую по этому же символу не открываем.
            return self._manage_exit(symbol, existing_position, final_signal, trend)

        if final_signal.action == Action.HOLD:
            return False

        portfolio_check = self.portfolio_risk.evaluate(final_signal, positions)
        if not portfolio_check.approved:
            logger.info("%s: сигнал отклонён Portfolio Risk Engine: %s", symbol, portfolio_check.reason)
            return False

        last_price = float(candles_df["close"].iloc[-1])
        check = self.risk_manager.evaluate(
            final_signal, positions, balance,
            atr_pct_of_price=indicators.get("atr_pct_of_price") if indicators else None,
            spread_pct=orderbook.get("spread_pct") if orderbook else None,
            funding_rate=funding_rate,
            position_size_multiplier=meta_decision.position_size_multiplier,
        )

        if not check.approved:
            logger.info("%s: сигнал отклонён Risk Manager: %s", symbol, check.reason)
            return False

        candidate = EntryCandidate(
            symbol=symbol,
            final_signal=final_signal,
            decision_report=decision_report,
            last_price=last_price,
            risk_check=check,
            atr_pct_of_price=indicators.get("atr_pct_of_price") if indicators else None,
            spread_pct=orderbook.get("spread_pct") if orderbook else None,
            funding_rate=funding_rate,
            position_size_multiplier=meta_decision.position_size_multiplier,
            rank_score=self._rank_entry_candidate(final_signal, decision_report),
            entry_snapshot=self._build_entry_snapshot(
                symbol=symbol,
                final_signal=final_signal,
                decision_report=decision_report,
                market_context=market_context,
                market_snapshot=market_snapshot,
                candles_df=candles_df,
                indicators=indicators,
                last_price=last_price,
                risk_check=check,
                trend_filter=trend,
                meta_decision=meta_decision,
            ),
            expert_vote_rows=self._expert_vote_rows(decision_report),
        )
        logger.info(
            "%s: кандидат на вход прошёл фильтры: action=%s confidence=%.3f expected_rr=%s "
            "rank=%.3f confirmations=%d families=%s regime=%s trend=%s rejected=%s",
            symbol,
            final_signal.action.value,
            final_signal.confidence,
            decision_report.expected_rr,
            candidate.rank_score,
            decision_report.confirmation_count,
            ", ".join(decision_report.confirmation_families) or "нет",
            market_context.regime,
            market_context.trend,
            decision_report.rejected_actions,
        )
        if not execute:
            return candidate
        return self._execute_candidate(candidate)

    def _execute_ranked_candidates(self, candidates: list, balance: float, positions: list):
        if not candidates:
            return

        ranked = sorted(candidates, key=lambda c: c.rank_score, reverse=True)
        logger.info(
            "Кандидаты цикла по качеству: %s",
            "; ".join(f"{c.symbol}:{c.final_signal.action.value}:rank={c.rank_score:.3f}" for c in ranked),
        )

        opened = 0
        max_new = max(0, self.cfg.max_new_positions_per_cycle)
        for candidate in ranked:
            if opened >= max_new:
                logger.info(
                    "%s: кандидат отложен anti-burst: достигнут лимит новых входов за цикл %d",
                    candidate.symbol, max_new,
                )
                continue

            if self._find_open_position(candidate.symbol, positions) is not None:
                logger.info("%s: кандидат пропущен: позиция уже открыта после обновления портфеля", candidate.symbol)
                continue

            portfolio_check = self.portfolio_risk.evaluate(candidate.final_signal, positions)
            if not portfolio_check.approved:
                logger.info(
                    "%s: кандидат отклонён при повторной проверке Portfolio Risk: %s",
                    candidate.symbol, portfolio_check.reason,
                )
                continue

            fresh_risk_check = self.risk_manager.evaluate(
                candidate.final_signal,
                positions,
                balance,
                atr_pct_of_price=candidate.atr_pct_of_price,
                spread_pct=candidate.spread_pct,
                funding_rate=candidate.funding_rate,
                position_size_multiplier=candidate.position_size_multiplier,
            )
            if not fresh_risk_check.approved:
                logger.info(
                    "%s: кандидат отклонён при повторной проверке Risk Manager: %s",
                    candidate.symbol, fresh_risk_check.reason,
                )
                continue
            candidate.risk_check = fresh_risk_check

            last_entry_ts = getattr(self, "_last_entry_ts", None)
            if last_entry_ts is not None:
                elapsed = time.time() - last_entry_ts
                if elapsed < self.cfg.min_seconds_between_entries:
                    logger.info(
                        "%s: кандидат отложен anti-burst: прошло %.1fs из %ds после последнего входа",
                        candidate.symbol, elapsed, self.cfg.min_seconds_between_entries,
                    )
                    continue

            if self._execute_candidate(candidate):
                opened += 1
                self._last_entry_ts = time.time()
                positions = self.execution.get_open_positions()

    @staticmethod
    def _rank_entry_candidate(signal: Signal, decision_report) -> float:
        rr = decision_report.expected_rr or 0.0
        rr_component = min(rr, 4.0) * 0.03
        confirmation_component = min(decision_report.confirmation_count, 4) * 0.08
        risk_penalty = decision_report.risk_score * 0.25
        return round(signal.confidence + confirmation_component + rr_component - risk_penalty, 4)

    def _execute_candidate(self, candidate: EntryCandidate) -> bool:
        symbol = candidate.symbol
        final_signal = candidate.final_signal
        check = candidate.risk_check
        decision_report = candidate.decision_report
        last_price = candidate.last_price

        if not self.cfg.trading_enabled:
            logger.info("%s: сигнал %s не исполнен: TRADING_ENABLED=false", symbol, final_signal.action.value)
            return False

        resp = self.execution.open_position(
            symbol=symbol,
            action=final_signal.action,
            size_usdt=check.approved_size_usdt,
            leverage=check.approved_leverage,
            last_price=last_price,
            stop_loss_pct=final_signal.stop_loss_pct or self.cfg.default_stop_loss_pct,
            take_profit_pct=final_signal.take_profit_pct,
            source=final_signal.source,
	)
        if resp.get("retCode") == 0:
            order_link_id = (
                resp.get("local_order_link_id")
                or resp.get("result", {}).get("orderLinkId")
                or resp.get("retExtInfo", {}).get("orderLinkId")
                or ""
            )
            if not order_link_id:
                logger.critical("%s: биржа приняла ордер, но order_link_id потерян: %s", symbol, resp)
                return True

            sl_pct = final_signal.stop_loss_pct or self.cfg.default_stop_loss_pct
            tp_pct = final_signal.take_profit_pct
            logger.info(
                "TRADE_OPEN symbol=%s direction=%s size_usdt=%.4f leverage=%sx entry=%.6f "
                "sl_pct=%s sl_price=%s tp_pct=%s tp_price=%s orderLinkId=%s supporters=%s reason=%s",
                symbol,
                final_signal.action.value,
                check.approved_size_usdt,
                check.approved_leverage,
                last_price,
                sl_pct,
                self._price_from_pct(last_price, final_signal.action, sl_pct, is_stop=True),
                tp_pct,
                self._price_from_pct(last_price, final_signal.action, tp_pct, is_stop=False),
                order_link_id,
                self._supporting_experts(decision_report),
                final_signal.reason,
            )

            journal_saved = self.journal.log_entry(
                symbol=symbol,
                action=final_signal.action,
                source=final_signal.source,
                reason=decision_report.journal_reason(),
                entry_price=last_price,
                size_usdt=check.approved_size_usdt,
                leverage=check.approved_leverage,
                stop_loss_pct=final_signal.stop_loss_pct or self.cfg.default_stop_loss_pct,
                take_profit_pct=final_signal.take_profit_pct,
                order_link_id=order_link_id,
                market_context=decision_report.market_context.summary(),
                regime=decision_report.market_context.regime,
                trend=decision_report.market_context.trend,
                decision_confidence=decision_report.confidence,
                expected_rr=decision_report.expected_rr,
                confirmation_count=decision_report.confirmation_count,
                confirmation_families=", ".join(decision_report.confirmation_families),
                entry_reason=decision_report.journal_reason(limit=2000),
                entry_snapshot=candidate.entry_snapshot,
                expert_votes=candidate.expert_vote_rows,
            )
            if not journal_saved:
                logger.critical(
                    "%s: ордер создан, но вход не записан в trade_log; позиция требует ручной сверки. order_link_id=%s",
                    symbol, order_link_id,
                )
            else:
                self.risk_manager.record_open_trade(symbol)
            return True

        logger.warning(
            "%s: ордер отклонён биржей retCode=%s retMsg=%s",
            symbol, resp.get("retCode"), resp.get("retMsg"),
        )
        return False

    @staticmethod
    def _find_open_position(symbol: str, positions: list) -> Optional[dict]:
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("size", 0)) > 0:
                return p
        return None
    
    def _manage_exit(self, symbol: str, position: dict, final_signal: Signal, trend: Optional[str]) -> bool:
        """
        Exit Manager: закрывает позицию только при явном разворотном сигнале.
        Выход по смене EMA trend отключён, чтобы сделка не закрывалась слишком рано.
        """
        side = position.get("side")
        size = float(position.get("size", 0))

        if size <= 0 or side not in ("Buy", "Sell"):
            return False

        position_direction = "long" if side == "Buy" else "short"

        close_reason = None

        if final_signal.action == Action.OPEN_LONG and position_direction == "short":
            close_reason = f"Явный разворотный сигнал против SHORT: {final_signal.reason}"
        elif final_signal.action == Action.OPEN_SHORT and position_direction == "long":
            close_reason = f"Явный разворотный сигнал против LONG: {final_signal.reason}"

        if close_reason is None:
            return False

        logger.info("%s: закрываю позицию (%s) -- %s", symbol, position_direction, close_reason)

        try:
            self.execution.close_position(symbol, side, size, source="exit_manager")
            self._pending_exit_reasons[symbol] = "exit manager"
            return True
        except Exception:
            logger.exception("Не удалось закрыть позицию %s через Exit Manager", symbol)
            return False

    def _apply_trend_filter(self, signal: Signal, trend: Optional[str], context, symbol: str) -> Signal:
        """
        Блокирует сигналы против старшего тренда (EMA50/200). trend=None означает
        "недостаточно данных для расчёта" — в этом случае фильтр НЕ блокирует,
        чтобы не парализовать систему на старте, пока не накопится 200+ свечей.
        trend="neutral" (EMA50/200 переплетены, тренда нет) — тоже не блокирует,
        так как в этом состоянии направление старшего тренда неопределённо.
        """
        if not self.cfg.trend_filter_enabled or trend is None or trend == "neutral":
            return signal
        is_counter_trend = (
            signal.action == Action.OPEN_LONG and trend == "short"
            or signal.action == Action.OPEN_SHORT and trend == "long"
        )
        if not is_counter_trend:
            return signal

        if (
            context.regime == "REVERSAL"
            and signal.confidence >= self.cfg.trend_filter_reversal_confidence
        ):
            logger.info(
                "%s: trend filter разрешил сильный REVERSAL против старшего тренда: signal=%s confidence=%.2f threshold=%.2f",
                symbol, signal.action.value, signal.confidence, self.cfg.trend_filter_reversal_confidence,
            )
            return signal

        if signal.action == Action.OPEN_LONG and trend == "short":
            return Signal(symbol=symbol, action=Action.HOLD, source=signal.source,
                           reason=f"Заблокировано trend filter: сигнал LONG против старшего тренда SHORT "
                                  f"при режиме {context.regime}, confidence={signal.confidence:.2f} "
                                  f"(было: {signal.reason})")
        if signal.action == Action.OPEN_SHORT and trend == "long":
            return Signal(symbol=symbol, action=Action.HOLD, source=signal.source,
                           reason=f"Заблокировано trend filter: сигнал SHORT против старшего тренда LONG "
                                  f"при режиме {context.regime}, confidence={signal.confidence:.2f} "
                                  f"(было: {signal.reason})")
        return signal

    def _build_entry_snapshot(
        self,
        symbol: str,
        final_signal: Signal,
        decision_report,
        market_context,
        market_snapshot: dict,
        candles_df: pd.DataFrame,
        indicators: dict,
        last_price: float,
        risk_check,
        trend_filter: Optional[str],
        meta_decision,
    ) -> dict:
        technical = self._technical_snapshot(candles_df, indicators)
        orderbook = market_snapshot.get("orderbook") or {}
        trade_flow = market_snapshot.get("trade_flow_last_minutes") or {}
        funding_trend = market_snapshot.get("funding_trend") or {}
        oi_trend = market_snapshot.get("open_interest_trend") or {}
        liquidations = market_snapshot.get("liquidations_last_hour") or {}
        return {
            "basic": {
                "symbol": symbol,
                "direction": final_signal.action.value,
                "entry_price": last_price,
                "position_size_usdt": risk_check.approved_size_usdt,
                "leverage": risk_check.approved_leverage,
                "primary_interval": self.cfg.primary_interval,
            },
            "market_context": {
                "trend": market_context.trend,
                "regime": market_context.regime,
                "volatility_state": market_context.volatility,
                "liquidity_state": market_context.liquidity,
                "volume_state": market_context.volume,
                "funding_state": market_context.funding_bias,
                "open_interest_state": market_context.open_interest_trend,
                "context_confidence": market_context.confidence,
                "risk_score": market_context.risk_score,
                "trend_filter": trend_filter,
            },
            "technical": technical,
            "microstructure": {
                "spread_pct": orderbook.get("spread_pct"),
                "orderbook_imbalance": orderbook.get("bid_ask_imbalance"),
                "trade_flow_imbalance": trade_flow.get("imbalance"),
                "funding_rate": market_snapshot.get("funding_rate"),
                "funding_trend": funding_trend.get("trend"),
                "oi_change_pct": oi_trend.get("change_pct"),
                "liquidation_count": liquidations.get("count"),
                "liquidation_volume": liquidations.get("total_volume"),
            },
            "decision": {
                "final_action": final_signal.action.value,
                "decision_confidence": decision_report.confidence,
                "expected_rr": decision_report.expected_rr,
                "risk_score": decision_report.risk_score,
                "confirmation_count": decision_report.confirmation_count,
                "confirmation_families": list(decision_report.confirmation_families),
                "selected_expert_votes": [
                    vote.source for vote in decision_report.votes
                    if not vote.ignored and vote.action == decision_report.winning_action
                ],
                "rejected_scenarios": decision_report.rejected_actions,
                "entry_reason": final_signal.reason,
                "meta_strategy_reasoning": list(meta_decision.notes),
                "ai_analyst_conclusion": decision_report.ai_analysis,
            },
        }

    def _build_exit_snapshot(self, symbol: str, closed_pnl: dict) -> dict:
        try:
            candles_df = self._load_recent_candles(symbol, limit=210)
            if candles_df is None or len(candles_df) < 30:
                return {
                    "symbol": symbol,
                    "closed_pnl": closed_pnl,
                    "market_state_available": False,
                    "reason": "not enough candles for exit market snapshot",
                }
            funding_info = self._load_latest_funding(symbol)
            funding_rate = funding_info["rate"] if funding_info else None
            funding_trend = self._load_funding_trend(symbol, limit=8)
            oi_trend = self._load_oi_trend(symbol, limit=20)
            orderbook = self._load_latest_orderbook(symbol)
            trade_flow = self._load_trade_flow(symbol, minutes=15)
            liquidations = self._load_recent_liquidations(symbol, minutes=60)
            indicators = compute_all_indicators(candles_df)
            trend = trend_direction(candles_df)
            market_snapshot = self._build_market_snapshot(
                symbol, candles_df, funding_rate, funding_trend, oi_trend,
                orderbook, trade_flow, liquidations, indicators,
            )
            market_snapshot["trend_filter"] = trend
            market_context = self.market_context_engine.analyze(symbol, candles_df, market_snapshot)
            return {
                "symbol": symbol,
                "market_state_available": True,
                "trend": market_context.trend,
                "regime": market_context.regime,
                "volatility_state": market_context.volatility,
                "liquidity_state": market_context.liquidity,
                "volume_state": market_context.volume,
                "funding_state": market_context.funding_bias,
                "open_interest_state": market_context.open_interest_trend,
                "context_confidence": market_context.confidence,
                "technical": self._technical_snapshot(candles_df, indicators),
                "microstructure": {
                    "spread_pct": (orderbook or {}).get("spread_pct"),
                    "orderbook_imbalance": (orderbook or {}).get("bid_ask_imbalance"),
                    "trade_flow_imbalance": (trade_flow or {}).get("imbalance"),
                    "funding_rate": funding_rate,
                    "funding_trend": (funding_trend or {}).get("trend"),
                    "oi_change_pct": (oi_trend or {}).get("change_pct"),
                    "liquidation_count": (liquidations or {}).get("count"),
                    "liquidation_volume": (liquidations or {}).get("total_volume"),
                },
                "signal_changes_vs_entry": None,
                "mfe_mae_note": "MFE/MAE not calculated: exact excursion requires candle-path attribution in a later stage",
            }
        except Exception:
            logger.exception("%s: не удалось построить exit snapshot", symbol)
            return {"symbol": symbol, "market_state_available": False, "reason": "snapshot build failed"}

    @staticmethod
    def _technical_snapshot(candles_df: pd.DataFrame, indicators: dict) -> dict:
        closes = candles_df["close"].astype(float)
        volumes = candles_df["volume"].astype(float)
        last_price = float(closes.iloc[-1])
        ema_fast = closes.ewm(span=12, adjust=False).mean()
        ema_slow = closes.ewm(span=26, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=9, adjust=False).mean()

        recent_20 = closes.tail(20)
        recent_40_vol = volumes.tail(min(40, len(volumes)))
        recent_volume_change = None
        if len(recent_40_vol) >= 10:
            recent = float(recent_40_vol.tail(5).mean())
            baseline = float(recent_40_vol.head(max(len(recent_40_vol) - 5, 1)).mean())
            recent_volume_change = (recent / baseline - 1) * 100 if baseline else None

        vwap_deviation = None
        if len(candles_df) >= 20:
            recent = candles_df.tail(20)
            volume = recent["volume"].astype(float)
            total_volume = float(volume.sum())
            if total_volume > 0:
                typical = (
                    recent["high"].astype(float)
                    + recent["low"].astype(float)
                    + recent["close"].astype(float)
                ) / 3
                vwap = float((typical * volume).sum() / total_volume)
                vwap_deviation = (last_price / vwap - 1) * 100 if vwap else None

        def finite(value):
            if value is None:
                return None
            value = float(value)
            return round(value, 6) if math.isfinite(value) else None

        return {
            "rsi": indicators.get("rsi"),
            "ema_fast": finite(ema_fast.iloc[-1]),
            "ema_slow": finite(ema_slow.iloc[-1]),
            "ema_distance_pct": finite((ema_fast.iloc[-1] - ema_slow.iloc[-1]) / last_price * 100 if last_price else None),
            "macd": finite(macd_line.iloc[-1]),
            "macd_signal": finite(macd_signal.iloc[-1]),
            "macd_histogram": indicators.get("macd_histogram"),
            "atr": indicators.get("atr"),
            "atr_pct_of_price": indicators.get("atr_pct_of_price"),
            "vwap_deviation_pct": finite(vwap_deviation),
            "recent_price_change_pct": finite((recent_20.iloc[-1] / recent_20.iloc[0] - 1) * 100 if len(recent_20) > 1 and recent_20.iloc[0] else None),
            "recent_volume_change_pct": finite(recent_volume_change),
        }

    @staticmethod
    def _expert_vote_rows(decision_report) -> list[dict]:
        rows = []
        for vote in decision_report.votes:
            rows.append({
                "source": vote.source,
                "family": DecisionEngine._source_family(vote.source),
                "action": vote.action.value,
                "confidence": vote.confidence,
                "reason": vote.reason,
                "weight": None,
                "contributed_to_final_decision": (
                    not vote.ignored
                    and decision_report.winning_action != Action.HOLD
                    and vote.action == decision_report.winning_action
                ),
            })
        return rows

    @staticmethod
    def _log_decision_summary(symbol: str, decision_report, final_signal: Signal, market_context, trend: Optional[str]):
        rejected = dict(decision_report.rejected_actions)
        if decision_report.final_signal.action != Action.HOLD and final_signal.action == Action.HOLD:
            rejected["trend_filter"] = final_signal.reason
        logger.info(
            "TRADE_CANDIDATE symbol=%s final_action=%s confidence=%.3f expected_rr=%s "
            "confirmation_count=%d confirmation_families=%s regime=%s context_trend=%s trend_filter=%s rejected=%s",
            symbol,
            final_signal.action.value,
            final_signal.confidence,
            decision_report.expected_rr,
            decision_report.confirmation_count,
            ",".join(decision_report.confirmation_families) or "none",
            market_context.regime,
            market_context.trend,
            trend or "unknown",
            rejected or "none",
        )

    @staticmethod
    def _supporting_experts(decision_report) -> str:
        supporters = [
            vote.source
            for vote in decision_report.votes
            if not vote.ignored and vote.action == decision_report.winning_action
        ]
        return ",".join(supporters) if supporters else "none"

    @staticmethod
    def _price_from_pct(last_price: float, action: Action, pct: Optional[float], is_stop: bool) -> Optional[float]:
        if pct is None:
            return None
        multiplier = pct / 100
        if action == Action.OPEN_LONG:
            price = last_price * (1 - multiplier if is_stop else 1 + multiplier)
        elif action == Action.OPEN_SHORT:
            price = last_price * (1 + multiplier if is_stop else 1 - multiplier)
        else:
            return None
        return round(price, 6)

    @staticmethod
    def _position_pnl_pct(trade: dict, exit_price: float) -> float:
        entry_price = float(trade.get("entry_price") or 0)
        if entry_price <= 0:
            return 0.0
        action = trade.get("action")
        if action == Action.OPEN_SHORT.value or action == "open_short":
            return round((entry_price - exit_price) / entry_price * 100, 4)
        return round((exit_price - entry_price) / entry_price * 100, 4)

    @staticmethod
    def _holding_seconds(opened_at_ms: Optional[int]) -> Optional[int]:
        if opened_at_ms is None:
            return None
        return max(0, int((time.time() * 1000 - opened_at_ms) / 1000))

    @staticmethod
    def _infer_exit_reason(closed_pnl: dict) -> str:
        text = " ".join(
            str(closed_pnl.get(key) or "")
            for key in ("orderType", "execType", "stopOrderType", "orderLinkId")
        ).lower()
        if "takeprofit" in text or "take_profit" in text or "tp" in text:
            return "TP"
        if "trailing" in text:
            return "trailing"
        if "stoploss" in text or "stop_loss" in text or "sl" in text:
            return "SL"
        return "manual/unknown"

    def _reconcile(self, rule_signal: Optional[Signal], ai_signal: Optional[Signal], symbol: str) -> Signal:
        """
        Простая, объяснимая логика примирения:
        - Если оба источника предлагают одно и то же направление — уверенный сигнал.
        - Если направления противоречат друг другу — HOLD (перестраховка).
        - Если один HOLD, а другой уверен (confidence >= 0.7) — пропускаем сигнал уверенного.
          Это и есть "AI сам видит выгодное — торгует по нему, схема совпала — торгует по ней".
        - Иначе HOLD.
        """
        rs = rule_signal.action if rule_signal else Action.HOLD
        as_ = ai_signal.action if ai_signal else Action.HOLD

        both_open = {Action.OPEN_LONG, Action.OPEN_SHORT}

        if rs in both_open and as_ in both_open:
            if rs == as_:
                return Signal(
                    symbol=symbol, action=rs, source="rule+ai",
                    confidence=max(rule_signal.confidence, ai_signal.confidence),
                    reason=f"Совпадение схемы и ИИ: {rule_signal.reason} | {ai_signal.reason}",
                    stop_loss_pct=min(
                        rule_signal.stop_loss_pct or 1.5, ai_signal.stop_loss_pct or 1.5
                    ),
                    take_profit_pct=rule_signal.take_profit_pct or ai_signal.take_profit_pct,
                )
            else:
                return Signal(symbol=symbol, action=Action.HOLD, source="rule+ai",
                               reason="Схема и ИИ дали противоположные сигналы — перестраховка")

        if rs in both_open and as_ == Action.HOLD:
            return rule_signal  # чистое срабатывание схемы

        if as_ in both_open and rs == Action.HOLD:
            if ai_signal.confidence >= 0.7:
                return ai_signal  # ИИ нашёл что-то своё и достаточно уверен
            return Signal(symbol=symbol, action=Action.HOLD, source="ai",
                           reason=f"ИИ предложил сигнал, но confidence={ai_signal.confidence:.2f} < 0.7")

        return Signal(symbol=symbol, action=Action.HOLD, source="rule+ai", reason="Нет сигналов")

    # ------------------------------------------------------------------

    def _load_recent_candles(self, symbol: str, limit: int = 100) -> Optional[pd.DataFrame]:
        session = self.db.get_session()
        try:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == symbol, Candle.interval == self.cfg.primary_interval)
                .order_by(Candle.start_time.desc())
                .limit(limit)
                .all()
            )
            if not rows:
                return None
            data = [{
                "start_time": r.start_time, "open": r.open, "high": r.high,
                "low": r.low, "close": r.close, "volume": r.volume,
            } for r in reversed(rows)]  # разворачиваем в хронологический порядок
            return pd.DataFrame(data)
        finally:
            session.close()

    def _load_latest_funding(self, symbol: str) -> Optional[dict]:
        session = self.db.get_session()
        try:
            row = (
                session.query(FundingRate)
                .filter(FundingRate.symbol == symbol)
                .order_by(FundingRate.funding_ts.desc())
                .first()
            )
            return {"rate": float(row.funding_rate), "ts": int(row.funding_ts)} if row else None
        finally:
            session.close()

    def _load_funding_trend(self, symbol: str, limit: int = 8) -> Optional[dict]:
        """
        Последние N значений funding rate. Тренд важен не меньше, чем текущее
        значение: устойчиво растущий funding говорит о нарастающем перекосе
        рынка в сторону лонгов (и наоборот).
        """
        session = self.db.get_session()
        try:
            rows = (
                session.query(FundingRate)
                .filter(FundingRate.symbol == symbol)
                .order_by(FundingRate.funding_ts.desc())
                .limit(limit)
                .all()
            )
            if not rows:
                return None
            values = [float(r.funding_rate) for r in reversed(rows)]
            return {
                "recent_values": values,
                "trend": "растёт" if values[-1] > values[0] else "падает" if values[-1] < values[0] else "стабилен",
                "latest_ts": int(max(r.funding_ts for r in rows)),
            }
        finally:
            session.close()

    def _load_oi_trend(self, symbol: str, limit: int = 20) -> Optional[dict]:
        """
        Open Interest: растущий OI при растущей цене — сильный тренд (новые деньги
        заходят в лонг). Растущий OI при падающей цене — усиление шортов.
        Падающий OI — закрытие позиций, тренд слабеет.
        """
        session = self.db.get_session()
        try:
            rows = (
                session.query(OpenInterest)
                .filter(OpenInterest.symbol == symbol)
                .order_by(OpenInterest.ts.desc())
                .limit(limit)
                .all()
            )
            if not rows:
                return None
            values = [float(r.open_interest) for r in reversed(rows)]
            change_pct = round((values[-1] / values[0] - 1) * 100, 3) if values[0] else 0.0
            return {"current": values[-1], "change_pct": change_pct, "latest_ts": int(max(r.ts for r in rows))}
        finally:
            session.close()

    def _load_latest_orderbook(self, symbol: str) -> Optional[dict]:
        """Топ стакана — спред и дисбаланс объёма bid/ask (кто сейчас агрессивнее давит)."""
        session = self.db.get_session()
        try:
            row = (
                session.query(OrderbookSnapshot)
                .filter(OrderbookSnapshot.symbol == symbol)
                .order_by(OrderbookSnapshot.ts.desc())
                .first()
            )
            if not row:
                return None
            bid_size = float(row.best_bid_size)
            ask_size = float(row.best_ask_size)
            total = bid_size + ask_size
            imbalance = round((bid_size - ask_size) / total, 3) if total > 0 else 0.0
            spread_pct = round(
                (float(row.best_ask_price) - float(row.best_bid_price)) / float(row.best_bid_price) * 100, 4
            )
            return {
                "ts": int(row.ts),
                "spread_pct": spread_pct,
                # imbalance: >0 значит больше объёма на покупку (bid), <0 — на продажу (ask)
                "bid_ask_imbalance": imbalance,
            }
        finally:
            session.close()

    def _load_trade_flow(self, symbol: str, minutes: int = 15) -> Optional[dict]:
        """
        Соотношение объёма покупок/продаж за последние N минут по реальным сделкам
        (не по стакану, а по факту исполненных сделок) — order flow imbalance.
        """
        session = self.db.get_session()
        try:
            since_ts = int(time.time() * 1000) - minutes * 60_000
            rows = (
                session.query(Trade)
                .filter(Trade.symbol == symbol, Trade.ts >= since_ts)
                .all()
            )
            if not rows:
                return None
            buy_vol = sum(float(r.size) for r in rows if r.side == "Buy")
            sell_vol = sum(float(r.size) for r in rows if r.side == "Sell")
            total = buy_vol + sell_vol
            if total == 0:
                return None
            return {
                "buy_volume": round(buy_vol, 4),
                "sell_volume": round(sell_vol, 4),
                # >0 значит покупки преобладают, <0 — продажи
                "imbalance": round((buy_vol - sell_vol) / total, 3),
                "window_minutes": minutes,
                "latest_ts": int(max(r.ts for r in rows)),
            }
        finally:
            session.close()

    def _load_recent_liquidations(self, symbol: str, minutes: int = 60) -> Optional[dict]:
        """
        Ликвидации — сигнал стресса рынка. Каскад ликвидаций часто предшествует
        развороту (капитуляция) или продолжению движения (шорт/лонг-сквиз).
        """
        session = self.db.get_session()
        try:
            since_ts = int(time.time() * 1000) - minutes * 60_000
            rows = (
                session.query(Liquidation)
                .filter(Liquidation.symbol == symbol, Liquidation.ts >= since_ts)
                .all()
            )
            if not rows:
                return {"count": 0, "total_volume": 0, "window_minutes": minutes}
            total_volume = sum(float(r.size) for r in rows)
            long_liqs = sum(1 for r in rows if r.side == "Sell")  # ликвидация лонга = принудительная продажа
            short_liqs = sum(1 for r in rows if r.side == "Buy")
            return {
                "count": len(rows),
                "total_volume": round(total_volume, 4),
                "long_liquidations": long_liqs,
                "short_liquidations": short_liqs,
                "window_minutes": minutes,
            }
        finally:
            session.close()

    def _build_market_snapshot(
        self, symbol: str, candles_df: pd.DataFrame, funding_rate: Optional[float],
        funding_trend: Optional[dict], oi_trend: Optional[dict],
        orderbook: Optional[dict], trade_flow: Optional[dict], liquidations: Optional[dict],
        indicators: Optional[dict],
    ) -> dict:
        recent_20 = candles_df.tail(20)
        recent_50 = candles_df.tail(min(50, len(candles_df)))
        closes = candles_df["close"].astype(float)
        returns = closes.pct_change().dropna()

        snapshot = {
            "last_price": float(recent_20["close"].iloc[-1]),
            "price_change_pct_last_20_candles": round(
                (float(recent_20["close"].iloc[-1]) / float(recent_20["close"].iloc[0]) - 1) * 100, 3
            ),
            "price_change_pct_last_50_candles": round(
                (float(recent_50["close"].iloc[-1]) / float(recent_50["close"].iloc[0]) - 1) * 100, 3
            ),
            "high_20": float(recent_20["high"].max()),
            "low_20": float(recent_20["low"].min()),
            "avg_volume_20": float(recent_20["volume"].astype(float).mean()),
            "volatility_pct": round(float(returns.tail(20).std() * 100), 4) if len(returns) >= 20 else None,
            "funding_rate": funding_rate,
            "funding_trend": funding_trend,
            "open_interest_trend": oi_trend,
            "orderbook": orderbook,
            "trade_flow_last_minutes": trade_flow,
            "liquidations_last_hour": liquidations,
        }
        if indicators:
            snapshot["indicators"] = indicators
        return snapshot

    def _check_data_freshness(
        self,
        symbol: str,
        candles_df: pd.DataFrame,
        funding_info: Optional[dict],
        oi_trend: Optional[dict],
        orderbook: Optional[dict],
        trade_flow: Optional[dict],
    ) -> dict:
        warnings = []
        critical = False

        last_candle_ts = int(candles_df["start_time"].iloc[-1])
        candle_age_min = self._age_seconds(last_candle_ts) / 60
        if candle_age_min > self.cfg.max_candle_age_minutes:
            critical = True
            warnings.append(
                f"candles stale {candle_age_min:.1f}m > {self.cfg.max_candle_age_minutes}m; проверь main.py/kline WS"
            )

        if orderbook is None:
            warnings.append("orderbook missing; проверь main.py/orderbook WS")
        else:
            orderbook_age = self._age_seconds(orderbook["ts"])
            if orderbook_age > self.cfg.max_orderbook_age_seconds:
                warnings.append(
                    f"orderbook stale {orderbook_age:.0f}s > {self.cfg.max_orderbook_age_seconds}s"
                )

        if trade_flow is None:
            warnings.append("trade flow missing; momentum будет HOLD")
        else:
            trade_flow_age = self._age_seconds(trade_flow["latest_ts"])
            if trade_flow_age > self.cfg.max_trade_flow_age_seconds:
                warnings.append(
                    f"trade flow stale {trade_flow_age:.0f}s > {self.cfg.max_trade_flow_age_seconds}s"
                )

        if funding_info is None:
            warnings.append("funding missing; funding expert ослаблен")
        else:
            funding_age_min = self._age_seconds(funding_info["ts"]) / 60
            if funding_age_min > self.cfg.max_funding_age_minutes:
                warnings.append(
                    f"funding stale {funding_age_min:.1f}m > {self.cfg.max_funding_age_minutes}m"
                )

        if oi_trend is None:
            warnings.append("open interest missing")
        else:
            oi_age_min = self._age_seconds(oi_trend["latest_ts"]) / 60
            if oi_age_min > self.cfg.max_open_interest_age_minutes:
                warnings.append(
                    f"open interest stale {oi_age_min:.1f}m > {self.cfg.max_open_interest_age_minutes}m"
                )

        if warnings:
            logger.info("%s: data freshness warnings: %s", symbol, "; ".join(warnings))
        return {"critical": critical, "warnings": warnings}

    @staticmethod
    def _age_seconds(ts_ms: int) -> float:
        return max(0.0, (time.time() * 1000 - ts_ms) / 1000)
