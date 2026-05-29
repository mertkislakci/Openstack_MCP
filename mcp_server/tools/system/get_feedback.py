"""
GET tool — LLM'in son operasyonları okuması için.

LLM bu tool'u şu durumlarda çağırır:
  • "Son ne yaptım?" sorusuna cevap vermek için
  • Bir önceki operasyonun sonucunu doğrulamak için
  • Audit trail görmek için
  • Hata durumunda ne olduğunu anlamak için
"""
from __future__ import annotations
from typing import Any, ClassVar

from core.feedback import get_feedback_bus
from core.audit import get_audit_log
from mcp_server.tools.base import BaseTool, ToolResult


class GetFeedback(BaseTool):
    NAME: ClassVar[str] = "get_feedback"
    DESCRIPTION: ClassVar[str] = (
        "Son OpenStack operasyonlarının çıktısını ve durumunu göster. "
        "Bir önceki tool çağrısının sonucunu doğrulamak, "
        "hata nedenini anlamak veya operasyon geçmişini görmek için kullan. "
        "mode='context' ile LLM-ready özet, mode='events' ile ham JSON döner."
    )
    INPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Kaç operasyon gösterilsin (varsayılan: 10)",
                "default": 10,
                "minimum": 1,
                "maximum": 100,
            },
            "mode": {
                "type": "string",
                "description": "context = LLM-ready özet metin | events = ham JSON listesi | audit = sadece SET operasyonları",
                "enum": ["context", "events", "audit"],
                "default": "context",
            },
            "tool_filter": {
                "type": "string",
                "description": "Belirli bir tool adına göre filtrele, örn: 'get_instances' (opsiyonel)",
            },
        },
        "required": [],
    }

    async def _run(
        self,
        n: int = 10,
        mode: str = "context",
        tool_filter: str | None = None,
        **_: Any,
    ) -> ToolResult:

        if mode == "audit":
            records = await get_audit_log().get_all()
            data = [
                {
                    "timestamp": r.timestamp,
                    "tool": r.tool_name,
                    "action": r.action,
                    "resource_id": r.resource_id,
                    "project_id": r.project_id,
                    "result": r.result,
                    "error": r.error,
                }
                for r in records[-n:]
            ]
            if tool_filter:
                data = [d for d in data if tool_filter in d["tool"]]
            return ToolResult(
                success=True,
                data=data,
                metadata={"mode": "audit", "count": len(data)},
            )

        bus = get_feedback_bus()
        events = await bus.get_recent(n)

        if tool_filter:
            events = [e for e in events if tool_filter in e.tool_name]

        if mode == "context":
            lines = ["=== Son OpenStack Operasyonlari ==="]
            for ev in events:
                icon = "READ" if ev.tool_type == "get" else "WRITE"
                lines.append(
                    f"[{ev.timestamp}] {icon} {ev.tool_name} -> "
                    f"{ev.status.upper()} ({ev.duration_ms:.0f}ms)"
                )
                if ev.outputs is not None:
                    out = str(ev.outputs)
                    lines.append(f"  Cikti: {out[:300]}{'...' if len(out) > 300 else ''}")
                if ev.error:
                    lines.append(f"  HATA: {ev.error}")
            return ToolResult(
                success=True,
                data="\n".join(lines) if len(lines) > 1 else "Henuz operasyon yok.",
                metadata={"mode": "context", "count": len(events)},
            )

        # mode == "events"
        return ToolResult(
            success=True,
            data=[e.model_dump() for e in events],
            metadata={"mode": "events", "count": len(events)},
        )
