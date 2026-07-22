"""
Risk Manager: единственный компонент, который решает — можно ли сигнал
превратить в реальный ордер. Ни Strategy Engine, ни ИИ-стратегия не имеют
прямого доступа к Execution Engine — только через этот слой.

Принцип: Risk Manager ничего не "оптимизирует" и не пытается быть умным.
Его задача — тупо и надёжно резать всё, что превышает жёсткие лимиты
из конфига. Чем проще этот код, тем меньше шанс, что в нём баг пропустит
что-то опасное.

Состояние (дневной PnL, счётчики, cooldown, circuit breaker) персистится через
RiskStateStore и переживает перезапуск процесса. Дневные значения обнуляются
ТОЛЬКО при смене UTC-дня. Рестарт сам по себе не снимает ни одного лимита —
раньше именно это позволяло упавшему боту забыть сегодняшний убыток.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from config.settings import BybitConfig
from strategy.signal import Signal, Action
from timeutils import parse_utc_day, utc_day_str, utc_today, utcnow

logger = logging.getLogger(__name__)

# Стабильный ключ причины "дневной лимит убытка". Привязан к суткам.
DAILY_LOSS_CAUSE = "daily_loss"


def orphan_cause(order_link_id: str) -> str:
    """Ключ причины breaker для конкретной orphaned-сделки."""
    return f"orphan:{order_link_id}"


def _causes_from_state(state: dict) -> Dict[str, dict]:
    """
    Восстанавливает причины из состояния. Строки, записанные до появления
    именованных причин, поднимаются как одна legacy-причина, чтобы взведённый
    breaker не потерялся при обновлении кода.
    """
    raw = state.get("circuit_breaker_causes")
    if isinstance(raw, dict) and raw:
        causes = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            causes[str(key)] = {
                "reason": str(value.get("reason", "")),
                "sticky": bool(value.get("sticky")),
            }
        if causes:
            return causes
    if state.get("circuit_breaker_tripped"):
        return {
            "legacy": {
                "reason": state.get("circuit_breaker_reason", "причина не сохранена"),
                "sticky": bool(state.get("circuit_breaker_sticky")),
            }
        }
    return {}


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str
    # Скорректированные параметры (Risk Manager может урезать размер,
    # но никогда не увеличивает то, что предложила стратегия)
    approved_size_usdt: Optional[float] = None
    approved_leverage: Optional[int] = None


class RiskManager:
    def __init__(self, cfg: BybitConfig, state_store=None):
        """
        state_store: RiskStateStore или None. None — состояние живёт только в
        памяти (используется в тестах и в backtest/paper-режимах, где рестарт
        не несёт финансового риска). В боевом торговом цикле store обязателен.
        """
        self.cfg = cfg
        self._store = state_store

        self._daily_pnl_usdt: float = 0.0
        self._daily_start_balance: Optional[float] = None
        self._daily_reset_date = utc_today()
        # cause_key -> {"reason": str, "sticky": bool}. Breaker взведён, пока
        # словарь непуст. Причины именованы, чтобы снятие одной (например,
        # восстановленной orphaned-сделки) не снимало остальные.
        self._breaker_causes: Dict[str, dict] = {}
        self._daily_trade_count = 0
        self._symbol_trade_counts: Dict[str, int] = {}
        self._last_entry_ts_by_symbol: Dict[str, float] = {}
        # Ордер принят биржей, но fill ещё не подтверждён: symbol -> unix seconds
        self._pending_entries: Dict[str, float] = {}
        # Символ заблокирован до ручного разбора: symbol -> причина
        self._blocked_symbols: Dict[str, str] = {}

        if self._store is not None:
            self._load_state()

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

        if self.circuit_breaker_tripped:
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

        # Все причины "символ занят" собраны в одном месте: блокировка, неподтверждённый
        # ордер, cooldown, лимит сделок по символу.
        busy_reason = self.symbol_block_reason(signal.symbol)
        if busy_reason:
            return RiskCheckResult(approved=False, reason=busy_reason)

        # --- Проверка 1: дневной лимит убытка (в % от баланса на начало дня) ---
        daily_loss_limit_usdt = self._daily_loss_limit_usdt()
        if daily_loss_limit_usdt is not None and self._daily_pnl_usdt <= -daily_loss_limit_usdt:
            self.trip_circuit_breaker(
                f"Дневной убыток {self._daily_pnl_usdt:.2f} USDT достиг лимита "
                f"{daily_loss_limit_usdt:.2f} USDT ({self.cfg.max_daily_loss_pct}% от баланса на начало дня)",
                sticky=False,          # привязан к суткам: новый день снимает
                cause=DAILY_LOSS_CAUSE,
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

    def symbol_block_reason(self, symbol: str) -> Optional[str]:
        """
        Единая точка ответа на вопрос "занят ли символ прямо сейчас".
        Возвращает причину блокировки или None, если вход по символу допустим.

        Живые позиции с биржи и открытые сделки в журнале сюда НЕ входят — это
        внешние источники, их проверяет Strategy Engine. Здесь только то, что
        Risk Manager знает сам.
        """
        blocked = self._blocked_symbols.get(symbol)
        if blocked:
            return f"Символ {symbol} заблокирован до ручного разбора: {blocked}"

        if symbol in self._pending_entries:
            age = max(0.0, utcnow().timestamp() - self._pending_entries[symbol])
            return (
                f"По {symbol} есть принятый, но ещё не подтверждённый ордер "
                f"({age:.0f}s назад) — повторный вход запрещён"
            )

        symbol_count = self._symbol_trade_counts.get(symbol, 0)
        if symbol_count >= self.cfg.max_trades_per_symbol:
            return (
                f"Достигнут дневной лимит сделок по {symbol} "
                f"({symbol_count}/{self.cfg.max_trades_per_symbol})"
            )

        last_entry_ts = self._last_entry_ts_by_symbol.get(symbol)
        if last_entry_ts is not None:
            elapsed_minutes = (utcnow().timestamp() - last_entry_ts) / 60
            if elapsed_minutes < self.cfg.cooldown_minutes:
                return (
                    f"Cooldown по {symbol}: прошло {elapsed_minutes:.1f}m "
                    f"из {self.cfg.cooldown_minutes}m"
                )
        return None

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
        self._persist()

    def record_open_trade(self, symbol: str):
        """
        Вызывать СРАЗУ после того, как биржа приняла ордер, — до записи в журнал
        и до подтверждения fill.

        Порядок критичен: если сначала писать журнал, то отказ БД оставит
        позицию открытой на бирже, но со снятым cooldown и необновлённым
        счётчиком — и следующий цикл войдёт в тот же символ повторно.
        Ошибка журнала не должна давать право на второй вход.
        """
        self._daily_trade_count += 1
        self._symbol_trade_counts[symbol] = self._symbol_trade_counts.get(symbol, 0) + 1
        self._last_entry_ts_by_symbol[symbol] = utcnow().timestamp()
        logger.info(
            "Счётчики сделок обновлены: daily=%d/%d, %s=%d/%d",
            self._daily_trade_count, self.cfg.max_daily_trades,
            symbol, self._symbol_trade_counts[symbol], self.cfg.max_trades_per_symbol,
        )
        self._persist()

    def mark_entry_pending(self, symbol: str):
        """Ордер принят биржей, fill ещё не подтверждён — символ занят."""
        self._pending_entries[symbol] = utcnow().timestamp()
        self._persist()

    def clear_entry_pending(self, symbol: str):
        """Судьба ордера выяснена (filled / partially_filled / rejected)."""
        if self._pending_entries.pop(symbol, None) is not None:
            self._persist()

    def pending_entry_age_seconds(self, symbol: str) -> Optional[float]:
        ts = self._pending_entries.get(symbol)
        return max(0.0, utcnow().timestamp() - ts) if ts is not None else None

    def pending_entry_symbols(self) -> List[str]:
        return list(self._pending_entries)

    def block_symbol(self, symbol: str, reason: str):
        """
        Жёсткая блокировка символа до ручного разбора. Применяется, когда
        состояние ордера/позиции неизвестно: торговать вслепую опаснее,
        чем не торговать.
        """
        if self._blocked_symbols.get(symbol) == reason:
            return
        self._blocked_symbols[symbol] = reason
        logger.critical("Символ %s заблокирован для новых входов: %s", symbol, reason)
        self._persist()

    def unblock_symbol(self, symbol: str):
        """Снимается только человеком, разобравшимся в причине."""
        if self._blocked_symbols.pop(symbol, None) is not None:
            logger.warning("Блокировка символа %s снята вручную оператором", symbol)
            self._persist()

    def blocked_symbols(self) -> Dict[str, str]:
        return dict(self._blocked_symbols)

    def trip_circuit_breaker(self, reason: str, sticky: bool = False, cause: str = "unspecified"):
        """
        Взводит circuit breaker с ИМЕНОВАННОЙ причиной.

        cause — стабильный ключ причины ("daily_loss", "orphan:<order_link_id>").
        Именно он позволяет снять ровно одну причину, когда она устранена, не
        трогая остальные: восстановленная orphaned-сделка не должна снимать
        breaker, взведённый дневным лимитом убытка.

        sticky=False — причина привязана к суткам (дневной лимит убытка):
        смена UTC-дня снимает её автоматически.
        sticky=True — причина к суткам не привязана (неизвестный финансовый
        результат): переживает и рестарт, и смену дня. Снимается только
        устранением причины (resolve_breaker_cause) или ручным сбросом.
        """
        existing = self._breaker_causes.get(cause)
        if existing is not None and existing["reason"] == reason and existing["sticky"] == sticky:
            return
        self._breaker_causes[cause] = {"reason": reason, "sticky": bool(sticky)}
        logger.error(
            "CIRCUIT BREAKER АКТИВИРОВАН [%s]%s: %s. Активных причин: %d",
            cause,
            " (снимается только устранением причины)" if sticky else "",
            reason,
            len(self._breaker_causes),
        )
        self._persist()

    def resolve_breaker_cause(self, cause: str) -> bool:
        """
        Снимает ОДНУ причину breaker, когда она реально устранена
        (например, для orphaned-сделки нашёлся настоящий PnL).

        Идемпотентно: повторный вызов по уже снятой причине ничего не делает и
        возвращает False. Breaker гаснет только когда снята последняя причина.
        """
        removed = self._breaker_causes.pop(cause, None)
        if removed is None:
            return False

        if self._breaker_causes:
            logger.warning(
                "Причина circuit breaker [%s] устранена (%s), но breaker остаётся взведён: "
                "ещё %d активных причин: %s",
                cause, removed["reason"], len(self._breaker_causes),
                ", ".join(self._breaker_causes),
            )
        else:
            logger.warning(
                "Причина circuit breaker [%s] устранена (%s) — активных причин больше нет, "
                "circuit breaker СНЯТ, торговля возобновляется",
                cause, removed["reason"],
            )
        self._persist()
        return True

    def breaker_causes(self) -> Dict[str, dict]:
        return {key: dict(value) for key, value in self._breaker_causes.items()}

    @property
    def circuit_breaker_tripped(self) -> bool:
        return bool(self._breaker_causes)

    @property
    def circuit_breaker_sticky(self) -> bool:
        return any(c["sticky"] for c in self._breaker_causes.values())

    @property
    def _circuit_breaker_reason(self) -> str:
        return "; ".join(
            f"[{key}] {value['reason']}" for key, value in self._breaker_causes.items()
        )

    def manual_reset_circuit_breaker(self):
        """
        Сознательно ручной метод — Risk Manager НЕ снимает circuit breaker сам.
        Только человек, посмотрев, что произошло, может возобновить торговлю.
        """
        logger.warning(
            "Circuit breaker сброшен вручную оператором (были причины: %s)",
            self._circuit_breaker_reason or "нет",
        )
        self._breaker_causes.clear()
        self._persist()

    def restore_daily_pnl_from_journal(self, journal_pnl_usdt: float, closed_trades: int):
        """
        Сверка сохранённого состояния с журналом при старте.

        Журнал — источник правды по фактически закрытым сделкам, но он может не
        включать то, что уже учтено в состоянии (или наоборот). При расхождении
        берём БОЛЕЕ КОНСЕРВАТИВНОЕ, то есть меньшее (более убыточное) значение:
        занизить сегодняшний убыток опаснее, чем завысить.
        """
        state_pnl = self._daily_pnl_usdt
        if abs(state_pnl - journal_pnl_usdt) < 1e-9:
            logger.info(
                "Сверка дневного PnL: risk_state и trade_log совпадают (%.2f USDT, %d закрытых сделок)",
                state_pnl, closed_trades,
            )
            return

        conservative = min(state_pnl, journal_pnl_usdt)
        logger.warning(
            "РАСХОЖДЕНИЕ дневного PnL при старте: risk_state=%.2f USDT, trade_log=%.2f USDT "
            "(%d закрытых сделок за сегодня). Беру более консервативное значение %.2f USDT. "
            "Причина расхождения требует проверки: возможна потерянная запись состояния или "
            "не учтённое в состоянии закрытие.",
            state_pnl, journal_pnl_usdt, closed_trades, conservative,
        )
        self._daily_pnl_usdt = conservative
        self._persist()

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _daily_loss_limit_usdt(self) -> Optional[float]:
        if self._daily_start_balance is None:
            return None
        return self._daily_start_balance * (self.cfg.max_daily_loss_pct / 100)

    def _maybe_reset_daily_counters(self, current_balance: float):
        """
        Сброс происходит ТОЛЬКО при смене UTC-дня.

        Раньше условием было ещё и `_daily_start_balance is None`, что при
        каждом старте процесса выглядело как "новый день" и обнуляло дневной
        убыток и circuit breaker. Теперь отсутствие стартового баланса — это
        отдельный случай: он просто фиксируется, без сброса лимитов.
        """
        today = utc_today()
        if today != self._daily_reset_date:
            logger.info(
                "Новый торговый день (UTC %s -> %s) — фиксирую баланс на начало дня: %.2f USDT, "
                "дневные счётчики и circuit breaker сброшены",
                self._daily_reset_date, today, current_balance,
            )
            self._daily_pnl_usdt = 0.0
            self._daily_trade_count = 0
            self._symbol_trade_counts.clear()
            self._last_entry_ts_by_symbol.clear()
            self._daily_start_balance = current_balance
            self._daily_reset_date = today
            expired = [k for k, v in self._breaker_causes.items() if not v["sticky"]]
            for key in expired:
                del self._breaker_causes[key]
            if self._breaker_causes:
                # Причины не привязаны к суткам (например, orphaned-сделка с
                # неизвестным результатом) — новый день их не отменяет.
                logger.critical(
                    "Новый UTC-день, но circuit breaker остаётся взведённым: %s. "
                    "Причины требуют устранения, автоматически не снимаются.",
                    self._circuit_breaker_reason,
                )
            # pending_entries и blocked_symbols НЕ сбрасываются: неподтверждённый
            # ордер и неизвестный финансовый результат не перестают быть
            # проблемой из-за смены суток.
            self._persist()
            return

        if self._daily_start_balance is None:
            logger.info(
                "Стартовый баланс дня не был зафиксирован — фиксирую текущий: %.2f USDT "
                "(дневные лимиты и счётчики при этом НЕ сбрасываются)",
                current_balance,
            )
            self._daily_start_balance = current_balance
            self._persist()

    def _load_state(self):
        state = self._store.load()
        if not state:
            logger.info("risk_state: сохранённого состояния нет — первый запуск, начинаю с нуля")
            return

        saved_day = parse_utc_day(state.get("day_utc"))
        today = utc_today()

        self._pending_entries = dict(state.get("pending_entries") or {})
        self._blocked_symbols = dict(state.get("blocked_symbols") or {})

        if saved_day is None or saved_day != today:
            # Состояние от прошлого дня. Дневные значения не переносим, но
            # sticky-breaker переносим: он не привязан к суткам.
            sticky_causes = {
                key: value for key, value in _causes_from_state(state).items() if value["sticky"]
            }
            if sticky_causes:
                self._breaker_causes = sticky_causes
                logger.critical(
                    "Circuit breaker, требующий устранения причины, перенесён через смену суток: %s",
                    self._circuit_breaker_reason,
                )
            logger.info(
                "risk_state: сохранённое состояние за %s, сегодня %s — дневные счётчики "
                "начинаются заново. pending=%d, blocked=%d перенесены.",
                saved_day, today, len(self._pending_entries), len(self._blocked_symbols),
            )
            self._daily_reset_date = today
            self._daily_start_balance = None
            return

        self._daily_reset_date = saved_day
        self._daily_start_balance = state.get("daily_start_balance")
        self._daily_pnl_usdt = state.get("daily_pnl_usdt", 0.0)
        self._daily_trade_count = state.get("daily_trade_count", 0)
        self._symbol_trade_counts = dict(state.get("symbol_trade_counts") or {})
        self._last_entry_ts_by_symbol = dict(state.get("last_entry_ts_by_symbol") or {})
        self._breaker_causes = _causes_from_state(state)

        logger.warning(
            "risk_state ВОССТАНОВЛЕН после перезапуска (UTC-день %s): дневной PnL=%.2f USDT, "
            "сделок=%d, стартовый баланс=%s, circuit_breaker=%s, символов в cooldown=%d, "
            "неподтверждённых ордеров=%d, заблокированных символов=%d. "
            "Дневные лимиты продолжают действовать с этой точки, а не с нуля.",
            saved_day, self._daily_pnl_usdt, self._daily_trade_count,
            f"{self._daily_start_balance:.2f}" if self._daily_start_balance is not None else "не зафиксирован",
            "ВЗВЕДЁН" if self.circuit_breaker_tripped else "нет",
            len(self._last_entry_ts_by_symbol), len(self._pending_entries), len(self._blocked_symbols),
        )
        if self.circuit_breaker_tripped:
            logger.critical(
                "Circuit breaker остаётся взведённым после перезапуска: %s. "
                "Новых входов не будет, пока причины не устранены "
                "(python risk_admin.py status).",
                self._circuit_breaker_reason,
            )

    def _snapshot(self) -> dict:
        return {
            "day_utc": utc_day_str(self._daily_reset_date),
            "daily_start_balance": self._daily_start_balance,
            "daily_pnl_usdt": self._daily_pnl_usdt,
            "daily_trade_count": self._daily_trade_count,
            "symbol_trade_counts": dict(self._symbol_trade_counts),
            "last_entry_ts_by_symbol": dict(self._last_entry_ts_by_symbol),
            "pending_entries": dict(self._pending_entries),
            "blocked_symbols": dict(self._blocked_symbols),
            "circuit_breaker_causes": self.breaker_causes(),
            # Денормализованные поля — только для чтения человеком в psql.
            # Логика их обратно не читает, источник правды — causes.
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
            "circuit_breaker_reason": self._circuit_breaker_reason[:500],
            "circuit_breaker_sticky": self.circuit_breaker_sticky,
        }

    def _persist(self):
        if self._store is None:
            return
        self._store.save(self._snapshot())
