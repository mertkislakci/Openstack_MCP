"""
GET tool — tüm Nova hypervisor'larını listeler.

Nova admin API gerektirir (all_tenants yetkisi olan kullanıcı).
Dönen bilgiler: hostname, durum, kaynak kullanımı (CPU/RAM/disk).
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from core.cache import cached
from core.openstack_client import get_admin_connection, list_sdk
from mcp_server.tools.base import BaseTool, ToolResult

log = structlog.get_logger(__name__)


class GetHypervisors(BaseTool):
    NAME: ClassVar[str] = "get_hypervisors"
    DESCRIPTION: ClassVar[str] = (
        "Tüm Nova hypervisor'larını listeler. "
        "Her hypervisor için hostname, durum, IP, "
        "toplam/kullanılan CPU-RAM-disk ve çalışan VM sayısını döner. "
        "Nova admin yetkisi gerektirir."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "status_filter": {
                "type": "string",
                "description": "Duruma göre filtrele: 'enabled' | 'disabled' (opsiyonel)",
                "enum": ["enabled", "disabled"],
            },
            "state_filter": {
                "type": "string",
                "description": "State'e göre filtrele: 'up' | 'down' (opsiyonel)",
                "enum": ["up", "down"],
            },
            "with_servers": {
                "type": "boolean",
                "description": "Her hypervisor'daki VM listesini de getir (varsayılan: false)",
                "default": False,
            },
        },
        "required": [],
    }
    TIMEOUT_SECONDS: ClassVar[float] = 30.0

    async def _run(
        self,
        status_filter: str | None = None,
        state_filter: str | None = None,
        with_servers: bool = False,
        **_: Any,
    ) -> ToolResult:
        hypervisors = await _fetch_hypervisors(with_servers=with_servers)

        if status_filter:
            hypervisors = [
                h for h in hypervisors
                if h["status"] == status_filter
            ]

        if state_filter:
            hypervisors = [
                h for h in hypervisors
                if h["state"] == state_filter
            ]

        # Özet istatistikler
        total_vcpus     = sum(h["vcpus"] for h in hypervisors)
        used_vcpus      = sum(h["vcpus_used"] for h in hypervisors)
        total_ram_gb    = sum(h["memory_mb"] for h in hypervisors) // 1024
        used_ram_gb     = sum(h["memory_mb_used"] for h in hypervisors) // 1024
        total_disk_gb   = sum(h["local_gb"] for h in hypervisors)
        used_disk_gb    = sum(h["local_gb_used"] for h in hypervisors)
        running_vms     = sum(h["running_vms"] for h in hypervisors)
        down_count      = sum(1 for h in hypervisors if h["state"] == "down")

        return ToolResult(
            success=True,
            data=hypervisors,
            metadata={
                "count": len(hypervisors),
                "down_count": down_count,
                "summary": {
                    "total_vcpus": total_vcpus,
                    "used_vcpus": used_vcpus,
                    "vcpu_usage_pct": round(used_vcpus / total_vcpus * 100, 1) if total_vcpus else 0,
                    "total_ram_gb": total_ram_gb,
                    "used_ram_gb": used_ram_gb,
                    "ram_usage_pct": round(used_ram_gb / total_ram_gb * 100, 1) if total_ram_gb else 0,
                    "total_disk_gb": total_disk_gb,
                    "used_disk_gb": used_disk_gb,
                    "disk_usage_pct": round(used_disk_gb / total_disk_gb * 100, 1) if total_disk_gb else 0,
                    "running_vms": running_vms,
                },
            },
        )


@cached(ttl=60, key_prefix="compute")
async def _fetch_hypervisors(with_servers: bool = False) -> list[dict[str, Any]]:
    conn = await get_admin_connection()

    raw = await list_sdk(
        conn.compute.hypervisors(details=True),
        service="nova",
    )

    result = []
    for hv in raw:
        entry: dict[str, Any] = {
            "id":                 hv.id,
            "hostname":           hv.name,
            "host_ip":            getattr(hv, "host_ip", None),
            "type":               getattr(hv, "hypervisor_type", None),
            "version":            getattr(hv, "hypervisor_version", None),
            "state":              hv.state,          # up | down
            "status":             hv.status,         # enabled | disabled
            "vcpus":              hv.vcpus or 0,
            "vcpus_used":         hv.vcpus_used or 0,
            "memory_mb":          hv.memory_size or 0,
            "memory_mb_used":     hv.memory_used or 0,
            "local_gb":           hv.disk_available or 0,
            "local_gb_used":      hv.local_disk_used or 0,
            "running_vms":        hv.running_vms or 0,
            "current_workload":   getattr(hv, "current_workload", None),
            "free_ram_mb":        getattr(hv, "free_ram_mb", None),
            "free_disk_gb":       getattr(hv, "free_disk_gb", None),
        }

        if with_servers:
            try:
                servers = await list_sdk(
                    conn.compute.servers(
                        all_tenants=True,
                        host=hv.name,
                    ),
                    service="nova",
                )
                entry["servers"] = [
                    {"id": s.id, "name": s.name, "status": s.status}
                    for s in servers
                ]
            except Exception as exc:
                log.warning("hypervisor server list failed",
                            hostname=hv.name, error=str(exc))
                entry["servers"] = []

        result.append(entry)

    log.info("hypervisors fetched", count=len(result))
    return result
