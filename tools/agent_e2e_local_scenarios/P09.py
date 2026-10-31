"""P09: unsupported. Independent local browser render capture and a goal-selected saved-version binding are unavailable; a style document alone cannot prove rendered labels."""
from __future__ import annotations


class Scenario:
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
