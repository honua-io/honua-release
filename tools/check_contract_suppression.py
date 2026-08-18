#!/usr/bin/env python3
"""Release gate: repo-wide contract breaking-change suppression must not survive publication.

honua-io/honua-server can switch off OpenAPI breaking-change enforcement for EVERY pull request with
one repository variable (`OPENAPI_ALLOW_BREAKING_CHANGES`). Pre-publication that is deliberate; at
publication it is a silent hole. This is the decision core of the release gate that makes the state of
that variable a certified, machine-readable part of every candidate instead of a note someone has to
remember (honua-release#71).

The core is pure: it takes the suppression register (certification/contract-suppression-policy.yaml)
and the repository-variable listing already fetched by the workflow, and returns a verdict. It never
reads a token, never performs network I/O, and never echoes a variable's value other than the single
boolean-ish flag it is asked about.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# Mirrors honua-io/honua-server scripts/ci/openapi-breaking-change-policy.py: a typo must never be
# read as "false" and silently disarm the gate.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})

PASS = "pass"
BLOCKED = "blocked"


class SuppressionPolicyError(ValueError):
    """Raised when the suppression register itself cannot be trusted."""


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SuppressionPolicyError(f"{field} must be an ISO-8601 UTC timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SuppressionPolicyError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_policy(path: Path) -> dict:
    """Load and structurally validate the suppression register."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SuppressionPolicyError(f"suppression register is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise SuppressionPolicyError("suppression register must be a mapping")
    suppression = raw.get("suppression")
    if not isinstance(suppression, dict):
        raise SuppressionPolicyError("suppression register has no `suppression` block")
    for field in ("repository", "variable", "window_opened_at"):
        if not isinstance(suppression.get(field), str) or not suppression[field].strip():
            raise SuppressionPolicyError(f"suppression.{field} is required")
    _parse_timestamp(suppression["window_opened_at"], "suppression.window_opened_at")
    return raw


def read_variable_flag(listing: dict, variable: str) -> tuple[bool, str]:
    """Resolve the suppression flag from a successfully-read repository-variable listing.

    `listing` is the GitHub `GET /repos/{owner}/{repo}/actions/variables` response. Reading the whole
    listing (rather than the single variable) is what makes "absent" distinguishable from "no read
    access": a 404 on the single-variable endpoint means either, while a listing that we could read at
    all proves access and therefore proves absence.
    """
    if not isinstance(listing, dict):
        raise SuppressionPolicyError("repository-variable listing must be an object")
    variables = listing.get("variables")
    if not isinstance(variables, list):
        raise SuppressionPolicyError("repository-variable listing has no `variables` array")
    for entry in variables:
        if isinstance(entry, dict) and entry.get("name") == variable:
            value = entry.get("value")
            if not isinstance(value, str):
                raise SuppressionPolicyError(f"{variable} has a non-string value")
            normalised = value.strip().lower()
            if normalised in _TRUE_VALUES:
                return True, f"{variable} is set and enables suppression"
            if normalised in _FALSE_VALUES:
                return False, f"{variable} is set but disables suppression"
            raise SuppressionPolicyError(
                f"{variable} has an unparseable value — refusing to read it as 'false'"
            )
    return False, f"{variable} is not set (enforcement is on by default)"


def _check_steady_state(policy: dict) -> tuple[bool, str]:
    mechanism = policy.get("steady_state_mechanism")
    if not isinstance(mechanism, dict):
        return False, "no steady-state mechanism is declared in the suppression register"
    if mechanism.get("status") != "landed":
        return False, (
            "the steady-state replacement mechanism is not declared landed "
            f"(status={mechanism.get('status')!r})"
        )
    evidence = mechanism.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False, "the declared steady-state mechanism carries no evidence references"
    return True, f"steady-state mechanism landed: {mechanism.get('mechanism', 'unnamed')}"


def _check_window_audit(policy: dict, *, suppression_on: bool, now: datetime) -> tuple[bool, str]:
    audit = policy.get("window_audit")
    if not isinstance(audit, dict):
        return False, "the suppression window has not been audited"
    if audit.get("verdict") != "no-genuine-break":
        return False, f"the suppression-window audit verdict is {audit.get('verdict')!r}"
    covers_through = _parse_timestamp(audit.get("covers_through"), "window_audit.covers_through")
    if not suppression_on:
        return True, f"suppression window audited through {covers_through.isoformat()}"
    max_age_days = audit.get("max_audit_age_days")
    if not isinstance(max_age_days, int) or max_age_days <= 0:
        return False, "window_audit.max_audit_age_days must be a positive integer"
    deadline = covers_through + timedelta(days=max_age_days)
    if now > deadline:
        return False, (
            f"the suppression window is still open and its audit is stale "
            f"(covers through {covers_through.isoformat()}, max age {max_age_days}d)"
        )
    return True, (
        f"suppression window audited through {covers_through.isoformat()} "
        f"(within {max_age_days}d while the window is open)"
    )


def evaluate(
    policy: dict,
    *,
    variable_listing: dict | None,
    unreadable_reason: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Return the gate verdict as a machine-readable evidence record."""
    now = now or datetime.now(timezone.utc)
    suppression = policy["suppression"]
    repository = suppression["repository"]
    variable = suppression["variable"]
    checks: list[dict] = []

    def record(name: str, ok: bool, why: str) -> None:
        checks.append({"check": name, "status": PASS if ok else BLOCKED, "why": why})

    suppression_on = False
    readable = True
    if variable_listing is None:
        readable = False
        record(
            "variable-readable",
            False,
            f"could not read {repository} repository variables: {unreadable_reason or 'unknown error'}"
            f" — the release token (RELEASE_GH_TOKEN) needs read access to {repository}'s Actions"
            " variables for this gate to certify anything (honua-io/honua-release#92). Until it can,"
            " the suppression state is UNKNOWN and is treated as active",
        )
        suppression_on = True  # fail closed: treat an unknown state as the dangerous one
    else:
        try:
            suppression_on, why = read_variable_flag(variable_listing, variable)
            record("variable-readable", True, why)
        except SuppressionPolicyError as exc:
            readable = False
            record("variable-readable", False, str(exc))
            suppression_on = True

    record(
        "suppression-off",
        not suppression_on,
        (
            f"repo-wide breaking-change suppression is ACTIVE in {repository} "
            f"(window opened {suppression['window_opened_at']}); it must be off, or replaced by the "
            f"per-PR mechanism, before the first published control-plane API release "
            f"({suppression.get('release_gate_issue', 'honua-io/honua-release#71')})"
        )
        if suppression_on
        else f"no repo-wide breaking-change suppression is in effect in {repository}",
    )

    ok, why = _check_steady_state(policy)
    record("steady-state-mechanism", ok, why)

    ok, why = _check_window_audit(policy, suppression_on=suppression_on, now=now)
    record("window-audit", ok, why)

    failed = [check for check in checks if check["status"] != PASS]
    status = BLOCKED if failed else PASS
    if failed:
        why = "; ".join(check["why"] for check in failed)
    else:
        why = (
            f"{variable} does not suppress breaking-change enforcement in {repository}, the "
            "steady-state per-PR mechanism has landed, and the suppression window is audited clean"
        )
    return {
        "gate": "contract-suppression",
        "status": status,
        "why": why,
        "repository": repository,
        "variable": variable,
        # `suppression_active` is the FAIL-CLOSED reading: unknown counts as active, because a gate
        # that cannot see the flag must not certify that the flag is off. `suppression_state` is the
        # HONEST one — a reader (or a human triaging a red train) can tell "the suppression really is
        # on" apart from "the release token lost its read access", which are the same `blocked` but
        # very different actions (honua-io/honua-release#92).
        "suppression_active": suppression_on,
        "suppression_state": ("active" if (readable and suppression_on)
                              else "off" if readable else "unknown"),
        "variable_readable": readable,
        "generatedAt": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument(
        "--variable-listing",
        type=Path,
        help="GET /repos/{owner}/{repo}/actions/variables response; omit when it could not be read",
    )
    parser.add_argument(
        "--unreadable-reason",
        default=None,
        help="why the listing could not be read (never pass a token or a variable value)",
    )
    parser.add_argument("--evidence-out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        policy = load_policy(args.policy)
        listing = None
        unreadable = args.unreadable_reason
        if args.variable_listing is not None:
            try:
                listing = json.loads(args.variable_listing.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                listing = None
                unreadable = unreadable or f"listing is not readable JSON: {exc}"
        evidence = evaluate(policy, variable_listing=listing, unreadable_reason=unreadable)
    except SuppressionPolicyError as exc:
        evidence = {
            "gate": "contract-suppression",
            "status": BLOCKED,
            "why": f"suppression register is not trustworthy: {exc}",
            "checks": [],
        }

    if args.evidence_out is not None:
        args.evidence_out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")

    print(f"== contract breaking-change suppression — {evidence['status'].upper()} ==")
    for check in evidence.get("checks", []):
        print(f"  [{check['status']}] {check['check']}: {check['why']}")
    print(evidence["why"])
    return 0 if evidence["status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
