"""
GET tool — detailed info for a single instance by ID.
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.cache import cached
from core.openstack_client import get_admin_connection, run_sdk
from mcp_server.tools.base import BaseTool, ToolResult


class GetInstanceDetail(BaseTool):
    NAME: ClassVar[str] = "get_instance_detail"
    DESCRIPTION: ClassVar[str] = (
        "Get detailed information about a single Nova instance by its UUID. "
        "Returns full metadata, network info, fault details, and console URLs."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "UUID of the Nova instance",
            },
        },
        "required": ["instance_id"],
    }

    async def _run(self, instance_id: str, **_: Any) -> ToolResult:
        detail = await _fetch_instance_detail(instance_id)
        if detail is None:
            return ToolResult(success=False, error=f"Instance {instance_id!r} not found")
        return ToolResult(success=True, data=detail)


@cached(ttl=30, key_prefix="compute")
async def _fetch_instance_detail(instance_id: str) -> dict[str, Any] | None:
    conn = await get_admin_connection()
    try:
        s = await run_sdk(conn.compute.get_server, instance_id)
    except Exception:
        return None

    ips: dict[str, list[str]] = {}
    for net, addrs in (s.addresses or {}).items():
        ips[net] = [a.get("addr", "") for a in addrs]

    return {
        "id": s.id,
        "name": s.name,
        "status": s.status,
        "project_id": s.project_id or s.tenant_id,
        "user_id": s.user_id,
        "flavor": {
            "id": getattr(s.flavor, "id", ""),
            "vcpus": getattr(s.flavor, "vcpus", None),
            "ram_mb": getattr(s.flavor, "ram", None),
            "disk_gb": getattr(s.flavor, "disk", None),
        },
        "image": {"id": getattr(s.image, "id", "") if s.image else ""},
        "ip_addresses": ips,
        "host": getattr(s, "OS-EXT-SRV-ATTR:host", None),
        "hypervisor_hostname": getattr(s, "OS-EXT-SRV-ATTR:hypervisor_hostname", None),
        "availability_zone": s.availability_zone,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "launched_at": getattr(s, "OS-SRV-USG:launched_at", None),
        "key_name": s.key_name,
        "security_groups": [sg.get("name") for sg in (s.security_groups or [])],
        "fault": getattr(s, "fault", None),
        "metadata": s.metadata or {},
        "tags": list(s.tags or []),
        "power_state": getattr(s, "OS-EXT-STS:power_state", None),
        "vm_state": getattr(s, "OS-EXT-STS:vm_state", None),
        "task_state": getattr(s, "OS-EXT-STS:task_state", None),
    }
