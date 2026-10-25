"""Rate limiter and client IP lookup (edge cases SEC-09, SEC-10)."""

from __future__ import annotations

from starlette.requests import Request

from dinewise.api import ratelimit
from dinewise.api.ratelimit import RateLimiter, client_ip


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_then_reports_wait():
    clock = FakeClock()
    limiter = RateLimiter(3, window_s=60, clock=clock)
    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    clock.now += 10
    assert limiter.hit("a") == 50  # the first hit leaves the window in 50 s
    assert limiter.hit("b") is None  # other clients are counted separately


def test_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(2, window_s=60, clock=clock)
    limiter.hit("a")
    clock.now += 30
    limiter.hit("a")
    clock.now += 31  # the first hit has expired, the second hasn't
    assert limiter.hit("a") is None
    assert limiter.hit("a") is not None


def test_rejected_hits_are_not_counted():
    clock = FakeClock()
    limiter = RateLimiter(1, window_s=60, clock=clock)
    limiter.hit("a")
    for _ in range(5):
        limiter.hit("a")
    clock.now += 60
    assert limiter.hit("a") is None


def test_idle_clients_are_forgotten(monkeypatch):
    monkeypatch.setattr(ratelimit, "SWEEP_EVERY", 3)
    clock = FakeClock()
    limiter = RateLimiter(5, window_s=60, clock=clock)
    limiter.hit("old")
    clock.now += 61
    limiter.hit("new")
    limiter.hit("new")  # third check: sweep
    assert set(limiter._hits) == {"new"}


def request_with(xff: str | None = None, peer: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_ip_uses_peer_when_no_proxy_is_trusted():
    assert client_ip(request_with("1.2.3.4"), forwarded_hops=0) == "10.0.0.1"


def test_client_ip_ignores_forged_entries():  # SEC-10
    # The client sent "6.6.6.6"; the trusted proxy appended the address it really saw.
    assert client_ip(request_with("6.6.6.6, 203.0.113.7"), forwarded_hops=1) == "203.0.113.7"
    assert client_ip(request_with("203.0.113.7, 10.1.1.1"), forwarded_hops=2) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_when_header_is_short():
    assert client_ip(request_with(), forwarded_hops=1) == "10.0.0.1"
    assert client_ip(request_with(" , "), forwarded_hops=1) == "10.0.0.1"
