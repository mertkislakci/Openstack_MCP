"""
Upstream router — selects which MCP server handles a given tool call.

Routing strategies
──────────────────
  round_robin   — distribute load evenly across upstreams
  tool_affinity — route specific tool prefixes to specific upstreams
                  (useful when compute tools live on a different server
                  than storage/network tools)
  first_healthy — always use first upstream that passes health check

Upstream health is checked periodically in the background.
Unhealthy upstreams are bypassed until they recover.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class RoutingStrategy(StrEnum):
    ROUND_ROBIN = "round_robin"
    TOOL_AFFINITY = "tool_affinity"
    FIRST_HEALTHY = "first_healthy"


@dataclass
class UpstreamServer:
    url: str
    name: str = ""
    healthy: bool = True
    last_check: float = field(default_factory=time.monotonic)
    failure_count: int = 0
    total_requests: int = 0
    total_errors: int = 0

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_errors / self.total_requests


class UpstreamRouter:
    """
    Manages upstream MCP servers and routes tool calls to them.

    Usage
    ─────
        router = UpstreamRouter(urls=["http://mcp1:8080", "http://mcp2:8080"])
        await router.start_health_checks()
        url = await router.pick("get_instances")
    """

    # Tool prefix → upstream index (for tool_affinity strategy)
    AFFINITY_MAP: dict[str, int] = {
        "get_": 0,
        "set_": 0,
    }

    def __init__(
        self,
        urls: list[str],
        strategy: RoutingStrategy = RoutingStrategy.ROUND_ROBIN,
        health_interval: float = 30.0,
    ) -> None:
        self._servers = [
            UpstreamServer(url=u, name=f"upstream-{i}")
            for i, u in enumerate(urls)
        ]
        self._strategy = strategy
        self._health_interval = health_interval
        self._rr_counter = itertools.cycle(range(len(self._servers)))
        self._health_task: asyncio.Task[Any] | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def start_health_checks(self) -> None:
        self._health_task = asyncio.create_task(self._health_loop())
        log.info("upstream health checker started", servers=len(self._servers))

    async def stop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
        if self._client:
            await self._client.aclose()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_interval)
            await asyncio.gather(*[self._check(s) for s in self._servers], return_exceptions=True)

    async def _check(self, server: UpstreamServer) -> None:
        try:
            r = await self.client.get(f"{server.url}/health", timeout=5.0)
            was_healthy = server.healthy
            server.healthy = r.status_code == 200
            server.failure_count = 0 if server.healthy else server.failure_count + 1
            server.last_check = time.monotonic()
            if was_healthy != server.healthy:
                log.warning("upstream health changed", server=server.url, healthy=server.healthy)
        except Exception as exc:
            server.healthy = False
            server.failure_count += 1
            server.last_check = time.monotonic()
            log.warning("upstream health check failed", server=server.url, error=str(exc))

    def pick(self, tool_name: str) -> UpstreamServer:
        healthy = [s for s in self._servers if s.healthy]
        if not healthy:
            # Fall back to all servers if all unhealthy
            healthy = self._servers
            log.warning("all upstreams unhealthy, using all")

        if self._strategy == RoutingStrategy.ROUND_ROBIN:
            # Filter round-robin to healthy only
            for _ in range(len(self._servers)):
                idx = next(self._rr_counter) % len(self._servers)
                s = self._servers[idx]
                if s.healthy:
                    return s
            return healthy[0]

        if self._strategy == RoutingStrategy.TOOL_AFFINITY:
            for prefix, idx in self.AFFINITY_MAP.items():
                if tool_name.startswith(prefix) and idx < len(self._servers):
                    s = self._servers[idx]
                    if s.healthy:
                        return s
            return healthy[0]

        # FIRST_HEALTHY
        return healthy[0]

    def record_result(self, server: UpstreamServer, success: bool) -> None:
        server.total_requests += 1
        if not success:
            server.total_errors += 1

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "url": s.url,
                "name": s.name,
                "healthy": s.healthy,
                "failure_count": s.failure_count,
                "total_requests": s.total_requests,
                "error_rate": round(s.error_rate, 3),
                "last_check_ago_s": round(time.monotonic() - s.last_check, 1),
            }
            for s in self._servers
        ]
