"""
Тесты привязки цен SL/TP к сетке тика инструмента.

Регрессия: цены округлялись жёстко до 4 знаков (`round(price, 4)`), без учёта
priceFilter.tickSize. Для BNBUSDT/BTCUSDT (tick 0.10), ETHUSDT (0.01) и
UNIUSDT (0.001) это давало цену, не попадающую на сетку тика — Bybit такую
цену не принимает, и позиция могла остаться без стоп-лосса. Для дешёвых монет
(1000PEPEUSDT, вход 0.00271) округление до 4 знаков дополнительно превращало
стоп 1.5% в 0.37%.

Сеть не задействована: instruments info подменяется через _lot_size_cache.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import BybitConfig
from execution.execution_engine import ExecutionEngine

# Реальные значения tickSize с Bybit testnet (проверены через get_instruments_info)
REAL_TICKS = {
    "BTCUSDT": 0.10,
    "BNBUSDT": 0.10,
    "ETHUSDT": 0.01,
    "UNIUSDT": 0.001,
    "XRPUSDT": 0.0001,
    "ADAUSDT": 0.0001,
    "DOTUSDT": 0.0001,
    "DOGEUSDT": 0.00001,
    "1000PEPEUSDT": 0.0000010,
}


def _execution(ticks=None) -> ExecutionEngine:
    execution = object.__new__(ExecutionEngine)
    execution.cfg = BybitConfig(api_key="x", api_secret="y")
    execution.cfg.trading_enabled = True
    execution.session = None  # сеть не нужна: кэш инструментов заполнен заранее
    execution._lot_size_cache = {
        symbol: {"qtyStep": 0.01, "minOrderQty": 0.01, "tickSize": tick}
        for symbol, tick in (ticks or REAL_TICKS).items()
    }
    return execution


def _is_on_tick(price: float, tick: float) -> bool:
    steps = price / tick
    return abs(round(steps) - steps) < 1e-6


class SnapToTickTest(unittest.TestCase):
    def test_snaps_to_grid_in_both_directions(self):
        snap = ExecutionEngine._snap_to_tick
        self.assertEqual(snap(564.7005, 0.10, round_down=True), 564.7)
        self.assertEqual(snap(564.7005, 0.10, round_down=False), 564.8)

    def test_no_float_artifact_on_small_ticks(self):
        """0.0027 / 0.000001 во float даёт 2700.0000000000005 -> ceil лишний тик."""
        snapped = ExecutionEngine._snap_to_tick(0.0027, 0.000001, round_down=False)
        self.assertEqual(snapped, 0.0027)

    def test_already_on_grid_is_unchanged(self):
        for round_down in (True, False):
            self.assertEqual(ExecutionEngine._snap_to_tick(100.0, 0.10, round_down), 100.0)
            self.assertEqual(ExecutionEngine._snap_to_tick(1735.76, 0.01, round_down), 1735.76)

    def test_zero_tick_falls_back_to_four_decimals(self):
        """Если биржа не отдала tickSize — не падаем, работаем как раньше."""
        self.assertEqual(ExecutionEngine._snap_to_tick(1.234567, 0, round_down=True), 1.2346)


class PriceWithOffsetTest(unittest.TestCase):
    def setUp(self):
        self.execution = _execution()

    def test_all_real_symbols_land_on_tick_grid(self):
        prices = {
            "BTCUSDT": 101234.56, "BNBUSDT": 573.3, "ETHUSDT": 1756.66,
            "UNIUSDT": 7.8345, "XRPUSDT": 1.1119, "ADAUSDT": 0.1688,
            "DOTUSDT": 0.8788, "DOGEUSDT": 0.0746, "1000PEPEUSDT": 0.00271,
        }
        for symbol, price in prices.items():
            tick = REAL_TICKS[symbol]
            for side in ("Buy", "Sell"):
                for is_sl, pct in ((True, 1.5), (False, 3.0)):
                    got = self.execution._price_with_offset(symbol, price, pct, side, is_sl)
                    self.assertTrue(
                        _is_on_tick(got, tick),
                        f"{symbol} {side} {'SL' if is_sl else 'TP'}={got} не кратна тику {tick}",
                    )

    def test_stop_distance_stays_close_to_requested_pct(self):
        """
        Ключевая регрессия: у 1000PEPEUSDT стоп 1.5% превращался в 0.37%,
        потому что round(0.00266935, 4) = 0.0027.
        """
        got = self.execution._price_with_offset("1000PEPEUSDT", 0.00271, 1.5, "Buy", True)
        actual_pct = (0.00271 - got) / 0.00271 * 100
        self.assertGreater(actual_pct, 1.3, f"стоп слишком близко: {actual_pct:.3f}%")
        self.assertLessEqual(actual_pct, 1.5, f"стоп дальше запрошенного: {actual_pct:.3f}%")

    def test_rounding_never_widens_risk(self):
        """
        Округление всегда играет в пользу безопасности: стоп не может оказаться
        ДАЛЬШЕ запрошенного (это молча увеличило бы риск сделки).
        """
        for symbol in REAL_TICKS:
            price = 100.0
            self.execution._lot_size_cache[symbol]["tickSize"] = REAL_TICKS[symbol]
            long_sl = self.execution._price_with_offset(symbol, price, 1.5, "Buy", True)
            self.assertGreaterEqual(long_sl, price * 0.985, f"{symbol}: LONG стоп ушёл ниже 1.5%")
            short_sl = self.execution._price_with_offset(symbol, price, 1.5, "Sell", True)
            self.assertLessEqual(short_sl, price * 1.015, f"{symbol}: SHORT стоп ушёл выше 1.5%")

    def test_take_profit_never_moves_further_away(self):
        """TP округляется к цене входа — фиксируем чуть раньше, а не позже."""
        for symbol in REAL_TICKS:
            price = 100.0
            long_tp = self.execution._price_with_offset(symbol, price, 3.0, "Buy", False)
            self.assertLessEqual(long_tp, price * 1.03, f"{symbol}: LONG TP уехал дальше 3%")
            short_tp = self.execution._price_with_offset(symbol, price, 3.0, "Sell", False)
            self.assertGreaterEqual(short_tp, price * 0.97, f"{symbol}: SHORT TP уехал дальше 3%")

    def test_sl_and_tp_on_correct_sides_of_entry(self):
        price = 573.3
        long_sl = self.execution._price_with_offset("BNBUSDT", price, 1.5, "Buy", True)
        long_tp = self.execution._price_with_offset("BNBUSDT", price, 3.0, "Buy", False)
        self.assertLess(long_sl, price)     # стоп лонга ниже входа
        self.assertGreater(long_tp, price)  # тейк лонга выше входа

        short_sl = self.execution._price_with_offset("BNBUSDT", price, 1.5, "Sell", True)
        short_tp = self.execution._price_with_offset("BNBUSDT", price, 3.0, "Sell", False)
        self.assertGreater(short_sl, price)  # стоп шорта выше входа
        self.assertLess(short_tp, price)     # тейк шорта ниже входа

    def test_missing_tick_size_does_not_crash(self):
        execution = _execution({"FOOUSDT": 0.0})
        got = execution._price_with_offset("FOOUSDT", 100.0, 1.5, "Buy", True)
        self.assertAlmostEqual(got, 98.5, places=4)


class CurrentOrderPriceTest(unittest.TestCase):
    def test_fresh_ticker_replaces_stale_candle_price(self):
        execution = _execution()

        class Session:
            @staticmethod
            def get_tickers(**_kwargs):
                return {"result": {"list": [{"markPrice": "0.8017"}]}}

        execution.session = Session()
        self.assertEqual(execution._current_order_price("DOTUSDT", 0.8225), 0.8017)

    def test_unknown_symbol_falls_back_without_network(self):
        """Инструмента нет в кэше и сессии нет — не падаем, а логируем и откатываемся."""
        execution = _execution()
        with self.assertLogs("execution.execution_engine", level="WARNING"):
            got = execution._price_with_offset("NEWUSDT", 100.0, 1.5, "Buy", True)
        self.assertAlmostEqual(got, 98.5, places=4)


class OrderParamsUseTickAwarePricesTest(unittest.TestCase):
    """Проверяем, что ордер реально уходит на биржу с tick-aware ценами."""

    class FakeSession:
        def __init__(self):
            self.last_params = None

        def set_leverage(self, **kwargs):
            return {"retCode": 0}

        def place_order(self, **kwargs):
            self.last_params = kwargs
            return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "x"}}

    def test_bnb_order_carries_tick_aligned_sl_tp(self):
        from strategy.signal import Action

        execution = _execution()
        execution.session = self.FakeSession()
        execution._lot_size_cache["BNBUSDT"].update({"qtyStep": 0.01, "minOrderQty": 0.01})

        execution.open_position(
            "BNBUSDT", Action.OPEN_SHORT, size_usdt=100, leverage=1,
            last_price=573.3, stop_loss_pct=1.5, take_profit_pct=3.0, source="test",
        )
        params = execution.session.last_params
        sl, tp = float(params["stopLoss"]), float(params["takeProfit"])
        self.assertTrue(_is_on_tick(sl, 0.10), f"stopLoss={sl} не кратен тику 0.10")
        self.assertTrue(_is_on_tick(tp, 0.10), f"takeProfit={tp} не кратен тику 0.10")
        # SHORT: стоп выше входа, тейк ниже
        self.assertGreater(sl, 573.3)
        self.assertLess(tp, 573.3)


class TrailingStopTickTest(unittest.TestCase):
    class FakeSession:
        def __init__(self):
            self.last = None

        def set_trading_stop(self, **kwargs):
            self.last = kwargs
            return {"retCode": 0}

    def test_trailing_distance_is_tick_aligned(self):
        execution = _execution()
        execution.session = self.FakeSession()
        execution.set_trailing_stop("BNBUSDT", last_price=573.3, distance_pct=0.8)
        distance = float(execution.session.last["trailingStop"])
        self.assertTrue(_is_on_tick(distance, 0.10), f"trailingStop={distance} не кратен тику")
        self.assertGreater(distance, 0, "нулевое расстояние отключило бы trailing stop")

    def test_tiny_distance_does_not_round_to_zero(self):
        """Расстояние меньше тика должно стать одним тиком, а не нулём."""
        execution = _execution()
        execution.session = self.FakeSession()
        execution.set_trailing_stop("BNBUSDT", last_price=1.0, distance_pct=0.01)
        distance = float(execution.session.last["trailingStop"])
        self.assertEqual(distance, 0.10)


if __name__ == "__main__":
    unittest.main()
