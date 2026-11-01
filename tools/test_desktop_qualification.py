import copy
import hashlib
import json
from pathlib import Path

import pytest

import desktop_qualification as q

DIGEST = "sha256:" + "1" * 64


@pytest.fixture
def inputs():
    grids = {
        "qgis-scripting": {"candidate": {"digest": DIGEST}, "rows": [
            {"row": "features", "cells": {"query_filter": {"pyqgis": {"value": "certified"}}}},
            {"row": "sensorthings", "cells": {"query_filter": {"pyqgis": {"value": "blocked", "note": "Preview"}}}}]},
    }
    scope = {"schema": "honua.desktop-scripting-scope/v1", "revision": "test.1", "client_limitations": [], "sources": {}}
    for source, grid in grids.items():
        cells = q.normalize(grid, source)
        applicable = {key: cell for key, cell in cells.items() if cell["value"] != "not-applicable"}
        deferred = [{"row": "sensorthings", "column": "query_filter", "target_release": "2026.2"}]
        scope["sources"][source] = {"total_count": len(cells), "total_identity_sha256": q.identity_digest(cells),
            "applicable_count": len(applicable), "applicable_identity_sha256": q.identity_digest(applicable),
            "required_count": len(applicable)-len(deferred), "deferred_preview_cells": deferred}
    return scope, grids


def test_preview_deferral_preserves_raw_blocked_outcome_and_never_certifies(inputs):
    scope, grids = inputs
    original = copy.deepcopy(grids)
    result = q.report(scope, grids, DIGEST)
    assert result["required_count"] == 1
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
    assert result["outstanding_count"] == 1


@pytest.mark.parametrize("digest", ["sha256:+" + "1" * 63, "sha256:-" + "1" * 63,
    "sha256: " + "1" * 63, "sha256:" + "g" * 64, "sha256:" + "A" * 64,
    "sha256:" + "1" * 63, "sha256:" + "1" * 65, DIGEST + "\n", None])
def test_invalid_candidate_digest_is_rejected(inputs, digest):
    scope, grids = inputs
    with pytest.raises(ValueError, match="Exact candidate digest"):
        q.report(scope, grids, digest)


@pytest.mark.parametrize("mutation", ["delete", "add", "duplicate", "make-na", "unknown-outcome"])
def test_denominator_changes_fail_closed(inputs, mutation):
    scope, grids = inputs
    grid = grids["qgis-scripting"]
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
        del grids["qgis-scripting"]
    with pytest.raises(ValueError):
        q.report(scope, grids, DIGEST)


def test_committed_scope_reconciles_the_approved_wave():
    scope = q.read_json(q.SCOPE)
    assert scope["client_limitations"] == []
    assert sum(c["applicable_count"] for c in scope["sources"].values()) == 487
    assert sum(c["required_count"] for c in scope["sources"].values()) == 473
    assert sum(len(c["deferred_preview_cells"]) for c in scope["sources"].values()) == 14
    assert all(len(c["baseline_commit"]) == 40 and len(c["baseline_sha256"]) == 64 for c in scope["sources"].values())


def test_cli_hashes_captured_bytes_when_input_is_replaced(inputs, tmp_path, monkeypatch, capsys):
    scope, grids = inputs
    paths = {name: tmp_path / (name + ".json") for name in ("scope", *q.DRIVERS)}
    values = {"scope": scope, **grids}
    original = {}
    for name, path in paths.items():
        original[name] = json.dumps(values[name]).encode()
        path.write_bytes(original[name])
    calls = []
    real_read = Path.read_bytes

    def replace_after_read(path):
        raw = real_read(path)
        if path in paths.values():
            calls.append(path)
            path.write_bytes(b'{"concurrently_replaced": true}')
        return raw

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["desktop_qualification.py", "--scope", str(paths["scope"]),
        "--qgis-grid", str(paths["qgis-scripting"]), "--candidate-digest", DIGEST,
        "--out", str(output)])
    q.main()
    result = json.loads(real_read(output))
    assert calls == list(paths.values())
    assert result["required_count"] == 1 and result["outstanding_count"] == 0
    assert result["input_file_sha256"] == {
        name: hashlib.sha256(raw).hexdigest() for name, raw in original.items()}
    assert all(real_read(path) != original[name] for name, path in paths.items())
    capsys.readouterr()


@pytest.mark.parametrize("payload", [b'{"duplicate":1,"duplicate":2}', b'{"value":NaN}',
    b'{"value":Infinity}', b'{"value":-Infinity}', b'[]', b'\xff'])
def test_captured_inputs_retain_strict_json_validation(tmp_path, payload):
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        q.read_input(path)
