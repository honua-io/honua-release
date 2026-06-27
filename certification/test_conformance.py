"""Tests for the cross-repo conformance verdict parser.

The conformance gate's whole purpose (issue #3) is to NOT false-pass — the very defect it retired
(geospatial-mcp#25 reported FULL while ignoring coverage). So the parser that turns the checker's
output into a release verdict must itself be proven to fail on each bad case. A gate that only ever
goes green is the anti-pattern AGENTS.md forbids.

Run: python -m pytest certification/test_conformance.py    (or: python certification/test_conformance.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_conformance as pc  # noqa: E402

REF_FULL = "conformance/manifests/honua.manifest.json: FULL [reference implementation] (12 standard tools, 9 resources, 0 known gaps; schema+coverage)"
REF_MAPPED = "conformance/manifests/honua.manifest.json: MAPPED [reference implementation] (12 standard tools, 9 resources, 3 known gaps; schema+coverage)"
OTHER_MAPPED = "conformance/manifests/partial.manifest.json: MAPPED (8 standard tools, 5 resources, 4 known gaps; coverage (stdlib only))"
OTHER_FAIL = "conformance/manifests/broken.manifest.json: FAIL (0 standard tools, 0 resources, 0 known gaps; schema+coverage)"


def test_parse_extracts_level_and_reference_flag():
    v = pc.parse_verdicts(REF_FULL + "\n" + OTHER_MAPPED)
    assert len(v) == 2
    assert v[0]["level"] == "FULL" and v[0]["is_reference"] is True
    assert v[1]["level"] == "MAPPED" and v[1]["is_reference"] is False


def test_parse_ignores_indented_detail_lines():
    out = REF_FULL + "\n  note: standard tool 'x' is a known gap\n  FAIL: something descriptive\n"
    v = pc.parse_verdicts(out)
    assert len(v) == 1 and v[0]["level"] == "FULL"  # the indented "  FAIL:" line is NOT a verdict


def test_pass_when_reference_full_and_no_fail():
    status, why = pc.evaluate(REF_FULL + "\n" + OTHER_MAPPED, rc=0)
    assert status == "pass", why


def test_fail_on_any_fail_line():
    status, why = pc.evaluate(REF_FULL + "\n" + OTHER_FAIL, rc=1)
    assert status == "fail" and "FAILED conformance" in why and "broken" in why


def test_fail_when_reference_not_full():
    # The exact regression geospatial-mcp#25 was about: a non-FULL reference must not pass.
    status, why = pc.evaluate(REF_MAPPED, rc=0)
    assert status == "fail" and "not FULL" in why


def test_fail_on_empty_output_is_not_a_silent_pass():
    status, why = pc.evaluate("", rc=0)
    assert status == "fail" and "vacuous" in why


def test_fail_on_setup_error_rc2():
    status, why = pc.evaluate("FAIL: index not found at .../index.json", rc=2)
    assert status == "fail" and "setup error" in why


def test_fail_when_no_reference_manifest_present():
    status, why = pc.evaluate(OTHER_MAPPED, rc=0)
    assert status == "fail" and "no reference-implementation" in why


def test_nonreference_mapped_allowed_when_reference_is_full():
    status, why = pc.evaluate(REF_FULL + "\n" + OTHER_MAPPED, rc=0)
    assert status == "pass", why


def test_require_reference_full_can_be_relaxed():
    # With the flag off, a reference MAPPED (no FAIL) passes — used if a candidate is intentionally
    # certified at MAPPED. Off by default so the strong assertion is the norm.
    status, why = pc.evaluate(REF_MAPPED, rc=0, require_reference_full=False)
    assert status == "pass", why


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
