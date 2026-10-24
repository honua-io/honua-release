"""Prepared command/SQLite/HTTP controls; no native/cloud qualification."""
import copy

import pytest

from agent_e2e_installed_container import McpOnlyExecutor
from agent_e2e_mcp import McpError
from agent_e2e_mcp_runner import run
from agent_e2e_worker import MCP_PROTOCOL, PackError
from test_agent_e2e_mcp import server
from test_agent_e2e_worker import CONFIG

E = 'execution-'+'1'*32


def actor(server):
    return {'endpoint': server.url, 'credentialKind': 'api-key', 'credential': 'private-issued-task',
        'workflowView': 'workspace', 'agentContext': {'workspaceId': 'original-workspace', 'turnId': 'original-turn'},
        'executionContext': {'executionId': E, 'claimGeneration': 1,
            'workerId': 'arn:aws:ecs:us-east-1:123456789012:task/original/'+'a'*32, 'capability': 'b'*64}}


def test_original_command_owns_action_header_and_sqlite_redelivery_never_repeats_it(server, tmp_path):
    config = {**CONFIG, 'attemptId': E}
    action = {'name': 'mcp_call_tool', 'input': {'name': 'observed-tool', 'arguments': {'actionId': 'model-cannot-retarget'}}}
    request = {'protocol': MCP_PROTOCOL, 'attemptId': E, 'actionId': 'original-command-action',
        'deadlineEpoch': 1120, 'timeoutSeconds': 120, 'action': action}
    calls = []
    def execute(value, seconds, _, *, action_id):
        calls.append(action_id)
        return {'status': 'completed', 'payload': run(value, actor(server), seconds, action_id=action_id)}
    executor = McpOnlyExecutor(config, tmp_path/'worker.sqlite', tmp_path, clock=lambda: 1000, execute_mcp=execute)
    try:
        first = executor.handle(request)
        assert first['payload']['error'] is None
        assert calls == ['original-command-action']
        for _, _, headers, _ in server.seen:
            normalized = {key.lower(): value for key, value in headers.items()}
            assert normalized['x-honua-execution-action'] == request['actionId']
            assert normalized['x-honua-execution'] == E
        count = len(server.seen)
        assert executor.handle(copy.deepcopy(request)) == first and len(server.seen) == count
        changed = copy.deepcopy(request)
        changed['action']['input']['arguments']['actionId'] = 'changed-model-argument'
        with pytest.raises(PackError, match='different content'): executor.handle(changed)
        assert len(server.seen) == count
    finally:
        executor.db.close()


@pytest.mark.parametrize('value', [None, '', 'two/actions', 'newline\r\nheader', True, 'a'*129])
def test_bound_worker_refuses_missing_or_malformed_action_before_native_http(server, value):
    action = {'name': 'mcp_list_tools', 'input': {}}
    if value is None:
        with pytest.raises(McpError, match='action-required'): run(action, actor(server), 30)
    else:
        observed = run(action, actor(server), 30, action_id=value)
        assert observed['error']['category'] == 'invalid-native-execution-action'
    assert not server.seen


def test_legacy_unbound_mcp_does_not_gain_execution_authority(server):
    config = actor(server)
    del config['executionContext']
    assert run({'name': 'mcp_call_tool', 'input': {'name': 'observed-tool', 'arguments': {}}}, config, 30)['error'] is None
    assert all('x-honua-execution-action' not in {key.lower() for key in headers} for _, _, headers, _ in server.seen)
