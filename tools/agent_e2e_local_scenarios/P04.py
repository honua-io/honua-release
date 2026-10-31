"""P04 is unsupported for local A/B evaluation.

The frozen Stack interface does not bind the model-visible input files or a controller-owned before snapshot, so source preservation cannot be independently established.
No controller evidence is fabricated; calls fail closed until this is resolved.
"""
from __future__ import annotations

id = "P04"
supported_variants = ()


def _unsupported():
    raise NotImplementedError("P04: independent evidence is unavailable; see module docstring")


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
