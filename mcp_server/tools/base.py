"""
Base class for all MCP tools.

Conventions
───────────
  • Tool name must start with  get_  (read)  or  set_  (write)
  • Every tool returns  ToolResult
  • Execution is measured and emitted to FeedbackBus automatically
  • SET tools additionally write an AuditRecord
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from core.audit import AuditRecord, get_audit_log
from core.feedback import FeedbackBus, FeedbackEvent, ToolStatus, get_feedback_bus

log = structlog.get_logger(__name__)


# ── Result model ─────────────────────────────────────────────────────────────

class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = {}

    def to_mcp_text(self) -> str:
        """Serialize to a JSON string for MCP TextContent."""
        import json
        if self.success:
            return json.dumps({"ok": True, "data": self.data, "meta": self.metadata}, default=str, indent=2)
        return json.dumps({"ok": False, "error": self.error}, indent=2)


# ── Base tool ─────────────────────────────────────────────────────────────────

class BaseTool(ABC):
    """
    Abstract base for all OpenStack MCP tools.

    Subclass and implement:
        NAME        = "get_instances"        (must start with get_ or set_)
        DESCRIPTION = "List all instances …"
        INPUT_SCHEMA = { "type": "object", "properties": {…} }
        async def _run(self, **kwargs) -> ToolResult
    """

    NAME: ClassVar[str]
    DESCRIPTION: ClassVar[str]
    INPUT_SCHEMA: ClassVar[dict[str, Any]]

    @property
    def is_read(self) -> bool:
        return self.NAME.startswith("get_")

    @property
    def is_write(self) -> bool:
        return self.NAME.startswith("set_")

    @abstractmethod
    async def _run(self, **kwargs: Any) -> ToolResult:
        """Implement the actual tool logic here."""
        ...

    async def __call__(self, **kwargs: Any) -> ToolResult:
        """
        Wraps _run() with:
          - timing
          - feedback emission      ← her tool yanıtına son operasyon özeti eklenir
          - audit record (SET only)
          - error capture
        """
        bus: FeedbackBus = get_feedback_bus()
        audit_id: str | None = None

        # Pre-flight audit for write operations
        if self.is_write:
            rec = AuditRecord(
                tool_name=self.NAME,
                action=self.NAME.removeprefix("set_"),
                inputs=kwargs,
                resource_id=kwargs.get("instance_id") or kwargs.get("resource_id"),
                project_id=kwargs.get("project_id"),
            )
            audit_rec = await get_audit_log().record(rec)
            audit_id = audit_rec.record_id

        start = time.monotonic()
        result: ToolResult
        try:
            result = await self._run(**kwargs)
            status = ToolStatus.SUCCESS if result.success else ToolStatus.ERROR
        except Exception as exc:
            log.exception("tool execution failed", tool=self.NAME)
            result = ToolResult(success=False, error=str(exc))
            status = ToolStatus.ERROR
        finally:
            duration_ms = (time.monotonic() - start) * 1000

        # Update audit
        if audit_id:
            await get_audit_log().update_result(
                audit_id,
                result="success" if result.success else "failed",
                error=result.error,
            )

        # Emit to feedback bus
        event = FeedbackEvent(
            tool_name=self.NAME,
            tool_type="get" if self.is_read else "set",
            status=status,
            duration_ms=duration_ms,
            inputs=kwargs,
            outputs=result.data,
            error=result.error,
        )
        await bus.emit(event)

        # ── Auto-inject: son 3 operasyonu metadata'ya ekle ──────────────────
        # LLM her tool yanıtıyla birlikte operasyon geçmişini görür.
        # get_feedback tool'u çağırmak zorunda kalmaz.
        recent = await bus.get_recent(3)
        result.metadata["_feedback"] = [
            {
                "tool": e.tool_name,
                "status": e.status,
                "duration_ms": round(e.duration_ms, 1),
                "timestamp": e.timestamp,
                "error": e.error,
            }
            for e in recent
        ]

        log.info(
            "tool executed",
            tool=self.NAME,
            status=status,
            duration_ms=round(duration_ms, 1),
        )
        return result

    def to_mcp_tool_definition(self) -> dict[str, Any]:
        """Return MCP-compatible tool definition dict."""
        return {
            "name": self.NAME,
            "description": self.DESCRIPTION,
            "inputSchema": self.INPUT_SCHEMA,
        }
