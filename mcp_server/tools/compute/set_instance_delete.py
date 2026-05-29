"""
SET tool — delete a Nova instance.

Safety features
───────────────
  • dry_run mode — validates existence without deleting
  • confirm flag required (must be explicitly set to True)
  • Audit record written before deletion
  • Cache invalidated after successful deletion
  • force flag for instances stuck in ERROR/task state
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from core.cache import get_cache
from core.openstack_client import get_admin_connection, run_sdk
from mcp_server.tools.base import BaseTool, ToolResult

log = structlog.get_logger(__name__)


class SetInstanceDelete(BaseTool):
    NAME: ClassVar[str] = "set_instance_delete"
    DESCRIPTION: ClassVar[str] = (
        "Delete a Nova instance by UUID. "
        "Requires confirm=true to proceed. "
        "Use dry_run=true to validate without deleting. "
        "Set force=true for instances stuck in ERROR state."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "UUID of the instance to delete",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to actually delete. Safety gate.",
                "default": False,
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, validate existence only — do not delete.",
                "default": False,
            },
            "force": {
                "type": "boolean",
                "description": "Force-delete instances stuck in ERROR/task state.",
                "default": False,
            },
        },
        "required": ["instance_id"],
    }

    async def _run(
        self,
        instance_id: str,
        confirm: bool = False,
        dry_run: bool = False,
        force: bool = False,
        **_: Any,
    ) -> ToolResult:
        conn = await get_admin_connection()

        # ── Validate instance exists ──────────────────────────────────────────
        try:
            server = await run_sdk(conn.compute.get_server, instance_id)
        except Exception:
            return ToolResult(
                success=False,
                error=f"Instance {instance_id!r} not found or not accessible.",
            )

        instance_info = {
            "id": server.id,
            "name": server.name,
            "status": server.status,
            "project_id": server.project_id or getattr(server, "tenant_id", ""),
        }

        # ── Dry run — just report ─────────────────────────────────────────────
        if dry_run:
            return ToolResult(
                success=True,
                data={
                    "dry_run": True,
                    "would_delete": instance_info,
                    "message": "Dry run complete. Set confirm=true and dry_run=false to delete.",
                },
            )

        # ── Safety gate ───────────────────────────────────────────────────────
        if not confirm:
            return ToolResult(
                success=False,
                error=(
                    f"Deletion NOT performed. "
                    f"Set confirm=true to delete instance '{server.name}' ({instance_id}). "
                    f"Use dry_run=true first to inspect."
                ),
                data={"instance": instance_info},
            )

        # ── Delete ────────────────────────────────────────────────────────────
        log.warning(
            "DELETING instance",
            instance_id=instance_id,
            name=server.name,
            project_id=instance_info["project_id"],
            force=force,
        )

        try:
            if force:
                await run_sdk(conn.compute.force_delete_server, instance_id)
            else:
                await run_sdk(conn.compute.delete_server, instance_id)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Delete API call failed: {exc}",
                data={"instance": instance_info},
            )

        # ── Invalidate caches ─────────────────────────────────────────────────
        cache = get_cache()
        for key in await cache.keys():
            if "get_instances" in key or instance_id in key:
                await cache.delete(key)

        return ToolResult(
            success=True,
            data={
                "deleted": instance_info,
                "force": force,
                "message": f"Instance '{server.name}' ({instance_id}) deletion requested successfully.",
            },
        )
