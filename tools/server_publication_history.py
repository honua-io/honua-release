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
from datetime import datetime, timezone
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


def verify(receipt: Any, repository: str = REPOSITORY) -> str:
    """Return the publisher identity proven to have no prior publication, or fail closed."""
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError(f"expected a {SCHEMA} receipt")
    if receipt.get("repository") != repository:
        raise ValueError(f"receipt describes {receipt.get('repository')!r}, not {repository}")
    if not SHA.fullmatch(str(receipt.get("observedDefaultBranchSha", ""))):
        raise ValueError("receipt must pin the observed default-branch revision")
    try:
        datetime.strptime(str(receipt.get("observedAt", "")), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("receipt must record a UTC observation timestamp") from exc
    sources = receipt.get("sources")
    if not isinstance(sources, list):
        raise ValueError("receipt must enumerate its sources")
    observed = {}
    for source in sources:
        if not isinstance(source, dict) or not str(source.get("api", "")).startswith("https://"):
            raise ValueError("each source must name the HTTPS endpoint it enumerated")
        if source.get("complete") is not True:
            raise ValueError(f"incomplete enumeration of {source.get('api')}; pagination must finish")
        endpoint = str(source["api"]).rsplit(f"{repository}/", 1)[-1]
        answer = source.get("answer")
        if answer is not None and (answer != EMPTY_NAMESPACE or endpoint != REF_NAMESPACE_ENDPOINT):
            raise ValueError(f"{endpoint} was not read as a complete listing: {answer}")
        observed[endpoint] = source.get("count")
    missing = [endpoint for endpoint in ENDPOINTS if endpoint not in observed]
    if missing:
        raise ValueError("publication namespaces not enumerated: " + ", ".join(missing))
    refs = receipt.get("publishedRefs")
    if not isinstance(refs, list):
        raise ValueError("receipt must list the refs it found")
    found = sum(count for count in observed.values() if isinstance(count, int))
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
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            args.output.write_text(json.dumps(collect(), indent=2) + "\n",
                                   encoding="utf-8", newline="\n")
            print(f"WROTE: {args.output} {digest(args.output)}")
            return 0
        verify(json.loads(args.receipt.read_text(encoding="utf-8")))
        print(f"PASS: {REPOSITORY} has no prior publication; {digest(args.receipt)}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        print(f"BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
