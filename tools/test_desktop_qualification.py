import copy

import pytest

import desktop_qualification as q

DIGEST = "sha256:" + "1" * 64


@pytest.fixture
def inputs():
    grids = {
        "licensed-scripting": {"candidate": {"digest": DIGEST}, "rows": [
            {"driver": q.SCRIPTING_DRIVER, "row": "features", "cells": {
                "query_filter": {"value": "certified", "citation": "immutable-receipt"},
                "edit_features": {"value": "not-applicable"}}}]},
        "qgis-scripting": {"candidate": {"digest": DIGEST}, "rows": [
            {"row": "features", "cells": {"query_filter": {"pyqgis": {"value": "certified"}}}},
            {"row": "sensorthings", "cells": {"query_filter": {"pyqgis": {"value": "blocked", "note": "Preview"}}}}]},
    }
    scope = {"schema": "honua.desktop-scripting-scope/v1", "revision": "test.1", "client_limitations": [], "sources": {}}
    for source, grid in grids.items():
        cells = q.normalize(grid, source)
        applicable = {key: cell for key, cell in cells.items() if cell["value"] != "not-applicable"}
        deferred = [] if source == "licensed-scripting" else [
            {"row": "sensorthings", "column": "query_filter", "target_release": "2026.2"}]
        scope["sources"][source] = {"total_count": len(cells), "total_identity_sha256": q.identity_digest(cells),
            "applicable_count": len(applicable), "applicable_identity_sha256": q.identity_digest(applicable),
            "required_count": len(applicable)-len(deferred), "deferred_preview_cells": deferred}
    return scope, grids


def test_preview_deferral_preserves_raw_blocked_outcome_and_never_certifies(inputs):
    scope, grids = inputs
    original = copy.deepcopy(grids)
    result = q.report(scope, grids, DIGEST)
    assert result["required_count"] == 2
    assert result["outstanding_count"] == 0
    assert result["release_gate_evaluated"] is False
    deferred = [c for c in result["cells"] if not c["required"]]
    assert len(deferred) == 1 and deferred[0]["original"]["value"] == "blocked"
    assert grids == original


@pytest.mark.parametrize("value", ["certified-prior", "failing", "blocked", "not-probed"])
def test_noncurrent_outcomes_stay_outstanding(inputs, value):
    scope, grids = inputs
    grids["qgis-scripting"]["rows"][0]["cells"]["query_filter"]["pyqgis"]["value"] = value
    result = q.report(scope, grids, DIGEST)
    assert result["outstanding_count"] == 1
    assert result["progress"] == "outstanding"
    assert result["summaries"]["qgis-scripting"]["required_outcomes"][value] == 1


def test_different_candidate_does_not_carry_forward_passes(inputs):
    scope, grids = inputs
    result = q.report(scope, grids, "sha256:" + "2" * 64)
    assert result["outstanding_count"] == 2


@pytest.mark.parametrize("digest", ["sha256:+" + "1" * 63, "sha256:-" + "1" * 63,
    "sha256: " + "1" * 63, "sha256:" + "g" * 64, "sha256:" + "A" * 64,
    "sha256:" + "1" * 63, "sha256:" + "1" * 65, DIGEST + "\n", None])
def test_invalid_candidate_digest_is_rejected(inputs, digest):
    scope, grids = inputs
    with pytest.raises(ValueError, match="Exact candidate digest"):
        q.report(scope, grids, digest)


@pytest.mark.parametrize("mutation", ["delete", "add", "duplicate", "make-na", "unknown-outcome", "other-driver"])
def test_denominator_changes_fail_closed(inputs, mutation):
    scope, grids = inputs
    grid = grids["licensed-scripting"]
    row = grid["rows"][0]
    if mutation == "delete":
        del row["cells"]["query_filter"]
    elif mutation == "add":
        row["cells"]["new_operation"] = {"value": "certified"}
    elif mutation == "duplicate":
        grid["rows"].append(copy.deepcopy(row))
    elif mutation == "make-na":
        row["cells"]["query_filter"]["value"] = "not-applicable"
    elif mutation == "unknown-outcome":
        row["cells"]["query_filter"]["value"] = "waived"
    elif mutation == "other-driver":
        row["driver"] = "pro-ui"
    with pytest.raises(ValueError):
        q.report(scope, grids, DIGEST)


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "wrong-release", "other-surface", "client-exception", "missing-source"])
def test_scope_cannot_silently_remove_required_failures(inputs, mutation):
    scope, grids = inputs
    source = scope["sources"]["qgis-scripting"]
    entry = source["deferred_preview_cells"][0]
    if mutation == "unknown":
        entry["column"] = "unknown"
    elif mutation == "duplicate":
        source["deferred_preview_cells"].append(copy.deepcopy(entry))
    elif mutation == "wrong-release":
        entry["target_release"] = "2026.3"
    elif mutation == "other-surface":
        entry["row"] = "features"
    elif mutation == "client-exception":
        scope["client_limitations"] = [{"row": "features"}]
    elif mutation == "missing-source":
        del grids["licensed-scripting"]
    with pytest.raises(ValueError):
        q.report(scope, grids, DIGEST)


def test_committed_scope_reconciles_the_approved_wave():
    scope = q.read_json(q.SCOPE)
    assert scope["client_limitations"] == []
    assert sum(c["applicable_count"] for c in scope["sources"].values()) == 964
    assert sum(c["required_count"] for c in scope["sources"].values()) == 950
    assert sum(len(c["deferred_preview_cells"]) for c in scope["sources"].values()) == 14
    assert all(len(c["baseline_commit"]) == 40 and len(c["baseline_sha256"]) == 64 for c in scope["sources"].values())
