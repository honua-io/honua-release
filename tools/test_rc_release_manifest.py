"""RC assets must identify the admitted image, not the working manifest's label."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rc_release_manifest as release

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
LOCK = "sha256:" + "c" * 64
IMAGE = "ghcr.io/honua-io/honua-server:nightly-aaaaaaa"


def inputs():
    return release.ReleaseInputs("2026.1-rc.1", IMAGE, LOCK)


def manifest():
    return {"platformRelease": "2026.1-rc.2", "status": "draft",
            "candidate": {"ref": SHA, "refSource": "trunk"},
            "components": {"honua-server": {"sha": SHA, "image": "old-tag", "digest": DIGEST},
                           "sdk": {"version": "1.2.3"}}, "dbSchema": {"min": 135}}


def report():
    return {"admissible": True, "candidate": {
        "server_sha": SHA, "server_digest": DIGEST, "platform_lock": LOCK}}


def test_snapshot_names_the_rc_and_preserves_candidate_metadata():
    original = manifest()
    before = copy.deepcopy(original)
    result = release.snapshot(original, report(), inputs())
    assert original == before
    assert result["platformRelease"] == "2026.1-rc.1"
    assert result["status"] == "rc"
    assert result["components"]["honua-server"] == {"sha": SHA, "image": IMAGE, "digest": DIGEST}
    assert result["components"]["sdk"] == original["components"]["sdk"]
    assert result["dbSchema"] == original["dbSchema"]
    assert result["releaseAdmission"] == report()["candidate"]
    # The existing server N-1 reader consumes this indented YAML block.
    text = yaml.safe_dump(result, sort_keys=False)
    assert f"  honua-server:\n    sha: {SHA}\n    image: {IMAGE}\n    digest: {DIGEST}\n" in text


@pytest.mark.parametrize("field,value", [
    ("server_sha", "d" * 40), ("server_sha", "short"),
    ("server_digest", "sha256:" + "d" * 64), ("server_digest", "latest"),
    ("platform_lock", "sha256:" + "d" * 64), ("platform_lock", None),
])
def test_refuses_mismatched_or_malformed_gate_candidate(field, value):
    evidence = report()
    evidence["candidate"][field] = value
    with pytest.raises(ValueError):
        release.snapshot(manifest(), evidence, inputs())


@pytest.mark.parametrize("admitted", [False, None, "true", 1])
def test_refuses_missing_or_nonboolean_admission(admitted):
    evidence = report()
    evidence["admissible"] = admitted
    with pytest.raises(ValueError, match="admitted"):
        release.snapshot(manifest(), evidence, inputs())


def test_refuses_a_different_dispatch_candidate():
    source = manifest()
    source["candidate"]["ref"] = "d" * 40
    with pytest.raises(ValueError, match="candidate"):
        release.snapshot(source, report(), inputs())


@pytest.mark.parametrize("label", ["2026.1", "2026.1-rc.0", "../bad", "2026.1-rc.1\n"])
def test_refuses_non_rc_release_labels(label):
    with pytest.raises(ValueError, match="label"):
        release.snapshot(manifest(), report(), release.ReleaseInputs(label, IMAGE, LOCK))


def test_cli_refuses_a_red_window_without_creating_a_manifest(tmp_path):
    source, gate, out = (tmp_path / name for name in ("manifest.yaml", "window.json", "out.yaml"))
    source.write_text(yaml.safe_dump(manifest()))
    gate.write_text(json.dumps({**report(), "admissible": False}))
    result = subprocess.run([sys.executable, str(Path(release.__file__)), "--manifest", str(source),
                             "--gate-window", str(gate), "--label", inputs().label,
                             "--image", IMAGE, "--platform-lock", LOCK, "--out", str(out)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "admitted" in result.stderr
    assert not out.exists()


def test_workflow_publishes_the_snapshot_and_renders_its_notes():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/rc-release.yml"
    workflow = yaml.safe_load(path.read_text())
    steps = workflow["jobs"]["gate-window"]["steps"]
    snapshot_step = next(step for step in steps if "tools/rc_release_manifest.py" in step.get("run", ""))
    assert "if" not in snapshot_step  # Successful gate evaluation is required.
    assert "--gate-window gate-window.json" in snapshot_step["run"]
    assert "--platform-lock" in snapshot_step["run"]
    assert "--out finalized-manifest.yaml" in snapshot_step["run"]
    notes = next(step["run"] for step in steps if step.get("name") == "Render the rc notes")
    assert "--manifest finalized-manifest.yaml" in notes
    assert "--server-image" not in notes and "--server-digest" not in notes
    upload = next(step["with"]["path"] for step in steps if "upload-artifact@" in step.get("uses", ""))
    assert "finalized-manifest.yaml" in upload.splitlines()
    publish = workflow["jobs"]["publish"]["steps"][-1]["run"]
    assert "out/finalized-manifest.yaml" in publish
