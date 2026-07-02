#!/usr/bin/env python3
"""Advertised-vs-actual docs gate (gate h) — every advertised capability must be backed by something
real, or be explicitly labelled roadmap.

The audit found capabilities advertised with no backing (a fabricated example citing a non-existent
SDK). This gate makes that structurally impossible: docs/capabilities.yaml lists each claim, and a
`shipped` claim FAILS unless its `evidence` resolves to a real artefact in THIS repo —
  kind: canonical-check  ref: a check name actually defined in e2e/canonical_checks.py
  kind: gate             ref: a wired release-train gate id (parsed from release-train.yml)
  kind: test             ref: a test file that exists
A `roadmap` claim passes (honestly labelled, no test required); an unknown status fails.

  check(capabilities, known_checks, known_gates) -> (rows, overall)   # pure, unit-tested
  python tools/check_capabilities.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES_PATH = REPO_ROOT / "docs" / "capabilities.yaml"
VALID_STATUS = {"shipped", "roadmap"}


def known_canonical_checks() -> set[str]:
    """The check names actually defined in the e2e canonical set (resolved by introspection)."""
    sys.path.insert(0, str(REPO_ROOT / "e2e"))
    import canonical_checks as cc  # noqa: E402
    return {r.name for r in cc.run_canonical("http://unused", fetch=lambda u: cc.HttpResponse(0, ""))}


def known_gates() -> set[str]:
    """Wired release-train gate ids, parsed from the report rows in release-train.yml (authoritative)."""
    text = (REPO_ROOT / ".github" / "workflows" / "release-train.yml").read_text(encoding="utf-8")
    # The report job lists each wired gate as a `gate-id|$SIGNAL_VAR` row (the status-signal env var).
    return set(re.findall(r"^\s*([a-z][a-z-]*)\|\$[A-Z]", text, flags=re.MULTILINE))


def _resolve(evidence: dict, known_checks: set[str], known_gates_: set[str]) -> tuple[bool, str]:
    if not isinstance(evidence, dict):
        return False, "no evidence object"
    kind, ref = evidence.get("kind"), str(evidence.get("ref", ""))
    if not ref:
        return False, f"evidence kind={kind!r} has no ref"
    if kind == "test":
        return ((REPO_ROOT / ref).is_file(), f"test file {ref!r}")
    if kind == "canonical-check":
        return (ref in known_checks, f"canonical-check {ref!r} (known: {sorted(known_checks)})")
    if kind == "gate":
        return (ref in known_gates_, f"gate {ref!r} (known: {sorted(known_gates_)})")
    return False, f"unknown evidence kind {kind!r}"


def check(capabilities: list[dict], known_checks: set[str], known_gates_: set[str]) -> tuple[list[dict], str]:
    rows = []
    for cap in capabilities:
        cid = cap.get("id", "<no-id>")
        status = cap.get("status")
        if status not in VALID_STATUS:
            rows.append({"id": cid, "status": "fail", "why": f"unknown capability status {status!r}"})
            continue
        if status == "roadmap":
            rows.append({"id": cid, "status": "pass", "why": "labelled roadmap (not advertised as shipped)"})
            continue
        # shipped -> must be backed.
        ok, detail = _resolve(cap.get("evidence") or {}, known_checks, known_gates_)
        if ok:
            rows.append({"id": cid, "status": "pass", "why": f"shipped, backed by {detail}"})
        else:
            rows.append({"id": cid, "status": "fail",
                         "why": f"ADVERTISED but no actual evidence — {detail} does not resolve"})
    overall = "fail" if any(r["status"] == "fail" for r in rows) else "pass"
    return rows, overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capabilities", default=str(CAPABILITIES_PATH))
    args = ap.parse_args(argv)

    data = yaml.safe_load(Path(args.capabilities).read_text(encoding="utf-8")) or {}
    caps = data.get("capabilities") or []
    rows, overall = check(caps, known_canonical_checks(), known_gates())

    print(f"== advertised-vs-actual docs gate — {overall.upper()} ({len(rows)} capabilities) ==")
    for r in rows:
        print(f"  [{r['status'].upper():4}] {r['id']}: {r['why']}")
    return 1 if overall == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
