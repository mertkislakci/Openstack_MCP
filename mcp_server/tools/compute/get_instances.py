"""
GET tool — list ALL instances across ALL projects.

Strategy
────────
1. Fetch all projects via get_projects (cached)
2. Query Nova with all_tenants=True from the admin connection
   (single API call — more efficient than per-project queries)
3. Enrich each instance with project name (joined from project list)
4. Whole result set is cached with project-agnostic TTL

Supports filtering by:
  - project_id / project_name
  - status  (ACTIVE, SHUTOFF, ERROR, …)
  - host    (hypervisor hostname)
  - name_pattern (substring match, case-insensitive)
"""

from __future__ import annotations

import fnmatch
from typing import Any, ClassVar

import structlog

from core.cache import cached
from core.openstack_client import get_admin_connection, list_sdk
from mcp_server.tools.base import BaseTool, ToolResult

log = structlog.get_logger(__name__)


class GetInstances(BaseTool):
    NAME: ClassVar[str] = "get_instances"
    DESCRIPTION: ClassVar[str] = (
        "List all Nova instances across all OpenStack projects. "
        "Supports filtering by project, status, host, and name pattern. "
        "Results include: id, name, status, project, flavor, image, IPs, host, created_at."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Filter by project UUID (optional)",
            },
            "project_name": {
                "type": "string",
                "description": "Filter by project name — case-insensitive substring match (optional)",
            },
            "status": {
                "type": "string",
                "description": "Filter by instance status: ACTIVE | SHUTOFF | ERROR | BUILD | …",
                "enum": ["ACTIVE", "SHUTOFF", "ERROR", "BUILD", "PAUSED", "SUSPENDED", "DELETED"],
            },
            "host": {
                "type": "string",
                "description": "Filter by hypervisor host name (optional)",
            },
            "name_pattern": {
                "type": "string",
                "description": "Glob/substring filter on instance name, e.g. 'web-*' (optional)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of instances to return (default: 500)",
                "default": 500,
                "minimum": 1,
                "maximum": 5000,
            },
        },
        "required": [],
    }

    async def _run(
        self,
        project_id: str | None = None,
        project_name: str | None = None,
        status: str | None = None,
        host: str | None = None,
        name_pattern: str | None = None,
        limit: int = 500,
        **_: Any,
    ) -> ToolResult:
        instances = await _fetch_all_instances()

        # ── Client-side filtering ─────────────────────────────────────────────
        if project_id:
            instances = [i for i in instances if i["project_id"] == project_id]

        if project_name:
            pn_lower = project_name.lower()
            instances = [i for i in instances if pn_lower in i["project_name"].lower()]

        if status:
            instances = [i for i in instances if i["status"].upper() == status.upper()]

        if host:
            instances = [i for i in instances if i.get("host", "").lower() == host.lower()]

        if name_pattern:
            instances = [
                i for i in instances
                if fnmatch.fnmatch(i["name"].lower(), name_pattern.lower())
                or name_pattern.lower() in i["name"].lower()
            ]

        total = len(instances)
        instances = instances[:limit]

        return ToolResult(
            success=True,
            data=instances,
            metadata={
                "total_matched": total,
                "returned": len(instances),
                "limit": limit,
            },
        )


@cached(ttl=60, key_prefix="compute")
async def _fetch_all_instances() -> list[dict[str, Any]]:
    """
    Single admin call with all_tenants=True — fetches every instance
    across every project in one request.  Cached for 60 s.
    """
    conn = await get_admin_connection()

    # Fetch projects for name enrichment
    from mcp_server.tools.identity.get_projects import _fetch_projects  # lazy import
    projects = await _fetch_projects()
    project_map: dict[str, str] = {p["id"]: p["name"] for p in projects}

    # Nova all_tenants
    raw = await list_sdk(
        conn.compute.servers(all_tenants=True, details=True)
    )

    result: list[dict[str, Any]] = []
    for s in raw:
        # Extract IP addresses
        ips: list[str] = []
        for net_addrs in (s.addresses or {}).values():
            for addr in net_addrs:
                ips.append(addr.get("addr", ""))

        project_id = s.project_id or s.tenant_id or ""
        result.append({
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "project_id": project_id,
            "project_name": project_map.get(project_id, project_id),
            "user_id": s.user_id,
            "flavor": _extract_flavor(s),
            "image": _extract_image(s),
            "ip_addresses": ips,
            "host": getattr(s, "hypervisor_hostname", None) or getattr(s, "OS-EXT-SRV-ATTR:host", None),
            "availability_zone": getattr(s, "availability_zone", None),
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "key_name": s.key_name,
            "power_state": getattr(s, "OS-EXT-STS:power_state", None),
            "task_state": getattr(s, "OS-EXT-STS:task_state", None),
            "vm_state": getattr(s, "OS-EXT-STS:vm_state", None),
            "metadata": s.metadata or {},
        })

    log.info("fetched all instances", count=len(result))
    return result


def _extract_flavor(server: Any) -> dict[str, Any]:
    flavor = getattr(server, "flavor", {}) or {}
    if hasattr(flavor, "id"):
        return {"id": flavor.id, "name": getattr(flavor, "original_name", flavor.id)}
    if isinstance(flavor, dict):
        return {"id": flavor.get("id", ""), "name": flavor.get("original_name", flavor.get("id", ""))}
    return {}


def _extract_image(server: Any) -> dict[str, Any]:
    image = getattr(server, "image", {}) or {}
    if hasattr(image, "id"):
        return {"id": image.id, "name": getattr(image, "name", image.id)}
    if isinstance(image, dict):
        return {"id": image.get("id", ""), "name": image.get("name", "")}
    return {}
