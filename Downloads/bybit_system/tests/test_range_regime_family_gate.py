"""
Тесты гейта "якорного семейства" для RANGE-режима в DecisionEngine.

Контекст: meta_strategy.py декларирует RSI и funding приоритетными для RANGE
(RANGE_EXPERTS содержит их, комментарий "приоритет RSI, VWAP, Funding и
mean-reversion логики"), но DecisionEngine.min_confirming_families=2 раньше
не проверял, КАКИЕ именно семейства подтвердили сигнал -- только их
количество. На 59 реальных testnet-сделках это дало 10 из 10 (100%) решений
в RANGE-режиме, подтверждённых исключительно vwap+rule:committee
(price_location + multi_indicator), ни разу не спросив RSI/funding.
Win rate 20%, net -10.91 USDT -- 53% всего убытка тестового прогона.

Фикс: для RANGE регistра требуется, чтобы confirmation_families включало
хотя бы одно из {mean_reversion, positioning} (RSI или funding). Другие
режимы (TREND -- единственный прибыльный в данных, BREAKOUT, REVERSAL,
неизвестный) этим гейтом не затрагиваются вообще.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decision_engine import DecisionEngine
from market_context import MarketContext
from meta_strategy import MetaStrategyDecision
from strategy.signal import Action, Signal


def _ctx(regime: str, trend: str = "NEUTRAL") -> MarketContext:
    return MarketContext(
        symbol="ETHUSDT", regime=regime, trend=trend,
        liquidity="GOOD", confidence=0.75, risk_score=0.20,
    )


def _meta(*sources: str) -> MetaStrategyDecision:
    return MetaStrategyDecision(allowed_sources=set(sources))


VWAP_ONLY = [
    Signal("ETHUSDT", Action.OPEN_LONG, "rule:committee", 0.75, "committee сигнал",
           stop_loss_pct=1.5, take_profit_pct=3.2),
    Signal("ETHUSDT", Action.OPEN_LONG, "expert:vwap", 0.66, "цена выше VWAP",
           stop_loss_pct=1.4, take_profit_pct=3.0),
]


class RangeRegimeFamilyGateTest(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine(min_confirming_families=2, min_rr=2.0)

    def test_vwap_plus_committee_alone_is_blocked_in_range(self):
        """Ровно сценарий, найденный в реальных данных: 10/10 проигрышных RANGE-сделок."""
        report = self.engine.decide(
            "ETHUSDT", _ctx("RANGE"),
            _meta("rule:committee", "expert:vwap"),
            VWAP_ONLY,
        )
        self.assertEqual(report.final_signal.action, Action.HOLD)
        rejection = report.rejected_actions["open_long"]
        self.assertIn("RANGE", rejection)
        self.assertIn("mean_reversion", rejection)
        self.assertIn("positioning", rejection)
        # Сообщение также честно называет, что реально подтвердилось
        # (multi_indicator, price_location -- ровно committee+vwap), а не
        # просто "недостаточно", как в проверке количества семейств выше.
        self.assertIn("multi_indicator", rejection)
        self.assertIn("price_location", rejection)

    def test_same_combo_opens_fine_in_trend_regime(self):
        """Тот же набор сигналов, но в TREND (единственный прибыльный режим в данных) -- не блокируется."""
        report = self.engine.decide(
            "ETHUSDT", _ctx("TREND", trend="UP"),
            _meta("rule:committee", "expert:vwap"),
            VWAP_ONLY,
        )
        self.assertEqual(report.final_signal.action, Action.OPEN_LONG)

    def test_rsi_confirmation_allows_range_trade(self):
        """RSI (mean_reversion) в подтверждениях -- гейт снят, RANGE открывает сделку."""
        signals = VWAP_ONLY + [
            Signal("ETHUSDT", Action.OPEN_LONG, "expert:rsi", 0.60, "RSI восстановление",
                   stop_loss_pct=1.3, take_profit_pct=2.8),
        ]
        report = self.engine.decide("ETHUSDT", _ctx("RANGE"), _meta("rule:committee", "expert:vwap", "expert:rsi"), signals)
        self.assertIn("mean_reversion", report.confirmation_families)
        self.assertEqual(report.final_signal.action, Action.OPEN_LONG)

    def test_funding_confirmation_allows_range_trade(self):
        """funding (positioning) тоже засчитывается как якорь для RANGE."""
        signals = VWAP_ONLY + [
            Signal("ETHUSDT", Action.OPEN_LONG, "expert:funding", 0.60, "funding перекос",
                   stop_loss_pct=1.4, take_profit_pct=2.8),
        ]
        report = self.engine.decide(
            "ETHUSDT", _ctx("RANGE"), _meta("rule:committee", "expert:vwap", "expert:funding"), signals,
        )
        self.assertIn("positioning", report.confirmation_families)
        self.assertEqual(report.final_signal.action, Action.OPEN_LONG)

    def test_breakout_and_reversal_regimes_are_not_affected(self):
        """Гейт специфичен для RANGE -- остальные режимы им не затрагиваются."""
        for regime in ("BREAKOUT", "REVERSAL", "UNKNOWN"):
            with self.subTest(regime=regime):
                report = self.engine.decide(
                    "ETHUSDT", _ctx(regime), _meta("rule:committee", "expert:vwap"), VWAP_ONLY,
                )
                self.assertEqual(report.final_signal.action, Action.OPEN_LONG)

    def test_family_count_check_still_fires_before_new_gate(self):
        """
        Один сигнал (1 семейство) в RANGE должен упасть на существующей проверке
        количества, а не на новой -- сообщение должно называть недостаток
        подтверждений, а не отсутствие RSI/funding.
        """
        single = [VWAP_ONLY[0]]  # только rule:committee
        report = self.engine.decide("ETHUSDT", _ctx("RANGE"), _meta("rule:committee"), single)
        self.assertEqual(report.final_signal.action, Action.HOLD)
        self.assertIn("Недостаточно независимых подтверждений", report.rejected_actions["open_long"])

    def test_short_direction_also_gated_in_range(self):
        """Гейт симметричен -- проверяем не только LONG, но и SHORT."""
        signals = [
            Signal("ETHUSDT", Action.OPEN_SHORT, "rule:committee", 0.75, "committee шорт",
                   stop_loss_pct=1.5, take_profit_pct=3.2),
            Signal("ETHUSDT", Action.OPEN_SHORT, "expert:vwap", 0.66, "цена ниже VWAP",
                   stop_loss_pct=1.4, take_profit_pct=3.0),
        ]
        report = self.engine.decide("ETHUSDT", _ctx("RANGE"), _meta("rule:committee", "expert:vwap"), signals)
        self.assertEqual(report.final_signal.action, Action.HOLD)
        self.assertIn("open_short", report.rejected_actions)

    def test_no_regime_configured_is_never_gated(self):
        """Режимы без записи в _REGIME_REQUIRES_ANY_FAMILY не проверяются вовсе."""
        self.assertNotIn("TREND", DecisionEngine._REGIME_REQUIRES_ANY_FAMILY)
        self.assertIn("RANGE", DecisionEngine._REGIME_REQUIRES_ANY_FAMILY)


if __name__ == "__main__":
    unittest.main()
