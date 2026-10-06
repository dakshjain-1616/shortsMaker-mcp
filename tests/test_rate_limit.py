import time

from shortsmaker_mcp.rate_limit import RequestRateLimiter


def test_rate_limiter_rejects_after_limit():
    limiter = RequestRateLimiter(limit=2, window_seconds=60)

    assert limiter.allow("client") is True
    assert limiter.allow("client") is True
    assert limiter.allow("client") is False
    assert limiter.allow("other-client") is True


def test_rate_limiter_sweeps_expired_keys(monkeypatch):
    from shortsmaker_mcp import rate_limit

    monkeypatch.setattr(rate_limit, "_SWEEP_THRESHOLD", 2)
    limiter = RequestRateLimiter(limit=5, window_seconds=0.01)
    for key in ("a", "b", "c"):
        limiter.allow(key)
    time.sleep(0.02)

    limiter.allow("d")

    assert set(limiter._events) == {"d"}
