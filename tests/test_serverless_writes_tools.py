"""Serving and task writes: scale_serving output bucket and sharing consent, deploy_serving availability, models
official catalog and availability, register_model takes_image, submit_task project / registry login / output bucket,
delete_tasks, cancel_task_upload, gpus for tasks and task counts."""
import json

import pytest
from meshive import SHARING_CONSENT
from meshive.exceptions import ConflictError, MeshiveAPIError, NotFoundError, PermissionDeniedError
from meshive.models import Serving, StorageCredential, Task

from conftest import FakeMeshive

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"
OP = "op-serverless-0001"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name] if "last" in fake else []


def _api_error(cls, status, title, message="m", **detail):
    return cls(status, message, title=title, raw={"detail": {"title": title, "message": message, **detail}})


def _serving(**over):
    d = {"id": 42, "namespaceName": WS, "modelName": "llama", "framework": "vllm", "status": "active",
         "currentReplicas": 2, "minReplicas": 1, "maxReplicas": 3, "priceCapPerHour": "1.0"}
    d.update(over)
    return d


@pytest.fixture
def serving_state(monkeypatch):
    state = {"serving": _serving(), "buckets": [{"credentialId": 7, "label": "results", "provider": "r2",
                                                 "bucket": "my-results", "verifiedAt": "2026-11-01T00:00:00Z"}]}

    async def get_serving(self, sid):
        self._rec("get_serving", sid)
        return Serving.from_dict({**state["serving"], "id": int(sid)})

    async def list_storage_credentials(self, ws, **kw):
        self._rec("list_storage_credentials", ws, **kw)
        return [StorageCredential.from_dict(b) for b in state["buckets"]]

    monkeypatch.setattr(FakeMeshive, "get_serving", get_serving)
    monkeypatch.setattr(FakeMeshive, "list_storage_credentials", list_storage_credentials, raising=False)
    return state


# --- scale_serving ---------------------------------------------------------------------------------------------------

async def test_scale_output_bucket_needs_no_confirm(mcp_client, fake, with_key, serving_state):
    body = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "output_bucket": 7,
                                                                 "operation_id": OP}))
    kw = _calls(fake, "scale_serving")[0][2]
    assert kw["output_bucket_id"] == 7 and kw["managed_output"] is False and kw["share_idle_capacity"] is None
    assert "outputs created from now on" in body["next_step"] and "replica counts" not in body["next_step"]

    _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "output_bucket": "managed",
                                                          "operation_id": OP}))
    kw = _calls(fake, "scale_serving")[0][2]
    assert kw["output_bucket_id"] is None and kw["managed_output"] is True

    fake.pop("last")
    result = await mcp_client.call_tool("scale_serving", {"serving": 42, "output_bucket": "elsewhere",
                                                          "operation_id": OP})
    assert result.is_error and not _calls(fake, "scale_serving")      # refused by the schema: nothing was sent


async def test_scale_sharing_on_needs_confirm_and_consent(mcp_client, fake, with_key, serving_state):
    prev = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True,
                                                                 "operation_id": OP}))
    assert prev["confirmed"] is False and not _calls(fake, "scale_serving")
    assert prev["sharing"] == {"consent": list(SHARING_CONSENT), "rolling_restart": True}
    assert prev["sharing_consent_required"] is True
    assert prev["question"].startswith("Turn on idle-capacity sharing for serving #42?")
    assert SHARING_CONSENT[0] in prev["question"] and "restart one by one" in prev["question"]

    # Confirmed without the consent flag: nothing is sent.
    result = await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True,
                                                          "confirm": True, "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == "sharing_consent_required" and not _calls(fake, "scale_serving")
    assert body["sharing"]["consent"] == list(SHARING_CONSENT) and "MUST reuse" not in body["next_step"]

    done = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True,
                                                                 "sharing_consent": True, "confirm": True,
                                                                 "operation_id": OP}))
    assert _calls(fake, "scale_serving")[0][2]["share_idle_capacity"] is True
    assert "sharing_state is pending" in done["next_step"]


async def test_scale_sharing_off_and_already_on_need_no_confirm(mcp_client, fake, with_key, serving_state):
    serving_state["serving"] = _serving(shareIdleCapacity=True, sharedRolloutState="ready")
    off = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": False,
                                                                "operation_id": OP}))
    assert _calls(fake, "scale_serving")[0][2]["share_idle_capacity"] is False
    assert "nothing restarts" in off["next_step"]
    on = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True,
                                                               "operation_id": OP}))
    assert on["action"] == "scale" and _calls(fake, "scale_serving")


async def test_scale_cost_and_sharing_in_one_preview(mcp_client, fake, with_key, serving_state):
    prev = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "max_replicas": 5,
                                                                 "share_idle_capacity": True, "operation_id": OP}))
    assert "cost goes up" in prev["question"] and "Turn on idle-capacity sharing" in prev["question"]
    assert prev["requested"] == {"max_replicas": 5, "share_idle_capacity": True}


@pytest.mark.parametrize("title,code", [("Sharing Not Available", "sharing_not_available"),
                                        ("Storage Credential Not Found", "output_bucket_not_found"),
                                        ("Storage Credential Not Verified", "output_bucket_not_verified"),
                                        ("Serving Not Changeable", "serving_not_changeable"),
                                        ("Serving Not Found", "not_serving_owner")])
async def test_scale_refusals_have_their_codes(mcp_client, fake, with_key, serving_state, title, code):
    fake["setup"] = lambda inst: inst.raise_on.update(scale_serving=_api_error(
        NotFoundError if title == "Serving Not Found" else MeshiveAPIError,
        404 if title == "Serving Not Found" else 400, title))
    result = await mcp_client.call_tool("scale_serving", {"serving": 42, "output_bucket": 7, "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == code and "MUST reuse" not in body["next_step"]



async def test_scale_of_a_serving_another_member_deployed(mcp_client, fake, with_key, serving_state, monkeypatch):
    # The serving reads back only for the member who deployed it: the read's 404 is that (or a wrong id).
    async def not_mine(self, sid):
        self._rec("get_serving", sid)
        raise _api_error(NotFoundError, 404, "Not Found", "Not Found")

    monkeypatch.setattr(FakeMeshive, "get_serving", not_mine)
    for args in ({"max_replicas": 5}, {"share_idle_capacity": False}, {"max_replicas": 5, "confirm": True}):
        result = await mcp_client.call_tool("scale_serving", {"serving": 42, "operation_id": OP, **args})
        body = _payload(result)
        assert result.is_error and body["code"] == "not_serving_owner", args
        assert "member who deployed" in body["next_step"] and "MUST reuse" not in body["next_step"]
        assert not _calls(fake, "scale_serving")


async def test_scale_refused_for_a_failed_or_deleted_serving_says_not_to_wait(mcp_client, fake, with_key,
                                                                             serving_state):
    fake["setup"] = lambda inst: inst.raise_on.update(scale_serving=_api_error(
        MeshiveAPIError, 400, "Serving Not Changeable", "Scaling unavailable while error."))
    body = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "output_bucket": 7,
                                                                 "operation_id": OP}))
    assert body["code"] == "serving_not_changeable" and "Wait until" not in body["next_step"]
    assert "Don't call again" in body["next_step"] and "delete_serving" in body["next_step"]
    assert "deploy a new serving" in body["next_step"] and "waiting won't help" in body["next_step"]


async def test_scale_sharing_back_on_without_a_restart(mcp_client, fake, with_key, serving_state):
    # Sharing was on before and then off: the rollout is still ready, so turning it on again restarts nothing.
    serving_state["serving"] = _serving(shareIdleCapacity=False, sharedRolloutState="ready")
    prev = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True,
                                                                 "operation_id": OP}))
    assert prev["sharing"]["rolling_restart"] is False and "Nothing restarts." in prev["question"]
    done = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True,
                                                                 "sharing_consent": True, "confirm": True,
                                                                 "operation_id": OP}))
    assert "nothing restarts" in done["next_step"] and "pending" not in done["next_step"]
    assert "restart one by one" not in done["next_step"]


# --- deploy_serving --------------------------------------------------------------------------------------------------

async def test_deploy_preview_shows_availability_under_the_cap(mcp_client, fake, with_key, serving_state):
    prev = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5,
                                                                  "price_cap_per_hour": 0.5, "workspace": WS,
                                                                  "max_context_tokens": 8192}))
    assert _calls(fake, "get_model_availability")[0][2] == {"workspace": WS, "max_context_tokens": 8192}
    a = prev["availability"]
    assert a["gpus_within_cap"] == 1 and a["cheapest_within_cap_per_hour_display"] == "$0.350"
    assert a["suggested_price_cap_per_hour_display"] == "$0.500" and a["deployable"] is True
    assert "No GPU" not in prev["question"] and "availability_error" not in prev

    low = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5,
                                                                 "price_cap_per_hour": 0.2, "workspace": WS}))
    assert low["availability"]["gpus_within_cap"] == 0
    assert "No GPU that fits this model is free at or under this cap" in low["question"]
    assert "(suggested cap: $0.500/hour)" in low["question"]


async def test_deploy_description_names_the_first_start_replica_limit(mcp_client):
    # With autoscale off the API starts a new serving with at most 10 replicas ('Too Many Replicas' otherwise).
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    description = tools["deploy_serving"].description
    assert "at most 10 replicas" in description and "scale_serving" in description


async def test_deploy_preview_survives_a_failed_availability_check(mcp_client, fake, with_key, serving_state):
    fake["setup"] = lambda inst: inst.raise_on.update(get_model_availability=PermissionDeniedError(403, "Admin only"))
    prev = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5,
                                                                  "price_cap_per_hour": 0.5, "workspace": WS}))
    assert prev["confirmed"] is False and prev["availability"] is None
    assert prev["availability_error"]["code"] == "forbidden"


async def test_deploy_output_bucket_and_sharing_consent(mcp_client, fake, with_key, serving_state):
    prev = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5,
                                                                  "price_cap_per_hour": 0.5, "workspace": WS,
                                                                  "output_bucket": 7, "share_idle_capacity": True}))
    assert prev["output_bucket"] == {"bucket_id": 7, "label": "results", "bucket": "my-results"}
    assert prev["sharing"]["rolling_restart"] is False and SHARING_CONSENT[-1] in prev["question"]

    unknown = await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 0.5,
                                                            "workspace": WS, "output_bucket": 99})
    assert unknown.is_error and _payload(unknown)["code"] == "invalid_argument"

    refused = await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 0.5,
                                                            "workspace": WS, "share_idle_capacity": True,
                                                            "confirm": True, "operation_id": OP})
    assert _payload(refused)["code"] == "sharing_consent_required" and not _calls(fake, "deploy_serving")

    await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 0.5,
                                                  "workspace": WS, "output_bucket": 7, "share_idle_capacity": True,
                                                  "sharing_consent": True, "confirm": True, "operation_id": OP})
    kw = _calls(fake, "deploy_serving")[0][2]
    assert kw["output_bucket_id"] == 7 and kw["share_idle_capacity"] is True


# --- models ----------------------------------------------------------------------------------------------------------

_OFFICIAL = {"id": 900, "displayName": "Qwen3 8B", "huggingfaceRepo": "Qwen/Qwen3-8B", "framework": "vllm",
             "isOfficial": True, "modelType": "llm"}


async def test_models_official_catalog_pages(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "official_models", [_OFFICIAL])
    body = _payload(await mcp_client.call_tool("models", {"source": "official"}))
    assert body["source"] == "official" and body["workspace"] is None and body["total"] == 1
    assert body["items"][0]["registration_id"] == 900 and body["items"][0]["is_official"] is True
    assert not _calls(fake, "list_models")

    refused = await mcp_client.call_tool("models", {"source": "official", "workspace": WS})
    assert _payload(refused)["code"] == "invalid_argument"
    bad = await mcp_client.call_tool("models", {"source": "shared"})
    assert bad.is_error


async def test_models_detail_with_availability(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: (setattr(inst, "official_models", [_OFFICIAL]),
                                  inst.availability.update(requiredHostRamGb=48))
    body = _payload(await mcp_client.call_tool("models", {"registration_id": 900, "max_context_tokens": 4096}))
    assert body["model"]["registration_id"] == 900 and body["workspace"] == WS
    assert _calls(fake, "get_model_availability")[0][1:] == ((900,), {"workspace": WS, "max_context_tokens": 4096})
    a = body["availability"]
    assert a["status"] == "available" and a["required_vram_gb"] == 16 and a["required_host_ram_gib"] == 48
    assert "required_host_ram_gb" not in a and a["suggested_cap_per_hour_display"] == "$0.500"
    assert a["price_low_display"] == "$0.350" and a["gpu_candidates"][0]["price_per_hour_display"] == "$0.350"
    assert "deploy_serving" in body["next_step"]

    own = _payload(await mcp_client.call_tool("models", {"registration_id": 12}))
    assert own["model"]["workspace"] == WS and own["model"]["name"] == "qwen-small"


async def test_models_detail_for_a_viewer_and_an_unknown_id(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(get_model_availability=PermissionDeniedError(403, "Admin only"))
    body = _payload(await mcp_client.call_tool("models", {"registration_id": 12}))
    assert body["availability"] is None and body["availability_error"]["code"] == "forbidden"

    fake["setup"] = None
    missing = await mcp_client.call_tool("models", {"registration_id": 404})
    assert _payload(missing)["code"] == "model_not_found" and not _calls(fake, "get_model_availability")

    stray = await mcp_client.call_tool("models", {"max_context_tokens": 4096})
    assert _payload(stray)["code"] == "invalid_argument"


async def test_models_availability_error_status(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.availability.update(overall=False, reason="error")
    body = _payload(await mcp_client.call_tool("models", {"registration_id": 12}))
    assert body["availability"]["status"] == "error"


# --- register_model --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("takes_image,expected", [(True, ["text", "image"]), (False, ["text"]), (None, None)])
async def test_register_model_takes_image(mcp_client, fake, with_key, takes_image, expected):
    args = {"huggingface_repo": "Qwen/Qwen3-VL-8B", "workspace": WS, "operation_id": OP}
    if takes_image is not None:
        args["takes_image"] = takes_image
    await mcp_client.call_tool("register_model", args)
    assert _calls(fake, "register_model")[0][2]["input_modalities"] == expected


async def test_register_model_modality_required(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(register_model=_api_error(MeshiveAPIError, 422,
                                                                                "Modality Required"))
    body = _payload(await mcp_client.call_tool("register_model", {"huggingface_repo": "x/y", "workspace": WS,
                                                                  "operation_id": OP}))
    assert body["code"] == "modality_required" and "takes_image" in body["next_step"]
    assert "MUST reuse" not in body["next_step"]


# --- submit_task / estimate_task -------------------------------------------------------------------------------------

_TASK = {"name": "t", "image": "img", "cpu_preset": "micro-2c8g", "workspace": WS}


async def test_task_hidden_secret_value_is_refused(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("submit_task", {**_TASK, "script": "pass", "env": {"TOKEN": "***"},
                                                        "confirm": True, "operation_id": OP})
    body = _payload(result)
    assert body["code"] == "masked_secret_value" and "TOKEN" in body["message"] and "Re-run" in body["next_step"]
    assert "last" not in fake


@pytest.mark.parametrize("args", [{"script": "pass", "template_id": 3, "registry_login_id": 4},
                                  {}, {"script": "pass", "project": {"asset": "asset_p", "entrypoint": "a.py"}}])
async def test_task_argument_combinations_are_refused(mcp_client, fake, with_key, args):
    result = await mcp_client.call_tool("estimate_task", {**_TASK, **args})
    assert _payload(result)["code"] == "invalid_argument" and "last" not in fake


async def test_task_preview_echoes_its_setup(mcp_client, fake, with_key, serving_state):
    project = {"asset": "asset_p", "entrypoint": "train.py", "requirements_path": "requirements.txt"}
    prev = _payload(await mcp_client.call_tool("submit_task", {
        **_TASK, "project": project, "registry_login_id": 4, "output_bucket": 7,
        "input_assets": ["asset_a", {"asset": "asset_b", "paths": ["data/*.csv"], "strip_prefix": "data"}]}))
    assert prev["image_source"] == "image img (registry login #4)" and prev["output_bucket"] == "bucket results"
    assert prev["project"] == project
    assert prev["input_assets"] == [{"asset": "asset_a"},
                                    {"asset": "asset_b", "paths": ["data/*.csv"], "strip_prefix": "data"}]
    est = _calls(fake, "estimate_task")[0]
    assert est[1] == ("t", None) and est[2]["project"] == project and est[2]["registry_login_id"] == 4
    assert est[2]["output_bucket"] == 7

    managed = _payload(await mcp_client.call_tool("estimate_task", {**_TASK, "script": "pass",
                                                                    "output_bucket": "managed"}))
    assert managed["output_bucket"] == "Meshive storage" and managed["image_source"] == "image img"
    assert _calls(fake, "estimate_task")[0][2]["output_bucket"] == "managed"
    default = _payload(await mcp_client.call_tool("estimate_task", {**_TASK, "script": "pass", "image": None,
                                                                    "template_id": 3}))
    assert default["output_bucket"] == "workspace default" and default["image_source"] == "template #3"

    await mcp_client.call_tool("submit_task", {**_TASK, "project": project, "confirm": True, "operation_id": OP})
    assert _calls(fake, "submit_task")[0][2]["project"] == project


# --- delete_tasks ----------------------------------------------------------------------------------------------------

def _task(tid, status, phase=None, ws=WS):
    d = {"externalId": tid, "name": f"name-{tid}", "namespaceName": ws, "status": status, "podName": "p",
         "image": "img", "cpuCores": 2, "ramGb": 8, "pricePerHour": "0.03", "costSoFar": "0", "totalCost": "0",
         "finishedAt": "2026-11-20T10:00:00Z" if status in ("succeeded", "failed") else None}
    if phase is not None:
        d["outputs"] = {"phase": phase, "filesSoFar": 3, "filesTotal": 10, "bytesSoFar": 300, "bytesTotal": 1000}
    return d


@pytest.fixture
def task_state(monkeypatch):
    state = {"tasks": {"task_a": _task("task_a", "succeeded", "saved"), "task_b": _task("task_b", "running"),
                       "task_c": _task("task_c", "succeeded", "uploading"),
                       "task_x": _task("task_x", "failed", ws="fedcba9876543210")}}

    async def get_task(self, tid):
        self._rec("get_task", tid)
        if tid not in state["tasks"]:
            raise NotFoundError(404, "Task not found", title="Task Not Found")
        return Task.from_dict(state["tasks"][tid])

    monkeypatch.setattr(FakeMeshive, "get_task", get_task)
    return state


async def test_delete_tasks_preview_classifies_each_task(mcp_client, fake, with_key, task_state):
    prev = _payload(await mcp_client.call_tool("delete_tasks", {
        "tasks": ["task_a", "task_b", "task_c", "task_x", "task_gone", "task_a"]}))
    will = {i["task_id"]: i["will"] for i in prev["items"]}
    assert will == {"task_a": "delete", "task_b": "skip_running", "task_c": "skip_uploading",
                    "task_x": "not_found", "task_gone": "not_found"}
    assert prev["question"].startswith("Delete 1 finished task(s)?") and "can't be undone" in prev["question"]
    assert prev["items"][0]["outputs_summary"] and prev["items"][0]["finished_at"].startswith("2026-11-20")
    assert f"Set `workspace` to {WS}" in prev["next_step"] and not _calls(fake, "delete_tasks")


async def test_delete_tasks_confirm_needs_the_workspace_id(mcp_client, fake, with_key, task_state):
    result = await mcp_client.call_tool("delete_tasks", {"tasks": ["task_a"], "workspace": "research",
                                                         "confirm": True, "operation_id": OP})
    assert _payload(result)["code"] == "invalid_argument" and not _calls(fake, "delete_tasks")
    too_many = await mcp_client.call_tool("delete_tasks", {"tasks": [f"task_{i}" for i in range(21)],
                                                           "workspace": WS})
    assert _payload(too_many)["code"] == "invalid_argument"


async def test_delete_tasks_confirmed_answer_and_next_steps(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "tasks_deleted", {
        "deleted": ["task_a"], "skipped": [{"taskId": "task_b", "reason": "active", "message": "running"},
                                           {"taskId": "task_c", "reason": "uploading", "message": "saving"},
                                           {"taskId": "task_d", "reason": "finalizing", "message": "ended"}]})
    body = _payload(await mcp_client.call_tool("delete_tasks", {"tasks": ["task_a", "task_b", "task_c", "task_d"],
                                                                "workspace": WS, "confirm": True,
                                                                "operation_id": OP}))
    call = _calls(fake, "delete_tasks")[0]
    assert call[1] == (WS, ["task_a", "task_b", "task_c", "task_d"]) and call[2] == {"idempotency_key": OP}
    assert body["deleted"] == ["task_a"] and body["skipped"][0] == {"task_id": "task_b", "reason": "active",
                                                                    "message": "running"}
    step = body["next_step"]
    assert "stop_task" in step and "cancel_task_upload" in step and "try again in a minute" in step


# --- cancel_task_upload ----------------------------------------------------------------------------------------------

async def test_cancel_task_upload_preview_and_nothing_to_cancel(mcp_client, fake, with_key, task_state):
    prev = _payload(await mcp_client.call_tool("cancel_task_upload", {"task": "task_c"}))
    assert prev["confirmed"] is False and prev["outputs"]["phase"] == "uploading"
    assert prev["outputs"]["files_to_upload"] == 10 and prev["outputs"]["bytes_so_far"] == 300
    assert "Files already uploaded stay" in prev["question"]

    done = await mcp_client.call_tool("cancel_task_upload", {"task": "task_a"})
    body = _payload(done)
    assert body["code"] == "nothing_to_cancel" and "saved" in body["message"] and "MUST reuse" not in body["next_step"]


async def test_cancel_task_upload_confirmed(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("cancel_task_upload", {"task": "task_c", "confirm": True,
                                                                      "operation_id": OP}))
    assert _calls(fake, "discard_task_outputs")[0][1:] == (("task_c",), {"idempotency_key": OP})
    assert body["task_id"] == "task_c" and body["output_upload_state"] == "uploading"
    assert "outputs.phase is discarded" in body["next_step"]

    fake["setup"] = lambda inst: inst.raise_on.update(discard_task_outputs=_api_error(
        ConflictError, 409, "Upload Not Active", "Nothing to discard.", code="upload_not_active"))
    result = await mcp_client.call_tool("cancel_task_upload", {"task": "task_c", "confirm": True,
                                                               "operation_id": OP})
    assert _payload(result)["code"] == "nothing_to_cancel"


# --- gpus for tasks, task counts -------------------------------------------------------------------------------------

async def test_gpus_for_tasks(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("gpus", {"rental_type": "task", "min_vram_gb": 30}))
    assert body["rental_type"] == "task" and len(body["items"]) == 1 and "estimate_task" in body["note"]
    row = body["items"][0]
    assert row["gpu_model"] == "RTX 5090" and row["price_per_gpu_hour_display"] == "$0.600"
    assert row["max_gpus_per_task"] == 4 and row["vcpu_per_gpu"] == 8 and row["ram_gb_per_gpu"] == 48


async def test_gpus_for_tasks_needs_a_key(mcp_client, fake):
    body = _payload(await mcp_client.call_tool("gpus", {"rental_type": "task"}))
    assert body["code"] == "no_api_key"


async def test_task_counts_only_on_the_first_page(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "tasks", [_task(f"task_{i}", "succeeded") for i in range(3)])
    first = _payload(await mcp_client.call_tool("tasks", {"limit": 2}))
    assert first["counts"] == {"by_status": {"running": 1, "succeeded": 3}, "total": 4}
    second = _payload(await mcp_client.call_tool("tasks", {"limit": 2, "cursor": first["next_cursor"]}))
    assert "counts" not in second and not _calls(fake, "get_task_counts")

    fake["setup"] = lambda inst: setattr(inst, "task_counts", None)     # a server without the count
    old = _payload(await mcp_client.call_tool("tasks", {}))
    assert "counts" not in old and "items" in old


# --- review fixes: "managed" output, sharing a model can't share ------------------------------------------------------

async def test_deploy_takes_managed_output_like_the_other_tools(mcp_client, fake, with_key, serving_state):
    prev = await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 0.5,
                                                         "workspace": WS, "output_bucket": "managed"})
    assert not prev.is_error and _payload(prev)["confirmed"] is False and "output_bucket" not in _payload(prev)
    await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 0.5,
                                                  "workspace": WS, "output_bucket": "managed", "confirm": True,
                                                  "operation_id": OP})
    assert _calls(fake, "deploy_serving")[0][2]["output_bucket_id"] is None


async def test_deploy_sharing_preview_refuses_a_model_that_cant_share(mcp_client, fake, with_key, serving_state):
    # Model 12 of the fake list is a workspace's own model, not an official one, so it can't share idle capacity.
    body = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 12,
                                                                  "price_cap_per_hour": 0.5, "workspace": WS,
                                                                  "share_idle_capacity": True}))
    assert body["code"] == "sharing_not_available" and "sharing_consent_required" not in body
    assert "without sharing" in body["next_step"]


async def test_scale_sharing_preview_refuses_a_model_that_cant_share(mcp_client, fake, with_key, serving_state):
    serving_state["serving"] = _serving(registrationId=12)
    body = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True}))
    assert body["code"] == "sharing_not_available" and not _calls(fake, "scale_serving")


_OFFICIAL_IMAGE = {"id": 62, "displayName": "flux-schnell", "framework": "diffusers", "modelType": "image",
                   "modality": "image", "isOfficial": True}
_OFFICIAL_TEXT = {"id": 63, "displayName": "qwen-official", "framework": "vllm", "modelType": "llm",
                  "modality": "text", "isOfficial": True}


@pytest.mark.parametrize("consent", [False, True])
async def test_deploy_sharing_preview_refuses_an_official_model_that_cant_share(mcp_client, fake, with_key,
                                                                               serving_state, consent):
    """An official model is not in the workspace's own registrations; it is looked up in the official catalog, so
    an official image model is refused before the user is asked to agree to the sharing terms."""
    fake["setup"] = lambda inst: setattr(inst, "official_models", [_OFFICIAL_IMAGE, _OFFICIAL_TEXT])
    body = _payload(await mcp_client.call_tool("deploy_serving", {
        "model_registration_id": 62, "price_cap_per_hour": 12.0, "workspace": WS, "share_idle_capacity": True,
        "sharing_consent": consent}))
    assert body["code"] == "sharing_not_available" and "sharing" not in body


async def test_deploy_sharing_preview_keeps_the_terms_for_an_official_model_that_can_share(
        mcp_client, fake, with_key, serving_state):
    fake["setup"] = lambda inst: setattr(inst, "official_models", [_OFFICIAL_IMAGE, _OFFICIAL_TEXT])
    body = _payload(await mcp_client.call_tool("deploy_serving", {
        "model_registration_id": 63, "price_cap_per_hour": 0.5, "workspace": WS, "share_idle_capacity": True}))
    assert body["confirmed"] is False and body["sharing"]["consent"]


async def test_scale_sharing_preview_refuses_an_official_model_that_cant_share(mcp_client, fake, with_key,
                                                                              serving_state):
    """scale_serving looks the serving's model up in the official catalog too."""
    fake["setup"] = lambda inst: setattr(inst, "official_models", [_OFFICIAL_IMAGE])
    serving_state["serving"] = _serving(registrationId=62)
    body = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "share_idle_capacity": True}))
    assert body["code"] == "sharing_not_available" and not _calls(fake, "scale_serving")
