"""Issue-linked probe quarantine for the demo canary (honua-release#84).

The pure, unit-tested decision core behind `e2e/canary-quarantine.yaml` (which carries the full
contract as prose). Kept separate from `demo_canary.py` so the rules can be proven without a live
target, the same shape as `tools/check_evidence_freshness.py` vs `gate-evidence.yml`.

The one rule worth restating here, because getting it wrong would corrupt the evidence chain:
**quarantine downgrades a CI verdict, never an evidence verdict.** `apply_quarantine` rewrites a
`fail` to `quarantined` so the run does not go red and the automated demo-canary issue is not opened;
`demo_canary.py` still maps `quarantined` to `red` in the live-canary envelope it publishes to
honua-evidence. A gap that is known and owned is still a gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from canonical_checks import CheckResult

E2E_DIR = Path(__file__).resolve().parent
QUARANTINE_PATH = E2E_DIR / "canary-quarantine.yaml"

QUARANTINED = "quarantined"

_REQUIRED_FIELDS = ("issue", "reason", "since", "reviewBy")


@dataclass
class QuarantineEntry:
    probe: str
    issue: str
    reason: str
    since: str
    review_by: str

    def expired(self, today: date) -> bool:
        """A quarantine past its reviewBy stops applying — it cannot silently become permanent."""
        return today > _parse_date(self.review_by)

    def as_dict(self) -> dict:
        return {"probe": self.probe, "issue": self.issue, "reason": self.reason,
                "since": self.since, "reviewBy": self.review_by}


def _parse_date(value: str) -> date:
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def load_quarantine(path: Path | str = QUARANTINE_PATH) -> dict[str, QuarantineEntry]:
    """Parse the registry. A missing file means "nothing quarantined" (valid, and the desired end
    state); a malformed entry raises rather than silently disabling the guard it describes."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = data.get("quarantine") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: 'quarantine' must be a mapping of probe name -> entry")

    entries: dict[str, QuarantineEntry] = {}
    for probe, body in raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"{p}: quarantine entry {probe!r} must be a mapping")
        missing = [f for f in _REQUIRED_FIELDS if not str(body.get(f) or "").strip()]
        if missing:
            raise ValueError(f"{p}: quarantine entry {probe!r} is missing required field(s): {missing}")
        issue = str(body["issue"]).strip()
        if not issue.startswith("http"):
            raise ValueError(f"{p}: quarantine entry {probe!r} 'issue' must be a full URL, got {issue!r}")
        _parse_date(body["since"])
        review_by = str(body["reviewBy"]).strip()
        _parse_date(review_by)
        entries[str(probe)] = QuarantineEntry(probe=str(probe), issue=issue,
                                              reason=" ".join(str(body["reason"]).split()),
                                              since=str(body["since"]).strip(), review_by=review_by)
    return entries


def apply_quarantine(results: list[CheckResult], entries: dict[str, QuarantineEntry],
                     today: date | None = None) -> tuple[list[CheckResult], dict]:
    """Rewrite owned, unexpired FAILs to `quarantined` and report on the registry's own health.

    Returns the (new) result list and an audit dict with three lists the caller must surface:
      applied  — entries that downgraded a real failure this run (each with its owning issue)
      expired  — entries whose reviewBy has passed; the probe stays FAIL and the run goes red
      stale    — entries whose probe is no longer failing; delete them
      unknown  — entries naming a probe the canary did not emit (usually a rename)
    """
    today = today or datetime.now(timezone.utc).date()
    by_name = {r.name: r for r in results}

    applied: list[dict] = []
    expired: list[dict] = []
    stale: list[dict] = []
    unknown: list[dict] = []

    out: list[CheckResult] = []
    for r in results:
        entry = entries.get(r.name)
        if entry is None or r.status != "fail":
            out.append(r)
            continue
        if entry.expired(today):
            expired.append(entry.as_dict())
            out.append(CheckResult(r.name, "fail",
                                   f"{r.why} — QUARANTINE EXPIRED (reviewBy {entry.review_by}, "
                                   f"{entry.issue}); failing the run again",
                                   r.evidence))
            continue
        applied.append(entry.as_dict())
        out.append(CheckResult(r.name, QUARANTINED,
                               f"{r.why} — QUARANTINED, owned by {entry.issue} (review by "
                               f"{entry.review_by}): {entry.reason}",
                               r.evidence))

    for name, entry in entries.items():
        observed = by_name.get(name)
        if observed is None:
            unknown.append(entry.as_dict())
        elif observed.status != "fail":
            stale.append({**entry.as_dict(), "observedStatus": observed.status})

    return out, {"applied": applied, "expired": expired, "stale": stale, "unknown": unknown}
