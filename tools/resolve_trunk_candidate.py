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
from platform_version import IMAGED_COMPONENTS, PRERELEASE, PUBLISHER
import sdk_baselines
from semver import parse as parse_semver
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
            detail = getattr(exc, 'stderr', '') or str(exc)
            if isinstance(detail, bytes):
                detail = detail.decode('utf-8', errors='replace')
            detail = detail.lower()
            transient = isinstance(exc, urllib.error.URLError) or any(term in detail for term in (
                'error connecting', 'could not resolve host', 'connection reset',
                'timeout', 'timed out', 'tls', '403'))
            if isinstance(exc, urllib.error.HTTPError):
                transient = exc.code in {403, 429, 500, 502, 503, 504}
            if not transient or index == len(DELAYS) - 1:
                raise


class GitHub:
    def __init__(self):
        self.commit_dates = {}

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
            sha = str(row.get('sha') or '')
            committed = ((row.get('commit') or {}).get('committer') or {}).get('date')
            if committed:
                self.commit_dates[sha] = committed
            yield sha

    def commit_date(self, sha):
        # Committer dates seen while listing trunk; the skip report reads them, nothing selects on them.
        return self.commit_dates.get(sha)

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
        """The exact blob bytes at `revision`, or a refusal; never a short or empty read."""
        response = self.json(f'repos/{repository}/contents/{path}?ref={revision}')
        if not isinstance(response, dict):
            raise ResolutionError(f'{repository}@{revision}:{path} is not a file')
        size, blob = response.get('size'), str(response.get('sha') or '')
        if response.get('encoding') == 'base64':
            raw = base64.b64decode(response.get('content') or '')
        else:
            # Over 1 MB the contents API answers encoding "none" and an empty content field; the
            # raw media type serves the blob itself (up to 100 MB).
            raw = self.raw(f'repos/{repository}/contents/{path}?ref={revision}')
        # The blob sha the API reports binds the bytes, so a truncated or substituted body refuses.
        actual = hashlib.sha1(b'blob %d\0' % len(raw) + raw).hexdigest()
        if len(raw) != size or actual != blob:
            raise ResolutionError(f'{repository}@{revision}:{path}: read {len(raw)} bytes (git blob {actual}), '
                                  f'not the {size} bytes of blob {blob or "(none)"}')
        return raw

    def raw(self, path):
        try:
            result = retry(lambda: subprocess.run(
                ['gh', 'api', path, '-H', 'Accept: application/vnd.github.raw+json'],
                capture_output=True, check=True))
        except subprocess.CalledProcessError as exc:
            detail = ' '.join((exc.stderr or b'').decode('utf-8', errors='replace').split()) or str(exc)
            raise ResolutionError(f'gh api {path} failed: {detail}') from exc
        return result.stdout


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


def select_component(name, component, github, registry, limit, skips=None):
    """`skips`, when given, receives every newer trunk commit passed over and why, whether or not
    a commit qualifies; a sha weeks behind its trunk head must never be selected silently."""
    repository = component['repository'].removeprefix('https://github.com/')
    reasons = []
    skipped = []

    def skip(sha, reason):
        reasons.append(f'{sha}: {reason}')
        skipped.append({'sha': sha, 'reason': reason})

    sha = None
    try:
        for sha in github.commits(repository, limit):
            if not SHA.fullmatch(sha):
                raise ResolutionError(f'{name}: trunk returned a non-immutable revision')
            if skips is not None and 'head' not in skips:
                skips.update(head=sha, skipped=skipped, selected=None)
            if component.get('image'):
                image_repository = component['image'].removeprefix('ghcr.io/').split('@')[0].split(':')[0]
                if registry is None or not registry.candidate_tags(image_repository, sha):
                    skip(sha, 'no published SHA-bound image')
                    continue
            green, why = github.green(name, repository, sha)
            if not green:
                skip(sha, f'CI {why}')
                continue
            selected = {**component, 'sha': sha}
            if component.get('image'):
                try:
                    selected.update(registry.image(name, component, sha))
                except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                    skip(sha, str(exc))
                    continue
            if str(component.get('artifact', '')).startswith('spec:'):
                path = component['artifact'].split('/blob/', 1)[1].split('/', 1)[1]
                data = github.file(repository, sha, path)
                selected.update(artifact=f'spec:https://github.com/{repository}/blob/{sha}/{path}',
                    artifactSourceRevision=sha, artifactSha256='sha256:' + hashlib.sha256(data).hexdigest(),
                    artifactVersion='1.0.0+' + sha[:8])
            if skips is not None:
                skips['selected'] = sha
            return selected
    except Exception as exc:
        # A read that raises mid-walk leaves older commits unexamined: the report names where and
        # why it stopped, never 'no qualifying trunk commit'.
        if skips is not None and 'head' in skips:
            # A sha already skipped was fully examined; the failure was listing the next one.
            at = None if skipped and skipped[-1]['sha'] == sha else sha
            skips['aborted'] = {'sha': at, 'reason': str(exc) or type(exc).__name__}
        raise
    raise ResolutionError(f'{name}: no qualifying trunk commit in newest {limit} commits; ' + '; '.join(reasons))


SKIP_REPORT_LIMIT = 10
STALE_DAYS = 3


def _commit_time(github, sha):
    value = sha and getattr(github, 'commit_date', lambda _: None)(sha)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None
    except ValueError:
        return None


def skip_report(name, skips, github, *, keep=SKIP_REPORT_LIMIT, stale_days=STALE_DAYS, now=None):
    """One component's bounded skip report: the newest `keep` skipped shas with their reason, how
    far the selected sha sits behind trunk head, and whether that is stale. Information only: R18
    mints what is certified, so staleness never refuses the night."""
    now = now or datetime.now(timezone.utc)
    skipped, selected, head = skips.get('skipped') or [], skips.get('selected'), skips.get('head')
    head_at, selected_at = _commit_time(github, head), _commit_time(github, selected)
    days = lambda delta: round(delta.total_seconds() / 86400, 1)
    # Staleness compares the unrounded lag: 73 hours is past a three-day threshold though it shows 3.0.
    lag = (head_at - selected_at).total_seconds() / 86400 if head_at and selected_at else None
    return {
        'component': name,
        'trunkHead': head,
        'selected': selected,
        'selectedCommittedAt': selected_at and selected_at.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'selectedAgeDays': days(now - selected_at) if selected_at else None,
        'commitsBehind': len(skipped) if selected else None,
        'daysBehind': None if lag is None else round(lag, 1),
        'staleAfterDays': stale_days,
        'stale': None if lag is None else lag > stale_days,
        'skippedTotal': len(skipped),
        'skipped': skipped[:keep],
        'aborted': skips.get('aborted'),
    }


def skip_report_lines(report):
    name, selected = report['component'], report['selected']
    if selected:
        age = report['selectedAgeDays']
        lines = [f"SKIPS {name}: selected {selected[:7]} "
                 f"({'age unknown' if age is None else f'{age} days old'}); "
                 f"{report['skippedTotal']} newer trunk commit(s) skipped"]
    elif report.get('aborted'):
        aborted = report['aborted']
        at = aborted['sha'][:7] if aborted['sha'] else 'trunk listing'
        lines = [f"SKIPS {name}: walk aborted at {at} ({aborted['reason']}); "
                 f"{report['skippedTotal']} newer commit(s) skipped before it; older commits not examined"]
    else:
        lines = [f"SKIPS {name}: no qualifying trunk commit; {report['skippedTotal']} commit(s) skipped"]
    lines += [f"  skipped {row['sha'][:7]}: {row['reason']}" for row in report['skipped']]
    if report['skippedTotal'] > len(report['skipped']):
        lines.append(f"  ... {report['skippedTotal'] - len(report['skipped'])} older skipped commit(s) not listed")
    if report['stale']:
        lines.append(f"STALE-CANDIDATE: {name} selected {selected[:7]} "
                     f"({report['commitsBehind']} commits, {report['daysBehind']} days behind)")
    return lines


def skip_report_markdown(reports):
    rows = ['### Trunk candidate selection', '',
            '| Component | Selected | Behind trunk head | Skipped | Newest skip reason |', '|---|---|---|---|---|']
    for report in reports.values():
        selected = report['selected']
        aborted = report.get('aborted')
        if aborted:
            where = f"walk aborted at {aborted['sha'][:7] if aborted['sha'] else 'trunk listing'}"
        elif not selected:
            where = 'none qualifies'
        elif report['daysBehind'] is None:
            where = f"{report['commitsBehind']} commits"
        else:
            where = f"{report['commitsBehind']} commits, {report['daysBehind']} days"
        if report['stale']:
            where = f'**STALE-CANDIDATE** {where}'
        reason = report['skipped'][0]['reason'] if report['skipped'] else ''
        if aborted:
            reason = f"aborted: {aborted['reason']}"
        reason = ' '.join(reason.split()).replace('|', '\\|')[:200]
        rows.append(f"| {report['component']} | {selected[:7] if selected else '-'} | {where} | "
                    f"{report['skippedTotal']} | {reason} |")
    return '\n'.join(rows) + '\n'


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
    return {**component, 'sha': sha, 'version': identity['version'], 'artifactVersion': identity['version'],
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

    Missing declarations for the source-only mobile/collect previews are empty sets (R39).
    Other missing, unreadable or invalid declarations refuse the component. Explicit empty maps
    are declarations only where the manifest marks the component sourcePinnedOnly.
    """
    if 'sourcePinnedOnly' in component and not isinstance(component['sourcePinnedOnly'], bool):
        raise ResolutionError(f'{name}: sourcePinnedOnly must be a boolean')
    repository = str(component.get('repository') or '').removeprefix('https://github.com/')
    sha = str(component.get('sha') or '')
    if not SHA.fullmatch(sha):
        raise ResolutionError(f'{name}: no immutable revision to read {COMPONENT_VERSIONS_PATH} at')
    where = f'{name}: {repository}@{sha}:{COMPONENT_VERSIONS_PATH}'
    preview = component.get('sourcePinnedOnly') is True and name in {'honua-mobile', 'honua-collect'}
    empty = {'contractVersions': {}, 'schemaVersions': {}}
    try:
        raw = github.file(repository, sha, COMPONENT_VERSIONS_PATH)
    except (KeyError, TypeError, ValueError) as exc:
        # Only an absent file is exempt. Authentication, network and malformed-response
        # errors must retain their refusal rather than masquerade as an empty declaration.
        if preview and 'HTTP 404' in str(exc):
            return empty
        raise ResolutionError(f'{where} is missing or unreadable: {exc}') from exc
    if preview and not raw.strip():
        return empty
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


# Each SDK repository declares the honua-server capabilities it requires, and the floor each was
# introduced at, in this file (schemas/sdk-capability-baseline.v1.schema.json,
# docs/SDK-SERVER-BASELINE-RULE.md; honua-release#231 WI-5). It is read at every published source
# revision of the SDK's packages, never at a newer head, and recorded as the component's
# serverCompatibility manifest and declaration. sdk_baselines.py resolves the floor in the lock.
SDK_BASELINE_PATH = 'release/sdk-capability-baseline.json'
SDK_BASELINE_SCHEMA = ROOT / 'schemas' / 'sdk-capability-baseline.v1.schema.json'
# The capabilities a candidate server advertises: its canonical capability-key vocabulary, read
# at the selected honua-server sha. Every SDK consumes this file (capability-keys fixtures).
SERVER_CAPABILITY_KEYS = ('honua-server', 'docs/gis/data/capability-keys.v1.json')


def _json_file(github, repository, revision, path, where):
    """`(raw bytes, parsed JSON)` at an exact revision, or a refusal naming `where`."""
    try:
        raw = github.file(repository, revision, path)
    except (KeyError, TypeError, ValueError) as exc:
        raise ResolutionError(f'{where} is missing or unreadable: {exc}') from exc
    try:
        return raw, json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ResolutionError(f'{where} is not a JSON document: {exc}') from exc


def advertised_capabilities(github, candidate):
    """The capability keys the selected honua-server advertises, or a refusal."""
    owner, path = SERVER_CAPABILITY_KEYS
    server = candidate['components'][owner]
    repository = str(server.get('repository') or '').removeprefix('https://github.com/')
    sha = str(server.get('sha') or '')
    if not SHA.fullmatch(sha):
        raise ResolutionError(f'{owner} has no immutable revision to read {path} at')
    where = f'{repository}@{sha}:{path}'
    _, document = _json_file(github, repository, sha, path, where)
    rows = document.get('capabilities') if isinstance(document, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ResolutionError(f'{where} has no capabilities list')
    keys = [row.get('key') if isinstance(row, dict) else None for row in rows]
    if not all(isinstance(key, str) and key for key in keys):
        raise ResolutionError(f'{where} has a capability row without a key')
    return frozenset(keys)


def published_revisions(name, component, client_artifacts):
    """Every source revision a published package of this SDK repository was built from.

    The primary package's revision is the component's artifactSourceRevision (#412). A companion
    package of the same repository (e.g. @honua/mcp-server beside @honua/sdk-js) ships too, so the
    lock's declarations must cover its revision as well (sdk_baselines.check_component). Optional
    (`required: false`) rows do not ship and contribute no revision.
    """
    repository = str(component.get('repository') or '').removeprefix('https://github.com/')
    primary = str(component.get('artifactSourceRevision') or '')
    if not SHA.fullmatch(primary):
        raise ResolutionError(f'{name}: no published source revision to read {SDK_BASELINE_PATH} at')
    revisions = [primary]
    for client, artifact in sorted((client_artifacts or {}).items()):
        # verify_manifest skips optional rows, so their bytes and publication are unverified: they
        # do not ship and their sourceSha is not a published revision.
        if not isinstance(artifact, dict) or artifact.get('required', True) is False:
            continue
        if str(artifact.get('repository') or '').removeprefix('https://github.com/') != repository:
            continue
        revision = str(artifact.get('sourceSha') or '')
        if not SHA.fullmatch(revision):
            raise ResolutionError(f'{name}: clientArtifacts.{client} has no immutable sourceSha')
        if revision not in revisions:
            revisions.append(revision)
    return repository, revisions


def _baseline_floor(where, baseline):
    """The declared floor must be the maximum of the declared introductions it summarises."""
    entries = [baseline['capabilities'][key] for key in baseline['requiredCapabilities']]
    if any(entry.get('introductionModel') == sdk_baselines.FIRST_RELEASE for entry in entries):
        # No earlier server exists, so the first release is above every numeric floor.
        expected = sdk_baselines.FIRST_RELEASE
    else:
        expected = str(max(parse_semver(entry['minimumServerVersion']) for entry in entries))
    if baseline['minimumServerVersion'] != expected:
        raise ResolutionError(f"{where}: minimumServerVersion {baseline['minimumServerVersion']!r} is not "
                              f'the maximum of its required capabilities ({expected!r})')
    return expected


def sdk_capability_baseline(github, name, component, client_artifacts, advertised):
    """The component's serverCompatibility, read at its published revisions, or a refusal."""
    repository, revisions = published_revisions(name, component, client_artifacts)
    schema = json.loads(SDK_BASELINE_SCHEMA.read_text(encoding='utf-8'))
    manifests, declarations, floors = [], [], set()
    for revision in revisions:
        where = f'{name}: {repository}@{revision}:{SDK_BASELINE_PATH}'
        raw, baseline = _json_file(github, repository, revision, SDK_BASELINE_PATH, where)
        errors = sorted(Draft202012Validator(schema).iter_errors(baseline), key=lambda e: list(e.absolute_path))
        if errors:
            raise ResolutionError(f'{where} does not match {SDK_BASELINE_SCHEMA.name}: ' + '; '.join(
                f"{'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}" for error in errors[:5]))
        if baseline['component'] != name:
            raise ResolutionError(f"{where} declares component {baseline['component']!r}, not {name!r}")
        required = baseline['requiredCapabilities']
        if set(baseline['capabilities']) != set(required):
            raise ResolutionError(f'{where}: capabilities must hold exactly one introduction per required '
                                  f"capability; got {sorted(baseline['capabilities'])} for {sorted(required)}")
        missing = sorted(set(required) - advertised)
        if missing:
            raise ResolutionError(f'{where}: the candidate honua-server does not advertise required '
                                  f"capabilit{'y' if len(missing) == 1 else 'ies'} {', '.join(missing)}")
        floors.add(_baseline_floor(where, baseline))
        manifests.append({
            'source': {'repository': f'https://github.com/{repository}', 'revision': revision,
                       'path': SDK_BASELINE_PATH},
            'sha256': sdk_baselines.content_digest(baseline),
            'content': baseline,
            'requiredCapabilities': list(required),
        })
        declarations.append({'revision': revision, 'path': SDK_BASELINE_PATH,
                             'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                             'minimumServerVersion': baseline['minimumServerVersion']})
    if len(floors) != 1:
        raise ResolutionError(f'{name}: published revisions {", ".join(revisions)} declare different '
                              f'minimumServerVersion values {sorted(floors)}; republish them together')
    return {'minimumServerVersion': floors.pop(), 'manifests': manifests, 'declarations': declarations}


# The lock's OKF and catalog content digests (honua-release#231 WI-7) are the byte sha256 of one file
# at a revision this candidate selected, declared as repository@revision:path#sha256 so
# verify_content_digests.py re-reads the same bytes. Each source is (owner, path): owner is a
# candidate component (read at its selected sha) or LEDGER (the bound protocolCertification.ledger
# repository at its commit).
LEDGER = 'protocolCertification.ledger'
OKF_CONTENT_SOURCE = ('honua-server', 'scripts/ci/okf-bundle.v1.json')
# Ruling R24 (honua-release#376, 2026-10-03): the catalog is the server's runtime feature catalog at
# the selected sha. feature-catalog.json is copied into the image (Dockerfile), embedded by
# Honua.Server.csproj and Honua.Ai.csproj, and served by FeatureCatalogResource. It is ~1.6 MB, so
# GitHub.file reads it through the raw media type.
CATALOG_CONTENT_SOURCE = ('honua-server', 'docs/gis/data/feature-catalog.json')
CONTENT_DIGEST_SOURCES = {'okf': OKF_CONTENT_SOURCE, 'catalog': CATALOG_CONTENT_SOURCE}


def content_digest_declaration(github, candidate, name):
    """`{repository, revision, path, sha256}` for one content digest, read at the selected revision."""
    owner, path = CONTENT_DIGEST_SOURCES[name]
    if owner == LEDGER:
        ledger = candidate['protocolCertification']['ledger']
        repository, revision = str(ledger.get('repository') or ''), str(ledger.get('commit') or '')
    else:
        component = candidate['components'][owner]
        repository, revision = str(component.get('repository') or ''), str(component.get('sha') or '')
    repository = repository.removeprefix('https://github.com/')
    if not SHA.fullmatch(revision):
        raise ResolutionError(f'{owner} has no immutable revision to read {path} at')
    raw = github.file(repository, revision, path)
    return {'repository': f'https://github.com/{repository}', 'revision': revision, 'path': path,
            'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest()}


def release_carried_platform_identity(components):
    """Drop a platform version, and a chart identity, that belong to another source.

    An image is re-resolved onto the selected sha, so its digest and artifactSourceRevision are
    tonight's. A chart is not: select_component copies yesterday's digest, artifactSourceRevision
    and artifactSha256 and only the sha moves. Those bytes must not receive tonight's platform
    version. Identity that is already bound to the selected sha is kept for the stamp. An imaged
    row's plain version is pre-release (the stamp lives on artifactVersion), so a carried one is
    reset rather than left for the generator or the release notes to read.
    """
    if not isinstance(components, dict):
        return
    for name in IMAGED_COMPONENTS:
        selected = components.get(name)
        if not isinstance(selected, dict):
            continue
        selected.pop('artifactVersion', None)
        if selected.get('version') not in (None, PRERELEASE):
            selected['version'] = PRERELEASE
        if name == PUBLISHER:
            selected.pop('releaseVersion', None)
        chart = str(selected.get('artifact') or '').startswith('oci-chart:') and not selected.get('image')
        if chart and str(selected.get('artifactSourceRevision') or '') != str(selected.get('sha') or ''):
            for key in ('digest', 'artifactSourceRevision', 'artifactSha256'):
                selected.pop(key, None)


def resolve(manifest, matrix, github, registry, limit=100, protocol_ledger='require', skips=None,
            skip_limit=SKIP_REPORT_LIMIT, stale_days=STALE_DAYS):
    """`protocol_ledger='produce'` is the nightly: its protocol-ledger job produces and binds the
    ledger for the server selected here, so an unbound ledger is not a refusal yet. Every other
    exact-candidate check still refuses, and the bound candidate is re-checked in full.
    `skips`, when given, receives each trunk-selected component's skip report, also on refusal."""
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
        walk = {}
        try:
            candidate['components'][name] = (
                select_sdk(name, component, candidate.get('clientArtifacts') or {}, identities, github)
                if name in SDK_COMPONENTS else select_component(name, component, github, registry, limit, walk))
            selected = candidate['components'][name]
            image = selected.get('image')
            print(f"RESOLVED {name} {selected['sha']}" + (f" {image}" if image else ''))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            detail = str(exc)
            if not detail.startswith(f'{name}:'):
                detail = f'{name}: {detail}'
            failures.append(detail)
            continue
        finally:
            if walk.get('head'):
                report = skip_report(name, walk, github, keep=skip_limit, stale_days=stale_days)
                print('\n'.join(skip_report_lines(report)))
                if skips is not None:
                    skips[name] = report
        declare(name, selected)
    # R22: mint stamps tonight's platform version beside the identity selected here. A version, or
    # a chart digest, carried forward from another night never survives selection.
    release_carried_platform_identity(candidate['components'])
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
    if protocol_ledger == 'produce':
        # The nightly binds only the ledger it produces tonight; no earlier binding survives.
        certification['ledger'].update(status='pending', commit='pending', requirementsSourceRevision='pending',
                                       sha256='pending')
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
    # SDK server floors are read at each SDK's published revisions; a carried-forward
    # serverCompatibility never survives. A missing or invalid baseline refuses the night.
    baseline_components = [name for name in sdk_baselines.SDK_COMPONENTS if name in candidate['components']]
    for name in baseline_components:
        candidate['components'][name].pop('serverCompatibility', None)
    advertised = None
    if baseline_components:
        try:
            advertised = advertised_capabilities(github, candidate)
        except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            failures.append(f'honua-server capability keys: {exc}')
    if advertised is not None:
        for name in baseline_components:
            try:
                candidate['components'][name]['serverCompatibility'] = sdk_capability_baseline(
                    github, name, candidate['components'][name], candidate.get('clientArtifacts') or {},
                    advertised)
            except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
                detail = str(exc)
                failures.append(detail if detail.startswith(f'{name}:') else f'{name}: {detail}')
    mcp = candidate['components'].get('geospatial-mcp', {})
    declaration = candidate.get('platformLockEvidence', {}).get('contentDigests', {}).get('geospatialMcp')
    if declaration and mcp:
        declaration.update(revision=mcp['sha'], sha256=mcp.get('artifactSha256'))
    # A hand or carried-forward declaration never survives: the selected bytes replace it, or the
    # night refuses with the digest undeclared.
    digests = candidate.setdefault('platformLockEvidence', {}).setdefault('contentDigests', {})
    for name in CONTENT_DIGEST_SOURCES:
        digests.pop(name, None)
        try:
            digests[name] = content_digest_declaration(github, candidate, name)
        except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            failures.append(f'contentDigests.{name}: {exc}')
    if certification['ledger'].get('status') != 'bound' and protocol_ledger != 'produce':
        failures.append(
            'protocolCertification.ledger: no bound ledger for the selected honua-server '
            f'{server}; exact-candidate refuses an unbound ledger')
    if failures:
        # Local qualification still runs so the refusal names every exact-candidate error.
        # Reachability and registry client probes are not a passing claim on this path.
        findings = validate_platform.validate(candidate, candidate_matrix, None, exact_candidate=True,
            ledger_produced_later=protocol_ledger == 'produce')
        failures.extend(findings.errors)
        raise ResolutionError('candidate qualification refused:\n' + '\n'.join(failures))
    findings = validate_platform.validate(candidate, candidate_matrix, None,
        exact_candidate=True, reachability_client=github, ledger_produced_later=protocol_ledger == 'produce')
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
    parser.add_argument('--protocol-ledger', choices=('require', 'produce'), default='require',
                        help='produce: the nightly binds a ledger it produces for the selected server')
    parser.add_argument('--skip-report-limit', type=int, default=SKIP_REPORT_LIMIT,
                        help='newest skipped shas listed per component in the log and skips.json')
    parser.add_argument('--stale-days', type=float, default=STALE_DAYS,
                        help='print STALE-CANDIDATE when the selected sha is this far behind trunk head')
    args = parser.parse_args(argv)
    skips = {}
    try:
        if args.max_commits < 1:
            raise ResolutionError('--max-commits must be positive')
        if args.skip_report_limit < 0 or args.stale_days < 0:
            raise ResolutionError('--skip-report-limit and --stale-days must not be negative')
        github = GitHub()
        manifest, matrix = resolve(yaml.safe_load(args.manifest.read_text()),
            yaml.safe_load(args.matrix.read_text()), github, Registry(github), args.max_commits,
            args.protocol_ledger, skips, args.skip_report_limit, args.stale_days)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for filename, value in [('platform-manifest.yaml', manifest), ('compatibility-matrix.yaml', matrix)]:
            (args.out_dir / filename).write_text(yaml.safe_dump(value, sort_keys=False))
        print(f'PASS: exact trunk candidate in {args.out_dir}; dry_run={args.dry_run}')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1
    finally:
        write_skip_report(args.out_dir, skips)


def write_skip_report(out_dir, skips):
    """skips.json and the job summary are written on refusal too: a night that resolves an old sha
    and then refuses on it must still say what it passed over."""
    if not skips:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'skips.json').write_text(json.dumps({'components': skips}, indent=2) + '\n')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write(skip_report_markdown(skips))


if __name__ == '__main__':
    raise SystemExit(main())
