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
from pathlib import Path

BINDING_SCHEMA_VERSION = 1
PLATFORM_MANIFEST = "platform-manifest.yaml"
COMPATIBILITY_MATRIX = "compatibility-matrix.yaml"
_ARTIFACT_FILENAMES = (PLATFORM_MANIFEST, COMPATIBILITY_MATRIX)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class CandidateBindingError(ValueError):
    """Raised when a candidate bundle cannot be safely created."""


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
    workflow_path: str,
    train_run_id: str,
    train_run_attempt: int,
    train_run_url: str,
) -> None:
    if source_repository.count("/") != 1 or any(not part for part in source_repository.split("/")):
        raise CandidateBindingError("source repository must be an owner/repository name")
    if not _SHA_PATTERN.fullmatch(source_sha):
        raise CandidateBindingError("source SHA must be a lowercase 40-64 character hexadecimal digest")
    if not workflow_path.startswith(".github/workflows/") or not workflow_path.endswith((".yml", ".yaml")):
        raise CandidateBindingError("workflow path must identify a GitHub Actions workflow")
    if not train_run_id.isdigit() or int(train_run_id) <= 0:
        raise CandidateBindingError("train run id must be a positive integer")
    if train_run_attempt <= 0:
        raise CandidateBindingError("train run attempt must be a positive integer")
    if train_run_url != f"https://github.com/{source_repository}/actions/runs/{train_run_id}":
        raise CandidateBindingError("train run URL does not match its repository and run id")


def build_candidate_binding(
    manifest_path: Path,
    matrix_path: Path,
    *,
    source_repository: str,
    source_sha: str,
    workflow_path: str,
    train_run_id: str,
    train_run_attempt: int,
    train_run_url: str,
) -> dict:
    """Build the report fragment binding candidate bytes to their train/source identity."""
    _require_artifact(manifest_path, PLATFORM_MANIFEST)
    _require_artifact(matrix_path, COMPATIBILITY_MATRIX)
    _require_identity(
        source_repository,
        source_sha,
        workflow_path,
        train_run_id,
        train_run_attempt,
        train_run_url,
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
        },
        "train": {
            "workflowPath": workflow_path,
            "runId": train_run_id,
            "runAttempt": train_run_attempt,
            "runUrl": train_run_url,
        },
        "artifacts": artifacts,
    }


def bind_gate_report(report: dict, manifest_path: Path, matrix_path: Path, **identity) -> dict:
    """Return a copy of ``report`` carrying one non-replaceable candidate binding."""
    if not isinstance(report, dict):
        raise CandidateBindingError("gate report must be an object")
    if "candidate" in report:
        raise CandidateBindingError("gate report already contains a candidate binding")
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
    workflow_path: str,
    train_run_id: str,
    train_run_attempt: int,
    train_run_url: str,
) -> tuple[bool, str]:
    """Verify candidate bytes and report identity against trusted Actions run metadata."""
    try:
        _require_artifact(manifest_path, PLATFORM_MANIFEST)
        _require_artifact(matrix_path, COMPATIBILITY_MATRIX)
        _require_identity(
            source_repository,
            source_sha,
            workflow_path,
            train_run_id,
            train_run_attempt,
            train_run_url,
        )
    except CandidateBindingError as exc:
        return False, str(exc)

    if not isinstance(report, dict):
        return False, "gate report is not an object"
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
        "train.workflowPath": workflow_path,
        "train.runId": train_run_id,
        "train.runAttempt": train_run_attempt,
        "train.runUrl": train_run_url,
    }
    actual_identity = {
        "source.repository": source.get("repository"),
        "source.sha": source.get("sha"),
        "train.workflowPath": train.get("workflowPath"),
        "train.runId": train.get("runId"),
        "train.runAttempt": train.get("runAttempt"),
        "train.runUrl": train.get("runUrl"),
    }
    mismatches = [
        name for name, expected in expected_identity.items()
        if actual_identity[name] != expected
    ]
    if mismatches:
        return False, f"candidate source/train identity mismatch: {mismatches}"

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
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--train-run-id", required=True)
    parser.add_argument("--train-run-attempt", required=True, type=int)
    if include_url:
        parser.add_argument("--train-run-url", required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

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
        if args.command == "bind":
            report = create_bundle(
                args.report,
                args.manifest,
                args.matrix,
                args.out_dir,
                source_repository=args.source_repository,
                source_sha=args.source_sha,
                workflow_path=args.workflow_path,
                train_run_id=args.train_run_id,
                train_run_attempt=args.train_run_attempt,
                train_run_url=args.train_run_url,
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
            workflow_path=args.workflow_path,
            train_run_id=args.train_run_id,
            train_run_attempt=args.train_run_attempt,
            train_run_url=args.train_run_url,
        )
        print(f"{'OK' if ok else 'REFUSED'}: {why}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    except (CandidateBindingError, OSError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
