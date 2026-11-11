"""Plan candidate verification and record one row per ticket.

The trigger stays off until HONUA_CANDIDATE_VERIFICATION=on. A caller that
has not set that value gets no plan, no row and no issue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SWITCH_ENV = "HONUA_CANDIDATE_VERIFICATION"
SWITCHED_OFF = 3
FAILURE_TITLE = "candidate verification failed"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RESULTS = frozenset({"pass", "fail", "blocked"})


@dataclass(frozen=True)
class Ticket:
    number: int
    lane: str
    workflows: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    ticket: int
    result: str
    run_url: str


@dataclass(frozen=True)
class Recording:
    when: str
    candidate_sha: str
    outcomes: tuple[Outcome, ...]
    existing: tuple[tuple[int, str], ...] = ()


TICKETS = (
    Ticket(1131, "s-rc2-vendor", ("gate-executable-docs",)),
    Ticket(1132, "s-qa-live", ("e2e-local-docker",)),
    Ticket(1133, "s-rc2-vendor", ("gate-installed-clients", "gate-protocol-certification")),
    Ticket(1136, "s-rc2-vendor", ()),
    Ticket(1137, "s-rc2-vendor", ("gate-protocol-certification",)),
    Ticket(1250, "s-rc2-vendor", ("gate-executable-docs",)),
)

_BY_NUMBER = {ticket.number: ticket for ticket in TICKETS}


def switched_on() -> bool:
    return os.environ.get(SWITCH_ENV) == "on"


def dispatch_workflows() -> tuple[str, ...]:
    seen: list[str] = []
    for ticket in TICKETS:
        for name in ticket.workflows:
            if name not in seen:
                seen.append(name)
    return tuple(seen)


def result_row(when: str, candidate_sha: str, outcome: Outcome) -> str:
    if not SHA_RE.fullmatch(candidate_sha):
        raise ValueError("candidate sha must be 40 lowercase hex characters")
    if outcome.result not in RESULTS:
        raise ValueError("result must be pass, fail or blocked")
    ticket = _BY_NUMBER.get(outcome.ticket)
    if ticket is None:
        raise ValueError("unknown verification ticket")
    if not when or not outcome.run_url:
        raise ValueError("when and run_url are required")
    return (
        f"{when} sha={candidate_sha} ticket={outcome.ticket} result={outcome.result} "
        f"run={outcome.run_url} lane={ticket.lane}"
    )


def _row_key(line: str) -> tuple[str, str] | None:
    sha = re.search(r"sha=([0-9a-f]{40})", line)
    ticket = re.search(r"ticket=(\d+)", line)
    if sha is None or ticket is None:
        return None
    return sha.group(1), ticket.group(1)


def upsert_row(body: str, row: str) -> str:
    key = _row_key(row)
    if key is None:
        raise ValueError("row is missing its candidate sha or ticket")
    kept = [line for line in body.splitlines() if line and _row_key(line) != key]
    kept.append(row)
    return "\n".join(kept)


def failure_body(rows: list[str]) -> str:
    if not rows:
        return ""
    lanes = []
    for row in rows:
        match = re.search(r"lane=(\S+)", row)
        if match is None:
            raise ValueError("failure row is missing its lane")
        if match.group(1) not in lanes:
            lanes.append(match.group(1))
    lines = ["Candidate verification failed.", ""]
    lines.extend(rows)
    lines.append("")
    lines.append("Owning lane: " + ", ".join(lanes) + ".")
    return "\n".join(lines)


def record(recording: Recording, post) -> dict[int, str]:
    if not switched_on():
        raise RuntimeError("candidate verification is switched off")
    previous = dict(recording.existing)
    rows: dict[int, str] = {}
    failures: list[str] = []
    for outcome in recording.outcomes:
        row = result_row(recording.when, recording.candidate_sha, outcome)
        rows[outcome.ticket] = row
        post("comment", outcome.ticket, upsert_row(previous.get(outcome.ticket, ""), row))
        if outcome.result == "fail":
            failures.append(row)
    body = failure_body(failures)
    if body:
        post("issue", FAILURE_TITLE, body)
    return rows


def load_outcomes(path: Path) -> tuple[Outcome, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("results must be a list")
    return tuple(
        Outcome(int(item["ticket"]), str(item["result"]), str(item["run_url"]))
        for item in payload
    )


def _sha(value: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise SystemExit(2)
    return value


def main(argv: list[str] | None = None) -> int:
    if not switched_on():
        return SWITCHED_OFF
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "record"))
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--results")
    args = parser.parse_args(argv)
    _sha(args.candidate_sha)
    if args.command == "plan":
        sys.stdout.write("\n".join(dispatch_workflows()) + "\n")
        return 0
    if not args.results:
        return 2
    when = os.environ.get("CANDIDATE_VERIFICATION_WHEN") or datetime.now().astimezone().isoformat(timespec="seconds")
    outcomes = load_outcomes(Path(args.results))
    record(Recording(when, args.candidate_sha, outcomes), lambda *_: None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
