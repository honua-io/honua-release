"""AI arc stage 2 (ops): the product-path driver, refusing until the product executors exist.

The arc's OpsStage delegates here (``arc_stage``). The ops stage only counts what the product executes
(prereg "Ops stage scope"): the model uses the installed server's /mcp ops and ``honua_propose_*``
tools, the harness approves as the separate principal, and the product's executor applies. The
driver is #1164's (``product_ops`` / ``ecs_stage``); the executor gates are data in
``arc-executors.json``. Until every gate for the target is merged (with its merge commit recorded)
and the driver exports ``arc_stage``, the stage is unavailable with the open gates named; nothing
here performs or simulates a product operation.
"""
from __future__ import annotations

import json
from pathlib import Path

GATES = Path(__file__).resolve().parent / 'arc-executors.json'
# local-docker: the arc's own Compose-executor driver (tools/ai_arc_ops_local.py); aws-ecs: #1164.
DRIVERS = {'local-docker': 'ai_arc_ops_local', 'aws-ecs': 'ecs_stage'}


def open_gates(target: str, scored=None) -> list[str]:
    """Unmerged gates the target's ops stage waits on: under a scope that scores only some assertions
    (``scored``), only gates whose executor covers one of them; with ``scored`` None (the full scope),
    every gate. A gate counts as merged only with its merge commit recorded."""
    spec = json.loads(GATES.read_text()).get(target)
    if spec is None:
        return [f'no ops executor is defined for {target}']
    wanted = None if scored is None else set(scored)
    return [f"{gate['id']} ({gate['ref']})" for gate in spec['executors']
            if (not gate.get('merged') or not gate.get('mergeCommit'))
            and (wanted is None or not isinstance(gate.get('covers'), list) or wanted & set(gate['covers']))]


def scored_assertions(ctx):
    """The assertions the target's founder-approved ops scope scores; None under the full scope."""
    from ai_arc_runner import ops_scope
    scope = ops_scope(ctx.arc, ctx.target)
    return None if scope['mode'] != 'executed-subset' else scope['scored']


class EcsOps:
    """aws-ecs ops through #1164's ``ecs_stage.run_ecs_ops_stage(slot, session, ledger)`` once it exists:
    the session is the arc's recorded CLI session and the ledger the stage's private action ledger."""
    stage, lane = 'ops', '#1164 ecs_stage.run_ecs_ops_stage'

    def __init__(self, entry):
        self.entry, self.result = entry, None

    def unavailable(self, ctx):
        return None

    def prepare(self, ctx):
        return None

    def notes(self, ctx):
        return None

    def attachment(self, ctx):
        return None

    def run(self, ctx, session, prompt):
        ledger = ctx.private_dir / 'ops' / 'ledger'
        ledger.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.result = self.entry(ctx.slot, session, ledger) or {}
        return self.result

    def observe(self, ctx, row):
        return list((self.result or {}).get('assertions') or [])


def ecs_entry():
    try:
        module = __import__(DRIVERS['aws-ecs'])
    except ImportError:
        return None
    return getattr(module, 'run_ecs_ops_stage', None)


class Unavailable:
    stage, lane = 'ops', 'arc_ops (executor gates)'

    def __init__(self, reason):
        self.reason = reason

    def unavailable(self, ctx):
        return self.reason


def arc_stage(ctx):
    gates = open_gates(ctx.target, scored_assertions(ctx))
    if gates:
        return Unavailable('ops product executors not merged: ' + '; '.join(gates))
    driver = DRIVERS.get(ctx.target)
    if ctx.target == 'aws-ecs':
        entry = ecs_entry()
        if entry is None:
            return Unavailable('the ECS ops entry point (#1164 ecs_stage.run_ecs_ops_stage(slot, session, ledger)) '
                               'is not on this branch')
        return EcsOps(entry)
    try:
        module = __import__(driver)
    except ImportError:
        return Unavailable(f'the ops product-path driver ({driver}) is not on this branch')
    factory = getattr(module, 'arc_stage', None)
    if factory is None:
        return Unavailable(f'the ops product-path driver ({driver}) exports no arc_stage')
    return factory(ctx)
