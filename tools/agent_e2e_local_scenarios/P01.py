"""P01: unsupported on the frozen local runner interface.

The runner supplies an already running stack, with no independent bootstrap credential revocation or worker replacement evidence. The install helper binds an existing deployment and cannot prove that the task installed it.
"""
from __future__ import annotations

from agent_e2e_local_api import Observation, Seeded, Stack

id = "P01"
supported_variants: tuple[str, ...] = ()


def _unsupported() -> None:
    raise RuntimeError("P01 is unsupported: see module docstring")


def seed(stack: Stack, variant: str) -> Seeded:
    _unsupported()


def fault_plan(variant: str) -> dict | None:
    _unsupported()


def needs_approver() -> bool:
    return False


def observe(stack: Stack, seeded: Seeded, variant: str) -> list[Observation]:
    _unsupported()


def expected(variant: str) -> dict:
    _unsupported()


def cleanup(stack: Stack, seeded: Seeded) -> None:
    _unsupported()
