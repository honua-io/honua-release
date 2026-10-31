"""P05 is unsupported for local A/B evaluation.

The invalid-row reporting assertion requires an independent record of what was reported; the interface prohibits reading the model transcript and supplies no independent report artifact.
No controller evidence is fabricated; calls fail closed until this is resolved.
"""
from __future__ import annotations

id = "P05"
supported_variants = ()


def _unsupported():
    raise NotImplementedError("P05: independent evidence is unavailable; see module docstring")


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
