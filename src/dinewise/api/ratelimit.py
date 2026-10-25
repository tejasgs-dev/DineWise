"""Per-client rate limiting for the LLM-backed endpoint (edge cases SEC-09, SEC-10).

In process and per instance, like the rest of the service's state (OPS-05).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

# Forget idle clients every this many checks, so the table can't grow without bound.
SWEEP_EVERY = 1000


class RateLimiter:
    """Sliding window: at most `limit` hits per key in any `window_s` seconds."""

    def __init__(
        self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._checks = 0
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next one is."""
        now = self._clock()
        cutoff = now - self.window_s
        with self._lock:
            self._checks += 1
            if self._checks % SWEEP_EVERY == 0:
                self._sweep(cutoff)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return hits[0] - cutoff
            hits.append(now)
            return None

    def _sweep(self, cutoff: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]


def client_ip(request: Request, forwarded_hops: int) -> str:
    """The caller's IP address.

    Behind `forwarded_hops` trusted proxies (Render's load balancer is one), each proxy
    appends the address it saw to X-Forwarded-For, so the client is that many entries from
    the right. Entries further left come from the client itself and can be forged (SEC-10).
    """
    if forwarded_hops > 0:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
        hops = [h for h in hops if h]
        if len(hops) >= forwarded_hops:
            return hops[-forwarded_hops]
    return request.client.host if request.client else "unknown"
