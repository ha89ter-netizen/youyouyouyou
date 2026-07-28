import unittest
from types import SimpleNamespace
from unittest.mock import patch

from data.ws_client import BybitPublicStream


class FakeWebSocket:
    def __init__(self, **_kwargs):
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

    def test_close_failure_does_not_prevent_watchdog_replacement(self):
        stream = self._stream()
        stream.ws.exit = unittest.mock.Mock(side_effect=RuntimeError("already closed"))
        stream.close()


if __name__ == "__main__":
    unittest.main()
