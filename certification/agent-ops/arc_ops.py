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
DRIVERS = {'local-docker': 'product_ops', 'aws-ecs': 'ecs_stage'}


def open_gates(target: str) -> list[str]:
    spec = json.loads(GATES.read_text()).get(target)
    if spec is None:
        return [f'no ops executor is defined for {target}']
    return [f"{gate['id']} ({gate['ref']})" for gate in spec['executors']
            if not gate.get('merged') or not gate.get('mergeCommit')]


class Unavailable:
    stage, lane = 'ops', 'arc_ops (executor gates)'

    def __init__(self, reason):
        self.reason = reason

    def unavailable(self, ctx):
        return self.reason


def arc_stage(ctx):
    gates = open_gates(ctx.target)
    if gates:
        return Unavailable('ops product executors not merged: ' + '; '.join(gates))
    driver = DRIVERS.get(ctx.target)
    try:
        module = __import__(driver)
    except ImportError:
        return Unavailable(f'the ops product-path driver ({driver}, #1164) is not on this branch')
    factory = getattr(module, 'arc_stage', None)
    if factory is None:
        return Unavailable(f'the ops product-path driver ({driver}) exports no arc_stage')
    return factory(ctx)
