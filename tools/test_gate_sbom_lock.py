"""gate-sbom's strict platform BOM on a plain dispatch binds only a verified qualification lock.

The verification is gate-studio-customer-journey.yml's (and gate-upgrade.yml's), step for step."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _steps(workflow, job):
    return yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"][job]["steps"]


def _verify(steps):
    return next(s for s in steps if "platform_lock_bundle.py" in s.get("run", "") and "rederived-lock" in s.get("run", ""))


def test_the_lock_verification_is_the_studio_and_upgrade_gates_step_for_step():
    sbom = _steps("gate-sbom.yml", "platform-sbom")
    studio = _steps("gate-studio-customer-journey.yml", "customer")
    upgrade = _steps("gate-upgrade.yml", "compose-upgrade")
    verify = _verify(sbom)
    assert verify["run"] == _verify(upgrade)["run"]
    assert verify["env"] == _verify(upgrade)["env"]
    assert verify["env"]["GH_TOKEN"] == _verify(studio)["env"]["GH_TOKEN"] == "${{ secrets.RELEASE_GH_TOKEN }}"
    lines = [line.strip() for line in verify["run"].splitlines() if line.strip()]
    position = 0
    for line in (line.strip() for line in _verify(studio)["run"].splitlines() if line.strip()):
        line = line.replace("platform_lock_digest", "lock_digest")
        assert line in lines[position:], line
        position = lines.index(line, position) + 1
    assert "|| true" not in verify["run"]
    download = next(s for s in sbom if str(s.get("uses", "")).startswith("actions/download-artifact@")
                    and s.get("with", {}).get("run-id"))
    studio_download = next(s for s in studio if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert download["uses"] == studio_download["uses"] and download["with"] == studio_download["with"]
    refuse = next(s for s in sbom if s.get("name") == "Refuse a half-bound dispatch")
    assert "lock_run_id and lock_digest are required together" in refuse["run"]
    order = [sbom.index(s) for s in (refuse, download, verify)]
    assert order == sorted(order)


def test_only_a_verified_lock_reaches_the_bundle():
    sbom = _steps("gate-sbom.yml", "platform-sbom")
    adopt = next(s for s in sbom if s.get("name") == "Adopt the verified qualification lock for BOM generation")
    assert adopt["if"] == "${{ steps.candidate_lock.outputs.verified == '1' }}"
    assert adopt["run"] == "cp candidate-lock/platform-lock.json platform-lock.json"
    gen = next(s for s in sbom if s.get("id") == "gen")
    assert gen["env"]["LOCK_VERIFIED"] == "${{ steps.candidate_lock.outputs.verified }}"
    run = gen["run"]
    assert 'if [ -n "$LOCK_RUN_ID" ] && [ "$LOCK_VERIFIED" != 1 ]; then' in run
    assert '[ "$CANDIDATE_SNAPSHOT" = "true" ] || [ -n "$LOCK_RUN_ID" ]; then' in run
    assert run.index('|| [ -n "$LOCK_RUN_ID" ]; then') < run.index("ARGS+=(--qualification)")
    assert run.index('"$LOCK_VERIFIED" != 1') < run.index("platform_lock_bundle.py")
    assert '["platformRelease"]' in run and '--label "$BUNDLE_LABEL"' in run


def test_inputs_and_permissions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gate-sbom.yml").read_text())
    for trigger in ("workflow_dispatch", "workflow_call"):
        inputs = workflow[True][trigger]["inputs"]
        assert inputs["lock_run_id"]["default"] == "" and inputs["lock_digest"]["default"] == ""
        assert inputs["lock_artifact"]["default"] == "candidate-manifest"
    permissions = workflow["jobs"]["platform-sbom"]["permissions"]
    assert permissions == {"contents": "read", "packages": "read", "actions": "read", "attestations": "read"}
