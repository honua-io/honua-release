#!/usr/bin/env python3
"""Fail closed on missing live release promises; never fetch private finding details."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import urllib.error
import zipfile

import rc_gate_window as w
import rc2_rigor
import release_outcomes as outcomes
import release_adversarial as adversarial
from execution_order import AmbiguousOrder, contenders, instant

CONTRACT = Path(__file__).resolve().parents[1] / 'certification/release-promise.v1.json'
WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / '.github/workflows'
REQUIRED_IDS = frozenset({*(f'operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)),
                         'operates-12-notify', 'security-hardened', 'security-open-high',
                         'cloud-launch-stack', 'security-whitehat', 'cloud-image-aot',
                         'cloud-no-redis-source', 'security-agent-default-deny',
                         'supply-sbom', 'supply-scan', 'contract-compat', 'docs-executable',
                         'clients-installed', 'operates-telemetry', 'operates-restore',
                         *rc2_rigor.IDS})
AI_IDS = frozenset(f'ai-{s}' for s in ('operate', 'publish', 'build', 'migrate', 'injection'))

GA_ONLY_IDS = AI_IDS | {'protocol-compat', 'operates-capacity', 'ga-capabilities',
                        'operates-8-upgrade', 'chaos'}
ALL_IDS = REQUIRED_IDS | GA_ONLY_IDS
SECURITY_SUMMARY_ROW = 'security-open-high'
SECURITY_SUMMARY_ACTORS = frozenset({'mikemcdougall'})
SECURITY_SUMMARY_FIELDS = frozenset({
    'schema', 'row', 'status', 'serverSha', 'imageDigest',
    'platformLockDigest', 'observedAt', 'assertions', 'openHighFindings',
})
MAX_MANIFEST_INPUT_BYTES = 4096
IMAGE_SIZE_BUDGETS = {'rc': 370_000_000, 'ga': 220_000_000}
# Rows whose verdict rests on older source evidence; its time must stay inside the window too.
SOURCE_DATED_IDS = frozenset({'operates-capacity', 'ga-capabilities'})
COMPANIONS = {'contract-compat': ['gate-contract-live.yml'],
              'docs-executable': ['gate-docs.yml']}


def producer_workflows(row):
    if row.get('source') == 'manifest-input':
        return []
    return [row['workflow'], *row.get('additionalWorkflows', [])]


def load_outcomes() -> list[dict]:
    return outcomes.load(CONTRACT)


def _validate_manifest_input_row(row: dict) -> None:
    if row.get('source') != 'manifest-input':
        raise ValueError(f"row {row['id']} must use source 'manifest-input'")
    if any(key in row for key in ('repo', 'workflow', 'artifact')):
        raise ValueError(f"manifest-input row {row['id']} must not declare a CI workflow")
    if not re.fullmatch(r'[a-z0-9-]+\.json', row.get('receipt', '')):
        raise ValueError('invalid producer receipt')


def _validate_contract_row(row: dict) -> None:
    expected = ['ga'] if row['id'] in GA_ONLY_IDS else ['rc', 'ga']
    if row.get('required') != expected:
        raise ValueError('release promise requirement disagrees with founder RC/GA ruling')
    if row.get('additionalWorkflows', []) != COMPANIONS.get(row['id'], []):
        raise ValueError('required companion producer changed')
    if row['id'] == SECURITY_SUMMARY_ROW:
        _validate_manifest_input_row(row)
        return
    if row.get('source') == 'manifest-input':
        raise ValueError(f"row {row['id']} is not allowed as a manifest-input")
    for key, pattern in (('repo', r'honua-io/[a-z0-9-]+'),
                         ('workflow', r'[a-z0-9-]+\.yml'),
                         ('artifact', r'[a-z0-9-]+'), ('receipt', r'[a-z0-9-]+\.json')):
        if not re.fullmatch(pattern, row.get(key, '')):
            raise ValueError(f'invalid producer {key}')


def missing_rc_workflows(contract_path: Path = CONTRACT, workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    doc = json.loads(contract_path.read_text())
    entries = [row for row in doc['rows'] + doc['outcomes'] if 'rc' in row.get('required', [])]
    needed = {wf for row in entries if row.get('repo') == 'honua-io/honua-release'
              for wf in producer_workflows(row)}
    return sorted(wf for wf in needed if not (workflows_dir / wf).is_file())


def load_contract(path: Path = CONTRACT) -> list[dict]:
    doc = json.loads(path.read_text())
    rows = doc['rows']
    ids = [r['id'] for r in rows]
    if (doc.get('schema') != 'honua.release-promise-contract/v1' or doc.get('windowDays') != 7
            or len(ids) != len(ALL_IDS) or set(ids) != ALL_IDS):
        raise ValueError('release promise contract must cover exactly the founder rows')
    for row in rows:
        _validate_contract_row(row)
    return rows


def _common_receipt_valid(doc: dict, row: dict) -> bool:
    if (not isinstance(doc, dict) or doc.get('schema') != 'honua.release-promise-receipt/v1'
            or doc.get('row') != row['id'] or doc.get('status') != 'PASS'):
        return False
    assertions = doc.get('assertions', {})
    if not isinstance(assertions, dict):
        return False
    return not any(type(assertions.get(key)) is not type(value) or assertions.get(key) != value
                   for key, value in row['assertions'].items())


def _canonical_summary_sha256(doc: dict) -> str:
    canonical = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def _manifest_input_valid(doc: dict, row: dict, context: tuple, actor: str) -> bool:
    candidate, now = context
    if actor not in SECURITY_SUMMARY_ACTORS:
        return False
    if not _common_receipt_valid(doc, row) or set(doc.get('assertions', {})) != set(row['assertions']):
        return False
    if set(doc) != SECURITY_SUMMARY_FIELDS:
        return False
    if (doc.get('serverSha') != candidate.server_sha
            or doc.get('imageDigest') != candidate.server_digest
            or doc.get('platformLockDigest') != candidate.platform_lock):
        return False
    observed = instant(doc.get('observedAt'))
    if observed is None or not now - dt.timedelta(days=7) <= observed <= now:
        return False
    count = doc.get('openHighFindings')
    return type(count) is int and count == 0


def load_manifest_input(path: str = '', raw_json: str = '', actor: str = '') -> list[dict]:
    """Load a sanitized manifest-input receipt from a local path or inline JSON string."""
    rejected = [{'source': 'manifest-input', 'actor': actor, 'reports': []}]
    if path and raw_json:
        return rejected
    if not path and not raw_json:
        return []
    try:
        if path:
            with Path(path).open('rb') as handle:
                raw = handle.read(MAX_MANIFEST_INPUT_BYTES + 1)
        else:
            raw = raw_json.encode('utf-8')
        if len(raw) > MAX_MANIFEST_INPUT_BYTES:
            return rejected
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            return rejected
        return [{'source': 'manifest-input', 'actor': actor, 'reports': [doc]}]
    except (OSError, ValueError, TypeError):
        return rejected


def _receipt_valid(doc: dict, row: dict, context: tuple) -> bool:
    candidate, now, run = context
    observed = instant(doc.get('observedAt'))
    started, ended = instant(run.get('run_started_at')), instant(run.get('updated_at'))
    if (not _common_receipt_valid(doc, row) or doc.get('certifying') is False
            or w.is_diagnostic(doc) or not w.matches_candidate(doc, candidate) or observed is None
            or started is None or ended is None
            or not now - dt.timedelta(days=7) <= started <= observed <= ended <= now):
        return False
    if row['id'] in ('security-hardened', 'cloud-launch-stack', 'cloud-image-aot'):
        if not re.fullmatch(r'honua-gate-[a-zA-Z0-9-]+', str(doc.get('stackName', ''))):
            return False
    if row['id'] == 'cloud-image-aot' and not _image_budget_met(doc, 'rc'):
        return False
    if row['id'] in SOURCE_DATED_IDS:
        source = instant(doc.get('sourceObservedAt'))
        if source is None or not now - dt.timedelta(days=7) <= source <= observed:
            return False
    if row['id'] in adversarial.IDS:
        return adversarial.valid(doc)
    if row['id'] in rc2_rigor.IDS:
        return rc2_rigor.valid(doc)
    if row['id'] in outcomes.IDS:
        return outcomes.valid(doc)
    return True


def _run_passes(run: dict, row: dict, context: tuple) -> bool:
    candidate, now = context
    if (run.get('status') != 'completed' or run.get('conclusion') != 'success'
            or run.get('head_branch') != 'trunk'
            or run.get('event') not in ('workflow_dispatch', 'schedule', 'repository_dispatch', 'workflow_run')
            or run.get('path') not in {'.github/workflows/' + name for name in producer_workflows(row)}):
        return False
    docs = run.get('reports', [])
    return len(docs) == 1 and _receipt_valid(docs[0], row, (candidate, now, run))


def _admitted_security_summary(token: str, now: dt.datetime) -> list[dict]:
    """Read the founder-attested security-open-high row from the latest gate-release-promise run."""
    records = []
    for run in _history(('honua-io/honua-release', w.PROMISE_GATE), token, now):
        actor_login = w.trusted_promise_actor(run)
        if actor_login not in SECURITY_SUMMARY_ACTORS:
            records.append({'source': 'admitted-promise-gate', 'actor': actor_login, 'reports': []})
            continue
        run_artifacts = _available_artifacts('honua-io/honua-release', token, run['id'])
        docs = _producer_reports(
            {'id': 'release-promise-report', 'repo': 'honua-io/honua-release',
             'artifact': 'release-promise-report', 'receipt': 'release-promise-report.json'},
            token, run_artifacts,
        )
        records.append({**run, 'source': 'admitted-promise-gate', 'actor': actor_login, 'reports': docs})
    return records


def _admitted_security_row_valid(run: dict, candidate: w.Candidate, now: dt.datetime) -> dict | None:
    actor = run.get('actor', '')
    docs = run.get('reports', [])
    if actor not in SECURITY_SUMMARY_ACTORS or len(docs) != 1:
        return None
    doc = docs[0]
    if doc.get('schema') != 'honua.release-promise-report/v1' or not w.matches_candidate(doc, candidate):
        return None
    rows = doc.get('rows')
    if not isinstance(rows, list):
        return None
    matches = [r for r in rows if isinstance(r, dict) and r.get('id') == SECURITY_SUMMARY_ROW]
    if len(matches) != 1:
        return None
    sec = matches[0]
    if sec.get('actor') != actor or not _report_row_fresh(sec, now, 'ga'):
        return None
    return sec


def _evaluate_manifest_input(row: dict, runs: list[dict], context: tuple) -> dict:
    result = {'id': row['id'], 'required': row['required'], 'status': 'missing',
              'producer': 'manifest-input/' + row['receipt']}
    if not runs:
        return result
    if all(isinstance(r, dict) and r.get('source') == 'admitted-promise-gate' for r in runs):
        candidate, now = context
        validated = [_admitted_security_row_valid(r, candidate, now) for r in runs]
        if not all(validated):
            return {**result, 'status': 'invalid', 'observedAt': None}
        chosen = min(validated, key=lambda item: instant(item['observedAt']))
        return {**result, 'status': 'PASS', 'observedAt': chosen['observedAt'],
                'actor': chosen['actor'], 'summarySha256': chosen['summarySha256']}
    if len(runs) != 1 or not isinstance(runs[0], dict) or runs[0].get('source') != 'manifest-input':
        return {**result, 'status': 'invalid', 'observedAt': None}
    actor = runs[0].get('actor', '')
    docs = runs[0].get('reports', [])
    if len(docs) != 1 or not _manifest_input_valid(docs[0], row, context, actor):
        return {**result, 'status': 'invalid', 'observedAt': None}
    return {**result, 'status': 'PASS', 'observedAt': docs[0]['observedAt'],
            'actor': actor, 'summarySha256': _canonical_summary_sha256(docs[0])}


def evaluate_row(row: dict, runs: list[dict], context: tuple) -> dict:
    if row.get('source') == 'manifest-input':
        return _evaluate_manifest_input(row, runs, context)
    result = {'id': row['id'], 'required': row['required'], 'status': 'missing',
              'producer': row['repo'] + '/' + row['workflow']}
    if not runs:
        return result
    try:
        deciding = []
        paths = {'.github/workflows/' + name for name in producer_workflows(row)}
        if any(run.get('path') not in paths for run in runs):
            return {**result, 'status': 'invalid'}
        for path in paths:
            producer_runs = [run for run in runs if run.get('path') == path]
            if not producer_runs:
                return result
            deciding.extend(contenders(producer_runs))
    except AmbiguousOrder:
        return {**result, 'status': 'invalid'}
    passed = all(_run_passes(run, row, context) for run in deciding)
    measured = outcomes.summarize(row, [run['reports'][0] for run in deciding]) if (
        passed and row['id'] in outcomes.IDS) else {}
    if passed and row['id'] == 'cloud-image-aot':
        measured['imageSizeBytes'] = max(run['reports'][0]['imageSizeBytes'] for run in deciding)
    if passed and row['id'] in SOURCE_DATED_IDS:
        measured['sourceObservedAt'] = min(
            (run['reports'][0]['sourceObservedAt'] for run in deciding), key=instant)
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


def collect(rows: list[dict], token: str, now: dt.datetime, manifest_inputs: dict | None = None) -> dict:
    records, histories, artifacts = {}, {}, {}
    for row in rows:
        if row.get('source') == 'manifest-input':
            records[row['id']] = list((manifest_inputs or {}).get(row['id'], []))
            continue
        records[row['id']] = []
        for workflow in producer_workflows(row):
            key = row['repo'], workflow
            if key not in histories:
                histories[key] = _history(key, token, now)
            # Keep each producer's latest failure; never fall back to an older green run.
            for run in histories[key]:
                artifact_key = row['repo'], run['id']
                if artifact_key not in artifacts:
                    artifacts[artifact_key] = _available_artifacts(row['repo'], token, run['id'])
                reports = _producer_reports(row, token, artifacts[artifact_key])
                records[row['id']].append({**run, 'reports': reports})
    return records


def report_valid(doc: dict, profile: str, now: dt.datetime, *, actor: str | None = None) -> bool:
    if doc.get('schema') != 'honua.release-promise-report/v1' or doc.get('status') != 'pass':
        return False
    rows = doc.get('rows')
    if not isinstance(rows, list) or len(rows) != len(ALL_IDS) or any(not isinstance(r, dict) for r in rows):
        return False
    if {r.get('id') for r in rows} != ALL_IDS:
        return False
    if not outcomes.report_valid(doc.get('outcomes'), profile, now):
        return False
    if actor is not None:
        if actor not in SECURITY_SUMMARY_ACTORS:
            return False
        sec_row = next(r for r in rows if r['id'] == SECURITY_SUMMARY_ROW)
        if sec_row.get('actor') != actor:
            return False
    required = ALL_IDS if profile == 'ga' else REQUIRED_IDS
    return all(_report_row_fresh(r, now, profile) for r in rows if r['id'] in required)


def _image_budget_met(doc: dict, profile: str) -> bool:
    size = doc.get('imageSizeBytes')
    return type(size) is int and 0 < size <= IMAGE_SIZE_BUDGETS[profile]


def _report_row_fresh(row: dict, now: dt.datetime, profile: str) -> bool:
    observed = instant(row.get('observedAt'))
    if row.get('status') != 'PASS' or observed is None:
        return False
    oldest = now - dt.timedelta(days=7)
    if not oldest <= observed <= now:
        return False
    if row['id'] == SECURITY_SUMMARY_ROW:
        return (row.get('actor') in SECURITY_SUMMARY_ACTORS
                and bool(re.fullmatch(r'[0-9a-f]{64}', str(row.get('summarySha256', '')))))
    if row['id'] == 'cloud-image-aot':
        return _image_budget_met(row, profile)
    if row['id'] in SOURCE_DATED_IDS:
        source = instant(row.get('sourceObservedAt'))
        return source is not None and oldest <= source <= observed
    return True


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-sha', required=True)
    parser.add_argument('--server-digest', required=True)
    parser.add_argument('--platform-lock', required=True)
    parser.add_argument('--profile', choices=('rc', 'ga'), default='rc')
    parser.add_argument('--security-summary', default='',
                        help='path to lane-host security-open-high.json manifest input')
    parser.add_argument('--security-summary-json', default='',
                        help='inline sanitized security-open-high.json manifest input')
    parser.add_argument('--security-summary-actor', default='',
                        help='authenticated actor supplying the security summary manifest input')
    parser.add_argument('--reuse-admitted-security-summary', action='store_true',
                        help='read founder-attested security-open-high row from gate-release-promise.yml')
    parser.add_argument('--out', default='release-promise-report.json')
    return parser, parser.parse_args()


def print_results(results):
    print('| Row | Required | Status | Measurement |\n|---|---|---|---|')
    for row in results:
        print(f"| {row['id']} | {', '.join(row['required'])} | {row['status']} | {row.get('value', '')} {row.get('unit', '')} |")


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
    summary_path = getattr(args, 'security_summary', '') or os.environ.get('SECURITY_SUMMARY_FILE', '')
    summary_json = getattr(args, 'security_summary_json', '') or os.environ.get('SECURITY_SUMMARY_JSON', '')
    summary_actor = getattr(args, 'security_summary_actor', '') or os.environ.get('SECURITY_SUMMARY_ACTOR', '')
    reuse_admitted = getattr(args, 'reuse_admitted_security_summary', False)
    if summary_path or summary_json:
        security_records = load_manifest_input(summary_path, summary_json, actor=summary_actor)
    elif reuse_admitted:
        security_records = _admitted_security_summary(token, now)
    else:
        security_records = []
    manifest_inputs = {SECURITY_SUMMARY_ROW: security_records}
    try:
        results = evaluate(rows, collect(rows, token, now, manifest_inputs), (candidate, now))
    except (urllib.error.URLError, ValueError, AmbiguousOrder):
        # Print no producer payload, URLs, private tracker identifiers or exception bodies.
        results = evaluate(rows, {}, (candidate, now))
    passed = all(r['status'] == 'PASS' for r in results if args.profile in r['required'])
    measurements = [r for r in results if r['id'] in outcomes.IDS]
    passed = passed and outcomes.report_valid(measurements, args.profile, now)
    passed = passed and all(_image_budget_met(r, args.profile) for r in results
                            if r['id'] == 'cloud-image-aot')
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
