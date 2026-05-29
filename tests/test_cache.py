"""Tests for AsyncTTLCache."""

from __future__ import annotations

import asyncio

import pytest

from core.cache import AsyncTTLCache


@pytest.fixture
def cache() -> AsyncTTLCache:
    return AsyncTTLCache(ttl=60, max_size=5)


@pytest.mark.asyncio
async def test_set_and_get(cache: AsyncTTLCache) -> None:
    await cache.set("k1", "hello")
    assert await cache.get("k1") == "hello"


@pytest.mark.asyncio
async def test_miss_returns_none(cache: AsyncTTLCache) -> None:
    assert await cache.get("missing") is None


@pytest.mark.asyncio
async def test_ttl_expiry(cache: AsyncTTLCache) -> None:
    await cache.set("k2", "soon", ttl=1)
    assert await cache.get("k2") == "soon"
    await asyncio.sleep(1.1)
    assert await cache.get("k2") is None
    assert cache.metrics.expirations == 1


@pytest.mark.asyncio
async def test_lru_eviction(cache: AsyncTTLCache) -> None:
    for i in range(6):
        await cache.set(f"k{i}", i)
    # k0 should be evicted (oldest)
    assert await cache.get("k0") is None
    assert cache.metrics.evictions == 1


@pytest.mark.asyncio
async def test_delete(cache: AsyncTTLCache) -> None:
    await cache.set("del", 42)
    assert await cache.delete("del") is True
    assert await cache.get("del") is None


@pytest.mark.asyncio
async def test_hit_rate(cache: AsyncTTLCache) -> None:
    await cache.set("x", 1)
    await cache.get("x")   # hit
    await cache.get("y")   # miss
    assert cache.metrics.hit_rate == 0.5


@pytest.mark.asyncio
async def test_clear(cache: AsyncTTLCache) -> None:
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.clear()
    assert len(cache) == 0
