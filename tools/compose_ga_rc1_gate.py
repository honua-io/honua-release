#!/usr/bin/env python3
"""Deterministic Compose gate for 2026.1-rc.1 upgrade, rollback, and a real migration interrupt.

gate-upgrade's compose-upgrade cell keeps resolving the newest published rc. This gate does not
replace that cell and is not part of e2e-cloud-aws. It installs the server image the 2026.1-rc.1
release notes publish, requires that digest to keep the recorded prefix, upgrades to a newer trunk
build, verifies, rolls back, verifies again, then interrupts a real pending migration in the
candidate runner.

  check-release    confirm a release body still names the pin
  check-candidate  refuse a candidate that is not a newer descendant
  amd64-child      read the linux/amd64 manifest from an index document
  receipt          write honua.compose-ga-rc1-receipt/v1; exit 0 only when every phase passed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RC1_TAG = "2026.1-rc.1"
RC1_DIGEST_PREFIX = "sha256:c8d7aef9"
RC1_INDEX_DIGEST = "sha256:c8d7aef98761618145725530b6a4e4b25ea4a9ae121da168362f9be4cf5f7d37"
RC1_AMD64_DIGEST = "sha256:2c3ce36be9b6a4c8b3911d150de329be002131609933bc3ca4a3932ea95dc574"
RC1_SERVER_SHA = "a4bde4b8c5ee8cc3dec05ad8b3d4f84470c1ea6e"
RC1_SCHEMA = 140
REPOSITORY = "ghcr.io/honua-io/honua-server"
SCHEMA = "honua.compose-ga-rc1-receipt/v1"
COMPOSE_PROJECT = "honua-ga5-compose-upgrade"
HOST_PORT = 15085
UPGRADE_PHASES = ("install", "load", "upgrade", "verify", "rollback", "verify_again")
INTERRUPT_MODES = ("partial-migration-failure:backend-terminated", "partial-migration-failure:server-killed")
IMAGE_REF = re.compile(r"^\S+@sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
RC_SERVER_ROW = re.compile(
    r"^\|\s*honua-server\s*\|[^\n]*?\|\s*(?P<image>[^\s|`@]+)\s*@\s*`(?P<digest>sha256:[0-9a-f]{64})`\s*\|\s*$",
    re.MULTILINE)
# Harness failures, in the order compose-upgrade-rollback can emit them. The first match is the
# phase that failed; earlier phases passed and later phases did not run.
FAIL_AT = (
    ("prior image did not become ready", "install"),
    ("running prior container is not exactly", "install"),
    ("real e2e seed failed", "load"),
    ("did not publish the e2e_src_fs layer", "load"),
    ("could not capture the seeded prior state", "load"),
    ("seed did not produce the deterministic rows", "load"),
    ("rollback-target guard probe", "load"),
    ("mandatory pre-upgrade backup", "load"),
    ("printed no contract-migration nonce", "upgrade"),
    ("after the contract migrations were approved", "upgrade"),
    ("could not capture the upgraded state", "verify"),
    ("running candidate container is not exactly", "verify"),
    ("did not advance public.schema_versions", "verify"),
    ("does not exactly match the declared candidate schema", "verify"),
    ("seeded rows did not survive the upgrade", "verify"),
    ("rolled-back container is not exactly", "rollback"),
    ("rollback-target-ineligible", "rollback"),
    ("rollback-target-guard-unproven", "rollback"),
    ("instead of refusing with schema_reader_incompatible", "rollback"),
    ("outside the documented boundary", "rollback"),
    ("did not bring the prior back", "rollback"),
    ("restored container is not exactly", "rollback"),
    ("could not serve the seeded rows after rollback", "verify_again"),
    ("seeded rows changed across the rollback", "verify_again"),
)


class GateError(ValueError):
    """The pin or the candidate is not acceptable. The gate must not run."""


def _assert_pin() -> None:
    if not RC1_INDEX_DIGEST.startswith(RC1_DIGEST_PREFIX):
        raise GateError("rc.1 index digest does not keep the recorded prefix")
    if RC1_INDEX_DIGEST == RC1_AMD64_DIGEST:
        raise GateError("rc.1 index and amd64 child must be different digests")
    if not SHA.fullmatch(RC1_SERVER_SHA):
        raise GateError("rc.1 server sha is not a full commit")


_assert_pin()


def prior_image() -> str:
    return f"{REPOSITORY}@{RC1_INDEX_DIGEST}"


def check_release(body: str) -> str:
    """Return repository@index-digest. Refuse a moved, missing, or prefix-mismatched pin."""
    matches = RC_SERVER_ROW.findall(body or "")
    if len(matches) != 1:
        raise GateError(f"{RC1_TAG} release notes do not name exactly one honua-server image digest")
    image, digest = matches[0]
    named = image.split("@", 1)[0]
    last = named.rsplit("/", 1)[-1]
    repository = named[: named.rfind(":")] if ":" in last else named
    if repository != REPOSITORY:
        raise GateError(f"{RC1_TAG} names {repository}, not {REPOSITORY}")
    if digest != RC1_INDEX_DIGEST or not digest.startswith(RC1_DIGEST_PREFIX):
        raise GateError(f"{RC1_TAG} server digest moved off the pinned rc.1 image")
    return prior_image()


def amd64_child(index: dict) -> str:
    """The linux/amd64 manifest digest inside an OCI/Docker index. Fail closed on anything else."""
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list):
        raise GateError("rc.1 image index has no manifests")
    found = []
    for item in manifests:
        if not isinstance(item, dict):
            continue
        platform = item.get("platform") or {}
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            found.append(str(item.get("digest") or ""))
    if found != [RC1_AMD64_DIGEST]:
        raise GateError("rc.1 linux/amd64 child is not the pinned child of the release index")
    return RC1_AMD64_DIGEST


def check_candidate(image: str, sha: str, schema: int, ancestor: str) -> dict:
    if not IMAGE_REF.fullmatch(image or ""):
        raise GateError("candidate must be repository@sha256:<64 lowercase hex digits>")
    digest = image.rsplit("@", 1)[1]
    if digest in {RC1_INDEX_DIGEST, RC1_AMD64_DIGEST}:
        raise GateError("candidate image is the rc.1 image; a newer trunk build is required")
    if not SHA.fullmatch(sha or "") or sha == RC1_SERVER_SHA:
        raise GateError("candidate sha must be a full commit other than the rc.1 server commit")
    if ancestor != "yes":
        raise GateError("candidate commit is not a descendant of the rc.1 server commit")
    if not isinstance(schema, int) or schema <= RC1_SCHEMA:
        raise GateError(f"candidate schema must be newer than rc.1 schema {RC1_SCHEMA}")
    return {"image": image, "sha": sha, "schema": schema}


def _load(path: Path | None, default):
    if path is None:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"cannot read {path}: {exc}") from exc


def _row(document: dict, name: str) -> dict:
    rows = [row for row in (document or {}).get("scenarios", []) if row.get("scenario") == name]
    return rows[-1] if rows else {}


def upgrade_phases(row: dict) -> dict[str, str]:
    status = row.get("status")
    if status == "pass":
        return {phase: "pass" for phase in UPGRADE_PHASES}
    phases = {phase: "not-run" for phase in UPGRADE_PHASES}
    if status not in {"fail", "blocked"}:
        return phases
    why = str(row.get("why") or "")
    failed = next((phase for needle, phase in FAIL_AT if needle in why), None)
    if failed is None:
        return {phase: "fail" for phase in UPGRADE_PHASES}
    seen = False
    for phase in UPGRADE_PHASES:
        if phase == failed:
            phases[phase] = "fail" if status == "fail" else "blocked"
            seen = True
        elif not seen:
            phases[phase] = "pass"
    return phases


def interrupt_status(document: dict) -> tuple[str, dict]:
    modes = {name: _row(document, name) for name in INTERRUPT_MODES}
    present = {name: row for name, row in modes.items() if row}
    if not present:
        return "not-run", {name: "not-run" for name in INTERRUPT_MODES}
    detail = {name: (row.get("status") or "fail") for name, row in modes.items()}
    if all(detail[name] == "pass" for name in INTERRUPT_MODES):
        return "pass", detail
    if any(detail[name] == "not-run" for name in INTERRUPT_MODES):
        return "fail", detail
    if any(detail[name] == "blocked" for name in INTERRUPT_MODES) and not any(detail[name] == "fail" for name in INTERRUPT_MODES):
        return "blocked", detail
    return "fail", detail


def build_receipt(*, upgrade: dict, interrupt: dict, observed: dict, candidate: dict,
                  runtime_prior: str, run: str, commit: str,
                  compose_project: str = COMPOSE_PROJECT, host_port: int = HOST_PORT) -> dict:
    if compose_project != COMPOSE_PROJECT or "5" not in compose_project:
        raise GateError("compose project must be the ga-5 project")
    if host_port != HOST_PORT or "5" not in str(host_port):
        raise GateError("host port must be the ga-5 port")
    upgrade_row = _row(upgrade, "compose-upgrade-rollback")
    phases = upgrade_phases(upgrade_row)
    interrupt_result, interrupt_modes = interrupt_status(interrupt)
    phases = {**phases, "interrupt": interrupt_result}
    passed = all(value == "pass" for value in phases.values())
    why = str(upgrade_row.get("why") or "")
    if interrupt_result != "pass":
        reasons = []
        for name in INTERRUPT_MODES:
            row = _row(interrupt, name)
            if row.get("status") != "pass":
                reasons.append(f"{name}: {row.get('status') or 'not-run'} {row.get('why') or ''}".strip())
        why = (why + " " + "; ".join(reasons)).strip()
    return {
        "schema": SCHEMA,
        "topology": "docker-compose",
        "platform": "linux/amd64",
        "status": "pass" if passed else "fail",
        "why": why,
        "releaseTag": RC1_TAG,
        "digestPrefix": RC1_DIGEST_PREFIX,
        "prior": {
            "releaseImage": prior_image(),
            "indexDigest": RC1_INDEX_DIGEST,
            "amd64Digest": RC1_AMD64_DIGEST,
            "runtimeImage": runtime_prior,
            "serverSha": RC1_SERVER_SHA,
            "schema": RC1_SCHEMA,
        },
        "candidate": candidate,
        "composeProject": compose_project,
        "hostPort": host_port,
        "phases": phases,
        "observed": observed or {},
        "upgrade": upgrade_row,
        "interrupt": interrupt_modes,
        "workflow": {"run": run, "commit": commit},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    release = sub.add_parser("check-release")
    release.add_argument("--body-file", type=Path, required=True)
    candidate = sub.add_parser("check-candidate")
    candidate.add_argument("--image", required=True)
    candidate.add_argument("--sha", required=True)
    candidate.add_argument("--schema", type=int, required=True)
    candidate.add_argument("--ancestor", required=True, choices=("yes", "no"))
    child = sub.add_parser("amd64-child")
    child.add_argument("--index", type=Path, required=True)
    built = sub.add_parser("receipt")
    built.add_argument("--upgrade-matrix", type=Path, required=True)
    built.add_argument("--interrupt-matrix", type=Path, required=True)
    built.add_argument("--observed", type=Path)
    built.add_argument("--candidate-image", required=True)
    built.add_argument("--candidate-sha", required=True)
    built.add_argument("--candidate-schema", type=int, required=True)
    built.add_argument("--ancestor", required=True, choices=("yes", "no"))
    built.add_argument("--runtime-prior", required=True)
    built.add_argument("--run", default="")
    built.add_argument("--commit", default="")
    built.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "check-release":
            print(check_release(args.body_file.read_text(encoding="utf-8")))
            return 0
        if args.command == "check-candidate":
            print(json.dumps(check_candidate(args.image, args.sha, args.schema, args.ancestor)))
            return 0
        if args.command == "amd64-child":
            print(amd64_child(json.loads(args.index.read_text(encoding="utf-8"))))
            return 0
        candidate_info = check_candidate(args.candidate_image, args.candidate_sha, args.candidate_schema, args.ancestor)
        if not IMAGE_REF.fullmatch(args.runtime_prior):
            raise GateError("runtime prior must be repository@sha256:<64 lowercase hex digits>")
        receipt = build_receipt(
            upgrade=_load(args.upgrade_matrix, {}),
            interrupt=_load(args.interrupt_matrix, {}),
            observed=_load(args.observed, {}),
            candidate=candidate_info,
            runtime_prior=args.runtime_prior,
            run=args.run,
            commit=args.commit,
        )
    except (OSError, ValueError) as exc:
        print(f"compose-ga-rc1: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"compose-ga-rc1: {receipt['status']} {args.output}")
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
