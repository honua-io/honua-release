#!/usr/bin/env python3
"""Resolve an attested retained rollback target without inventing a historical lock."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from check_upgrade import ReleaseLookupError, list_releases, prior_platform_release
from tag_signing import publication_tag


class Finding(ValueError):
    pass


def promoted_tag(candidate_tag: str) -> str:
    """Return the GA tag that promote.yml derives from a candidate tag."""
    return publication_tag(candidate_tag.removeprefix("honua-"))


def release_version(tag: str) -> tuple[int, int, int] | None:
    """Parse a Honua release tag for the unpublished-candidate fallback boundary."""
    match = re.fullmatch(r"honua-(\d+)\.(\d+)(?:\.(\d+))?(?:-rc\.\d+)?", tag)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise Finding(f"ROLLBACK_RETAINED_LOOKUP_FAILED: {result.stderr or result.stdout}")
    return result.stdout


def resolve(candidate: Path, target: Path, repository: str, command=gh) -> dict:
    candidate_digest = digest(candidate)
    candidate_tag = json.loads(candidate.read_text())["platform"]["id"]
    ga_tag = promoted_tag(candidate_tag)
    try:
        # --paginate avoids declaring a first release because the only lock is on page 2.
        all_releases = list_releases(repository, command)
    except ReleaseLookupError as exc:
        raise Finding(f"ROLLBACK_RETAINED_LOOKUP_FAILED: {exc}") from exc
    releases = [
        release for release in all_releases
        if not release["draft"] and release["tag_name"].startswith("honua-")
    ]
    candidate_release = next((release for release in releases if release["tag_name"] == ga_tag), None)
    candidate_published_at = (candidate_release or {}).get("published_at")
    candidate_version = release_version(ga_tag)

    def earlier(release: dict) -> bool:
        return release["tag_name"] not in {candidate_tag, ga_tag} and bool(
            (candidate_published_at and release.get("published_at", "") < candidate_published_at)
            or (
                not candidate_published_at
                and candidate_version is not None
                and (version := release_version(release["tag_name"])) is not None
                and version < candidate_version
            ))

    # The rollback target is the same "prior platform release" the upgrade gate certifies against
    # (R26): pre-release snapshots and releases without a signed lock are scanned but never retained.
    retained = prior_platform_release(releases, earlier)
    scanned = []
    for release in sorted(filter(earlier, releases), key=lambda release: release["published_at"], reverse=True):
        scanned.append(release["tag_name"])
        if release is retained:
            break
    target.parent.mkdir(parents=True, exist_ok=True)
    if retained is not None:
        tag = retained["tag_name"]
        command("release", "download", tag, "--repo", repository, "--pattern", "platform-lock.json",
                "--dir", str(target.parent))
        try:
            command("attestation", "verify", str(target), "--repo", repository)
        except Finding as exc:
            # An untrusted or unavailable historical asset is a finding, never permission
            # to downgrade to self-rollback or to reconstruct a lock from its manifest.
            raise Finding(f"ROLLBACK_RETAINED_ATTESTATION_FAILED: {tag}: {exc}") from exc
        return {
            "first_lock_bearing_release": False, "no_earlier_lock_exists": False,
            "candidate_lock_digest": candidate_digest, "rollback_target_digest": digest(target),
            "retained_release": tag, "scanned_releases": scanned,
            "reason": "verified_retained_release_lock",
        }
    target.write_bytes(candidate.read_bytes())
    return {
        "first_lock_bearing_release": True, "no_earlier_lock_exists": True,
        "candidate_lock_digest": candidate_digest, "rollback_target_digest": candidate_digest,
        "retained_release": None, "scanned_releases": scanned,
        "reason": "no_earlier_attested_platform_lock_exists",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report = {"schema": "honua.rollback-gate/v1", "overall_status": "fail"}
    try:
        report.update(resolve(args.candidate, args.target, args.repository))
        report["overall_status"] = "pending"
    except (Finding, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        report["finding"] = f"ROLLBACK_TARGET_RESOLUTION_FAILED: {exc}"
        print(report["finding"])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["overall_status"] == "pending" else 1


if __name__ == "__main__":
    raise SystemExit(main())
