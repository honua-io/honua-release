#!/usr/bin/env python3
"""Fetch and authenticate upstream workflow proof artifacts before gate validation."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path
import re
import subprocess

import rc2_rigor as rigor

ALLOWED_REPOS = frozenset({'honua-io/honua-release', 'honua-io/honua-server'})
RUN_ID_RE = re.compile(r'^[1-9][0-9]*$')
ARTIFACT_RE = re.compile(r'^[A-Za-z0-9._-]+$')
MAX_RUN_AGE = dt.timedelta(days=7)


@dataclass(frozen=True)
class EvidenceSource:
    repo: str
    run_id: str
    artifact: str


def validate_source(source: EvidenceSource) -> None:
    if source.repo not in ALLOWED_REPOS:
        raise ValueError(f'unsupported evidence repository: {source.repo}')
    if not RUN_ID_RE.fullmatch(source.run_id):
        raise ValueError(f'invalid source run id: {source.run_id}')
    if not ARTIFACT_RE.fullmatch(source.artifact):
        raise ValueError(f'invalid artifact name: {source.artifact}')


def _allowed_branch(branch: object) -> bool:
    return isinstance(branch, str) and (
        branch == 'trunk' or branch.startswith('nightly-candidate-')
    )


def verify_run_metadata(run: dict, now: dt.datetime) -> None:
    if not isinstance(run, dict):
        raise ValueError('workflow run response must be a JSON object')
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        raise ValueError('source workflow run must be completed with conclusion=success')
    if not _allowed_branch(run.get('head_branch')):
        raise ValueError('source workflow run must come from trunk or nightly-candidate-*')
    updated = rigor.parse_utc(run.get('updated_at'))
    if updated is None or not now - MAX_RUN_AGE <= updated <= now:
        raise ValueError('source workflow run updated_at is outside the 7-day window')


def verify_downloaded_tree(root: Path) -> None:
    if not root.is_dir():
        raise ValueError('downloaded evidence directory does not exist')
    files = 0
    resolved_root = root.resolve()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'symlink rejected in downloaded evidence: {path.name}')
        if not path.resolve().is_relative_to(resolved_root):
            raise ValueError(f'path escapes evidence root: {path}')
        if path.is_file():
            files += 1
    if files == 0:
        raise ValueError('downloaded evidence artifact contained no files')


def _run_gh(args: list[str], runner=subprocess.run) -> str:
    done = runner(['gh', *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ValueError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()[:240]}")
    return done.stdout


def fetch_evidence(
    source: EvidenceSource, out_dir: Path,
    runner=subprocess.run, now: dt.datetime | None = None,
) -> Path:
    validate_source(source)
    if out_dir.is_absolute() or '..' in out_dir.parts:
        raise ValueError('output directory must be a relative path')
    raw_run = _run_gh(['api', f'repos/{source.repo}/actions/runs/{source.run_id}'], runner=runner)
    verify_run_metadata(json.loads(raw_run), now or dt.datetime.now(dt.timezone.utc))
    out_dir.mkdir(parents=True, exist_ok=True)
    _run_gh(
        ['run', 'download', source.run_id, '--repo', source.repo,
         '--name', source.artifact, '--dir', str(out_dir)],
        runner=runner,
    )
    verify_downloaded_tree(out_dir)
    return out_dir


def main(argv: list[str] | None = None, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--artifact', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    source = EvidenceSource(args.repo, args.run_id, args.artifact)
    try:
        fetch_evidence(source, args.out, runner=runner)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f'Evidence retrieval rejected: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
