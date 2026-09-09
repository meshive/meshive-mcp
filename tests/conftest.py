"""테스트 공용: 가짜 SDK 클라이언트 + in-process MCP 클라이언트.

`meshive_mcp.client._new_client` 를 갈아끼워 네트워크 없이 도구 로직만 검증한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from mcp.client import Client
from meshive.models import (Credit, GpuAvailability, Logs, Pod, PodCreated, PodEstimate, ResourceAction, Serving,
                            Storage, StorageCreated, StorageEstimate, TaskEstimate, TaskSubmitted, WhoAmI, Workspace)

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

    # --- 쓰기 표면 (0.1.0) ---
    ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"},
                "resources": {"gpu_model": "RTX 3060", "vram_gb": 12, "gpu_count": 1, "vcpu": 4, "ram_gb": 12, "disk_gb": 25,
                              "rental_type": "demand"}, "availability": {"available_gpus": 2}, "template": {"id": 457},
                "volumes": [], "note": "Estimated"}

    async def estimate_pod(self, name, template_id, **kw):
        self._rec("estimate_pod", name, template_id, **kw); return PodEstimate.from_dict(self.ESTIMATE)

    async def create_pod(self, name, template_id, **kw):
        self._rec("create_pod", name, template_id, **kw)
        return PodCreated.from_dict({"name": name, "workspace": kw["workspace"], "transactionId": 14765, "estimate": self.ESTIMATE})

    async def _action(self, method, resource, ident, *a, **kw):
        self._rec(method, ident, *a, **kw)
        return ResourceAction.from_dict({"resource": resource, "id": ident, "action": method.split("_")[0], "result": 1})

    async def stop_pod(self, pod, ws, **kw): return await self._action("stop_pod", "pod", pod, ws, **kw)
    async def start_pod(self, pod, ws, **kw): return await self._action("start_pod", "pod", pod, ws, **kw)
    async def restart_pod(self, pod, ws, **kw): return await self._action("restart_pod", "pod", pod, ws, **kw)
    async def delete_pod(self, pod, ws, **kw): return await self._action("delete_pod", "pod", pod, ws, **kw)

    async def estimate_storage(self, name, size_gb, **kw):
        self._rec("estimate_storage", name, size_gb, **kw)
        return StorageEstimate.from_dict({"pricePerHourUsd": "0.000097", "pricePerGbMonthUsd": "0.07", "sizeGb": size_gb,
                                          "storageType": kw.get("storage_type", "nfs"), "maxSizeGb": 305})

    async def create_storage(self, name, size_gb, **kw):
        self._rec("create_storage", name, size_gb, **kw)
        return StorageCreated.from_dict({"name": name, "workspace": kw["workspace"], "transactionId": 5,
                                         "estimate": {"pricePerHourUsd": "0.000097", "pricePerGbMonthUsd": "0.07",
                                                      "sizeGb": size_gb, "storageType": "nfs", "maxSizeGb": 305}})

    async def get_storage(self, pv, ws):
        self._rec("get_storage", pv, ws)
        return Storage.from_dict({"pvName": pv, "namespaceName": ws, "userAlias": "data", "storageType": "nfs",
                                  "status": "running", "totalSize": 10.0, "linkedPod": [{"podName": "p-1"}]})

    async def delete_storage(self, pv, ws, **kw): return await self._action("delete_storage", "storage", pv, ws, **kw)

    async def deploy_serving(self, reg, **kw): return await self._action("deploy_serving", "serving", "42", reg, **kw)
    async def scale_serving(self, sid, **kw): return await self._action("scale_serving", "serving", str(sid), **kw)
    async def pause_serving(self, sid, **kw): return await self._action("pause_serving", "serving", str(sid), **kw)
    async def delete_serving(self, sid, **kw): return await self._action("delete_serving", "serving", str(sid), **kw)

    async def get_serving(self, sid):
        self._rec("get_serving", sid)
        return Serving.from_dict({"id": int(sid), "namespaceName": "0123456789abcdef", "modelName": "llama", "framework": "vllm",
                                  "status": "active", "currentReplicas": 2, "minReplicas": 1, "maxReplicas": 3})

    async def estimate_task(self, name, script, **kw):
        self._rec("estimate_task", name, script, **kw)
        return TaskEstimate.from_dict({"pricePerHourUsd": "0.068423", "maxCostUsd": "0.068423", "maxDurationS": 3600,
                                       "resources": {"gpu_count": 1}, "note": "ceiling"})

    async def submit_task(self, name, script, **kw):
        self._rec("submit_task", name, script, **kw)
        return TaskSubmitted.from_dict({"task": {"externalId": "task_1", "name": name, "namespaceName": kw["workspace"],
                                                "status": "queued", "podName": "task-1", "image": "img", "gpuCount": 1,
                                                "cpuCores": 4, "ramGb": 12, "pricePerHour": "0.068", "costSoFar": "0",
                                                "totalCost": "0"},
                                       "estimate": {"pricePerHourUsd": "0.068423", "maxCostUsd": "0.068423",
                                                    "maxDurationS": 3600, "resources": {}}})

    async def stop_task(self, tid, **kw): return await self._action("stop_task", "task", tid, **kw)

    async def get_pod_logs(self, pod, ws, **kw):
        self._rec("get_pod_logs", pod, ws, **kw)
        return Logs.from_dict({"podName": pod, "workspace": ws, "source": "live", "count": 2,
                               "lines": [{"line": "tick 1"}, {"line": "tick 2"}]})

    async def get_task_logs(self, tid, **kw):
        self._rec("get_task_logs", tid, **kw)
        return Logs.from_dict({"podName": "task-1", "workspace": "0123456789abcdef", "source": "live", "count": 1,
                               "lines": [{"line": "hello"}], "taskId": tid, "finished": False})

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
