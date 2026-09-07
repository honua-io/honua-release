#!/usr/bin/env python3
"""Enumerate the server publisher's complete release history; never infer a missing floor.

#233 needs, for every capability an SDK requires, the earliest server version that
implements it. `honua-io/honua-server` has never published a version: no git tags and no
GitHub releases exist. That is not a gap in the publisher's diligence, it is the shape of a
first release — there is no earlier server to name, so the only correct introduction floor
for every capability in the first release is that release itself.

Asserting "no prior publication" needs evidence, so this tool records the *complete*
enumeration of the publisher's tag and release namespaces at an observed default-branch SHA
and refuses to accept a receipt that reports even one. A single tag or release means some
capability may predate the candidate, and per-capability introduction evidence is required
instead. The receipt is read-only input; it never certifies a pairing and never invents a
number.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any

SCHEMA = "honua.server-publication-history/v1"
ISSUE = "honua-io/honua-release#233"
REPOSITORY = "honua-io/honua-server"
# Every namespace a released server identity could occupy. All three must be enumerated:
# a release can exist without a git tag object, and a tag can exist without a release.
ENDPOINTS = ("tags", "releases", "git/refs/tags")
SHA = re.compile(r"^[0-9a-f]{40}$")
ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "certification/sources/server-publication-history.v1.json"


# GitHub serves an empty git ref namespace as HTTP 404 rather than an empty array. That is a
# documented emptiness answer for the ref-listing endpoint only; the collection endpoints must
# answer 200, so a 404 from them is an unreadable namespace and stays an error.
EMPTY_NAMESPACE = "http-404-empty-ref-namespace"
REF_NAMESPACE_ENDPOINT = "git/refs/tags"


def _api(path: str, *, allow_empty_namespace: bool = False) -> tuple[list, str | None]:
    """Read only, with bounded backoff; a 403 cools down and never re-authenticates."""
    command = ["gh", "api", "--paginate", "--slurp", path]
    error = "not attempted"
    for delay in (0, 10, 30, 60, 120, 60):
        if delay:
            time.sleep(delay)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return [item for page in json.loads(result.stdout) for item in page], None
        error = result.stderr.strip()
        if allow_empty_namespace and "http 404" in error.lower():
            return [], EMPTY_NAMESPACE
        if not any(s in error.lower() for s in (
            "error connecting", "could not resolve host", "connection reset",
            "timeout", "http 403", "http 429", "status code: 499",
        )):
            break
    raise ValueError(f"cannot enumerate {path}: {error.splitlines()[0] if error else 'unknown error'}")


def collect(repository: str = REPOSITORY) -> dict[str, Any]:
    metadata = json.loads(subprocess.run(
        ["gh", "api", f"repos/{repository}"], capture_output=True, text=True, check=True).stdout)
    branch = metadata["default_branch"]
    tip = json.loads(subprocess.run(
        ["gh", "api", f"repos/{repository}/branches/{branch}"], capture_output=True, text=True,
        check=True).stdout)
    sources = []
    names: list[str] = []
    for endpoint in ENDPOINTS:
        rows, note = _api(f"repos/{repository}/{endpoint}?per_page=100",
                          allow_empty_namespace=endpoint == REF_NAMESPACE_ENDPOINT)
        source = {"api": f"https://api.github.com/repos/{repository}/{endpoint}",
                  "count": len(rows), "complete": True}
        if note:
            source["answer"] = note
        sources.append(source)
        for row in rows:
            value = row.get("name") or row.get("tag_name") or row.get("ref")
            if value:
                names.append(str(value).removeprefix("refs/tags/"))
    return {
        "schema": SCHEMA,
        "issue": ISSUE,
        "repository": repository,
        "observedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "observedDefaultBranch": branch,
        "observedDefaultBranchSha": tip["commit"]["sha"],
        "sources": sources,
        "publishedRefs": sorted(set(names)),
    }


def expected_endpoints(repository: str) -> dict[str, str]:
    return {f"https://api.github.com/repos/{repository}/{endpoint}": endpoint
            for endpoint in ENDPOINTS}


def verify(receipt: Any, repository: str = REPOSITORY, *, now: datetime | None = None,
           max_age_days: int | None = None) -> str:
    """Return the publisher identity proven to have no prior publication, or fail closed.

    Every field is load-bearing, so every field is checked exactly. The endpoint URLs must be
    the three real GitHub collection endpoints (this verifier does not fetch them, so a
    plausible-looking URL on another host would otherwise pass); each count must be a genuine
    nonnegative integer (a string or null count would otherwise be summed as zero); and when a
    freshness bound is supplied the observation must be inside it, because emptiness proven
    months ago says nothing about the repository at the cut.
    """
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError(f"expected a {SCHEMA} receipt")
    if receipt.get("repository") != repository:
        raise ValueError(f"receipt describes {receipt.get('repository')!r}, not {repository}")
    if not SHA.fullmatch(str(receipt.get("observedDefaultBranchSha", ""))):
        raise ValueError("receipt must pin the observed default-branch revision")
    try:
        observed = datetime.strptime(str(receipt.get("observedAt", "")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("receipt must record a UTC observation timestamp") from exc
    observed = observed.replace(tzinfo=timezone.utc)
    if max_age_days is not None:
        if not isinstance(max_age_days, int) or isinstance(max_age_days, bool) or max_age_days < 1:
            raise ValueError("publication-history freshness bound must be a positive number of days")
        current = now or datetime.now(timezone.utc)
        if observed > current + timedelta(minutes=5):
            raise ValueError("receipt was observed in the future")
        age = current - observed
        if age > timedelta(days=max_age_days):
            raise ValueError(f"publication history was enumerated {age.days} days ago against a "
                             f"{max_age_days}-day bound; re-enumerate it at the cut, because a "
                             "server published after the observation would break the first-release "
                             "premise")
    sources = receipt.get("sources")
    if not isinstance(sources, list):
        raise ValueError("receipt must enumerate its sources")
    expected = expected_endpoints(repository)
    observed_counts: dict[str, int] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("each source must be a mapping")
        api = source.get("api")
        if api not in expected:
            raise ValueError(f"{api!r} is not one of this publisher's GitHub publication "
                             "endpoints; only the exact api.github.com collections are evidence")
        endpoint = expected[api]
        if endpoint in observed_counts:
            raise ValueError(f"{endpoint} is enumerated more than once")
        if source.get("complete") is not True:
            raise ValueError(f"incomplete enumeration of {api}; pagination must finish")
        answer = source.get("answer")
        if answer is not None and (answer != EMPTY_NAMESPACE or endpoint != REF_NAMESPACE_ENDPOINT):
            raise ValueError(f"{endpoint} was not read as a complete listing: {answer}")
        count = source.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{endpoint} count must be a nonnegative integer, not {count!r}")
        observed_counts[endpoint] = count
    missing = [endpoint for endpoint in ENDPOINTS if endpoint not in observed_counts]
    if missing:
        raise ValueError("publication namespaces not enumerated: " + ", ".join(missing))
    refs = receipt.get("publishedRefs")
    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
        raise ValueError("receipt must list the refs it found")
    found = sum(observed_counts.values())
    if found or refs:
        raise ValueError(f"{repository} has {found or len(refs)} prior publication ref(s); the "
                         "first-release model does not apply and each capability needs its own "
                         "introduction evidence")
    return repository


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    capture = subs.add_parser("collect", help="enumerate the publisher's live tag/release namespaces")
    capture.add_argument("--output", type=Path, default=RECEIPT)
    check = subs.add_parser("verify", help="accept a receipt only if it proves no prior publication")
    check.add_argument("receipt", type=Path, nargs="?", default=RECEIPT)
    check.add_argument("--max-age-days", type=int,
                       help="reject an enumeration older than this; the release cut must bound it")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            args.output.write_text(json.dumps(collect(), indent=2) + "\n",
                                   encoding="utf-8", newline="\n")
            print(f"WROTE: {args.output} {digest(args.output)}")
            return 0
        verify(json.loads(args.receipt.read_text(encoding="utf-8")),
               max_age_days=args.max_age_days)
        print(f"PASS: {REPOSITORY} has no prior publication; {digest(args.receipt)}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        print(f"BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
