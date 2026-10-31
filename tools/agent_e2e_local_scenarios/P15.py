"""Unsupported on the frozen local Stack: publication.private and tenant.denied require
authenticated operator, owner and outsider controls against the same saved map,
including native tenant identity. Stack has no operator or outsider credential binding.
custody.bootstrap-revoked and custody.worker-replaced also require original bootstrap
retirement and isolated worker evidence. StudioTenantObserver and StudioCustodyObserver
cannot be replaced by an admin read or an anonymous denial.

All pack assertions remain present with unavailable observations. This is an
unsupported case declaration, not a partial qualification. No setup, approval,
publication, key operation or model-visible guidance is performed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_e2e_local_api import Observation, Seeded, Stack

id = "P15"
supported_variants: tuple[str, ...] = ()
_EXPECTATIONS = (Path(__file__).resolve().parents[2] /
                 "certification/agent-e2e/expectations/local" / (id + ".json"))


def _manifest(variant: str) -> dict[str, Any]:
    if variant not in ("A", "B"):
        raise ValueError("Only development variants A and B are available")
    return json.loads(_EXPECTATIONS.read_text(encoding="utf-8"))


def seed(stack: Stack, variant: str) -> Seeded:
    """Refuse before any side effect; repeating this refusal creates nothing."""
    manifest = _manifest(variant)
    raise NotImplementedError(id + ": " + manifest["unsupportedReason"])


def fault_plan(variant: str) -> dict | None:
    _manifest(variant)
    return None


def needs_approver() -> bool:
    return False


def observe(stack: Stack, seeded: Seeded, variant: str) -> list[Observation]:
    """Fail closed even if a caller bypasses supported_variants and seed refusal."""
    manifest = _manifest(variant)
    return [Observation(assertion, None,
                        {"requests": [], "unsupportedReason": manifest["unsupportedReason"]})
            for assertion in manifest["variants"][variant]]


def expected(variant: str) -> dict[str, Any]:
    """Unavailable frozen facts remain null; oracle.verify rejects every one."""
    return _manifest(variant)["variants"][variant]


def cleanup(stack: Stack, seeded: Seeded) -> None:
    """No fixture was created; never adopt or delete caller-supplied resources."""
    if seeded.state:
        raise ValueError("Unsupported scenario cannot own seeded resources")
