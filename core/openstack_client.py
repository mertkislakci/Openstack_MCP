"""
OpenStack client — gelişmiş versiyon
─────────────────────────────────────
Yeni özellikler:
  • Circuit breaker — art arda hata sonrası hızlı fail
  • Retry + exponential backoff — geçici ağ hatalarında otomatik tekrar
  • Lazy SDK import
  • Per-project connection pool
  • asyncio.to_thread ile sync SDK çağrıları
"""

from __future__ import annotations

import asyncio
import importlib
import random
from typing import TYPE_CHECKING, Any

import structlog

from core.circuit_breaker import get_breaker
from core.config import get_settings

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


# ── Lazy import ───────────────────────────────────────────────────────────────

class _LazyOpenstack:
    _mod: Any = None

    @classmethod
    def get(cls) -> Any:
        if cls._mod is None:
            cls._mod = importlib.import_module("openstack")
            log.debug("openstack SDK imported")
        return cls._mod


# ── Retry + exponential backoff ───────────────────────────────────────────────

async def run_sdk(
    fn: Any,
    *args: Any,
    retries: int = 3,
    base_wait: float = 1.0,
    service: str = "nova",
    **kwargs: Any,
) -> Any:
    """
    Sync SDK fonksiyonunu thread pool'da çalıştır.
    Circuit breaker + retry ile korunur.
    """
    breaker = get_breaker(service, threshold=5, timeout=30.0)

    for attempt in range(retries):
        try:
            return await breaker.call(fn, *args, **kwargs)
        except Exception as exc:
            from core.circuit_breaker import CircuitBreakerOpen
            if isinstance(exc, CircuitBreakerOpen):
                raise  # Breaker açık — retry etme

            is_last = attempt == retries - 1
            if is_last:
                raise

            wait = (base_wait * (2 ** attempt)) + random.uniform(0.0, 0.5)
            log.warning(
                "sdk call retrying",
                service=service,
                attempt=attempt + 1,
                wait_s=round(wait, 2),
                error=str(exc),
            )
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable")  # mypy


async def list_sdk(
    generator: Any,
    service: str = "nova",
    retries: int = 3,
) -> list[Any]:
    """Lazy SDK generator'ı thread pool'da liste olarak topla."""
    def _collect() -> list[Any]:
        return list(generator)

    for attempt in range(retries):
        try:
            breaker = get_breaker(service)
            return await breaker.call(_collect)
        except Exception as exc:
            from core.circuit_breaker import CircuitBreakerOpen
            if isinstance(exc, CircuitBreakerOpen) or attempt == retries - 1:
                raise
            wait = (2 ** attempt) + random.uniform(0, 0.5)
            log.warning("list_sdk retrying", attempt=attempt + 1, wait=wait)
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable")


# ── Connection pool ───────────────────────────────────────────────────────────

class OpenStackConnectionPool:
    def __init__(self) -> None:
        self._pool: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._settings = get_settings()

    async def get_connection(self, project_id: str | None = None) -> Any:
        key = project_id or "__admin__"
        async with self._lock:
            if key not in self._pool:
                self._pool[key] = await self._create_connection(project_id)
            return self._pool[key]

    async def _create_connection(self, project_id: str | None) -> Any:
        osk = _LazyOpenstack.get()
        cfg = self._settings
        auth = dict(cfg.os_auth_dict)
        if project_id:
            auth["project_id"] = project_id
            auth.pop("project_name", None)

        log.info("creating openstack connection", project_id=project_id or "admin")

        # Keystone auth breaker altında bağlan
        keystone_breaker = get_breaker("keystone", threshold=3, timeout=60.0)
        conn = await keystone_breaker.call(
            osk.connect,
            auth=auth,
            region_name=cfg.os_region_name,
        )
        return conn

    async def close_all(self) -> None:
        async with self._lock:
            for conn in self._pool.values():
                try:
                    await asyncio.to_thread(conn.close)
                except Exception:
                    pass
            self._pool.clear()
            log.info("all connections closed")


# ── Singleton ─────────────────────────────────────────────────────────────────

_pool: OpenStackConnectionPool | None = None


def get_pool() -> OpenStackConnectionPool:
    global _pool
    if _pool is None:
        _pool = OpenStackConnectionPool()
    return _pool


async def get_admin_connection() -> Any:
    return await get_pool().get_connection()


async def get_project_connection(project_id: str) -> Any:
    return await get_pool().get_connection(project_id)
