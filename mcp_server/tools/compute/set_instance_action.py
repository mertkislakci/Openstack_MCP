"""
SET tool — perform power actions on a Nova instance.
Actions: start | stop | reboot (soft/hard) | pause | unpause | suspend | resume
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from core.cache import get_cache
from core.openstack_client import get_admin_connection, run_sdk
from mcp_server.tools.base import BaseTool, ToolResult

log = structlog.get_logger(__name__)

VALID_ACTIONS = {
    "start": "start_server",
    "stop": "stop_server",
    "reboot_soft": "reboot_server",
    "reboot_hard": "reboot_server",
    "pause": "pause_server",
    "unpause": "unpause_server",
    "suspend": "suspend_server",
    "resume": "resume_server",
}


class SetInstanceAction(BaseTool):
    NAME: ClassVar[str] = "set_instance_action"
    DESCRIPTION: ClassVar[str] = (
        "Perform a power action on a Nova instance: "
        "start, stop, reboot_soft, reboot_hard, pause, unpause, suspend, resume."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "UUID of the instance",
            },
            "action": {
                "type": "string",
                "description": "Action to perform",
                "enum": list(VALID_ACTIONS.keys()),
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to proceed with destructive actions (stop, reboot_hard).",
                "default": False,
            },
        },
        "required": ["instance_id", "action"],
    }

    DESTRUCTIVE = {"stop", "reboot_hard", "suspend"}

    async def _run(
        self,
        instance_id: str,
        action: str,
        confirm: bool = False,
        **_: Any,
    ) -> ToolResult:
        if action not in VALID_ACTIONS:
            return ToolResult(
                success=False,
                error=f"Unknown action '{action}'. Valid: {list(VALID_ACTIONS)}",
            )

        if action in self.DESTRUCTIVE and not confirm:
            return ToolResult(
                success=False,
                error=(
                    f"Action '{action}' requires confirm=true. "
                    "This operation may affect running workloads."
                ),
            )

        conn = await get_admin_connection()

        # Verify instance exists
        try:
            server = await run_sdk(conn.compute.get_server, instance_id)
        except Exception:
            return ToolResult(success=False, error=f"Instance {instance_id!r} not found.")

        sdk_method_name = VALID_ACTIONS[action]
        sdk_method = getattr(conn.compute, sdk_method_name)

        log.warning(
            "instance action",
            action=action,
            instance_id=instance_id,
            name=server.name,
        )

        try:
            kwargs: dict[str, Any] = {"server": instance_id}
            if action in ("reboot_soft", "reboot_hard"):
                kwargs["reboot_type"] = "HARD" if action == "reboot_hard" else "SOFT"
            await run_sdk(sdk_method, **kwargs)
        except Exception as exc:
            return ToolResult(success=False, error=f"Action failed: {exc}")

        # Invalidate instance caches
        cache = get_cache()
        for key in await cache.keys():
            if instance_id in key or "get_instances" in key:
                await cache.delete(key)

        return ToolResult(
            success=True,
            data={
                "instance_id": instance_id,
                "instance_name": server.name,
                "action": action,
                "message": f"Action '{action}' dispatched to instance '{server.name}'.",
            },
        )
