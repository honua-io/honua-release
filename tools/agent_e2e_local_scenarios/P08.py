"""P08: unsupported. The local render helper requires independent browser network, label and region capture; the interface supplies no such capture or goal-selected saved-map binding."""
from __future__ import annotations


class Scenario:
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
