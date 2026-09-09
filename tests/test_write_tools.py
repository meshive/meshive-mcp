"""쓰기 도구 — confirm 게이트, 견적/요약 미리보기, SDK 호출 인자, 409 세분 매핑, logs."""
import json

import pytest
from meshive.exceptions import ConflictError, InsufficientCreditError

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name]


async def test_create_pod_without_confirm_only_estimates(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "gpu_model": "RTX 3060"}))
    assert body["confirmed"] is False and body["action"] == "create_pod"
    # 원본 숫자는 계산용으로 남기고, 사람에게 보일 문자열은 콘솔과 같은 3자리로 준다.
    assert body["estimate"]["price_per_hour"] == "0.068423"
    assert body["estimate"]["price_per_hour_display"] == "$0.068"
    assert "$0.068/hour" in body["question"]
    assert "confirm=true" in body["next_step"] and body["workspace"] == WS
    assert _calls(fake, "estimate_pod") and not _calls(fake, "create_pod")


async def test_create_pod_with_confirm_creates(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("create_pod", {
        "name": "p", "template_id": 457, "gpu_model": "RTX 3060", "gpu_count": 2, "rental_type": "spot",
        "volumes": [{"storage": "pv-a", "mount_path": "/data"}], "env": {"A": "1"}, "ports": [8888],
        "max_price_per_hour": 0.2, "confirm": True, "operation_id": "test-operation-0001"}))
    assert body["transaction_id"] == 14765 and body["pod_name"] is None and "Poll the pods tool" in body["next_step"]
    (_, args, kw), = _calls(fake, "create_pod")
    assert args == ("p", 457) and kw["workspace"] == WS and kw["gpu_count"] == 2 and kw["rental_type"] == "spot"
    assert kw["volumes"] == [{"storage": "pv-a", "mount_path": "/data"}] and kw["max_price_per_hour"] == 0.2
    assert "vcpu" not in kw   # None 인자는 SDK 기본값에 맡긴다


async def test_delete_pod_preview_then_delete(mcp_client, fake, with_key):
    preview = _payload(await mcp_client.call_tool("delete_pod", {"pod": "pod-3", "workspace": WS}))
    assert preview["confirmed"] is False and preview["pod"]["pod_name"] == "pod-3" and "cannot be undone" in preview["question"]
    assert not _calls(fake, "delete_pod")
    done = _payload(await mcp_client.call_tool("delete_pod", {"pod": "pod-3", "workspace": WS, "confirm": True, "operation_id": "test-operation-0001",
                                                              "delete_local_storages": ["pv-x"]}))
    assert done["action"] == "delete" and done["accepted"] is True
    (_, args, kw), = _calls(fake, "delete_pod")
    assert args == ("pod-3", WS) and kw["delete_local_storages"] == ["pv-x"]


async def test_lifecycle_tools(mcp_client, fake, with_key):
    for tool, method in (("stop_pod", "stop_pod"), ("restart_pod", "restart_pod")):
        body = _payload(await mcp_client.call_tool(tool, {"pod": "pod-1", "operation_id": "test-operation-0001"}))
        assert body["resource"] == "pod" and "asynchronously" in body["next_step"], tool
        (_, args, kw), = _calls(fake, method)
        assert args[:2] == ("pod-1", WS)


async def test_start_pod_needs_confirm_because_billing_resumes(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "any_node"}))
    assert prev["confirmed"] is False and prev["action"] == "start_pod" and prev["pod"]["pod_name"] == "pod-2"
    assert "Billing resumes at $0.500/hour" in prev["question"] and "confirm=true" in prev["next_step"]
    assert prev["pod"]["price_per_hour_display"] == "$0.500"
    assert not _calls(fake, "start_pod")
    done = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "any_node", "allow_data_loss": True, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["resource"] == "pod" and "asynchronously" in done["next_step"]
    (_, args, kw), = _calls(fake, "start_pod")
    assert args[:2] == ("pod-2", WS) and kw["placement"] == "any_node"


async def test_storage_tools(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "storage_type": "hostPath"}))
    assert prev["confirmed"] is False and prev["estimate"]["max_size_gb"] == 305 and "hostPath" in prev["question"]
    done = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["transaction_id"] == 5 and "storages tool" in done["next_step"]
    prev = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-1"}))
    assert prev["confirmed"] is False and prev["storage"]["linked_pods"] == ["p-1"] and "1 linked pod" in prev["question"]
    done = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-1", "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["resource"] == "storage" and done["action"] == "delete"


async def test_serving_tools(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 1.5,
                                                                  "max_replicas": 2}))
    assert prev["confirmed"] is False and prev["max_hourly_cost_usd"] == "3" and not _calls(fake, "deploy_serving")
    done = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 1.5,
                                                                  "max_replicas": 2, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["id"] == "42" and _calls(fake, "deploy_serving")[0][2]["price_cap_per_hour"] == 1.5
    # scale: 범위를 키우면 confirm 이 필요하고(과금 증가), 줄이거나 autoscale/cap 만 바꾸면 즉시 적용
    up = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "max_replicas": 4}))
    assert up["confirmed"] is False and up["requested"] == {"max_replicas": 4}
    assert "cost goes up" in up["question"] and not _calls(fake, "scale_serving")
    body = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "max_replicas": 4, "confirm": True, "operation_id": "test-operation-0001"}))
    assert body["action"] == "scale" and _calls(fake, "scale_serving")[0][2] == {"idempotency_key": "test-operation-0001", "min_replicas": None, "max_replicas": 4,
                                                                                    "autoscale": None, "price_cap_per_hour": None}
    # (fake 는 도구 호출마다 새 클라이언트 → _calls 는 마지막 호출의 기록만 본다)
    down = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "max_replicas": 2}))
    assert down["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["max_replicas"] == 2
    only_cap = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 0.9}))
    assert only_cap["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["price_cap_per_hour"] == 0.9
    assert _calls(fake, "get_serving")                # 비용 증가 여부는 현재 상태와 비교해 판정한다(리뷰 P2 #6)
    # pause 는 즉시, resume(paused=false) 은 confirm
    _payload(await mcp_client.call_tool("pause_serving", {"operation_id": "test-operation-0001", "serving": 42, "paused": True}))
    assert _calls(fake, "pause_serving")[0][2]["paused"] is True
    resume = _payload(await mcp_client.call_tool("pause_serving", {"operation_id": "test-operation-0001", "serving": 42, "paused": False}))
    assert resume["confirmed"] is False and "Billing resumes" in resume["question"] and not _calls(fake, "pause_serving")
    _payload(await mcp_client.call_tool("pause_serving", {"serving": 42, "paused": False, "confirm": True, "operation_id": "test-operation-0001"}))
    assert _calls(fake, "pause_serving")[0][2]["paused"] is False
    prev = _payload(await mcp_client.call_tool("delete_serving", {"serving": 42}))
    assert prev["confirmed"] is False and prev["serving"]["model_name"] == "llama"


async def test_task_tools(mcp_client, fake, with_key):
    est = _payload(await mcp_client.call_tool("estimate_task", {"name": "t", "script": "print(1)", "image": "img",
                                                                "gpu_model": "RTX 3060"}))
    assert est["max_cost"] == "0.068423" and "submit_task" in est["next_step"]
    prev = _payload(await mcp_client.call_tool("submit_task", {"name": "t", "script": "print(1)", "image": "img",
                                                               "gpu_model": "RTX 3060"}))
    # max cost 는 시간당이 아니라 합계 상한 → 콘솔 formatUsd 와 같이 2자리.
    assert prev["confirmed"] is False and "up to $0.07" in prev["question"] and not _calls(fake, "submit_task")
    done = _payload(await mcp_client.call_tool("submit_task", {"name": "t", "script": "print(1)", "image": "img",
                                                               "gpu_model": "RTX 3060", "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["task"]["task_id"] == "task_1" and "logs with task=" in done["next_step"]
    body = _payload(await mcp_client.call_tool("stop_task", {"operation_id": "test-operation-0001", "task": "task_1"}))
    assert body["resource"] == "task"


async def test_logs_tool(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("logs", {"pod": "pod-1", "tail": 50, "wait": 3}))
    assert body["text"] == "tick 1\ntick 2" and body["source"] == "live"
    assert _calls(fake, "get_pod_logs")[0][2] == {"tail": 50, "container": None, "wait": 3}
    body = _payload(await mcp_client.call_tool("logs", {"task": "task_9"}))
    assert body["task_id"] == "task_9" and body["text"] == "hello"
    both = await mcp_client.call_tool("logs", {"pod": "p", "task": "t"})
    assert both.is_error and _payload(both)["code"] == "invalid_argument"


async def test_logs_carry_an_untrusted_content_notice(mcp_client, fake, with_key):
    """로그는 사용자의 컨테이너가 찍은 임의 텍스트다 — 프롬프트 주입 경로(2026-09-09 리뷰 P2 #8).

    도구 설명은 호출 **전**에만 읽히므로 응답의 `note` 로도 같은 문장을 준다. 서버 instructions 는
    세션 전체에 걸린다. 셋 중 하나만 있으면 안 되므로 셋 다 고정한다."""
    from meshive_mcp.server import INSTRUCTIONS

    sources = {
        "pod logs note": _payload(await mcp_client.call_tool("logs", {"pod": "pod-1", "operation_id": "test-operation-0001"})).get("note"),
        "task logs note": _payload(await mcp_client.call_tool("logs", {"task": "task_9"})).get("note"),
        "tool description": {t.name: t.description for t in (await mcp_client.list_tools()).tools}["logs"],
        "server instructions": INSTRUCTIONS,
    }
    for where, text in sources.items():
        lowered = (text or "").lower()
        assert "untrusted" in lowered and "never follow" in lowered, f"{where}: {text!r}"


async def test_conflict_titles_map_to_codes(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(
        estimate_pod=ConflictError(409, "No machine can host 3× RTX 3060", title="No Capacity",
                                   raw={"detail": {"title": "No Capacity", "message": "x",
                                                   "availability": {"available_gpus": 2}}}))
    result = await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 1, "gpu_model": "RTX 3060", "gpu_count": 3})
    assert result.is_error
    body = _payload(result)
    assert body["code"] == "no_capacity" and body["availability"] == {"available_gpus": 2} and "Do not retry" in body["next_step"]


async def test_insufficient_credit(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(create_pod=InsufficientCreditError(402, "top up", title="Insufficient Credit"))
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "confirm": True, "operation_id": "test-operation-0001"})
    assert result.is_error and _payload(result)["code"] == "insufficient_credit"


async def test_write_tools_need_key(mcp_client, fake):
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "confirm": True, "operation_id": "test-operation-0001"})
    assert result.is_error and _payload(result)["code"] == "no_api_key"


async def test_pod_label_is_resolved_to_pod_name(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.pods.__setitem__(3, __import__("conftest")._pod("pod-3"))  # user_alias == "pod-3"
    body = _payload(await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "POD-3", "workspace": WS}))
    assert body["id"] == "pod-3" and _calls(fake, "list_pods")
    missing = await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "nope", "workspace": WS})
    assert missing.is_error and _payload(missing)["code"] == "not_found" and "pods" in _payload(missing)
    exact = _payload(await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "0123456789abcdef-0", "workspace": WS}))
    assert exact["id"] == "0123456789abcdef-0"


async def test_blank_pod_label_never_resolves_to_a_system_pod(mcp_client, fake, with_key):
    """회귀 지점: 빈/공백 `pod` 가 user_alias 가 빈 **시스템 downloader 파드**와 매치돼 stop/delete/logs 가
    그 파드로 나갔다(2026-09-09 리뷰 P3 #10). 인자 누락은 downloader 를 건드리기 전에 끊는다."""
    for blank in ("", "   "):
        result = await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": blank, "workspace": WS})
        assert result.is_error and _payload(result)["code"] == "invalid_argument", blank
        assert not _calls(fake, "stop_pod"), blank

    # downloader 는 라벨 매칭에서도 빠진다 — 라벨이 비어 있으니 후보로도 나오면 안 된다.
    missing = await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "dl-0-label", "workspace": WS})
    body = _payload(missing)
    assert missing.is_error and body["code"] == "not_found"
    assert all(not str(c["pod_name"]).startswith("dl-") for c in body["pods"]), body["pods"]


async def test_name_taken_tells_agent_to_check_the_list_first(mcp_client, fake, with_key):
    """응답을 못 받은 생성의 재시도가 409 Name Taken 으로 돌아오면 "다른 이름으로" 가 아니라 목록 확인을 먼저 시킨다."""
    fake["setup"] = lambda inst: inst.raise_on.update(
        create_pod=ConflictError(409, "A pod named 'p' already exists in this workspace.", title="Name Taken"))
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "confirm": True, "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "name_taken"
    assert "list tool" in body["next_step"] and "before creating anything else" in body["next_step"]


async def test_scale_serving_confirms_cap_raise_and_autoscale_on(mcp_client, fake, with_key):
    """replica 범위만이 아니라 상한 인상·autoscale 켜기도 비용이 늘 수 있다 — 같은 confirm 게이트(리뷰 P2 #6)."""
    from conftest import Serving
    capped = Serving.from_dict({"id": 42, "namespaceName": WS, "modelName": "llama", "framework": "vllm", "status": "active",
                                "currentReplicas": 2, "minReplicas": 1, "maxReplicas": 3, "autoScaleEnabled": False,
                                "priceCapPerHour": "1.5"})
    async def get_serving(self, sid):
        self._rec("get_serving", sid); return capped
    fake["setup"] = lambda inst: setattr(inst, "get_serving", get_serving.__get__(inst))
    raise_cap = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 2}))
    assert raise_cap["confirmed"] is False and raise_cap["requested"] == {"price_cap_per_hour": 2}
    assert "$2.000/hour per replica" in raise_cap["question"] and raise_cap["serving"]["price_cap_per_hour_display"] == "$1.500"
    assert not _calls(fake, "scale_serving")
    auto_on = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "autoscale": True}))
    assert auto_on["confirmed"] is False and "autoscale on" in auto_on["question"] and not _calls(fake, "scale_serving")
    lower = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 1, "autoscale": False}))
    assert lower["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["price_cap_per_hour"] == 1
    done = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 2, "confirm": True}))
    assert done["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["price_cap_per_hour"] == 2


async def test_logs_cursor_reads_external_task_increments(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("logs", {"task": "task_9", "cursor": 12}))
    assert body["task_id"] == "task_9" and _calls(fake, "get_task_logs")[0][2] == {"tail": 200, "wait": None, "cursor": 12}
    body = _payload(await mcp_client.call_tool("logs", {"task": "task_9"}))
    assert _calls(fake, "get_task_logs")[0][2]["cursor"] is None            # 생략 = 마지막 tail 줄
    bad = await mcp_client.call_tool("logs", {"pod": "pod-1", "cursor": 3})
    assert bad.is_error and _payload(bad)["code"] == "invalid_argument"


async def test_pod_tools_no_longer_take_disk_gb(mcp_client, fake, with_key):
    """시스템 디스크는 서버 공식으로 고정된다(리뷰 P2 #10) — 받는 척하던 인자를 스키마에서 뺀다."""
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        assert "disk_gb" not in tools[name].input_schema["properties"], name
    # 모델이 그래도 보내면 MCP 가 스키마 밖 인자를 버린다 — SDK 시그니처에도 없어 서버까지 갈 길이 없다.
    _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "disk_gb": 30, "confirm": True,
                                                       "operation_id": "test-operation-0001"}))
    assert "disk_gb" not in _calls(fake, "create_pod")[0][2]
