"""P22 is unsupported on local Docker.

A two-session count does not provide the distinct session identities or
conversation visibility required by session.fresh. The starting partial map and
its immutable version are not established by the runner or TownSetup, and the
oracle requires frozen resource and publication bindings.
"""
from __future__ import annotations

id = "P22"
supported_variants: tuple[str, ...] = ()


def _unsupported():
    raise NotImplementedError("P22 has no independently observable local implementation")


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
