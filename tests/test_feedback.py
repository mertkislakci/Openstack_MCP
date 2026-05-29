"""Tests for FeedbackBus."""

from __future__ import annotations

import pytest

from core.feedback import FeedbackBus, FeedbackEvent, ToolStatus


@pytest.fixture
def bus() -> FeedbackBus:
    return FeedbackBus(maxsize=10)


def make_event(tool: str = "get_instances", status: ToolStatus = ToolStatus.SUCCESS) -> FeedbackEvent:
    return FeedbackEvent(
        tool_name=tool,
        tool_type="get",
        status=status,
        duration_ms=42.0,
        inputs={"limit": 100},
        outputs=[{"id": "abc", "name": "vm1"}],
    )


@pytest.mark.asyncio
async def test_emit_and_recent(bus: FeedbackBus) -> None:
    ev = make_event()
    await bus.emit(ev)
    recent = await bus.get_recent(5)
    assert len(recent) == 1
    assert recent[0].tool_name == "get_instances"


@pytest.mark.asyncio
async def test_buffer_overflow(bus: FeedbackBus) -> None:
    for i in range(15):
        await bus.emit(make_event(tool=f"tool_{i}"))
    recent = await bus.get_recent(100)
    assert len(recent) == 10  # maxsize=10


@pytest.mark.asyncio
async def test_llm_context_format(bus: FeedbackBus) -> None:
    await bus.emit(make_event())
    ctx = await bus.get_llm_context(5)
    assert "get_instances" in ctx
    assert "SUCCESS" in ctx
    assert "42.0ms" in ctx


@pytest.mark.asyncio
async def test_subscriber_receives_event(bus: FeedbackBus) -> None:
    q = bus.subscribe()
    ev = make_event()
    await bus.emit(ev)
    received = q.get_nowait()
    assert received.tool_name == ev.tool_name
    bus.unsubscribe(q)
