"""P23 is unsupported on local Docker.

TownSetup creates town data and services, not the owned project with shared
and foreign resources required by this case. The interface carries an ownership
inventory only if seed can establish one; no available local adapter establishes
and independently enumerates that project inventory. Cleanup completeness
therefore cannot be proved.
"""
from __future__ import annotations

id = "P23"
supported_variants: tuple[str, ...] = ()


def _unsupported():
    raise NotImplementedError("P23 has no independently observable local implementation")


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
