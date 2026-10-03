#!/usr/bin/env python3
"""Fetch the retained evidence a per-lock promotion record names (honua-release#386, R19-R21).

`fetch` writes the layout tools/check_promotion_readiness.py reads: the minting train's
certified candidate (including the selected lock's exact bytes), one workflow receipt per
declared evidence class, the seven recorded canaries, and the complete canary ledger since
minting. `candidates` lists committed promotion records whose burn has reached hour 48 and
that are not already published or being promoted.

Run identities are checked here against Actions metadata; the checker decides readiness.
Nothing in this tool can pass a record: missing artifacts leave gaps the checker refuses.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


MINTING_WORKFLOWS = (".github/workflows/release-train.yml", ".github/workflows/nightly-certification.yml")
MINTING_EVENTS = ("schedule", "workflow_dispatch")
CANARY_WORKFLOW = ".github/workflows/demo-canary.yml"
CANARY_ARTIFACT = "live-canary-evidence"
CANDIDATE_ARTIFACT = "certified-candidate"
# Each producer uploads its class receipt as `promotion-receipt-<class>` containing receipt.json.
RECEIPT_ARTIFACT = "promotion-receipt-{}"
NIGHTLY_CLASSES = ("build-test", "contract", "sbom", "security", "upgrade", "capacity-soak", "dr",
                   "lambda-certification", "protocol-ledger", "deterministic-journey", "nightly-model-journey")
# The workflows (and triggering events) allowed to produce each class receipt. The checker trusts
# the class and lock a receipt names, so only an allowlisted producer's run may supply it. Nightly
# receipts come from the minting run itself. Qualifying producers (genuine-model-journey,
# update-rollback, esri-bundle, cite) are added here as they land (#386/#381); until then those
# classes, and any other declared class, have no producer and refuse.
RECEIPT_PRODUCERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    **dict.fromkeys(NIGHTLY_CLASSES, (MINTING_WORKFLOWS, MINTING_EVENTS)),
}
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
CLASS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Labels promotion may request: 2026.1 RCs and patch RCs (x.y.z), as before this schedule.
PROMOTABLE_LABEL_RE = re.compile(r"(?:2026\.1|[0-9]+\.[0-9]+\.[0-9]+)-rc\.[1-9][0-9]*")
BURN = timedelta(hours=48)
# A promotion requested in the last day is waiting for, or was refused by, its human
# reviewer; requesting it again every hour would only queue duplicate approvals.
REQUEST_COOLDOWN = timedelta(hours=24)


class FetchError(ValueError):
    pass


def _time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FetchError(f"{value!r} is not a UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _run_id(value: Any, field: str) -> str:
    value = str(value)
    if not RUN_ID_RE.fullmatch(value):
        raise FetchError(f"{field} must be a positive Actions run id")
    return value


class GitHub:
    """The few read-only gh calls the fetcher needs; tests substitute `runner`."""

    def __init__(self, repository: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.repository = repository
        self.runner = runner

    def _gh(self, *args: str) -> subprocess.CompletedProcess:
        return self.runner(["gh", *args], capture_output=True, text=True)

    def api(self, path: str = "") -> Any:
        result = self._gh("api", f"repos/{self.repository}" + (f"/{path}" if path else ""))
        if result.returncode != 0:
            raise FetchError(f"GitHub API read failed for {path}")
        return json.loads(result.stdout)

    def pages(self, path: str, key: str) -> list[dict[str, Any]]:
        result = self._gh("api", "--paginate", "--slurp", f"repos/{self.repository}/{path}")
        if result.returncode != 0:
            raise FetchError(f"GitHub API read failed for {path}")
        return [item for page in json.loads(result.stdout) for item in page.get(key, [])]

    def download(self, run_id: str, artifact: str, dest: Path) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        result = self._gh("run", "download", run_id, "--repo", self.repository, "--name", artifact, "--dir", str(dest))
        return result.returncode == 0

    def release_exists(self, tag: str) -> bool:
        return self._gh("release", "view", tag, "--repo", self.repository).returncode == 0


def _identity(run: dict[str, Any], repository: str, default_branch: str, run_id: str,
              paths: tuple[str, ...] | None = None, events: tuple[str, ...] | None = None) -> None:
    repo = run.get("repository") if isinstance(run.get("repository"), dict) else {}
    head = run.get("head_repository") if isinstance(run.get("head_repository"), dict) else {}
    problems = [name for name, ok in (
        ("id", str(run.get("id")) == run_id),
        ("repository", repo.get("full_name") == head.get("full_name") == repository),
        ("branch", run.get("head_branch") == default_branch),
        ("workflow", paths is None or run.get("path") in paths),
        ("event", events is None or run.get("event") in events),
        ("status", run.get("status") == "completed"),
        ("conclusion", run.get("conclusion") == "success"),
    ) if not ok]
    if problems:
        raise FetchError(f"run {run_id} failed identity checks: {problems}")


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _retain(gh: GitHub, run_id: str, artifact: str, root: Path, run: dict[str, Any],
            names: tuple[str, ...] | None = None) -> bool:
    """Extract an artifact in isolation, copy only `names` (or every file but run.json), then
    write the Actions run metadata, so artifact contents can never replace the API record."""
    with tempfile.TemporaryDirectory() as scratch:
        downloaded = gh.download(run_id, artifact, Path(scratch))
        root.mkdir(parents=True, exist_ok=True)
        if downloaded:
            for path in sorted(Path(scratch).rglob("*")):
                relative = path.relative_to(scratch)
                if path.is_symlink() or not path.is_file() or relative == Path("run.json"):
                    continue
                if names is not None and relative.as_posix() not in names:
                    continue
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, root / relative)
    _write(root / "run.json", run)
    return downloaded


def _canary_lock(gh: GitHub, run_id: str) -> str | None:
    """The lock digest a canary recorded, or None when it failed before binding one."""
    with tempfile.TemporaryDirectory() as scratch:
        if not gh.download(run_id, CANARY_ARTIFACT, Path(scratch)):
            return None
        try:
            envelope = json.loads((Path(scratch) / "live-canary-evidence.json").read_text(encoding="utf-8"))
            digest = envelope["candidateLock"]["digest"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return digest if isinstance(digest, str) else None


def canary_sequence(gh: GitHub, *, lock_digest: str, minted_at: datetime) -> dict[str, Any]:
    """Every completed canary that could have observed the lock since minting.

    Earlier attempts of a re-run canary are kept as unattributed observations, so a
    successful re-run cannot erase a failure.
    """
    since = (minted_at - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = gh.pages(f"actions/workflows/demo-canary.yml/runs?created=%3E%3D{since}&per_page=100",
                    "workflow_runs")
    observed = []
    for run in sorted(runs, key=lambda run: str(run.get("updated_at"))):
        if run.get("status") != "completed":
            continue
        run_id = _run_id(run.get("id"), "canary run id")
        observed.append({"runId": run_id, "completedAt": run.get("updated_at"),
                         "status": "pass" if run.get("conclusion") == "success" else str(run.get("conclusion")),
                         "lockDigest": _canary_lock(gh, run_id)})
        attempts = run.get("run_attempt")
        for attempt in range(1, attempts if isinstance(attempts, int) else 1):
            previous = gh.api(f"actions/runs/{run_id}/attempts/{attempt}")
            if previous.get("conclusion") != "success":
                observed.append({"runId": run_id, "completedAt": previous.get("updated_at"),
                                 "status": str(previous.get("conclusion")), "lockDigest": None})
    return {"lockDigest": lock_digest, "runs": observed}


def fetch(record: dict[str, Any], gh: GitHub, out: Path) -> None:
    default_branch = gh.api().get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise FetchError("repository has no default branch")
    lock = record.get("lock") if isinstance(record.get("lock"), dict) else {}
    digest = lock.get("digest")
    if not isinstance(digest, str):
        raise FetchError("record has no lock digest")

    rc = _run_id(record.get("rcTrainRunId"), "rcTrainRunId")
    train = gh.api(f"actions/runs/{rc}")
    _identity(train, gh.repository, default_branch, rc, MINTING_WORKFLOWS, MINTING_EVENTS)
    root = out / "trains" / rc
    if not _retain(gh, rc, CANDIDATE_ARTIFACT, root, train) or not (root / "platform-lock.json").is_file():
        raise FetchError(f"minting train {rc} has no retained certified candidate lock")

    runs: dict[str, dict[str, Any]] = {rc: train}
    for row in record.get("evidence") or []:
        name = row.get("class") if isinstance(row, dict) else None
        if not isinstance(name, str) or not CLASS_RE.fullmatch(name):
            raise FetchError("evidence row has no valid class")
        run_id = _run_id(row.get("runId"), f"{name} runId")
        if name not in RECEIPT_PRODUCERS:
            raise FetchError(f"evidence class {name} has no allowlisted producer workflow")
        if run_id not in runs:
            runs[run_id] = gh.api(f"actions/runs/{run_id}")
        # The run must be a successful default-branch run of this class's producer.
        _identity(runs[run_id], gh.repository, default_branch, run_id, *RECEIPT_PRODUCERS[name])
        # A missing receipt is left missing: the checker refuses it.
        _retain(gh, run_id, RECEIPT_ARTIFACT.format(name), out / "evidence" / name / run_id, runs[run_id],
                ("receipt.json",))

    for row in record.get("demoCanaries") or []:
        run_id = _run_id(row.get("runId") if isinstance(row, dict) else None, "canary runId")
        run = gh.api(f"actions/runs/{run_id}")
        _identity(run, gh.repository, default_branch, run_id, (CANARY_WORKFLOW,), ("schedule",))
        _retain(gh, run_id, CANARY_ARTIFACT, out / "canaries" / run_id, run, ("live-canary-evidence.json",))

    _write(out / "canary-sequence.json",
           canary_sequence(gh, lock_digest=digest, minted_at=_time(train.get("updated_at"))))


def candidates(promotions: Path, gh: GitHub, *, now: datetime,
               published: Callable[[str], bool]) -> list[dict[str, str]]:
    """Committed records at or past hour 48 with no release and no recent promotion request."""
    requests = gh.pages("actions/workflows/promote.yml/runs?per_page=100", "workflow_runs")
    selected = []
    for path in sorted(promotions.glob("*.json")):
        label = path.name[:-len(".json")]
        if not PROMOTABLE_LABEL_RE.fullmatch(label):
            continue
        # One unreadable or malformed record must not stop every other candidate's check.
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            print(f"::warning::{path} is not readable JSON; skipped", file=sys.stderr)
            continue
        if not isinstance(record, dict) or record.get("platformLabel") != label:
            continue
        try:
            burn_start = _time((record.get("lock") or {}).get("burnStartedAt"))
            rc = _run_id(record.get("rcTrainRunId"), "rcTrainRunId")
        except (FetchError, ValueError, AttributeError):
            continue
        if now - burn_start < BURN or published(label):
            continue
        pending = [run for run in requests
                   if str(run.get("display_title", "")).startswith(f"promote {label} ")
                   and (run.get("status") != "completed"
                        or now - _time(run.get("created_at")) < REQUEST_COOLDOWN)]
        if pending:
            continue
        selected.append({"label": label, "record": f"certification/promotions/{label}.json", "rcTrainRunId": rc})
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = commands.add_parser("fetch", help="fetch the evidence one promotion record names")
    fetch_cmd.add_argument("--record", required=True, type=Path)
    fetch_cmd.add_argument("--repository", required=True)
    fetch_cmd.add_argument("--out-dir", required=True, type=Path)
    list_cmd = commands.add_parser("candidates", help="list burning promotion candidates at or past hour 48")
    list_cmd.add_argument("--promotions-dir", required=True, type=Path)
    list_cmd.add_argument("--repository", required=True)
    list_cmd.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    gh = GitHub(args.repository)
    try:
        if args.command == "fetch":
            fetch(json.loads(args.record.read_text(encoding="utf-8")), gh, args.out_dir)
            print(f"fetched promotion evidence into {args.out_dir}")
            return 0

        def published(label: str) -> bool:
            tag = subprocess.run([sys.executable, str(Path(__file__).with_name("tag_signing.py")),
                                  "publication-tag", "honua-release", label],
                                 capture_output=True, text=True, check=True).stdout.strip()
            return gh.release_exists(tag)

        selected = candidates(args.promotions_dir, gh, now=datetime.now(timezone.utc), published=published)
        args.out.write_text(json.dumps(selected) + "\n", encoding="utf-8")
        print(f"{len(selected)} promotion candidate(s) at or past hour 48")
        return 0
    except (FetchError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
