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

REQUIRED_STAGES = frozenset({
    'publisher-auth', 'import-publish-customer-regions.geojson', 'import-publish-customer-sites.csv',
    'two-layers-filter-widget', 'session-expiry-recovery', 'authored-style-labels', 'save-reload-reopen',
    'independent-server-render', 'separate-approval', 'public-audience', 'accessibility',
})
REPO = 'honua-io/honua-console'
WORKFLOW = 'console-live-journey.yml'


def resolve_pins(manifest: dict) -> dict:
    result = {}
    for component in ('console', 'server'):
        pin = manifest['components'][f'honua-{component}']
        digest, sha = pin.get('digest', ''), pin.get('sha', '')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError(f'{component} requires an immutable digest and source SHA')
        image = pin.get('image', '')
        prefix = rf'ghcr\.io/honua-io/honua-{component}(?::[A-Za-z0-9_.-]+)?'
        if not re.fullmatch(prefix + r'(?:@sha256:[0-9a-f]{64})?', image):
            raise ValueError(f'{component} must use its own GHCR repository')
        if '@' in image and image.split('@')[1] != digest:
            raise ValueError(f'{component} image disagrees with the manifest digest')
        result[component] = {'image': image.split('@')[0] + '@' + digest, 'sha': sha, 'digest': digest}
    return result


def validate_lock(raw: bytes, expected_digest: str, manifest: bytes, pins: dict) -> None:
    if 'sha256:' + hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError('platform lock bytes do not match the supplied digest')
    lock = json.loads(raw)
    if lock.get('sourceInputs', {}).get('platformManifest', {}).get('sha256') != 'sha256:' + hashlib.sha256(manifest).hexdigest():
        raise ValueError('platform lock does not bind this manifest')
    for name, pin in pins.items():
        artifacts = lock.get('components', {}).get(f'honua-{name}', {}).get('artifacts', [])
        images = [item for item in artifacts if isinstance(item, dict) and item.get('kind') == 'image']
        if len(images) != 1:
            raise ValueError(f'platform lock must contain exactly one {name} image')
        image = images[0]
        repository = pin['image'].split('@')[0].rsplit(':', 1)[0]
        if (image.get('coordinate', '').removeprefix('oci://') != repository or
                image.get('digest') != pin['digest'] or image.get('sourceRevision') != pin['sha']):
            raise ValueError(f'platform lock {name} image differs from manifest')


def validate_evidence(pins: dict, runtime: dict, results: dict, progress: dict) -> None:
    for component, selected in pins.items():
        observed = runtime.get(component, {})
        if observed.get('requested') != selected['image'] or observed.get('sourceRevision') != selected['sha']:
            raise ValueError(f'{component} runtime does not match the manifest')
    if runtime.get('suiteSourceRevision') != pins['console']['sha']:
        raise ValueError('suite did not run at the manifest-pinned Console revision')
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


def dispatch_customer(pins: dict, dispatch_id: str) -> None:
    # GitHub workflow dispatch accepts a branch or tag, not a raw commit SHA.
    # A fresh tag per dispatch avoids moving or trusting an existing ref.
    tag = f'studio-customer-gate/{dispatch_id}'
    ref = f'refs/tags/{tag}'
    sha = pins['console']['sha']
    gh('api', '--method', 'POST', f'repos/{REPO}/git/refs', '-f', f'ref={ref}', '-f', f'sha={sha}')
    target = gh('api', f'repos/{REPO}/git/ref/tags/{tag}', json_output=True).get('object', {})
    if target.get('type') != 'commit' or target.get('sha') != sha:
        raise ValueError('Console dispatch tag does not resolve to the pinned commit')
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
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.platform_lock):
        parser.error('--platform-lock must identify the candidate platform-lock.json')
    manifest_bytes = Path(args.manifest).read_bytes()
    pins = resolve_pins(yaml.safe_load(manifest_bytes))
    validate_lock(Path(args.lock_file).read_bytes(), args.platform_lock, manifest_bytes, pins)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dispatch_id = 'release-' + uuid.uuid4().hex
    report = {'status': 'fail', 'certifying': False, 'schemaVersion': 'honua.studio-customer-gate/v1',
              'serverSha': pins['server']['sha'], 'serverDigest': pins['server']['digest'],
              'platformLockDigest': args.platform_lock,
              'manifestDigest': 'sha256:' + hashlib.sha256(manifest_bytes).hexdigest(),
              'consoleSha': pins['console']['sha'], 'consoleDigest': pins['console']['digest'],
              'dispatchId': dispatch_id}
    try:
        dispatch_customer(pins, dispatch_id)
        deadline = time.monotonic() + 2700
        run = None
        while time.monotonic() < deadline:
            runs = gh('api', f'repos/{REPO}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100', json_output=True)
            matches = [item for item in runs['workflow_runs'] if item.get('display_title', '').endswith(dispatch_id)]
            if len(matches) > 1:
                raise ValueError('ambiguous dispatch correlation')
            if matches:
                run = matches[0]
                if run['head_sha'] != pins['console']['sha']:
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
        validate_evidence(pins, one_file(evidence, 'runtime-identity.json'), results, progress_attachments[0])
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
