"""Stage adapter for the release-owned AI arc runner.

The arc runner owns slot provisioning, approval and independent observations.
It provides live backends (including a long-lived devops process for stage 1)
and calls this adapter once per stage. This module never provisions resources.
"""
from __future__ import annotations

from agent_cli_sessions import CliModelSession
from agent_cli_backends import HttpMcpBackend


def run_stage(*, agent, model, stage, target_kind, prompt, backends, log_dir,
              max_actions=80, max_turns=40, timeout=600, cli_factory=CliModelSession):
    if stage not in (1,2,3,4) or target_kind not in ('remote-vm','aws-ecs'):
        raise ValueError('invalid arc stage or target')
    required = ('quickstart_shell' if target_kind == 'remote-vm' else 'honua_devops') if stage == 1 else (
        'honua' if stage in (2,3) else 'studio')
    if required not in backends:
        raise ValueError('stage is missing its required product MCP backend')
    if set(backends) != {required}:
        raise ValueError('the arc stage may expose only its own product surface')
    if required == 'honua' and isinstance(backends[required], HttpMcpBackend) and backends[required].workflow_view != 'full':
        raise ValueError('the installed server must expose its verified full MCP tool view')
    if 'quickstart_shell' in backends and (stage != 1 or target_kind != 'remote-vm'):
        raise ValueError('shell is available only for a remote VM quickstart')
    health = backends['quickstart_shell'].check_health() if required == 'quickstart_shell' else None
    if health is not None and health.get('status') != 'pass':
        raise ValueError('remote VM Docker health did not pass')
    servers = {name: {'tool_view': backend.tool_view(), 'execute': backend.execute}
               for name, backend in backends.items()}
    result = cli_factory(agent, model).run(prompt=prompt, servers=servers, stage=stage,
        target_kind=target_kind, log_dir=log_dir, session=stage,
        max_actions=max_actions, max_turns=max_turns, timeout=timeout)
    if health is not None: result['targetHealth'] = health
    return result
