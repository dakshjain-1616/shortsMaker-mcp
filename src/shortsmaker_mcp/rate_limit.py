"""Small per-process request limiter for the local/prototype HTTP boundary."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

_SWEEP_THRESHOLD = 10_000


class RequestRateLimiter:
    """Limit requests per client key in one process.

    This is intentionally a lightweight guard for the standalone prototype. A multi-worker or
    multi-instance deployment needs a shared limiter at the gateway or Redis layer instead.
    """

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _sweep(self, cutoff: float) -> None:
        """Drop keys whose events have all expired so one-off clients don't accumulate."""
        for key in [k for k, v in self._events.items() if not v or v[-1] <= cutoff]:
            del self._events[key]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            if len(self._events) > _SWEEP_THRESHOLD:
                self._sweep(cutoff)
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True
