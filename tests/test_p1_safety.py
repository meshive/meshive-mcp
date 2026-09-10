"""P1 operation identity, loss consent and bounded opaque output regressions."""
import json
from decimal import Decimal

import httpx
import pytest
from meshive.models import Pod
from meshive_mcp.money import hourly
from meshive_mcp.serialize import normalize_number, to_dict
from meshive_mcp.tools._common import MAX_RESPONSE_BYTES, _bounded_result

pytestmark = pytest.mark.anyio


def payload(result):
    return json.loads(result.content[0].text)


def writes(fake, method):
    return [c for c in fake['last'].calls if c[0] == method]


async def test_preview_key_is_required_and_reused_on_reinvocation(mcp_client, fake, with_key):
    args = {'name': 'p', 'template_id': 457}
    preview = payload(await mcp_client.call_tool('create_pod', args))
    op = preview['operation_id']
    refused = payload(await mcp_client.call_tool('create_pod', {**args, 'confirm': True}))
    assert refused['code'] == 'operation_id_required' and not writes(fake, 'create_pod')
    for _ in range(2):
        result = payload(await mcp_client.call_tool('create_pod', {**args, 'confirm': True, 'operation_id': op}))
        assert result['operation_id'] == op
        assert writes(fake, 'create_pod')[0][2]['idempotency_key'] == op


async def test_unknown_write_error_retains_original_key(mcp_client, fake, with_key):
    def setup(client):
        client.raise_on['submit_task'] = httpx.ReadTimeout('offline')
    fake['setup'] = setup
    result = payload(await mcp_client.call_tool('submit_task', {
        'name': 't', 'script': 'pass', 'image': 'image', 'cpu_preset': 'micro-2c8g',
        'confirm': True, 'operation_id': 'task-timeout-0001'}))
    assert result['operation_id'] == 'task-timeout-0001'
    assert 'MUST reuse' in result['next_step'] and 'new operation' in result['next_step']
    assert writes(fake, 'submit_task')[0][2]['idempotency_key'] == 'task-timeout-0001'


async def test_any_node_requires_separate_data_loss_consent(mcp_client, fake, with_key):
    def setup(client):
        for pod in client.pods:
            pod.has_unpreserved_workspace = True
    fake['setup'] = setup
    args = {'pod': 'pod-2', 'placement': 'any_node', 'confirm': True, 'operation_id': 'move-pod-0001'}
    blocked = payload(await mcp_client.call_tool('start_pod', args))
    assert blocked['confirmed'] is False and blocked['requires_data_loss_consent'] is True
    assert 'permanently deleted' in blocked['question'] and not writes(fake, 'start_pod')
    result = payload(await mcp_client.call_tool('start_pod', {**args, 'allow_data_loss': True}))
    assert result['accepted'] is True
    assert writes(fake, 'start_pod')[0][2]['allow_data_loss'] is True


def test_strings_are_opaque_and_numeric_formatting_cannot_expand_exponents():
    data = {'label': '1.0', 'logs': ['1e999999', '1e99999999999999'], 'script': '0E-8'}
    assert to_dict(data) == data
    for value in data['logs']:
        assert normalize_number(value) == value
        assert len(to_dict(Decimal(value))) < 128
        assert hourly(value) == '-'
    pod = Pod.from_dict({'podName': '1.0', 'userAlias': '1.0', 'hasUnpreservedWorkspace': True})
    assert to_dict(pod)['user_alias'] == '1.0' and to_dict(pod)['has_unpreserved_workspace'] is True


def test_final_envelope_size_is_bounded_including_multibyte_text():
    result = _bounded_result({'logs': ['가' * MAX_RESPONSE_BYTES], 'operation_id': 'retained-key-0001'})
    assert result.is_error and payload(result)['code'] == 'response_too_large'
    assert payload(result)['operation_id'] == 'retained-key-0001'
    assert len(result.model_dump_json(by_alias=True).encode()) < MAX_RESPONSE_BYTES
