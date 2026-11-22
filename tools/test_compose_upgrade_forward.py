"""Forward-only GA admission rejects missing, borrowed or unstable restart evidence."""
import json
from pathlib import Path
import subprocess

import pytest

from compose_upgrade_forward import RESTART_CHECKS, forward_evidence
from compose_upgrade_prior import main
from test_compose_upgrade_version import evidence, forward_observed, receipt


@pytest.mark.parametrize("check", RESTART_CHECKS)
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_each_restart_assertion_must_be_literal_true(check, value):
    pair, phases = evidence()
    observed = forward_observed()
    observed["candidateRestart"][check] = value
    assert forward_evidence(pair, observed, phases)["status"] == "fail"


@pytest.mark.parametrize("field", ["migrationPolicy", "customerSteps", "candidateRestart"])
@pytest.mark.parametrize("value", [None, [], {}, "pass"])
def test_missing_or_malformed_forward_proof_fails_closed(field, value):
    pair, phases = evidence()
    observed = forward_observed()
    observed[field] = value
    assert forward_evidence(pair, observed, phases)["status"] == "fail"


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_a_backup_claim_must_be_a_boolean(value):
    pair, phases = evidence()
    observed = forward_observed()
    observed["customerSteps"]["preUpgradeBackup"] = value
    assert forward_evidence(pair, observed, phases)["status"] == "fail"


def test_candidate_identity_cannot_substitute_for_restart_identity():
    pair, phases = evidence()
    del phases["restart"]
    assert forward_evidence(pair, forward_observed(), phases)["status"] == "fail"


@pytest.mark.parametrize("field,value", [("imageID", "wrong"), ("platform", "linux/arm64"),
                                         ("imageVersion", "2026.1.0-rc.2"), ("runtimeIdentity", {})])
def test_restart_must_run_the_same_candidate_with_its_ga_version(field, value):
    pair, phases = evidence()
    phases["restart"][field] = value
    assert forward_evidence(pair, forward_observed(), phases)["status"] == "fail"


def test_valid_restart_passes_and_missing_restart_demotes_an_otherwise_valid_receipt():
    pair, phases = evidence()
    assert receipt(pair, phases)["forwardEvidence"]["status"] == "pass"
    del phases["restart"]
    result = receipt(pair, phases)
    assert result["status"] == "fail"
    assert result["why"].startswith("forward upgrade refused:")
    assert result["versionEvidence"]["status"] == "pass"


def test_non_ga_policy_and_existing_failure_are_preserved():
    pair, phases = evidence()
    assert receipt(pair, phases, status="blocked")["status"] == "blocked"
    assert receipt(pair, phases, status="fail")["status"] == "fail"
    pair.pop("candidate_version")
    assert forward_evidence(pair, {}, {})["status"] == "not-applicable"


@pytest.mark.parametrize("fault,reason", [
    ("none", ""), ("start", "restart failed"), ("ready", "did not become ready"),
    ("identity", "identity changed"), ("capture", "could not capture"),
    ("restart-journal-change", "changed migration journal"),
    ("restart-row-change", "changed seeded rows"), ("services", "preserved resources"),
    ("maps", "preserved resources"), ("credentials", "preserved resources"),
])
def test_actual_shell_restart_control_flow_rejects_state_and_resource_faults(tmp_path, fault, reason):
    harness = (Path(__file__).resolve().parents[1] / "e2e/harness/upgrade-chaos.sh").read_text()
    function = harness.split("cu_verify_restart() {", 1)[1].split("\n}\n", 1)[0]
    (tmp_path / "before-journal").write_text("existing migration\n")
    (tmp_path / "before-rows").write_text("existing checksum\n")
    # Stub external Docker/API boundaries; execute the real orchestration, cmp and fault injection.
    script = r'''
set -u
OUT=$1
FAULT=$2
COMPOSE_FAULT=$FAULT
CU_RESTART_READY=false CU_RESTART_IDENTITY=false CU_RESTART_JOURNAL=false
CU_RESTART_ROWS=false CU_RESTART_PRESERVATION=false
stop_server() { echo stop >> "$OUT/calls"; }
cu_start_server() { echo "start:$1" >> "$OUT/calls"; [ "$FAULT" != start ]; }
cu_wait_ready() { [ "$FAULT" != ready ]; }
cu_identity() { echo "$1:$2" >> "$OUT/calls"; [ "$FAULT" != identity ]; }
cu_fail() { echo "$1" > "$OUT/failure"; return 1; }
db_exec() { echo changed > "$OUT/row-fault"; }
capture_state() {
  [ "$FAULT" != capture ] || return 1
  cp "$OUT/before-journal" "$1"
  cp "$OUT/before-rows" "$2"
  [ ! -f "$OUT/row-fault" ] || echo changed > "$2"
  return 0
}
cu_services() { echo "services:$1" >> "$OUT/calls"; [ "$FAULT" != services ]; }
cu_maps() { echo "maps:$1" >> "$OUT/calls"; [ "$FAULT" != maps ]; }
cu_credentials() { echo "credentials:$1" >> "$OUT/calls"; [ "$FAULT" != credentials ]; }
'''
    script += "\ncu_verify_restart() {" + function + "\n}\n"
    script += 'cu_verify_restart candidate-digest "$OUT/before-journal" "$OUT/before-rows"\n'
    result = subprocess.run(["bash", "-c", script, "restart-test", str(tmp_path), fault], capture_output=True, text=True)
    if reason:
        assert result.returncode != 0, result.stderr
        assert reason in (tmp_path / "failure").read_text()
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "calls").read_text().splitlines() == [
            "stop", "start:candidate-digest", "restart:candidate-digest", "services:verify", "maps:verify", "credentials:verify"]


@pytest.mark.parametrize("restart_present", [True, False])
def test_receipt_command_loads_restart_identity_and_does_not_borrow_rollback(tmp_path, restart_present):
    pair, phases = evidence()
    valid = receipt(pair, phases)
    for name, value in (("pair", pair), ("observed", valid["observed"]),
                        ("compose-candidate-identity", phases["candidate"]),
                        ("compose-rollback-identity", phases["restart"])):
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    if restart_present:
        (tmp_path / "compose-restart-identity.json").write_text(json.dumps(phases["restart"]))
    output = tmp_path / "receipt.json"
    assert main(["receipt", "--pair", str(tmp_path / "pair.json"), "--scenario", "normal",
                 "--status", "pass", "--observed", str(tmp_path / "observed.json"),
                 "--phase-dir", str(tmp_path), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["forwardEvidence"]["status"] == ("pass" if restart_present else "fail")
    assert result["status"] == ("pass" if restart_present else "fail")
    assert "rollback" not in result["phases"]
