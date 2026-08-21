import unittest
import logging
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from data.ws_client import BybitPublicStream
from main import _PybitTelemetryHandler, _closed_klines, _repair_closed_klines
from runtime_resilience import ReconnectBackoff, RecoveryLoop
from storage.models import Candle
from storage.repository import _upsert


class FakeWebSocket:
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        self.callbacks = {}
        self.exited = False

    def _save(self, name, callback):
        self.callbacks[name] = callback

    def orderbook_stream(self, depth, symbol, callback):
        self._save(f"orderbook:{depth}:{symbol}", callback)

    def trade_stream(self, symbol, callback):
        self._save(f"trade:{symbol}", callback)

    def kline_stream(self, interval, symbol, callback):
        self._save(f"kline:{interval}:{symbol}", callback)

    def all_liquidation_stream(self, symbol, callback):
        self._save(f"liquidation:{symbol}", callback)

    def ticker_stream(self, symbol, callback):
        self._save(f"ticker:{symbol}", callback)

    def exit(self):
        self.exited = True


class PublicStreamWatchdogTests(unittest.TestCase):
    def _stream(self):
        cfg = SimpleNamespace(testnet=True, ws_channel_type="linear")
        with patch("data.ws_client.WebSocket", FakeWebSocket):
            return BybitPublicStream(cfg)

    def test_useful_message_resets_watchdog_age(self):
        stream = self._stream()
        received = []
        stream._last_message_monotonic -= 300
        self.assertGreater(stream.seconds_since_message(), 299)

        stream.subscribe_ticker("ETHUSDT", received.append)
        stream.ws.callbacks["ticker:ETHUSDT"]({"topic": "tickers.ETHUSDT"})

        self.assertLess(stream.seconds_since_message(), 1)
        self.assertEqual(received, [{"topic": "tickers.ETHUSDT"}])

    def test_close_terminates_stale_websocket_before_replacement(self):
        stream = self._stream()
        stream.close()
        self.assertTrue(stream.ws.exited)

    def test_pybit_internal_tight_reconnect_is_disabled(self):
        self._stream()
        self.assertEqual(FakeWebSocket.last_kwargs["retries"], 2)
        self.assertFalse(FakeWebSocket.last_kwargs["restart_on_error"])

    def test_close_failure_does_not_prevent_watchdog_replacement(self):
        stream = self._stream()
        stream.ws.exit = unittest.mock.Mock(side_effect=RuntimeError("already closed"))
        stream.close()

    def test_pybit_transport_lifecycle_is_forwarded_to_durable_health(self):
        telemetry = unittest.mock.Mock()
        handler = _PybitTelemetryHandler(telemetry)
        logger = logging.getLogger("test.pybit.telemetry")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.error("WebSocket Unified V5 encountered error: ping timeout")
            logger.info("WebSocket Unified V5 attempting connection...")
            logger.info("WebSocket Unified V5 connected")
            logger.info("unrelated library message")
        finally:
            logger.removeHandler(handler)

        self.assertEqual(telemetry.record_health.call_count, 3)
        self.assertEqual(
            [call.args[1] for call in telemetry.record_health.call_args_list],
            ["websocket_disconnect", "websocket_reconnect_attempt", "websocket_connected"],
        )

    def test_rest_repair_persists_only_closed_candles(self):
        now = 1_800_000
        rows = [
            {"start": "0"},
            {"start": "900000"},
            {"start": "1800000"},
        ]
        self.assertEqual(
            [row["start"] for row in _closed_klines(rows, "15", now_ms=now)],
            ["0", "900000"],
        )

    def test_periodic_rest_repair_backfills_every_symbol_idempotently(self):
        cfg = SimpleNamespace(symbols=["ETHUSDT", "SOLUSDT"], primary_interval="15")
        rest = unittest.mock.Mock()
        rest.get_klines.return_value = [{
            "start": "0", "open": "1", "high": "2", "low": "0.5",
            "close": "1.5", "volume": "10", "turnover": "15",
        }]
        store = unittest.mock.Mock(); telemetry = unittest.mock.Mock()
        self.assertEqual(
            _repair_closed_klines(cfg, store, rest, telemetry, now_ms=900_000), 2
        )
        self.assertEqual(store.save_historical_klines.call_count, 2)
        self.assertEqual(telemetry.record_health.call_args.args[1], "candle_rest_repair")

    def test_rest_repair_sizes_request_to_cover_entire_detected_candle_gap(self):
        now = 10 * 900_000
        cfg = SimpleNamespace(symbols=["ETHUSDT"], primary_interval="15")
        rest = unittest.mock.Mock(); rest.get_klines.return_value = []
        store = unittest.mock.Mock()
        store.latest_candle_start.return_value = 900_000
        _repair_closed_klines(cfg, store, rest, unittest.mock.Mock(), now_ms=now)
        kwargs = rest.get_klines.call_args.kwargs
        self.assertEqual(kwargs["start"], 900_000)
        self.assertGreaterEqual(kwargs["limit"], 11)
        self.assertEqual(kwargs["end"], now)

    def test_candle_conflict_updates_final_ohlcv_instead_of_freezing_partial_row(self):
        session = unittest.mock.Mock()
        _upsert(session, Candle, [{
            "symbol": "ETHUSDT", "interval": "15", "start_time": 1,
            "open": 1, "high": 2, "low": 0.5, "close": 1.5,
            "volume": 10, "turnover": 15,
        }])
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT", sql)
        self.assertIn("DO UPDATE SET", sql)
        self.assertIn("close = excluded.close", sql)

    def test_bounded_exponential_backoff_has_no_zero_delay_cpu_spin(self):
        clock = [0.0]
        backoff = ReconnectBackoff(
            initial_seconds=2, maximum_seconds=10, jitter_ratio=0,
            monotonic_fn=lambda: clock[0],
        )
        self.assertEqual([backoff.failure_delay() for _ in range(6)], [2, 4, 8, 10, 10, 10])
        self.assertTrue(all(value >= 2 for value in [backoff.failure_delay() for _ in range(3)]))

    def test_backoff_resets_only_after_stable_message_period(self):
        clock = [0.0]
        backoff = ReconnectBackoff(
            initial_seconds=2, maximum_seconds=10, jitter_ratio=0,
            stable_reset_seconds=30, monotonic_fn=lambda: clock[0],
        )
        backoff.failure_delay(); backoff.failure_delay()
        backoff.connected()
        clock[0] = 29
        self.assertFalse(backoff.maybe_reset_after_stable())
        clock[0] = 31
        self.assertTrue(backoff.maybe_reset_after_stable())
        self.assertEqual(backoff.failure_delay(), 2)

    def test_healthy_connection_does_not_emit_periodic_fake_resets(self):
        clock = [0.0]
        backoff = ReconnectBackoff(
            stable_reset_seconds=30, monotonic_fn=lambda: clock[0],
        )
        backoff.connected()
        clock[0] = 300
        self.assertFalse(backoff.maybe_reset_after_stable())
        self.assertIsNone(backoff.connected_since)

    def test_repeated_disconnects_eventually_request_supervisor_restart(self):
        clock = [0.0]
        backoff = ReconnectBackoff(
            initial_seconds=1, maximum_seconds=5, jitter_ratio=0,
            restart_after_seconds=20, monotonic_fn=lambda: clock[0],
        )
        backoff.failure_delay()
        clock[0] = 19
        self.assertFalse(backoff.restart_required())
        clock[0] = 20
        self.assertTrue(backoff.restart_required())

    def test_reconnect_wait_runs_rest_repair_without_busy_loop(self):
        clock = [0.0]; sleeps = []; repairs = []
        def sleep(seconds):
            sleeps.append(seconds); clock[0] += seconds
        RecoveryLoop(sleep_fn=sleep, monotonic_fn=lambda: clock[0]).wait(
            5, repair_interval=2, repair=lambda: repairs.append(clock[0])
        )
        self.assertEqual(repairs, [0.0, 2.0, 4.0])
        self.assertEqual(len(sleeps), 5)
        self.assertTrue(all(seconds > 0 for seconds in sleeps))


if __name__ == "__main__":
    unittest.main()
