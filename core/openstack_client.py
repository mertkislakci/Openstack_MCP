"""
OpenStack client with:
  • Lazy initialization — SDK imported only on first use
  • Per-project connection pool
  • Admin connection for cross-project queries (all-projects listing)
  • Async wrapper via asyncio.to_thread (openstacksdk is sync)
  • Automatic re-auth on token expiry
"""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING, Any

import structlog

from core.config import get_settings

if TYPE_CHECKING:
    import openstack as _openstack_type

log = structlog.get_logger(__name__)

# ── Lazy module holder ────────────────────────────────────────────────────────

class _LazyOpenstack:
    """Defers `import openstack` until first use."""
    _mod: Any = None

    @classmethod
    def get(cls) -> Any:
        if cls._mod is None:
            cls._mod = importlib.import_module("openstack")
            log.debug("openstack SDK imported lazily")
        return cls._mod


# ── Connection pool ───────────────────────────────────────────────────────────

class OpenStackConnectionPool:
    """
    Maintains one SDK connection per project_id.
    Admin connection uses the configured admin project.

    All SDK calls are run in a thread pool via asyncio.to_thread()
    so they never block the event loop.
    """

    def __init__(self) -> None:
        self._pool: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._settings = get_settings()

    async def get_connection(self, project_id: str | None = None) -> Any:
        """
        Return a connection for the given project (or admin if None).
        Creates a new connection on first call; returns cached afterward.
        """
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

        conn = await asyncio.to_thread(
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
            log.info("all openstack connections closed")


# ── SDK call helpers ──────────────────────────────────────────────────────────

async def run_sdk(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous SDK call in a thread pool."""
    return await asyncio.to_thread(func, *args, **kwargs)


async def list_sdk(generator: Any) -> list[Any]:
    """
    Consume a lazy SDK generator (e.g. conn.compute.servers()) in a thread,
    returning a plain list. Safe for large result sets.
    """
    def _collect() -> list[Any]:
        return list(generator)

    return await asyncio.to_thread(_collect)


# ── Module-level singleton ────────────────────────────────────────────────────

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
