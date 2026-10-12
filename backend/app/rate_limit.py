"""Per-IP rate limiting.

An in-process fixed-window counter. Deliberately simple, because this service
has no external state store and adding Redis would mean another thing to
operate and secure for a free service with a 20-req/minute ceiling.

What that means honestly: the limit is **per instance**. Vercel autoscales, so
with N instances the effective ceiling is roughly N x 20/min. That is an
acceptable trade for abuse control, and the real protection against expensive
work is the conversion timeout and the per-request CPU ceiling, not this.

What this does buy: it stops a single client from pinning the process in a
tight loop, which is the actual failure mode that matters.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from fastapi import Request

from .config import Settings, get_settings


class _FixedWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, key: str) -> tuple[bool, int, int]:
        """Return ``(allowed, remaining, retry_after_seconds)``."""
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            # Periodic sweep keeps the dict from growing without bound when a
            # scan hits a fresh key each time. Cheap because it only runs when
            # the window has actually advanced.
            if now - self._last_sweep > self.window:
                stale = [k for k, ts in self._hits.items() if not ts or ts[-1] < cutoff]
                for k in stale:
                    del self._hits[k]
                self._last_sweep = now

            timestamps = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(timestamps) >= self.limit:
                retry_after = max(1, int(self.window - (now - timestamps[0])) + 1)
                self._hits[key] = timestamps
                return False, 0, retry_after

            timestamps.append(now)
            self._hits[key] = timestamps
            return True, self.limit - len(timestamps), 0


_limiter: _FixedWindowLimiter | None = None
_limiter_key: tuple[int, int] | None = None
_limiter_lock = threading.Lock()


def get_limiter(settings: Settings | None = None) -> _FixedWindowLimiter:
    """Lazily build the limiter so it picks up test overrides."""
    global _limiter, _limiter_key
    s = settings or get_settings()
    wanted = (s.rate_limit_requests, s.rate_limit_window_seconds)
    with _limiter_lock:
        if _limiter is None or _limiter_key != wanted:
            _limiter = _FixedWindowLimiter(*wanted)
            _limiter_key = wanted
        return _limiter


def client_key(request: Request) -> str:
    """Identify the caller.

    Prefers the leftmost X-Forwarded-For entry, because behind Vercel (or any
    proxy) the socket peer is the proxy. The header is client-controlled, so
    this is abuse *deterrence*, not authentication — a caller willing to forge
    the header can evade it. Nothing here depends on it being trustworthy.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return (request.client.host if request.client else "unknown")[:64]


def reset_limiter() -> None:
    """Test hook: clear all state."""
    global _limiter, _limiter_key
    with _limiter_lock:
        _limiter = None
        _limiter_key = None
