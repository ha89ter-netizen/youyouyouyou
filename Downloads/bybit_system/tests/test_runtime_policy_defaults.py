import os
import unittest
from unittest.mock import patch

from config.settings import BybitConfig


class RuntimePolicyDefaultsTest(unittest.TestCase):
    def test_confirmed_symbol_and_capacity_defaults(self):
        names = {
            "SYMBOLS", "MAX_OPEN_POSITIONS", "MAX_DAILY_TRADES",
            "TIME_RANGE_TIGHTENING_AFTER_SECONDS", "TIME_RANGE_TIGHTENING_FACTOR",
            "TIME_RANGE_SECOND_TIGHTENING_AFTER_SECONDS",
            "TIME_RANGE_SECOND_TIGHTENING_FACTOR",
            "FUNDING_RAW_RETENTION_HOURS", "OPEN_INTEREST_RAW_RETENTION_HOURS",
            "STORAGE_ENTRY_BLOCK_RATIO",
        }
        clean_environment = {key: value for key, value in os.environ.items() if key not in names}
        with patch.dict(os.environ, clean_environment, clear=True):
            cfg = BybitConfig()

        self.assertEqual(len(cfg.symbols), 26)
        self.assertEqual(len(set(cfg.symbols)), 26)
        self.assertIn("BTCUSDT", cfg.symbols)
        self.assertNotIn("AVAXUSDT", cfg.symbols)
        self.assertNotIn("TONUSDT", cfg.symbols)
        self.assertNotIn("LTCUSDT", cfg.symbols)
        self.assertNotIn("ATOMUSDT", cfg.symbols)
        self.assertEqual(cfg.max_open_positions, 10)
        self.assertEqual(cfg.max_daily_trades, 200)
        self.assertEqual(cfg.time_range_tightening_after_seconds, 3600)
        self.assertEqual(cfg.time_range_tightening_factor, 0.5)
        self.assertEqual(cfg.time_range_second_tightening_after_seconds, 18000)
        self.assertEqual(cfg.time_range_second_tightening_factor, 0.5)
        self.assertEqual(cfg.funding_raw_retention_hours, 6)
        self.assertEqual(cfg.open_interest_raw_retention_hours, 6)
        self.assertEqual(cfg.storage_entry_block_ratio, 0.70)


if __name__ == "__main__":
    unittest.main()
