"""P21 is unsupported on local Docker.

The fault event supplies kind, location and request ID, but no independently
bound committed map or injection ID for the oracle's fault and recovery
bindings. Model-created publication identities also cannot be frozen from town
fixtures. Enabling the case would allow a 503 unrelated to a committed map to
stand in for the required post-commit failure.
"""
from __future__ import annotations

id = "P21"
supported_variants: tuple[str, ...] = ()


def _unsupported():
    raise NotImplementedError("P21 has no independently observable local implementation")


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
