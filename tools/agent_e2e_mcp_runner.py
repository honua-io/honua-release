"""Worker-only MCP process. No controller credentials, oracle or provisioning."""
from __future__ import annotations

import json
import os
import sys

from agent_e2e_mcp import Client, McpError


def run(action, config, seconds):
    fields = {'endpoint', 'credentialKind', 'credential', 'workflowView'}
    if (not isinstance(config, dict) or not fields <= set(config) <= fields | {'agentContext', 'executionContext'}
            or config['credential'] is not None and not isinstance(config['credential'], str)):
        raise McpError('invalid-agent-mcp-configuration')
    token_key = 'HONUA_AGENT_MCP_TOKEN'
    previous = os.environ.get(token_key)
    if config['credential'] is not None:
        os.environ[token_key] = config['credential']
    client = None
    payload = {'result':None, 'error':None, 'observations':[], 'sessionCleanup':{'status':'not-started'}}
    try:
        client = Client(config['endpoint'], credential_kind=config['credentialKind'],
            credential_env=token_key if config['credential'] is not None else None,
            workflow_view=config['workflowView'], timeout=min(seconds, 120),
            **({'agent_context': config['agentContext']} if 'agentContext' in config else {}),
            **({'execution_context': config['executionContext']} if 'executionContext' in config else {}))
        client.initialize()
        if action['name'] == 'mcp_list_tools' and set(action['input']) <= {'view'}:
            payload['result'] = client.list_tools(**action['input'])
        elif action['name'] == 'mcp_call_tool' and set(action['input']) == {'name','arguments'}:
            payload['result'] = client.call_tool(action['input']['name'], action['input']['arguments'])
        else:
            raise McpError('invalid-agent-mcp-action')
    except McpError as exc:
        payload['error'] = {'category':exc.category, 'rpcError':exc.public_error}
    finally:
        if client is not None:
            try:
                client.close()
                payload['sessionCleanup'] = {'status':'pass'}
            except McpError as exc:
                payload['sessionCleanup'] = {'status':'fail', 'category':exc.category}
            payload['observations'] = client.observations
        if previous is None:
            os.environ.pop(token_key, None)
        else:
            os.environ[token_key] = previous
    return payload


def main():
    try:
        raw = sys.stdin.buffer.read(100001)
        if len(raw) > 100000:
            raise McpError('oversized-agent-mcp-request')
        request = json.loads(raw)
        config_raw = os.environ.pop('HONUA_AGENT_MCP_CONFIG', '')
        if len(config_raw.encode()) > 16384:
            raise McpError('oversized-agent-mcp-configuration')
        payload = run(request['action'], json.loads(config_raw), request['seconds'])
        sys.stdout.write(json.dumps(payload, sort_keys=True, allow_nan=False))
        return 0
    except Exception:
        # Never print credential-bearing configuration or exception text.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
