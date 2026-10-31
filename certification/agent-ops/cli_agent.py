"""Native CLI hook for real OPS scenarios only. Never run the simulated controller.

W1 registers real adapters in real_scenarios.REAL_SCENARIOS once seed, MCP
execution, live probes, independent readback and cleanup are wired. No such
scenario exists at this revision. This hook refuses before any CLI/model call.
"""
from __future__ import annotations
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools'))
from agent_cli_sessions import CliModelSession


def require_real_scenarios():
    try:
        module = importlib.import_module('real_scenarios')
    except ModuleNotFoundError as exc:
        if exc.name != 'real_scenarios': raise
        raise RuntimeError('not-real: OPS real scenarios are not registered; no CLI session started') from None
    registry = getattr(module, 'REAL_SCENARIOS', {})
    if not registry: raise RuntimeError('not-real: OPS has no executable real scenarios')
    return registry


def run_real_session(scenario_id, *, agent, model, context, prompt, log_dir, max_actions=32, timeout=600):
    scenario = require_real_scenarios().get(scenario_id)
    required = ('execute_mcp', 'observe_server', 'cleanup_real', 'tools_for_agent')
    if (scenario is None or getattr(scenario, 'execution_mode', None) != 'real'
            or any(not callable(getattr(scenario, name, None)) for name in required)):
        raise RuntimeError('not-real: incomplete real OPS execution/readback adapter')
    if context.get('server_readback') is not True or context.get('seed_verified') is not True:
        raise RuntimeError('not-real: real seeded deployment and independent probes required')
    try:
        result = CliModelSession(agent, model).run(prompt=prompt, tool_view=scenario.tools_for_agent(context),
            execute=lambda kind, action: scenario.execute_mcp(context, kind, action),
            log_dir=log_dir, max_actions=max_actions, timeout=timeout)
        # No transcript or final answer enters the independent state observer.
        observed = scenario.observe_server(context)
        return {'session': result, 'observations': observed, 'server_readback': True, 'cost': None}
    finally:
        scenario.cleanup_real(context)


def main(argv=None):
    require_real_scenarios()
    raise RuntimeError('not-real: W1 must wire seeded real trials before the OPS CLI campaign can run')


if __name__ == '__main__': raise SystemExit(main())
