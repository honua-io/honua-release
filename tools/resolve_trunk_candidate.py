#!/usr/bin/env python3
"""Resolve green trunk sources and registry identities, without building or publishing.

A discovery tag is only a lookup key. Candidate images are repo@digest, with every
linux architecture and its source revision checked against registry config bytes.
Client publication pins remain independent of their newest green source snapshot.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'certification'))
import check_build_test as ci
import validate_platform
from verify_client_artifacts import verify_manifest

SHA = re.compile(r'[0-9a-f]{40}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
DELAYS = (0, 10, 30, 60, 120, 60)


class ResolutionError(ValueError):
    pass


def retry(operation):
    for index, delay in enumerate(DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return operation()
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = (getattr(exc, 'stderr', '') or str(exc)).lower()
            transient = isinstance(exc, urllib.error.URLError) or any(term in detail for term in (
                'error connecting', 'could not resolve host', 'connection reset',
                'timeout', 'timed out', 'tls', '403'))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in {403, 429, 500, 502, 503, 504}
            if not transient or index == len(DELAYS) - 1:
                raise


class GitHub:
    def json(self, path):
        result = retry(lambda: subprocess.run(
            ['gh', 'api', path], capture_output=True, text=True, check=True))
        return json.loads(result.stdout)

    def pages(self, path, key=None):
        separator = '&' if '?' in path else '?'
        page = 1
        while True:
            result = self.json(f'{path}{separator}per_page=100&page={page}')
            rows = result[key] if key else result
            yield from rows
            if len(rows) < 100:
                break
            page += 1

    def commits(self, repository, limit):
        for index, row in enumerate(self.pages(f'repos/{repository}/commits?sha=trunk')):
            if index >= limit:
                break
            yield row['sha']

    def green(self, name, repository, sha):
        checks = list(self.pages(f'repos/{repository}/commits/{sha}/check-runs', 'check_runs'))
        runs = list(self.pages(f'repos/{repository}/actions/runs?head_sha={sha}', 'workflow_runs'))
        payload = ci._enrich_action_workflow_ids({'check_runs': checks}, {'workflow_runs': runs})
        payload['_workflow_runs'] = runs
        result = ci.evaluate({'components': {name: {'sha': sha}}}, lambda *_: payload,
                             enforcement='strict', env_gated=ci.load_env_gated(),
                             rollup=ci.load_rollup(), security=ci.load_security(),
                             governance=ci.load_governance(), full_matrix=ci.load_full_matrix())
        row = result['components'][0]
        return row['decided'] == 'pass', row['why']

    def file(self, repository, revision, path):
        response = self.json(f'repos/{repository}/contents/{path}?ref={revision}')
        return base64.b64decode(response['content'])


class Registry:
    def __init__(self, github):
        self.github = github
        self.tokens = {}
        self.tag_cache = {}

    def request(self, repository, suffix, *, manifest=False):
        if repository not in self.tokens:
            url = 'https://ghcr.io/token?' + urllib.parse.urlencode({
                'service': 'ghcr.io', 'scope': f'repository:{repository}:pull'})
            headers = {}
            token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
            if token:
                headers['Authorization'] = 'Basic ' + base64.b64encode(f'honua:{token}'.encode()).decode()
            req = urllib.request.Request(url, headers=headers)
            def auth():
                with urllib.request.urlopen(req, timeout=60) as response:
                    return json.load(response)['token']
            self.tokens[repository] = retry(auth)
        headers = {'Authorization': 'Bearer ' + self.tokens[repository]}
        if manifest:
            headers['Accept'] = ', '.join(('application/vnd.oci.image.index.v1+json',
                'application/vnd.docker.distribution.manifest.list.v2+json',
                'application/vnd.oci.image.manifest.v1+json',
                'application/vnd.docker.distribution.manifest.v2+json'))
        req = urllib.request.Request(f'https://ghcr.io/v2/{repository}/{suffix}', headers=headers)
        def read():
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read(), response.headers
        return retry(read)

    def tags(self, repository):
        if repository not in self.tag_cache:
            tags, suffix = [], 'tags/list?n=1000'
            while suffix:
                raw, headers = self.request(repository, suffix)
                tags.extend(json.loads(raw).get('tags') or [])
                link = headers.get('Link', '')
                match = re.search(r'<([^>]+)>;\s*rel="?next', link)
                suffix = match.group(1).split(f'/v2/{repository}/', 1)[-1] if match else ''
                if suffix and (suffix.startswith(('https:', 'http:')) or '..' in suffix):
                    raise ResolutionError('registry pagination escaped repository')
            self.tag_cache[repository] = tags
        return self.tag_cache[repository]

    def document(self, repository, digest, *, manifest=False):
        if not DIGEST.fullmatch(digest):
            raise ResolutionError('registry returned an invalid digest')
        raw, _ = self.request(repository, ('manifests/' if manifest else 'blobs/') + digest,
                              manifest=manifest)
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
            raise ResolutionError(f'registry bytes do not match {digest}')
        return json.loads(raw)

    def identity(self, repository, tag, sha, architectures):
        raw, headers = self.request(repository, 'manifests/' + tag, manifest=True)
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if headers.get('Docker-Content-Digest') != digest:
            raise ResolutionError('registry index digest differs from returned bytes')
        index = json.loads(raw)
        children = {}
        for descriptor in index.get('manifests', []):
            platform = descriptor.get('platform', {})
            if platform.get('os') != 'linux' or platform.get('architecture') not in architectures:
                continue
            arch = platform['architecture']
            if arch in children:
                raise ResolutionError(f'duplicate linux/{arch} manifest')
            child_digest = descriptor['digest']
            child = self.document(repository, child_digest, manifest=True)
            config = self.document(repository, child['config']['digest'])
            revision = config.get('config', {}).get('Labels', {}).get('org.opencontainers.image.revision')
            if revision != sha or config.get('architecture') != arch or config.get('os') != 'linux':
                raise ResolutionError(f'linux/{arch} config is not bound to {sha}')
            children[arch] = child_digest
        if set(children) != set(architectures):
            raise ResolutionError('published image missing architectures: ' + ', '.join(sorted(set(architectures)-children.keys())))
        reference = f'ghcr.io/{repository}@{digest}'
        retry(lambda: subprocess.run(['gh', 'attestation', 'verify', 'oci://' + reference,
            '--repo', repository, '--source-ref', 'refs/heads/trunk', '--source-digest', sha],
            capture_output=True, text=True, check=True))
        return {'image': reference, 'digest': digest, 'platformDigests': children,
                'artifactSourceRevision': sha}

    def image(self, name, component, sha):
        repository = component['image'].removeprefix('ghcr.io/').split('@')[0].split(':')[0]
        # Never inspect a channel tag. Console publishers use candidate-SHA-run-attempt;
        # server publishers use nightly-SHA. The full revision is checked in every child.
        prefixes = (f'nightly-{sha[:7]}', f'candidate-{sha[:12]}-')
        tags = sorted(t for t in self.tags(repository) if any(t.startswith(p) for p in prefixes))
        reasons = []
        for tag in tags:
            try:
                result = self.identity(repository, tag, sha, component.get('architectures', ['amd64']))
                if component.get('awsLambdaImage'):
                    lambda_tag = f'nightly-lambda-aot-{sha[:7]}-amd64'
                    raw, _ = self.request(repository, 'manifests/' + lambda_tag, manifest=True)
                    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
                    child = self.document(repository, digest, manifest=True)
                    config = self.document(repository, child['config']['digest'])
                    if config.get('config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != sha:
                        raise ResolutionError('Lambda image is not bound to candidate source')
                    result.update(awsLambdaImage=f'ghcr.io/{repository}@{digest}', awsLambdaDigest=digest,
                                  awsLambdaEcrDigest='pending-ecr-mirror')
                return result
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                reasons.append(f'{tag}: {exc}')
        raise ResolutionError('no published SHA-bound image' + (': ' + '; '.join(reasons) if reasons else f' for {sha}'))


def select_component(name, component, github, registry, limit):
    repository = component['repository'].removeprefix('https://github.com/')
    reasons = []
    for sha in github.commits(repository, limit):
        if not SHA.fullmatch(sha):
            raise ResolutionError(f'{name}: trunk returned a non-immutable revision')
        green, why = github.green(name, repository, sha)
        if not green:
            reasons.append(f'{sha}: CI {why}')
            continue
        selected = {**component, 'sha': sha}
        if component.get('image'):
            try:
                selected.update(registry.image(name, component, sha))
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                reasons.append(f'{sha}: {exc}')
                continue
        if str(component.get('artifact', '')).startswith('spec:'):
            path = component['artifact'].split('/blob/', 1)[1].split('/', 1)[1]
            data = github.file(repository, sha, path)
            selected.update(artifact=f'spec:https://github.com/{repository}/blob/{sha}/{path}',
                artifactSourceRevision=sha, artifactSha256='sha256:' + hashlib.sha256(data).hexdigest(),
                artifactVersion='1.0.0+' + sha[:8])
        return selected
    raise ResolutionError(f'{name}: no qualifying trunk commit in newest {limit} commits; ' + '; '.join(reasons))


def resolve(manifest, matrix, github, registry, limit=100):
    candidate, candidate_matrix = copy.deepcopy(manifest), copy.deepcopy(matrix)
    failures = []
    for name, component in manifest['components'].items():
        try:
            candidate['components'][name] = select_component(name, component, github, registry, limit)
            print(f"RESOLVED {name} {candidate['components'][name]['sha']}")
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            failures.append(str(exc))
    if failures:
        raise ResolutionError('\n'.join(failures))
    server = candidate['components']['honua-server']['sha']
    candidate['candidate'] = {'ref': server, 'refSource': 'trunk'}
    candidate['protocolCertification']['serverCertificationProducerSha'] = server
    if server != manifest['components']['honua-server']['sha']:
        # A bound ledger for yesterday's image cannot certify tonight's image.
        candidate['protocolCertification']['ledger']['status'] = 'pending'
    for name in ('honua-iac', 'honua-helm'):
        row = candidate_matrix.get('deploy', {}).get(name, {})
        for key in ('deploysServerImage', 'appVersion'):
            if key in row:
                row[key] = 'sha:' + server
    mcp = candidate['components'].get('geospatial-mcp', {})
    declaration = candidate.get('platformLockEvidence', {}).get('contentDigests', {}).get('geospatialMcp')
    if declaration:
        declaration.update(revision=mcp['sha'], sha256=mcp['artifactSha256'])
    # Client sourceSha describes already-published bytes; never advance it with source CI.
    verify_manifest(candidate, github_token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))
    findings = validate_platform.validate(candidate, candidate_matrix, None,
        exact_candidate=True, reachability_client=github)
    if findings.errors:
        raise ResolutionError('candidate qualification refused:\n' + '\n'.join(findings.errors))
    return candidate, candidate_matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'platform-manifest.yaml')
    parser.add_argument('--matrix', type=Path, default=ROOT / 'compatibility-matrix.yaml')
    parser.add_argument('--out-dir', type=Path, default=Path('resolved-candidate'))
    parser.add_argument('--max-commits', type=int, default=100)
    parser.add_argument('--dry-run', action='store_true', help='read live trunk/registries; publish nothing')
    args = parser.parse_args(argv)
    try:
        if args.max_commits < 1:
            raise ResolutionError('--max-commits must be positive')
        github = GitHub()
        manifest, matrix = resolve(yaml.safe_load(args.manifest.read_text()),
            yaml.safe_load(args.matrix.read_text()), github, Registry(github), args.max_commits)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for filename, value in [('platform-manifest.yaml', manifest), ('compatibility-matrix.yaml', matrix)]:
            (args.out_dir / filename).write_text(yaml.safe_dump(value, sort_keys=False))
        print(f'PASS: exact trunk candidate in {args.out_dir}; dry_run={args.dry_run}')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
