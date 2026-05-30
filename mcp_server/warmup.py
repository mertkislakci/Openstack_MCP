"""
Cache warm-up — server başlarken sık kullanılan verileri önceden çeker.

Çekilen veriler:
  • Tüm projeler (Keystone)
  • Tüm instance'lar (Nova all_tenants)

Server başladıktan 2 saniye sonra arka planda çalışır.
Hata olursa sessizce geçer — warm-up kritik değil.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger(__name__)


async def warm_cache(delay: float = 2.0) -> None:
    """
    server.py lifespan içinde çağrılır:
        asyncio.create_task(warm_cache())
    """
    await asyncio.sleep(delay)
    log.info("cache warm-up starting")

    tasks = [
        ("projects",   _warm_projects()),
        ("instances",  _warm_instances()),
    ]

    results = await asyncio.gather(
        *[t for _, t in tasks],
        return_exceptions=True,
    )

    for (name, _), result in zip(tasks, results):
        if isinstance(result, Exception):
            log.warning("warm-up failed", target=name, error=str(result))
        else:
            log.info("warm-up ok", target=name)

    log.info("cache warm-up complete")


async def _warm_projects() -> None:
    from mcp_server.tools.identity.get_projects import _fetch_projects
    await _fetch_projects()


async def _warm_instances() -> None:
    from mcp_server.tools.compute.get_instances import _fetch_all_instances
    await _fetch_all_instances()
