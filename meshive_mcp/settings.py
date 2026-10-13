"""Environment-variable settings. One per server process.

MESHIVE_BASE_URL          backend URL. Empty = the SDK default (production).
MESHIVE_MCP_HOST/PORT     HTTP transport binding (default 127.0.0.1:8080).
MESHIVE_MCP_ALLOWED_HOSTS comma-separated Host header allowlist. Empty turns off DNS rebinding protection
                          (behind the ingress, Host is the public domain, so the SDK's default check blocks requests).
MESHIVE_API_KEY           fallback key read only in stdio (development) mode. Ignored in HTTP mode —
                          a shared server must not handle every request with one key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_csv(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


@dataclass
class Settings:
    base_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080
    allowed_hosts: list[str] = field(default_factory=list)
    # Turned on only when __main__ starts with stdio. Never on for the HTTP server.
    env_api_key_fallback: bool = False
    request_timeout: float = 30.0
    # Cap for list tools. Full lists can overflow the context window and kill the session.
    default_limit: int = 20
    max_limit: int = 100

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            base_url=os.getenv("MESHIVE_BASE_URL") or None,
            host=os.getenv("MESHIVE_MCP_HOST", "127.0.0.1"),
            port=int(os.getenv("MESHIVE_MCP_PORT", "8080")),
            allowed_hosts=_split_csv(os.getenv("MESHIVE_MCP_ALLOWED_HOSTS")),
        )


settings = Settings.from_env()
