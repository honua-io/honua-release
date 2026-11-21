"""Unit tests for staging declared package subdirectories into the reader workspace."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stage_paths  # noqa: E402


def _resolver(trees: dict[str, dict], files: dict[str, bytes]) -> SimpleNamespace:
    def fetch(url: str, accept=None, token=None) -> bytes:
        if "/git/trees/" in url:
            key = url.split("/git/trees/", 1)[1].split("?", 1)[0]
            return json.dumps(trees[key]).encode("utf-8")
        return files[url]

    return SimpleNamespace(fetch=fetch, token="tok")


def test_stage_tree_fetches_blobs_preserves_exec_mode_and_encodes_urls(tmp_path):
    rev = "a" * 40
    doc = {"repo": "honua-io/honua-server", "stagePaths": ["docker/proj-grids", "skills"]}
    trees = {
        f"{rev}%3Adocker%2Fproj-grids": {
            "truncated": False,
            "tree": [
                {"path": "Dockerfile", "type": "blob", "mode": "100644"},
                {"path": "fetch-grids.sh", "type": "blob", "mode": "100755"},
                {"path": "grid #1.txt", "type": "blob", "mode": "100644"},
            ],
        },
        f"{rev}%3Askills": {
            "truncated": False,
            "tree": [
                {"path": "honua-ops", "type": "tree", "mode": "040000"},
                {"path": "honua-ops/SKILL.md", "type": "blob", "mode": "100644"},
            ],
        },
    }
    raw_base = f"https://raw.githubusercontent.com/honua-io/honua-server/{rev}"
    files = {
        f"{raw_base}/docker/proj-grids/Dockerfile": b"FROM scratch\n",
        f"{raw_base}/docker/proj-grids/fetch-grids.sh": b"#!/bin/sh\n",
        f"{raw_base}/docker/proj-grids/grid%20%231.txt": b"grid\n",
        f"{raw_base}/skills/honua-ops/SKILL.md": b"# ops\n",
    }
    staged = stage_paths.stage_tree(doc, rev, _resolver(trees, files))
    assert [(item.path, item.mode) for item in staged] == [
        ("docker/proj-grids/Dockerfile", "0644"),
        ("docker/proj-grids/fetch-grids.sh", "0755"),
        ("docker/proj-grids/grid #1.txt", "0644"),
        ("skills/honua-ops/SKILL.md", "0644"),
    ]
    written: list[tuple[str, Path, bytes]] = []
    chmodded: list[tuple[str, Path, str]] = []
    session = SimpleNamespace(
        cwd=str(tmp_path),
        put_bytes=lambda rt, p, data: written.append((rt, p, data)),
        chmod=lambda rt, p, mode: chmodded.append((rt, p, mode)),
    )
    stage_paths.write_staged_files(session, "python", staged)
    assert [(rt, p.relative_to(tmp_path).as_posix(), data) for rt, p, data in written] == [
        ("python", "docker/proj-grids/Dockerfile", b"FROM scratch\n"),
        ("python", "docker/proj-grids/fetch-grids.sh", b"#!/bin/sh\n"),
        ("python", "docker/proj-grids/grid #1.txt", b"grid\n"),
        ("python", "skills/honua-ops/SKILL.md", b"# ops\n"),
    ]
    assert [(rt, p.relative_to(tmp_path).as_posix(), mode) for rt, p, mode in chmodded] == [
        ("python", "docker/proj-grids/fetch-grids.sh", "0755"),
    ]


def test_stage_tree_rejects_truncated_empty_symlinks_gitlinks_and_non_list():
    rev = "b" * 40
    doc = {"repo": "honua-io/honua-server", "stagePaths": ["docker/proj-grids"]}
    truncated = _resolver({f"{rev}%3Adocker%2Fproj-grids": {"truncated": True, "tree": []}}, {})
    with pytest.raises(stage_paths.StageError, match="truncated"):
        stage_paths.stage_tree(doc, rev, truncated)

    empty = _resolver({f"{rev}%3Adocker%2Fproj-grids": {"truncated": False, "tree": []}}, {})
    with pytest.raises(stage_paths.StageError, match="matched no files"):
        stage_paths.stage_tree(doc, rev, empty)

    symlink = _resolver({
        f"{rev}%3Adocker%2Fproj-grids": {
            "truncated": False,
            "tree": [{"path": "link", "type": "blob", "mode": "120000"}],
        }
    }, {})
    with pytest.raises(stage_paths.StageError, match="unsupported staged entry"):
        stage_paths.stage_tree(doc, rev, symlink)

    gitlink = _resolver({
        f"{rev}%3Adocker%2Fproj-grids": {
            "truncated": False,
            "tree": [{"path": "sub", "type": "commit", "mode": "160000"}],
        }
    }, {})
    with pytest.raises(stage_paths.StageError, match="unsupported staged entry"):
        stage_paths.stage_tree(doc, rev, gitlink)

    with pytest.raises(stage_paths.StageError, match="must be a list"):
        stage_paths.stage_tree({"repo": "honua-io/honua-server", "stagePaths": "skills"}, rev, empty)


@pytest.mark.parametrize("bad", ["", "../secrets", "docker/../etc", "/etc/passwd", "./foo", "a//b", "a\x00b"])
def test_safe_stage_path_rejects_traversal(bad):
    with pytest.raises(stage_paths.StageError, match="unsafe stage path"):
        stage_paths.safe_stage_path(bad)


def test_quickstart_declares_stage_paths_and_first_dataset_substitutes_sdk_pin():
    sources = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
    quickstart = next(d for d in sources["documents"] if d["path"] == "docs/get-started/quickstart.md")
    assert quickstart["stagePaths"] == ["docker/proj-grids", "skills"]

    first_dataset_vars = json.loads(
        (HERE / "vars/honua-server-docs-get-started-first-dataset.json").read_text(encoding="utf-8")
    )
    assert first_dataset_vars["substitute"]["@honua/sdk-js@0.2.10"]["value"] == "@honua/sdk-js@{client.sdkJs}"
