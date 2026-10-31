"""P11: unsupported. The managed job observer requires original action and native job bindings unavailable through the local scenario interface; completion and mapped output cannot be independently joined."""
from __future__ import annotations


class Scenario:
    id = "P11"
    supported_variants: tuple[str, ...] = ()

    def seed(self, stack, variant):
        raise RuntimeError("P11 has no independent local observer")

    def fault_plan(self, variant):
        return None

    def needs_approver(self):
        return False

    def observe(self, stack, seeded, variant):
        raise RuntimeError("P11 has no independent local observer")

    def expected(self, variant):
        raise RuntimeError("P11 has no independent local observer")

    def cleanup(self, stack, seeded):
        raise RuntimeError("P11 has no independent local observer")
