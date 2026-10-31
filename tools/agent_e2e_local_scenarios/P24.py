"""P24: unsupported on the frozen local runner interface.

The runner supplies an already running stack and no bootstrap credential revocation or worker replacement evidence. The interface also lacks an independent fresh-session identity for reopening.
"""
from __future__ import annotations

from agent_e2e_local_api import Observation, Seeded, Stack

id = "P24"
supported_variants: tuple[str, ...] = ()


def _unsupported() -> None:
    raise RuntimeError("P24 is unsupported: see module docstring")


def seed(stack: Stack, variant: str) -> Seeded:
    _unsupported()


def fault_plan(variant: str) -> dict | None:
    _unsupported()


def needs_approver() -> bool:
    return True


def observe(stack: Stack, seeded: Seeded, variant: str) -> list[Observation]:
    _unsupported()


def expected(variant: str) -> dict:
    _unsupported()


def cleanup(stack: Stack, seeded: Seeded) -> None:
    _unsupported()
