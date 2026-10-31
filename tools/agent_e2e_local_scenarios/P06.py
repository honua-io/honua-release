"""P06 is unsupported for local A/B evaluation.

TownSetup does not seed survey points, and Stack supplies no separate restricted principal for a write-denial probe with an authorized positive control.
No controller evidence is fabricated; calls fail closed until this is resolved.
"""
from __future__ import annotations

id = "P06"
supported_variants = ()


def _unsupported():
    raise NotImplementedError("P06: independent evidence is unavailable; see module docstring")


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
