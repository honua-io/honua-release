#!/usr/bin/env python3
"""Admission for a 2026.1 maintenance backport. Does not create release/2026.1.

Trunk stays the only place a patch is written. After an accepted RC and before GA, the founder
or the release session creates and protects release/2026.1. This tool only plans a cherry-pick
onto that branch, and only admits a pull request that carries the backport label and whose
commits are already patch-equivalent to trunk (`git cherry` lines that start with '-').

  plan     say whether a sha can be cherry-picked, or print the founder step
  admit    refuse a pull request that is not a labelled trunk cherry-pick
"""
from __future__ import annotations

import argparse
import re
import sys

TARGET = "release/2026.1"
LABEL = "backport/2026.1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
FOUNDER_STEP = """\
release/2026.1 does not exist. This workflow will not create it.
Creating and protecting that branch is a one-step founder or release-session action,
after the accepted RC and before GA, and it must not race the session cutting 2026.1-rc.2:

  git fetch origin
  git checkout -b release/2026.1 <accepted-rc-commit>
  git push origin release/2026.1

Then protect the branch: pull request required, this workflow's admit check required,
no force-push, no deletion. Privileged release workflows stay on trunk. Arbitrary
commits on the maintenance branch are not a patch.
"""


class BackportError(ValueError):
    """The backport is not admissible."""


def plan(sha: str, branch_exists: bool) -> dict:
    if not SHA.fullmatch(sha or ""):
        raise BackportError("trunk sha must be a full 40-character commit")
    if not branch_exists:
        return {"action": "refuse", "target": TARGET, "founder": FOUNDER_STEP.strip()}
    return {
        "action": "cherry-pick",
        "target": TARGET,
        "label": LABEL,
        "head": f"backport/{sha[:12]}",
        "sha": sha,
    }


def admit(base: str, labels: list[str], cherry_lines: list[str]) -> str:
    """Return 'admit' or raise. cherry_lines are `git cherry -v <trunk> <head>` lines."""
    if base != TARGET:
        raise BackportError(f"backport pull requests target {TARGET}")
    if LABEL not in labels:
        raise BackportError(f"missing {LABEL} label")
    rows = [line.strip() for line in cherry_lines if line.strip()]
    if not rows:
        raise BackportError("backport has no commits")
    foreign = [line for line in rows if not line.startswith("-")]
    if foreign:
        raise BackportError("a commit is not a cherry-pick of trunk")
    return "admit"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    planned = sub.add_parser("plan")
    planned.add_argument("--sha", required=True)
    planned.add_argument("--branch-exists", required=True, choices=("yes", "no"))
    admitted = sub.add_parser("admit")
    admitted.add_argument("--base", required=True)
    admitted.add_argument("--labels", default="", help="comma-separated pull request labels")
    admitted.add_argument("--cherry-file", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args.sha, args.branch_exists == "yes")
            if result["action"] == "refuse":
                print(result["founder"])
                return 2
            print(f"head={result['head']}")
            print(f"target={result['target']}")
            print(f"label={result['label']}")
            return 0
        labels = [part.strip() for part in args.labels.split(",") if part.strip()]
        lines = open(args.cherry_file, encoding="utf-8").read().splitlines()
        print(admit(args.base, labels, lines))
        return 0
    except (OSError, ValueError) as exc:
        print(f"backport: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
