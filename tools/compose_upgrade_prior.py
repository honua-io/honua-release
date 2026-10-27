#!/usr/bin/env python3
"""Prior/candidate pair and receipt for the Docker Compose upgrade lane (gate-upgrade.yml `compose-upgrade`).

Docker Compose on-prem is a GA topology for 2026.1 (founder re-tier 2026-10-25,
https://github.com/honua-io/honua-release/issues/376#issuecomment-6407009748), so its upgrade and
rollback path is the REQUIRED runtime fragment of the upgrade gate (founder decision D9, 2026-10-26,
honua-release#950). Helm on kind is Preview evidence.

2026.1 is the first release, so "prior" has no promoted release to come from. It is defined here, in
this order, and nowhere else:

  1. published-rc   The newest published (non-draft) GitHub release of this repository tagged
                    `2026.1-rc.N` / `honua-2026.1-rc.N` (tools/rc_release_notes.py, rc-release.yml).
                    Prior = the honua-server image digest those release notes publish
                    (`... @ \\`sha256:<64 hex>\\``). A release whose server row has no immutable digest
                    fails closed; it is never skipped silently.
  2. nightly-lock   Else the newest signed nightly lock, refs/tags/nightly-lock/2026.1-rc.N
                    (tools/mint_nightly_lock.py): prior = that lock's honua-server amd64 image binding.
                    The workflow verifies the lock's keyless signature before it uses the image.
  3. certified-nightly
                    Else (ruling compose-upgrade-prior-nightly-2026-10-27, founder decision 2026-10-27)
                    the newest published, certified nightly honua-server image OLDER than the candidate:
                    walking honua-server trunk from the candidate's own commit towards older commits, the
                    first `nightly-<sha7>` image whose commit has a green full-matrix ci.yml under the
                    trunk resolver's own evaluator (tools/resolve_trunk_candidate.py GitHub.green), bound by
                    the registry index, its linux/amd64 child digest and the config revision label
                    (Registry.identity). Example: nightly-08ace24. Once rc.1 is published, source 1 wins
                    again and this source is never consulted.

A candidate of either kind whose server image IS the candidate's (index digest or amd64 child) is
skipped: it is the same image, not a prior. When neither source yields a different immutable image,
the pair is unavailable (ready=0) and the fragment is `blocked` (strict promotes blocked to fail). The
lane never falls back to the candidate, a floating tag, or a manifest-history pin as its prior. A nightly
whose commit is the candidate's, or whose image is the candidate's, is refused, not selected; if the
candidate's commit is not on the walked trunk window, "older" is unprovable and the pair is unavailable.

Architecture is x86_64 only (linux/amd64); multi-tenant scenarios are out of scope (ADR-0024).

  resolve   print GITHUB_OUTPUT lines (ready=1, prior=..., candidate=..., ...) or ready=0 + why
  receipt   assemble the compose upgrade receipt from the harness observations
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from upgrade_trunk_pair import DIGEST, PairError, _bytes_digest, server_identity

SCHEMA = "honua.upgrade-compose-receipt/v1"
PUBLISHED_RC = "published-rc"
NIGHTLY_LOCK = "nightly-lock"
CERTIFIED_NIGHTLY = "certified-nightly"
PRIOR_SOURCES = (PUBLISHED_RC, NIGHTLY_LOCK, CERTIFIED_NIGHTLY)
NIGHTLY_PRIOR_RULING = "compose-upgrade-prior-nightly-2026-10-27"
NIGHTLY_TAG = re.compile(r"nightly-([0-9a-f]{7})\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ARCHITECTURE = "amd64"
PLATFORM = "linux/amd64"
D9_RULING = "https://github.com/honua-io/honua-release/issues/950"
RETIER_RULING = "https://github.com/honua-io/honua-release/issues/376#issuecomment-6407009748"
RC_TAG = re.compile(r"(?:honua-)?(\d{4})\.(\d+)-rc\.([1-9][0-9]*)\Z")
LOCK_LABEL = re.compile(r"(?:honua-)?(\d{4})\.(\d+)-rc\.([1-9][0-9]*)\Z")
# The honua-server row rc_release_notes.component_rows renders:
#   | honua-server | pre-release | `see image tag` | ghcr.io/honua-io/honua-server:<tag> @ `sha256:<hex>` |
RC_SERVER_ROW = re.compile(
    r"^\|\s*honua-server\s*\|[^\n]*?\|\s*(?P<image>[^\s|`@]+)\s*@\s*`(?P<digest>sha256:[0-9a-f]{64})`\s*\|\s*$",
    re.MULTILINE)
SCENARIOS = ("normal", "negative-control")


def _untagged(image: str) -> str:
    """Repository part of an image reference (drops a tag, keeps a registry port)."""
    named = image.split("@", 1)[0]
    last = named.rsplit("/", 1)[-1]
    return named[: named.rfind(":")] if ":" in last else named


def candidate_identity(manifest: dict) -> dict:
    """The candidate server: amd64 child reference plus every digest that names the same image."""
    identity = server_identity(manifest, "candidate manifest")
    server = manifest["components"]["honua-server"]
    same = {identity["reference"].rsplit("@", 1)[1], str(server.get("digest") or "")}
    same |= {str(v) for v in (server.get("platformDigests") or {}).values()}
    identity["sameImageDigests"] = sorted(d for d in same if DIGEST.fullmatch(d))
    return identity


def rc_releases(releases) -> list[dict]:
    """Published (non-draft) rc releases, newest first by published_at."""
    rows = [r for r in releases or [] if isinstance(r, dict) and not r.get("draft") and r.get("published_at")
            and RC_TAG.fullmatch(str(r.get("tag_name", "")))]
    return sorted(rows, key=lambda r: r["published_at"], reverse=True)


def rc_server_reference(release: dict) -> str:
    """repository@sha256 the rc's release notes publish for honua-server; fails closed otherwise."""
    tag = release.get("tag_name")
    matches = RC_SERVER_ROW.findall(str(release.get("body") or ""))
    if len(matches) != 1:
        raise PairError(f"published rc {tag} does not name exactly one honua-server image digest in its notes")
    image, digest = matches[0]
    repository = _untagged(image)
    if not repository or not DIGEST.fullmatch(digest):
        raise PairError(f"published rc {tag} names no immutable honua-server digest")
    return f"{repository}@{digest}"


def lock_order(label: str) -> tuple[int, int, int]:
    match = LOCK_LABEL.fullmatch(label)
    if not match:
        raise PairError(f"nightly lock label {label!r} is not 2026.N-rc.N")
    return tuple(int(part) for part in match.groups())


def lock_server_reference(label: str, lock: dict) -> tuple[str, str]:
    """(amd64 reference, source revision) of a nightly lock's honua-server image binding."""
    # Imported lazily: the binding helpers need jsonschema, which the resolve path does not.
    from upgrade_lock_binding import BindingError, image_binding
    try:
        binding = image_binding(lock, ARCHITECTURE)
    except (BindingError, KeyError, TypeError) as exc:
        raise PairError(f"nightly lock {label}: {exc}") from exc
    return binding["reference"], str(binding.get("sourceRevision") or "")


def resolve(candidate_manifest: dict, candidate_bytes: bytes, *, releases=None, nightly_locks=None,
            certified_nightlies=None) -> dict:
    """Select the prior per the module docstring. `nightly_locks`: iterable of (label, lock dict, signed).

    `certified_nightlies`: a callable taking the candidate identity and returning an iterable of
    observations, newest first and strictly older than the candidate on trunk, each
    {"tag", "sha", "reference" (repository@sha256 amd64 child), "certified" (bool), "why"}. It is called
    only when no rc and no lock supplies a prior, so the GitHub walk never runs once rc.1 exists.
    """
    candidate = candidate_identity(candidate_manifest)
    same = set(candidate["sameImageDigests"])
    skipped: list[str] = []
    for release in rc_releases(releases):
        reference = rc_server_reference(release)
        if reference.rsplit("@", 1)[1] in same:
            skipped.append(f"{release['tag_name']} (same image as the candidate)")
            continue
        return _pair(candidate, candidate_bytes, source=PUBLISHED_RC, prior=reference, source_revision="",
                     label=release["tag_name"], url=str(release.get("html_url") or ""), skipped=skipped,
                     identity=_bytes_digest(json.dumps({"tag": release["tag_name"], "prior": reference},
                                                       sort_keys=True).encode()))
    for label, lock, signed in sorted(nightly_locks or [], key=lambda item: lock_order(item[0]), reverse=True):
        if not signed:
            raise PairError(f"nightly lock {label} has no signature bundle (platform-lock.sigstore.json)")
        reference, revision = lock_server_reference(label, lock)
        if reference.rsplit("@", 1)[1] in same or (revision and revision == candidate["sourceRevision"]):
            skipped.append(f"nightly-lock/{label} (same image as the candidate)")
            continue
        return _pair(candidate, candidate_bytes, source=NIGHTLY_LOCK, prior=reference, source_revision=revision,
                     label=f"nightly-lock/{label}", url="", skipped=skipped,
                     identity=_bytes_digest(json.dumps(lock, sort_keys=True, separators=(",", ":")).encode()))
    for nightly in (certified_nightlies(candidate) if certified_nightlies else []):
        tag, sha, reference = str(nightly.get("tag") or ""), str(nightly.get("sha") or ""), str(nightly.get("reference") or "")
        match = NIGHTLY_TAG.fullmatch(tag)
        digest = reference.rsplit("@", 1)[1] if "@" in reference else ""
        if not match or not SHA.fullmatch(sha) or match.group(1) != sha[:7]:
            raise PairError(f"nightly observation {tag or '(no tag)'} is not a nightly-<sha7> tag of its commit")
        if nightly.get("certified") is True and not DIGEST.fullmatch(digest):
            raise PairError(f"certified nightly {tag} is not bound by an immutable amd64 digest")
        if sha == candidate["sourceRevision"] or digest in same:
            skipped.append(f"{tag} (refused: the candidate itself)")
            continue
        if nightly.get("certified") is not True:
            skipped.append(f"{tag} (not certified: {' '.join(str(nightly.get('why') or 'no green full-matrix ci.yml').split())})")
            continue
        return _pair(candidate, candidate_bytes, source=CERTIFIED_NIGHTLY, prior=reference, source_revision=sha,
                     label=f"{tag} ({NIGHTLY_PRIOR_RULING})", url="", skipped=skipped,
                     identity=_bytes_digest(json.dumps({"tag": tag, "sha": sha, "prior": reference},
                                                       sort_keys=True).encode()))
    detail = f"; skipped: {', '.join(skipped)}" if skipped else ""
    raise PairError("no published 2026.1 rc release, no signed nightly lock and no certified nightly image older "
                    "than the candidate names a server image other than the candidate's (prior = latest "
                    "published rc image, else the newest signed nightly lock, else the newest certified "
                    f"nightly-<sha7> image older than the candidate; never the candidate itself){detail}")


def certified_nightlies_from_github(limit: int = 200):
    """Observation source for resolve(): honua-server trunk, older than the candidate, newest first.

    Reuses tools/resolve_trunk_candidate.py (the nightly's own candidate resolver) for the trunk walk,
    the green full-matrix ci.yml decision and the digest-bound registry identity, so a nightly is
    "certified" here exactly when it would have qualified as a nightly candidate.
    """
    def observe(candidate: dict):
        from resolve_trunk_candidate import GitHub, Registry, ResolutionError
        server_sha = candidate["sourceRevision"]
        repository, image_repository = "honua-io/honua-server", "honua-io/honua-server"
        github = GitHub()
        registry = Registry(github)
        older = False
        for sha in github.commits(repository, limit):
            if sha == server_sha:
                older = True
                continue
            if not older:
                continue        # newer than the candidate: never a prior
            tag = f"nightly-{sha[:7]}"
            if tag not in registry.tags(image_repository):
                continue        # no published nightly image for this commit
            green, why = github.green("honua-server", repository, sha)
            if not green:
                yield {"tag": tag, "sha": sha, "certified": False, "why": why}
                continue
            try:
                identity = registry.identity(image_repository, tag, sha, [ARCHITECTURE])
            except (OSError, ValueError, subprocess.CalledProcessError, ResolutionError) as exc:
                yield {"tag": tag, "sha": sha, "certified": False, "why": f"image not bound by digest: {exc}"}
                continue
            yield {"tag": tag, "sha": sha, "certified": True, "why": "",
                   "reference": f"ghcr.io/{image_repository}@{identity['platformDigests'][ARCHITECTURE]}"}
        if not older:
            raise PairError(f"candidate honua-server {server_sha} is not within the newest {limit} trunk commits; "
                            "which nightlies are older than it cannot be established")
    return observe


def _pair(candidate: dict, candidate_bytes: bytes, *, source: str, prior: str, source_revision: str,
          label: str, url: str, skipped: list[str], identity: str) -> dict:
    if prior.rsplit("@", 1)[1] in candidate["sameImageDigests"]:  # defence in depth; resolve() skips these
        raise PairError("prior resolves to the candidate image")
    return {
        "ready": "1",
        "prior_source": source,
        "prior": prior,
        "prior_source_revision": source_revision,
        "prior_selected_from": label,
        "prior_release_url": url,
        "prior_identity_digest": identity,
        "skipped": "; ".join(skipped),
        "candidate": candidate["reference"],
        "candidate_source_revision": candidate["sourceRevision"],
        "candidate_schema": str(candidate["dbSchema"]),
        "candidate_manifest_digest": _bytes_digest(candidate_bytes),
        "platform": PLATFORM,
    }


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def nightly_locks_from_git(remote: str = "origin"):
    """(label, lock, signed) for every refs/tags/nightly-lock/* on `remote` (lock bundle at <label>/)."""
    locks = []
    for line in _git("ls-remote", remote, "refs/tags/nightly-lock/*").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}") or not ref.startswith("refs/tags/nightly-lock/"):
            continue
        label = ref.removeprefix("refs/tags/nightly-lock/")
        _git("fetch", "--no-tags", "--depth=1", remote, f"{ref}:refs/compose-upgrade/nightly-lock/{label}")
        raw = _git("show", f"{sha}:{label}/platform-lock.json")
        signed = subprocess.run(["git", "cat-file", "-e", f"{sha}:{label}/platform-lock.sigstore.json"],
                                capture_output=True).returncode == 0
        locks.append((label, json.loads(raw), signed))
    return locks


def _load_json(path: Path | None, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path else default
    except (OSError, ValueError):
        return default


def build_receipt(pair: dict, *, scenario: str, status: str, why: str, observed: dict, phases: dict,
                  harness: dict, run: str, commit: str) -> dict:
    """Same shape as the kind pair receipt (tools/upgrade_trunk_pair.build_receipt), for Compose."""
    if status not in {"pass", "fail", "blocked"}:
        status = "fail"
    source = pair.get("prior_source") if pair.get("prior_source") in PRIOR_SOURCES else None
    return {
        "schema": SCHEMA,
        "topology": "docker-compose",
        "platform": PLATFORM,
        "priorSource": source,
        "certification": {
            "role": "ga-compose-upgrade-path",
            "verdict": "required",
            "ruling": D9_RULING,
            "tierRuling": RETIER_RULING,
            "priorDefinition": "latest published 2026.1 rc image, else the previous signed nightly lock's "
                               "server; never the candidate itself",
            "note": "negative-control cells inject a defect and pass only when the gate's own assertion fails",
        },
        "certifiedReleasePair": False,
        "scenario": scenario,
        "status": status,
        "why": why,
        "prior": {"image": pair.get("prior"), "sourceRevision": pair.get("prior_source_revision"),
                  "selectedFrom": pair.get("prior_selected_from"), "releaseUrl": pair.get("prior_release_url"),
                  "identityDigest": pair.get("prior_identity_digest"), "skipped": pair.get("skipped")},
        "candidate": {"image": pair.get("candidate"), "sourceRevision": pair.get("candidate_source_revision"),
                      "declaredSchema": pair.get("candidate_schema"),
                      "manifestDigest": pair.get("candidate_manifest_digest")},
        "compose": {"file": "e2e/harness/compose.candidate.yml", "harness": "e2e/harness/upgrade-chaos.sh",
                    "harnessScenario": "compose-upgrade-rollback", "result": harness},
        "observed": observed,
        "phases": phases,
        "workflow": {"run": run, "commit": commit},
    }


def classify(scenario: str, exit_code: int, harness: dict, expect_why: str = "") -> tuple[str, str]:
    """Fragment status from the harness exit code and its own recorded verdict.

    normal            pass only on exit 0 AND a recorded compose-upgrade-rollback pass.
    negative-control  the injected defect must be caught by the gate's own assertion: pass only on a
                      non-zero exit AND a recorded fail whose reason contains `expect_why`. A crash
                      (no recorded verdict) or a pass is a FAIL: the gate could not prove it can fail.
    """
    rows = [s for s in (harness or {}).get("scenarios", []) if s.get("scenario") == "compose-upgrade-rollback"]
    row = rows[-1] if rows else {}
    recorded, reason = row.get("status"), str(row.get("why") or "")
    if scenario == "normal":
        if exit_code == 0 and recorded == "pass":
            return "pass", reason
        if recorded == "blocked":
            return "blocked", reason
        return "fail", reason or f"harness exited {exit_code} without a recorded verdict"
    if scenario == "negative-control":
        if exit_code != 0 and recorded == "fail" and expect_why and expect_why in reason:
            return "pass", f"injected defect caught by the gate's own assertion: {reason}"
        return "fail", (f"negative control not caught as expected (exit {exit_code}, recorded {recorded or 'nothing'}"
                        f", expected reason containing {expect_why!r}): {reason}")
    return "fail", f"unknown compose upgrade scenario {scenario!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--candidate-manifest", type=Path, default=Path("platform-manifest.yaml"))
    r.add_argument("--releases-json", type=Path, required=True,
                   help="`gh api --paginate --slurp repos/<repo>/releases` output (list of pages or a list)")
    r.add_argument("--nightly-locks-from-git", action="store_true",
                   help="read refs/tags/nightly-lock/* from the origin remote")
    r.add_argument("--certified-nightly-from-github", action="store_true",
                   help=f"fallback ({NIGHTLY_PRIOR_RULING}): newest certified nightly-<sha7> older than the candidate")
    c = sub.add_parser("classify")
    c.add_argument("--scenario", required=True, choices=SCENARIOS)
    c.add_argument("--exit-code", type=int, required=True)
    c.add_argument("--harness", type=Path, required=True)
    c.add_argument("--expect-why", default="")
    b = sub.add_parser("receipt")
    b.add_argument("--pair", type=Path, required=True, help="JSON of the resolve outputs")
    b.add_argument("--scenario", required=True)
    b.add_argument("--status", required=True)
    b.add_argument("--why", default="")
    b.add_argument("--observed", type=Path)
    b.add_argument("--harness", type=Path)
    b.add_argument("--phase-dir", type=Path, required=True)
    b.add_argument("--run", default="")
    b.add_argument("--commit", default="")
    b.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "resolve":
        try:
            candidate_bytes = args.candidate_manifest.read_bytes()
            pages = json.loads(args.releases_json.read_text(encoding="utf-8"))
            releases = [r for page in pages for r in (page if isinstance(page, list) else [page])]
            locks = nightly_locks_from_git() if args.nightly_locks_from_git else []
            nightlies = certified_nightlies_from_github() if args.certified_nightly_from_github else None
            pair = resolve(yaml.safe_load(candidate_bytes), candidate_bytes, releases=releases, nightly_locks=locks,
                           certified_nightlies=nightlies)
        except (OSError, ValueError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
            print("ready=0")
            print("why=" + " ".join(f"compose prior unavailable: {exc}".split()))
            return 0
        for key, value in pair.items():
            print(f"{key}={' '.join(str(value).split())}")
        return 0

    if args.command == "classify":
        status, why = classify(args.scenario, args.exit_code, _load_json(args.harness, {}), args.expect_why)
        print(f"status={status}")
        print("why=" + " ".join(why.split()))
        return 0

    phases = {phase: _load_json(args.phase_dir / f"compose-{phase}-identity.json", None)
              for phase in ("install", "candidate", "rollback")}
    receipt = build_receipt(_load_json(args.pair, {}), scenario=args.scenario, status=args.status, why=args.why,
                            observed=_load_json(args.observed, {}),
                            phases={k: v for k, v in phases.items() if v is not None},
                            harness=_load_json(args.harness, {}), run=args.run, commit=args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"compose upgrade receipt: {receipt['status']} ({args.output})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
