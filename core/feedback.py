"""
LLM Feedback System
═══════════════════
Every tool execution emits a structured FeedbackEvent that is:
  1. Stored in an async bounded queue (in-memory ring buffer)
  2. Exposed via /feedback HTTP endpoint (consumed by LLM context injection)
  3. Optionally streamed via SSE for real-time LLM awareness

The feedback JSON is designed to be directly injectable into an LLM system
prompt or tool-result payload so the model has full operational context.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, Field

from core.config import get_settings

log = structlog.get_logger(__name__)


class ToolStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"


class FeedbackEvent(BaseModel):
    """A single tool execution record emitted to the feedback bus."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    tool_name: str
    tool_type: str  # "get" | "set"
    status: ToolStatus
    duration_ms: float
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: Any = None  # The actual result (list, dict, str …)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_llm_context(self) -> str:
        """
        Render a compact, human-readable string suitable for
        injection into an LLM system prompt or tool result.
        """
        lines = [
            f"[{self.timestamp}] Tool: {self.tool_name} ({self.tool_type.upper()})",
            f"Status: {self.status.value.upper()}  |  Duration: {self.duration_ms:.1f}ms",
        ]
        if self.inputs:
            lines.append(f"Inputs: {self.inputs}")
        if self.outputs is not None:
            out_str = str(self.outputs)
            if len(out_str) > 500:
                out_str = out_str[:500] + "…"
            lines.append(f"Output: {out_str}")
        if self.error:
            lines.append(f"Error: {self.error}")
        return "\n".join(lines)


class FeedbackBus:
    """
    Async event bus for tool execution feedback.

    • Bounded asyncio.Queue acts as a ring buffer (FIFO, drops oldest when full)
    • Subscribers (SSE clients) receive a copy via fan-out queues
    • LLM can call get_recent() to obtain the last N events as context
    """

    def __init__(self, maxsize: int | None = None) -> None:
        cfg = get_settings()
        self._maxsize = maxsize or cfg.feedback_buffer_size
        self._events: list[FeedbackEvent] = []
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue[FeedbackEvent]] = []
        self._enabled = cfg.feedback_enabled

    async def emit(self, event: FeedbackEvent) -> None:
        if not self._enabled:
            return
        async with self._lock:
            self._events.append(event)
            if len(self._events) > self._maxsize:
                self._events.pop(0)

        # Fan-out to SSE subscribers (best-effort, non-blocking)
        dead: list[asyncio.Queue[FeedbackEvent]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            async with self._lock:
                for d in dead:
                    self._subscribers.remove(d)

        log.debug(
            "feedback emitted",
            tool=event.tool_name,
            status=event.status,
            duration_ms=event.duration_ms,
        )

    async def get_recent(self, n: int = 20) -> list[FeedbackEvent]:
        """Return the last n events (newest first)."""
        async with self._lock:
            return list(reversed(self._events[-n:]))

    async def get_llm_context(self, n: int = 10) -> str:
        """
        Return a formatted string of the last n events ready to inject
        into an LLM system prompt or assistant message.
        """
        events = await self.get_recent(n)
        if not events:
            return "No recent tool executions."
        parts = ["=== Recent OpenStack Operations ==="]
        for ev in events:
            parts.append(ev.to_llm_context())
            parts.append("─" * 40)
        return "\n".join(parts)

    def subscribe(self) -> asyncio.Queue[FeedbackEvent]:
        """Register an SSE subscriber queue."""
        q: asyncio.Queue[FeedbackEvent] = asyncio.Queue(maxsize=50)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[FeedbackEvent]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def clear(self) -> None:
        async with self._lock:
            self._events.clear()


# ── Module-level singleton ────────────────────────────────────────────────────

_bus: FeedbackBus | None = None


def get_feedback_bus() -> FeedbackBus:
    global _bus
    if _bus is None:
        _bus = FeedbackBus()
    return _bus
