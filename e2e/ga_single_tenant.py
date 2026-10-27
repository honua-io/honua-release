"""2026.1 GA cloud cells run the terminal journey single-tenant (founder ruling
ga-cells-single-tenant-2026-10-27; ADR-0024: multi-tenancy is internal only).

Two effects, both narrow and both fail-closed:
- On a cell the ruling lists, the journey runs single-tenant: the run-owned issuer stays
  provisioned (S11 still mints its bearer from it) but the journey's principals are not re-signed
  into tenants, so the proposer stays the minted API key (`journey_tenant_key`).
- There, a receipt is admitted only when 8.tenant-isolation is its single non-passing check,
  blocked by the tracked second-tenant gap (#377), and every other check passes, including the
  rest of stages 7 and 8 (proposal and approval). That check is then declared not-applicable.

A missing, malformed or non-founder ruling, or one naming any cell outside the GA aws-ecs cells,
lists nothing: the journey keeps its tenant mode and every receipt is judged as before.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DECISION_OVERRIDES = ROOT / "docs/2026.1-release-decision-overrides.json"
RULING = "ga-cells-single-tenant-2026-10-27"
GA_CELLS = frozenset({"aws-ecs/redis-off", "aws-ecs/redis-on"})
CHECK = "8.tenant-isolation"
STAGE = 8
BLOCKER = "https://github.com/honua-io/honua-release/issues/377"
PROFILE = "ga-single-tenant"


def ruled_cells(path: Path | None = None) -> frozenset[str]:
    """GA cells the founder ruling runs single-tenant; empty for anything but an exact ruling."""
    try:
        ruling = json.loads((path or DECISION_OVERRIDES).read_text(encoding="utf-8"))["rulings"][RULING]
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()
    if not isinstance(ruling, dict):
        return frozenset()
    cells, checks = ruling.get("cells"), ruling.get("not_applicable_checks")
    if ("founder" not in str(ruling.get("owner", "")).lower()
            or not isinstance(cells, list) or not cells
            or not all(isinstance(cell, str) for cell in cells) or not set(cells) <= GA_CELLS
            or checks != [CHECK]):
        return frozenset()
    return frozenset(cells)


def single_tenant(cell: str, path: Path | None = None) -> bool:
    return cell in ruled_cells(path)


def journey_tenant_key(cell: str, tenant_signing_key: str | None, path: Path | None = None) -> str | None:
    """The key the journey may sign tenant principals with: none on a single-tenant GA cell."""
    return None if single_tenant(cell, path) else tenant_signing_key


def admits(receipt: Any, cell: str, path: Path | None = None) -> bool:
    """True only for a live single-tenant receipt whose sole non-pass is the declared check."""
    if not single_tenant(cell, path) or not isinstance(receipt, dict):
        return False
    if (receipt.get("status") != "blocked" or receipt.get("mode") != "live"
            or not isinstance(receipt.get("roster"), dict) or receipt["roster"].get("status") != "pass"
            or not isinstance(receipt.get("target"), dict) or receipt["target"].get("id") != cell):
        return False
    stages = receipt.get("stages")
    if not isinstance(stages, list) or not stages:
        return False
    non_pass = []
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("checks"), list) or not stage["checks"]:
            return False
        if (stage.get("evidence") or {}).get("source") != "live-" + cell.split("/")[0]:
            return False
        for check in stage["checks"]:
            if not isinstance(check, dict):
                return False
            if check.get("status") != "pass":
                non_pass.append((stage, check))
        if stage.get("number") != STAGE and stage.get("status") != "pass":
            return False
    if len(non_pass) != 1:
        return False
    stage, check = non_pass[0]
    return (stage.get("number") == STAGE and stage.get("status") == "blocked"
            and stage.get("blockedBy") == [BLOCKER]
            and check.get("id") == CHECK and check.get("status") == "blocked"
            and check.get("blockedBy") == [BLOCKER])


def declaration(cell: str) -> dict[str, Any]:
    """How an admitted receipt is reported: the raw verdict stays in the receipt itself."""
    return {"profile": PROFILE, "ruling": RULING, "cell": cell, "notApplicable": [CHECK]}
