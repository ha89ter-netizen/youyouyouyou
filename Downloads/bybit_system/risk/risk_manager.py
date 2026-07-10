"""
Risk Manager: единственный компонент, который решает — можно ли сигнал
превратить в реальный ордер. Ни Strategy Engine, ни ИИ-стратегия не имеют
прямого доступа к Execution Engine — только через этот слой.

Принцип: Risk Manager ничего не "оптимизирует" и не пытается быть умным.
Его задача — тупо и надёжно резать всё, что превышает жёсткие лимиты
из конфига. Чем проще этот код, тем меньше шанс, что в нём баг пропустит
что-то опасное.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

from config.settings import BybitConfig
from strategy.signal import Signal, Action

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str
    # Скорректированные параметры (Risk Manager может урезать размер,
    # но никогда не увеличивает то, что предложила стратегия)
    approved_size_usdt: Optional[float] = None
    approved_leverage: Optional[int] = None


class RiskManager:
    def __init__(self, cfg: BybitConfig):
        self.cfg = cfg
        self._daily_pnl_usdt: float = 0.0
        self._daily_start_balance: Optional[float] = None
        self._daily_reset_date = datetime.now(timezone.utc).date()
        self._circuit_breaker_tripped = False
        self._circuit_breaker_reason = ""
        self._daily_trade_count = 0
        self._symbol_trade_counts: dict[str, int] = {}
        self._last_entry_ts_by_symbol: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        signal: Signal,
        open_positions: List[dict],
        account_balance_usdt: float,
        atr_pct_of_price: Optional[float] = None,
        spread_pct: Optional[float] = None,
        funding_rate: Optional[float] = None,
        position_size_multiplier: float = 1.0,
    ) -> RiskCheckResult:
        """
        open_positions: список текущих открытых позиций (из get_positions())
        account_balance_usdt: текущий баланс счёта
        atr_pct_of_price / spread_pct: опциональные данные для volatility/liquidity
            гейтов. Если не переданы — эти проверки просто пропускаются
            (не блокируют), а не считаются автоматическим провалом.
        """
        self._maybe_reset_daily_counters(account_balance_usdt)

        if self._circuit_breaker_tripped:
            return RiskCheckResult(
                approved=False,
                reason=f"Circuit breaker активен: {self._circuit_breaker_reason}",
            )

        if signal.action == Action.HOLD:
            return RiskCheckResult(approved=False, reason="Сигнал HOLD, действие не требуется")

        if signal.action == Action.CLOSE:
            # Закрытие позиций всегда разрешаем — снижение риска не опасно
            return RiskCheckResult(approved=True, reason="Закрытие позиции разрешено без ограничений")

        if signal.confidence < self.cfg.min_open_confidence:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"Confidence итогового сигнала {signal.confidence:.2f} ниже "
                    f"порога {self.cfg.min_open_confidence:.2f}"
                ),
            )

        if self._daily_trade_count >= self.cfg.max_daily_trades:
            return RiskCheckResult(
                approved=False,
                reason=f"Достигнут дневной лимит сделок ({self._daily_trade_count}/{self.cfg.max_daily_trades})",
            )

        symbol_count = self._symbol_trade_counts.get(signal.symbol, 0)
        if symbol_count >= self.cfg.max_trades_per_symbol:
            return RiskCheckResult(
                approved=False,
                reason=(
                    f"Достигнут дневной лимит сделок по {signal.symbol} "
                    f"({symbol_count}/{self.cfg.max_trades_per_symbol})"
                ),
            )

        last_entry_ts = self._last_entry_ts_by_symbol.get(signal.symbol)
        if last_entry_ts is not None:
            elapsed_minutes = (datetime.now(timezone.utc).timestamp() - last_entry_ts) / 60
            if elapsed_minutes < self.cfg.cooldown_minutes:
                return RiskCheckResult(
                    approved=False,
                    reason=(
                        f"Cooldown по {signal.symbol}: прошло {elapsed_minutes:.1f}m "
                        f"из {self.cfg.cooldown_minutes}m"
                    ),
                )

        # --- Проверка 1: дневной лимит убытка (в % от баланса на начало дня) ---
        daily_loss_limit_usdt = self._daily_loss_limit_usdt()
        if daily_loss_limit_usdt is not None and self._daily_pnl_usdt <= -daily_loss_limit_usdt:
            self._trip_circuit_breaker(
                f"Дневной убыток {self._daily_pnl_usdt:.2f} USDT достиг лимита "
                f"{daily_loss_limit_usdt:.2f} USDT ({self.cfg.max_daily_loss_pct}% от баланса на начало дня)"
            )
            return RiskCheckResult(approved=False, reason=self._circuit_breaker_reason)

        # --- Проверка 2: волатильность (ATR) ---
        if atr_pct_of_price is not None and atr_pct_of_price > self.cfg.max_volatility_atr_pct:
            return RiskCheckResult(
                approved=False,
                reason=f"Волатильность слишком высокая: ATR={atr_pct_of_price:.2f}% "
                       f"> лимита {self.cfg.max_volatility_atr_pct}%",
            )

        # --- Проверка 3: ликвидность (спред) ---
        if spread_pct is not None and spread_pct > self.cfg.max_spread_pct:
            return RiskCheckResult(
                approved=False,
                reason=f"Спред слишком широкий: {spread_pct:.3f}% > лимита {self.cfg.max_spread_pct}% "
                       f"(низкая ликвидность, риск плохого исполнения)",
            )

        # --- Проверка 4: funding не должен делать удержание позиции заведомо дорогим ---
        if funding_rate is not None:
            if signal.action == Action.OPEN_LONG and funding_rate > self.cfg.max_long_funding_rate:
                return RiskCheckResult(
                    approved=False,
                    reason=(
                        f"Funding слишком дорогой для LONG: {funding_rate:.5f} "
                        f"> {self.cfg.max_long_funding_rate:.5f}"
                    ),
                )
            if signal.action == Action.OPEN_SHORT and funding_rate < -self.cfg.max_short_funding_rate_abs:
                return RiskCheckResult(
                    approved=False,
                    reason=(
                        f"Funding слишком дорогой для SHORT: {funding_rate:.5f} "
                        f"< -{self.cfg.max_short_funding_rate_abs:.5f}"
                    ),
                )

        # --- Проверка 5: количество открытых позиций ---
        open_count = len([p for p in open_positions if float(p.get("size", 0)) > 0])
        if open_count >= self.cfg.max_open_positions:
            return RiskCheckResult(
                approved=False,
                reason=f"Достигнут лимит открытых позиций ({open_count}/{self.cfg.max_open_positions})",
            )

        # --- Проверка 6: не открываем вторую позицию по тому же символу ---
        for p in open_positions:
            if p.get("symbol") == signal.symbol and float(p.get("size", 0)) > 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"По {signal.symbol} уже есть открытая позиция",
                )

        # --- Проверка 7: обязательный стоп-лосс (нужен и для сайзинга, и для ордера) ---
        stop_loss_pct = signal.stop_loss_pct or self.cfg.default_stop_loss_pct
        if signal.stop_loss_pct is None:
            logger.warning(
                "Сигнал %s по %s без stop_loss_pct — применяю дефолтный %.2f%%",
                signal.source, signal.symbol, self.cfg.default_stop_loss_pct,
            )

        # --- Проверка 8: размер позиции — risk-based sizing ---
        # Формула: сколько USDT готовы потерять на сделке (risk_amount), делим на
        # дистанцию до стоп-лосса в долях -> получаем номинальный размер позиции,
        # при котором срабатывание SL даст убыток ровно risk_amount, а не больше.
        risk_amount_usdt = account_balance_usdt * (self.cfg.risk_per_trade_pct / 100)
        sizing_size = risk_amount_usdt / (stop_loss_pct / 100)

        requested_size = signal.suggested_size_usdt
        # Если стратегия сама предложила размер — не даём ей превысить risk-based расчёт,
        # берём меньшее из двух (стратегия может попросить МЕНЬШЕ, но не больше)
        approved_size = min(sizing_size, requested_size) if requested_size else sizing_size
        # Жёсткий потолок из конфига — не даёт risk-sizing'у улететь при большом балансе
        approved_size = min(approved_size, self.cfg.max_position_usdt)
        # Meta Strategy Manager может только УМЕНЬШАТЬ размер в сложном контексте
        # (high volatility / low liquidity). Увеличивать риск этим множителем нельзя.
        approved_size *= max(0.1, min(position_size_multiplier, 1.0))
        # Никогда не рискуем больше, чем позволяет баланс
        approved_size = min(approved_size, account_balance_usdt * 0.9)

        if approved_size <= 0:
            return RiskCheckResult(approved=False, reason="Недостаточно баланса для открытия позиции")

        # --- Проверка 9: плечо ---
        requested_leverage = signal.suggested_leverage or 1
        approved_leverage = min(requested_leverage, self.cfg.max_leverage)

        logger.info(
            "Risk Manager одобрил: %s %s size=%.2f leverage=%dx SL=%.2f%% "
            "(risk=%.2f%% баланса = %.2f USDT, sizing_size=%.2f)",
            signal.action, signal.symbol, approved_size, approved_leverage, stop_loss_pct,
            self.cfg.risk_per_trade_pct, risk_amount_usdt, sizing_size,
        )

        return RiskCheckResult(
            approved=True,
            reason="OK",
            approved_size_usdt=approved_size,
            approved_leverage=approved_leverage,
        )

    def ensure_daily_reset(self, current_balance: float):
        """
        Вызывать РАЗ ЗА ЦИКЛ из Strategy Engine, независимо от того, есть ли
        сигналы на сделку. Раньше сброс происходил только внутри evaluate(),
        а evaluate() вызывается лишь когда есть реальный сигнал — если рынок
        "молчит" в начале нового дня, точка отсчёта захватывалась бы позже,
        потенциально уже на просевшем балансе (например, из-за досрочного
        закрытия вчерашней позиции рано утром).
        """
        self._maybe_reset_daily_counters(current_balance)

    def record_closed_pnl(self, pnl_usdt: float):
        """Вызывать после КАЖДОГО закрытия позиции — чтобы дневной лимит убытка работал."""
        self._daily_pnl_usdt += pnl_usdt
        logger.info("Дневной PnL обновлён: %.2f USDT (изменение %.2f)", self._daily_pnl_usdt, pnl_usdt)

    def record_open_trade(self, symbol: str):
        """Вызывать после успешного создания ордера, чтобы работали лимиты частоты торговли."""
        self._daily_trade_count += 1
        self._symbol_trade_counts[symbol] = self._symbol_trade_counts.get(symbol, 0) + 1
        self._last_entry_ts_by_symbol[symbol] = datetime.now(timezone.utc).timestamp()
        logger.info(
            "Счётчики сделок обновлены: daily=%d/%d, %s=%d/%d",
            self._daily_trade_count, self.cfg.max_daily_trades,
            symbol, self._symbol_trade_counts[symbol], self.cfg.max_trades_per_symbol,
        )

    def manual_reset_circuit_breaker(self):
        """
        Сознательно ручной метод — Risk Manager НЕ снимает circuit breaker сам.
        Только человек, посмотрев, что произошло, может возобновить торговлю.
        """
        logger.warning("Circuit breaker сброшен вручную оператором")
        self._circuit_breaker_tripped = False
        self._circuit_breaker_reason = ""

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _daily_loss_limit_usdt(self) -> Optional[float]:
        if self._daily_start_balance is None:
            return None
        return self._daily_start_balance * (self.cfg.max_daily_loss_pct / 100)

    def _trip_circuit_breaker(self, reason: str):
        self._circuit_breaker_tripped = True
        self._circuit_breaker_reason = reason
        logger.error("CIRCUIT BREAKER АКТИВИРОВАН: %s", reason)

    def _maybe_reset_daily_counters(self, current_balance: float):
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date or self._daily_start_balance is None:
            logger.info(
                "Новый торговый день (или первый запуск) — фиксирую баланс на начало дня: %.2f USDT",
                current_balance,
            )
            self._daily_pnl_usdt = 0.0
            self._daily_trade_count = 0
            self._symbol_trade_counts.clear()
            self._last_entry_ts_by_symbol.clear()
            self._daily_start_balance = current_balance
            self._daily_reset_date = today
            self._circuit_breaker_tripped = False
            self._circuit_breaker_reason = ""
