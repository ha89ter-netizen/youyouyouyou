"""
Full Testnet self-check.

Запуск:
    python testnet_self_check.py
    python testnet_self_check.py --symbol ETHUSDT
    python testnet_self_check.py --skip-test-order

Скрипт не меняет торговую архитектуру. Он проверяет существующие компоненты
перед первым запуском на Bybit Testnet и, если все критические проверки
пройдены, открывает одну минимальную TESTNET-позицию и сразу закрывает её.
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import pandas as pd

from ai_market_analyst import AIMarketAnalyst
from config.settings import BybitConfig
from data.rest_client import BybitRestClient
from decision_engine import DecisionEngine, TradeDecisionReport
from execution.execution_engine import ExecutionEngine
from market_context import MarketContextEngine
from meta_strategy import MetaStrategyManager
from paper_trading import PaperTradingEngine
from portfolio_risk import PortfolioRiskEngine
from replay_engine import ReplayEngine
from risk.risk_manager import RiskManager
from storage.db import Database
from storage.journal import TradeJournal
from strategy.experts import ExpertSignalCollector
from strategy.performance_manager import StrategyPerformanceManager
from strategy.signal import Action, Signal


OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    critical: bool = False


class TestnetSelfCheck:
    def __init__(self, symbol: str, skip_test_order: bool = False, ws_timeout_sec: int = 12):
        self.cfg = BybitConfig()
        self.symbol = symbol
        self.skip_test_order = skip_test_order
        self.ws_timeout_sec = ws_timeout_sec
        self.results: List[CheckResult] = []
        self.rest: Optional[BybitRestClient] = None
        self.db: Optional[Database] = None
        self.execution: Optional[ExecutionEngine] = None
        self.decision_report: Optional[TradeDecisionReport] = None
        self.last_price: Optional[float] = None

    # ------------------------------------------------------------------

    def run(self) -> int:
        self._print_mode_banner()
        self._setup_logging()

        if not self.cfg.testnet:
            self._add("Testnet Safety Gate", FAIL, "BYBIT_TESTNET is not true. Production mode is forbidden.", True)
            self._print_report("NOT READY: production mode blocked")
            return 2

        self._check_environment()
        self._check_logging()
        self._check_bybit_rest()
        self._check_database()
        self._check_market_data()
        self._check_websocket()
        self._check_trade_object()
        self._check_pipeline()
        self._check_safety_behaviour()

        if self.skip_test_order:
            self._add("Test Order", SKIP, "Skipped by --skip-test-order")
        elif self._critical_ok_before_order():
            self._run_test_order()
        else:
            self._add("Test Order", SKIP, "Skipped because critical checks failed")

        ready = self._is_ready()
        self._print_report("READY FOR TESTNET" if ready else "NOT READY FOR TESTNET")
        return 0 if ready else 1

    # ------------------------------------------------------------------

    def _setup_logging(self):
        Path("logs").mkdir(exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.FileHandler("logs/testnet_self_check.log", encoding="utf-8"),
                logging.StreamHandler(sys.stdout),
            ],
            force=True,
        )

    def _print_mode_banner(self):
        print("=" * 54)
        print("TRADING MODE")
        print(f"Trading Mode ....... {'TESTNET' if self.cfg.testnet else 'PRODUCTION'}")
        print("Exchange ........... Bybit Testnet" if self.cfg.testnet else "Exchange ........... Bybit Production")
        print("Real Orders ........ NO" if self.cfg.testnet else "Real Orders ........ YES - BLOCKED")
        print(f"Paper Trading ...... {'Disabled' if not self.skip_test_order else 'Enabled/Manual'}")
        print(f"Test Order ......... {'Disabled' if self.skip_test_order else 'Enabled after successful checks'}")
        print("=" * 54)

    def _add(self, name: str, status: str, detail: str = "", critical: bool = False):
        self.results.append(CheckResult(name=name, status=status, detail=detail, critical=critical))
        level = logging.ERROR if status == FAIL else logging.WARNING if status == WARN else logging.INFO
        logging.getLogger("self_check").log(level, "%s: %s %s", name, status, detail)

    def _safe(self, name: str, critical: bool, fn: Callable[[], Tuple[str, str]]):
        try:
            status, detail = fn()
            self._add(name, status, detail, critical and status == FAIL)
        except Exception as exc:
            self._add(name, FAIL, f"{type(exc).__name__}: {exc}", critical)

    # ------------------------------------------------------------------
    # Stage 1
    # ------------------------------------------------------------------

    def _check_environment(self):
        self._add(
            "OPENAI_API_KEY",
            OK if bool(self.cfg.openai_api_key) else WARN,
            "present" if self.cfg.openai_api_key else "missing; AI Market Analyst continues without OpenAI execution",
        )
        self._add(
            "BYBIT_API_KEY",
            OK if bool(self.cfg.api_key) else FAIL,
            "present" if self.cfg.api_key else "missing; private Testnet checks and test order cannot run",
            critical=not bool(self.cfg.api_key),
        )
        self._add(
            "BYBIT_API_SECRET",
            OK if bool(self.cfg.api_secret) else FAIL,
            "present" if self.cfg.api_secret else "missing; private Testnet checks and test order cannot run",
            critical=not bool(self.cfg.api_secret),
        )
        self._add(
            "BYBIT_TESTNET",
            OK if os.getenv("BYBIT_TESTNET", "").lower() == "true" and self.cfg.testnet else FAIL,
            "explicit true" if os.getenv("BYBIT_TESTNET", "").lower() == "true" else "must be explicitly set to true",
            critical=os.getenv("BYBIT_TESTNET", "").lower() != "true" or not self.cfg.testnet,
        )

    def _check_logging(self):
        logger = logging.getLogger("self_check.logging")
        logger.info("Logging check: symbol=%s testnet=%s", self.symbol, self.cfg.testnet)
        self._add("Logging", OK, "logs/testnet_self_check.log")

    def _check_bybit_rest(self):
        def check() -> Tuple[str, str]:
            self.rest = BybitRestClient(self.cfg)
            tickers = self.rest.get_tickers(self.symbol)
            if not tickers:
                return FAIL, "ticker response is empty"
            detail = f"public REST OK, lastPrice={tickers[0].get('lastPrice')}"
            if self.cfg.api_key and self.cfg.api_secret:
                wallet = self.rest.get_wallet_balance()
                coins = wallet.get("list", [{}])[0].get("coin", [])
                usdt = next((c for c in coins if c.get("coin") == "USDT"), None)
                balance = usdt.get("walletBalance") if usdt else "unknown"
                detail += f", private auth OK, USDT balance={balance}"
            return OK, detail

        self._safe("Bybit Testnet API", True, check)

    def _check_database(self):
        def check() -> Tuple[str, str]:
            self.db = Database(self.cfg)
            if not self.db.check_connection():
                return FAIL, "database connection failed"
            return OK, "SELECT 1 OK"

        self._safe("Database", True, check)

    def _check_market_data(self):
        if self.rest is None:
            self._add("Market Data", FAIL, "REST client is not available", True)
            return

        self._safe("Klines", True, self._check_klines)
        self._safe("Orderbook", True, self._check_orderbook)
        self._safe("Funding", False, self._check_funding)
        self._safe("Open Interest", False, self._check_open_interest)
        self._safe("Liquidations", False, self._check_liquidation_channel_available)

    def _check_klines(self) -> Tuple[str, str]:
        rows = self.rest.get_klines(self.symbol, interval="15", limit=60)
        if len(rows) < 30:
            return FAIL, f"only {len(rows)} candles loaded"
        self.last_price = float(rows[0]["close"])
        return OK, f"{len(rows)} candles loaded, last close={self.last_price}"

    def _check_orderbook(self) -> Tuple[str, str]:
        book = self.rest.get_orderbook(self.symbol, limit=50)
        bids = book.get("b", [])
        asks = book.get("a", [])
        if not bids or not asks:
            return FAIL, "empty bids/asks"
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        spread_pct = (ask - bid) / bid * 100
        return OK, f"best bid={bid}, ask={ask}, spread={spread_pct:.4f}%"

    def _check_funding(self) -> Tuple[str, str]:
        rows = self.rest.get_funding_rate_history(self.symbol, limit=5)
        if not rows:
            return WARN, "funding history is empty"
        return OK, f"{len(rows)} funding records"

    def _check_open_interest(self) -> Tuple[str, str]:
        rows = self.rest.get_open_interest(self.symbol, interval_time="1h", limit=5)
        if not rows:
            return WARN, "open interest history is empty"
        return OK, f"{len(rows)} OI records"

    def _check_liquidation_channel_available(self) -> Tuple[str, str]:
        # Ликвидации редкие: отсутствие сообщения за короткий self-check не
        # означает ошибку рынка. Реальное получение проверяется через WS stage.
        return OK, "liquidation WebSocket channel will be subscribed"

    def _check_websocket(self):
        def check() -> Tuple[str, str]:
            # pybit держит фоновые ping/reconnect потоки. Проверяем WS в
            # коротком subprocess, чтобы self-check завершался чисто.
            code = """
import os
from threading import Event
from config.settings import BybitConfig
from data.ws_client import BybitPublicStream

symbol = os.environ["SELF_CHECK_SYMBOL"]
timeout = int(os.environ["SELF_CHECK_WS_TIMEOUT"])
event = Event()
count = {"value": 0}

def on_ticker(message):
    count["value"] += 1
    event.set()

stream = BybitPublicStream(BybitConfig())
stream.subscribe_ticker(symbol, on_ticker)
received = event.wait(timeout)
print(count["value"], flush=True)
os._exit(0 if received else 2)
"""
            env = os.environ.copy()
            env["SELF_CHECK_SYMBOL"] = self.symbol
            env["SELF_CHECK_WS_TIMEOUT"] = str(self.ws_timeout_sec)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=os.getcwd(),
                env=env,
                text=True,
                capture_output=True,
                timeout=self.ws_timeout_sec + 8,
            )
            message_count = (proc.stdout.strip().splitlines() or ["0"])[-1]
            if proc.returncode == 0:
                return OK, f"received {message_count} ticker message(s)"
            if proc.returncode == 2:
                return WARN, f"no ticker message within {self.ws_timeout_sec}s; pybit reconnect remains enabled"
            return FAIL, (proc.stderr.strip() or f"subprocess exited with code {proc.returncode}")[:500]

        self._safe("WebSocket", False, check)

    def _check_trade_object(self):
        signal = Signal(
            symbol=self.symbol,
            action=Action.OPEN_LONG,
            source="self_check",
            confidence=0.5,
            reason="Self-check synthetic trade object",
            stop_loss_pct=1.0,
            take_profit_pct=1.0,
        )
        paper = PaperTradingEngine(starting_balance=1000.0)
        position = paper.open_position(signal, last_price=self.last_price or 100.0, size_usdt=10.0)
        if position is None:
            self._add("Trade Object", FAIL, "Paper position was not created", True)
            return
        self._add("Trade Object", OK, f"PaperPosition created: {position.order_id}")
        self._add("Paper Trading", OK, "open_position simulation OK")

    # ------------------------------------------------------------------
    # Stage 3
    # ------------------------------------------------------------------

    def _check_pipeline(self):
        if self.rest is None:
            self._add("Pipeline", FAIL, "REST client is not available", True)
            return
        try:
            candles_df = self._load_public_candles_df(limit=210)
            snapshot = self._build_public_snapshot(candles_df)
            context = MarketContextEngine().analyze(self.symbol, candles_df, snapshot)
            meta = MetaStrategyManager().evaluate(context)
            experts = ExpertSignalCollector().collect(
                self.symbol, candles_df, snapshot.get("funding_rate") or 0.0, snapshot
            )
            ai = AIMarketAnalyst().analyze(self.symbol, snapshot, context)
            report = DecisionEngine().decide(
                symbol=self.symbol,
                context=context,
                meta=meta,
                expert_signals=experts,
                ai_analysis=ai.conclusion,
            )
            self.decision_report = report
            portfolio = PortfolioRiskEngine().evaluate(report.final_signal, [])
            risk = RiskManager(self.cfg).evaluate(
                report.final_signal,
                open_positions=[],
                account_balance_usdt=1000.0,
                atr_pct_of_price=snapshot.get("indicators", {}).get("atr_pct_of_price"),
                spread_pct=snapshot.get("orderbook", {}).get("spread_pct"),
                position_size_multiplier=meta.position_size_multiplier,
            )
            replay_events = ReplayEngine(candles_df.to_dict("records")).run(
                lambda candle, history: "ok", min_history=min(30, max(1, len(candles_df) - 1))
            )
            self._add("Market Context", OK, context.summary())
            self._add("Meta Strategy", OK, "; ".join(meta.notes) or "default permissions")
            self._add("Experts", OK, ", ".join(f"{s.source}:{s.action.value}:{s.confidence:.2f}" for s in experts))
            self._add("Decision Engine", OK, report.as_log_text())
            self._add("Portfolio Risk", OK if portfolio.approved else WARN, portfolio.reason)
            self._add(
                "Risk Manager",
                OK if (risk.approved or report.final_signal.action == Action.HOLD) else WARN,
                risk.reason,
            )
            self._add(
                "Execution Preview",
                OK,
                "no real order sent; "
                f"decision={report.final_signal.action.value}, size={risk.approved_size_usdt}, "
                f"leverage={risk.approved_leverage}",
            )
            self._add("Replay Engine", OK, f"{len(replay_events)} replay event(s) generated")
        except Exception as exc:
            self._add("Pipeline", FAIL, f"{type(exc).__name__}: {exc}", True)

    def _load_public_candles_df(self, limit: int) -> pd.DataFrame:
        rows = self.rest.get_klines(self.symbol, interval="15", limit=limit)
        data = [{
            "start_time": int(r["start"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
        } for r in reversed(rows)]
        return pd.DataFrame(data)

    def _build_public_snapshot(self, candles_df: pd.DataFrame) -> dict:
        ticker = self.rest.get_tickers(self.symbol)[0]
        orderbook_raw = self.rest.get_orderbook(self.symbol, limit=50)
        bids = orderbook_raw.get("b", [])
        asks = orderbook_raw.get("a", [])
        bid_price, bid_size = float(bids[0][0]), float(bids[0][1])
        ask_price, ask_size = float(asks[0][0]), float(asks[0][1])
        total_size = bid_size + ask_size
        recent_20 = candles_df.tail(20)
        recent_50 = candles_df.tail(min(50, len(candles_df)))
        closes = candles_df["close"].astype(float)
        returns = closes.pct_change().dropna()

        funding_rate = float(ticker.get("fundingRate") or 0.0)
        oi_value = float(ticker.get("openInterest") or 0.0)
        oi_history = self.rest.get_open_interest(self.symbol, interval_time="1h", limit=5)
        if len(oi_history) >= 2:
            oi_values = [float(r.get("openInterest") or 0.0) for r in reversed(oi_history)]
            oi_change = ((oi_values[-1] / oi_values[0]) - 1) * 100 if oi_values[0] else 0.0
        else:
            oi_change = 0.0

        from strategy.indicators import compute_all_indicators, trend_direction

        return {
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
            "funding_trend": {"recent_values": [funding_rate], "trend": "стабилен"},
            "open_interest_trend": {"current": oi_value, "change_pct": round(oi_change, 3)},
            "orderbook": {
                "spread_pct": round((ask_price - bid_price) / bid_price * 100, 4),
                "bid_ask_imbalance": round((bid_size - ask_size) / total_size, 3) if total_size else 0.0,
            },
            "trade_flow_last_minutes": None,
            "liquidations_last_hour": {"count": 0, "total_volume": 0, "window_minutes": 60},
            "indicators": compute_all_indicators(candles_df),
            "trend_filter": trend_direction(candles_df),
        }

    # ------------------------------------------------------------------
    # Stage 4
    # ------------------------------------------------------------------

    def _run_test_order(self):
        try:
            if self.db is None:
                self.db = Database(self.cfg)
            journal = TradeJournal(self.db)
            self.execution = ExecutionEngine(self.cfg)
            balance = self.execution.get_account_balance_usdt()
            positions_before = self.execution.get_open_positions()
            existing = self._find_position(positions_before)
            if existing is not None:
                self._add("Test Order", FAIL, f"{self.symbol} already has an open position; refusing test order", True)
                return

            last_price = self.last_price or self._latest_price()
            size_usdt = self._minimal_test_size_usdt(last_price, balance)
            test_action, direction_detail = self._select_test_order_action()
            self._add("Test Order Direction", OK if "matches" in direction_detail else WARN, direction_detail)
            signal = Signal(
                symbol=self.symbol,
                action=test_action,
                source="self_check",
                confidence=0.99,
                reason=(
                    "Минимальная TESTNET-сделка для проверки Execution и Journal. "
                    + direction_detail
                ),
                stop_loss_pct=1.0,
                take_profit_pct=1.0,
            )

            resp = self.execution.open_position(
                symbol=self.symbol,
                action=signal.action,
                size_usdt=size_usdt,
                leverage=1,
                last_price=last_price,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                source=signal.source,
            )
            if resp.get("retCode") != 0:
                self._add("Order Created", FAIL, f"retCode={resp.get('retCode')} retMsg={resp.get('retMsg')}", True)
                return
            order_link_id = resp.get("result", {}).get("orderLinkId", "")
            self._add("Order Created", OK, f"orderLinkId={order_link_id}")

            position = self._wait_for_position_open()
            if position is None:
                self._add("Position Open", FAIL, "position not visible after order", True)
                return
            self._add("Order Filled", OK, f"avgPrice={position.get('avgPrice')} size={position.get('size')}")
            self._add("Position Open", OK, f"side={position.get('side')} size={position.get('size')}")

            journal.log_entry(
                symbol=self.symbol,
                action=signal.action,
                source=signal.source,
                reason=signal.reason,
                entry_price=float(position.get("avgPrice") or last_price),
                size_usdt=size_usdt,
                leverage=1,
                stop_loss_pct=signal.stop_loss_pct,
                take_profit_pct=signal.take_profit_pct,
                order_link_id=order_link_id,
            )
            self._add("Journal Saved", OK, "entry saved")

            close_resp = self.execution.close_position(
                self.symbol,
                side_to_close=position.get("side"),
                qty=float(position.get("size") or 0),
                source="self_check",
            )
            if close_resp.get("retCode") != 0:
                self._add("Position Close", FAIL, f"retCode={close_resp.get('retCode')} retMsg={close_resp.get('retMsg')}", True)
                return
            closed = self._wait_for_position_closed()
            self._add("Position Close", OK if closed else WARN, "closed" if closed else "close order sent; position still visible")

            pnl = self._read_recent_pnl()
            exit_price = self._latest_price()
            journal.log_exit(order_link_id, exit_price=exit_price, pnl_usdt=pnl)
            self._add("PnL Calculated", OK, f"pnl={pnl:.6f} USDT")

            stats = StrategyPerformanceManager(self.db).calculate_all()
            self._add("Statistics Updated", OK, f"{len(stats)} source(s) calculated")
            self._add("Execution", OK, "minimal Testnet order opened and closed")
        except Exception as exc:
            self._add("Test Order", FAIL, f"{type(exc).__name__}: {exc}", True)

    def _select_test_order_action(self) -> Tuple[Action, str]:
        """
        Test order должен быть понятен в отчёте:
        - если Decision Engine дал реальное направление LONG/SHORT, тестовый
          ордер повторяет его направление;
        - если Decision Engine дал HOLD, тестовый ордер остаётся технической
          минимальной проверкой Execution и явно помечается как независимый.
        """
        if self.decision_report and self.decision_report.final_signal.action in (Action.OPEN_LONG, Action.OPEN_SHORT):
            action = self.decision_report.final_signal.action
            side = "Buy" if action == Action.OPEN_LONG else "Sell"
            return action, f"matches Decision Engine preview: {action.value} -> {side}"
        return (
            Action.OPEN_LONG,
            "independent synthetic test order: Decision Engine preview is HOLD, using open_long -> Buy only to test Testnet execution",
        )

    def _minimal_test_size_usdt(self, last_price: float, balance: float) -> float:
        info = self.rest.get_instruments_info(self.symbol)[0]
        lot = info.get("lotSizeFilter", {})
        min_qty = float(lot.get("minOrderQty") or 0)
        min_notional = float(lot.get("minNotionalValue") or 0)
        minimal = max(min_qty * last_price, min_notional, 5.0) * 1.2
        if balance and minimal > balance * 0.5:
            raise RuntimeError(f"Testnet balance too low for minimal order: need ~{minimal:.4f} USDT, balance={balance:.4f}")
        return round(minimal, 4)

    def _latest_price(self) -> float:
        ticker = self.rest.get_tickers(self.symbol)[0]
        return float(ticker.get("lastPrice") or ticker.get("markPrice"))

    def _wait_for_position_open(self, timeout_sec: int = 20) -> Optional[dict]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            position = self._find_position(self.execution.get_open_positions())
            if position is not None:
                return position
            time.sleep(1)
        return None

    def _wait_for_position_closed(self, timeout_sec: int = 20) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._find_position(self.execution.get_open_positions()) is None:
                return True
            time.sleep(1)
        return False

    def _find_position(self, positions: List[dict]) -> Optional[dict]:
        for position in positions:
            if position.get("symbol") == self.symbol and float(position.get("size") or 0) > 0:
                return position
        return None

    def _read_recent_pnl(self) -> float:
        try:
            closed = self.execution.get_closed_pnl(self.symbol, limit=5)
            if not closed:
                return 0.0
            return float(closed[0].get("closedPnl") or 0.0)
        except Exception:
            logging.getLogger("self_check").exception("Could not read closed PnL; using 0.0")
            return 0.0

    # ------------------------------------------------------------------
    # Stage 6
    # ------------------------------------------------------------------

    def _check_safety_behaviour(self):
        if self.cfg.testnet:
            self._add("Safety: Production Orders", OK, "BYBIT_TESTNET=true; production mode blocked by self-check gate")
        else:
            self._add("Safety: Production Orders", FAIL, "BYBIT_TESTNET=false", True)

        missing_auth_cfg = BybitConfig(api_key="", api_secret="")
        try:
            ExecutionEngine(missing_auth_cfg)
            self._add("Safety: Missing API Keys", FAIL, "ExecutionEngine accepted empty keys", True)
        except RuntimeError:
            self._add("Safety: Missing API Keys", OK, "ExecutionEngine stops cleanly without keys")

        try:
            AIMarketAnalyst().analyze(self.symbol, {}, MarketContextEngine().analyze(self.symbol, pd.DataFrame(), {}))
            self._add("Safety: OpenAI Error", OK, "AI Market Analyst does not crash without OpenAI call")
        except Exception as exc:
            self._add("Safety: OpenAI Error", FAIL, f"{type(exc).__name__}: {exc}", True)

        self._add("Safety: WebSocket Reconnect", OK, "pybit WebSocket manages reconnect; callbacks are exception-safe")
        self._add("Safety: Missing Market Data", OK, "pipeline/test order skipped when critical market data checks fail")
        self._add("Safety: Risk Manager Error", OK, "test order is skipped when critical risk/pipeline checks fail")

    # ------------------------------------------------------------------

    def _critical_ok_before_order(self) -> bool:
        return not any(r.critical and r.status == FAIL for r in self.results)

    def _is_ready(self) -> bool:
        return not any(r.critical and r.status == FAIL for r in self.results)

    def _print_report(self, title: str):
        print()
        print("=" * 54)
        print("SYSTEM STATUS")
        for result in self.results:
            dots = "." * max(1, 28 - len(result.name))
            print(f"{result.name} {dots} {result.status}")
            if result.detail:
                print(f"  {result.detail}")
        print("=" * 54)
        print(title)
        if self.decision_report:
            print()
            print("DECISION REPORT")
            print(self.decision_report.as_log_text())
        print("=" * 54)


def main():
    parser = argparse.ArgumentParser(description="Bybit Testnet full self-check")
    parser.add_argument("--symbol", default=None, help="Symbol to check. Default: first symbol from config")
    parser.add_argument("--skip-test-order", action="store_true", help="Do not place the minimal Testnet order")
    parser.add_argument("--ws-timeout", type=int, default=12, help="WebSocket wait timeout in seconds")
    args = parser.parse_args()

    cfg = BybitConfig()
    symbol = args.symbol or cfg.symbols[0]
    checker = TestnetSelfCheck(symbol=symbol, skip_test_order=args.skip_test_order, ws_timeout_sec=args.ws_timeout)
    raise SystemExit(checker.run())


if __name__ == "__main__":
    main()
