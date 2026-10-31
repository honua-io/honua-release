"""P05 CSV import remains unsupported.

The runner needs a controller read of an agent-imported table before publication;
a sealed import report is retained when the agent actually invokes the tool.
"""
from agent_e2e_local_api import Scenario, Seeded


class P05(Scenario):
    id = 'P05'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        return Seeded({'variant': variant})

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        actions = [action for action in recorder.actions()
                   if action.kind == 'tool_call' and action.tool == 'honua_ingest_dataset']
        if actions:
            recorder.action_result('p05-import-report', actions[-1])
        return []


scenario = P05()
