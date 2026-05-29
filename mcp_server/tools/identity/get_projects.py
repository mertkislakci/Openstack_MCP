"""
GET tool — list all Keystone projects visible to the admin account.
Results are cached (TTL from config) to avoid hammering Keystone.
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from core.cache import cached
from core.openstack_client import get_admin_connection, list_sdk
from mcp_server.tools.base import BaseTool, ToolResult

log = structlog.get_logger(__name__)


class GetProjects(BaseTool):
    NAME: ClassVar[str] = "get_projects"
    DESCRIPTION: ClassVar[str] = (
        "List all OpenStack projects (tenants) visible to the admin account. "
        "Returns id, name, description, enabled status, and domain."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "enabled_only": {
                "type": "boolean",
                "description": "If true, return only enabled projects (default: true)",
                "default": True,
            },
            "domain_id": {
                "type": "string",
                "description": "Filter projects by domain ID (optional)",
            },
        },
        "required": [],
    }

    async def _run(
        self,
        enabled_only: bool = True,
        domain_id: str | None = None,
        **_: Any,
    ) -> ToolResult:
        projects = await _fetch_projects(enabled_only=enabled_only, domain_id=domain_id)
        return ToolResult(
            success=True,
            data=projects,
            metadata={"count": len(projects)},
        )


@cached(ttl=120, key_prefix="identity")
async def _fetch_projects(
    enabled_only: bool = True,
    domain_id: str | None = None,
) -> list[dict[str, Any]]:
    conn = await get_admin_connection()

    filters: dict[str, Any] = {}
    if enabled_only:
        filters["is_enabled"] = True
    if domain_id:
        filters["domain_id"] = domain_id

    raw = await list_sdk(conn.identity.projects(**filters))

    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description or "",
            "enabled": p.is_enabled,
            "domain_id": p.domain_id,
        }
        for p in raw
    ]
