"""Offline contracts for the release-tier Python producer."""
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release-bundle.yml").read_text())
STEPS = WORKFLOW["jobs"]["evidence"]["steps"]
DISPATCH = next(step for step in STEPS if "PYTHON_HARNESS_SHA" in step.get("env", {}))
HEAD = "a" * 40
RELEASE_SHA = "b" * 40


@pytest.fixture
def fake_gh(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env bash
set -eu
case "$1 $2" in
  'api repos/honua-io/honua-sdk-python/branches/trunk')
    printf '{"protected":%s,"commit":{"sha":"%s"}}\\n' "$BRANCH_PROTECTED" "$PYTHON_HARNESS_SHA" ;;
  'api repos/honua-io/honua-sdk-python/compare/'*)
    [[ "$2" == *"$PYTHON_RELEASE_HARNESS_MINIMUM...$PYTHON_HARNESS_SHA" ]]
    echo "$BRANCH_RELATION" ;;
  'workflow run')
    printf '%s\\n' "$@" > "$DISPATCH_ARGS"
    echo 'https://github.com/honua-io/honua-sdk-python/actions/runs/123' ;;
  'run watch') exit 0 ;;
  'api repos/honua-io/honua-sdk-python/actions/runs/123')
    printf '{"event":"workflow_dispatch","head_sha":"%s","conclusion":"success","path":".github/workflows/conformance.yml","name":"Python SDK Conformance","run_attempt":2}\\n' "$ACTUAL_HEAD" ;;
  'api repos/honua-io/honua-sdk-python/actions/runs/123/artifacts?per_page=100')
    echo '{"artifacts":[{"id":3,"name":"python-sdk-conformance-123-2","expired":false,"workflow_run":{"id":123}}]}' ;;
  'run download')
    [[ "$3" == 123 && "$6 $7 $8" == "--name python-sdk-conformance-123-2 --dir" ]]
    echo '{"fixture":true}' > "$9/protocol-certification-fragment.json" ;;
  *) exit 99 ;;
esac
""")
    gh.chmod(0o755)
    return {
        **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(tmp_path / "output"), "DISPATCH_ARGS": str(tmp_path / "args"),
        "PYTHON_HARNESS_SHA": HEAD, "REQUIREMENTS_REF": RELEASE_SHA,
        "CANDIDATE_IMAGE": "image@sha256:" + "c" * 64, "SERVER_REVISION": "d" * 40,
        "CANDIDATE_CUT": "2026-11-24T12:00:00Z",
    }


@pytest.mark.parametrize("branch,accepted", [
    (("true", "identical"), True),
    (("true", "ahead"), True),
    (("true", "behind"), False),
    (("true", "diverged"), False),
    (("false", "ahead"), False),
])
def test_protected_harness_must_contain_release_wheel_binding(fake_gh, branch, accepted):
    protected, relation = branch
    step = next(step for step in STEPS if step.get("id") == "python_harness")
    env = {**fake_gh, **step["env"], "BRANCH_PROTECTED": protected, "BRANCH_RELATION": relation}
    result = subprocess.run(["bash", "-c", step["run"]], env=env, check=False)
    assert (result.returncode == 0) is accepted
    if accepted:
        assert Path(env["GITHUB_OUTPUT"]).read_text() == f"sha={HEAD}\n"


@pytest.mark.parametrize("actual_head", [HEAD, "e" * 40])
def test_release_dispatch_checks_head_and_collects_matching_artifact(fake_gh, tmp_path, actual_head):
    checkout = STEPS[0]
    assert checkout["with"]["ref"] == DISPATCH["env"]["REQUIREMENTS_REF"] == "${{ github.sha }}"
    assert DISPATCH["env"]["REQUIREMENTS_REVISION"] == "${{ steps.producer_pins.outputs.requirements_revision }}"
    assert DISPATCH["env"]["PYTHON_HARNESS_SHA"] == "${{ steps.python_harness.outputs.sha }}"
    catalog = ROOT / "certification/protocol-certification-requirements.v1.json"
    revision = json.loads(catalog.read_text())["revision"]

    step = next(step for step in STEPS if step.get("id") == "producer_pins")
    pin_script = step["run"].split("python3 - <<'PY'")[1]
    subprocess.run(["bash", "-ec", "python3 - <<'PY'" + pin_script], cwd=ROOT, env=fake_gh, check=True)
    assert f"requirements_revision={revision}" in Path(fake_gh["GITHUB_OUTPUT"]).read_text().splitlines()

    commands = DISPATCH["run"].replace("\\\n", "").splitlines()
    command = next(line for line in commands if "--id sdk-python" in line)
    out = tmp_path / "collected"
    command = command.replace("evidence/sdk-python", shlex.quote(str(out)))
    env = {**fake_gh, "REQUIREMENTS_REVISION": revision, "ACTUAL_HEAD": actual_head}
    result = subprocess.run(
        ["bash", "-ec", command], cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    args = Path(env["DISPATCH_ARGS"]).read_text().splitlines()
    assert args[:6] == shlex.split("workflow run conformance.yml -R honua-io/honua-sdk-python --ref")
    assert args[6] == "trunk"
    assert args[7:] == [
        "-f", f"server_image={env['CANDIDATE_IMAGE']}",
        "-f", f"server_seed_ref={env['SERVER_REVISION']}",
        "-f", f"candidate_cut_at={env['CANDIDATE_CUT']}",
        "-f", "certification_tier=release",
        "-f", f"requirements_ref={RELEASE_SHA}",
        "-f", f"requirements_revision={revision}",
    ]
    if actual_head == HEAD:
        assert result.returncode == 0, result.stderr
        metadata = json.loads((out / "metadata.json").read_text())
        assert (metadata["runId"], metadata["runAttempt"], metadata["artifactId"]) == ("123", "2", "3")
        assert metadata["headSha"] == HEAD
        assert json.loads(Path(metadata["receipt"]).read_text()) == {"fixture": True}
    else:
        assert result.returncode == 1
        assert f"head SHA does not match expected producer pin {HEAD}" in result.stderr
        assert not out.exists()
