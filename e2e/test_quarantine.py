"""Unit tests for the demo-canary quarantine core (honua-release#84, e2e/quarantine.py).

These prove the four properties the quarantine contract rests on, without touching a live target:
  1. only FAILs are rewritten, and only for probes an entry actually names;
  2. an expired quarantine reddens the run again (it cannot become permanent by neglect);
  3. the registry reports its own rot — stale and unknown entries;
  4. the committed e2e/canary-quarantine.yaml is well-formed and every entry names a real issue.

Property 5 — "quarantine never downgrades EVIDENCE" — is asserted at the demo_canary level, since that
is where the envelope is built.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import demo_canary  # noqa: E402
from canonical_checks import CheckResult  # noqa: E402
from quarantine import (  # noqa: E402
    QUARANTINE_PATH,
    QUARANTINED,
    QuarantineEntry,
    apply_quarantine,
    load_quarantine,
)

TODAY = date(2026, 8, 16)


def _entry(probe="p-fail", review_by="2026-12-31") -> QuarantineEntry:
    return QuarantineEntry(probe=probe, issue="https://github.com/honua-io/honua-release/issues/87",
                           reason="known CDN gap", since="2026-08-16", review_by=review_by)


def _results() -> list[CheckResult]:
    return [CheckResult("p-fail", "fail", "boom"),
            CheckResult("p-pass", "pass", "ok"),
            CheckResult("p-blocked", "blocked", "no key")]


def test_quarantine_downgrades_only_the_named_failure():
    out, audit = apply_quarantine(_results(), {"p-fail": _entry()}, today=TODAY)
    by_name = {r.name: r for r in out}
    assert by_name["p-fail"].status == QUARANTINED
    assert "issues/87" in by_name["p-fail"].why and "boom" in by_name["p-fail"].why
    # Untouched verdicts stay exactly as observed.
    assert by_name["p-pass"].status == "pass"
    assert by_name["p-blocked"].status == "blocked"
    assert [e["probe"] for e in audit["applied"]] == ["p-fail"]
    assert audit["expired"] == [] and audit["stale"] == [] and audit["unknown"] == []


def test_quarantine_never_upgrades_a_blocked_or_passing_probe():
    """An entry aimed at a non-failing probe must not rewrite it — and must be reported as stale."""
    out, audit = apply_quarantine(_results(), {"p-blocked": _entry(probe="p-blocked")}, today=TODAY)
    assert {r.name: r.status for r in out}["p-blocked"] == "blocked"
    assert audit["applied"] == []
    assert [e["probe"] for e in audit["stale"]] == ["p-blocked"]


def test_expired_quarantine_fails_the_run_again():
    out, audit = apply_quarantine(_results(), {"p-fail": _entry(review_by="2026-08-15")}, today=TODAY)
    failed = {r.name: r for r in out}["p-fail"]
    assert failed.status == "fail"
    assert "QUARANTINE EXPIRED" in failed.why and "2026-08-15" in failed.why
    assert [e["probe"] for e in audit["expired"]] == ["p-fail"]
    assert audit["applied"] == []


def test_quarantine_applies_on_the_review_date_itself():
    out, _ = apply_quarantine(_results(), {"p-fail": _entry(review_by="2026-08-16")}, today=TODAY)
    assert {r.name: r.status for r in out}["p-fail"] == QUARANTINED


def test_stale_and_unknown_entries_are_reported():
    entries = {"p-pass": _entry(probe="p-pass"), "p-gone": _entry(probe="p-gone")}
    _, audit = apply_quarantine(_results(), entries, today=TODAY)
    assert [e["probe"] for e in audit["stale"]] == ["p-pass"]
    assert audit["stale"][0]["observedStatus"] == "pass"
    assert [e["probe"] for e in audit["unknown"]] == ["p-gone"]


def test_missing_registry_means_nothing_quarantined(tmp_path):
    assert load_quarantine(tmp_path / "nope.yaml") == {}


@pytest.mark.parametrize("body, fragment", [
    ("quarantine:\n  p:\n    issue: https://x/1\n    reason: r\n    since: '2026-08-16'\n", "reviewBy"),
    ("quarantine:\n  p:\n    issue: '#87'\n    reason: r\n    since: '2026-08-16'\n"
     "    reviewBy: '2026-09-30'\n", "full URL"),
    ("quarantine:\n  - p\n", "must be a mapping"),
])
def test_malformed_entries_raise_rather_than_silently_disabling_the_guard(tmp_path, body, fragment):
    path = tmp_path / "q.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError) as err:
        load_quarantine(path)
    assert fragment in str(err.value)


def test_committed_registry_is_wellformed():
    entries = load_quarantine(QUARANTINE_PATH)
    for name, entry in entries.items():
        assert entry.issue.startswith("https://github.com/honua-io/"), name
        assert entry.reason.strip(), name
        assert date.fromisoformat(entry.review_by) > date.fromisoformat(entry.since), name


def test_every_quarantined_probe_is_a_probe_the_canary_can_emit():
    """Guards against a rename quietly orphaning an entry (it would then hide nothing, and the real
    regression would surface as a brand-new red with no owner)."""
    entries = load_quarantine(QUARANTINE_PATH)
    known = set(demo_canary.PROBE_CAPABILITY_KEYS) | {
        "health", "geoservices-error", "service-catalog", "capabilities", "geoprocessing",
        "security-headers", "deploy-preflight",
    }
    assert set(entries) <= known, f"unknown probe name(s) in canary-quarantine.yaml: {set(entries) - known}"


def test_quarantined_probe_is_still_red_in_the_evidence_envelope(monkeypatch):
    """The load-bearing honesty property: quarantine moves the CI verdict, never the evidence."""
    monkeypatch.setattr(demo_canary, "make_fetch", lambda *a, **k: (lambda url: None))
    monkeypatch.setattr(demo_canary, "run_canonical", lambda *a, **k: [])
    monkeypatch.setattr(demo_canary.canary_probes, "run_canary",
                        lambda *a, **k: [CheckResult("stac-collections", "fail", "0 collections")])
    monkeypatch.setattr(demo_canary, "load_quarantine",
                        lambda *a, **k: {"stac-collections": _entry(probe="stac-collections")})

    report, envelope = demo_canary.run("https://demo.honua.io", None, None, None, True, 3000.0, None)

    assert report["status"] == "blocked"          # the run does not go red...
    assert [e["probe"] for e in report["quarantine"]["applied"]] == ["stac-collections"]
    probe = next(p for p in envelope["probes"] if p["probeName"] == "stac-collections")
    assert probe["status"] == "red"               # ...but honua-evidence is still told the truth
    assert probe["lastGreenAt"] == ""
    assert "issues/87" in probe["detail"]
    assert envelope["overallStatus"] == "partial"
