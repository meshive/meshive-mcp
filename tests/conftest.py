"""테스트 공용: 가짜 SDK 클라이언트 + in-process MCP 클라이언트.

`meshive_mcp.client._new_client` 를 갈아끼워 네트워크 없이 도구 로직만 검증한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from mcp.client import Client
from meshive.models import Credit, GpuAvailability, Pod, WhoAmI, Workspace

from meshive_mcp import client as client_module
from meshive_mcp.server import create_server

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _ws(name: str, label: str) -> Workspace:
    return Workspace.from_dict({"namespaceName": name, "workspaceName": label})


def _pod(name: str, status: str = "running", *, downloader: bool = False) -> Pod:
    return Pod.from_dict({"podName": name, "namespaceName": "0123456789abcdef", "userAlias": "" if downloader else name,
                          "status": status, "rentalType": "demand", "pricePerHour": "0E-8" if downloader else "0.5",
                          "isMaintenance": False, "createdAt": NOW.isoformat(), "isDownloader": downloader})


class FakeMeshive:
    """AsyncMeshive 의 읽기 메서드 중 테스트가 쓰는 것만. 호출 기록을 남긴다."""

    def __init__(self, key: str, label: str) -> None:
        self.key = key
        self.label = label
        self.calls: list[tuple[str, tuple, dict]] = []
        self.workspaces = [_ws("0123456789abcdef", "research")]
        self.pods = [_pod(f"pod-{i}", "running" if i % 2 else "stopped") for i in range(45)]
        # 시스템 downloader 파드 2개 — 기본 숨김 대상
        self.pods += [_pod(f"dl-{i}", "running", downloader=True) for i in range(2)]
        self.raise_on: dict[str, BaseException] = {}

    def _rec(self, name: str, *a: Any, **kw: Any) -> None:
        self.calls.append((name, a, kw))
        if name in self.raise_on:
            raise self.raise_on[name]

    async def me(self):
        self._rec("me")
        return WhoAmI.from_dict({"email": "u@example.com", "name": "U"})

    async def get_credit(self):
        self._rec("get_credit")
        return Credit.from_dict({"creditBalance": 12.5, "paidBalance": 10, "bonusBalance": 2.5})

    async def list_workspaces(self):
        self._rec("list_workspaces")
        return self.workspaces

    async def list_pods(self, workspace):
        self._rec("list_pods", workspace)
        return self.pods

    async def get_pod(self, pod, workspace):
        self._rec("get_pod", pod, workspace)
        return _pod(pod)

    async def list_gpus(self, *, rental_type="demand", min_vram=None):
        self._rec("list_gpus", rental_type=rental_type, min_vram=min_vram)
        return [GpuAvailability.from_dict({"gpuModel": "RTX 5090", "vram": 32, "rentalType": rental_type,
                                           "gpuPrice": "0.6", "combinations": [{"maxGpu": 2}, {"maxGpu": 1}]})]

    async def close(self):
        self.calls.append(("close", (), {}))


@pytest.fixture
def fake(monkeypatch) -> FakeMeshive:
    holder: dict[str, FakeMeshive] = {}

    def factory(key: str, label: str) -> FakeMeshive:
        inst = FakeMeshive(key, label)
        # 테스트가 fake["setup"] 에 콜백을 두면 인스턴스마다 적용 (워크스페이스 수, 심어둘 예외 등).
        setup = holder.get("setup")
        if setup:
            setup(inst)  # type: ignore[operator]
        holder["last"] = inst
        return inst

    monkeypatch.setattr(client_module, "_new_client", factory)
    return holder  # type: ignore[return-value]


@pytest.fixture
def server():
    return create_server()


@pytest.fixture
def with_key(monkeypatch):
    """stdio 경로처럼 env 폴백으로 키를 준다 (in-process 클라이언트는 HTTP 헤더가 없다)."""
    from meshive_mcp.settings import settings
    monkeypatch.setattr(settings, "env_api_key_fallback", True)
    monkeypatch.setenv("MESHIVE_API_KEY", "meshive_" + "a" * 64)
    yield
    monkeypatch.setattr(settings, "env_api_key_fallback", False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def mcp_client(server):
    async with Client(server) as c:
        yield c
