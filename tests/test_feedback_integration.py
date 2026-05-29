"""
Feedback bağlantı testi:
  - Tool çalışınca metadata._feedback otomatik doluyor mu?
  - get_feedback tool'u doğru veri döndürüyor mu?
  - Auto-inject son 3 operasyonu gösteriyor mu?
"""
from __future__ import annotations
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from core.feedback import FeedbackBus, FeedbackEvent, ToolStatus, get_feedback_bus
from core.cache import get_cache


# ── get_feedback tool testi ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_feedback_context_mode():
    """get_feedback tool'u context mode'da LLM-ready string dönmeli."""
    # Önce bus'a birkaç event ekle
    bus = get_feedback_bus()
    await bus.clear()
    for tool in ["get_projects", "get_instances", "set_instance_delete"]:
        await bus.emit(FeedbackEvent(
            tool_name=tool,
            tool_type="get" if tool.startswith("get") else "set",
            status=ToolStatus.SUCCESS,
            duration_ms=100.0,
            inputs={},
            outputs={"ok": True},
        ))

    from mcp_server.tools.system.get_feedback import GetFeedback
    tool = GetFeedback()
    result = await tool._run(n=10, mode="context")

    assert result.success
    assert "get_projects" in result.data
    assert "get_instances" in result.data
    assert "set_instance_delete" in result.data
    assert "Son OpenStack" in result.data


@pytest.mark.asyncio
async def test_get_feedback_events_mode():
    """events mode ham JSON listesi döndürmeli."""
    bus = get_feedback_bus()
    await bus.clear()
    await bus.emit(FeedbackEvent(
        tool_name="get_instances",
        tool_type="get",
        status=ToolStatus.SUCCESS,
        duration_ms=200.0,
        inputs={"limit": 50},
        outputs=[{"id": "abc"}],
    ))

    from mcp_server.tools.system.get_feedback import GetFeedback
    tool = GetFeedback()
    result = await tool._run(n=5, mode="events")

    assert result.success
    assert isinstance(result.data, list)
    assert result.data[0]["tool_name"] == "get_instances"


@pytest.mark.asyncio
async def test_get_feedback_tool_filter():
    """tool_filter ile sadece ilgili tool'un eventleri gelmeli."""
    bus = get_feedback_bus()
    await bus.clear()
    for t in ["get_instances", "get_projects", "get_instances"]:
        await bus.emit(FeedbackEvent(
            tool_name=t, tool_type="get",
            status=ToolStatus.SUCCESS, duration_ms=50.0,
            inputs={}, outputs=None,
        ))

    from mcp_server.tools.system.get_feedback import GetFeedback
    tool = GetFeedback()
    result = await tool._run(n=10, mode="events", tool_filter="get_instances")

    assert result.success
    assert all(e["tool_name"] == "get_instances" for e in result.data)
    assert len(result.data) == 2


# ── Auto-inject testi ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_inject_in_metadata():
    """
    Her tool yanıtının metadata'sında _feedback anahtarı olmalı
    ve son 3 operasyonu içermeli.
    """
    from mcp_server.tools.system.get_feedback import GetFeedback
    bus = get_feedback_bus()
    await bus.clear()

    # get_feedback tool'u çalıştır — bu aynı zamanda kendi eventini emit eder
    tool = GetFeedback()
    result = await tool(n=5, mode="context")

    # metadata'da _feedback olmalı
    assert "_feedback" in result.metadata
    fb = result.metadata["_feedback"]
    assert isinstance(fb, list)
    # En az 1 event olmalı (get_feedback'in kendisi)
    assert len(fb) >= 1
    assert fb[0]["tool"] == "get_feedback"


@pytest.mark.asyncio
async def test_auto_inject_max_3():
    """_feedback listesi maksimum 3 item içermeli."""
    bus = get_feedback_bus()
    await bus.clear()

    # 5 event ekle
    for i in range(5):
        await bus.emit(FeedbackEvent(
            tool_name=f"get_instances",
            tool_type="get",
            status=ToolStatus.SUCCESS,
            duration_ms=float(i * 10),
            inputs={}, outputs=None,
        ))

    from mcp_server.tools.system.get_feedback import GetFeedback
    tool = GetFeedback()
    result = await tool(n=5, mode="context")

    # Auto-inject sadece son 3'ü almalı
    assert len(result.metadata["_feedback"]) <= 3
