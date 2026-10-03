#!/usr/bin/env python3
"""Resolve green trunk sources and registry identities, without building or publishing.

A discovery tag is only a lookup key. Candidate images are repo@digest, with every
linux architecture and its source revision checked against registry config bytes.
SDK source pins name the published primary package's source, never an unpublished head.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
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

from jsonschema import Draft202012Validator
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'certification'))
import check_build_test as ci
from component_versions import version_map
import upgrade_lock_binding
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
        # gh colors JSON when its config asks for color, and json.loads then sees an empty token.
        env = os.environ.copy()
        env['NO_COLOR'] = '1'
        env['GH_FORCE_TTY'] = '0'
        # A 404 (a repository or package this token cannot see), an exhausted rate limit or any
        # other refusal stops this component; it never reads as an empty or green answer.
        try:
            result = retry(lambda: subprocess.run(
                ['gh', 'api', path], capture_output=True, text=True, check=True, env=env))
        except subprocess.CalledProcessError as exc:
            detail = ' '.join(str(exc.stderr or exc).split())
            raise ResolutionError(f'gh api {path} failed: {detail}') from exc
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ResolutionError(f'gh api {path} returned non-JSON ({exc})') from exc

    def pages(self, path, key=None):
        """Every row, or a refusal. Keyed pages must add up to their declared total_count."""
        separator = '&' if '?' in path else '?'
        page, seen, total = 1, 0, None
        while True:
            result = self.json(f'{path}{separator}per_page=100&page={page}')
            if key:
                rows = result.get(key) if isinstance(result, dict) else None
                count = result.get('total_count') if isinstance(result, dict) else None
                if not isinstance(rows, list) or not isinstance(count, int) or isinstance(count, bool):
                    raise ResolutionError(f'gh api {path} page {page} has no {key} list and total_count')
                if total is not None and count != total:
                    raise ResolutionError(f'gh api {path} total_count moved from {total} to {count} while paging')
                total = count
            else:
                rows = result
                if not isinstance(rows, list):
                    raise ResolutionError(f'gh api {path} page {page} is not a list')
            if not all(isinstance(row, dict) for row in rows):
                raise ResolutionError(f'gh api {path} page {page} contains a non-object row')
            seen += len(rows)
            yield from rows
            if len(rows) < 100:
                break
            page += 1
        if total is not None and seen != total:
            raise ResolutionError(f'gh api {path} returned {seen} of {total} {key}; refusing a truncated page')

    def commits(self, repository, limit):
        for index, row in enumerate(self.pages(f'repos/{repository}/commits?sha=trunk')):
            if index >= limit:
                break
            yield str(row.get('sha') or '')

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

    def candidate_tags(self, repository, sha):
        """Immutable per-SHA tags only. Channel tags (`latest`, `stable`, `nightly`, `2026.1`) never match."""
        exact = {f'nightly-{sha[:7]}', f'nightly-aot-{sha[:7]}'}
        prefix = f'candidate-{sha[:12]}-'
        return sorted(
            tag for tag in self.tags(repository)
            if 'lambda' not in tag and (tag in exact or tag.startswith(prefix))
        )

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
        # Pin policy binds the image by the registry index, each architecture digest, and the
        # config revision label. A GitHub attestation is a later gate, not a substitute for those bytes.
        return {'image': f'ghcr.io/{repository}@{digest}', 'digest': digest, 'platformDigests': children,
                'artifactSourceRevision': sha}

    def image(self, name, component, sha):
        repository = component['image'].removeprefix('ghcr.io/').split('@')[0].split(':')[0]
        reasons = []
        for tag in self.candidate_tags(repository, sha):
            try:
                result = self.identity(repository, tag, sha, component.get('architectures', ['amd64']))
                if component.get('awsLambdaImage'):
                    lambda_tag = f'nightly-lambda-aot-{sha[:7]}-amd64'
                    raw, _ = self.request(repository, 'manifests/' + lambda_tag, manifest=True)
                    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
                    child = self.document(repository, digest, manifest=True)
                    config = self.document(repository, child['config']['digest'])
                    if (config.get('config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != sha
                            or config.get('architecture') != 'amd64' or config.get('os') != 'linux'):
                        raise ResolutionError('Lambda image is not bound to candidate source')
                    existing = str(component.get('awsLambdaEcrDigest') or '')
                    # A moved source pin cannot keep yesterday's mirror digest (honua-release#99).
                    ecr = existing if component.get('sha') == sha and (
                        existing == 'pending-ecr-mirror' or DIGEST.fullmatch(existing)) else 'pending-ecr-mirror'
                    result.update(awsLambdaImage=f'ghcr.io/{repository}@{digest}', awsLambdaDigest=digest,
                                  awsLambdaEcrDigest=ecr)
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
        if component.get('image'):
            image_repository = component['image'].removeprefix('ghcr.io/').split('@')[0].split(':')[0]
            if registry is None or not registry.candidate_tags(image_repository, sha):
                reasons.append(f'{sha}: no published SHA-bound image')
                continue
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


SDK_COMPONENTS = {'honua-sdk-dotnet', 'honua-sdk-js', 'honua-sdk-python'}


def select_sdk(name, component, artifacts, identities, github):
    """Bind a primary package by coordinate and repository, not by client row name.

    Companion packages can have different published source revisions. Their identities
    remain in clientArtifacts; they cannot choose the source checkout for the primary SDK.
    """
    repository = component['repository'].removeprefix('https://github.com/')
    ecosystem, separator, package = str(component.get('artifact') or '').partition(':')
    if not separator or ecosystem not in {'npm', 'pypi', 'nuget'} or not package:
        raise ResolutionError(f'{name}: no primary published package coordinate')
    matches = [(client, artifact) for client, artifact in artifacts.items()
               if isinstance(artifact, dict) and artifact.get('ecosystem') == ecosystem
               and artifact.get('package') == package]
    if len(matches) != 1:
        raise ResolutionError(f'{name}: primary package {ecosystem}:{package} must have exactly one clientArtifacts row')
    client, artifact = matches[0]
    if str(artifact.get('repository') or '').removeprefix('https://github.com/') != repository:
        raise ResolutionError(f'{name}: primary package repository does not match the component')
    identity = identities.get(client)
    if not identity:
        raise ResolutionError(f'{name}: primary package {client} was not verified as published')
    sha = identity['sourceRevision']
    green, why = github.green(name, repository, sha)
    if not green:
        raise ResolutionError(f'{name}: published source {sha}: CI {why}')
    return {**component, 'sha': sha, 'artifactVersion': identity['version'],
            'artifactSourceRevision': sha, 'artifactSha256': identity['sha256']}


# Each component repository declares its own contract and schema versions in this file
# (schemas/component-versions.v1.schema.json, docs/COMPONENT-VERSION-DECLARATIONS.md). It is read
# at the revision the candidate pins, so a version map in the manifest is never a hand value.
COMPONENT_VERSIONS_PATH = 'release/component-versions.json'
COMPONENT_VERSIONS_SCHEMA = ROOT / 'schemas' / 'component-versions.v1.schema.json'
# Schema versions the resolver derives itself; the component declares every other one.
DERIVED_SCHEMA_VERSIONS = {'honua-server': 'database'}


def _unique_keys(pairs):
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ResolutionError('duplicate key ' + ', '.join(map(repr, duplicates)))
    return dict(pairs)


def component_versions(github, name, component):
    """The declared {contractVersions, schemaVersions} at the component's pinned sha, or a refusal.

    A missing, unreadable or invalid declaration refuses the component. An explicit empty map is a
    declaration only where the manifest marks the component sourcePinnedOnly.
    """
    if 'sourcePinnedOnly' in component and not isinstance(component['sourcePinnedOnly'], bool):
        raise ResolutionError(f'{name}: sourcePinnedOnly must be a boolean')
    repository = str(component.get('repository') or '').removeprefix('https://github.com/')
    sha = str(component.get('sha') or '')
    if not SHA.fullmatch(sha):
        raise ResolutionError(f'{name}: no immutable revision to read {COMPONENT_VERSIONS_PATH} at')
    where = f'{name}: {repository}@{sha}:{COMPONENT_VERSIONS_PATH}'
    try:
        raw = github.file(repository, sha, COMPONENT_VERSIONS_PATH)
    except (KeyError, TypeError, ValueError) as exc:
        raise ResolutionError(f'{where} is missing or unreadable: {exc}') from exc
    try:
        declaration = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ResolutionError(f'{where} is not a JSON document: {exc}') from exc
    schema = json.loads(COMPONENT_VERSIONS_SCHEMA.read_text(encoding='utf-8'))
    errors = sorted(Draft202012Validator(schema).iter_errors(declaration), key=lambda e: list(e.absolute_path))
    if errors:
        raise ResolutionError(f'{where} does not match {COMPONENT_VERSIONS_SCHEMA.name}: ' + '; '.join(
            f"{'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}" for error in errors[:5]))
    if declaration['component'] != name:
        raise ResolutionError(f"{where} declares component {declaration['component']!r}, not {name!r}")
    source_pinned = component.get('sourcePinnedOnly') is True
    declared = {}
    for group in ('contractVersions', 'schemaVersions'):
        derived = group == 'schemaVersions' and name in DERIVED_SCHEMA_VERSIONS
        try:
            declared[group] = version_map(declaration[group], allow_empty=source_pinned or derived)
        except ValueError as exc:
            hint = '' if declaration[group] else ' (an empty map is permitted only for a sourcePinnedOnly component)'
            raise ResolutionError(f'{where}: {group} {exc}{hint}') from exc
    return declared


def migration_tree(github, repository, sha):
    """Every migration script path in the selected source tree, or a refusal."""
    tree = github.json(f'repos/{repository}/git/trees/{sha}?recursive=1')
    if not isinstance(tree, dict):
        raise ResolutionError(f'{repository}@{sha}: migration tree was not an object')
    if tree.get('truncated'):
        raise ResolutionError(f'{repository}@{sha}: migration tree truncated; refusing to guess the migration set')
    return [path for path in (str((row or {}).get('path') or '') for row in tree.get('tree') or [])
            if '/Migrations/' in path and path.endswith('.sql')]


def migration_floor(paths, repository, sha):
    numbers = []
    for path in paths:
        match = re.match(r'(\d+)_', path.rsplit('/', 1)[-1])
        if match:
            numbers.append(int(match.group(1)))
    if not numbers:
        raise ResolutionError(f'{repository}@{sha}: no numbered migration; refusing to guess dbSchema')
    return str(max(numbers))


# The journal a lock declares is the one a default deployment records in public.schema_versions:
# DbUp names each embedded script `Honua.Server.Migrations.<file>` (EmbeddedResource
# `Migrations\*.sql`, not recursive). Two script sets are conditional and outside it:
# - `Honua.Postgres.Migrations.*` (src/Honua.Db/Postgres/Migrations) runs only where the optional
#   postgis_raster extension is provisioned; the upgrade gate's PostGIS image does not install it.
# - the configured-schema adoption script runs only for a non-default Database:Schema.
SERVER_MIGRATION_ROOT = 'src/Honua.Server/Migrations/'
SERVER_MIGRATION_RESOURCE = 'Honua.Server.Migrations.'
CONFIGURED_SCHEMA_ADOPTION = 'Honua.Server.Migrations.109_AdoptConfiguredGuardedSchema.sql'
CONFIGURED_SCHEMA_ADOPTION_DECLARATION = 'src/Honua.Server/Startup/ServerCoreSchemaMigrations.cs'


def migration_journal(github, paths, repository, sha):
    """The exact default-deployment journal (script names) declared by the selected server source."""
    names = [SERVER_MIGRATION_RESOURCE + path.removeprefix(SERVER_MIGRATION_ROOT) for path in paths
             if path.startswith(SERVER_MIGRATION_ROOT) and '/' not in path.removeprefix(SERVER_MIGRATION_ROOT)]
    if CONFIGURED_SCHEMA_ADOPTION not in names:
        raise ResolutionError(f'{repository}@{sha}: {CONFIGURED_SCHEMA_ADOPTION} is gone; '
                              'refusing to guess the default-deployment migration set')
    declaration = github.file(repository, sha, CONFIGURED_SCHEMA_ADOPTION_DECLARATION).decode('utf-8')
    if f'"{CONFIGURED_SCHEMA_ADOPTION}"' not in declaration:
        raise ResolutionError(f'{repository}@{sha}: {CONFIGURED_SCHEMA_ADOPTION_DECLARATION} no longer names '
                              f'{CONFIGURED_SCHEMA_ADOPTION}; refusing to guess the default-deployment migration set')
    return [name for name in names if name != CONFIGURED_SCHEMA_ADOPTION]


def resolve(manifest, matrix, github, registry, limit=100):
    candidate, candidate_matrix = copy.deepcopy(manifest), copy.deepcopy(matrix)
    failures = []
    # Verify once, before selecting SDK checkouts or reading declarations. A component
    # identity must come from verified package bytes, even when another gate later refuses.
    identities = verify_manifest(candidate, include_identities=True,
        github_token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))

    def declare(name, selected):
        # A version map carried in the manifest never survives: the declaration at the pinned
        # sha replaces it, or the component has none and the night refuses.
        for group in ('contractVersions', 'schemaVersions'):
            selected.pop(group, None)
        try:
            selected.update(component_versions(github, name, selected))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            failures.append(str(exc) if str(exc).startswith(f'{name}:') else f'{name}: {exc}')

    for name, component in manifest['components'].items():
        try:
            candidate['components'][name] = (
                select_sdk(name, component, candidate.get('clientArtifacts') or {}, identities, github)
                if name in SDK_COMPONENTS else select_component(name, component, github, registry, limit))
            selected = candidate['components'][name]
            image = selected.get('image')
            print(f"RESOLVED {name} {selected['sha']}" + (f" {image}" if image else ''))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            detail = str(exc)
            if not detail.startswith(f'{name}:'):
                detail = f'{name}: {detail}'
            failures.append(detail)
            continue
        declare(name, selected)
    # Experimental rows are not selected from trunk; they declare at the sha the manifest pins.
    for name, selected in (candidate.get('experimental') or {}).items():
        declare(name, selected)
    if failures:
        raise ResolutionError('\n'.join(failures))
    server_component = candidate['components']['honua-server']
    server = server_component['sha']
    original = manifest['components']['honua-server']['sha']
    candidate['candidate'] = {'ref': server, 'refSource': 'trunk'}
    certification = candidate['protocolCertification']
    certification['serverCertificationProducerSha'] = server
    now = datetime.now(timezone.utc)
    certification['candidateCutAt'] = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    candidate['snapshotDate'] = now.strftime('%Y-%m-%d')
    if server != original:
        # A bound ledger for yesterday's image cannot certify tonight's image.
        certification['ledger']['status'] = 'pending'
    # Schema floor and migration journal are read from the selected tree on every night; a hand
    # value (or a value left over from another sha) never survives into the candidate.
    server_component.pop('migrationJournalSha256', None)
    try:
        repository = server_component['repository'].removeprefix('https://github.com/')
        paths = migration_tree(github, repository, server)
        floor = migration_floor(paths, repository, server)
        journal = migration_journal(github, paths, repository, server)
        server_component['migrationJournalSha256'] = upgrade_lock_binding.journal_digest(journal)
        server_component['dbSchema'] = floor
        server_component['schemaVersions'][DERIVED_SCHEMA_VERSIONS['honua-server']] = floor
        data = candidate_matrix.setdefault('data', {}).setdefault('honua-server', {})
        if 'requiresDbSchema' in data and not str(data['requiresDbSchema']).startswith('>'):
            data['requiresDbSchema'] = floor
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        failures.append(f'honua-server migrations: {exc}')
    for name in ('honua-iac', 'honua-helm'):
        row = candidate_matrix.get('deploy', {}).get(name, {})
        for key in ('deploysServerImage', 'appVersion'):
            if key in row:
                row[key] = 'sha:' + server
    mcp = candidate['components'].get('geospatial-mcp', {})
    declaration = candidate.get('platformLockEvidence', {}).get('contentDigests', {}).get('geospatialMcp')
    if declaration and mcp:
        declaration.update(revision=mcp['sha'], sha256=mcp.get('artifactSha256'))
    if certification['ledger'].get('status') != 'bound':
        failures.append(
            'protocolCertification.ledger: no bound ledger for the selected honua-server '
            f'{server}; exact-candidate refuses an unbound ledger')
    if failures:
        # Local qualification still runs so the refusal names every exact-candidate error.
        # Reachability and registry client probes are not a passing claim on this path.
        findings = validate_platform.validate(candidate, candidate_matrix, None, exact_candidate=True)
        failures.extend(findings.errors)
        raise ResolutionError('candidate qualification refused:\n' + '\n'.join(failures))
    findings = validate_platform.validate(candidate, candidate_matrix, None,
        exact_candidate=True, reachability_client=github)
    evidence_path = ROOT / 'certification' / 'conformance-evidence.yaml'
    if evidence_path.exists():
        validate_platform.check_legacy_evidence_pin_coherence(
            candidate, yaml.safe_load(evidence_path.read_text()), findings)
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
