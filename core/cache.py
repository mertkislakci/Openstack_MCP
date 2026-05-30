"""
Async TTL cache — gelişmiş versiyon
────────────────────────────────────
Yeni özellikler:
  • Stale-while-revalidate (SWR) — eski veri döner, arka planda yenilenir
  • Stampede koruması      — aynı key için tek fetch, diğerleri bekler
  • Per-key TTL override
  • LRU eviction (OrderedDict)
  • CacheMetrics
  • @cached decorator
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar

import structlog

from core.config import get_settings

log = structlog.get_logger(__name__)
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass
class _Entry:
    value: Any
    expires_at: float
    set_at: float = field(default_factory=time.monotonic)


@dataclass
class CacheMetrics:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0
    stale_hits: int = 0
    stampedes_blocked: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class AsyncTTLCache:
    """
    Async in-memory cache — SWR + stampede koruması ile.

    Kullanım:
        cache = AsyncTTLCache(ttl=60, max_size=1000, stale_ttl=300)
        await cache.set("k", value)
        result = await cache.get("k")
        result = await cache.get_or_fetch("k", fetch_fn)
    """

    def __init__(
        self,
        ttl: int | None = None,
        max_size: int | None = None,
        stale_ttl: int | None = None,
    ) -> None:
        cfg = get_settings()
        self._ttl = ttl if ttl is not None else cfg.cache_ttl
        self._stale_ttl = stale_ttl or self._ttl * 5
        self._max_size = max_size if max_size is not None else cfg.cache_max_size
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        # Stampede: key → asyncio.Event (fetch devam ediyor)
        self._inflight: dict[str, asyncio.Event] = {}
        self.metrics = CacheMetrics()

    # ── Temel get/set/delete ─────────────────────────────────────────────────

    async def get(self, key: str) -> Any:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.metrics.misses += 1
                return None
            now = time.monotonic()
            if now > entry.expires_at:
                del self._store[key]
                self.metrics.expirations += 1
                self.metrics.misses += 1
                return None
            self._store.move_to_end(key)
            self.metrics.hits += 1
            return entry.value

    async def get_stale(self, key: str) -> tuple[Any, bool]:
        """
        (value, is_stale) döner.
        TTL dolmuş ama stale_ttl içindeyse is_stale=True, value=eski veri.
        """
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.metrics.misses += 1
                return None, False
            now = time.monotonic()
            age = now - entry.set_at
            if age < self._ttl:
                self._store.move_to_end(key)
                self.metrics.hits += 1
                return entry.value, False
            if age < self._stale_ttl:
                self.metrics.stale_hits += 1
                return entry.value, True
            del self._store[key]
            self.metrics.expirations += 1
            self.metrics.misses += 1
            return None, False

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        effective_ttl = ttl if ttl is not None else self._ttl
        expires_at = time.monotonic() + effective_ttl
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = _Entry(value=value, expires_at=expires_at)
            if len(self._store) > self._max_size:
                self._store.popitem(last=False)
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

    # ── Stale-while-revalidate + Stampede koruması ────────────────────────────

    async def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        ttl: int | None = None,
    ) -> Any:
        """
        1. Taze veri var → döndür
        2. Eski (stale) veri var → anında döndür, arka planda yenile
        3. Veri yok + başka fetch devam ediyor → bekle (stampede koruması)
        4. Veri yok + fetch yok → fetch yap, kaydet, döndür
        """
        value, is_stale = await self.get_stale(key)

        if value is not None and not is_stale:
            return value  # taze, hemen dön

        if value is not None and is_stale:
            # Eski veriyi döndür, arka planda yenile
            if key not in self._inflight:
                asyncio.create_task(self._refresh(key, fetch_fn, ttl))
                log.debug("swr background refresh scheduled", key=key)
            return value

        # Cache miss — stampede kontrolü
        if key in self._inflight:
            self.metrics.stampedes_blocked += 1
            log.debug("stampede blocked, waiting", key=key)
            await self._inflight[key].wait()
            return await self.get(key)

        # Fetch yap
        return await self._refresh(key, fetch_fn, ttl)

    async def _refresh(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        ttl: int | None = None,
    ) -> Any:
        ev = asyncio.Event()
        self._inflight[key] = ev
        try:
            result = await fetch_fn()
            await self.set(key, result, ttl=ttl)
            log.debug("cache refreshed", key=key)
            return result
        except Exception as exc:
            log.warning("cache refresh failed", key=key, error=str(exc))
            raise
        finally:
            ev.set()
            self._inflight.pop(key, None)


# ── Module-level singleton ────────────────────────────────────────────────────

_default_cache: AsyncTTLCache | None = None


def get_cache() -> AsyncTTLCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = AsyncTTLCache()
    return _default_cache


# ── Decorator API ─────────────────────────────────────────────────────────────

def cached(ttl: int | None = None, key_prefix: str = "") -> Callable[[F], F]:
    """
    Async fonksiyonu cache'ler. SWR + stampede koruması otomatik.

        @cached(ttl=120, key_prefix="identity")
        async def get_projects(): ...
    """
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache = get_cache()
            key_parts = [key_prefix or func.__module__, func.__name__]
            for a in args:
                try:
                    key_parts.append(str(a))
                except Exception:
                    key_parts.append(type(a).__name__)
            for k, v in sorted(kwargs.items()):
                key_parts.append(f"{k}={v}")
            cache_key = ":".join(key_parts)

            async def fetch():
                return await func(*args, **kwargs)

            return await cache.get_or_fetch(cache_key, fetch, ttl=ttl)

        return wrapper  # type: ignore[return-value]

    return decorator
