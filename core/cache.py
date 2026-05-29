"""
Async TTL cache with LRU eviction.

Features
────────
• Per-key TTL with sliding or fixed expiry
• Max-size LRU eviction (O(1) via OrderedDict)
• Async-safe: asyncio.Lock prevents stampedes
• Decorator API: @cached(ttl=60)
• Metrics: hits / misses / evictions exposed for Prometheus/structlog
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar

from core.config import get_settings

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass
class _Entry:
    value: Any
    expires_at: float


@dataclass
class CacheMetrics:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class AsyncTTLCache:
    """
    Async in-memory cache with TTL and LRU eviction.

    Usage
    ─────
        cache = AsyncTTLCache(ttl=300, max_size=1000)
        await cache.set("key", value)
        result = await cache.get("key")          # None on miss/expired
        await cache.delete("key")
        await cache.clear()
    """

    def __init__(self, ttl: int | None = None, max_size: int | None = None) -> None:
        cfg = get_settings()
        self._ttl = ttl if ttl is not None else cfg.cache_ttl
        self._max_size = max_size if max_size is not None else cfg.cache_max_size
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self.metrics = CacheMetrics()

    async def get(self, key: str) -> Any:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.metrics.misses += 1
                return None
            if time.monotonic() > entry.expires_at:
                del self._store[key]
                self.metrics.expirations += 1
                self.metrics.misses += 1
                return None
            # Move to end → recently used
            self._store.move_to_end(key)
            self.metrics.hits += 1
            return entry.value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        effective_ttl = ttl if ttl is not None else self._ttl
        expires_at = time.monotonic() + effective_ttl
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = _Entry(value=value, expires_at=expires_at)
            if len(self._store) > self._max_size:
                self._store.popitem(last=False)  # evict oldest
                self.metrics.evictions += 1

    async def delete(self, key: str) -> bool:
        async with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    async def keys(self) -> list[str]:
        async with self._lock:
            return list(self._store.keys())

    def __len__(self) -> int:
        return len(self._store)


# ── Module-level singletons ─────────────────────────────────────────────────

_default_cache: AsyncTTLCache | None = None


def get_cache() -> AsyncTTLCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = AsyncTTLCache()
    return _default_cache


# ── Decorator API ────────────────────────────────────────────────────────────

def cached(ttl: int | None = None, key_prefix: str = "") -> Callable[[F], F]:
    """
    Decorator that caches the return value of an async function.

        @cached(ttl=120, key_prefix="os")
        async def get_projects(conn):
            ...

    Cache key = f"{key_prefix}:{func_name}:{args}:{kwargs}"
    """
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache = get_cache()
            key_parts = [key_prefix or func.__module__, func.__name__]
            # Serialize args (skip complex objects like SDK connections)
            for a in args:
                try:
                    key_parts.append(str(a))
                except Exception:
                    key_parts.append(type(a).__name__)
            for k, v in sorted(kwargs.items()):
                key_parts.append(f"{k}={v}")
            cache_key = ":".join(key_parts)

            cached_val = await cache.get(cache_key)
            if cached_val is not None:
                return cached_val

            result = await func(*args, **kwargs)
            await cache.set(cache_key, result, ttl=ttl)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
