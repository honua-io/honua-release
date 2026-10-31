"""P12: unsupported on the frozen local runner interface.

The interface has no independent restart timeline or fresh-session identity linking the saved document to the reopened document and its protected source baseline.
"""
from __future__ import annotations

from agent_e2e_local_api import Observation, Seeded, Stack

id = "P12"
supported_variants: tuple[str, ...] = ()


def _unsupported() -> None:
    raise RuntimeError("P12 is unsupported: see module docstring")


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
