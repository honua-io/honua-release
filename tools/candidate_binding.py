#!/usr/bin/env python3
"""Bind a release-train report to the exact candidate artifacts it certified.

The release train emits one immutable bundle containing the gate report, platform manifest, and
compatibility matrix. The report carries the SHA-256 digest of both files and the GitHub source/run
identity that produced them. Promotion verifies all of those fields against the Actions run API
before it parses or finalizes the candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

BINDING_SCHEMA_VERSION = 1
PLATFORM_MANIFEST = "platform-manifest.yaml"
COMPATIBILITY_MATRIX = "compatibility-matrix.yaml"
CERTIFICATION_MODES = frozenset({"live", "dry-run"})
MAX_REQUIRED_REVIEWERS = 6
_ARTIFACT_FILENAMES = (PLATFORM_MANIFEST, COMPATIBILITY_MATRIX)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LIVE_REPORT_MAX_AGE_HOURS = 24
REQUIRED_RELEASE_GATES = frozenset({
    "manifest", "artifact-consume", "e2e", "cloud-parity", "build-test", "contract",
    "conformance", "security", "sbom", "observability", "docs", "upgrade", "evidence",
    "protocol-certification",
})


class CandidateBindingError(ValueError):
    """Raised when a candidate bundle cannot be safely created."""


def validate_live_report(report: dict, *, now: datetime | None = None) -> tuple[bool, str]:
    """Require a fresh, complete, skip-free report before live certification or promotion."""
    if not isinstance(report, dict):
        return False, "gate report must be an object"
    if report.get("dry_run") is not False:
        return False, "live certification requires dry_run=false"
    if report.get("overallStatus") != "pass":
        return False, f"live gate report overallStatus is {report.get('overallStatus')!r}, expected 'pass'"

    generated = report.get("generatedAt")
    try:
        generated_at = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        return False, "live gate report has no valid timezone-aware generatedAt receipt timestamp"
    age_hours = ((now or datetime.now(timezone.utc)) - generated_at).total_seconds() / 3600
    if age_hours < -1:
        return False, "live gate report generatedAt is implausibly in the future"
    if age_hours > LIVE_REPORT_MAX_AGE_HOURS:
        return False, (
            f"live gate report is stale ({age_hours:.1f}h old; max-age "
            f"{LIVE_REPORT_MAX_AGE_HOURS}h)"
        )

    gates = report.get("gates")
    if not isinstance(gates, list):
        return False, "live gate report gates must be a list"
    by_name = {}
    for row in gates:
        if not isinstance(row, dict) or not isinstance(row.get("gate"), str):
            return False, "live gate report contains a malformed gate receipt"
        name = row["gate"]
        if name in by_name:
            return False, f"live gate report contains duplicate receipt for required gate {name!r}"
        by_name[name] = row
    missing = sorted(REQUIRED_RELEASE_GATES - by_name.keys())
    if missing:
        return False, f"live gate report is missing required gate receipt(s): {', '.join(missing)}"
    non_pass = sorted(
        f"{name}={by_name[name].get('status')!r}"
        for name in REQUIRED_RELEASE_GATES
        if by_name[name].get("status") != "pass"
    )
    if non_pass:
        return False, "live required gates must be pass (skip/blocked/fail is RED): " + ", ".join(non_pass)
    return True, f"all {len(REQUIRED_RELEASE_GATES)} required gates have fresh pass receipts"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_artifact(path: Path, filename: str) -> None:
    if path.name != filename:
        raise CandidateBindingError(f"candidate artifact must be named {filename!r}, got {path.name!r}")
    if path.is_symlink():
        raise CandidateBindingError(f"candidate artifact {filename!r} must not be a symlink")
    if not path.is_file():
        raise CandidateBindingError(f"candidate artifact {filename!r} is missing")


def _require_identity(
    source_repository: str,
    source_sha: str,
    source_branch: str,
    workflow_path: str,
    train_run_id: str,
    train_run_attempt: int,
    train_run_url: str,
    certification_mode: str,
) -> None:
    if source_repository.count("/") != 1 or any(not part for part in source_repository.split("/")):
        raise CandidateBindingError("source repository must be an owner/repository name")
    if not _SHA_PATTERN.fullmatch(source_sha):
        raise CandidateBindingError("source SHA must be a lowercase 40-64 character hexadecimal digest")
    if not source_branch or any(ord(char) < 33 for char in source_branch):
        raise CandidateBindingError("source branch must be a non-empty Git branch name without whitespace")
    if not workflow_path.startswith(".github/workflows/") or not workflow_path.endswith((".yml", ".yaml")):
        raise CandidateBindingError("workflow path must identify a GitHub Actions workflow")
    if not train_run_id.isdigit() or int(train_run_id) <= 0:
        raise CandidateBindingError("train run id must be a positive integer")
    if train_run_attempt <= 0:
        raise CandidateBindingError("train run attempt must be a positive integer")
    if train_run_url != f"https://github.com/{source_repository}/actions/runs/{train_run_id}":
        raise CandidateBindingError("train run URL does not match its repository and run id")
    if certification_mode not in CERTIFICATION_MODES:
        raise CandidateBindingError(
            f"certification mode must be one of {sorted(CERTIFICATION_MODES)}, got {certification_mode!r}"
        )


def validate_train_run_metadata(
    run: dict,
    repository: dict,
    branch: dict,
    *,
    expected_repository: str,
    expected_workflow_path: str,
    expected_run_id: str,
) -> tuple[bool, str, dict | None]:
    """Validate a certifying run against authoritative Actions and repository metadata."""
    if not isinstance(run, dict) or not isinstance(repository, dict) or not isinstance(branch, dict):
        return False, "Actions run, repository, and branch metadata must be objects", None
    if repository.get("full_name") != expected_repository:
        return False, "repository metadata does not match the promotion repository", None

    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        return False, "repository metadata has no default branch", None
    if branch.get("name") != default_branch:
        return False, "branch metadata does not describe the repository default branch", None
    if branch.get("protected") is not True:
        return False, f"repository default branch {default_branch!r} is not protected", None

    run_repository = run.get("repository")
    head_repository = run.get("head_repository")
    checks = {
        "run repository": isinstance(run_repository, dict)
        and run_repository.get("full_name") == expected_repository,
        "head repository": isinstance(head_repository, dict)
        and head_repository.get("full_name") == expected_repository,
        "workflow path": run.get("path") == expected_workflow_path,
        "event": run.get("event") == "workflow_dispatch",
        "status": run.get("status") == "completed",
        "conclusion": run.get("conclusion") == "success",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        return False, f"selected run failed trusted release-train metadata checks: {failed}", None

    source_branch = run.get("head_branch")
    if source_branch != default_branch:
        return (
            False,
            f"selected run head branch {source_branch!r} is not repository default branch {default_branch!r}",
            None,
        )

    run_id = run.get("id")
    run_attempt = run.get("run_attempt")
    source_sha = run.get("head_sha")
    run_url = run.get("html_url")
    if not isinstance(run_id, int) or run_id <= 0:
        return False, "selected run id must be a positive integer", None
    if str(run_id) != expected_run_id:
        return False, "selected run metadata id does not match requested train run id", None
    if not isinstance(run_attempt, int) or run_attempt <= 0:
        return False, "selected run attempt must be a positive integer", None
    if not isinstance(source_sha, str) or not _SHA_PATTERN.fullmatch(source_sha):
        return False, "selected run head SHA is not a lowercase hexadecimal commit digest", None
    expected_url = f"https://github.com/{expected_repository}/actions/runs/{run_id}"
    if run_url != expected_url:
        return False, "selected run URL does not match its repository and run id", None

    identity = {
        "run_id": str(run_id),
        "run_attempt": str(run_attempt),
        "run_url": run_url,
        "source_repository": expected_repository,
        "source_sha": source_sha,
        "source_branch": source_branch,
        "default_branch": default_branch,
        "workflow_path": expected_workflow_path,
    }
    return True, f"selected run is a successful release train from default branch {default_branch!r}", identity


def validate_environment_metadata(
    environment: dict,
    *,
    expected_name: str,
    expected_reviewer_ids: list[int],
) -> tuple[bool, str]:
    """Require a protected-branch environment with the exact expected human-reviewer roster."""
    if not isinstance(expected_reviewer_ids, list) or not expected_reviewer_ids:
        return False, "expected reviewer roster must contain at least one reviewer id"
    if len(expected_reviewer_ids) > MAX_REQUIRED_REVIEWERS:
        return False, f"expected reviewer roster may contain at most {MAX_REQUIRED_REVIEWERS} reviewer ids"
    if any(
        not isinstance(reviewer_id, int) or isinstance(reviewer_id, bool) or reviewer_id <= 0
        for reviewer_id in expected_reviewer_ids
    ):
        return False, "expected reviewer roster must contain only positive integer ids"
    if len(set(expected_reviewer_ids)) != len(expected_reviewer_ids):
        return False, "expected reviewer roster must not contain duplicate reviewer ids"

    if not isinstance(environment, dict):
        return False, "environment metadata must be an object"
    if environment.get("name") != expected_name:
        return False, f"environment metadata does not describe {expected_name!r}"

    branch_policy = environment.get("deployment_branch_policy")
    if not isinstance(branch_policy, dict) or branch_policy.get("protected_branches") is not True:
        return False, "release-promotion environment must allow deployments only from protected branches"
    if branch_policy.get("custom_branch_policies") is not False:
        return False, "release-promotion environment protected-branch policy is inconsistent"

    rules = environment.get("protection_rules")
    if not isinstance(rules, list):
        return False, "release-promotion environment protection_rules must be a list"
    reviewer_rules = [rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "required_reviewers"]
    if not reviewer_rules:
        return False, "release-promotion environment has no required-reviewer protection rule"
    if len(reviewer_rules) != 1:
        return False, "release-promotion environment must have exactly one required-reviewer rule"

    reviewers = reviewer_rules[0].get("reviewers")
    if not isinstance(reviewers, list) or not reviewers:
        return False, "release-promotion environment reviewer roster must contain at least one reviewer"
    if len(reviewers) > MAX_REQUIRED_REVIEWERS:
        return False, (
            "release-promotion environment reviewer roster may contain at most "
            f"{MAX_REQUIRED_REVIEWERS} reviewers"
        )

    observed_ids: list[int] = []
    for reviewer in reviewers:
        principal = reviewer.get("reviewer") if isinstance(reviewer, dict) else None
        reviewer_id = principal.get("id") if isinstance(principal, dict) else None
        if (
            not isinstance(reviewer, dict)
            or reviewer.get("type") != "User"
            or not isinstance(reviewer_id, int)
            or isinstance(reviewer_id, bool)
            or reviewer_id <= 0
        ):
            return False, "release-promotion environment reviewer roster must contain only User reviewers"
        observed_ids.append(reviewer_id)

    if len(set(observed_ids)) != len(observed_ids):
        return False, "release-promotion environment reviewer roster contains duplicate reviewer ids"
    if set(observed_ids) != set(expected_reviewer_ids):
        return False, (
            f"release-promotion environment reviewer ids {sorted(observed_ids)} do not match "
            f"expected roster {sorted(expected_reviewer_ids)}"
        )

    prevents_self_review = reviewer_rules[0].get("prevent_self_review") is True
    self_review = "disabled" if prevents_self_review else "allowed"
    return True, (
        "release-promotion environment requires the exact expected human-reviewer roster "
        f"{sorted(observed_ids)}; self-review is {self_review}"
    )


def build_candidate_binding(
    manifest_path: Path,
    matrix_path: Path,
    *,
    source_repository: str,
    source_sha: str,
    source_branch: str,
    workflow_path: str,
    train_run_id: str,
    train_run_attempt: int,
    train_run_url: str,
    certification_mode: str,
) -> dict:
    """Build the report fragment binding candidate bytes to their train/source identity."""
    _require_artifact(manifest_path, PLATFORM_MANIFEST)
    _require_artifact(matrix_path, COMPATIBILITY_MATRIX)
    _require_identity(
        source_repository,
        source_sha,
        source_branch,
        workflow_path,
        train_run_id,
        train_run_attempt,
        train_run_url,
        certification_mode,
    )

    artifacts = {}
    for path in (manifest_path, matrix_path):
        artifacts[path.name] = {
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }

    return {
        "schemaVersion": BINDING_SCHEMA_VERSION,
        "source": {
            "repository": source_repository,
            "sha": source_sha,
            "branch": source_branch,
        },
        "train": {
            "workflowPath": workflow_path,
            "runId": train_run_id,
            "runAttempt": train_run_attempt,
            "runUrl": train_run_url,
            "certificationMode": certification_mode,
        },
        "artifacts": artifacts,
    }


def bind_gate_report(report: dict, manifest_path: Path, matrix_path: Path, **identity) -> dict:
    """Return a copy of ``report`` carrying one non-replaceable candidate binding."""
    if not isinstance(report, dict):
        raise CandidateBindingError("gate report must be an object")
    if "candidate" in report:
        raise CandidateBindingError("gate report already contains a candidate binding")
    dry_run = report.get("dry_run")
    if not isinstance(dry_run, bool):
        raise CandidateBindingError("gate report dry_run must be a boolean")
    report_mode = "dry-run" if dry_run else "live"
    if identity.get("certification_mode") != report_mode:
        raise CandidateBindingError("gate report dry_run does not match candidate certification mode")
    if report_mode == "live":
        ok, why = validate_live_report(report)
        if not ok:
            raise CandidateBindingError(why)
    bound = dict(report)
    bound["candidate"] = build_candidate_binding(manifest_path, matrix_path, **identity)
    return bound


def verify_candidate_binding(
    report: dict,
    manifest_path: Path,
    matrix_path: Path,
    *,
    source_repository: str,
    source_sha: str,
    source_branch: str,
    workflow_path: str,
    train_run_id: str,
    train_run_attempt: int,
    train_run_url: str,
    certification_mode: str,
) -> tuple[bool, str]:
    """Verify candidate bytes and report identity against trusted Actions run metadata."""
    try:
        _require_artifact(manifest_path, PLATFORM_MANIFEST)
        _require_artifact(matrix_path, COMPATIBILITY_MATRIX)
        _require_identity(
            source_repository,
            source_sha,
            source_branch,
            workflow_path,
            train_run_id,
            train_run_attempt,
            train_run_url,
            certification_mode,
        )
    except CandidateBindingError as exc:
        return False, str(exc)

    if not isinstance(report, dict):
        return False, "gate report is not an object"
    dry_run = report.get("dry_run")
    if not isinstance(dry_run, bool):
        return False, "gate report dry_run must be a boolean"
    candidate = report.get("candidate")
    if not isinstance(candidate, dict):
        return False, "gate report has no candidate binding"
    if candidate.get("schemaVersion") != BINDING_SCHEMA_VERSION:
        return False, f"unsupported candidate binding schemaVersion={candidate.get('schemaVersion')!r}"

    source = candidate.get("source")
    train = candidate.get("train")
    if not isinstance(source, dict) or not isinstance(train, dict):
        return False, "candidate binding has incomplete source/train identity"

    expected_identity = {
        "source.repository": source_repository,
        "source.sha": source_sha,
        "source.branch": source_branch,
        "train.workflowPath": workflow_path,
        "train.runId": train_run_id,
        "train.runAttempt": train_run_attempt,
        "train.runUrl": train_run_url,
        "train.certificationMode": certification_mode,
    }
    actual_identity = {
        "source.repository": source.get("repository"),
        "source.sha": source.get("sha"),
        "source.branch": source.get("branch"),
        "train.workflowPath": train.get("workflowPath"),
        "train.runId": train.get("runId"),
        "train.runAttempt": train.get("runAttempt"),
        "train.runUrl": train.get("runUrl"),
        "train.certificationMode": train.get("certificationMode"),
    }
    mismatches = [
        name for name, expected in expected_identity.items()
        if actual_identity[name] != expected
    ]
    if mismatches:
        return False, f"candidate source/train identity mismatch: {mismatches}"

    report_mode = "dry-run" if dry_run else "live"
    if train.get("certificationMode") != report_mode:
        return False, "gate report dry_run does not match bound train certificationMode"
    if report_mode == "live":
        ok, why = validate_live_report(report)
        if not ok:
            return False, why

    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(_ARTIFACT_FILENAMES):
        return False, "candidate binding must contain exactly the platform manifest and compatibility matrix"

    for path in (manifest_path, matrix_path):
        record = artifacts.get(path.name)
        if not isinstance(record, dict):
            return False, f"candidate binding for {path.name!r} is invalid"
        expected_digest = record.get("sha256")
        expected_size = record.get("size")
        if not isinstance(expected_digest, str) or not _SHA256_PATTERN.fullmatch(expected_digest):
            return False, f"candidate binding for {path.name!r} has an invalid SHA-256 digest"
        if not isinstance(expected_size, int) or expected_size < 0:
            return False, f"candidate binding for {path.name!r} has an invalid size"
        if path.stat().st_size != expected_size:
            return False, f"candidate artifact {path.name!r} size mismatch"
        if not hmac.compare_digest(_sha256(path), expected_digest):
            return False, f"candidate artifact {path.name!r} SHA-256 mismatch"

    return True, f"candidate artifacts match certified train run {train_run_id} attempt {train_run_attempt}"


def create_bundle(
    report_path: Path,
    manifest_path: Path,
    matrix_path: Path,
    out_dir: Path,
    **identity,
) -> Path:
    """Create a fresh directory containing the exact candidate bytes and bound report."""
    if out_dir.exists():
        raise CandidateBindingError(f"output bundle directory already exists: {out_dir}")
    out_dir.mkdir(parents=True)

    bundled_manifest = out_dir / PLATFORM_MANIFEST
    bundled_matrix = out_dir / COMPATIBILITY_MATRIX
    shutil.copyfile(manifest_path, bundled_manifest)
    shutil.copyfile(matrix_path, bundled_matrix)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    bound = bind_gate_report(report, bundled_manifest, bundled_matrix, **identity)
    bound_report = out_dir / "gate-report.json"
    bound_report.write_text(json.dumps(bound, indent=2) + "\n", encoding="utf-8")
    return bound_report


def _add_identity_args(parser: argparse.ArgumentParser, *, include_url: bool) -> None:
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--train-run-id", required=True)
    parser.add_argument("--train-run-attempt", required=True, type=int)
    parser.add_argument("--certification-mode", required=True, choices=sorted(CERTIFICATION_MODES))
    if include_url:
        parser.add_argument("--train-run-url", required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate_run = commands.add_parser("validate-run", help="validate certifying Actions run metadata")
    validate_run.add_argument("--run-metadata", required=True, type=Path)
    validate_run.add_argument("--repository-metadata", required=True, type=Path)
    validate_run.add_argument("--branch-metadata", required=True, type=Path)
    validate_run.add_argument("--expected-repository", required=True)
    validate_run.add_argument("--expected-workflow-path", required=True)
    validate_run.add_argument("--expected-run-id", required=True)
    validate_run.add_argument("--github-output", required=True, type=Path)

    validate_environment = commands.add_parser(
        "validate-environment",
        help="validate the promotion environment approval policy",
    )
    validate_environment.add_argument("--environment-metadata", required=True, type=Path)
    validate_environment.add_argument("--expected-name", required=True)
    validate_environment.add_argument(
        "--expected-reviewer-id",
        dest="expected_reviewer_ids",
        required=True,
        action="append",
        type=int,
        help="expected human reviewer id; repeat for every roster member",
    )

    bind = commands.add_parser("bind", help="create a certified candidate bundle")
    bind.add_argument("--report", required=True, type=Path)
    bind.add_argument("--manifest", required=True, type=Path)
    bind.add_argument("--matrix", required=True, type=Path)
    bind.add_argument("--out-dir", required=True, type=Path)
    _add_identity_args(bind, include_url=True)

    verify = commands.add_parser("verify", help="verify a certified candidate bundle")
    verify.add_argument("--report", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--matrix", required=True, type=Path)
    _add_identity_args(verify, include_url=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-environment":
            environment = json.loads(args.environment_metadata.read_text(encoding="utf-8"))
            ok, why = validate_environment_metadata(
                environment,
                expected_name=args.expected_name,
                expected_reviewer_ids=args.expected_reviewer_ids,
            )
            print(f"{'OK' if ok else 'REFUSED'}: {why}", file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1

        if args.command == "validate-run":
            run = json.loads(args.run_metadata.read_text(encoding="utf-8"))
            repository = json.loads(args.repository_metadata.read_text(encoding="utf-8"))
            branch = json.loads(args.branch_metadata.read_text(encoding="utf-8"))
            ok, why, identity = validate_train_run_metadata(
                run,
                repository,
                branch,
                expected_repository=args.expected_repository,
                expected_workflow_path=args.expected_workflow_path,
                expected_run_id=args.expected_run_id,
            )
            if not ok or identity is None:
                print(f"REFUSED: {why}", file=sys.stderr)
                return 1
            with args.github_output.open("a", encoding="utf-8") as output:
                for name, value in identity.items():
                    output.write(f"{name}={value}\n")
            print(f"OK: {why}")
            return 0

        if args.command == "bind":
            report = create_bundle(
                args.report,
                args.manifest,
                args.matrix,
                args.out_dir,
                source_repository=args.source_repository,
                source_sha=args.source_sha,
                source_branch=args.source_branch,
                workflow_path=args.workflow_path,
                train_run_id=args.train_run_id,
                train_run_attempt=args.train_run_attempt,
                train_run_url=args.train_run_url,
                certification_mode=args.certification_mode,
            )
            print(f"certified candidate bundle -> {report.parent}")
            return 0

        report = json.loads(args.report.read_text(encoding="utf-8"))
        ok, why = verify_candidate_binding(
            report,
            args.manifest,
            args.matrix,
            source_repository=args.source_repository,
            source_sha=args.source_sha,
            source_branch=args.source_branch,
            workflow_path=args.workflow_path,
            train_run_id=args.train_run_id,
            train_run_attempt=args.train_run_attempt,
            train_run_url=args.train_run_url,
            certification_mode=args.certification_mode,
        )
        print(f"{'OK' if ok else 'REFUSED'}: {why}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    except (CandidateBindingError, OSError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
