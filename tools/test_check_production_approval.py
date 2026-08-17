"""The production approval gate must refuse every weakened configuration (honua-release#44).

Covers the AC test matrix: missing environment, absent reviewer, wrong reviewer, unprotected branch
policy, self-approval, an automation-only reviewer, a stale/superseded attestation, an unreviewed
protection-rule type, a non-default promotion ref, a self-dispatched promotion — and the one
successful independent-approval configuration.

Run: python -m pytest tools/test_check_production_approval.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_production_approval import (  # noqa: E402
    ApprovalPolicyError,
    evaluate,
    load_policy,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "certification" / "production-approval.yaml"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
REVIEWER_ID = 12301237
REVIEWER_LOGIN = "mikemcdougall"


def _policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))


def _environment(**overrides) -> dict:
    environment = {
        "name": "production",
        "updated_at": "2026-08-15T00:00:00Z",
        "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False},
        "protection_rules": [
            {
                "id": 1,
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {"type": "User", "reviewer": {"id": REVIEWER_ID, "login": REVIEWER_LOGIN}},
                ],
            },
            {"id": 2, "type": "branch_policy"},
        ],
    }
    environment.update(overrides)
    return environment


def _repository() -> dict:
    return {"full_name": "honua-io/honua-release", "default_branch": "trunk"}


def _branch() -> dict:
    return {"name": "trunk", "protected": True}


def _evaluate(environment: dict | None, policy: dict | None = None, **kwargs) -> dict:
    params = {
        "repository": _repository(),
        "branch": _branch(),
        "promotion_ref": "trunk",
        "promotion_actor": "github-actions[bot]",
        "now": NOW,
    }
    params.update(kwargs)
    return evaluate(policy or _policy(), environment=environment, **params)


def _failed(result: dict) -> list[str]:
    return [check["check"] for check in result["checks"] if check["status"] != "pass"]


# ── the shipped policy is itself valid ──────────────────────────────────────────────────────────


def test_shipped_policy_declares_a_human_reviewer_and_an_attestation():
    policy = load_policy(POLICY_PATH)
    assert policy["environment"]["name"] == "production"
    assert policy["approval"]["required_reviewer"]["kind"] == "human"
    assert policy["approval"]["require_prevent_self_review"] is True
    assert policy["attestation"]["max_attestation_age_days"] > 0


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"environment": {}, "approval": {}, "attestation": {}},
        {
            "environment": {"name": "production"},
            "approval": {"required_reviewer": {"id": 1, "kind": "bot"}},
            "attestation": {"attested_at": "2026-01-01T00:00:00Z", "max_attestation_age_days": 30},
        },
        {
            "environment": {"name": "production"},
            "approval": {"required_reviewer": {"id": 1, "kind": "human"}},
            "attestation": {"attested_at": "nope", "max_attestation_age_days": 30},
        },
    ],
)
def test_an_untrustworthy_policy_is_refused(policy: dict, tmp_path: Path):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    with pytest.raises(ApprovalPolicyError):
        load_policy(path)


# ── the AC test matrix ──────────────────────────────────────────────────────────────────────────


def test_successful_independent_approval_passes():
    result = _evaluate(_environment())
    assert result["status"] == "pass", result["why"]
    assert _failed(result) == []


def test_missing_environment_fails_closed():
    result = _evaluate(None, unreadable_reason="HTTP 404")
    assert result["status"] == "fail"
    assert "environment-readable" in _failed(result)
    assert "HTTP 404" in result["why"]


def test_absent_reviewer_rule_fails():
    result = _evaluate(_environment(protection_rules=[{"id": 2, "type": "branch_policy"}]))
    assert result["status"] == "fail"
    assert "environment-policy" in _failed(result)
    assert "human-reviewer" in _failed(result)


def test_wrong_reviewer_fails():
    environment = _environment()
    environment["protection_rules"][0]["reviewers"] = [
        {"type": "User", "reviewer": {"id": 999, "login": "someone-else"}}
    ]
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "environment-policy" in _failed(result)
    assert "reviewer-matches-policy" in _failed(result)


def test_unprotected_branch_deployment_policy_fails():
    environment = _environment(
        deployment_branch_policy={"protected_branches": False, "custom_branch_policies": True}
    )
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "environment-policy" in _failed(result)


def test_absent_deployment_branch_policy_fails():
    environment = _environment()
    environment.pop("deployment_branch_policy")
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "environment-policy" in _failed(result)


def test_self_approval_allowed_fails():
    environment = _environment()
    environment["protection_rules"][0]["prevent_self_review"] = False
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "prevent-self-review" in _failed(result)


def test_a_promotion_started_by_the_reviewer_is_refused():
    result = _evaluate(_environment(), promotion_actor=REVIEWER_LOGIN)
    assert result["status"] == "fail"
    assert "independent-promoting-actor" in _failed(result)


def test_a_promotion_started_by_the_reviewer_id_is_refused():
    result = _evaluate(_environment(), promotion_actor="renamed-account", promotion_actor_id=REVIEWER_ID)
    assert result["status"] == "fail"
    assert "independent-promoting-actor" in _failed(result)


@pytest.mark.parametrize("login", ["github-actions[bot]", "dependabot[bot]", "some-app[bot]", ""])
def test_an_automation_principal_cannot_be_the_approval_authority(login: str):
    environment = _environment()
    environment["protection_rules"][0]["reviewers"] = [
        {"type": "User", "reviewer": {"id": REVIEWER_ID, "login": login}}
    ]
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "human-reviewer" in _failed(result)


def test_an_unreviewed_protection_rule_type_is_refused():
    environment = _environment()
    environment["protection_rules"].append({"id": 3, "type": "deployment_protection_rule"})
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "protection-rule-types" in _failed(result)


def test_promotion_from_a_non_default_branch_is_refused():
    result = _evaluate(_environment(), promotion_ref="feature/ship-it")
    assert result["status"] == "fail"
    assert "promotion-ref" in _failed(result)


def test_an_unprotected_default_branch_is_refused():
    result = _evaluate(_environment(), branch={"name": "trunk", "protected": False})
    assert result["status"] == "fail"
    assert "protected-default-branch" in _failed(result)


def test_missing_repository_metadata_is_refused():
    result = _evaluate(_environment(), repository=None, branch=None)
    assert result["status"] == "fail"
    assert "protected-default-branch" in _failed(result)


def test_an_expired_attestation_is_refused():
    policy = _policy()
    policy["attestation"]["attested_at"] = "2025-01-01T00:00:00Z"
    result = _evaluate(_environment(), policy=policy)
    assert result["status"] == "fail"
    assert "attestation-fresh" in _failed(result)


def test_settings_changed_after_the_attestation_are_refused():
    result = _evaluate(_environment(updated_at="2026-08-19T00:00:00Z"))
    assert result["status"] == "fail"
    assert "attestation-current" in _failed(result)


def test_environment_without_an_update_timestamp_is_refused():
    environment = _environment()
    environment.pop("updated_at")
    result = _evaluate(environment)
    assert result["status"] == "fail"
    assert "attestation-current" in _failed(result)


# ── evidence ────────────────────────────────────────────────────────────────────────────────────


def test_evidence_is_machine_readable_and_carries_no_secret(tmp_path: Path):
    environment = tmp_path / "environment.json"
    environment.write_text(json.dumps(_environment()), encoding="utf-8")
    repository = tmp_path / "repository.json"
    repository.write_text(json.dumps(_repository()), encoding="utf-8")
    branch = tmp_path / "branch.json"
    branch.write_text(json.dumps(_branch()), encoding="utf-8")
    evidence = tmp_path / "evidence.json"

    rc = main(
        [
            "--policy",
            str(POLICY_PATH),
            "--environment-metadata",
            str(environment),
            "--repository-metadata",
            str(repository),
            "--branch-metadata",
            str(branch),
            "--promotion-ref",
            "trunk",
            "--promotion-actor",
            "github-actions[bot]",
            "--evidence-out",
            str(evidence),
        ]
    )

    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["gate"] == "production-approval"
    assert record["status"] in {"pass", "fail"}
    assert {check["check"] for check in record["checks"]} >= {
        "environment-readable",
        "environment-policy",
        "prevent-self-review",
        "human-reviewer",
        "protected-default-branch",
        "attestation-fresh",
    }
    serialised = json.dumps(record).lower()
    for forbidden in ("token", "secret", "password", "ghp_", "github_pat"):
        assert forbidden not in serialised
    # rc mirrors the verdict so a caller can never mistake a refusal for a pass.
    assert rc == (0 if record["status"] == "pass" else 1)


def test_cli_fails_closed_when_the_environment_cannot_be_read():
    assert main(["--policy", str(POLICY_PATH), "--unreadable-reason", "HTTP 403"]) == 1


def test_cli_fails_closed_on_an_untrustworthy_policy(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("environment: []\n", encoding="utf-8")
    assert main(["--policy", str(policy)]) == 1


# ── a policy that omits a control is a WEAKENED policy, not a smaller one ───────────────────────


def _write(policy: dict, tmp_path: Path) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "delete",
    [
        ("approval", "require_prevent_self_review"),
        ("approval", "require_independent_promoting_actor"),
        ("approval", "automation_principals"),
        ("environment", "allowed_protection_rule_types"),
        ("environment", "deployment_branch_policy"),
        ("environment", "repository"),
        ("attestation", "applied"),
        ("attestation", "attested_by"),
    ],
)
def test_deleting_any_declared_control_is_refused(delete: tuple[str, str], tmp_path: Path):
    """Every control must be present and explicitly enabled — omission cannot disable it silently."""
    policy = _policy()
    policy[delete[0]].pop(delete[1])
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


@pytest.mark.parametrize("block", ["environment", "approval", "promotion", "attestation"])
def test_deleting_any_policy_block_is_refused(block: str, tmp_path: Path):
    policy = _policy()
    policy.pop(block)
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


@pytest.mark.parametrize(
    "key", ["require_prevent_self_review", "require_independent_promoting_actor"]
)
@pytest.mark.parametrize("value", [False, "false", "no", 0, None])
def test_a_control_set_to_anything_but_true_is_refused(key: str, value, tmp_path: Path):
    policy = _policy()
    policy["approval"][key] = value
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


@pytest.mark.parametrize(
    "key", ["require_protected_default_branch", "require_promotion_from_default_branch"]
)
def test_a_disabled_promotion_control_is_refused(key: str, tmp_path: Path):
    policy = _policy()
    policy["promotion"][key] = False
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


def test_allow_listing_an_auto_approving_protection_rule_type_is_refused(tmp_path: Path):
    policy = _policy()
    policy["environment"]["allowed_protection_rule_types"].append("deployment_protection_rule")
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


def test_an_allow_list_without_required_reviewers_is_refused(tmp_path: Path):
    policy = _policy()
    policy["environment"]["allowed_protection_rule_types"] = ["wait_timer"]
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


def test_a_weakened_declared_branch_policy_is_refused(tmp_path: Path):
    policy = _policy()
    policy["environment"]["deployment_branch_policy"] = {
        "protected_branches": False,
        "custom_branch_policies": True,
    }
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


@pytest.mark.parametrize("days", [0, -1, 100000, 366, "180", True])
def test_an_unbounded_attestation_window_is_refused(days, tmp_path: Path):
    policy = _policy()
    policy["attestation"]["max_attestation_age_days"] = days
    with pytest.raises(ApprovalPolicyError):
        load_policy(_write(policy, tmp_path))


def test_the_shipped_policy_still_loads_after_all_of_that():
    assert load_policy(POLICY_PATH)["environment"]["name"] == "production"


# ── mode: the promotion identity cannot be skipped by omitting the arguments ────────────────────


def test_promote_mode_requires_the_promotion_ref_and_actor():
    result = evaluate(
        _policy(),
        environment=_environment(),
        repository=_repository(),
        branch=_branch(),
        mode="promote",
        now=NOW,
    )
    assert result["status"] == "fail"
    assert "promotion-ref" in _failed(result)
    assert "independent-promoting-actor" in _failed(result)


def test_drift_mode_omits_the_promotion_checks_rather_than_faking_them():
    result = evaluate(
        _policy(),
        environment=_environment(),
        repository=_repository(),
        branch=_branch(),
        mode="drift",
        now=NOW,
    )
    names = {check["check"] for check in result["checks"]}
    assert "promotion-ref" not in names
    assert "independent-promoting-actor" not in names
    assert result["status"] == "pass"
    assert result["mode"] == "drift"


def test_an_unknown_mode_is_refused():
    with pytest.raises(ApprovalPolicyError):
        evaluate(_policy(), environment=_environment(), mode="whatever")


def test_cli_promote_mode_without_an_actor_refuses(tmp_path: Path):
    environment = tmp_path / "environment.json"
    environment.write_text(json.dumps(_environment()), encoding="utf-8")
    assert (
        main(
            [
                "--policy",
                str(POLICY_PATH),
                "--mode",
                "promote",
                "--environment-metadata",
                str(environment),
            ]
        )
        == 1
    )


def test_the_default_mode_is_the_enforcing_one(tmp_path: Path):
    """An omitted --mode must fail closed, not silently run the reduced drift check set."""
    from check_production_approval import DEFAULT_MODE

    assert DEFAULT_MODE == "promote"
    environment = tmp_path / "environment.json"
    environment.write_text(json.dumps(_environment()), encoding="utf-8")
    repository = tmp_path / "repository.json"
    repository.write_text(json.dumps(_repository()), encoding="utf-8")
    branch = tmp_path / "branch.json"
    branch.write_text(json.dumps(_branch()), encoding="utf-8")
    evidence = tmp_path / "evidence.json"

    # A fully healthy GitHub side, but no promotion identity and no --mode: still refused.
    rc = main(
        [
            "--policy",
            str(POLICY_PATH),
            "--environment-metadata",
            str(environment),
            "--repository-metadata",
            str(repository),
            "--branch-metadata",
            str(branch),
            "--evidence-out",
            str(evidence),
        ]
    )
    assert rc == 1
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["mode"] == "promote"
    failed = {check["check"] for check in record["checks"] if check["status"] != "pass"}
    assert failed == {"promotion-ref", "independent-promoting-actor"}


def test_evaluate_defaults_to_the_enforcing_mode():
    result = evaluate(_policy(), environment=_environment(), repository=_repository(), branch=_branch(), now=NOW)
    assert result["mode"] == "promote"
    assert result["status"] == "fail"
