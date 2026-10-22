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

Two pairs, both certifying what a customer receives:

  published-release  (headline) prior = the server of the newest published honua-* GitHub release,
                     pulled by the public GHCR digest its release assets name; the upgrade follows
                     the documented customer steps (helm upgrade, contract-migration nonce approval,
                     restore-from-backup at the documented rollback boundary).
  trunk-candidate    (scaffolding) prior = the previous honua-server pin in manifest history. It is
                     labelled scaffolding in its receipt and is never a customer release pair.

Neither has a signed prior lock yet, so `certifiedReleasePair: false` and the signed-lock
requirement stays blocked.
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
PUBLISHED_RELEASE = "published-release"
PRIOR_SOURCES = (PUBLISHED_RELEASE, PRIOR_SOURCE)
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


def select_release(releases) -> dict:
    """Newest published (non-draft) honua-* GitHub release; pre-releases count, and are labelled."""
    rows = [r for r in releases or [] if isinstance(r, dict) and not r.get("draft")
            and str(r.get("tag_name", "")).startswith("honua-") and r.get("published_at")]
    if not rows:
        raise PairError("no published honua-* release to upgrade from")
    newest = max(rows, key=lambda r: r["published_at"])
    return {"tag": newest["tag_name"], "url": newest.get("html_url", ""), "prerelease": bool(newest.get("prerelease"))}


def public_reference(manifest: dict) -> str:
    """The reference a customer copies from the release: tag@digest when the release names a tag."""
    server = ((manifest or {}).get("components") or {}).get(SERVER) or {}
    image, digest = str(server.get("image") or ""), str(server.get("digest") or "")
    if not DIGEST.fullmatch(digest):
        raise PairError("published release does not name an immutable server digest")
    return image.split("@", 1)[0] + "@" + digest


def resolve(candidate_manifest: dict, candidate_bytes: bytes, *, prior_manifest: dict | None = None,
            prior_bytes: bytes | None = None, prior_label: str = "", history=None,
            prior_source: str = PRIOR_SOURCE, release: dict | None = None) -> dict:
    if prior_source not in PRIOR_SOURCES:
        raise PairError(f"unknown prior source {prior_source!r}")
    if prior_source == PUBLISHED_RELEASE and (prior_manifest is None or not (release or {}).get("tag")):
        raise PairError("published-release pair needs the release's finalized manifest and tag")
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
    extra = {}
    if prior_source == PUBLISHED_RELEASE:
        extra = {"release_tag": release["tag"], "release_url": release.get("url", ""),
                 "release_prerelease": "true" if release.get("prerelease") else "false",
                 "prior_public_reference": public_reference(prior_manifest)}
    return {
        "ready": "1",
        "prior_source": prior_source,
        **extra,
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
    source = pair.get("prior_source") if pair.get("prior_source") in PRIOR_SOURCES else PRIOR_SOURCE
    if source == PUBLISHED_RELEASE:
        certification = {
            "role": "customer-upgrade-path",
            "customerReachable": True,
            "publicReference": pair.get("prior_public_reference"),
            "release": {"tag": pair.get("release_tag"), "url": pair.get("release_url"),
                        "prerelease": pair.get("release_prerelease") == "true"},
            "steps": "helm upgrade with the release's chart and values; contract-migration nonce approval when "
                     "the candidate gates it; restore the pre-upgrade backup at the documented rollback boundary",
        }
    else:
        certification = {"role": "scaffolding",
                         "note": "previous trunk manifest pin -> candidate; not a customer release pair"}
    return {
        "schema": SCHEMA,
        "priorSource": source,
        "certification": certification,
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
    r.add_argument("--prior-source", choices=PRIOR_SOURCES, default=PRIOR_SOURCE)
    r.add_argument("--release-tag", default="")
    r.add_argument("--release-url", default="")
    r.add_argument("--release-prerelease", choices=("true", "false"), default="false")
    sel = sub.add_parser("select-release", help="print tag/url/prerelease of the newest published honua-* release")
    sel.add_argument("--releases-json", type=Path, required=True)
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

    if args.command == "select-release":
        try:
            chosen = select_release(json.loads(args.releases_json.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print("release_ready=0")
            print("release_why=" + " ".join(str(exc).split()))
            return 0
        print("release_ready=1")
        print(f"release_tag={chosen['tag']}")
        print(f"release_url={chosen['url']}")
        print(f"release_prerelease={'true' if chosen['prerelease'] else 'false'}")
        return 0

    if args.command == "resolve":
        try:
            candidate_bytes = args.candidate_manifest.read_bytes()
            prior_manifest = prior_bytes = None
            if args.prior_manifest:
                if not args.prior_manifest.is_file():
                    raise PairError(f"{args.prior_label or 'prior manifest'} is unavailable")
                prior_bytes = args.prior_manifest.read_bytes()
                prior_manifest = yaml.safe_load(prior_bytes)
            release = {"tag": args.release_tag, "url": args.release_url,
                       "prerelease": args.release_prerelease == "true"} if args.release_tag else None
            pair = resolve(yaml.safe_load(candidate_bytes), candidate_bytes, prior_manifest=prior_manifest,
                           prior_bytes=prior_bytes, prior_label=args.prior_label,
                           prior_source=args.prior_source, release=release)
        except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
            print("ready=0")
            print("why=" + " ".join(f"{args.prior_source} pair unavailable: {exc}".split()))
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
    print(f"{receipt['priorSource']} receipt: {receipt['status']} ({args.output})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
