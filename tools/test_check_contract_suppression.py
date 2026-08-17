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
