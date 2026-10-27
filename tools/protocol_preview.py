"""Protocol producers declared Preview evidence by a founder ruling (cng-preview-producer-2026-10-27).

A declared-Preview producer is still dispatched and its run is still recorded, but neither the
nightly bind nor the protocol-certification gate requires a candidate-bound receipt from it: its
skipped cells are reported as declared Preview instead of refusing the lock.

The exemption is narrow and ends by itself:
- only producers the named founder ruling lists in `preview_producers` are exempt, and only while
  the ruling is in docs/2026.1-release-decision-overrides.json;
- it lapses for a producer the moment any of that producer's cells carries an evidence receipt
  (honua-server#3753 lands), after which its cells are judged like every other producer's;
- a pass or fail is always judged normally, and every other producer's missing receipt still fails.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DECISION_OVERRIDES = ROOT / "docs/2026.1-release-decision-overrides.json"
PREVIEW_PRODUCER_RULING = "cng-preview-producer-2026-10-27"


def declared_preview_producers(path: Path | None = None) -> frozenset[str]:
    """Producers the founder ruling lists. A missing, malformed or non-founder ruling lists none."""
    try:
        rulings = json.loads((path or DECISION_OVERRIDES).read_text(encoding="utf-8"))["rulings"]
        ruling = rulings[PREVIEW_PRODUCER_RULING]
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset()
    producers = ruling.get("preview_producers") if isinstance(ruling, dict) else None
    if ("founder" not in str(ruling.get("owner", "")).lower() or not isinstance(producers, list)
            or not producers or not all(isinstance(item, str) and item for item in producers)):
        return frozenset()
    return frozenset(producers)


def selects(entry: dict[str, Any], row: dict[str, Any]) -> bool:
    """certification/generate-protocol-requirements.py `selects`: the producer that owns a row."""
    lane = str(row.get("client_lane", ""))
    if any(fnmatch.fnmatchcase(lane, pattern) for pattern in entry.get("except_client_lanes", [])):
        return False
    return any(fnmatch.fnmatchcase(lane, pattern) for pattern in entry.get("client_lanes", [])) \
        or row.get("deployment_target") in entry.get("deployment_targets", [])


def active_preview_producers(catalog: dict[str, Any], cells: list[dict[str, Any]],
                             path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Declared-Preview producers whose exemption still holds for this ledger, by name.

    A producer's exemption holds only while none of its cells carries an evidence receipt."""
    declared = declared_preview_producers(path)
    producers = ((catalog or {}).get("production") or {}).get("producers") or []
    active = {}
    for entry in producers:
        if not isinstance(entry, dict) or entry.get("producer") not in declared:
            continue
        owned = [cell for cell in cells if isinstance(cell, dict) and selects(entry, cell)]
        if not any(isinstance(cell.get("evidence_receipt"), dict) for cell in owned):
            active[entry["producer"]] = entry
    return active


def preview_owner(cell: dict[str, Any], active: dict[str, dict[str, Any]]) -> str | None:
    """The active declared-Preview producer that owns a skipped cell, else None."""
    if cell.get("result") != "skip" or cell.get("evidence_receipt") is not None:
        return None
    return next((name for name, entry in active.items() if selects(entry, cell)), None)
