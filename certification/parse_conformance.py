#!/usr/bin/env python3
"""Parse the geospatial-mcp conformance checker output into a release-gate verdict.

The cross-repo conformance gate (issue #3) runs geospatial-mcp's `conformance/check_manifest.py`
against the pinned candidate and must turn its human output into a machine verdict that can FAIL —
*without* re-introducing the false-pass that issue retired (geospatial-mcp#25: the checker reported
FULL while ignoring resource-family coverage; that is now fixed upstream, and this gate refuses to
manufacture a green if the checker produces no verdict at all).

`check_manifest.py` prints one line per manifest:

    conformance/manifests/honua.manifest.json: FULL [reference implementation] (12 standard tools, …)
    some-other.manifest.json: MAPPED (…)

and exits 1 if any manifest is FAIL, 2 on a setup error (missing index / no manifests / --strict
without jsonschema), 0 otherwise. Verdict rules here:

  fail   — checker setup error (rc==2); any manifest FAIL; NO verdict line parsed at all
           (vacuous / no evidence — the anti-false-pass guard); or, when require_reference_full,
           the reference-implementation manifest is absent or not FULL (this is exactly the
           overstated-FULL class geospatial-mcp#25 was about).
  pass   — at least one verdict parsed, none FAIL, and (if required) the reference impl is FULL.

Usage:
  python certification/parse_conformance.py --rc <int> [--output FILE] [--no-require-reference-full]
  # output defaults to stdin
"""
from __future__ import annotations

import argparse
import re
import sys

# Verdict lines start at column 0 ("path: LEVEL …"); the checker's "  note:"/"  FAIL:" detail lines
# are indented, so anchoring on a non-space first char excludes them.
_VERDICT_RE = re.compile(
    r"^(?P<path>\S.*?): (?P<level>FULL|MAPPED|FAIL)\b(?P<ref>\s*\[reference implementation\])?"
)


def parse_verdicts(output: str) -> list[dict]:
    """Extract [{path, level, is_reference}] from the checker's stdout."""
    verdicts = []
    for line in output.splitlines():
        m = _VERDICT_RE.match(line)
        if not m:
            continue
        verdicts.append({
            "path": m.group("path"),
            "level": m.group("level"),
            "is_reference": bool(m.group("ref")),
        })
    return verdicts


def evaluate(output: str, rc: int, require_reference_full: bool = True) -> tuple[str, str]:
    """Return (status, why) where status is 'pass' or 'fail'."""
    if rc == 2:
        first = next((ln for ln in output.splitlines() if ln.strip()), "")
        return "fail", f"mcp conformance checker setup error (rc=2): {first or 'no output'}"

    verdicts = parse_verdicts(output)
    if not verdicts:
        # The checker ran but emitted no verdict — never let "no evidence" read as success.
        return "fail", f"no conformance verdict parsed from checker output (rc={rc}) — vacuous/no evidence"

    failed = [v["path"] for v in verdicts if v["level"] == "FAIL"]
    if failed:
        return "fail", f"{len(failed)} manifest(s) FAILED conformance: {', '.join(failed)}"

    if require_reference_full:
        refs = [v for v in verdicts if v["is_reference"]]
        if not refs:
            return "fail", "no reference-implementation manifest verdict found (expected one at FULL)"
        not_full = [v for v in refs if v["level"] != "FULL"]
        if not_full:
            levels = ", ".join(f"{v['path']}={v['level']}" for v in not_full)
            return "fail", f"reference implementation not FULL ({levels}) — overstated conformance (geospatial-mcp#25)"

    summary = ", ".join(f"{v['level']}" for v in verdicts)
    return "pass", f"{len(verdicts)} manifest(s) conformant ({summary}); no FAIL, reference=FULL"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rc", type=int, required=True, help="exit code from check_manifest.py")
    ap.add_argument("--output", default=None, help="file with the checker stdout (default: stdin)")
    ap.add_argument("--no-require-reference-full", dest="require_reference_full",
                    action="store_false", help="do not require the reference impl to be FULL")
    args = ap.parse_args(argv)

    output = open(args.output, encoding="utf-8").read() if args.output else sys.stdin.read()
    status, why = evaluate(output, args.rc, args.require_reference_full)
    # Emit the verdict on stdout for the workflow to capture into a gate fragment.
    print(f"status={status}")
    print(f"why={why}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
