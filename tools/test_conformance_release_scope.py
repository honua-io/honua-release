"""Tests for tools/conformance_release_scope.py (certification gate release-scope decisions)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conformance_release_scope as scope  # noqa: E402

ENTRY = {
    "requirement": "advisory",
    "releases": ["2026.1"],
    "decidedOn": "2026-10-22",
    "reason": "operator ruling 2026-10-22",
    "reportAs": "not certified for 2026.1",
}


def test_decision_applies_to_rc_labels_of_the_release():
    got = scope.scope_for({"releaseScope": {"lane-a": ENTRY}}, "2026.1-rc.2")
    assert got == {"lane-a": {k: ENTRY[k] for k in ("requirement", "decidedOn", "reason", "reportAs")}}


def test_decision_does_not_apply_to_other_releases():
    assert scope.scope_for({"releaseScope": {"lane-a": ENTRY}}, "2026.2-rc.1") == {}


def test_no_decisions_means_every_lane_required():
    assert scope.scope_for({}, "2026.1") == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("requirement", "skip"),
        ("releases", []),
        ("decidedOn", "soon"),
        ("reason", ""),
        ("reportAs", None),
    ],
)
def test_malformed_decision_is_refused(field, value):
    entry = dict(ENTRY, **{field: value})
    with pytest.raises(scope.ScopeError):
        scope.scope_for({"releaseScope": {"lane-a": entry}}, "2026.1")


def test_committed_policy_is_valid_and_scopes_only_the_geoservices_lane():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "certification" / "conformance-evidence.yaml").read_text(encoding="utf-8"))
    got = scope.scope_for(config, "2026.1-rc.2")
    assert len(got) == 1
    (lane,) = got
    assert lane.startswith("conformance-") and lane.endswith("-geoservices")
    assert got[lane]["decidedOn"] == "2026-10-22"
    # The OGC/CITE/STAC and MCP lanes stay required.
    assert "conformance-ogc-stac" not in got and "conformance-mcp" not in got
