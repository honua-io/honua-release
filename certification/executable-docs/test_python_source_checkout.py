"""Real filesystem transposition controls, not clone execution or source admission."""
import hashlib
import os
from pathlib import Path

import pytest

import python_source_checkout as checkout


def fixture(tmp_path):
    stage, target = tmp_path / "stage", tmp_path / "target"
    files = {"AGENTS.md": (b"immutable instructions\n", "100644"),
             "nested/with\nnewline.py": (b"print('actual bytes')\n", "100755"),
             "CLAUDE.md": (b"AGENTS.md", "120000")}
    expected = {}
    for root in (stage, target):
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".git/config").write_bytes(b"separate metadata\n")
        for name, (data, mode) in files.items():
            path = root / name
            path.parent.mkdir(exist_ok=True)
            if mode == "120000":
                path.symlink_to(os.fsdecode(data))
            else:
                path.write_bytes(data)
                path.chmod(0o755 if mode == "100755" else 0o644)
            expected[name] = {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode,
                "gitBlob": hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()}
    return stage, target, expected


def test_exact_bytes_modes_symlink_and_newline_paths_transpose_without_touching_git(tmp_path):
    stage, target, expected = fixture(tmp_path)
    before = checkout._files(target, expected)
    metadata = (target / ".git/config").read_bytes()
    checkout._copy(stage, target, expected)
    assert checkout._files(target, expected) == before
    assert (target / ".git/config").read_bytes() == metadata
    assert (target / "CLAUDE.md").is_symlink()
    assert os.readlink(target / "CLAUDE.md") == "AGENTS.md"
    assert (target / "nested/with\nnewline.py").stat().st_mode & 0o111


@pytest.mark.parametrize("defect", ["extra-file", "extra-empty-directory", "missing", "bytes", "mode", "symlink"])
def test_full_census_refuses_ignored_stale_missing_and_malformed_inputs(tmp_path, defect):
    _, target, expected = fixture(tmp_path)
    if defect == "extra-file": (target / ".ignored-stale").write_bytes(b"extra")
    if defect == "extra-empty-directory": (target / ".ignored-empty").mkdir()
    if defect == "missing": (target / "AGENTS.md").unlink()
    if defect == "bytes": (target / "AGENTS.md").write_bytes(b"wrong")
    if defect == "mode": (target / "AGENTS.md").chmod(0o755)
    if defect == "symlink":
        (target / "CLAUDE.md").unlink()
        (target / "CLAUDE.md").symlink_to("/foreign/AGENTS.md")
    with pytest.raises(ValueError):
        checkout._files(target, expected)


def test_transposition_refuses_document_writable_symlink_ancestor_without_outside_write(tmp_path):
    stage, target, expected = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "nested/with\nnewline.py").unlink()
    (target / "nested").rmdir()
    (target / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        checkout._copy(stage, target, expected)
    assert list(outside.iterdir()) == []
    assert (target / ".git/config").read_bytes() == b"separate metadata\n"


@pytest.mark.parametrize("name", ["../escape", "/absolute", ".git/config", "nested/../escape"])
def test_transposition_rejects_metadata_and_escaping_selected_paths(tmp_path, name):
    stage, target, expected = fixture(tmp_path)
    expected = {name: expected["AGENTS.md"]}
    with pytest.raises(ValueError, match="escapes"):
        checkout._copy(stage, target, expected)
    assert (target / ".git/config").read_bytes() == b"separate metadata\n"
