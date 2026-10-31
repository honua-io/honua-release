"""P13: unsupported. The interface provides no independent original edit/restore lineage or complete shared-resource inventory; a matching document alone cannot prove restoration and preservation."""
from __future__ import annotations


class Scenario:
    id = "P13"
    supported_variants: tuple[str, ...] = ()

    def seed(self, stack, variant):
        raise RuntimeError("P13 has no independent local observer")

    def fault_plan(self, variant):
        return None

    def needs_approver(self):
        return False

    def observe(self, stack, seeded, variant):
        raise RuntimeError("P13 has no independent local observer")

    def expected(self, variant):
        raise RuntimeError("P13 has no independent local observer")

    def cleanup(self, stack, seeded):
        raise RuntimeError("P13 has no independent local observer")
