"""P19: Unsupported: the runner needs controller-side DNS and egress capture, independent action and shared-resource observations, an artifact secret scan and real worker-custody evidence."""
from __future__ import annotations

from agent_e2e_local_api import Scenario as ScenarioBase


class P19(ScenarioBase):
    id = "P19"
    supported_variants: tuple[str, ...] = ()

    def seed(self, stack, variant):
        raise RuntimeError("P19 has no independent local observer")

    def fault_plan(self, variant):
        return None

    def needs_approver(self):
        return False

    def observe(self, stack, seeded, variant):
        raise RuntimeError("P19 has no independent local observer")

    def expected(self, variant):
        raise RuntimeError("P19 has no independent local observer")

    def cleanup(self, stack, seeded):
        raise RuntimeError("P19 has no independent local observer")


scenario = P19()
