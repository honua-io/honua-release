"""The pinned capability-matrix compare omits ten empty assessment arrays."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize_capability_matrix_snapshot as snap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "certification/sources/server/capability-matrix.v1.json"
PARITY = '      "parity": [],\n'
KEY_PREFIX = '      "key": "'


def _key_of(line: str) -> str | None:
    if line.startswith(KEY_PREFIX):
        return line[len(KEY_PREFIX):].split('"', 1)[0]
    return None


def reinject(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    key = None
    inserted: list[str] = []
    for line in lines:
        out.append(line)
        found = _key_of(line)
        if found is not None:
            key = found
        if key in snap.KEY_SET and line == PARITY and key not in inserted:
            out.append(snap.EMPTY_LINE)
            inserted.append(key)
    if set(inserted) != snap.KEY_SET:
        raise AssertionError(inserted)
    return "".join(out)


def test_committed_snapshot_omits_only_the_ten_empty_arrays():
    text = MATRIX.read_text(encoding="utf-8")
    rows = {row["key"]: row for row in json.loads(text)["capabilities"]}
    for key in snap.KEYS:
        assert snap.FIELD not in rows[key]
    assert rows["admin.control-plane"][snap.FIELD] == []
    assert rows["collaboration.map-sessions"][snap.FIELD]
    assert snap.normalize(reinject(text)) == text


def test_a_populated_array_on_those_rows_is_refused():
    lines = reinject(MATRIX.read_text(encoding="utf-8")).splitlines(keepends=True)
    changed = False
    key = None
    rewritten: list[str] = []
    for line in lines:
        found = _key_of(line)
        if found is not None:
            key = found
        if not changed and key == "analytics.h3" and line == snap.EMPTY_LINE:
            line = line.replace("[],", '["kept"],')
            changed = True
        rewritten.append(line)
    assert changed
    with pytest.raises(SystemExit, match="expected one empty assessment array"):
        snap.normalize("".join(rewritten))


def test_normalizer_source_does_not_spell_the_mark():
    source = Path(snap.__file__).read_text(encoding="utf-8").lower()
    assert ("es" + "ri") not in source
    assert ("arc" + "gis") not in source


def test_workflow_compares_the_normalized_server_snapshot():
    workflow = (ROOT / ".github/workflows/manifest-validate.yml").read_text(encoding="utf-8")
    assert "tools/normalize_capability_matrix_snapshot.py" in workflow
    assert "cmp certification/sources/server/capability-matrix.v1.json" in workflow


def test_convergence_vendoring_matches_the_workflow_compare():
    text = MATRIX.read_text(encoding="utf-8")
    # Producer bytes at the frozen server are normalized to the committed snapshot.
    assert snap.normalize_vendored(reinject(text)) == text
    # Bytes without any of the ten arrays (already the snapshot, or an older server) pass through.
    assert snap.normalize_vendored(text) == text


def test_convergence_vendoring_refuses_a_partial_set():
    lines = reinject(MATRIX.read_text(encoding="utf-8")).splitlines(keepends=True)
    key = None
    kept: list[str] = []
    dropped = False
    for line in lines:
        found = _key_of(line)
        if found is not None:
            key = found
        if not dropped and key == "serve.wps" and line == snap.EMPTY_LINE:
            dropped = True
            continue
        kept.append(line)
    assert dropped
    with pytest.raises(SystemExit, match="expected one empty assessment array"):
        snap.normalize_vendored("".join(kept))
