"""
Tool Registry with lazy imports.

Tools are NOT imported at startup.  Each tool module is loaded via
importlib only when first accessed (or when explicitly pre-loaded).

This keeps startup fast and allows adding new tools without touching
the server entrypoint — just drop a file into tools/ and register
its import path here.
"""

from __future__ import annotations

import importlib
from typing import Any

import structlog

from mcp_server.tools.base import BaseTool

log = structlog.get_logger(__name__)


# ── Tool manifest ─────────────────────────────────────────────────────────────
# Format: "tool_name": "module.path:ClassName"
# Add new tools here only — server.py needs no changes.

TOOL_MANIFEST: dict[str, str] = {
    # ── System ───────────────────────────────────────────────────────────────
    # LLM bu tool'u kullanarak kendi operasyon geçmişini okur.
    "get_feedback":          "mcp_server.tools.system.get_feedback:GetFeedback",

    # ── Identity (Keystone) ──────────────────────────────────────────────────
    "get_projects":          "mcp_server.tools.identity.get_projects:GetProjects",

    # ── Compute (Nova) ───────────────────────────────────────────────────────
    "get_instances":         "mcp_server.tools.compute.get_instances:GetInstances",
    "get_instance_detail":   "mcp_server.tools.compute.get_instance_detail:GetInstanceDetail",
    "set_instance_delete":   "mcp_server.tools.compute.set_instance_delete:SetInstanceDelete",
    "set_instance_action":   "mcp_server.tools.compute.set_instance_action:SetInstanceAction",
}


class ToolRegistry:
    """
    Lazy-loading tool registry.

    _loaded  → already-instantiated tool objects
    _loading → set of names currently being imported (re-entrancy guard)
    """

    def __init__(self) -> None:
        self._loaded: dict[str, BaseTool] = {}
        self._loading: set[str] = set()

    def _load(self, name: str) -> BaseTool:
        if name in self._loaded:
            return self._loaded[name]

        if name not in TOOL_MANIFEST:
            raise KeyError(f"Unknown tool: {name!r}. Available: {list(TOOL_MANIFEST)}")

        if name in self._loading:
            raise RuntimeError(f"Circular import detected while loading tool: {name!r}")

        self._loading.add(name)
        try:
            module_path, class_name = TOOL_MANIFEST[name].rsplit(":", 1)
            module = importlib.import_module(module_path)
            cls: type[BaseTool] = getattr(module, class_name)
            instance = cls()
            self._loaded[name] = instance
            log.debug("tool loaded", tool=name, module=module_path)
            return instance
        finally:
            self._loading.discard(name)

    def get(self, name: str) -> BaseTool:
        return self._load(name)

    def preload_all(self) -> None:
        """Eagerly load all tools — useful for warm-up / health check."""
        for name in TOOL_MANIFEST:
            try:
                self._load(name)
            except Exception as exc:
                log.warning("failed to preload tool", tool=name, error=str(exc))

    def list_definitions(self) -> list[dict[str, Any]]:
        """Return MCP tool-definition dicts for all registered tools."""
        defs = []
        for name in TOOL_MANIFEST:
            try:
                tool = self._load(name)
                defs.append(tool.to_mcp_tool_definition())
            except Exception as exc:
                log.warning("skipping tool definition", tool=name, error=str(exc))
        return defs

    @property
    def names(self) -> list[str]:
        return list(TOOL_MANIFEST.keys())

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)


# ── Module-level singleton ────────────────────────────────────────────────────

_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
