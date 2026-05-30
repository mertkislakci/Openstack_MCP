"""
BaseTool — tüm MCP tool'larının temel sınıfı.

Yeni özellikler:
  • TIMEOUT_SECONDS — her tool başına configürasyonlu timeout
  • Prometheus metrik entegrasyonu
  • RBAC readonly flag
  • Feedback auto-inject (son 3 operasyon metadata'ya eklenir)
  • Audit log (SET tool'ları için)
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from core.audit import AuditRecord, get_audit_log
from core.feedback import FeedbackBus, FeedbackEvent, ToolStatus, get_feedback_bus
from core.metrics import tool_calls_total, tool_duration_seconds

log = structlog.get_logger(__name__)


# ── Result ────────────────────────────────────────────────────────────────────

class ToolResult(BaseModel):
    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = {}

    def to_mcp_text(self) -> str:
        import json
        if self.success:
            return json.dumps(
                {"ok": True, "data": self.data, "meta": self.metadata},
                default=str, indent=2,
            )
        return json.dumps({"ok": False, "error": self.error}, indent=2)


# ── BaseTool ──────────────────────────────────────────────────────────────────

class BaseTool(ABC):
    """
    Tüm OpenStack MCP tool'larının base class'ı.

    Zorunlu class değişkenleri:
        NAME         = "get_instances"   (get_ veya set_ ile başlamalı)
        DESCRIPTION  = "..."
        INPUT_SCHEMA = { "type": "object", "properties": {...} }

    Opsiyonel:
        TIMEOUT_SECONDS = 30.0     (varsayılan)
        READONLY        = True     (get_ için otomatik True)
    """

    NAME: ClassVar[str]
    DESCRIPTION: ClassVar[str]
    INPUT_SCHEMA: ClassVar[dict[str, Any]]
    TIMEOUT_SECONDS: ClassVar[float] = 30.0
    READONLY: ClassVar[bool | None] = None   # None → NAME prefix'inden çıkarılır

    @property
    def is_read(self) -> bool:
        if self.READONLY is not None:
            return self.READONLY
        return self.NAME.startswith("get_")

    @property
    def is_write(self) -> bool:
        return not self.is_read

    @abstractmethod
    async def _run(self, **kwargs: Any) -> ToolResult: ...

    async def __call__(self, **kwargs: Any) -> ToolResult:
        """
        _run() çevresinde:
          • Timeout (TIMEOUT_SECONDS)
          • Prometheus metrik
          • Feedback emit + auto-inject
          • Audit (SET tool'ları)
          • Hata yakalama
        """
        bus: FeedbackBus = get_feedback_bus()
        audit_id: str | None = None

        # SET tool'ları için ön audit kaydı
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
        status = ToolStatus.ERROR

        try:
            result = await asyncio.wait_for(
                self._run(**kwargs),
                timeout=self.TIMEOUT_SECONDS,
            )
            status = ToolStatus.SUCCESS if result.success else ToolStatus.ERROR

        except asyncio.TimeoutError:
            result = ToolResult(
                success=False,
                error=f"Timeout: {self.NAME} {self.TIMEOUT_SECONDS}s içinde tamamlanamadı.",
            )
            log.error("tool timeout", tool=self.NAME, timeout=self.TIMEOUT_SECONDS)

        except Exception as exc:
            log.exception("tool execution failed", tool=self.NAME)
            result = ToolResult(success=False, error=str(exc))

        finally:
            duration_ms = (time.monotonic() - start) * 1000

        # Audit güncelle
        if audit_id:
            await get_audit_log().update_result(
                audit_id,
                result="success" if result.success else "failed",
                error=result.error,
            )

        # Prometheus
        tool_type = "get" if self.is_read else "set"
        tool_calls_total.labels(
            tool=self.NAME,
            tool_type=tool_type,
            status=status.value,
        ).inc()
        tool_duration_seconds.labels(tool=self.NAME).observe(duration_ms / 1000)

        # Feedback emit
        event = FeedbackEvent(
            tool_name=self.NAME,
            tool_type=tool_type,
            status=status,
            duration_ms=duration_ms,
            inputs=kwargs,
            outputs=result.data,
            error=result.error,
        )
        await bus.emit(event)

        # Auto-inject: son 3 operasyonu metadata'ya ekle
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
        return {
            "name": self.NAME,
            "description": self.DESCRIPTION,
            "inputSchema": self.INPUT_SCHEMA,
            "readonly": self.is_read,
        }
