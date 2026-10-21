from pathlib import Path

import yaml


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/python-source-input-witness.yml"


def test_cross_repository_credential_only_reaches_fixed_fetch_on_protected_dispatch():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML's YAML1.1 interpretation of `on` is accepted only for reading here.
    assert workflow.get("on", workflow.get(True)) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    job = workflow["jobs"]["verify"]
    assert job["if"] == "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/trunk' && github.repository == 'honua-io/honua-release'"
    steps = job["steps"]
    credentialed = [row for row in steps if "RELEASE_GH_TOKEN" in str(row)]
    assert len(credentialed) == 1
    fetch = credentialed[0]
    assert fetch["env"] == {"GH_TOKEN": "${{ secrets.RELEASE_GH_TOKEN }}"}
    assert fetch["run"] == ('python certification/executable-docs/fetch_python_source_inputs.py '
                            '--output "$RUNNER_TEMP/python-source-fetch"')
    checkout = steps[0]
    assert checkout["with"] == {"ref": "${{ github.sha }}", "persist-credentials": False}
    verify = next(row for row in steps if row.get("name", "").startswith("Verify original outer"))
    assert "env" not in verify
    assert "verify_python_source_witness.py" in verify["run"]
    assert steps.index(fetch) < steps.index(verify)
    assert not any("generate" in row.get("run", "") or "--action install" in row.get("run", "") for row in steps)


def test_verification_upload_excludes_original_proposal_wheels_and_credentials():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    upload = workflow["jobs"]["verify"]["steps"][-1]
    assert upload["if"] == "always()"
    paths = upload["with"]["path"].splitlines()
    assert paths == ["${{ runner.temp }}/python-source-fetch/*.json",
                     "${{ runner.temp }}/python-source-verification/*.json",
                     "${{ runner.temp }}/python-source-verification/*.stdout",
                     "${{ runner.temp }}/python-source-verification/*.stderr"]
    assert upload["with"]["if-no-files-found"] == "error"
