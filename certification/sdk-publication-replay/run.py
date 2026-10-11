#!/usr/bin/env python3
"""Diagnose published SDK 0.1.15 and MCP 0.1.16 against a temporary candidate.

This driver does not edit pins, expected matrix outcomes, or release locks. A gate
failure is retained as failure; it never creates a certification claim itself.
This diagnostic is not a release pin change or a promotion receipt.
"""
import argparse
import json
import os
import signal
import socket
import time
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request
import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--release-root', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--manifest', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--evidence-uri', required=True)
parser.add_argument('--mode', choices=['preflight', 'query', 'docs', 'all'], default='preflight')
parser.add_argument('--port', type=int, default=19812)
parser.add_argument('--project', default='honua-sdk-015-replay')
args = parser.parse_args()
root = args.release_root.resolve()
manifest_path = args.manifest.resolve()
output = args.output.resolve()
manifest = yaml.safe_load(manifest_path.read_text())
expected = {
    'honua-sdk-js': '0.1.15', 'honua-mcp-server': '0.1.16',
    'honua-sdk': '0.1.15', 'honua-sdk-esri-compat': '0.1.15',
    'honua-react': '0.1.15', 'honua-geometry': '0.1.15',
    'honua-app-platform': '0.1.15', 'create-honua-app': '0.1.8',
}
observations = []
provenance_only_clients = {'honua-sdk', 'honua-sdk-esri-compat', 'honua-react',
                           'honua-geometry', 'honua-app-platform'}
for key, version in expected.items():
    pin = manifest['clientArtifacts'][key]
    if pin['version'] != version or pin.get('publicationState') != 'published':
        raise SystemExit(f'{key}: manifest must name actually published {version}')
    url = 'https://registry.npmjs.org/' + urllib.parse.quote(pin['package'], safe='') + '/' + version
    with urllib.request.urlopen(url, timeout=30) as response:
        metadata = json.load(response)
    git_head = metadata.get('gitHead')
    if (metadata['version'] != version
            or (git_head is not None and git_head != pin['sourceSha'])
            or (git_head is None and key not in provenance_only_clients)):
        raise SystemExit(f'{key}: public registry source/version differs from manifest')
    if metadata['dist']['integrity'] != pin['integrity']:
        raise SystemExit(f'{key}: public registry integrity differs from manifest')
    if metadata['dist']['tarball'].rsplit('/', 1)[-1] != pin['filename']:
        raise SystemExit(f'{key}: public registry filename differs from manifest')
    observations.append({'client': key, 'package': pin['package'], 'version': version,
                         'sourceSha': pin['sourceSha'], 'registryGitHead': git_head,
                         'integrity': metadata['dist']['integrity'],
                         'tarball': metadata['dist']['tarball']})
with urllib.request.urlopen('https://registry.npmjs.org/create-honua-app/latest', timeout=30) as response:
    scaffold_latest = json.load(response)
if scaffold_latest['version'] != expected['create-honua-app']:
    raise SystemExit('Documented create-honua-app@latest does not resolve the selected scaffold cut')
sdk = manifest['clientArtifacts']['honua-sdk-js']
if manifest['components']['honua-sdk-js']['sha'] != sdk['sourceSha']:
    raise SystemExit('Primary SDK component source differs from published SDK pin')
if manifest['components']['honua-sdk-js']['version'] != sdk['version']:
    raise SystemExit('Primary SDK component version differs from published SDK pin')
if manifest['evidenceSources']['sdk']['producerSha'] != sdk['sourceSha']:
    raise SystemExit('SDK evidence producer source differs from published SDK pin')
output.mkdir(parents=True, exist_ok=False)
(output / 'public-registry-observations.json').write_text(json.dumps(observations, indent=2) + '\n')
env = dict(os.environ)
env.update(E2E_SERVER_PORT=str(args.port), HONUA_SERVER_URL=f'http://localhost:{args.port}',
           E2E_BASE=f'http://localhost:{args.port}', HONUA_PUBLIC_BASE_URL=f'http://localhost:{args.port}')

results = {}

def run(command, label, extra_env=None, cwd=None):
    with (output / f'{label}.log').open('w') as log:
        result = subprocess.run(command, cwd=cwd or root, env={**env, **(extra_env or {})},
                                stdout=log, stderr=subprocess.STDOUT)
    print(f'{label}: exit {result.returncode}; {output / (label + ".log")}', flush=True)
    results[label] = result.returncode
    (output / 'gate-exit-status.json').write_text(json.dumps(results, indent=2) + '\n')
    return result.returncode

# This mandatory verifier downloads the bytes and verifies signed publisher
# provenance for the allowlisted split packages whose registry lacks gitHead.
code = run([sys.executable, 'tools/verify_client_artifacts.py', '--manifest', str(manifest_path),
            '--ecosystem', 'npm'], 'published-client-bytes')
if code:
    raise SystemExit(code)
if args.mode in ('docs', 'all'):
    # Independent public-registry/default-peer proof, outside the replay guard.
    # The canonical guard below adds exact-candidate confinement, not a substitute
    # registry that is allowed to be the only evidence a documented command works.
    public = output / 'public-consumer'
    public.mkdir()
    public_env = {'npm_config_registry': 'https://registry.npmjs.org',
                  'npm_config_userconfig': '/dev/null', 'npm_config_yes': 'true',
                  'npm_config_force': 'false', 'npm_config_legacy_peer_deps': 'false',
                  'npm_config_cache': str(output / 'public-npm-cache')}
    for command, label, cwd in (
        (['npm', 'create', 'honua-app@latest', 'my-map'],
         'public-scaffold', public),
        (['npm', 'install'], 'public-default-install', public / 'my-map'),
        (['npm', 'run', 'build'], 'public-scaffold-build', public / 'my-map'),
    ):
        code = run(command, label, public_env, cwd)
        results[label] = code
        if code:
            raise SystemExit(code)
    consumer = public / 'my-map'
    lock = json.loads((consumer / 'package-lock.json').read_text())
    installed = lock['packages']['node_modules/@honua/sdk-js']
    if installed['version'] != sdk['version'] or installed['integrity'] != sdk['integrity']:
        raise SystemExit('Public scaffold resolved SDK bytes different from the manifest')
    # Use the documented unmodified dev command and fail if its port is occupied.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 5173))
    with (output / 'public-dev-readiness.log').open('w') as log:
        process = subprocess.Popen(['npm', 'run', 'dev'], cwd=consumer,
                                   env={**env, **public_env}, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        ready = False
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen('http://localhost:5173/', timeout=2) as response:
                        ready = (response.status == 200 and bool(response.read())
                                 and 'http://localhost:5173/' in (output / 'public-dev-readiness.log').read_text()
                                 and process.poll() is None)
                    if ready:
                        break
                except OSError:
                    pass
                time.sleep(0.25)
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=15)
    (output / 'public-dev-readiness.json').write_text(json.dumps({
        'command': ['npm', 'run', 'dev'], 'url': 'http://localhost:5173/',
        'ready': ready, 'sdk': installed,
    }, indent=2) + '\n')
    results['public-dev-readiness'] = 0 if ready else 1
    if not ready:
        raise SystemExit('Published documented dev command did not become ready')
if args.mode in ('query', 'all'):
    # Preserve the complete installed-client denominator. Existing unrelated
    # failures remain visible and do not invalidate a narrower raw query result.
    results['installed-clients'] = run([
        sys.executable, 'certification/installed-clients/run.py', '--manifest', str(manifest_path),
        '--live', '--output', str(output / 'installed-clients.json'), '--evidence-uri', args.evidence_uri,
    ], 'installed-clients', {'COMPOSE_PROJECT_NAME': args.project + '-queries'})
if args.mode in ('docs', 'all'):
    inventory = output / 'executable-docs-inventory.json'
    code = run([sys.executable, 'certification/executable-docs/inventory.py', '--manifest', str(manifest_path),
                '--inventory', str(inventory), '--write'], 'docs-inventory')
    results['docs-inventory'] = code
    if not code:
        docs_command = [sys.executable, 'certification/executable-docs/run.py',
                        '--manifest', str(manifest_path), '--inventory', str(inventory),
                        '--boot', '--network', 'candidate', '--output', str(output / 'executable-docs.json'),
                        '--summary', str(output / 'executable-docs-summary.md'),
                        '--workdir', str(output / 'docs-work'), '--evidence-uri', args.evidence_uri]
        for document in ('honua-sdk-js-readme', 'honua-sdk-js-install', 'honua-sdk-js-docs-quickstart',
                         'honua-sdk-js-mcp-readme', 'honua-sdk-js-packages-create-honua-app-readme'):
            docs_command.extend(['--only', document])
        results['executable-docs'] = run(docs_command, 'executable-docs',
                                          {'COMPOSE_PROJECT_NAME': args.project + '-docs',
                                           'HONUA_PUBLIC_BASE_URL': 'http://localhost:8080'})
(output / 'gate-exit-status.json').write_text(json.dumps(results, indent=2) + '\n')
raise SystemExit(1 if any(results.values()) else 0)
