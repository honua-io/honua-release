"""The suppression gate must fail closed — a gate that can't fail is worse than no gate.

Covers honua-release#71: an active repo-wide suppression, an unreadable or unparseable variable, an
undecided steady-state mechanism, and a missing/adverse/stale window audit must all refuse to pass.

Run: python -m pytest tools/test_check_contract_suppression.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_contract_suppression import (  # noqa: E402
    SuppressionPolicyError,
    evaluate,
    load_policy,
    main,
    read_variable_flag,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER = REPO_ROOT / "certification" / "contract-suppression-policy.yaml"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _policy() -> dict:
    return yaml.safe_load(REGISTER.read_text(encoding="utf-8"))


def _listing(value: str | None) -> dict:
    variables = [{"name": "SOMETHING_ELSE", "value": "1"}]
    if value is not None:
        variables.append({"name": "OPENAPI_ALLOW_BREAKING_CHANGES", "value": value})
    return {"total_count": len(variables), "variables": variables}


def _status(policy: dict, listing: dict | None, **kwargs) -> dict:
    return evaluate(policy, variable_listing=listing, now=NOW, **kwargs)


# ── the shipped register is itself valid ────────────────────────────────────────────────────────


def test_shipped_register_loads_and_declares_the_landed_mechanism():
    policy = load_policy(REGISTER)
    assert policy["suppression"]["repository"] == "honua-io/honua-server"
    assert policy["suppression"]["variable"] == "OPENAPI_ALLOW_BREAKING_CHANGES"
    assert policy["steady_state_mechanism"]["status"] == "landed"
    assert policy["window_audit"]["verdict"] == "no-genuine-break"


# ── variable resolution ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " True "])
def test_truthy_values_are_suppression(value):
    active, _ = read_variable_flag(_listing(value), "OPENAPI_ALLOW_BREAKING_CHANGES")
    assert active is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", ""])
def test_falsy_values_are_not_suppression(value):
    active, _ = read_variable_flag(_listing(value), "OPENAPI_ALLOW_BREAKING_CHANGES")
    assert active is False


def test_absent_variable_is_not_suppression():
    active, why = read_variable_flag(_listing(None), "OPENAPI_ALLOW_BREAKING_CHANGES")
    assert active is False
    assert "not set" in why


@pytest.mark.parametrize("value", ["ture", "maybe", "false-ish", "2"])
def test_unparseable_value_is_never_read_as_false(value):
    with pytest.raises(SuppressionPolicyError):
        read_variable_flag(_listing(value), "OPENAPI_ALLOW_BREAKING_CHANGES")


def test_malformed_listing_is_refused():
    with pytest.raises(SuppressionPolicyError):
        read_variable_flag({"total_count": 0}, "OPENAPI_ALLOW_BREAKING_CHANGES")


# ── verdicts ────────────────────────────────────────────────────────────────────────────────────


def test_suppression_off_passes():
    result = _status(_policy(), _listing("false"))
    assert result["status"] == "pass"
    assert result["suppression_active"] is False


def test_suppression_on_blocks_and_says_why():
    result = _status(_policy(), _listing("true"))
    assert result["status"] == "blocked"
    assert result["suppression_active"] is True
    assert "suppression is ACTIVE" in result["why"]


def test_unreadable_variables_fail_closed():
    result = _status(_policy(), None, unreadable_reason="HTTP 403")
    assert result["status"] == "blocked"
    assert result["suppression_active"] is True
    assert "HTTP 403" in result["why"]


def test_unparseable_value_fails_closed():
    result = _status(_policy(), _listing("ture"))
    assert result["status"] == "blocked"
    assert result["suppression_active"] is True


def test_missing_steady_state_mechanism_blocks_even_with_suppression_off():
    policy = _policy()
    policy.pop("steady_state_mechanism")
    result = _status(policy, _listing("false"))
    assert result["status"] == "blocked"
    assert "steady-state mechanism" in result["why"]


def test_undecided_steady_state_mechanism_blocks():
    policy = _policy()
    policy["steady_state_mechanism"]["status"] = "under-consideration"
    result = _status(policy, _listing("false"))
    assert result["status"] == "blocked"


def test_steady_state_mechanism_without_evidence_blocks():
    policy = _policy()
    policy["steady_state_mechanism"]["evidence"] = []
    result = _status(policy, _listing("false"))
    assert result["status"] == "blocked"


def test_missing_window_audit_blocks():
    policy = _policy()
    policy.pop("window_audit")
    result = _status(policy, _listing("false"))
    assert result["status"] == "blocked"
    assert "not been audited" in result["why"]


def test_adverse_window_audit_blocks():
    policy = _policy()
    policy["window_audit"]["verdict"] = "break-found"
    result = _status(policy, _listing("false"))
    assert result["status"] == "blocked"


def test_stale_audit_blocks_only_while_the_window_is_open():
    policy = _policy()
    policy["window_audit"]["covers_through"] = "2026-01-01T00:00:00Z"
    assert _status(policy, _listing("true"))["status"] == "blocked"
    # Once suppression is off the window is closed: an old audit is history, not a live gap.
    assert _status(policy, _listing("false"))["status"] == "pass"


def test_fresh_audit_does_not_block_an_open_window_on_its_own():
    policy = _policy()
    policy["window_audit"]["covers_through"] = "2026-08-18T00:00:00Z"
    result = _status(policy, _listing("true"))
    audit = next(c for c in result["checks"] if c["check"] == "window-audit")
    assert audit["status"] == "pass"
    # ...the ACTIVE suppression is still what blocks the candidate.
    assert result["status"] == "blocked"


@pytest.mark.parametrize(
    "register",
    [
        {"suppression": {}},
        {"suppression": {"repository": "honua-io/honua-server"}},
        {"suppression": {"repository": "x/y", "variable": "V", "window_opened_at": "not-a-date"}},
        {"nothing": True},
    ],
)
def test_a_broken_register_is_refused_not_trusted(register: dict, tmp_path: Path):
    path = tmp_path / "register.yaml"
    path.write_text(yaml.safe_dump(register), encoding="utf-8")
    with pytest.raises(SuppressionPolicyError):
        load_policy(path)


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────


def test_cli_exit_codes_and_evidence(tmp_path: Path):
    listing = tmp_path / "vars.json"
    listing.write_text(json.dumps(_listing("true")), encoding="utf-8")
    evidence = tmp_path / "evidence.json"
    rc = main(
        [
            "--policy",
            str(REGISTER),
            "--variable-listing",
            str(listing),
            "--evidence-out",
            str(evidence),
        ]
    )
    assert rc == 1
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["status"] == "blocked"
    assert record["gate"] == "contract-suppression"
    # The evidence must describe state, never carry a value that could leak configuration.
    assert "value" not in json.dumps(record)


def test_cli_passes_when_suppression_is_off(tmp_path: Path):
    listing = tmp_path / "vars.json"
    listing.write_text(json.dumps(_listing(None)), encoding="utf-8")
    assert main(["--policy", str(REGISTER), "--variable-listing", str(listing)]) == 0


def test_cli_fails_closed_on_an_unreadable_listing(tmp_path: Path):
    listing = tmp_path / "vars.json"
    listing.write_text("not json", encoding="utf-8")
    assert main(["--policy", str(REGISTER), "--variable-listing", str(listing)]) == 1


def test_cli_fails_closed_on_an_unreadable_register(tmp_path: Path):
    register = tmp_path / "register.yaml"
    register.write_text("suppression: []\n", encoding="utf-8")
    assert main(["--policy", str(register)]) == 1


# ── honua-release#92: losing the cross-repo read must be loud and distinguishable ────────────────
# The gate's input is a CROSS-REPO read of honua-server's Actions variables, which this repository's
# GITHUB_TOKEN cannot do — it needs RELEASE_GH_TOKEN. If that access is ever lost, the danger is that
# the red reads as "suppression is on" (or worse, that some future edit reads absence as "off"). The
# verdict must stay fail-closed AND the evidence must say which of the two it is.


def test_unreadable_variables_report_unknown_not_off_and_never_read_as_not_suppressed():
    result = _status(_policy(), None, unreadable_reason="HTTP 403")
    assert result["status"] == "blocked"
    assert result["variable_readable"] is False
    assert result["suppression_state"] == "unknown"
    assert result["suppression_active"] is True  # fail-closed reading of an unknown state
    assert "no repo-wide breaking-change suppression is in effect" not in result["why"]


def test_unreadable_variables_name_the_token_and_the_tracking_issue():
    """A token-scope red must say so, so nobody triages it as a suppression problem."""
    result = _status(_policy(), None, unreadable_reason="HTTP 404")
    assert "RELEASE_GH_TOKEN" in result["why"]
    assert "honua-io/honua-release#92" in result["why"]


def test_a_readable_listing_reports_the_real_state():
    off = _status(_policy(), _listing("false"))
    assert off["variable_readable"] is True and off["suppression_state"] == "off"
    on = _status(_policy(), _listing("true"))
    assert on["variable_readable"] is True and on["suppression_state"] == "active"


def test_an_unparseable_value_is_unknown_rather_than_active():
    result = _status(_policy(), _listing("ture"))
    assert result["suppression_state"] == "unknown"
    assert result["suppression_active"] is True


# ── the workflow wiring that feeds the decision core ─────────────────────────────────────────────
# The core above cannot be fooled, but the gate-contract job could stop feeding it honestly (e.g. by
# passing an empty listing on the failure path, or by defaulting the fragment to something other than
# `blocked`). These assertions pin the fail-closed wiring in the workflow itself.

GATE_CONTRACT = REPO_ROOT / ".github" / "workflows" / "gate-contract.yml"


def _suppression_job() -> dict:
    workflow = yaml.safe_load(GATE_CONTRACT.read_text(encoding="utf-8"))
    return workflow["jobs"]["breaking-change-suppression"]


def _step(job: dict, needle: str) -> dict:
    for step in job["steps"]:
        if needle in str(step.get("name", "")) or needle in str(step.get("id", "")):
            return step
    raise AssertionError(f"no step matching {needle!r} in the breaking-change-suppression job")


def test_the_gate_job_self_tests_the_decision_core_before_trusting_it():
    run = " ".join(str(s.get("run", "")) for s in _suppression_job()["steps"])
    assert "test_check_contract_suppression.py" in run


def test_the_variable_read_step_only_claims_readable_on_success():
    read = _step(_suppression_job(), "Read the enforcing repository")["run"]
    assert 'readable=1' in read and 'readable=0' in read
    # the failure branch must never echo the response body — variable VALUES live there
    assert "cat /tmp/vars.err" not in read
    assert "jq -r '.variables[].name'" in read, "names only are ever printed"


def test_the_evaluate_step_passes_an_unreadable_reason_when_the_read_failed():
    evaluate_step = _step(_suppression_job(), "Evaluate the suppression register")["run"]
    assert "--variable-listing variables.json" in evaluate_step
    assert "--unreadable-reason" in evaluate_step
    assert "steps.read.outputs.readable" in evaluate_step


def test_the_fragment_defaults_to_blocked_and_the_job_cannot_be_soft_failed():
    job = _suppression_job()
    assert "continue-on-error" not in job
    fragment = _step(job, "Emit fragment")
    assert fragment["with"]["gate"] == "contract-suppression"
    assert "blocked" in str(fragment["with"]["status"]), "a crashed job must not default to pass"
    assert "continue-on-error" not in fragment
