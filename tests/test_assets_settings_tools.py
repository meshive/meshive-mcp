"""Assets from a connected bucket and saved credentials: asset_files of an asset linked from a bucket folder,
import_asset from a bucket and out of a bucket folder, source_credentials usage, delete_credential and
set_default_output_bucket."""
import json

import pytest
from meshive.exceptions import MeshiveAPIError, NotFoundError, PermissionDeniedError
from meshive.models import (Asset, AssetFileList, AssetStorage, AssetStorageSetting, AssetSubsetCopy,
                            BucketAssetRegistered, BucketListing, CredentialUsage, RegistryLoginUsage, ResourceAction,
                            StorageCredential, StorageCredentialDeleteCheck, SubsetPreview)

from conftest import FakeMeshive

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"
OP = "op-settings-0001"
GIB = 2 ** 30


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name] if "last" in fake else []


def _api_error(cls, status, title, message="m"):
    return cls(status, message, title=title, raw={"detail": {"title": title, "message": message}})


def _raise(fake, method, exc):
    fake["setup"] = lambda inst: inst.raise_on.update({method: exc})


@pytest.fixture
def settings_fake(monkeypatch):
    """The SDK methods of this area on the fake client, answering from `state`."""
    state = {
        "asset": {"assetExternalId": "asset_ref", "name": "imagenet", "assetType": "dataset", "status": "active",
                  "storageProvider": "user_s3", "totalSizeBytes": 0, "fileCount": 0, "namespaceName": WS,
                  "kind": "collection", "files": [], "referenceMode": "prefix", "referencePrefix": "datasets/imagenet/"},
        "reference_listing": {"assetExternalId": "asset_ref", "bucket": "team-data", "prefix": "datasets/imagenet/",
                              "subPrefix": "", "commonPrefixes": ["train", "val"],
                              "objects": [{"key": "datasets/imagenet/labels.csv", "relativePath": "labels.csv",
                                           "sizeBytes": 12, "ext": "csv"}],
                              "nextContinuationToken": "tok-2"},
        "subset": {"assetExternalId": "asset_ref", "prefix": "datasets/imagenet/", "matchedCount": 3,
                   "totalSizeBytes": 2 * GIB, "fetchable": True, "sampleRelativePaths": ["train/a.jpg"]},
        "credentials": [
            {"id": 7, "label": "team bucket", "provider": "r2", "bucket": "team-data",
             "verifiedAt": "2026-11-01T00:00:00Z"},
            {"id": 8, "label": "unchecked", "provider": "aws_s3", "bucket": "raw-data"}],
        "setting": {"storageCredentialId": 7, "provider": "r2", "label": "team bucket", "bucket": "team-data"},
        "delete_check": {"credentialId": 7, "label": "team bucket", "bucket": "team-data", "assetCount": 2,
                         "fileCount": 40, "totalTrackedBytes": GIB, "isWorkspaceDefault": True,
                         "blockers": {"runningPods": [{"name": "p"}], "runningTasks": []}},
        "hf_usage": [{"assetExternalId": "asset_hf", "name": "llama", "activePodCount": 2}],
    }

    async def get_asset(self, asset_id):
        self._rec("get_asset", asset_id)
        return Asset.from_dict({**state["asset"], "assetExternalId": asset_id})

    async def browse_reference_objects(self, asset_id, *, sub_prefix="", continuation_token=None):
        self._rec("browse_reference_objects", asset_id, sub_prefix=sub_prefix, continuation_token=continuation_token)
        return BucketListing.from_dict({**state["reference_listing"], "subPrefix": sub_prefix})

    async def list_asset_detail_files(self, asset_id, **kw):
        self._rec("list_asset_detail_files", asset_id, **kw)
        return AssetFileList.from_dict({"assetExternalId": asset_id, "assetName": "x", "items": [], "total": 0})

    async def register_bucket_objects(self, credential_id, object_keys, **kw):
        self._rec("register_bucket_objects", credential_id, list(object_keys), **kw)
        return BucketAssetRegistered.from_dict({"assetExternalId": "asset_new", "name": kw["name"],
                                                "status": "active", "storageProvider": "user_s3",
                                                "fileCount": len(object_keys), "totalSizeBytes": 3 * GIB})

    async def register_bucket_prefix(self, credential_id, prefix, **kw):
        self._rec("register_bucket_prefix", credential_id, prefix, **kw)
        return BucketAssetRegistered.from_dict({"assetExternalId": "asset_dir", "name": "imagenet", "status": "active",
                                                "storageProvider": "user_s3", "objectCount": 12000,
                                                "totalSizeBytes": 0, "referenceMode": "prefix", "prefix": prefix,
                                                "sizeMeasured": False, "sizeNotice": "Too many objects to count."})

    async def preview_reference_subset(self, asset_id, *, include=(), exclude=()):
        self._rec("preview_reference_subset", asset_id, include=list(include))
        return SubsetPreview.from_dict(state["subset"])

    async def fetch_reference_subset(self, asset_id, **kw):
        self._rec("fetch_reference_subset", asset_id, **kw)
        return AssetSubsetCopy.from_dict({"assetExternalId": "asset_copy", "name": "imagenet subset",
                                          "status": "uploading", "sourceAssetExternalId": asset_id, "fileCount": 3,
                                          "totalSizeBytes": 2 * GIB})

    async def get_asset_storage(self, workspace):
        self._rec("get_asset_storage", workspace)
        return AssetStorage.from_dict({"managedBytes": 0, "pricePerGbMonth": 0.02, "estimatedMonthlyCost": 0})

    async def list_storage_credentials(self, workspace, *, include_deleted=False):
        self._rec("list_storage_credentials", workspace)
        return [StorageCredential.from_dict(c) for c in state["credentials"]]

    async def get_asset_storage_setting(self, workspace):
        self._rec("get_asset_storage_setting", workspace)
        return AssetStorageSetting.from_dict(state["setting"])

    async def set_asset_storage_setting(self, workspace, credential_id, **kw):
        self._rec("set_asset_storage_setting", workspace, credential_id, **kw)
        if credential_id is None:
            return AssetStorageSetting.from_dict({"storageCredentialId": None})
        return AssetStorageSetting.from_dict({"storageCredentialId": credential_id, "label": "unchecked",
                                              "bucket": "raw-data"})

    async def get_hf_token_usage(self, token_id, workspace):
        self._rec("get_hf_token_usage", token_id, workspace)
        return [CredentialUsage.from_dict(d) for d in state["hf_usage"]]

    async def get_civitai_key_usage(self, key_id, workspace):
        self._rec("get_civitai_key_usage", key_id, workspace)
        return []

    async def get_registry_login_usage(self, registry_login_id, workspace):
        self._rec("get_registry_login_usage", registry_login_id, workspace)
        return RegistryLoginUsage.from_dict({"registryLoginId": registry_login_id,
                                             "templates": [{"templateId": 31, "name": "private-trainer"}],
                                             "activePodCount": 1, "activeTaskCount": 0})

    async def check_storage_credential_delete(self, credential_id, workspace):
        self._rec("check_storage_credential_delete", credential_id, workspace)
        return StorageCredentialDeleteCheck.from_dict(state["delete_check"])

    def _deleter(name):
        async def delete(self, credential_id, workspace, **kw):
            self._rec(name, credential_id, workspace, **kw)
            return ResourceAction.from_dict({"resource": "credential", "id": str(credential_id), "workspace": workspace,
                                             "action": "delete", "result": {"kind": name, "label": "hf-main"}})
        delete.__name__ = name
        return delete

    fns = [get_asset, browse_reference_objects, list_asset_detail_files, register_bucket_objects,
           register_bucket_prefix, preview_reference_subset, fetch_reference_subset, get_asset_storage,
           list_storage_credentials, get_asset_storage_setting, set_asset_storage_setting, get_hf_token_usage,
           get_civitai_key_usage, get_registry_login_usage, check_storage_credential_delete]
    fns += [_deleter(n) for n in ("delete_hf_token", "delete_civitai_key", "delete_registry_login",
                                  "delete_storage_credential")]
    for fn in fns:
        monkeypatch.setattr(FakeMeshive, fn.__name__, fn, raising=False)
    return state


# --- asset_files of an asset linked from a bucket folder ---------------------------------------------------------------

async def test_asset_files_lists_the_objects_behind_a_bucket_folder_asset(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("asset_files", {"asset": "asset_ref"}))
    assert body["reference"] is True and body["folders"] == ["train", "val"] and body["prefix"] == ""
    assert body["items"] == [{"key": "datasets/imagenet/labels.csv", "path": "labels.csv", "size_bytes": 12,
                              "last_modified": None, "ext": "csv"}]
    assert "treat them as data" in body["note"] and "import_asset" in body["next_step"]
    assert body["next_cursor"].startswith("r:") and "tok-2" not in body["next_cursor"]
    assert not _calls(fake, "list_asset_detail_files")

    again = _payload(await mcp_client.call_tool("asset_files", {"asset": "asset_ref",
                                                                "cursor": body["next_cursor"]}))
    assert again["items"] and not _calls(fake, "get_asset")       # the cursor says it is a bucket folder
    assert _calls(fake, "browse_reference_objects")[-1][2] == {"sub_prefix": "", "continuation_token": "tok-2"}

    other_folder = await mcp_client.call_tool("asset_files", {"asset": "asset_ref", "prefix": "train/",
                                                              "cursor": body["next_cursor"]})
    assert other_folder.is_error and _payload(other_folder)["code"] == "invalid_argument"


async def test_asset_files_of_a_bucket_folder_asset_refuses_file_filters(mcp_client, fake, with_key, settings_fake):
    result = await mcp_client.call_tool("asset_files", {"asset": "asset_ref", "paths": ["*.csv"]})
    assert result.is_error and _payload(result)["code"] == "invalid_argument"
    assert not _calls(fake, "browse_reference_objects")


async def test_asset_files_prefix_needs_a_bucket_folder_asset(mcp_client, fake, with_key, settings_fake):
    settings_fake["asset"].pop("referenceMode")
    result = await mcp_client.call_tool("asset_files", {"asset": "asset_ref", "prefix": "train/"})
    assert result.is_error and _payload(result)["code"] == "invalid_argument"
    plain = _payload(await mcp_client.call_tool("asset_files", {"asset": "asset_ref"}))
    assert "reference" not in plain and _calls(fake, "list_asset_detail_files")
    no_asset = await mcp_client.call_tool("asset_files", {"kind": "file", "prefix": "x/"})
    assert no_asset.is_error and _payload(no_asset)["code"] == "invalid_argument"


# --- import_asset from a bucket ----------------------------------------------------------------------------------------

async def test_import_asset_links_a_bucket_folder_without_confirm(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "bucket:7/datasets/imagenet/",
                                                                "operation_id": OP}))
    call = _calls(fake, "register_bucket_prefix")[0]
    assert call[1] == (7, "datasets/imagenet/")
    assert call[2] == {"workspace": WS, "name": None, "asset_type": None, "idempotency_key": OP}
    assert body["reference"] is True and body["storage_provider"] == "user_s3"
    # Too many objects to count: the count and size are unknown, not 0.
    assert body["object_count"] is None and body["total_size_gib"] is None and body["size_measured"] is False
    assert "egress" in body["next_step"] and "asset_files" in body["next_step"]
    assert "confirmed" not in body


async def test_import_asset_links_bucket_objects_with_their_folder(mcp_client, fake, with_key, settings_fake):
    keys = ["datasets/imagenet/train/a.jpg", "other/b.jpg"]
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "bucket:7/datasets/imagenet",
                                                                "paths": keys, "name": "two images",
                                                                "operation_id": OP}))
    call = _calls(fake, "register_bucket_objects")[0]
    assert call[1] == (7, keys) and call[2]["base_prefix"] == "datasets/imagenet" and call[2]["name"] == "two images"
    assert body["file_count"] == 2 and body["total_size_gib"] == 3.0 and body["reference"] is False


@pytest.mark.parametrize("args", [
    {"source": "bucket:7/"},                                            # the bucket root
    {"source": "bucket:x/data/"},                                       # no bucket_id
    {"source": "bucket:7/data/", "paths": ["data/a.bin"]},              # objects need a name
    {"source": "bucket:7/data/", "revision": "main"},
    {"source": "bucket:7/data/", "hf_token_id": 4},
    {"source": "asset_ref", "civitai_key_id": 9},
])
async def test_import_asset_from_a_bucket_refuses_bad_arguments(mcp_client, fake, with_key, settings_fake, args):
    result = await mcp_client.call_tool("import_asset", {**args, "operation_id": OP})
    assert result.is_error and _payload(result)["code"] == "invalid_argument"
    assert not _calls(fake, "register_bucket_prefix") and not _calls(fake, "register_bucket_objects")


async def test_import_asset_refuses_more_bucket_objects_than_the_server_takes(mcp_client, fake, with_key,
                                                                              settings_fake):
    result = await mcp_client.call_tool("import_asset", {"source": "bucket:7/d/", "name": "n", "operation_id": OP,
                                                         "paths": [f"d/{i}" for i in range(501)]})
    assert _payload(result)["code"] == "too_many_files" and not _calls(fake, "register_bucket_objects")


@pytest.mark.parametrize("method, args, error, code", [
    ("register_bucket_objects", {"paths": ["d/missing"], "name": "n"},
     _api_error(NotFoundError, 404, "Object Not Found"), "bucket_objects_not_found"),
    ("register_bucket_prefix", {}, _api_error(NotFoundError, 404, "Prefix Is Empty"), "bucket_prefix_empty"),
    ("register_bucket_prefix", {}, _api_error(MeshiveAPIError, 400, "Storage Credential Error"),
     "bucket_access_failed"),
    ("register_bucket_prefix", {}, _api_error(NotFoundError, 404, "Storage Credential Not Found"), "not_found"),
])
async def test_import_asset_from_a_bucket_reads_the_servers_refusals(mcp_client, fake, with_key, settings_fake,
                                                                     method, args, error, code):
    _raise(fake, method, error)
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "bucket:7/d/", "operation_id": OP, **args}))
    assert body["code"] == code
    if code.startswith("bucket_"):
        # These say what has to change, so the generic "retry with the same operation_id" advice isn't added.
        assert "MUST reuse this operation_id" not in body["next_step"]


# --- import_asset out of a bucket folder -------------------------------------------------------------------------------

async def test_copying_out_of_a_bucket_folder_needs_confirm(mcp_client, fake, with_key, settings_fake):
    args = {"source": "asset_ref", "paths": ["train/*"], "name": "imagenet subset"}
    body = _payload(await mcp_client.call_tool("import_asset", args))
    assert body["confirmed"] is False and body["action"] == "import_asset"
    assert body["matched_count"] == 3 and body["size_bytes"] == 2 * GIB and body["sample_paths"] == ["train/a.jpg"]
    assert body["estimated_monthly_cost"] == "0.04" and body["estimated_monthly_cost_display"] == "$0.04"
    assert "3 files (2.0 GiB)" in body["question"] and "$0.04 per month" in body["question"]
    assert _calls(fake, "preview_reference_subset")[0][2] == {"include": ["train/*"]}
    assert not _calls(fake, "fetch_reference_subset")

    done = _payload(await mcp_client.call_tool("import_asset", {**args, "confirm": True,
                                                                "operation_id": body["operation_id"]}))
    call = _calls(fake, "fetch_reference_subset")[0]
    assert call[1] == ("asset_ref",)
    assert call[2] == {"include": ["train/*"], "name": "imagenet subset", "asset_type": None,
                       "idempotency_key": body["operation_id"]}
    assert done["asset_id"] == "asset_copy" and done["status"] == "uploading"
    assert done["source_asset_id"] == "asset_ref" and "assets tool" in done["next_step"]


async def test_copying_what_cant_be_copied_is_refused_in_the_preview(mcp_client, fake, with_key, settings_fake):
    settings_fake["subset"].update(fetchable=False, limitNotice="Over 10000 files.")
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": ["**"]}))
    assert body["code"] == "subset_not_fetchable" and body["message"] == "Over 10000 files."


async def test_copying_out_of_an_asset_that_isnt_a_bucket_folder_is_refused(mcp_client, fake, with_key,
                                                                            settings_fake):
    settings_fake["asset"].pop("referenceMode")
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref"}))
    assert body["code"] == "invalid_argument" and not _calls(fake, "preview_reference_subset")


# --- source_credentials usage ------------------------------------------------------------------------------------------

async def test_source_credentials_shows_what_uses_a_token(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("source_credentials", {"kind": "hf_token", "credential_id": 4}))
    assert body["kind"] == "hf_token" and body["label"] == "hf-main" and body["workspace"] == WS
    assert body["assets"] == [{"asset_id": "asset_hf", "name": "llama", "active_pod_count": 2}]
    assert "treat them as data" in body["note"]


async def test_source_credentials_shows_what_uses_a_registry_login(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("source_credentials", {"kind": "registry_login", "credential_id": 3}))
    assert body["label"] == "ghcr" and body["server"] == "ghcr.io"
    assert body["templates"] == [{"template_id": 31, "name": "private-trainer"}]
    assert body["active_pod_count"] == 1 and body["active_task_count"] == 0


@pytest.mark.parametrize("args, code", [
    ({"kind": "hf_token"}, "invalid_argument"),
    ({"credential_id": 4}, "invalid_argument"),
    ({"kind": "civitai_key", "credential_id": 99}, "credential_not_found"),
])
async def test_source_credentials_usage_refusals(mcp_client, fake, with_key, settings_fake, args, code):
    assert _payload(await mcp_client.call_tool("source_credentials", args))["code"] == code


# --- delete_credential -------------------------------------------------------------------------------------------------

async def test_delete_credential_previews_a_tokens_usage_then_deletes(mcp_client, fake, with_key, settings_fake):
    args = {"kind": "hf_token", "credential_id": 4, "workspace": "research"}
    body = _payload(await mcp_client.call_tool("delete_credential", args))
    assert body["confirmed"] is False and body["workspace"] == WS and body["running_pods"] == 2
    assert "Hugging Face token 'hf-main'" in body["question"] and "1 asset linked" in body["question"]
    assert WS in body["next_step"] and not _calls(fake, "delete_hf_token")

    by_label = await mcp_client.call_tool("delete_credential", {**args, "confirm": True,
                                                                "operation_id": body["operation_id"]})
    assert by_label.is_error and _payload(by_label)["code"] == "invalid_argument"
    assert not _calls(fake, "delete_hf_token")

    done = _payload(await mcp_client.call_tool("delete_credential", {**args, "workspace": WS, "confirm": True,
                                                                     "operation_id": body["operation_id"]}))
    assert _calls(fake, "delete_hf_token")[0] == ("delete_hf_token", (4, WS), {"idempotency_key": body["operation_id"]})
    assert done == {**done, "deleted": True, "kind": "hf_token", "credential_id": 4, "label": "hf-main",
                    "workspace": WS}


async def test_delete_credential_previews_a_registry_login(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("delete_credential", {"kind": "registry_login", "credential_id": 3,
                                                                     "workspace": WS}))
    assert "registry login 'ghcr' (ghcr.io)" in body["question"] and body["templates"][0]["template_id"] == 31


async def test_delete_credential_bucket_names_the_price_only_for_the_default_output(mcp_client, fake, with_key,
                                                                                   settings_fake):
    args = {"kind": "bucket", "credential_id": 7, "workspace": WS}
    body = _payload(await mcp_client.call_tool("delete_credential", args))
    assert body["is_default_output"] is True and body["running_pods"] == 1 and body["total_tracked_size_gib"] == 1.0
    assert "Nothing in the bucket is deleted" in body["question"]
    assert "back to Meshive storage, billed at $0.02/GiB-month" in body["question"]
    assert "2 assets registered from it stay readable" in body["question"]

    settings_fake["delete_check"].update(isWorkspaceDefault=False, assetCount=0)
    plain = _payload(await mcp_client.call_tool("delete_credential", args))
    assert "billed" not in plain["question"] and "registered from it" not in plain["question"]
    assert "price_per_gb_month" not in plain


@pytest.mark.parametrize("error, code", [
    (_api_error(NotFoundError, 404, "Credential Not Found"), "credential_not_found"),
    (PermissionDeniedError(403, "Only workspace admins and billing members can do this."), "forbidden"),
])
async def test_delete_credential_refusals(mcp_client, fake, with_key, settings_fake, error, code):
    _raise(fake, "delete_civitai_key", error)
    body = _payload(await mcp_client.call_tool("delete_credential", {"kind": "civitai_key", "credential_id": 9,
                                                                     "workspace": WS, "confirm": True,
                                                                     "operation_id": OP}))
    assert body["code"] == code


# --- set_default_output_bucket -----------------------------------------------------------------------------------------

async def test_set_default_output_bucket_says_when_nothing_changes(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("set_default_output_bucket", {"workspace": WS, "output_bucket": 7}))
    assert body["changed"] is False and "confirmed" not in body and not _calls(fake, "set_asset_storage_setting")


async def test_set_default_output_bucket_to_meshive_storage_names_the_price(mcp_client, fake, with_key,
                                                                             settings_fake):
    args = {"workspace": "research", "output_bucket": "managed"}
    body = _payload(await mcp_client.call_tool("set_default_output_bucket", args))
    assert body["confirmed"] is False and body["current"]["bucket_id"] == 7 and body["new"]["destination"] == "managed"
    assert "'research' to Meshive storage" in body["question"] and "$0.02 per GiB-month" in body["question"]
    assert "Tasks already running keep their destination" in body["question"] and WS in body["next_step"]

    done = _payload(await mcp_client.call_tool("set_default_output_bucket", {
        "workspace": WS, "output_bucket": "managed", "confirm": True, "operation_id": body["operation_id"]}))
    assert _calls(fake, "set_asset_storage_setting")[0] == (
        "set_asset_storage_setting", (WS, None), {"idempotency_key": body["operation_id"]})
    assert done["changed"] is True and done["default_output_bucket"]["destination"] == "managed"


async def test_set_default_output_bucket_refuses_an_unverified_bucket(mcp_client, fake, with_key, settings_fake):
    settings_fake["setting"] = None
    body = _payload(await mcp_client.call_tool("set_default_output_bucket", {"workspace": WS, "output_bucket": 8}))
    assert body["code"] == "bucket_not_verified" and "console" in body["next_step"]
    missing = _payload(await mcp_client.call_tool("set_default_output_bucket", {"workspace": WS, "output_bucket": 99}))
    assert missing["code"] == "credential_not_found"


async def test_set_default_output_bucket_to_a_bucket(mcp_client, fake, with_key, settings_fake):
    settings_fake["setting"] = None
    settings_fake["credentials"][1]["verifiedAt"] = "2026-11-02T00:00:00Z"
    body = _payload(await mcp_client.call_tool("set_default_output_bucket", {"workspace": WS, "output_bucket": 8}))
    assert "bucket 'unchecked' (raw-data)" in body["question"] and "doesn't bill" in body["question"]
    assert body["new"]["bucket_id"] == 8 and "price_per_gb_month" not in body

    _raise(fake, "set_asset_storage_setting", _api_error(MeshiveAPIError, 400, "Storage Not Verified"))
    refused = _payload(await mcp_client.call_tool("set_default_output_bucket", {
        "workspace": WS, "output_bucket": 8, "confirm": True, "operation_id": OP}))
    assert refused["code"] == "bucket_not_verified"


# --- what a linked folder can do, and exact paths out of it ------------------------------------------------------------

async def test_a_linked_folder_is_not_offered_as_a_pod_or_task_input(mcp_client, fake, with_key, settings_fake):
    # The server refuses a linked folder (reference mode "prefix") as a pod's or task's input, so the answer points at
    # browsing and copying its files instead.
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "bucket:7/datasets/imagenet/",
                                                                "operation_id": OP}))
    step = body["next_step"]
    assert "can't be used as input_assets directly" in step and 'import_asset(source="asset_dir"' in step
    assert "when it starts" not in step and "when each pod or task starts" not in step
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    desc = " ".join(tools["import_asset"].description.split())
    assert "contents are read when a pod or task starts" not in desc and "can't be a pod's or task's" in desc


async def test_exact_paths_with_any_character_are_copied_as_object_keys(mcp_client, fake, with_key, settings_fake):
    # A space or parentheses can't be in a server pattern, so the preview reads the file's folder instead, and the
    # copy names the object exactly.
    name = "train/my style (v2).safetensors"
    settings_fake["reference_listing"]["objects"] = [
        {"key": "datasets/imagenet/" + name, "relativePath": name, "sizeBytes": 3 * 2 ** 20, "ext": "safetensors"},
        {"key": "datasets/imagenet/train/other.bin", "relativePath": "train/other.bin", "sizeBytes": 5, "ext": "bin"}]
    settings_fake["reference_listing"]["nextContinuationToken"] = None
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": [name]}))
    assert body["confirmed"] is False and body["matched_count"] == 1 and body["size_bytes"] == 3 * 2 ** 20
    assert _calls(fake, "browse_reference_objects")[0][2]["sub_prefix"] == "train"
    assert not _calls(fake, "preview_reference_subset")
    _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": [name], "confirm": True,
                                                         "operation_id": body["operation_id"]}))
    call = _calls(fake, "fetch_reference_subset")[0]
    assert call[2]["object_keys"] == ["datasets/imagenet/" + name] and "include" not in call[2]


async def test_an_exact_path_the_folder_doesnt_have_is_refused(mcp_client, fake, with_key, settings_fake):
    settings_fake["reference_listing"]["nextContinuationToken"] = None
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref",
                                                                "paths": ["train/no such (1).jpg"]}))
    assert body["code"] == "bucket_objects_not_found" and body["missing"] == ["train/no such (1).jpg"]
    assert "MUST reuse" not in body["next_step"] and not _calls(fake, "fetch_reference_subset")


async def test_brackets_in_a_path_are_literal(mcp_client, fake, with_key, settings_fake):
    # "shot[01].exr" read as a pattern would match shot0.exr and shot1.exr instead of the file the user picked.
    settings_fake["subset"].update(matchedCount=1, sampleRelativePaths=["frames/shot[01].exr"], totalSizeBytes=10)
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref",
                                                                "paths": ["frames/shot[01].exr"]}))
    assert _calls(fake, "preview_reference_subset")[0][2] == {"include": ["frames/shot[[]01].exr"]}
    assert body["matched_count"] == 1
    _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": ["frames/shot[01].exr"],
                                                         "confirm": True, "operation_id": body["operation_id"]}))
    assert _calls(fake, "fetch_reference_subset")[0][2]["object_keys"] == ["datasets/imagenet/frames/shot[01].exr"]
    # A glob keeps brackets literal too.
    _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": ["frames/shot[01]*"]}))
    assert _calls(fake, "preview_reference_subset")[-1][2] == {"include": ["frames/shot[[]01]*"]}


async def test_an_exact_path_the_preview_doesnt_match_is_refused(mcp_client, fake, with_key, settings_fake):
    settings_fake["subset"].update(matchedCount=1, sampleRelativePaths=["a.txt"])
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": ["a.txt", "b.txt"]}))
    assert body["code"] == "bucket_objects_not_found" and body["missing"] == ["b.txt"]


async def test_a_glob_with_a_name_it_cant_hold_is_refused_before_any_call(mcp_client, fake, with_key, settings_fake):
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref",
                                                                "paths": ["train/*.jpg", "my file.txt"]}))
    assert body["code"] == "invalid_argument" and "separate call" in body["message"]
    assert not _calls(fake, "preview_reference_subset") and not _calls(fake, "get_asset")
