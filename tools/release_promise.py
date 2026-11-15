#!/usr/bin/env python3
"""Fail closed on missing live release promises; never fetch private finding details."""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import urllib.error
import zipfile

import rc_gate_window as w
import release_outcomes as outcomes
import release_adversarial as adversarial
from execution_order import AmbiguousOrder, contenders, instant

CONTRACT = Path(__file__).resolve().parents[1] / 'certification/release-promise.v1.json'
REQUIRED_IDS = frozenset({*(f'operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)),
                         'operates-12-notify', 'security-hardened', 'security-open-high',
                         'cloud-launch-stack', 'security-whitehat', 'chaos'})
AI_IDS = frozenset(f'ai-{s}' for s in ('operate', 'publish', 'build', 'migrate', 'injection'))


def load_outcomes() -> list[dict]:
    return outcomes.load(CONTRACT)


def load_contract(path: Path = CONTRACT) -> list[dict]:
    doc = json.loads(path.read_text())
    rows = doc['rows']
    ids = [r['id'] for r in rows]
    if (doc.get('schema') != 'honua.release-promise-contract/v1' or doc.get('windowDays') != 7
            or len(ids) != 20 or set(ids) != REQUIRED_IDS | AI_IDS):
        raise ValueError('release promise contract must cover exactly the 20 founder rows')
    for row in rows:
        expected = ['ga'] if row['id'] in AI_IDS else ['rc', 'ga']
        if row.get('required') != expected:
            raise ValueError('release promise requirement disagrees with founder RC/GA ruling')
        for key, pattern in (('repo', r'honua-io/[a-z0-9-]+'),
                             ('workflow', r'[a-z0-9-]+\.yml'),
                             ('artifact', r'[a-z0-9-]+'), ('receipt', r'[a-z0-9-]+\.json')):
            if not re.fullmatch(pattern, row.get(key, '')):
                raise ValueError(f'invalid producer {key}')
    return rows


def _receipt_valid(doc: dict, row: dict, context: tuple) -> bool:
    candidate, now, run = context
    observed = instant(doc.get('observedAt'))
    started, ended = instant(run.get('run_started_at')), instant(run.get('updated_at'))
    if (doc.get('schema') != 'honua.release-promise-receipt/v1' or doc.get('row') != row['id']
            or doc.get('status') != 'PASS' or doc.get('certifying') is False
            or w.is_diagnostic(doc) or not w.matches_candidate(doc, candidate) or observed is None
            or started is None or ended is None
            or not now - dt.timedelta(days=7) <= started <= observed <= ended <= now):
        return False
    assertions = doc.get('assertions', {})
    if not isinstance(assertions, dict) or any(
            type(assertions.get(key)) is not type(value) or assertions.get(key) != value
            for key, value in row['assertions'].items()):
        return False
    if row['id'] in ('security-hardened', 'cloud-launch-stack'):
        if not re.fullmatch(r'honua-gate-[a-zA-Z0-9-]+', str(doc.get('stackName', ''))):
            return False
    if row['id'] == 'security-open-high':
        count = doc.get('openHighFindings')
        return type(count) is int and count == 0
    if row['id'] in adversarial.IDS:
        return adversarial.valid(doc)
    if row['id'] in outcomes.IDS:
        return outcomes.valid(doc)
    return True


def _run_passes(run: dict, row: dict, context: tuple) -> bool:
    candidate, now = context
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_branch') != 'trunk'
            or run.get('event') not in ('workflow_dispatch', 'schedule', 'repository_dispatch')
            or run.get('path') != '.github/workflows/' + row['workflow']):
        return False
    docs = run.get('reports', [])
    return len(docs) == 1 and _receipt_valid(docs[0], row, (candidate, now, run))


def evaluate_row(row: dict, runs: list[dict], context: tuple) -> dict:
    result = {'id': row['id'], 'required': row['required'], 'status': 'missing',
              'producer': row['repo'] + '/' + row['workflow']}
    if not runs:
        return result
    try:
        deciding = contenders(runs)
    except AmbiguousOrder:
        return {**result, 'status': 'invalid'}
    passed = all(_run_passes(run, row, context) for run in deciding)
    measured = outcomes.summarize(row, [run['reports'][0] for run in deciding]) if (
        passed and row['id'] in outcomes.IDS) else {}
    return {**result, 'status': 'PASS' if passed else 'invalid',
            **measured,
            'runs': [run['id'] for run in deciding],
            'observedAt': min(run['reports'][0]['observedAt'] for run in deciding) if passed else None}


def evaluate(rows: list[dict], records: dict, context: tuple) -> list[dict]:
    return [evaluate_row(row, records.get(row['id'], []), context) for row in rows]


def _receipt_from_zip(blob: bytes, filename: str) -> list[dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            matches = [info for info in archive.infolist() if info.filename == filename]
            if len(matches) != 1 or matches[0].file_size > 100_000:
                return []
            doc = json.loads(archive.read(matches[0]))
            return [doc] if isinstance(doc, dict) else []
    except (zipfile.BadZipFile, ValueError, OSError):
        return []


def _artifacts(repo: str, token: str, run_id: int) -> list[dict]:
    artifacts = []
    for page in range(1, 11):
        batch = w._get(f'{w.API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100&page={page}', token)
        artifacts.extend(batch.get('artifacts', []))
        if len(batch.get('artifacts', [])) < 100:
            return artifacts
    raise ValueError('producer artifact history incomplete')


def _reports(row: dict, token: str, artifacts: list[dict]) -> list[dict]:
    found = [a for a in artifacts if a.get('name') == row['artifact'] and not a.get('expired')]
    if len(found) != 1 or not 0 < found[0].get('size_in_bytes', 0) <= 1_000_000:
        return []
    blob = w._get_bytes(f"{w.API}/repos/{row['repo']}/actions/artifacts/{found[0]['id']}/zip", token, 1_000_001)
    docs = _receipt_from_zip(blob, row['receipt'])
    if row['id'] == 'security-whitehat' and docs:
        docs[0]['_archivedReports'] = adversarial.archived_reports(docs[0], blob)
    if row['id'] in outcomes.IDS and docs:
        docs[0]['releaseNotes'] = _release_notes(blob)
    return docs


def _release_notes(blob: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            files = [info for info in archive.infolist() if info.filename == 'release-notes.md']
            if len(files) != 1 or files[0].file_size > 100_000:
                return ''
            return archive.read(files[0]).decode('utf-8')
    except (zipfile.BadZipFile, ValueError, OSError):
        return ''


def _history(key: tuple, token: str, now: dt.datetime) -> list[dict]:
    url = (f'{w.API}/repos/{key[0]}/actions/workflows/{key[1]}/runs'
           '?branch=trunk&status=completed')
    try:
        runs, complete = w._completed_history(url, token, now - dt.timedelta(days=37))
        return contenders(runs) if runs and complete else []
    except (urllib.error.URLError, ValueError, AmbiguousOrder):
        return []


def _producer_reports(row: dict, token: str, run_artifacts: list[dict]) -> list[dict]:
    try:
        return _reports(row, token, run_artifacts)
    except (urllib.error.URLError, ValueError):
        return []


def _available_artifacts(repo: str, token: str, run_id: int) -> list[dict]:
    try:
        return _artifacts(repo, token, run_id)
    except (urllib.error.URLError, ValueError):
        return []


def collect(rows: list[dict], token: str, now: dt.datetime) -> dict:
    records, histories, artifacts = {}, {}, {}
    for row in rows:
        key = row['repo'], row['workflow']
        if key not in histories:
            histories[key] = _history(key, token, now)
        # Order every completed execution, including receipt-less failures. Never fall back to old green.
        records[row['id']] = []
        for run in histories[key]:
            artifact_key = row['repo'], run['id']
            if artifact_key not in artifacts:
                artifacts[artifact_key] = _available_artifacts(row['repo'], token, run['id'])
            reports = _producer_reports(row, token, artifacts[artifact_key])
            records[row['id']].append({**run, 'reports': reports})
    return records


def report_valid(doc: dict, profile: str, now: dt.datetime) -> bool:
    if doc.get('schema') != 'honua.release-promise-report/v1' or doc.get('status') != 'pass':
        return False
    rows = doc.get('rows')
    if not isinstance(rows, list) or len(rows) != 20 or any(not isinstance(r, dict) for r in rows):
        return False
    if {r.get('id') for r in rows} != REQUIRED_IDS | AI_IDS:
        return False
    if not outcomes.report_valid(doc.get('outcomes'), profile, now):
        return False
    required = REQUIRED_IDS | AI_IDS if profile == 'ga' else REQUIRED_IDS
    return all(r.get('status') == 'PASS' and instant(r.get('observedAt')) is not None
               and now - dt.timedelta(days=7) <= instant(r['observedAt']) <= now
               for r in rows if r['id'] in required)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-sha', required=True)
    parser.add_argument('--server-digest', required=True)
    parser.add_argument('--platform-lock', required=True)
    parser.add_argument('--profile', choices=('rc', 'ga'), default='rc')
    parser.add_argument('--out', default='release-promise-report.json')
    return parser, parser.parse_args()


def print_results(results):
    print('| Row | Required | Status | Minutes |\n|---|---|---|---|')
    for row in results:
        print(f"| {row['id']} | {', '.join(row['required'])} | {row['status']} | {row.get('value', '')} |")


def main() -> int:
    parser, args = arguments()
    candidate = w.Candidate(w.normalize_sha(args.server_sha), w.normalize_digest(args.server_digest),
                            w.normalize_digest(args.platform_lock))
    if not all((candidate.server_sha, candidate.server_digest, candidate.platform_lock)):
        parser.error('candidate SHA and digests must be complete')
    token = os.environ.get('GH_TOKEN')
    if not token:
        parser.error('GH_TOKEN with producer Actions read access is required')
    now = dt.datetime.now(dt.timezone.utc)
    rows = load_contract() + load_outcomes()
    try:
        results = evaluate(rows, collect(rows, token, now), (candidate, now))
    except (urllib.error.URLError, ValueError, AmbiguousOrder):
        # Print no producer payload, URLs, private tracker identifiers or exception bodies.
        results = evaluate(rows, {}, (candidate, now))
    passed = all(r['status'] == 'PASS' for r in results if args.profile in r['required'])
    measurements = [r for r in results if r['id'] in outcomes.IDS]
    passed = passed and outcomes.report_valid(measurements, args.profile, now)
    report = dict(schema='honua.release-promise-report/v1', status='pass' if passed else 'fail',
                  profile=args.profile, serverSha=candidate.server_sha,
                  imageDigest=candidate.server_digest, platformLockDigest=candidate.platform_lock,
                  evaluatedAt=now.isoformat(), rows=[r for r in results if r['id'] not in outcomes.IDS],
                  outcomes=measurements)
    Path(args.out).write_text(json.dumps(report, indent=2) + '\n')
    print_results(results)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
