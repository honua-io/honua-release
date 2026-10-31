"""P03 is unsupported for local A/B evaluation.

Cross-tenant denial requires a separate tenant and positive control; multi-tenant work is prohibited in this lane.
No controller evidence is fabricated; calls fail closed until this is resolved.
"""
from __future__ import annotations

id = "P03"
supported_variants = ()


def _unsupported():
    raise NotImplementedError("P03: independent evidence is unavailable; see module docstring")


def seed(stack, variant):
    _unsupported()


def fault_plan(variant):
    _unsupported()


def needs_approver():
    return False


def observe(stack, seeded, variant):
    _unsupported()


def expected(variant):
    _unsupported()


def cleanup(stack, seeded):
    _unsupported()
