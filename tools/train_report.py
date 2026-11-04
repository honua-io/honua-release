"""Release-train gate report statuses.

A semantic verdict is kept. A workflow job result is not a verdict: success is
pass, cancelled is blocked, and every other result is fail. Blocked never
becomes pass just because the job exited 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

VERDICTS = ("pass", "fail", "blocked", "skipped")
_VERDICTS = frozenset(VERDICTS)


def status_from_signal(signal: str) -> str:
    text = signal.strip()
    if text in _VERDICTS:
        return text
    if text == "success":
        return "pass"
    if text == "cancelled":
        return "blocked"
    return "fail"


def _blocking(statuses: list[str]) -> str | None:
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    return None


def rehearsal_overall(statuses: list[str]) -> str:
    return _blocking(statuses) or "pass"


def strict_overall(statuses: list[str]) -> str:
    return _blocking(statuses) or ("fail" if "skipped" in statuses else "pass")


@dataclass(frozen=True)
class ReportIdentity:
    platform_label: str
    dry_run: bool
    generated_at: str
    evidence_url: str


def assemble(lines: list[str], identity: ReportIdentity,
             fold_statuses: Callable[[list[str]], str]) -> dict:
    gates = [_gate_row(line, identity.evidence_url) for line in lines if "|" in line]
    statuses = [row["status"] for row in gates]
    return {
        "platform_label": identity.platform_label,
        "dry_run": identity.dry_run,
        "generatedAt": identity.generated_at,
        "evidence_url": identity.evidence_url,
        "gates": gates,
        "summary": {name: statuses.count(name) for name in VERDICTS},
        "overallStatus": fold_statuses(statuses),
    }


def _gate_row(line: str, evidence_url: str) -> dict:
    gate, signal = line.strip().split("|", 1)
    return {
        "gate": gate.strip(),
        "status": status_from_signal(signal),
        "why": "job/gate signal: " + signal.strip(),
        "evidence_url": evidence_url,
    }


def write_report(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble the release-train gate report.")
    parser.add_argument("--label", required=True)
    parser.add_argument("--dry-run", required=True, choices=("true", "false"))
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    fold_statuses = rehearsal_overall if args.dry_run == "true" else strict_overall
    document = assemble(
        sys.stdin.read().splitlines(),
        ReportIdentity(args.label, args.dry_run == "true", args.generated_at, args.run_url),
        fold_statuses,
    )
    write_report(args.out, document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
