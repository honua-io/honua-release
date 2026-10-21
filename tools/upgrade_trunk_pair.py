#!/usr/bin/env python3
"""Trunk-candidate fallback pair for the kind upgrade lane (gate-upgrade.yml).

The kind-upgrade lane certifies an upgrade between two SIGNED platform locks: the latest promoted
honua-* release (R26) and the candidate. Until the first promotion there is no signed prior lock, so
that lane self-blocked on every run and the install -> seed -> upgrade -> verify -> rollback ->
prior-still-serves sequence never executed at all.

This module picks a truthful substitute pair so the sequence runs anyway:

  prior      the previous honua-server pin in platform-manifest.yaml's own trunk history
             (`previous-pin`, the default), or the honua-2026.1 pre-release snapshot's server
             (`honua-2026.1`). Neither is a promoted platform release.
  candidate  the honua-server pin of the checked-out (or candidate_ref) platform-manifest.yaml.

Everything this produces is labelled `prior-source: trunk-candidate`, `certifiedReleasePair: false`.
It never satisfies the signed-lock requirement, which the workflow keeps reporting as blocked; it
only adds a separately named fragment that fails when the real lifecycle fails.

  resolve   print GITHUB_OUTPUT lines (ready=1, prior=..., candidate=..., ...) or ready=0 + why
  receipt   assemble the trunk-candidate receipt from the lane's observations
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

SCHEMA = "honua.upgrade-trunk-candidate-receipt/v1"
PRIOR_SOURCE = "trunk-candidate"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SERVER = "honua-server"
HELM = "honua-helm"


class PairError(ValueError):
    """No usable trunk-candidate pair; the lane stays blocked and says why."""


def _bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def server_identity(manifest: dict, where: str) -> dict:
    """The exact, digest-pinned honua-server image a manifest declares (amd64 child when present)."""
    server = ((manifest or {}).get("components") or {}).get(SERVER)
    if not isinstance(server, dict):
        raise PairError(f"{where}: no components.{SERVER}")
    sha = str(server.get("sha") or "")
    if not SHA.fullmatch(sha):
        raise PairError(f"{where}: {SERVER}.sha is not a full commit sha")
    image = str(server.get("image") or "")
    repository = image.split("@", 1)[0]
    # Drop a tag from the repository part ("ghcr.io/x/y:tag"), keeping a registry port intact.
    last = repository.rsplit("/", 1)
    if ":" in last[-1]:
        repository = repository[: repository.rfind(":")]
    digest = str(((server.get("platformDigests") or {}).get("amd64")) or server.get("digest") or "")
    if not repository or not DIGEST.fullmatch(digest):
        raise PairError(f"{where}: {SERVER} has no immutable image digest")
    schema = str(server.get("dbSchema") or "")
    if not schema.isdigit():
        raise PairError(f"{where}: {SERVER}.dbSchema is not a migration number")
    return {"reference": f"{repository}@{digest}", "sourceRevision": sha, "dbSchema": int(schema)}


def helm_sha(manifest: dict) -> str:
    sha = str((((manifest or {}).get("components") or {}).get(HELM) or {}).get("sha") or "")
    if not SHA.fullmatch(sha):
        raise PairError(f"candidate manifest: components.{HELM}.sha is not a full commit sha")
    return sha


def previous_pin(candidate: dict, history) -> tuple[dict, str, dict]:
    """Newest manifest in `history` (newest first: (commit, manifest-dict)) whose server differs."""
    for commit, manifest in history:
        try:
            identity = server_identity(manifest, f"platform-manifest.yaml@{commit[:12]}")
        except PairError:
            continue
        if identity["sourceRevision"] != candidate["sourceRevision"]:
            return identity, f"platform-manifest.yaml@{commit} (previous honua-server pin on trunk)", manifest
    raise PairError("no earlier honua-server pin in platform-manifest.yaml history")


def git_history(path: str = "platform-manifest.yaml", limit: int = 200):
    commits = subprocess.run(["git", "log", f"-n{limit}", "--format=%H", "--", path],
                             capture_output=True, text=True, check=True).stdout.split()
    for commit in commits:
        shown = subprocess.run(["git", "show", f"{commit}:{path}"], capture_output=True, text=True)
        if shown.returncode != 0:
            continue
        try:
            yield commit, yaml.safe_load(shown.stdout)
        except yaml.YAMLError:
            continue


def resolve(candidate_manifest: dict, candidate_bytes: bytes, *, prior_manifest: dict | None = None,
            prior_bytes: bytes | None = None, prior_label: str = "", history=None) -> dict:
    candidate = server_identity(candidate_manifest, "candidate manifest")
    if prior_manifest is not None:
        prior = server_identity(prior_manifest, prior_label or "prior manifest")
        selected = prior_label or "prior manifest"
        prior_digest = _bytes_digest(prior_bytes or b"")
    else:
        prior, selected, prior_manifest = previous_pin(candidate, history if history is not None else git_history())
        prior_digest = _bytes_digest(json.dumps(prior, sort_keys=True).encode())
    candidate_helm = helm_sha(candidate_manifest)
    try:
        # Install the prior server with the chart its own manifest pinned, as an operator of that
        # release would have; the upgrade then moves chart and image together, like the signed path.
        prior_helm = helm_sha(prior_manifest)
    except PairError:
        prior_helm = candidate_helm
    if prior["dbSchema"] > candidate["dbSchema"]:
        raise PairError(f"prior schema {prior['dbSchema']} is newer than candidate schema {candidate['dbSchema']}")
    cross = prior["reference"] != candidate["reference"] and prior["sourceRevision"] != candidate["sourceRevision"]
    return {
        "ready": "1",
        "prior_source": PRIOR_SOURCE,
        "prior": prior["reference"],
        "prior_source_revision": prior["sourceRevision"],
        "prior_schema": str(prior["dbSchema"]),
        "prior_selected_from": selected,
        "candidate": candidate["reference"],
        "candidate_source_revision": candidate["sourceRevision"],
        "candidate_schema": str(candidate["dbSchema"]),
        "helm_sha": candidate_helm,
        "prior_helm_sha": prior_helm,
        # Not lock digests: these bind the deployment env (HONUA_PLATFORM_LOCK_DIGEST) to the exact
        # manifest identities this run used, and are labelled as such in the receipt.
        "prior_manifest_digest": prior_digest,
        "candidate_manifest_digest": _bytes_digest(candidate_bytes),
        "cross": "yes" if cross else "no",
    }


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def build_receipt(pair: dict, *, scenario: str, status: str, why: str, signed_lock_why: str,
                  observed: dict, phases: dict, run: str, commit: str) -> dict:
    if status not in {"pass", "fail", "blocked"}:
        status = "fail"
    return {
        "schema": SCHEMA,
        "priorSource": PRIOR_SOURCE,
        "certifiedReleasePair": False,
        "signedLockRequirement": {"status": "blocked", "why": signed_lock_why},
        "scenario": scenario,
        "status": status,
        "why": why,
        "prior": {"image": pair.get("prior"), "sourceRevision": pair.get("prior_source_revision"),
                  "declaredSchema": pair.get("prior_schema"), "selectedFrom": pair.get("prior_selected_from"),
                  "identityDigest": pair.get("prior_manifest_digest")},
        "candidate": {"image": pair.get("candidate"), "sourceRevision": pair.get("candidate_source_revision"),
                      "declaredSchema": pair.get("candidate_schema"),
                      "manifestDigest": pair.get("candidate_manifest_digest")},
        "chart": {"repository": "honua-io/honua-helm", "install": pair.get("prior_helm_sha"),
                  "upgrade": pair.get("helm_sha"),
                  "note": "manifest-pinned chart sources: install with the prior's, upgrade with the candidate's"},
        "observed": observed,
        "phases": phases,
        "workflow": {"run": run, "commit": commit},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--candidate-manifest", type=Path, default=Path("platform-manifest.yaml"))
    r.add_argument("--prior-manifest", type=Path)
    r.add_argument("--prior-label", default="")
    b = sub.add_parser("receipt")
    b.add_argument("--pair", type=Path, required=True, help="JSON of the resolve outputs")
    b.add_argument("--scenario", required=True)
    b.add_argument("--status", required=True)
    b.add_argument("--why", default="")
    b.add_argument("--signed-lock-why", required=True)
    b.add_argument("--observed", type=Path)
    b.add_argument("--phase-dir", type=Path, default=Path("/tmp"))
    b.add_argument("--run", default="")
    b.add_argument("--commit", default="")
    b.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "resolve":
        try:
            candidate_bytes = args.candidate_manifest.read_bytes()
            prior_manifest = prior_bytes = None
            if args.prior_manifest:
                if not args.prior_manifest.is_file():
                    raise PairError(f"{args.prior_label or 'prior manifest'} is unavailable")
                prior_bytes = args.prior_manifest.read_bytes()
                prior_manifest = yaml.safe_load(prior_bytes)
            pair = resolve(yaml.safe_load(candidate_bytes), candidate_bytes, prior_manifest=prior_manifest,
                           prior_bytes=prior_bytes, prior_label=args.prior_label)
        except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
            print("ready=0")
            print("why=" + " ".join(f"trunk-candidate pair unavailable: {exc}".split()))
            return 0
        for key, value in pair.items():
            print(f"{key}={value}")
        return 0

    pair = _load_json(args.pair, {})
    observed = _load_json(args.observed, {}) if args.observed else {}
    phases = {phase: _load_json(args.phase_dir / f"{phase}-identity.json", None)
              for phase in ("install", "candidate", "rollback")}
    receipt = build_receipt(pair, scenario=args.scenario, status=args.status, why=args.why,
                            signed_lock_why=args.signed_lock_why, observed=observed,
                            phases={k: v for k, v in phases.items() if v is not None},
                            run=args.run, commit=args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"trunk-candidate receipt: {receipt['status']} ({args.output})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
