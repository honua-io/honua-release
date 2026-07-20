#!/usr/bin/env python3
"""Advertised-vs-actual docs gate (gate h) — every advertised capability must be backed by something
real, or be explicitly labelled roadmap.

The audit found capabilities advertised with no backing (a fabricated example citing a non-existent
SDK). This gate makes that structurally impossible: docs/capabilities.yaml lists each claim, and a
`shipped` claim FAILS unless its `evidence` resolves to a real artefact —
  kind: canonical-check   ref: a check name actually defined in e2e/canonical_checks.py
  kind: gate              ref: a wired release-train gate id (parsed from release-train.yml)
  kind: test              ref: a test file path that exists
  kind: capability-key    ref: a key in honua-evidence's capability-matrix.v1.json that meets the GA
                           criteria (honua-release#59): `maturity.implemented > 0` (not
                           experimental/deferred-only), `provingTestCount` >= a configurable floor, and
                           100% CITE pass rate on every joined suite. The matrix itself is fetched by
                           the CALLING WORKFLOW (gate-docs.yml — raw.githubusercontent, pinned ref) and
                           handed in as a local path; this module never makes network calls itself, so
                           it stays pure and unit-testable (same pattern as check_slo.py/
                           check_upgrade.py). A missing/unfetchable/malformed matrix resolves every
                           `capability-key` claim to BLOCKED — fail-closed, never a fake pass.
A `roadmap` claim passes (honestly labelled, no test required); an unknown status fails.

tools/check_ga_surface.py is the sibling "advertised-GA ⊆ evidenced-GA" check: it applies the SAME
`resolve_capability_key` criteria to EVERY advertised-GA key in the matrix, not just the handful of
claims hand-picked here.

  check(capabilities, known_checks, known_gates, capability_matrix=None, min_proving_tests=5)
      -> (rows, overall)   # pure, unit-tested; overall in {pass, fail, blocked}
  python tools/check_capabilities.py [--capability-matrix path/to/capability-matrix.v1.json]
"""
from __future__ import annotations

import argparse
import json
import os
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

# Default GA floor for the `capability-key` evidence kind. docs/capabilities.yaml's top-level
# `defaults.minProvingTests` overrides this for the committed claims (and for check_ga_surface.py,
# which reads the same file) without touching code — "thresholds land in config, not buried in code".
DEFAULT_MIN_PROVING_TESTS = 5


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


def load_capability_matrix(path: str | Path | None) -> dict | None:
    """Load a locally-fetched copy of honua-evidence's capability-matrix.v1.json.

    Returns None (never raises) on a missing path, unreadable file, or malformed/unexpected JSON, so
    every caller fails CLOSED to `blocked` — a stale/missing matrix must never resolve as a pass.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("capabilities"), list):
        return None
    return data


def _capability_entry(matrix: dict, key: str) -> dict | None:
    for entry in matrix.get("capabilities") or []:
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry
    return None


def resolve_capability_key(ref: str, matrix: dict | None,
                           min_proving_tests: int = DEFAULT_MIN_PROVING_TESTS) -> tuple[str, str]:
    """GA criteria for one capability-matrix key.

    Returns (status, why) with status in {pass, fail, blocked}:
      blocked  ONLY for an unavailable matrix (fetch failed / not fetched / malformed). This is the
               single "cannot verify" state — never treated as a pass.
      fail     the matrix is real and parsed but the key doesn't meet the bar: not found, no
               implemented (non-experimental) surface — this is also how a `deferred`-only key would
               fail once the matrix schema adds that maturity state, since `implemented` stays 0 —
               provingTestCount under the floor, or a joined CITE suite below 100% pass rate.
      pass     implemented > 0, provingTestCount >= floor, and any joined CITE suite is 100%.
    """
    if matrix is None:
        return "blocked", "capability matrix unavailable (unfetchable/stale honua-evidence snapshot) — cannot evaluate"
    entry = _capability_entry(matrix, ref)
    if entry is None:
        return "fail", f"capability key {ref!r} not found in the capability matrix"
    maturity = entry.get("maturity") or {}
    implemented = maturity.get("implemented") or 0
    if implemented <= 0:
        return "fail", (f"capability key {ref!r} has no implemented GA surface "
                        f"(maturity={maturity!r} — experimental/deferred-only)")
    proving = entry.get("provingTestCount") or 0
    if proving < min_proving_tests:
        return "fail", f"capability key {ref!r} provingTestCount={proving} below floor {min_proving_tests}"
    cite = entry.get("cite") or []
    if cite:
        short = [c for c in cite if (c.get("passRate") or 0) < 100.0]
        if short:
            suites = ", ".join(f"{c.get('suite')} {c.get('passRate')}%" for c in short)
            return "fail", f"capability key {ref!r} has a CITE suite below 100% pass rate: {suites}"
    cite_summary = [c.get("suite") for c in cite] or "n/a"
    return "pass", (f"capability key {ref!r}: implemented={implemented}, "
                    f"provingTestCount={proving}, cite={cite_summary}")


def _resolve(evidence: dict, known_checks: set[str], known_gates_: set[str],
             capability_matrix: dict | None = None,
             min_proving_tests: int = DEFAULT_MIN_PROVING_TESTS) -> tuple[str, str]:
    """-> (status, detail) with status in {pass, fail, blocked}."""
    if not isinstance(evidence, dict):
        return "fail", "no evidence object"
    kind, ref = evidence.get("kind"), str(evidence.get("ref", ""))
    if not ref:
        return "fail", f"evidence kind={kind!r} has no ref"
    if kind == "test":
        return ("pass" if (REPO_ROOT / ref).is_file() else "fail"), f"test file {ref!r}"
    if kind == "canonical-check":
        return ("pass" if ref in known_checks else "fail"), f"canonical-check {ref!r} (known: {sorted(known_checks)})"
    if kind == "gate":
        return ("pass" if ref in known_gates_ else "fail"), f"gate {ref!r} (known: {sorted(known_gates_)})"
    if kind == "capability-key":
        return resolve_capability_key(ref, capability_matrix, min_proving_tests)
    return "fail", f"unknown evidence kind {kind!r}"


def check(capabilities: list[dict], known_checks: set[str], known_gates_: set[str],
          capability_matrix: dict | None = None,
          min_proving_tests: int = DEFAULT_MIN_PROVING_TESTS) -> tuple[list[dict], str]:
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
        res_status, detail = _resolve(cap.get("evidence") or {}, known_checks, known_gates_,
                                       capability_matrix, min_proving_tests)
        if res_status == "pass":
            rows.append({"id": cid, "status": "pass", "why": f"shipped, backed by {detail}"})
        elif res_status == "blocked":
            rows.append({"id": cid, "status": "blocked", "why": f"cannot verify — {detail}"})
        else:
            rows.append({"id": cid, "status": "fail",
                         "why": f"ADVERTISED but no actual evidence — {detail} does not resolve"})
    if any(r["status"] == "fail" for r in rows):
        overall = "fail"
    elif any(r["status"] == "blocked" for r in rows):
        overall = "blocked"
    else:
        overall = "pass"
    return rows, overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capabilities", default=str(CAPABILITIES_PATH))
    ap.add_argument("--capability-matrix", default=None,
                    help="path to a locally-fetched honua-evidence capability-matrix.v1.json "
                         "(env HONUA_CAPABILITY_MATRIX also honored); absent/unreadable -> every "
                         "capability-key claim reports BLOCKED, never a fake pass")
    ap.add_argument("--min-proving-tests", type=int, default=None,
                    help="override docs/capabilities.yaml's defaults.minProvingTests")
    ap.add_argument("--require-real", action="store_true", help="promote BLOCKED to FAIL (real train cuts)")
    args = ap.parse_args(argv)

    data = yaml.safe_load(Path(args.capabilities).read_text(encoding="utf-8")) or {}
    caps = data.get("capabilities") or []
    min_proving = args.min_proving_tests
    if min_proving is None:
        min_proving = (data.get("defaults") or {}).get("minProvingTests", DEFAULT_MIN_PROVING_TESTS)

    matrix_path = args.capability_matrix or os.environ.get("HONUA_CAPABILITY_MATRIX")
    matrix = load_capability_matrix(matrix_path)

    rows, overall = check(caps, known_canonical_checks(), known_gates(), matrix, min_proving)

    print(f"== advertised-vs-actual docs gate — {overall.upper()} ({len(rows)} capabilities, "
          f"matrix={'loaded' if matrix else 'unavailable'}) ==")
    for r in rows:
        print(f"  [{r['status'].upper():4}] {r['id']}: {r['why']}")
    if overall == "fail":
        return 1
    if overall == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
