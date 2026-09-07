"""환경변수 기반 설정. 서버 프로세스 전체에 하나.

MESHIVE_BASE_URL          백엔드 주소. 비우면 SDK 기본값(프로덕션).
MESHIVE_MCP_HOST/PORT     HTTP transport 바인딩 (기본 127.0.0.1:8080).
MESHIVE_MCP_ALLOWED_HOSTS 콤마 구분 Host 헤더 allowlist. 비우면 DNS rebinding 보호를 끈다
                          (ingress 뒤에서는 Host 가 공인 도메인이라 SDK 기본 검사가 요청을 막는다).
MESHIVE_API_KEY           stdio(개발) 모드에서만 읽는 폴백 키. HTTP 모드에서는 무시 —
                          공용 서버가 한 키로 모든 요청을 처리하면 안 되기 때문.
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
    # __main__ 이 stdio 로 뜰 때만 True 로 켠다. HTTP 서버에서는 절대 켜지 않는다.
    env_api_key_fallback: bool = False
    request_timeout: float = 30.0
    # 목록 도구 캡. RunPod 사례: 전체 목록이 컨텍스트 윈도우를 넘겨 세션이 죽는다.
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
