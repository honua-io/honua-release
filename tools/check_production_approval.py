#!/usr/bin/env python3
"""Enforceable production approval gate (honua-release#44).

promote.yml turns a certified candidate into a signed, tagged, published platform release. Artifact
integrity proves *what* is released; this decision core proves *who authorised it*, and does so in a
way the release automation cannot satisfy on its own:

  * the intended settings live in git (certification/production-approval.yaml) so they are reviewable
    and diffable, and the live environment is compared against them;
  * a designated HUMAN reviewer must be required by the GitHub environment, and neither a bot
    principal nor the actor that started the promotion may satisfy that requirement;
  * production deployment is restricted to the repository's protected default branch;
  * every failure mode — missing environment, unreadable metadata, weakened rule, expired or
    superseded attestation, automation-only reviewer — is a REFUSAL, never a pass.

The core is pure: it takes metadata the caller already fetched and returns a verdict plus a
machine-readable evidence record. It performs no network I/O, reads no token, and never emits a
secret or an approval token — only names, ids, and policy outcomes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate_binding import validate_environment_metadata  # noqa: E402

PASS = "pass"
FAIL = "fail"


class ApprovalPolicyError(ValueError):
    """Raised when the approval policy itself cannot be trusted."""


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalPolicyError(f"{field} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalPolicyError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_policy(path: Path) -> dict:
    """Load and structurally validate the approval policy."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ApprovalPolicyError(f"approval policy is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ApprovalPolicyError("approval policy must be a mapping")

    environment = raw.get("environment")
    approval = raw.get("approval")
    attestation = raw.get("attestation")
    if not isinstance(environment, dict) or not isinstance(environment.get("name"), str):
        raise ApprovalPolicyError("environment.name is required")
    if not isinstance(approval, dict):
        raise ApprovalPolicyError("approval block is required")
    reviewer = approval.get("required_reviewer")
    if not isinstance(reviewer, dict) or not isinstance(reviewer.get("id"), int):
        raise ApprovalPolicyError("approval.required_reviewer.id is required")
    if reviewer.get("kind") != "human":
        raise ApprovalPolicyError("approval.required_reviewer.kind must be 'human'")
    if not isinstance(attestation, dict):
        raise ApprovalPolicyError("attestation block is required")
    _timestamp(attestation.get("attested_at"), "attestation.attested_at")
    if not isinstance(attestation.get("max_attestation_age_days"), int):
        raise ApprovalPolicyError("attestation.max_attestation_age_days must be an integer")
    return raw


def _reviewer_rule(environment: dict) -> dict | None:
    rules = environment.get("protection_rules")
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers":
            return rule
    return None


def _is_automation_principal(login: object, automation_principals: list) -> bool:
    if not isinstance(login, str):
        return True  # an unnamed reviewer cannot be shown to be human
    lowered = login.strip().lower()
    if not lowered or lowered.endswith("[bot]"):
        return True
    return lowered in {str(name).strip().lower() for name in automation_principals}


def evaluate(
    policy: dict,
    *,
    environment: dict | None,
    unreadable_reason: str | None = None,
    repository: dict | None = None,
    branch: dict | None = None,
    promotion_ref: str | None = None,
    promotion_actor: str | None = None,
    promotion_actor_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Return the approval-gate verdict as a machine-readable evidence record."""
    now = now or datetime.now(timezone.utc)
    env_policy = policy["environment"]
    approval = policy["approval"]
    promotion_policy = policy.get("promotion") or {}
    attestation = policy["attestation"]
    reviewer_policy = approval["required_reviewer"]
    expected_name = env_policy["name"]
    checks: list[dict] = []

    def record(name: str, ok: bool, why: str) -> None:
        checks.append({"check": name, "status": PASS if ok else FAIL, "why": why})

    if not isinstance(environment, dict):
        record(
            "environment-readable",
            False,
            f"environment {expected_name!r} metadata is missing or unreadable: "
            f"{unreadable_reason or 'no metadata supplied'}",
        )
        environment = None
    else:
        record("environment-readable", True, f"environment {expected_name!r} metadata was read")

    if environment is not None:
        # The #43 preflight, unchanged and unweakened: name, protected-branch-only deployment policy,
        # exactly one required-reviewer rule, exactly one User reviewer, exact expected id.
        ok, why = validate_environment_metadata(
            environment,
            expected_name=expected_name,
            expected_reviewer_id=reviewer_policy["id"],
        )
        record("environment-policy", ok, why)

        allowed_types = env_policy.get("allowed_protection_rule_types")
        if isinstance(allowed_types, list) and allowed_types:
            rules = environment.get("protection_rules")
            observed = [
                rule.get("type") for rule in rules if isinstance(rule, dict)
            ] if isinstance(rules, list) else []
            unexpected = sorted({str(kind) for kind in observed if kind not in allowed_types})
            record(
                "protection-rule-types",
                not unexpected,
                f"unreviewed protection rule type(s) present: {unexpected} — a rule that is not in "
                f"the policy could approve automatically"
                if unexpected
                else f"only reviewed protection rule types are configured: {sorted(set(observed))}",
            )

        rule = _reviewer_rule(environment)
        if approval.get("require_prevent_self_review"):
            record(
                "prevent-self-review",
                bool(rule) and rule.get("prevent_self_review") is True,
                "the required-reviewer rule must have prevent_self_review enabled so the actor that "
                "starts a promotion cannot approve it"
                if not (rule and rule.get("prevent_self_review") is True)
                else "GitHub refuses an approval from the actor that started the deployment",
            )

        reviewers = (rule or {}).get("reviewers")
        reviewer_logins = [
            entry.get("reviewer", {}).get("login")
            for entry in (reviewers if isinstance(reviewers, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("reviewer"), dict)
        ]
        automation_principals = approval.get("automation_principals") or []
        human_reviewers = [
            login
            for login in reviewer_logins
            if not _is_automation_principal(login, automation_principals)
        ]
        record(
            "human-reviewer",
            bool(human_reviewers),
            "the environment names no human reviewer — an automation principal (or an unnamed "
            f"reviewer) cannot be the approval authority: {reviewer_logins}"
            if not human_reviewers
            else f"required reviewer(s) are human accounts: {human_reviewers}",
        )

        expected_login = reviewer_policy.get("login")
        if isinstance(expected_login, str) and expected_login:
            record(
                "reviewer-matches-policy",
                reviewer_logins == [expected_login],
                f"live reviewer(s) {reviewer_logins} do not match the attested reviewer "
                f"{[expected_login]} — settings drifted from certification/production-approval.yaml"
                if reviewer_logins != [expected_login]
                else f"live reviewer matches the attested reviewer {expected_login!r}",
            )

    # ── branch restriction ────────────────────────────────────────────────────────────────────
    if promotion_policy.get("require_protected_default_branch"):
        default_branch = repository.get("default_branch") if isinstance(repository, dict) else None
        protected = branch.get("protected") if isinstance(branch, dict) else None
        named = branch.get("name") if isinstance(branch, dict) else None
        ok = bool(default_branch) and named == default_branch and protected is True
        record(
            "protected-default-branch",
            ok,
            f"default branch {default_branch!r} is protected"
            if ok
            else "could not prove the repository default branch is protected "
            f"(default={default_branch!r}, branch={named!r}, protected={protected!r})",
        )
        if promotion_policy.get("require_promotion_from_default_branch") and promotion_ref is not None:
            record(
                "promotion-ref",
                promotion_ref == default_branch,
                f"promotion ran from {promotion_ref!r}, not the protected default branch "
                f"{default_branch!r}"
                if promotion_ref != default_branch
                else f"promotion ran from the protected default branch {promotion_ref!r}",
            )

    # ── two-actor property ────────────────────────────────────────────────────────────────────
    if approval.get("require_independent_promoting_actor") and (
        promotion_actor is not None or promotion_actor_id is not None
    ):
        same_login = (
            isinstance(promotion_actor, str)
            and isinstance(reviewer_policy.get("login"), str)
            and promotion_actor.strip().lower() == reviewer_policy["login"].strip().lower()
        )
        same_id = promotion_actor_id is not None and promotion_actor_id == reviewer_policy["id"]
        record(
            "independent-promoting-actor",
            not (same_login or same_id),
            f"promotion was started by the required reviewer ({promotion_actor}) — approval would "
            "not be independent of the actor requesting the release"
            if (same_login or same_id)
            else f"promotion actor {promotion_actor!r} is not the required reviewer",
        )

    # ── attestation freshness + drift ─────────────────────────────────────────────────────────
    attested_at = _timestamp(attestation["attested_at"], "attestation.attested_at")
    max_age = attestation["max_attestation_age_days"]
    expires = attested_at + timedelta(days=max_age)
    record(
        "attestation-fresh",
        now <= expires,
        f"the production approval attestation expired on {expires.date().isoformat()} — re-review "
        "the live settings and refresh attestation.attested_at"
        if now > expires
        else f"attestation from {attested_at.date().isoformat()} is valid until {expires.date().isoformat()}",
    )
    if environment is not None:
        updated_at = environment.get("updated_at")
        if isinstance(updated_at, str) and updated_at.strip():
            try:
                changed = _timestamp(updated_at, "environment.updated_at")
            except ApprovalPolicyError:
                changed = None
            if changed is None:
                record("attestation-current", False, f"environment updated_at is unparseable: {updated_at!r}")
            else:
                record(
                    "attestation-current",
                    changed <= attested_at,
                    f"the production environment was modified at {changed.isoformat()}, after the "
                    f"last attestation at {attested_at.isoformat()} — review the change and re-attest"
                    if changed > attested_at
                    else "the live environment has not been modified since the last attestation",
                )
        else:
            record("attestation-current", False, "environment metadata carries no updated_at timestamp")

    failed = [check for check in checks if check["status"] != PASS]
    status = FAIL if failed else PASS
    why = (
        "; ".join(check["why"] for check in failed)
        if failed
        else f"environment {expected_name!r} enforces an independent human approval for production"
    )
    return {
        "gate": "production-approval",
        "status": status,
        "why": why,
        "environment": expected_name,
        "repository": env_policy.get("repository"),
        "expected_reviewer": {
            "id": reviewer_policy["id"],
            "login": reviewer_policy.get("login"),
        },
        "generatedAt": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "checks": checks,
    }


def _read_json(path: Path | None) -> tuple[dict | None, str | None]:
    if path is None:
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name} is not readable JSON: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--environment-metadata", type=Path, default=None)
    parser.add_argument(
        "--unreadable-reason",
        default=None,
        help="why the environment could not be read (pass an HTTP status, never a token)",
    )
    parser.add_argument("--repository-metadata", type=Path, default=None)
    parser.add_argument("--branch-metadata", type=Path, default=None)
    parser.add_argument("--promotion-ref", default=None)
    parser.add_argument("--promotion-actor", default=None)
    parser.add_argument("--promotion-actor-id", type=int, default=None)
    parser.add_argument("--evidence-out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        policy = load_policy(args.policy)
        environment, env_error = _read_json(args.environment_metadata)
        repository, _ = _read_json(args.repository_metadata)
        branch, _ = _read_json(args.branch_metadata)
        evidence = evaluate(
            policy,
            environment=environment,
            unreadable_reason=args.unreadable_reason or env_error,
            repository=repository,
            branch=branch,
            promotion_ref=args.promotion_ref,
            promotion_actor=args.promotion_actor,
            promotion_actor_id=args.promotion_actor_id,
        )
    except ApprovalPolicyError as exc:
        evidence = {
            "gate": "production-approval",
            "status": FAIL,
            "why": f"approval policy is not trustworthy: {exc}",
            "checks": [],
        }

    if args.evidence_out is not None:
        args.evidence_out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    print(f"== production approval gate — {evidence['status'].upper()} ==")
    for check in evidence.get("checks", []):
        print(f"  [{check['status']}] {check['check']}: {check['why']}")
    print(f"{'OK' if evidence['status'] == PASS else 'REFUSED'}: {evidence['why']}")
    return 0 if evidence["status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
