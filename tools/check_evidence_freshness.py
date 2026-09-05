#!/usr/bin/env python3
"""Freeze-phase evidence lineage + freshness gate (honua-release#60).

The release train's freeze job pins platform-manifest.yaml's honua-server SHA as the release
candidate's identity. Nothing previously checked that honua-evidence's capability-matrix.v1.json —
the evidence backing every `capability-key` docs-gate claim (honua-release#59) — is actually ABOUT
that pinned SHA, or is even recent. A candidate could be cut whose evidence chain is silently broken
at its very first link (plan §8 traceability). This gate makes that decidable and fail-able:

  lineage    the matrix's `server-matrix` producer sourceVersion sha must share commit history with
             the candidate's pinned honua-server sha (identical, an ancestor of it, or a descendant of
             it — i.e. NOT diverged onto an unrelated branch/fork). Ancestor-or-descendant (not just
             ancestor) is accepted deliberately: honua-evidence refreshes on its own cadence, so its
             observed sha is very often AHEAD of a manifest frozen earlier in time — that is normal
             lineage continuity, not a break. A true fork (diverged) means the evidence isn't actually
             about this release's history at all, which IS the failure this catches.
  freshness  each producer configured in certification/evidence-freshness.yaml must be no older than
             its maxAgeHours, per the matrix's own `freshness` block (fetchedAt/sourceVersion/ageDays/
             status — see honua-io/honua-evidence#8, "per-producer freshness/sourceVersion metadata
             documented as a stable contract for gate consumers").
  ledger     the matrix's own `generatedAt` must be within `ledger.maxAgeHours` (honua-release#84).
             Every freshness threshold above is computed from timestamps the aggregator stamps, so a
             DEAD aggregator freezes the whole block — and on 2026-08-16 exactly that happened for
             42h (honua-io/honua-evidence#17) while this gate stayed green, because the frozen
             server-matrix fetchedAt was still inside its window. Checking generatedAt directly names
             the real failure instead of misattributing it to whichever producer ages out first.
  producers  every producer the ledger carries is now evaluated, not just the two with thresholds.
             One the ledger self-reports as `stale`/`missing` must be named in the config's
             `acknowledged:` block with an owning issue and an unexpired reviewBy, or the gate goes
             RED (honua-release#84 AC-2). Same contract, and the same reason, as the demo canary's
             e2e/canary-quarantine.yaml: a known gap must be owned, never silent, never deleted.

Both the ancestor/descendant relationship (git history) and the matrix fetch are network/VCS
operations performed by the THIN WORKFLOW SHIM (gate-evidence.yml: raw.githubusercontent fetch + a
GitHub compare-API call) and handed in as plain values — this module makes no network calls itself,
so the decision core stays pure and unit-testable (same pattern as check_slo.py / check_upgrade.py).

A missing/unfetchable matrix, an unparseable/absent evidence sha, an undecidable lineage (compare
unreachable), or a producer entirely absent from the freshness block (most commonly because
honua-io/honua-evidence#8 hasn't landed that producer yet) -> BLOCKED, never a fake pass. A genuinely
DIVERGED lineage or a stale producer -> FAIL in both dry-run and real cuts (a gate that can't fail is
worse than no gate); only BLOCKED is tolerated in bootstrap mode.

  evaluate_evidence_freshness(candidate_sha, matrix, lineage_status, thresholds, now=None)
      -> (rows, overall)   # pure, unit-tested; overall in {pass, fail, blocked}
  python tools/check_evidence_freshness.py --manifest platform-manifest.yaml \
      --matrix path/to/capability-matrix.v1.json --lineage-status ancestor
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / "certification" / "evidence-freshness.yaml"

# Relations that mean "same commit history, not forked" — see module docstring on why descendant
# (evidence observed AFTER the candidate pin) is accepted alongside identical/ancestor.
LINEAGE_OK = {"identical", "ancestor", "descendant"}
SOURCE_VERSION_RE = re.compile(r"^(?P<sha>[0-9a-f]{6,40})@(?P<ts>.+)$")

# Statuses the ledger itself uses to mean "this producer is not green". Anything else it reports is
# treated as green — we deliberately do not re-derive honua-evidence's own verdict here.
LEDGER_NOT_GREEN = {"stale", "missing"}

ACKNOWLEDGED = "acknowledged"
NOTE = "note"

_ACK_REQUIRED_FIELDS = ("issue", "reason", "since", "reviewBy")


@dataclass(frozen=True)
class AcknowledgedProducer:
    """One `acknowledged:` entry — a ledger-red producer with a named owner and a hard expiry."""

    producer: str
    issue: str
    reason: str
    since: str
    review_by: str

    def expired(self, today: date) -> bool:
        """Past reviewBy the entry stops applying, so an acknowledgement cannot become permanent."""
        return today > _parse_date(self.review_by)

    def as_dict(self) -> dict:
        return {"producer": self.producer, "issue": self.issue, "reason": self.reason,
                "since": self.since, "reviewBy": self.review_by}


def _parse_date(value: str) -> date:
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


def load_matrix(path: str | Path | None) -> dict | None:
    """Same fail-closed contract as check_capabilities.load_capability_matrix: never raises, returns
    None on anything missing/unreadable/malformed, or lacking a `freshness` block entirely."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("freshness"), dict):
        return None
    return data


def load_config(path: Path = THRESHOLDS_PATH) -> dict:
    """The whole certification/evidence-freshness.yaml document (producers + ledger + acknowledged)."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict:
    return load_config(path).get("producers") or {}


def load_ledger_policy(path: Path = THRESHOLDS_PATH) -> dict:
    return load_config(path).get("ledger") or {}


def load_acknowledged(path: Path = THRESHOLDS_PATH) -> dict[str, AcknowledgedProducer]:
    """Parse the `acknowledged:` registry.

    A missing block means "nothing acknowledged" (valid, and the desired end state); a malformed
    entry raises rather than silently disabling the guard it describes — the same fail-loud contract
    as e2e/quarantine.py's load_quarantine.
    """
    raw = load_config(path).get("acknowledged") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 'acknowledged' must be a mapping of producer name -> entry")

    entries: dict[str, AcknowledgedProducer] = {}
    for producer, body in raw.items():
        if not isinstance(body, dict):
            raise ValueError(f"{path}: acknowledged entry {producer!r} must be a mapping")
        missing = [f for f in _ACK_REQUIRED_FIELDS if not str(body.get(f) or "").strip()]
        if missing:
            raise ValueError(f"{path}: acknowledged entry {producer!r} is missing required "
                             f"field(s): {missing}")
        issue = str(body["issue"]).strip()
        if not issue.startswith("http"):
            raise ValueError(f"{path}: acknowledged entry {producer!r} 'issue' must be a full URL, "
                             f"got {issue!r}")
        _parse_date(body["since"])
        review_by = str(body["reviewBy"]).strip()
        _parse_date(review_by)
        entries[str(producer)] = AcknowledgedProducer(
            producer=str(producer), issue=issue,
            reason=" ".join(str(body["reason"]).split()),
            since=str(body["since"]).strip(), review_by=review_by)
    return entries


def _age_hours(entry: dict, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    fetched = entry.get("fetchedAt")
    if not fetched:
        return None
    try:
        ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds() / 3600.0


def _source_sha(entry: dict) -> str | None:
    sv = entry.get("sourceVersion")
    if not sv:
        return None
    m = SOURCE_VERSION_RE.match(str(sv))
    return m.group("sha") if m else None


def evaluate_evidence_freshness(candidate_sha: str | None, matrix: dict | None,
                                lineage_status: str | None, thresholds: dict,
                                now: datetime | None = None, ledger_policy: dict | None = None,
                                acknowledged: dict | None = None) -> tuple[list[dict], str]:
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []

    if matrix is None:
        rows.append({"check": "matrix", "status": "blocked",
                     "why": "honua-evidence capability-matrix.v1.json unavailable/unfetchable — "
                            "cannot verify evidence lineage or freshness"})
        return rows, "blocked"

    freshness = matrix.get("freshness") or {}
    rows.extend(_ledger_rows(matrix, ledger_policy or {}, now))

    # --- lineage: the server-matrix producer's observed sha must share history with the candidate ---
    server_entry = freshness.get("server-matrix") or {}
    evidence_sha = _source_sha(server_entry)
    if not candidate_sha:
        rows.append({"check": "lineage", "status": "blocked",
                     "why": "no honua-server sha pinned in the candidate manifest"})
    elif not evidence_sha:
        rows.append({"check": "lineage", "status": "blocked",
                     "why": "server-matrix producer has no parseable sourceVersion sha in the evidence freshness block"})
    elif lineage_status is None:
        rows.append({"check": "lineage", "status": "blocked",
                     "why": f"could not determine ancestry between evidence sha {evidence_sha[:12]} "
                            f"and candidate sha {candidate_sha[:12]} (compare unreachable)"})
    elif lineage_status in LINEAGE_OK:
        rows.append({"check": "lineage", "status": "pass",
                     "why": f"evidence sha {evidence_sha[:12]} is {lineage_status} with candidate sha {candidate_sha[:12]}"})
    else:
        rows.append({"check": "lineage", "status": "fail",
                     "why": f"evidence sha {evidence_sha[:12]} has DIVERGED from candidate sha "
                            f"{candidate_sha[:12]} ({lineage_status}) — evidence is not about this "
                            f"release's history"})

    # --- freshness: each configured producer must be within its threshold -----------------------------
    for producer, cfg in (thresholds or {}).items():
        max_age = (cfg or {}).get("maxAgeHours")
        entry = freshness.get(producer)
        if entry is None:
            rows.append({"check": f"freshness:{producer}", "status": "blocked",
                         "why": f"producer {producer!r} not present in the evidence freshness metadata "
                                f"yet (see honua-io/honua-evidence#8)"})
            continue
        if entry.get("status") == "missing":
            rows.append({"check": f"freshness:{producer}", "status": "blocked",
                         "why": f"producer {producer!r} freshness status=missing: {entry.get('detail', 'no detail')}"})
            continue
        age = _age_hours(entry, now)
        if age is None:
            rows.append({"check": f"freshness:{producer}", "status": "blocked",
                         "why": f"producer {producer!r} has no parseable fetchedAt timestamp"})
            continue
        if max_age is not None and age > max_age:
            rows.append({"check": f"freshness:{producer}", "status": "fail",
                         "why": f"producer {producer!r} is {age:.1f}h old, exceeds threshold {max_age}h"})
        else:
            rows.append({"check": f"freshness:{producer}", "status": "pass",
                         "why": f"producer {producer!r} is {age:.1f}h old (threshold {max_age}h)"})

    rows.extend(_ledger_declared_rows(freshness, thresholds or {}, acknowledged or {}, now))

    if any(r["status"] == "fail" for r in rows):
        overall = "fail"
    elif any(r["status"] == "blocked" for r in rows):
        overall = "blocked"
    else:
        overall = "pass"
    return rows, overall


def _ledger_rows(matrix: dict, policy: dict, now: datetime) -> list[dict]:
    """Is the ledger itself still being regenerated? (honua-release#84 / honua-evidence#17.)

    Every per-producer freshness verdict below is computed from timestamps honua-evidence's
    aggregator stamps, so a stalled aggregator freezes them all at whatever they last said. Checking
    `generatedAt` directly is the only way this gate can tell "the evidence is stale" apart from
    "the thing that measures staleness is dead" — and it is the second one that needs a different
    repo, a different owner, and a different fix.
    """
    max_age = policy.get("maxAgeHours")
    if max_age is None:
        return []
    generated = matrix.get("generatedAt")
    if not generated:
        return [{"check": "ledger", "status": "blocked",
                 "why": "capability-matrix.v1.json has no generatedAt — cannot tell whether the "
                        "honua-evidence aggregator is still running"}]
    age = _age_hours({"fetchedAt": generated}, now)
    if age is None:
        return [{"check": "ledger", "status": "blocked",
                 "why": f"capability-matrix.v1.json generatedAt {generated!r} is unparseable — "
                        f"cannot tell whether the honua-evidence aggregator is still running"}]
    if age > max_age:
        return [{"check": "ledger", "status": "fail",
                 "why": f"the honua-evidence ledger has not been regenerated for {age:.1f}h "
                        f"(threshold {max_age}h) — the aggregator is stalled, so EVERY freshness "
                        f"verdict below is frozen at {generated} and not to be trusted. Check "
                        f"honua-io/honua-evidence's `aggregate` workflow for a run parked in "
                        f"`waiting`/`queued` holding the aggregate-pages concurrency group "
                        f"(honua-io/honua-evidence#17)."}]
    return [{"check": "ledger", "status": "pass",
             "why": f"honua-evidence ledger regenerated {age:.1f}h ago (threshold {max_age}h)"}]


def _ledger_declared_rows(freshness: dict, thresholds: dict, acknowledged: dict,
                          now: datetime) -> list[dict]:
    """Every producer the ledger carries that has no threshold of its own.

    Before honua-release#84 these were invisible: the gate looked only at the producers named in
    `producers:`, so the ledger could self-report server-keys `stale` and dr-drills `missing` while
    this gate reported PASS. A ledger-red producer now has exactly two honest outcomes — fixed, or
    named in `acknowledged:` with an owning issue and a date the acknowledgement dies on.
    """
    today = now.date()
    rows: list[dict] = []

    for producer in sorted(freshness):
        if producer in thresholds:
            continue  # judged by its own threshold above; never overridden by an acknowledgement
        entry = freshness.get(producer) or {}
        status = str(entry.get("status") or "").lower()
        if status not in LEDGER_NOT_GREEN:
            rows.append({"check": f"producer:{producer}", "status": "pass",
                         "why": f"ledger reports {producer!r} as {status or 'unknown'}"})
            continue

        detail = entry.get("detail") or f"ageDays={entry.get('ageDays')}"
        ack = acknowledged.get(producer)
        if ack is None:
            rows.append({"check": f"producer:{producer}", "status": "fail",
                         "why": f"ledger reports producer {producer!r} as {status} ({detail}) and no "
                                f"owning issue claims it — add an `acknowledged:` entry in "
                                f"certification/evidence-freshness.yaml or fix the producer"})
        elif ack.expired(today):
            rows.append({"check": f"producer:{producer}", "status": "fail",
                         "why": f"ledger reports producer {producer!r} as {status} ({detail}) and its "
                                f"acknowledgement EXPIRED on {ack.review_by} ({ack.issue}) — "
                                f"re-own it or fix it"})
        else:
            rows.append({"check": f"producer:{producer}", "status": ACKNOWLEDGED,
                         "why": f"ledger reports producer {producer!r} as {status} ({detail}) — "
                                f"owned by {ack.issue} (since {ack.since}, review by "
                                f"{ack.review_by}): {ack.reason}"})

    # Registry rot: an acknowledgement that outlived its gap re-hides the next real one.
    for producer, ack in sorted(acknowledged.items()):
        entry = freshness.get(producer)
        if entry is None:
            rows.append({"check": f"acknowledgement:{producer}", "status": NOTE,
                         "why": f"acknowledges producer {producer!r}, which the ledger does not "
                                f"carry (renamed or removed?) — {ack.issue}"})
        elif str(entry.get("status") or "").lower() not in LEDGER_NOT_GREEN:
            rows.append({"check": f"acknowledgement:{producer}", "status": NOTE,
                         "why": f"producer {producer!r} is green again — delete this acknowledgement "
                                f"({ack.issue}), it is now hiding nothing and will hide the next "
                                f"real regression"})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    ap.add_argument("--matrix", default=None, help="path to a locally-fetched capability-matrix.v1.json")
    ap.add_argument("--lineage-status", default=None,
                    help="identical|ancestor|descendant|diverged (computed by the workflow shim); "
                         "omitted/empty = undecidable -> BLOCKED")
    ap.add_argument("--thresholds", default=str(THRESHOLDS_PATH))
    ap.add_argument("--require-real", action="store_true", help="promote BLOCKED to FAIL (real train cuts)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the per-check rows to this path so the workflow can render the "
                         "acknowledged-producer table into the run summary")
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    candidate_sha = ((manifest.get("components") or {}).get("honua-server") or {}).get("sha") or None
    matrix = load_matrix(args.matrix)
    thresholds_path = Path(args.thresholds)
    thresholds = load_thresholds(thresholds_path)
    ledger_policy = load_ledger_policy(thresholds_path)
    acknowledged = load_acknowledged(thresholds_path)
    lineage_status = args.lineage_status or None

    rows, overall = evaluate_evidence_freshness(candidate_sha, matrix, lineage_status, thresholds,
                                                ledger_policy=ledger_policy,
                                                acknowledged=acknowledged)
    print(f"== evidence lineage/freshness — {overall.upper()} ==")
    for r in rows:
        print(f"  [{r['status'].upper():12}] {r['check']}: {r['why']}")

    # Registry rot is not a failure but it must not be quiet either — an acknowledgement that
    # outlived its gap re-hides the next real one.
    for r in rows:
        if r["status"] == NOTE:
            print(f"::warning title=evidence acknowledgement rot::{r['check']}: {r['why']}")

    if args.json_out:
        payload = {"overall": overall, "rows": rows,
                   "acknowledged": [a.as_dict() for a in acknowledged.values()]}
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if overall == "fail":
        return 1
    if overall == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
