#!/usr/bin/env python3
"""Resolve an attested retained rollback target without inventing a historical lock."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


class Finding(ValueError):
    pass


def promoted_tag(candidate_tag: str) -> str:
    """Return the GA tag that promote.yml derives from a candidate tag."""
    return candidate_tag.split("-rc", 1)[0]


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
    # --paginate avoids declaring a first release because the only lock is on page 2.
    pages = json.loads(command("api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100"))
    releases = [
        release for page in pages for release in page
        if not release["draft"] and release["tag_name"].startswith("honua-")
    ]
    candidate_release = next((release for release in releases if release["tag_name"] == ga_tag), None)
    candidate_published_at = (candidate_release or {}).get("published_at")
    candidate_version = release_version(ga_tag)
    releases = sorted(
        (release for release in releases
         if release["tag_name"] not in {candidate_tag, ga_tag}
         and (
             (candidate_published_at and release.get("published_at", "") < candidate_published_at)
             or (
                 not candidate_published_at
                 and candidate_version is not None
                 and (version := release_version(release["tag_name"])) is not None
                 and version < candidate_version
             )
         )),
        key=lambda release: release["published_at"], reverse=True,
    )
    scanned = []
    target.parent.mkdir(parents=True, exist_ok=True)
    for release in releases:
        tag = release["tag_name"]
        scanned.append(tag)
        if not any(asset["name"] == "platform-lock.json" for asset in release["assets"]):
            continue
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
