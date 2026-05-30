"""
Centralized configuration via pydantic-settings.
Single source of truth — read once, cached forever.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── OpenStack ──────────────────────────────────────────────────────────────
    os_auth_url: str = Field(..., description="Keystone auth URL")
    os_username: str = Field(..., description="OpenStack username")
    os_password: str = Field(..., description="OpenStack password")
    os_project_name: str = Field("admin", description="Default project name")
    os_user_domain_name: str = Field("Default")
    os_project_domain_name: str = Field("Default")
    os_region_name: str = Field("RegionOne")

    # ── MCP Server ────────────────────────────────────────────────────────────
    mcp_host: str = Field("0.0.0.0")
    mcp_port: int = Field(8080, ge=1024, le=65535)
    mcp_transport: str = Field("streamable-http")  # streamable-http | stdio | sse (legacy)
    # Bearer token for streamable-http auth. Empty = auth disabled.
    mcp_auth_token: str = Field("", description="Bearer token for HTTP transports")

    # ── MCP Proxy ─────────────────────────────────────────────────────────────
    proxy_host: str = Field("0.0.0.0")
    proxy_port: int = Field(8000, ge=1024, le=65535)
    proxy_upstream_urls: list[str] = Field(
        default_factory=lambda: ["http://localhost:8080"],
        description="Upstream MCP server URLs",
    )

    # ── Cache ─────────────────────────────────────────────────────────────────
    cache_ttl: Annotated[int, Field(ge=1)] = 300
    cache_max_size: Annotated[int, Field(ge=10)] = 1000

    # ── Feedback ──────────────────────────────────────────────────────────────
    feedback_enabled: bool = True
    feedback_buffer_size: Annotated[int, Field(ge=10)] = 200

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field("INFO")

    @field_validator("proxy_upstream_urls", mode="before")
    @classmethod
    def split_urls(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [u.strip() for u in v.split(",") if u.strip()]
        return v

    @field_validator("log_level")
    @classmethod
    def upper_log_level(cls, v: str) -> str:
        return v.upper()

    @property
    def os_auth_dict(self) -> dict[str, str]:
        """Ready-to-use dict for openstack SDK connect()."""
        return {
            "auth_url": self.os_auth_url,
            "username": self.os_username,
            "password": self.os_password,
            "project_name": self.os_project_name,
            "user_domain_name": self.os_user_domain_name,
            "project_domain_name": self.os_project_domain_name,
            "verify": False,
            "insecure":True,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached singleton Settings instance."""
    return Settings()
