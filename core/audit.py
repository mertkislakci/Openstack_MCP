"""
Audit log — immutable append-only record of every SET (write) operation.
Stored in memory with optional file persistence.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)


class AuditRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    tool_name: str
    operator: str = "llm-agent"      # who triggered the action
    project_id: str | None = None
    resource_type: str | None = None  # instance, volume, …
    resource_id: str | None = None
    action: str                        # delete, stop, start, …
    inputs: dict[str, Any] = Field(default_factory=dict)
    result: str = "pending"           # pending | success | failed
    error: str | None = None


class AuditLog:
    def __init__(self, persist_path: str | None = None) -> None:
        self._records: list[AuditRecord] = []
        self._lock = asyncio.Lock()
        self._path = Path(persist_path) if persist_path else None

    async def record(self, rec: AuditRecord) -> AuditRecord:
        async with self._lock:
            self._records.append(rec)
            if self._path:
                await self._append_to_file(rec)
        log.info("audit", tool=rec.tool_name, action=rec.action, resource=rec.resource_id)
        return rec

    async def update_result(self, record_id: str, result: str, error: str | None = None) -> None:
        async with self._lock:
            for r in self._records:
                if r.record_id == record_id:
                    r.result = result
                    r.error = error
                    break

    async def get_all(self) -> list[AuditRecord]:
        async with self._lock:
            return list(self._records)

    async def _append_to_file(self, rec: AuditRecord) -> None:
        try:
            with self._path.open("a") as f:  # type: ignore[union-attr]
                f.write(rec.model_dump_json() + "\n")
        except OSError as e:
            log.warning("audit file write failed", error=str(e))


_audit: AuditLog | None = None


def get_audit_log() -> AuditLog:
    global _audit
    if _audit is None:
        _audit = AuditLog()
    return _audit
