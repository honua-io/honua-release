"""Machine-readable scenario result + gate-report assembly.

Mirrors the release-train's gate-report.json contract ({gate,status,why,evidence}) so the AI/MCP layer
parses results instead of scraping logs (plan §15). The e2e gate (release-train.yml: gate_e2e) consumes
this artifact.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"          # a real regression — the seam is broken
    SKIPPED = "skipped"    # a probe toolchain/SDK is unavailable locally (not a release blocker alone)
    BLOCKED = "blocked"    # depends on a real published image/metric that does not exist yet (TODO)


# Statuses that the release train must not accept as success. SKIPPED/BLOCKED are tolerated per-PR
# (honest while pins are placeholders) but become failures under E2E_REQUIRE_REAL.
FAILING = {Status.FAIL}


@dataclass
class Result:
    scenario: str
    status: Status
    why: str = ""
    seeded_from: str = ""           # the audit finding this scenario is a regression test for
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def assemble(results: list[Result], require_real: bool) -> dict:
    """Build the gate-report and decide the overall gate verdict.

    AI proposes, the pipeline disposes: the verdict here is mechanical, not a judgement call.
    """
    blocking = set(FAILING)
    if require_real:
        # When real artifacts are expected, a BLOCKED/SKIPPED is a real red — the gate can FAIL.
        blocking |= {Status.BLOCKED, Status.SKIPPED}

    failed = [r for r in results if r.status in blocking]
    overall = Status.FAIL if failed else Status.PASS
    return {
        "gate": "e2e-local-docker",
        "status": overall.value,
        "require_real": require_real,
        "summary": {s.value: sum(1 for r in results if r.status is s) for s in Status},
        "scenarios": [r.to_dict() for r in results],
    }


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
