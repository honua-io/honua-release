"""One recorded agent session for the Studio app eval, on the server's Studio MCP surface."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from agent_cli_backends import HttpMcpBackend
from agent_cli_sessions import CliFailure, CliModelSession

ARMS = {
    'opus': ('claude-cli', 'claude-opus-5-5', '2.1.316 (Claude Code)'),
    'sol': ('codex-cli', 'gpt-6.1-sol', 'codex-cli 0.166.0'),
}
DISCOVERY_TOOLS = frozenset(('honua_list_layers', 'honua_describe_layer'))
MAX_ACTIONS, MAX_TURNS, TIMEOUT_SECONDS = 80, 40, 1800
PROMPT = (
    'Build a public web app for residents from our two published services, "Community parks" and '
    '"Public libraries". The app needs a map showing both, a table of parks, a chart of parks by '
    'category, a filter by district, and a popup that shows details when someone selects a feature. '
    'Save it and submit it for publication at the route {route} so anyone can open it without signing in. '
    'Tell me what you submitted and what still has to happen before it is live.'
)


@dataclass(frozen=True)
class SessionInputs:
    arm: str
    binary: Path
    mcp_endpoint: str
    proposer_key: str


def studio_surface(backend: HttpMcpBackend) -> dict:
    tools = [tool for tool in backend.tool_view()['tools']
             if tool['name'].startswith('honua_studio_') or tool['name'] in DISCOVERY_TOOLS]
    if not any(tool['name'] == 'honua_studio_propose_publication' for tool in tools):
        raise ValueError('server MCP does not offer Studio publication proposals')
    return {'tools': tools}


def run_session(inputs: SessionInputs, route: str, log_dir: Path) -> dict:
    agent, model, version = ARMS[inputs.arm]
    backend = HttpMcpBackend(inputs.mcp_endpoint, header='X-API-Key', token=inputs.proposer_key,
                             workflow_view='full').start()
    servers = {'honua': {'tool_view': studio_surface(backend), 'execute': backend.execute}}
    session = CliModelSession(agent, model, binary=str(inputs.binary), expected_version=version)
    started = time.monotonic()
    try:
        result = session.run(prompt=PROMPT.format(route=route), servers=servers, log_dir=log_dir,
                             max_actions=MAX_ACTIONS, max_turns=MAX_TURNS, timeout=TIMEOUT_SECONDS)
        result['outcome'] = 'completed'
    except CliFailure as failure:
        result = {**failure.progress, 'outcome': failure.category, 'failure': str(failure)}
    result['wallSeconds'] = round(time.monotonic() - started, 3)
    return result


def recorded_calls(log_dir: Path) -> list[dict]:
    calls = []
    for path in sorted(log_dir.glob('action-*.json')):
        response = path.with_suffix('.response')
        calls.append({'action': json.loads(path.read_bytes()),
                      'response': json.loads(response.read_bytes()) if response.is_file() else None})
    return calls
