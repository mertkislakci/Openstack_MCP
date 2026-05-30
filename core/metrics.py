"""
Prometheus metrikleri — /metrics endpoint'i için.

Ölçülen metrikler:
  mcp_tool_calls_total        — tool adı + status (success/error) başına sayaç
  mcp_tool_duration_seconds   — tool latency histogramı
  mcp_cache_hits_total        — hit/miss/stale/eviction sayaçları
  mcp_circuit_breaker_state   — breaker state gauge (0=closed,1=half,2=open)
  mcp_openstack_api_calls     — nova/keystone API çağrı sayacı
"""

from __future__ import annotations

from typing import Any

_prometheus_available = False
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )
    _prometheus_available = True
except ImportError:
    pass


def _noop(*args: Any, **kwargs: Any) -> Any:
    class _Noop:
        def labels(self, **kw: Any) -> "_Noop":
            return self
        def inc(self, *a: Any) -> None: ...
        def observe(self, *a: Any) -> None: ...
        def set(self, *a: Any) -> None: ...
    return _Noop()


if _prometheus_available:
    tool_calls_total = Counter(
        "mcp_tool_calls_total",
        "Total MCP tool calls",
        ["tool", "tool_type", "status"],
    )
    tool_duration_seconds = Histogram(
        "mcp_tool_duration_seconds",
        "MCP tool execution duration",
        ["tool"],
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )
    cache_ops_total = Counter(
        "mcp_cache_ops_total",
        "Cache operation counts",
        ["operation"],  # hit, miss, stale_hit, eviction
    )
    circuit_breaker_state = Gauge(
        "mcp_circuit_breaker_state",
        "Circuit breaker state (0=closed, 1=half, 2=open)",
        ["breaker"],
    )
    openstack_api_calls_total = Counter(
        "mcp_openstack_api_calls_total",
        "OpenStack API calls",
        ["service", "status"],
    )
else:
    tool_calls_total = _noop()           # type: ignore[assignment]
    tool_duration_seconds = _noop()      # type: ignore[assignment]
    cache_ops_total = _noop()            # type: ignore[assignment]
    circuit_breaker_state = _noop()      # type: ignore[assignment]
    openstack_api_calls_total = _noop()  # type: ignore[assignment]


def metrics_response() -> tuple[str, str]:
    """(body, content_type) döner — /metrics handler'ında kullan."""
    if not _prometheus_available:
        return "# prometheus-client not installed\n", "text/plain"
    return (
        generate_latest().decode("utf-8"),
        CONTENT_TYPE_LATEST,
    )


def update_breaker_gauges() -> None:
    """Circuit breaker durumlarını Prometheus gauge'larına yaz."""
    if not _prometheus_available:
        return
    from core.circuit_breaker import all_breaker_statuses, BreakerState
    state_map = {BreakerState.CLOSED: 0, BreakerState.HALF: 1, BreakerState.OPEN: 2}
    for s in all_breaker_statuses():
        circuit_breaker_state.labels(breaker=s["name"]).set(
            state_map.get(s["state"], 0)
        )
