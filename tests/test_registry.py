"""Tests for ToolRegistry lazy loading."""

from __future__ import annotations

import pytest

from mcp_server.registry import TOOL_MANIFEST, ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


def test_list_names(registry: ToolRegistry) -> None:
    names = registry.names
    assert "get_instances" in names
    assert "set_instance_delete" in names
    assert "get_projects" in names


def test_lazy_load(registry: ToolRegistry) -> None:
    assert registry.loaded_count == 0
    tool = registry.get("get_projects")
    assert tool.NAME == "get_projects"
    assert registry.loaded_count == 1


def test_unknown_tool_raises(registry: ToolRegistry) -> None:
    with pytest.raises(KeyError, match="Unknown tool"):
        registry.get("nonexistent_tool")


def test_tool_definitions_have_required_fields(registry: ToolRegistry) -> None:
    defs = registry.list_definitions()
    for d in defs:
        assert "name" in d
        assert "description" in d
        assert "inputSchema" in d


def test_all_get_tools_start_with_get(registry: ToolRegistry) -> None:
    for name in TOOL_MANIFEST:
        assert name.startswith("get_") or name.startswith("set_"), \
            f"Tool '{name}' must start with get_ or set_"
