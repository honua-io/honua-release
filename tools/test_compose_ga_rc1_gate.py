"""The rc.1 Compose gate pins the published image and fails closed around the trunk candidate."""
import json
from pathlib import Path

import pytest
import yaml

import compose_ga_rc1_gate as gate

ROOT = Path(__file__).resolve().parents[1]
REPO = gate.REPOSITORY
INDEX = gate.RC1_INDEX_DIGEST
CHILD = gate.RC1_AMD64_DIGEST
SHA = "b" * 40
CANDIDATE = f"127.0.0.1:55000/honua-server@sha256:{'ab' * 32}"


def release_body(digest=INDEX):
    return (
        "| honua-console | pre-release | `abc` | ghcr.io/honua-io/honua-console@sha256:" + "f" * 64 + " |\n"
        f"| honua-server | pre-release | `see image tag` | {REPO}:nightly-a4bde4b @ `{digest}` |\n"
    )


def test_release_pin_keeps_the_prefix_and_rejects_a_moved_digest():
    assert INDEX.startswith(gate.RC1_DIGEST_PREFIX)
    assert gate.check_release(release_body()) == f"{REPO}@{INDEX}"
    with pytest.raises(gate.GateError, match="moved off the pinned"):
        gate.check_release(release_body("sha256:" + "1" * 64))
    with pytest.raises(gate.GateError, match="exactly one"):
        gate.check_release("no server row")


def test_amd64_child_must_be_the_pinned_child():
    index = {"manifests": [
        {"digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": "sha256:" + "e" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
    ]}
    assert gate.amd64_child(index) == CHILD
    index["manifests"][0]["digest"] = "sha256:" + "9" * 64
    with pytest.raises(gate.GateError, match="linux/amd64 child"):
        gate.amd64_child(index)


def test_candidate_must_be_a_newer_descendant_and_not_rc1():
    assert gate.check_candidate(CANDIDATE, SHA, 142, "yes")["schema"] == 142
    with pytest.raises(gate.GateError, match="rc.1 image"):
        gate.check_candidate(f"{REPO}@{INDEX}", SHA, 142, "yes")
    with pytest.raises(gate.GateError, match="rc.1 image"):
        gate.check_candidate(f"{REPO}@{CHILD}", SHA, 142, "yes")
    with pytest.raises(gate.GateError, match="not a descendant"):
        gate.check_candidate(CANDIDATE, SHA, 142, "no")
    with pytest.raises(gate.GateError, match="newer than rc.1"):
        gate.check_candidate(CANDIDATE, SHA, 140, "yes")
    with pytest.raises(gate.GateError, match="other than the rc.1"):
        gate.check_candidate(CANDIDATE, gate.RC1_SERVER_SHA, 142, "yes")


def _receipt(upgrade_status="pass", upgrade_why="advanced", interrupt=None):
    if interrupt is None:
        interrupt = [{"scenario": name, "status": "pass", "why": "rolled back and retried"} for name in gate.INTERRUPT_MODES]
    upgrade = {"scenarios": [{"scenario": "compose-upgrade-rollback", "status": upgrade_status, "why": upgrade_why}]}
    return gate.build_receipt(
        upgrade=upgrade, interrupt={"scenarios": interrupt}, observed={"schemaVersionsRows": {"beforeUpgrade": "1"}},
        candidate={"image": CANDIDATE, "sha": SHA, "schema": 142}, runtime_prior=f"{REPO}@{CHILD}",
        run="local", commit="a" * 40)


def test_receipt_passes_only_when_every_phase_and_both_interrupts_pass():
    receipt = _receipt()
    assert receipt["status"] == "pass"
    assert receipt["phases"] == {phase: "pass" for phase in (*gate.UPGRADE_PHASES, "interrupt")}
    assert receipt["composeProject"] == "honua-ga5-compose-upgrade"
    assert receipt["hostPort"] == 15085
    assert receipt["releaseTag"] == "2026.1-rc.1"
    assert receipt["prior"]["indexDigest"].startswith(gate.RC1_DIGEST_PREFIX)


def test_a_failed_upgrade_marks_only_the_phase_that_failed():
    receipt = _receipt("fail", "seeded rows did not survive the upgrade (2 -> 1)")
    assert receipt["status"] == "fail"
    assert receipt["phases"]["install"] == "pass"
    assert receipt["phases"]["load"] == "pass"
    assert receipt["phases"]["upgrade"] == "pass"
    assert receipt["phases"]["verify"] == "fail"
    assert receipt["phases"]["rollback"] == "not-run"
    assert receipt["phases"]["interrupt"] == "pass"


def test_an_unproved_interrupt_cannot_pass():
    rows = [
        {"scenario": "partial-migration-failure:backend-terminated", "status": "blocked", "why": "no pause"},
        {"scenario": "partial-migration-failure:server-killed", "status": "pass", "why": "killed"},
    ]
    receipt = _receipt(interrupt=rows)
    assert receipt["status"] == "fail"
    assert receipt["phases"]["interrupt"] == "blocked"


def test_unknown_failure_does_not_invent_passed_phases():
    receipt = _receipt("fail", "something unexpected")
    assert receipt["status"] == "fail"
    assert {receipt["phases"][phase] for phase in gate.UPGRADE_PHASES} == {"fail"}


def _triggers(document):
    # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_workflow_is_dispatchable_and_not_part_of_the_cloud_gate():
    document = yaml.safe_load((ROOT / ".github/workflows/gate-compose-ga-rc1.yml").read_text(encoding="utf-8"))
    assert set(_triggers(document)) == {"workflow_dispatch"}
    text = (ROOT / ".github/workflows/gate-compose-ga-rc1.yml").read_text(encoding="utf-8")
    assert "e2e/harness/compose-ga-rc1-gate.sh" in text
    assert "e2e-cloud-aws" not in text
    for name in ("e2e-cloud-aws.yml", "e2e-cloud-aws-cell.yml", "nightly-certification.yml"):
        assert "gate-compose-ga-rc1" not in (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    script = (ROOT / "e2e/harness/compose-ga-rc1-gate.sh").read_text(encoding="utf-8")
    assert "honua-ga5-compose-upgrade" in script and "15085" in script
    assert "compose-upgrade-rollback" in script and "partial-migration-failure" in script
    assert "docker rm -f" in script and "honua-ga5-registry" in script
    assert "build_slot_take" in script and "not bypassing" in script
    assert "BUILD_RESERVATION_HELPER" in script
    assert gate.RC1_SERVER_SHA in script


def test_cli_writes_a_failing_receipt(tmp_path):
    upgrade = tmp_path / "upgrade.json"
    interrupt = tmp_path / "interrupt.json"
    observed = tmp_path / "observed.json"
    upgrade.write_text(json.dumps({"scenarios": []}), encoding="utf-8")
    interrupt.write_text(json.dumps({"scenarios": []}), encoding="utf-8")
    observed.write_text("{}", encoding="utf-8")
    output = tmp_path / "receipt.json"
    rc = gate.main(["receipt", "--upgrade-matrix", str(upgrade), "--interrupt-matrix", str(interrupt),
                    "--observed", str(observed), "--candidate-image", CANDIDATE, "--candidate-sha", SHA,
                    "--candidate-schema", "142", "--ancestor", "yes", "--runtime-prior", f"{REPO}@{CHILD}",
                    "--output", str(output)])
    assert rc == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "fail"
