"""Representative tool contracts for a single account using the remote MCP server.

The SDK method is the business operation for these arguments. Resolution of workspace
names and other prerequisite reads may precede it. Console delivery applies to OAuth.
A confirmed case assumes the named resource exists in the stated role's workspace.
"""
from dataclasses import dataclass

from conftest import MACHINE_ID


@dataclass(frozen=True)
class Case:
    tool: str
    arguments: dict
    scope: str
    role: str
    sdk: str | None
    effect: str
    retry: str
    delivery: str


ACCOUNT = "account holder"
MEMBER = "workspace member"
ADMIN = "workspace admin or billing"
HOST = "machine owner"
DEPLOYER = "serving owner with workspace admin or billing role"
WS = "0123456789abcdef"
POD = "0123456789abcdef-0"
TASK = "task_abc"
ASSET = "asset_abc"
PORT = {"workspace": WS, "pod": POD, "port": 8188}
OP = {"operation_id": "contract-operation-0001"}
YES = {"confirm": True, **OP}


def read(tool, sdk, role, *, scope="read", delivery="tool", **args):
    return Case(tool, args, scope, role, sdk, "none", "read again", delivery)


def write(tool, sdk, role, effect, *, recorded=True, delivery="tool", **args):
    retry = "same operation ID" if recorded else "reconcile before retry"
    return Case(tool, args, "read write", role, sdk, effect, retry, delivery)


CASES = (
    read("gpus", None, "anyone", scope="public"),
    read("cpus", "list_cpus", ACCOUNT),
    read("templates", "list_templates", MEMBER, workspace=WS),
    read("model_sets", "list_model_sets", MEMBER, workspace=WS, template_id=457),
    write("save_template", "create_template", ADMIN, "create a template", workspace=WS,
          name="demo", image="ubuntu:24.04", **OP),
    write("delete_template", "delete_template", ADMIN, "delete a template", workspace=WS, template_id=31, **YES),
    read("account", "me", ACCOUNT),
    read("activity", "list_activity", ACCOUNT),
    read("workspaces", "list_workspaces", ACCOUNT),
    read("billing_history", "list_credit_history", ACCOUNT),
    write("create_workspace", "create_workspace", ACCOUNT, "create a workspace", name="demo", **YES),
    write("update_workspace", "update_workspace", ADMIN, "rename a workspace", workspace=WS, name="renamed", **YES),
    write("delete_workspace", "delete_workspace", ADMIN, "delete an empty workspace", workspace=WS, **YES),
    read("pods", "get_pod", MEMBER, workspace=WS, pod=POD),
    read("storages", "list_storages", MEMBER, workspace=WS),
    read("estimate_pod", "estimate_pod", MEMBER, name="demo", template_id=457, workspace=WS, gpu_model="RTX 3060"),
    read("transactions", "list_transactions", MEMBER, workspace=WS),
    read("watched_folders", "get_watched_folders", MEMBER, workspace=WS, pod=POD),
    write("create_pod", "create_pod", ADMIN, "start a billable pod", workspace=WS, name="demo",
          template_id=457, gpu_model="RTX 3060", **YES),
    write("start_pod", "start_pod", ADMIN, "resume pod billing", workspace=WS, pod=POD, placement="same_node", **YES),
    write("stop_pod", "stop_pod", ADMIN, "stop a pod", workspace=WS, pod=POD, **OP),
    write("restart_pod", "restart_pod", ADMIN, "restart a pod", workspace=WS, pod=POD, **OP),
    write("update_pod", "update_pod", ADMIN, "rename a pod", workspace=WS, pod=POD, name="renamed", **YES),
    write("delete_pod", "delete_pod", ADMIN, "delete a pod", workspace=WS, pod=POD, **YES),
    write("set_watched_folders", "set_watched_folders", ADMIN, "replace watched folders", workspace=WS,
          pod=POD, expected_version=3, user=[], template={}, **YES),
    write("create_storage", "create_storage", ADMIN, "provision billable storage", workspace=WS,
          name="data", size_gb=50, **YES),
    write("update_storage", "update_storage", ADMIN, "rename storage", workspace=WS, storage="pv-a", name="renamed", **YES),
    write("delete_storage", "delete_storage", ADMIN, "delete storage and its contents", workspace=WS, storage="pv-a", **YES),
    read("servings", "list_servings", MEMBER, workspace=WS),
    read("models", "list_models", ADMIN, workspace=WS),
    read("detect_model", "detect_model", ADMIN, workspace=WS, huggingface_repo="example/model"),
    read("source_credentials", "list_hf_tokens", ADMIN, delivery="metadata", workspace=WS),
    read("tasks", "list_tasks", MEMBER, workspace=WS),
    read("estimate_task", "estimate_task", MEMBER, name="demo", workspace=WS, script="print(1)"),
    read("operation_status", "get_operation", ACCOUNT, method="POST", path="/pods", **OP),
    write("register_model", "register_model", ADMIN, "register a model", workspace=WS, huggingface_repo="example/model", **OP),
    write("delete_model", "delete_model", ADMIN, "delete a model registration", workspace=WS, registration_id=12, **YES),
    write("deploy_serving", "deploy_serving", ADMIN, "deploy billable replicas", workspace=WS,
          model_registration_id=5, price_cap_per_hour=1.5, **YES),
    write("scale_serving", "scale_serving", DEPLOYER, "change replica range", serving=42, max_replicas=4, **YES),
    write("pause_serving", "pause_serving", DEPLOYER, "pause a deployment", serving=42, **OP),
    write("delete_serving", "delete_serving", DEPLOYER, "delete a deployment", serving=42, **YES),
    write("submit_task", "submit_task", ADMIN, "enqueue a billable task", workspace=WS, name="demo", script="print(1)", **YES),
    write("stop_task", "stop_task", ADMIN, "stop a task", task=TASK, **OP),
    write("delete_tasks", "delete_tasks", ADMIN, "delete finished task records", workspace=WS, tasks=[TASK], **YES),
    write("cancel_task_upload", "discard_task_outputs", ADMIN, "discard pending task outputs", task=TASK, **YES),
    read("assets", "list_assets", MEMBER, workspace=WS),
    read("asset_files", "list_asset_files", MEMBER, workspace=WS, kind="output"),
    read("buckets", "list_storage_credentials", MEMBER, delivery="metadata", workspace=WS),
    read("download_links", "asset_download_urls", MEMBER, delivery="scoped file links", asset=ASSET, paths=["demo.txt"]),
    write("import_asset", "import_asset", ADMIN, "import an external asset", workspace=WS, source="https://example.org/demo.txt", **OP),
    write("upload_asset", "start_asset_upload", ADMIN, "reserve an upload", recorded=False, delivery="scoped upload links",
          workspace=WS, name="demo", files=[{"path": "demo.txt", "size_bytes": 4}]),
    write("finish_upload", "complete_asset_upload", ADMIN, "finalize the upload", recorded=False, upload_id="upload_abc"),
    write("update_asset", "update_asset", ADMIN, "rename an asset", asset=ASSET, name="renamed", **OP),
    write("delete_asset", "delete_asset", ADMIN, "delete an asset", asset=ASSET, **YES),
    write("delete_asset_files", "delete_asset_files", ADMIN, "delete selected files", workspace=WS, file_ids=["file_demo"], expected_file_count=1, **YES),
    write("export_asset", "export_asset", ADMIN, "copy an asset to a connected bucket", asset=ASSET, bucket=7, **YES),
    write("add_pod_asset", "add_pod_asset", ADMIN, "attach an asset", workspace=WS, pod=POD, asset=ASSET, **YES),
    write("remove_pod_asset", "remove_pod_asset", ADMIN, "detach an asset", workspace=WS, pod=POD, connection_id=12, **YES),
    read("access_keys", "list_access_keys", MEMBER, delivery="metadata", **PORT),
    write("run_command", "run_command", ADMIN, "execute one pod command", recorded=False, workspace=WS, pod=POD, command="pwd"),
    read("command_status", "get_command_run", ADMIN, scope="read write", workspace=WS, pod=POD, run_id="run_0123456789ab"),
    write("ssh_access", "ssh_access", MEMBER, "issue pod access", recorded=False, delivery="console", workspace=WS, pod=POD),
    write("connect_storage", "connect_storage", MEMBER, "start a storage connection", recorded=False, delivery="console",
          workspace=WS, storage="pv-a", **YES),
    write("set_port_lock", "set_port_lock", ADMIN, "lock a port", locked=True, **PORT, **OP),
    write("set_port_preflight", "set_port_preflight", ADMIN, "allow preflight requests", allow=True, **PORT, **YES),
    write("revoke_browser_sessions", "revoke_browser_sessions", ADMIN, "revoke pod browser sessions", workspace=WS, pod=POD, **YES),
    write("create_access_key", "create_access_key", ADMIN, "issue a port key", recorded=False, delivery="console",
          name="demo", **PORT, **YES),
    write("roll_access_key", "roll_access_key", ADMIN, "rotate a port key", recorded=False, delivery="console",
          key_id=12, **PORT, **YES),
    write("revoke_access_key", "revoke_access_key", ADMIN, "revoke a port key", key_id=12, **PORT, **YES),
    read("machines", "list_machines", HOST),
    read("machine_history", "get_downtime_history", HOST, machine=MACHINE_ID),
    read("host_messages", "list_host_messages", HOST, machine=MACHINE_ID),
    write("schedule_maintenance", "schedule_maintenance", HOST, "schedule downtime", machine=MACHINE_ID, reason="service",
          start="2030-01-02T09:00:00Z", end="2030-01-02T12:00:00Z", **YES),
    write("maintenance_window", "cancel_maintenance", HOST, "cancel planned downtime", maintenance_id=1, action="cancel", **YES),
    write("set_machine_listing", "set_machine_listing", HOST, "unlist a machine", machine=MACHINE_ID, listed=False, **YES),
    write("retry_machine_setup", "retry_machine_setup", HOST, "retry setup", machine=MACHINE_ID, **OP),
    write("delete_machine", "delete_machine", HOST, "remove a machine", machine=MACHINE_ID, **YES),
    write("send_host_message", "send_host_message", HOST, "send a message to renters", machine=MACHINE_ID,
          to="all", title="Maintenance notice", **YES),
    write("mark_host_messages_read", "mark_host_messages_read", HOST, "mark messages read", machine=MACHINE_ID, message_ids=[1], **OP),
    write("machine_credentials", "reveal_machine_credentials", HOST, "reveal a machine login and notify owner",
          recorded=False, delivery="console", machine=MACHINE_ID, kind="ssh", **YES),
    write("host_install_command", "issue_install_command", ACCOUNT, "issue an install token and notify owner",
          recorded=False, delivery="console", **YES),
    read("logs", "get_pod_logs", MEMBER, workspace=WS, pod=POD),
    write("send_feedback", "send_feedback", ACCOUNT, "send one feedback report", summary="The price label is missing.", **YES),
)

VARIANTS = (
    read("gpus", None, "anyone", scope="public", rental_type="spot", min_vram_gb=16),
    read("gpus", "list_gpus", ACCOUNT),
    read("gpus", "list_gpus", ACCOUNT, rental_type="spot"),
    read("gpus", "list_task_gpus", ACCOUNT, rental_type="task"),
    read("pods", "get_pod", MEMBER, scope="read write", delivery="console", workspace=WS, pod=POD, show_secrets=True),
    read("account", "list_api_keys", ACCOUNT, delivery="metadata", include_api_keys=True),
    read("tasks", "get_task", MEMBER, workspace=WS, task=TASK, include_code=True),
    write("retry_machine_setup", "retry_port_check", HOST, "request a port check", recorded=False,
          machine=MACHINE_ID, check="ports", **OP),
    write("send_host_message", "message_host", MEMBER, "send a renter message to the host", workspace=WS,
          pod=POD, title="Maintenance notice", **YES),
    write("mark_host_messages_read", "mark_workspace_messages_read", MEMBER, "mark renter messages read",
          workspace=WS, message_ids=[1], **OP),
)
