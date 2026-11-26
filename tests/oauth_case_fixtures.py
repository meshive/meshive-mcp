"""Feature fixtures for SDK calls that prepare an OAuth acceptance case's business operation."""
import pytest

from test_assets_settings_tools import settings_fake
from test_asset_files_tools import files_fake
from test_fan_curve_tools import fan_fake
from test_inference_webhook_tools import state as inference_fake
from test_pod_assets_tools import pa_fake
from test_templates_messages_tools import misc
from test_workspace_account_tools import account_fake


@pytest.fixture
def case_prerequisites(case, request):
    fixtures = {
        "save_template": "misc", "add_pod_asset": "pa_fake", "export_asset": "files_fake",
        "save_webhook": "inference_fake", "delete_webhook": "inference_fake",
        "pod_history": "account_fake", "remove_member": "account_fake", "workspaces": "account_fake",
        "machines": "fan_fake", "set_default_output_bucket": "settings_fake",
        "delete_credential": "settings_fake", "import_asset": "settings_fake", "asset_files": "settings_fake",
        "source_credentials": "settings_fake",
    }
    if case.tool in fixtures:
        request.getfixturevalue(fixtures[case.tool])
