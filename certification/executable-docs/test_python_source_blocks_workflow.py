"""The credentialed venue cannot accept an arbitrary PR/ref or leak its token."""
from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/python-source-blocks.yml"


def test_only_protected_dispatch_can_reach_fixed_fetch_and_source_execution():
    workflow = yaml.safe_load(WORKFLOW.read_bytes())
    assert workflow.get("on", workflow.get(True)) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["execute"]
    assert job["if"] == "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/trunk' && github.repository == 'honua-io/honua-release'"
    steps = job["steps"]
    assert steps[0]["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
    credentialed = [row for row in steps if "RELEASE_GH_TOKEN" in str(row)]
    assert len(credentialed) == 1
    assert credentialed[0]["env"] == {"GH_TOKEN": "${{ secrets.RELEASE_GH_TOKEN }}"}
    assert "fetch_python_source_inputs.py" in credentialed[0]["run"]
    native = next(row for row in steps if "exercise_python_source_runtime.py" in row.get("run", ""))
    execute = next(row for row in steps if "python_source_session.py" in row.get("run", ""))
    assert steps.index(native) < steps.index(credentialed[0]) < steps.index(execute)
    assert "env" not in native and "env" not in execute
    assert not any("--action generate" in row.get("run", "") for row in steps)


def test_source_block_artifact_preserves_receipts_without_reuploading_original_inputs_or_venvs():
    workflow = yaml.safe_load(WORKFLOW.read_bytes())
    upload = workflow["jobs"]["execute"]["steps"][-1]
    assert upload["if"] == "always()"
    paths = upload["with"]["path"].splitlines()
    assert not any("originals" in path or "environment" in path or "original.zip" in path or "/node-prerequisite/prefix" in path for path in paths)
    for suffix in ("/evidence/*", "/wire/*", "/reader-*/bridge/original-block.sh", "/clone-staging/*/command-*", "/node-prerequisite/*.json", "/reader-*/bridge/node-prerequisite.json"):
        assert any(path.endswith(suffix) for path in paths)
    assert upload["with"]["if-no-files-found"] == "error"
