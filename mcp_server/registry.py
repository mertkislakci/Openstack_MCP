"""
Tool Registry — lazy import + RBAC filtresi.

RBAC:
  Her tool manifest'inde "readonly" flag'i var.
  list_definitions(scope="readonly") ile sadece get_ tool'ları döner.
  list_definitions(scope="write") ile tüm tool'lar döner.
"""

from __future__ import annotations

import importlib
from typing import Any

import structlog

from mcp_server.tools.base import BaseTool

log = structlog.get_logger(__name__)

TOOL_MANIFEST: dict[str, str] = {
    # ── System ───────────────────────────────────────────────────────────────
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
    def __init__(self) -> None:
        self._loaded: dict[str, BaseTool] = {}
        self._loading: set[str] = set()

    def _load(self, name: str) -> BaseTool:
        if name in self._loaded:
            return self._loaded[name]
        if name not in TOOL_MANIFEST:
            raise KeyError(f"Unknown tool: {name!r}. Available: {list(TOOL_MANIFEST)}")
        if name in self._loading:
            raise RuntimeError(f"Circular import: {name!r}")

        self._loading.add(name)
        try:
            module_path, class_name = TOOL_MANIFEST[name].rsplit(":", 1)
            module = importlib.import_module(module_path)
            cls: type[BaseTool] = getattr(module, class_name)
            instance = cls()
            self._loaded[name] = instance
            log.debug("tool loaded", tool=name)
            return instance
        finally:
            self._loading.discard(name)

    def get(self, name: str) -> BaseTool:
        return self._load(name)

    def preload_all(self) -> None:
        for name in TOOL_MANIFEST:
            try:
                self._load(name)
            except Exception as exc:
                log.warning("preload failed", tool=name, error=str(exc))

    def list_definitions(
        self,
        scope: str = "write",  # "readonly" | "write"
    ) -> list[dict[str, Any]]:
        """
        scope="readonly" → sadece get_ tool'ları (read-only agent)
        scope="write"    → tüm tool'lar (tam yetkili agent)
        """
        defs = []
        for name in TOOL_MANIFEST:
            try:
                tool = self._load(name)
                if scope == "readonly" and not tool.is_read:
                    continue
                defs.append(tool.to_mcp_tool_definition())
            except Exception as exc:
                log.warning("definition skipped", tool=name, error=str(exc))
        return defs

    @property
    def names(self) -> list[str]:
        return list(TOOL_MANIFEST.keys())

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
