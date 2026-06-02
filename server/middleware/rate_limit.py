"""
Lightweight in-memory rate limiter for unauthenticated requests.

Limits per-IP request rate for requests that don't carry a Bearer token,
preventing scanner bursts (e.g. 40+ PHP probe requests in seconds) from
consuming server resources.

Authenticated requests are not rate-limited here (they have their own
per-user limits elsewhere).
"""

import logging
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from constants import RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS

logger = logging.getLogger(__name__)


class _TokenBucket:
    """Simple per-IP token bucket for rate limiting."""

    __slots__ = ("_buckets",)

    def __init__(self):
        # ip -> (tokens_remaining, last_refill_timestamp)
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (float(RATE_LIMIT_MAX_REQUESTS), time.monotonic())
        )

    def allow(self, ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        tokens, last_refill = self._buckets[ip]

        # Refill tokens based on elapsed time
        elapsed = now - last_refill
        tokens = min(RATE_LIMIT_MAX_REQUESTS, tokens + elapsed * (RATE_LIMIT_MAX_REQUESTS / RATE_LIMIT_WINDOW_SECONDS))

        if tokens >= 1.0:
            self._buckets[ip] = (tokens - 1.0, now)
            return True

        self._buckets[ip] = (tokens, now)
        return False

    def cleanup(self, max_age: float = 300.0):
        """Remove stale entries older than max_age seconds."""
        now = time.monotonic()
        stale = [ip for ip, (_, ts) in self._buckets.items() if now - ts > max_age]
        for ip in stale:
            del self._buckets[ip]


_bucket = _TokenBucket()
_last_cleanup = time.monotonic()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate-limit unauthenticated requests by client IP."""

    async def dispatch(self, request: StarletteRequest, call_next):
        # Only rate-limit unauthenticated requests
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            return await call_next(request)

        # Extract client IP
        ip = request.client.host if request.client else "unknown"

        # Periodic cleanup of stale buckets (every 5 minutes)
        global _last_cleanup
        now = time.monotonic()
        if now - _last_cleanup > 300:
            _bucket.cleanup()
            _last_cleanup = now

        if not _bucket.allow(ip):
            logger.debug(f"Rate limited | ip={ip} | path={request.url.path}")
            return JSONResponse(
                {"error": "too_many_requests", "retry_after": RATE_LIMIT_WINDOW_SECONDS},
                status_code=429,
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )

        return await call_next(request)
