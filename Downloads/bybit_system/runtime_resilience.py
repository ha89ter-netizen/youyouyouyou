"""Small deterministic primitives for collector recovery and event suppression."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ReconnectBackoff:
    """Bounded exponential backoff with stability-aware reset and restart budget."""

    initial_seconds: float = 5.0
    maximum_seconds: float = 60.0
    jitter_ratio: float = 0.20
    stable_reset_seconds: float = 120.0
    restart_after_seconds: float = 900.0
    random_fn: Callable[[], float] = random.random
    monotonic_fn: Callable[[], float] = time.monotonic

    def __post_init__(self):
        self.initial_seconds = max(0.1, float(self.initial_seconds))
        self.maximum_seconds = max(self.initial_seconds, float(self.maximum_seconds))
        self.jitter_ratio = max(0.0, min(float(self.jitter_ratio), 1.0))
        self.stable_reset_seconds = max(0.0, float(self.stable_reset_seconds))
        self.restart_after_seconds = max(0.0, float(self.restart_after_seconds))
        self.failures = 0
        self.degraded_since: Optional[float] = None
        self.connected_since: Optional[float] = None

    def failure_delay(self) -> float:
        now = self.monotonic_fn()
        if self.degraded_since is None:
            self.degraded_since = now
        self.connected_since = None
        base = min(self.maximum_seconds, self.initial_seconds * (2 ** self.failures))
        self.failures += 1
        # Symmetric bounded jitter; tests can inject a deterministic random_fn.
        jitter = (self.random_fn() * 2.0 - 1.0) * self.jitter_ratio
        return max(0.1, min(self.maximum_seconds, base * (1.0 + jitter)))

    def connected(self) -> None:
        # A stable-period timer is meaningful only after a failure.  Starting
        # it on every healthy loop iteration used to emit a fake
        # ``backoff_reset`` event every ``stable_reset_seconds`` forever.
        if self.failures == 0 and self.degraded_since is None:
            self.connected_since = None
            return
        if self.connected_since is None:
            self.connected_since = self.monotonic_fn()

    def maybe_reset_after_stable(self) -> bool:
        if self.failures == 0 and self.degraded_since is None:
            self.connected_since = None
            return False
        if self.connected_since is None:
            return False
        if self.monotonic_fn() - self.connected_since < self.stable_reset_seconds:
            return False
        self.failures = 0
        self.degraded_since = None
        self.connected_since = None
        return True

    def restart_required(self) -> bool:
        return bool(
            self.restart_after_seconds > 0
            and self.degraded_since is not None
            and self.monotonic_fn() - self.degraded_since >= self.restart_after_seconds
        )


class RecoveryLoop:
    """One reconnect wait slice that keeps REST candle repair alive."""

    def __init__(self, *, sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn

    def wait(self, delay: float, *, repair_interval: float, repair: Callable[[], None]) -> None:
        deadline = self.monotonic_fn() + max(0.0, delay)
        next_repair = self.monotonic_fn()
        while self.monotonic_fn() < deadline:
            now = self.monotonic_fn()
            if now >= next_repair:
                repair()
                next_repair = now + max(1.0, repair_interval)
            self.sleep_fn(min(1.0, max(0.0, deadline - self.monotonic_fn())))
