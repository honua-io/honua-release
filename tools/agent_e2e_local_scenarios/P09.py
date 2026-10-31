"""P09: Unsupported: render.actual needs independent signed-in browser capture, runtime assets and a bound saved version. The seeded map also needs assertion-specific style, save and source projections."""
from __future__ import annotations

from agent_e2e_local_api import Scenario as ScenarioBase


class P09(ScenarioBase):
    id = "P09"
    supported_variants: tuple[str, ...] = ()

    def seed(self, stack, variant):
        raise RuntimeError("P09 has no independent local observer")

    def fault_plan(self, variant):
        return None

    def needs_approver(self):
        return False

    def observe(self, stack, seeded, variant):
        raise RuntimeError("P09 has no independent local observer")

    def expected(self, variant):
        raise RuntimeError("P09 has no independent local observer")

    def cleanup(self, stack, seeded):
        raise RuntimeError("P09 has no independent local observer")


scenario = P09()
