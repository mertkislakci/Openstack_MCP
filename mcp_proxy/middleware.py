"""
FastAPI middleware stack for MCP Proxy:
  • RequestID injection
  • Structured access logging
  • Rate limiting (token-bucket, per-client-IP)
  • API key authentication (optional)
  • Request timing
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

log = structlog.get_logger(__name__)


# ── Request ID ────────────────────────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── Access log ────────────────────────────────────────────────────────────────

class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 1),
            client=request.client.host if request.client else "?",
            request_id=getattr(request.state, "request_id", "?"),
        )
        return response


# ── Rate limiter (token bucket) ────────────────────────────────────────────────

class _TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate          # tokens/second
        self.capacity = capacity
        self.tokens = capacity
        self._last = time.monotonic()

    def consume(self, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP token-bucket rate limiter.
    Defaults: 30 req/s, burst of 60.
    Pass rate= and capacity= as constructor args.
    """

    def __init__(self, app: Any, rate: float = 30.0, capacity: float = 60.0) -> None:
        super().__init__(app)
        self._buckets: dict[str, _TokenBucket] = defaultdict(
            lambda: _TokenBucket(rate, capacity)
        )
        self._lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        async with self._lock:
            bucket = self._buckets[client_ip]
            allowed = bucket.consume()

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Too many requests", "retry_after": "1s"},
                headers={"Retry-After": "1"},
            )
        return await call_next(request)


# ── Optional API key auth ─────────────────────────────────────────────────────

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """
    Simple bearer / X-API-Key header authentication.
    Disabled if api_keys is empty.
    """

    def __init__(self, app: Any, api_keys: list[str] | None = None) -> None:
        super().__init__(app)
        self._keys: set[str] = set(api_keys or [])

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._keys:
            return await call_next(request)

        # Skip health / metrics
        if request.url.path in ("/health", "/metrics"):
            return await call_next(request)

        key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        if key not in self._keys:
            return JSONResponse(status_code=401, content={"error": "Invalid or missing API key"})

        return await call_next(request)
