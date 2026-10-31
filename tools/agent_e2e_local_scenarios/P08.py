"""P08: Unsupported: render.actual needs independent signed-in browser capture, runtime assets and a bound saved version. The runner must provide those plus assertion-specific map and source projections."""
from __future__ import annotations

from agent_e2e_local_api import Scenario as ScenarioBase


class P08(ScenarioBase):
    id = "P08"
    supported_variants: tuple[str, ...] = ()

    def seed(self, stack, variant):
        raise RuntimeError("P08 has no independent local observer")

    def fault_plan(self, variant):
        return None

    def needs_approver(self):
        return False

    def observe(self, stack, seeded, variant):
        raise RuntimeError("P08 has no independent local observer")

    def expected(self, variant):
        raise RuntimeError("P08 has no independent local observer")

    def cleanup(self, stack, seeded):
        raise RuntimeError("P08 has no independent local observer")


scenario = P08()
