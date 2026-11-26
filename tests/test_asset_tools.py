"""Asset tools: the assets list filters and detail, update_asset, delete_asset and delete_asset_files."""
import json

import pytest
from meshive.exceptions import ConflictError, MeshiveAPIError, PermissionDeniedError
from meshive.models import (Asset, AssetFileDeletePlan, AssetFilesDeleted, AssetPage, AssetStorage, AssetUpdated,
                            ResourceAction)

from conftest import FakeMeshive

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"
OP = "op-asset-0001"
GIB = 2 ** 30
# The token in a CivitAI download URL must never reach the agent.
URL_TOKEN = "civitai-token-value"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    # No client is built when the tool refuses its arguments before calling the API.
    return [c for c in fake["last"].calls if c[0] == name] if "last" in fake else []


def _files(n):
    # Listed out of order on purpose: the detail sorts by path.
    return [{"fileExternalId": f"file_{i:03d}", "relativePath": f"renders/{i:03d}.png", "sizeBytes": 10, "status": "ready",
             "ext": "png", "fileType": "file", "mediaKind": "image", "detectedFormat": "png",
             "pickleFlag": "detected" if i == 7 else None, "formatUnverified": False}
            for i in reversed(range(n))]


def _asset(**over):
    d = {"assetExternalId": "asset_abc", "name": "renders", "assetType": "dataset", "status": "active",
         "storageProvider": "meshive_r2", "totalSizeBytes": 3 * GIB, "fileCount": 250, "inUse": False,
         "namespaceName": WS, "ingestSource": "civitai_import", "kind": "collection",
         "importSourceRef": f"https://civitai.com/api/download/models/1?token={URL_TOKEN}#x",
         "childTypeSummary": {"file": 250}, "weightFormats": ["safetensors"], "hasRootConfig": False,
         "pickleFlag": "detected", "files": _files(250), "activeUsageContexts": []}
    d.update(over)
    return d


@pytest.fixture
def assets_fake(monkeypatch):
    """The asset methods of the fake client, answering from `state` (changed per test)."""
    state = {"asset": _asset(), "catalog_hidden": 4, "plan": None, "deleted": None, "update": None, "row": {}}

    async def list_assets(self, workspace, **kw):
        self._rec("list_assets", workspace, **kw)
        start = (kw["page"] - 1) * kw["page_size"]
        rows = [_asset(assetExternalId=f"asset_{i}", files=None, **state["row"])
                for i in range(3)][start:start + kw["page_size"]]
        return AssetPage.from_dict({"items": rows, "total": 3, "page": kw["page"], "pageSize": kw["page_size"],
                                    "catalogAddedTotal": state["catalog_hidden"] if kw["page"] == 1 else 0},
                                   namespace_name=workspace)

    async def get_asset(self, asset_id):
        self._rec("get_asset", asset_id)
        return Asset.from_dict({**state["asset"], "assetExternalId": asset_id})

    async def get_asset_storage(self, workspace):
        self._rec("get_asset_storage", workspace)
        return AssetStorage.from_dict({"managedBytes": 3 * GIB, "pricePerGbMonth": 0.02,
                                       "estimatedMonthlyCost": 0.06, "creditState": "normal"})

    async def update_asset(self, asset_id, **kw):
        self._rec("update_asset", asset_id, **kw)
        return AssetUpdated.from_dict(state["update"] or {
            "resource": "asset", "id": asset_id, "action": "update",
            "result": {"assetExternalId": asset_id, "name": kw.get("name") or "renders",
                       "assetType": kw.get("asset_type") or "dataset"}})

    async def delete_asset(self, asset_id, **kw):
        self._rec("delete_asset", asset_id, **kw)
        return ResourceAction.from_dict({"resource": "asset", "id": asset_id, "workspace": WS, "action": "delete",
                                         "result": {"status": "deleted"}})

    async def preview_asset_file_delete(self, workspace, **kw):
        self._rec("preview_asset_file_delete", workspace, **kw)
        return AssetFileDeletePlan.from_dict(state["plan"] or {
            "action": "deleteFiles", "requestedFileCount": 30, "resolvedFileCount": 30, "activeFileCount": 30,
            "deletableFileCount": 30, "blockedFileCount": 0, "staleFileCount": 0, "activeTotalSizeBytes": GIB,
            "assetExternalId": "asset_abc", "assetName": "renders",
            "samplePaths": [f"renders/{i:03d}.png" for i in range(20)]})

    async def delete_asset_files(self, workspace, **kw):
        self._rec("delete_asset_files", workspace, **kw)
        return AssetFilesDeleted.from_dict(state["deleted"] or {
            "results": [{"fileExternalId": "file_001", "relativePath": "renders/001.png", "status": "deleted"},
                        {"fileExternalId": "file_002", "relativePath": "renders/002.png", "status": "alreadyDeleted"},
                        {"fileExternalId": "file_003", "relativePath": "renders/003.png", "status": "blocked",
                         "reason": "asset_in_use"}],
            "retiredAssetExternalIds": []})

    for fn in (list_assets, get_asset, get_asset_storage, update_asset, delete_asset, preview_asset_file_delete,
               delete_asset_files):
        monkeypatch.setattr(FakeMeshive, fn.__name__, fn, raising=False)
    return state


def _raise(fake, method, exc):
    fake["setup"] = lambda inst: inst.raise_on.update({method: exc})


def _api_error(cls, status, title, message="m", **detail):
    return cls(status, message, title=title, raw={"detail": {"title": title, "message": message, **detail}})


# --- assets ------------------------------------------------------------------------------------------------------------

async def test_assets_list_passes_the_filters_and_counts_hidden_catalog_assets_on_the_first_page(
        mcp_client, fake, with_key, assets_fake):
    first = _payload(await mcp_client.call_tool("assets", {"workspace": WS, "collection": True}))
    (_, args, kw), = _calls(fake, "list_assets")
    assert args == (WS,) and kw["collection"] is True and kw["include_catalog"] is False
    assert first["catalog_hidden"] == 4 and "include_catalog=true" in first["note"]
    row = first["items"][0]
    assert row["from_catalog"] is False and row["pickle_flag"] == "detected" and row["weight_formats"] == ["safetensors"]
    assert row["has_root_config"] is False and row["has_unverified_format"] is False
    # A list row has a compact source: no URL, no composition, no files.
    assert row["source"]["ingest_source"] == "civitai_import" and "url" not in row["source"]
    assert "child_type_summary" not in row and "files" not in row and "raw" not in row

    shown = _payload(await mcp_client.call_tool("assets", {"workspace": WS, "include_catalog": True, "limit": 1}))
    # The server counts catalog-added assets in both toggle states; with include_catalog they are listed, not hidden.
    assert shown["catalog_hidden"] == 0 and "note" not in shown
    assert _calls(fake, "list_assets")[-1][2]["include_catalog"] is True
    later = _payload(await mcp_client.call_tool("assets", {"workspace": WS, "cursor": shown["next_cursor"], "limit": 1}))
    assert "catalog_hidden" not in later and "note" not in later


async def test_assets_list_filters_are_refused_with_a_single_asset(mcp_client, fake, with_key, assets_fake):
    for extra in ({"collection": False}, {"include_catalog": True}):
        body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc", **extra}))
        assert body["code"] == "invalid_argument"
    assert not _calls(fake, "get_asset")


async def test_asset_detail_shows_the_first_200_files_with_their_flags(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc"}))
    asset = body["asset"]
    assert asset["files_total"] == 250 and asset["files_truncated"] is True and len(asset["files"]) == 200
    assert [f["path"] for f in asset["files"][:2]] == ["renders/000.png", "renders/001.png"]
    assert body["next_step"] == ("The list is cut to the first 200 files; page through all of them with "
                                 "asset_files(asset=\"asset_abc\").")
    f = asset["files"][7]
    assert {k: f[k] for k in ("file_id", "ext", "file_type", "media_kind", "detected_format", "pickle_flag",
                              "format_unverified")} == {
        "file_id": "file_007", "ext": "png", "file_type": "file", "media_kind": "image", "detected_format": "png",
        "pickle_flag": "detected", "format_unverified": False}
    assert "pickle_flag marks files that can run code" in body["note"]
    assert asset["child_type_summary"] == {"file": 250} and "raw" not in asset
    # The source URL is kept for the detail, without the query string that can carry a token.
    assert asset["source"]["url"] == "https://civitai.com/api/download/models/1"
    assert URL_TOKEN not in json.dumps(body)


async def test_asset_detail_with_few_files_is_not_truncated(mcp_client, fake, with_key, assets_fake):
    assets_fake["asset"] = _asset(files=_files(3)[:2], pickleFlag=None)
    body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc"}))
    assert body["asset"]["files_truncated"] is False and body["asset"]["files_total"] == 2
    assert "next_step" not in body and "note" not in body


_BUCKET_KEYS = {"storageCredentialId": 30, "storageCredentialLabel": "team bucket", "storageCredentialStatus": "ok"}


@pytest.mark.parametrize("over, reference", [
    # A file registered from a bucket: no reference, the bucket comes only from the asset fields.
    ({"ingestSource": "s3_register"}, None),
    # A linked folder: its reference names the same bucket connection.
    ({"ingestSource": "s3_prefix_reference", "referenceMode": "prefix", "referencePrefix": "data/"},
     {"mode": "prefix", "prefix": "data/", "credential_label": "team bucket", "credential_status": "ok"}),
])
async def test_assets_say_which_bucket_connection_an_asset_reads_through(mcp_client, fake, with_key, assets_fake,
                                                                        over, reference):
    asset = {"storageProvider": "user_s3", "importSourceRef": None, **_BUCKET_KEYS, **over}
    assets_fake["asset"] = _asset(**asset)
    body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc"}))["asset"]
    assert (body["bucket_id"], body["bucket_label"], body["bucket_status"]) == (30, "team bucket", "ok")
    if reference is None:
        assert body["reference"] is None
    else:
        assert {k: body["reference"][k] for k in reference} == reference

    assets_fake["row"] = asset
    row = _payload(await mcp_client.call_tool("assets", {"workspace": WS}))["items"][0]
    assert (row["bucket_id"], row["bucket_label"], row["bucket_status"]) == (30, "team bucket", "ok")


async def test_assets_say_when_the_bucket_connection_was_removed(mcp_client, fake, with_key, assets_fake):
    assets_fake["asset"] = _asset(storageProvider="user_s3", ingestSource="s3_register", importSourceRef=None,
                                  storageCredentialId=None, storageCredentialLabel=None,
                                  storageCredentialStatus="removed")
    body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc"}))["asset"]
    assert (body["bucket_id"], body["bucket_label"], body["bucket_status"]) == (None, None, "removed")


async def test_an_asset_in_meshive_storage_reads_through_no_bucket(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc"}))["asset"]
    assert (body["bucket_id"], body["bucket_label"], body["bucket_status"]) == (None, None, None)


async def test_a_copy_out_of_a_bucket_folder_names_the_asset_it_was_copied_from(mcp_client, fake, with_key,
                                                                               assets_fake):
    """The server's source reference of such a copy is "s3subset:<asset_id>:<bucket>/<prefix>?include=..."."""
    copy = {"storageProvider": "meshive_r2", "ingestSource": "s3_subset_fetch",
            "importSourceRef": "s3subset:asset_0a62378e51791846:test/e2e/run/?include=sub%2F%2A"}
    assets_fake["asset"] = _asset(**copy)
    body = _payload(await mcp_client.call_tool("assets", {"asset": "asset_abc"}))["asset"]
    assert body["source"]["from_asset_id"] == "asset_0a62378e51791846"
    assets_fake["row"] = copy
    row = _payload(await mcp_client.call_tool("assets", {"workspace": WS}))["items"][0]
    assert row["source"]["from_asset_id"] == "asset_0a62378e51791846"


# --- update_asset ------------------------------------------------------------------------------------------------------

async def test_update_asset_needs_something_to_change(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("update_asset", {"asset": "asset_abc", "operation_id": OP}))
    assert body["code"] == "invalid_argument" and not _calls(fake, "update_asset")


async def test_update_asset_renames_with_the_operation_id(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("update_asset", {"asset": "asset_abc", "name": "renders v2",
                                                                "operation_id": OP}))
    (_, args, kw), = _calls(fake, "update_asset")
    assert args == ("asset_abc",) and kw == {"name": "renders v2", "asset_type": None, "idempotency_key": OP}
    assert body["asset_id"] == "asset_abc" and body["name"] == "renders v2" and body["asset_type"] == "dataset"
    assert body["next_step"].startswith("Saved.") and "watched folder" in body["next_step"]
    assert body["operation_id"] == OP and "replayed" not in body

    retyped = _payload(await mcp_client.call_tool("update_asset", {"asset": "asset_abc", "asset_type": "model",
                                                                   "operation_id": "op-asset-0002"}))
    assert retyped["asset_type"] == "model" and retyped["next_step"] == "Saved."


async def test_update_asset_says_a_replayed_answer(mcp_client, fake, with_key, assets_fake):
    assets_fake["update"] = {"id": "asset_abc", "replayed": True,
                             "result": {"assetExternalId": "asset_abc", "name": "x", "assetType": "model"}}
    body = _payload(await mcp_client.call_tool("update_asset", {"asset": "asset_abc", "name": "x", "operation_id": OP}))
    assert body["replayed"] is True and "nothing was done again" in body["next_step"]


@pytest.mark.parametrize("exc,code,step", [
    (_api_error(ConflictError, 409, "Asset Name Conflict", conflictingName="renders"), "name_taken",
     "include_catalog=true"),
    (_api_error(MeshiveAPIError, 400, "Output Type Is Immutable"), "asset_type_locked", "change only the name"),
    (_api_error(MeshiveAPIError, 400, "Asset Not Editable"), "asset_not_editable", "can't be changed"),
    (_api_error(MeshiveAPIError, 400, "Invalid Name"), "invalid_argument", "1-255 characters"),
    (_api_error(MeshiveAPIError, 400, "Invalid Asset Type"), "invalid_argument", "no asset can be made an output"),
    (_api_error(PermissionDeniedError, 403, "Not an admin", "Admin or billing role required."), "forbidden",
     "do not retry"),
])
async def test_update_asset_errors(mcp_client, fake, with_key, assets_fake, exc, code, step):
    _raise(fake, "update_asset", exc)
    body = _payload(await mcp_client.call_tool("update_asset", {"asset": "asset_abc", "name": "renders",
                                                                "operation_id": OP}))
    assert body["code"] == code and step in body["next_step"]
    if code == "name_taken":
        assert body["conflictingName"] == "renders"
        # Its own advice: no generic "retry with the same operation_id".
        assert "MUST reuse this operation_id" not in body["next_step"]


# --- delete_asset ------------------------------------------------------------------------------------------------------

async def test_delete_asset_preview_shows_the_monthly_saving_for_meshive_storage(mcp_client, fake, with_key,
                                                                                 assets_fake):
    body = _payload(await mcp_client.call_tool("delete_asset", {"asset": "asset_abc"}))
    assert body["confirmed"] is False and body["action"] == "delete_asset" and body["operation_id"]
    assert body["storage_freed_gib"] == 3.0
    assert body["monthly_storage_saving"] == "0.06" and body["monthly_storage_saving_display"] == "$0.06"
    assert "permanently removed from Meshive storage" in body["effect"]
    assert body["question"].startswith("Delete asset 'renders' (250 files, 3.0 GiB)?")
    assert set(body["asset"]) == {"asset_id", "name", "asset_type", "kind", "storage_provider", "size_bytes",
                                  "file_count", "status", "ingest_source", "in_use", "active_usage_contexts"}
    assert _calls(fake, "get_asset_storage")[0][1] == (WS,) and not _calls(fake, "delete_asset")


@pytest.mark.parametrize("provider,ingest,effect", [
    ("user_s3", "web_upload", "the objects stay in the user's bucket"),
    ("external", "hf_import", "the source on Hugging Face, CivitAI or the URL is untouched"),
    ("meshive_r2", "task_output", "The task's output links stop working."),
])
async def test_delete_asset_preview_effect_per_provider(mcp_client, fake, with_key, assets_fake, provider, ingest,
                                                        effect):
    assets_fake["asset"] = _asset(storageProvider=provider, ingestSource=ingest)
    body = _payload(await mcp_client.call_tool("delete_asset", {"asset": "asset_abc"}))
    assert effect in body["effect"] and effect in body["question"]
    managed = provider == "meshive_r2"
    assert ("monthly_storage_saving_display" in body) is managed and bool(_calls(fake, "get_asset_storage")) is managed


@pytest.mark.parametrize("over,code", [({"status": "frozen"}, "asset_frozen"), ({"inUse": True}, "asset_in_use"),
                                       ({"status": "purged"}, "asset_not_editable")])
async def test_delete_asset_preview_refuses_what_the_server_refuses(mcp_client, fake, with_key, assets_fake, over,
                                                                    code):
    assets_fake["asset"] = _asset(activeUsageContexts=[{"kind": "pod", "identifier": "pod-7", "name": "comfy",
                                                        "status": "running"}], **over)
    result = await mcp_client.call_tool("delete_asset", {"asset": "asset_abc"})
    body = _payload(result)
    assert result.is_error and body["code"] == code and "question" not in body
    assert "MUST reuse this operation_id" not in body["next_step"]
    if code == "asset_in_use":
        assert body["active_usage_contexts"][0]["identifier"] == "pod-7"


async def test_delete_asset_preview_of_a_deleted_asset(mcp_client, fake, with_key, assets_fake):
    assets_fake["asset"] = _asset(status="deleted")
    body = _payload(await mcp_client.call_tool("delete_asset", {"asset": "asset_abc"}))
    assert body["deleted"] is True and body["already_deleted"] is True and "question" not in body


async def test_delete_asset_confirm_reuses_the_operation_id(mcp_client, fake, with_key, assets_fake):
    prev = _payload(await mcp_client.call_tool("delete_asset", {"asset": "asset_abc"}))
    body = _payload(await mcp_client.call_tool("delete_asset", {"asset": "asset_abc", "confirm": True,
                                                                "operation_id": prev["operation_id"]}))
    (_, args, kw), = _calls(fake, "delete_asset")
    assert args == ("asset_abc",) and kw == {"idempotency_key": prev["operation_id"]}
    assert body["deleted"] is True and body["asset_id"] == "asset_abc" and "no longer appears" in body["next_step"]


@pytest.mark.parametrize("exc,code", [
    (_api_error(ConflictError, 409, "Upload Session Active"), "upload_in_progress"),
    (_api_error(ConflictError, 409, "Asset In Use"), "asset_in_use"),
    (_api_error(PermissionDeniedError, 403, "Asset Frozen"), "asset_frozen"),
    (_api_error(MeshiveAPIError, 400, "Asset Not Deletable"), "asset_not_editable"),
])
async def test_delete_asset_server_refusals(mcp_client, fake, with_key, assets_fake, exc, code):
    _raise(fake, "delete_asset", exc)
    body = _payload(await mcp_client.call_tool("delete_asset", {"asset": "asset_abc", "confirm": True,
                                                                "operation_id": OP}))
    assert body["code"] == code and "MUST reuse this operation_id" not in body["next_step"]


# --- delete_asset_files ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("args", [{}, {"file_ids": ["file_1"], "asset": "asset_abc", "paths": ["*"]},
                                  {"asset": "asset_abc"}, {"paths": ["*"]}])
async def test_delete_asset_files_needs_exactly_one_selection(mcp_client, fake, with_key, assets_fake, args):
    body = _payload(await mcp_client.call_tool("delete_asset_files", args))
    assert body["code"] == "invalid_argument" and not _calls(fake, "preview_asset_file_delete")


async def test_delete_asset_files_refuses_more_than_1000_ids(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"file_ids": [f"file_{i}" for i in range(1001)]}))
    assert body["code"] == "too_many_files" and not _calls(fake, "preview_asset_file_delete")


async def test_delete_asset_files_preview_by_paths(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["renders/*"]}))
    (_, args, kw), = _calls(fake, "preview_asset_file_delete")
    # The workspace comes from the asset.
    assert args == (WS,) and kw == {"asset_id": "asset_abc", "paths": ["renders/*"]}
    assert body["confirmed"] is False and body["action"] == "delete_asset_files"
    assert body["matched_file_count"] == 30 and body["deletable_file_count"] == 30 and body["expected_file_count"] == 30
    assert len(body["sample_paths"]) == 20 and body["asset_name"] == "renders" and body["size_bytes"] == GIB
    assert body["storage_freed_gib"] == 1.0 and body["monthly_storage_saving_display"] == "$0.02"
    assert "matched again when you confirm" in body["note"]
    assert body["question"] == "Delete 30 files (1.0 GiB) from 'renders'? Files in Meshive storage can't be recovered."
    assert "expected_file_count=30" in body["next_step"] and not _calls(fake, "delete_asset_files")


async def test_delete_asset_files_preview_by_ids_resolves_the_workspace(mcp_client, fake, with_key, assets_fake):
    assets_fake["asset"] = _asset(storageProvider="user_s3")
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"file_ids": ["file_001", "file_002"]}))
    (_, args, kw), = _calls(fake, "preview_asset_file_delete")
    assert args == (WS,) and kw == {"file_ids": ["file_001", "file_002"]}
    assert "monthly_storage_saving_display" not in body and "storage_freed_gib" not in body


async def test_delete_asset_files_preview_never_offers_the_size_of_skipped_files(mcp_client, fake, with_key,
                                                                                  assets_fake):
    # 2 deletable files of an idle asset, 3 of an asset in use: the server's active total counts all 5.
    assets_fake["plan"] = {"action": "deleteFiles", "requestedFileCount": 5, "activeFileCount": 5,
                           "deletableFileCount": 2, "blockedFileCount": 3, "staleFileCount": 0,
                           "activeTotalSizeBytes": 32 * GIB, "samplePaths": ["a/1.bin", "a/2.bin"]}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"file_ids": [f"file_{i}" for i in range(5)]}))
    assert body["size_bytes"] is None and body["selected_size_bytes"] == 32 * GIB
    assert "storage_freed_gib" not in body and "monthly_storage_saving_display" not in body
    assert body["question"] == ("Delete 2 files (32.0 GiB selected, including the 3 files skipped) from several "
                                "assets? Files in Meshive storage can't be recovered.")

    # A server that sends the deletable size on its own: that size is offered, and the saving follows it.
    assets_fake["plan"] = {**assets_fake["plan"], "assetExternalId": "asset_abc", "assetName": "renders",
                           "deletableTotalSizeBytes": 2 * GIB}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"file_ids": [f"file_{i}" for i in range(5)]}))
    assert body["size_bytes"] == 2 * GIB and body["storage_freed_gib"] == 2.0 and "selected_size_bytes" not in body
    assert body["question"].startswith("Delete 2 files (2.0 GiB) from 'renders'?")


async def test_delete_asset_files_every_file_of_a_collection_points_to_delete_asset(mcp_client, fake, with_key,
                                                                                     assets_fake):
    assets_fake["plan"] = {"action": "deleteAsset", "requestedFileCount": 250, "activeFileCount": 250,
                           "deletableFileCount": 250, "assetExternalId": "asset_abc", "assetName": "renders"}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["*"]}))
    assert body["code"] == "whole_asset_delete_required" and "all of 'renders''s files" in body["message"]
    assert "call delete_asset" in body["next_step"] and body["asset_id"] == "asset_abc"
    assert "MUST reuse this operation_id" not in body["next_step"]


async def test_delete_asset_files_nothing_deletable_in_a_frozen_asset(mcp_client, fake, with_key, assets_fake):
    assets_fake["asset"] = _asset(status="frozen")
    assets_fake["plan"] = {"action": "deleteFiles", "requestedFileCount": 3, "activeFileCount": 3,
                           "deletableFileCount": 0, "blockedFileCount": 3, "assetExternalId": "asset_abc"}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["*.png"]}))
    assert body["code"] == "asset_frozen"
    assets_fake["asset"] = _asset(inUse=True)
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["*.png"]}))
    assert body["code"] == "asset_in_use"


async def test_delete_asset_files_confirm_needs_the_previews_count(mcp_client, fake, with_key, assets_fake):
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["*"],
                                                                      "confirm": True, "operation_id": OP}))
    assert body["code"] == "invalid_argument" and not _calls(fake, "delete_asset_files")


async def test_delete_asset_files_confirm_sends_the_expected_count(mcp_client, fake, with_key, assets_fake):
    prev = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["renders/*"]}))
    body = _payload(await mcp_client.call_tool("delete_asset_files", {
        "asset": "asset_abc", "paths": ["renders/*"], "expected_file_count": prev["expected_file_count"],
        "confirm": True, "operation_id": prev["operation_id"]}))
    (_, args, kw), = _calls(fake, "delete_asset_files")
    assert args == (WS,) and kw == {"asset_id": "asset_abc", "paths": ["renders/*"], "expected_file_count": 30,
                                    "idempotency_key": prev["operation_id"]}
    assert body["asset_id"] == "asset_abc" and body["deleted"] == [{"file_id": "file_001", "path": "renders/001.png"}]
    assert body["already_deleted"] == 1 and body["failed"] == [] and body["asset_deleted"] is False
    assert body["blocked"] == [{"file_id": "file_003", "path": "renders/003.png", "reason": "asset_in_use"}]
    assert body["next_step"].startswith("Deleted 1 file.") and "1 were skipped" in body["next_step"]


async def test_delete_asset_files_ids_that_name_no_file_here_are_not_offered_a_retry(mcp_client, fake, with_key,
                                                                                     assets_fake):
    assets_fake["deleted"] = {"results": [
        {"fileExternalId": "file_1", "relativePath": "a.png", "status": "deleted"},
        {"fileExternalId": "file_x", "status": "failed", "reason": "not_found"},
        {"fileExternalId": "file_y", "status": "failed", "reason": "namespace_mismatch"},
        {"fileExternalId": "file_2", "relativePath": "b.png", "status": "failed", "reason": "delete_failed:timeout"}],
        "retiredAssetExternalIds": []}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {
        "file_ids": ["file_1", "file_x", "file_y", "file_2"], "expected_file_count": 2, "confirm": True,
        "operation_id": OP}))
    assert [f["file_id"] for f in body["not_found"]] == ["file_x", "file_y"]
    assert body["failed"] == [{"file_id": "file_2", "path": "b.png", "reason": "delete_failed:timeout"}]
    assert "2 are not files of this workspace any more" in body["next_step"]
    assert "1 failed; preview again to retry them." in body["next_step"]

    assets_fake["deleted"] = {"results": [{"fileExternalId": "file_x", "status": "failed", "reason": "not_found"}],
                              "retiredAssetExternalIds": []}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {
        "file_ids": ["file_x"], "expected_file_count": 0, "confirm": True, "operation_id": OP}))
    assert body["failed"] == [] and "preview again" not in body["next_step"]


async def test_update_asset_invalid_name_is_not_told_to_retry_the_same_arguments(mcp_client, fake, with_key,
                                                                                 assets_fake):
    _raise(fake, "update_asset", _api_error(MeshiveAPIError, 400, "Invalid Name"))
    body = _payload(await mcp_client.call_tool("update_asset", {"asset": "asset_abc", "name": "x" * 300,
                                                                "operation_id": OP}))
    assert body["code"] == "invalid_argument" and body["operation_id"] == OP
    assert "MUST reuse this operation_id" not in body["next_step"] and "same arguments" not in body["next_step"]
    body = _payload(await mcp_client.call_tool("delete_asset_files", {"asset": "asset_abc", "paths": ["*"],
                                                                      "confirm": True, "operation_id": OP}))
    assert body["code"] == "invalid_argument" and "MUST reuse this operation_id" not in body["next_step"]


async def test_delete_asset_files_reports_an_asset_that_went_with_its_last_file(mcp_client, fake, with_key,
                                                                                assets_fake):
    assets_fake["deleted"] = {"results": [{"fileExternalId": "file_9", "relativePath": "a.png", "status": "deleted"}],
                              "retiredAssetExternalIds": ["asset_one"]}
    body = _payload(await mcp_client.call_tool("delete_asset_files", {
        "file_ids": ["file_9"], "expected_file_count": 1, "confirm": True, "operation_id": OP}))
    assert body["asset_deleted"] is True and body["deleted_asset_ids"] == ["asset_one"] and "asset_id" not in body


@pytest.mark.parametrize("exc,code", [
    (_api_error(ConflictError, 409, "Selection Changed", deletableFileCount=29), "selection_changed"),
    (_api_error(ConflictError, 409, "Whole Collection Delete Required"), "whole_asset_delete_required"),
    (_api_error(MeshiveAPIError, 400, "Too Many Files"), "too_many_files"),
    (_api_error(MeshiveAPIError, 400, "No Files"), "no_files_matched"),
])
async def test_delete_asset_files_server_refusals(mcp_client, fake, with_key, assets_fake, exc, code):
    _raise(fake, "delete_asset_files", exc)
    body = _payload(await mcp_client.call_tool("delete_asset_files", {
        "asset": "asset_abc", "paths": ["*"], "expected_file_count": 30, "confirm": True, "operation_id": OP}))
    assert body["code"] == code and "MUST reuse this operation_id" not in body["next_step"]


async def test_asset_titles_keep_their_general_meaning_elsewhere():
    """"Invalid Name" and "No Files" come from other routes too; only the asset tools read them as asset errors."""
    from meshive_mcp.errors import asset_write_error, translate

    exc = _api_error(MeshiveAPIError, 400, "Invalid Name")
    assert json.loads(str(translate(exc)))["code"] == "invalid_request"
    assert json.loads(str(asset_write_error(exc)))["code"] == "invalid_argument"
    assert asset_write_error(_api_error(MeshiveAPIError, 400, "Workspace does not exist")) is None
    assert asset_write_error(ValueError("x")) is None


async def test_asset_tool_descriptions_fit(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("assets", "update_asset", "delete_asset", "delete_asset_files"):
        assert len(tools[name].description.encode("utf-16-le")) // 2 <= 4096, name
    assert tools["delete_asset"].annotations.destructive_hint is True
    assert tools["update_asset"].annotations.destructive_hint is False
    assert "confirm" not in tools["update_asset"].input_schema["properties"]


def test_sizes_and_counts_in_questions_read_naturally():
    # A 1 MiB render must not be offered as "1 files, 0.0 GiB" in a delete question.
    from meshive_mcp.tools.assets import _files, _size_text
    assert (_files(1), _files(0), _files(2)) == ("1 file", "0 files", "2 files")
    assert _size_text(None) == "size not measured yet" and _size_text(10) == "10 bytes" and _size_text(1536) == "1.5 KiB"
    assert _size_text(1186255) == "1.1 MiB" and _size_text(57890280) == "55.2 MiB"
    assert _size_text(3 * GIB) == "3.0 GiB" and _size_text(GIB + GIB // 2) == "1.5 GiB"
