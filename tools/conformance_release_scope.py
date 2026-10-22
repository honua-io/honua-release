#!/usr/bin/env python3
"""Release-scope decisions for the cross-repo conformance gate (certification.yml `report`).

A scope decision moves a conformance lane from required to advisory for named platform releases.
The lane keeps running and its row stays in gate-report.json with its own status. The report marks
it `decided: advisory` and labels it with the decision's `reportAs` text (for example
"not certified for 2026.1"). Advisory rows do not count toward `overallStatus`.

This is an explicit, dated scope decision, not a skipped check. Every entry must name the releases
it covers, the date of the decision, and the reason. A malformed entry is refused (exit 2) rather
than read as "no decisions", because a silently ignored decision and a silently applied one are both
wrong. Decisions live in certification/conformance-evidence.yaml under `releaseScope`.

CLI:
  python tools/conformance_release_scope.py --manifest platform-manifest.yaml [--config ...]
prints {"<gate>": {requirement, decidedOn, reason, reportAs}, ...} for the manifest's release.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "certification" / "conformance-evidence.yaml"
REQUIREMENTS = frozenset({"advisory"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ScopeError(ValueError):
    """A releaseScope entry is malformed."""


def base_release(platform_release: str) -> str:
    """`2026.1-rc.2` -> `2026.1`; `2026.1` -> `2026.1`."""
    return str(platform_release or "").split("-", 1)[0].strip()


def scope_for(config: dict, platform_release: str) -> dict[str, dict]:
    """Return the validated scope decisions that apply to `platform_release`, keyed by gate."""
    entries = (config or {}).get("releaseScope") or {}
    if not isinstance(entries, dict):
        raise ScopeError("releaseScope must be a mapping of gate -> decision")
    release = base_release(platform_release)
    out: dict[str, dict] = {}
    for gate, entry in entries.items():
        if not isinstance(entry, dict):
            raise ScopeError(f"releaseScope.{gate} must be a mapping")
        requirement = entry.get("requirement")
        if requirement not in REQUIREMENTS:
            raise ScopeError(f"releaseScope.{gate}.requirement must be one of {sorted(REQUIREMENTS)}")
        releases = entry.get("releases")
        if not isinstance(releases, list) or not releases or not all(isinstance(r, str) and r for r in releases):
            raise ScopeError(f"releaseScope.{gate}.releases must be a non-empty list of release labels")
        decided_on = str(entry.get("decidedOn") or "")
        if not _DATE_RE.match(decided_on):
            raise ScopeError(f"releaseScope.{gate}.decidedOn must be a YYYY-MM-DD date")
        for field in ("reason", "reportAs"):
            if not str(entry.get(field) or "").strip():
                raise ScopeError(f"releaseScope.{gate}.{field} is required")
        if release and release in {base_release(r) for r in releases}:
            out[str(gate)] = {
                "requirement": requirement,
                "decidedOn": decided_on,
                "reason": str(entry["reason"]).strip(),
                "reportAs": str(entry["reportAs"]).strip(),
            }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    try:
        scope = scope_for(config, str(manifest.get("platformRelease") or ""))
    except ScopeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    print(json.dumps(scope, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
