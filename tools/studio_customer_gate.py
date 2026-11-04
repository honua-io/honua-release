#!/usr/bin/env python3
"""Dispatch the manifest-pinned Console suite and validate its complete image-bound evidence."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import yaml

from candidate_lock import resolve_pins, validate_lock

REQUIRED_STAGES = frozenset({
    'publisher-auth', 'import-publish-customer-regions.geojson', 'import-publish-customer-sites.csv',
    'two-layers-filter-widget', 'session-expiry-recovery', 'authored-style-labels', 'save-reload-reopen',
    'independent-server-render', 'separate-approval', 'public-audience', 'accessibility',
})
REPO = 'honua-io/honua-console'
WORKFLOW = 'console-live-journey.yml'
# Release-session ruling 2026-11-03 (rc.2 redo; disclosed in the release notes): the suite may run at a
# descendant of the pinned Console commit only when every changed path between the two is test harness
# under e2e/. The Console image stays the lock's pinned digest and source; the receipt records both shas
# and the changed paths. Any other difference keeps the equality rule (suite == pinned Console sha).
SUITE_EXCEPTION_PREFIX = 'e2e/'
STUDIO_COMPONENTS = ('console', 'server')
SUITE_EXCEPTION_RULING = 'studio-suite-harness-exception-2026-11-03'


def suite_exception(pinned: str, suite: str, compare: dict) -> list[str]:
    """Changed paths when `suite` is a strict descendant of `pinned` touching only e2e/**; else ValueError.

    `compare` is GitHub's `repos/{REPO}/compare/{pinned}...{suite}` answer. The gate reads it itself; a
    truncated or paginated file list, a non-descendant, an empty diff or any path outside e2e/ refuses.
    """
    if not (re.fullmatch(r'[0-9a-f]{40}', pinned) and re.fullmatch(r'[0-9a-f]{40}', suite)) or pinned == suite:
        raise ValueError('suite exception needs two distinct full Console commit SHAs')
    if (compare.get('status') != 'ahead' or compare.get('behind_by') != 0
            or (compare.get('base_commit') or {}).get('sha') != pinned
            or not isinstance(compare.get('ahead_by'), int) or compare['ahead_by'] < 1):
        raise ValueError('suite revision is not a descendant of the pinned Console commit')
    if (compare.get('merge_base_commit') or {}).get('sha') != pinned:
        raise ValueError('suite revision is not a descendant of the pinned Console commit')
    commits = compare.get('commits') or []
    if not commits or commits[-1].get('sha') != suite:
        raise ValueError('suite compare does not end at the requested suite revision')
    files = compare.get('files')
    if not isinstance(files, list) or not files or len(files) >= 300 or compare.get('total_commits', 0) > 250:
        raise ValueError('suite diff file list is empty or truncated')
    paths = []
    for item in files:
        for key in ('filename', 'previous_filename'):
            name = item.get(key)
            if name is None and key == 'previous_filename':
                continue
            if not isinstance(name, str) or not name.startswith(SUITE_EXCEPTION_PREFIX) or '..' in name.split('/'):
                raise ValueError('suite revision changes a path outside e2e/: ' + str(name))
            paths.append(name)
    return sorted(set(paths))


def validate_evidence(pins: dict, runtime: dict, results: dict, progress: dict, suite_sha: str | None = None) -> None:
    for component, selected in pins.items():
        observed = runtime.get(component, {})
        if observed.get('requested') != selected['image'] or observed.get('sourceRevision') != selected['sha']:
            raise ValueError(f'{component} runtime does not match the manifest')
    if runtime.get('suiteSourceRevision') != (suite_sha or pins['console']['sha']):
        raise ValueError('suite did not run at the manifest-pinned Console revision'
                         + (' or the approved e2e-only suite revision' if suite_sha else ''))
    stats = results.get('stats', {})
    if not isinstance(stats.get('expected'), int) or stats['expected'] < 1:
        raise ValueError('no passing customer tests')
    if any(stats.get(key) != 0 for key in ('unexpected', 'flaky', 'skipped')):
        raise ValueError('customer tests failed, retried or skipped')
    if progress.get('qualificationComplete') is not True or not REQUIRED_STAGES.issubset(progress.get('completed', [])):
        raise ValueError('customer journey is partial')


def gh(*args: str, json_output: bool = False):
    result = subprocess.run(['gh', *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if json_output else result.stdout


def dispatch_customer(pins: dict, dispatch_id: str, suite_sha: str | None = None) -> None:
    # GitHub workflow dispatch accepts a branch or tag, not a raw commit SHA.
    # A fresh tag per dispatch avoids moving or trusting an existing ref.
    tag = f'studio-customer-gate/{dispatch_id}'
    ref = f'refs/tags/{tag}'
    sha = suite_sha or pins['console']['sha']
    gh('api', '--method', 'POST', f'repos/{REPO}/git/refs', '-f', f'ref={ref}', '-f', f'sha={sha}')
    target = gh('api', f'repos/{REPO}/git/ref/tags/{tag}', json_output=True).get('object', {})
    if target.get('type') != 'commit' or target.get('sha') != sha:
        raise ValueError('Console dispatch tag does not resolve to the pinned (or approved suite) commit')
    gh('workflow', 'run', WORKFLOW, '--repo', REPO, '--ref', ref,
       '-f', 'suite=studio-customer', '-f', f'dispatch_id={dispatch_id}',
       '-f', f'console_image={pins["console"]["image"]}', '-f', f'server_image={pins["server"]["image"]}')


def one_file(root: Path, name: str) -> dict:
    files = list(root.rglob(name))
    if len(files) != 1:
        raise ValueError(f'expected exactly one {name}, observed {len(files)}')
    return json.loads(files[0].read_text())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default='platform-manifest.yaml')
    parser.add_argument('--platform-lock', required=True)
    parser.add_argument('--lock-file', default='platform-lock.json')
    parser.add_argument('--out', default='out/studio-customer')
    parser.add_argument('--suite-sha', default='',
                        help='Console suite revision; empty = the pinned Console sha. A different sha must pass '
                             'suite_exception (descendant, e2e/-only diff); the image stays the pinned one.')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.platform_lock):
        parser.error('--platform-lock must identify the candidate platform-lock.json')
    manifest_bytes = Path(args.manifest).read_bytes()
    pins = resolve_pins(yaml.safe_load(manifest_bytes), STUDIO_COMPONENTS)
    validate_lock(Path(args.lock_file).read_bytes(), args.platform_lock, manifest_bytes, pins)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dispatch_id = 'release-' + uuid.uuid4().hex
    report = {'status': 'fail', 'certifying': False, 'schemaVersion': 'honua.studio-customer-gate/v1',
              'serverSha': pins['server']['sha'], 'serverDigest': pins['server']['digest'],
              'platformLockDigest': args.platform_lock,
              'manifestDigest': 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest(),
              'consoleSha': pins['console']['sha'], 'consoleDigest': pins['console']['digest'],
              'dispatchId': dispatch_id, 'suiteSha': pins['console']['sha']}
    suite_sha = None
    try:
        if args.suite_sha and args.suite_sha != pins['console']['sha']:
            compare = gh('api', f'repos/{REPO}/compare/{pins["console"]["sha"]}...{args.suite_sha}', json_output=True)
            changed = suite_exception(pins['console']['sha'], args.suite_sha, compare)
            suite_sha = args.suite_sha
            report.update(suiteSha=suite_sha, suiteException={
                'ruling': SUITE_EXCEPTION_RULING, 'pinnedConsoleSha': pins['console']['sha'],
                'suiteSha': suite_sha, 'changedPaths': changed})
        dispatch_customer(pins, dispatch_id, suite_sha)
        deadline = time.monotonic() + 2700
        run = None
        while time.monotonic() < deadline:
            runs = gh('api', f'repos/{REPO}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100', json_output=True)
            matches = [item for item in runs['workflow_runs'] if item.get('display_title', '').endswith(dispatch_id)]
            if len(matches) > 1:
                raise ValueError('ambiguous dispatch correlation')
            if matches:
                run = matches[0]
                if run['head_sha'] != (suite_sha or pins['console']['sha']):
                    raise ValueError('dispatch ran a different Console source revision')
                if run['status'] == 'completed':
                    break
            time.sleep(15)
        else:
            raise ValueError('Console workflow exceeded the gate deadline')
        report['consoleRunUrl'] = run['html_url']
        if run['conclusion'] != 'success':
            raise ValueError('Console customer workflow did not pass: ' + str(run['conclusion']))
        evidence = out / 'console-evidence'
        gh('run', 'download', str(run['id']), '--repo', REPO, '--name', 'studio-customer-journey-evidence', '--dir', str(evidence))
        # Progress is a Playwright attachment, not an unverified workflow output.
        results = one_file(evidence, 'playwright-results.json')
        progress_attachments = []
        def visit(value):
            if isinstance(value, dict):
                if value.get('name') == 'customer-journey-progress.json' and value.get('body'):
                    import base64
                    progress_attachments.append(json.loads(base64.b64decode(value['body'])))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(results)
        if len(progress_attachments) != 1:
            raise ValueError('missing unique inline customer journey progress attachment')
        validate_evidence(pins, one_file(evidence, 'runtime-identity.json'), results, progress_attachments[0], suite_sha)
        report.update(status='pass', certifying=True)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        report['reason'] = str(exc)
    finally:
        report['evaluatedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out / 'studio-customer-gate-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
