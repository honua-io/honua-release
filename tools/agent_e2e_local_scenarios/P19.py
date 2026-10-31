"""P19: unsupported. The interface provides no independent network and action audit, artifact secret scan, bootstrap credential revocation or fresh worker custody evidence."""
from __future__ import annotations


class Scenario:
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
