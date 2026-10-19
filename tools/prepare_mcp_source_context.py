#!/usr/bin/env python3
"""Prepare an isolated MCP source-context review; never select or bind a candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile

import yaml

import convergence_rebind as convergence
import resolve_trunk_candidate as resolver

REPOSITORY = 'honua-io/geospatial-mcp'
ROOT = Path(__file__).resolve().parents[1]
FILES = ('certification/package.json', 'certification/package-lock.json')
POLICY = 'certification/sources/mcp-operation-auth-policy.v1.json'
CHECKER = 'tools/check_protocol_certification.py'


class PreparationError(ValueError):
    pass


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def authenticated_source(github, source):
    if not isinstance(source, str) or not re.fullmatch('[0-9a-f]{40}', source):
        raise PreparationError('producer source must be an exact lowercase commit')
    repo = github.json(f'repos/{REPOSITORY}')
    branch = github.json(f'repos/{REPOSITORY}/branches/trunk')
    head = branch.get('commit', {}).get('sha')
    if (repo.get('full_name') != REPOSITORY or repo.get('default_branch') != 'trunk'
            or branch.get('name') != 'trunk' or branch.get('protected') is not True
            or not re.fullmatch('[0-9a-f]{40}', str(head))):
        raise PreparationError('producer repository or protected trunk identity differs')
    comparison = github.json(f'repos/{REPOSITORY}/compare/{source}...{head}')
    if (comparison.get('status') not in {'ahead', 'identical'}
            or comparison.get('base_commit', {}).get('sha') != source
            or comparison.get('merge_base_commit', {}).get('sha') != source):
        raise PreparationError('producer source is not authenticated protected-trunk history')
    # GitHub.file verifies each returned body's exact size and Git blob identity.
    snapshots = {path: github.file(REPOSITORY, source, path) for path in FILES}
    return head, snapshots


def preserve_obligations(before, after, source):
    """Only selected MCP client versions and their producer source may change."""
    expected = copy.deepcopy(before)
    expected['source_revisions']['geospatial-mcp']['commit'] = source
    old_rows = [row for row in before['requirements'] if row['surface'] == 'mcp']
    new_rows = [row for row in after['requirements'] if row['surface'] == 'mcp']
    key = lambda row: (row['operation'], row['canonical_client'], row['deployment_target'])
    if (len(old_rows) != 82 or len(new_rows) != 82
            or len({key(row) for row in old_rows}) != 82
            or len({key(row) for row in new_rows}) != 82):
        raise PreparationError('source preparation must preserve all82 distinct MCP obligations')
    versions = {}
    for row in new_rows:
        lane = row['client_lane']
        if lane in versions and versions[lane] != row['client_version']:
            raise PreparationError('one canonical client has mixed versions')
        versions[lane] = row['client_version']
    if set(versions) != {'mcp-typescript-sdk', 'mcp-inspector'}:
        raise PreparationError('canonical MCP client roster differs')
    for row in expected['requirements']:
        if row['surface'] == 'mcp':
            row['client_version'] = versions[row['client_lane']]
    normalise = lambda value: {**value, 'requirements': sorted(value['requirements'], key=lambda row: json.dumps(row, sort_keys=True))}
    if len(before['requirements']) < 3008 or normalise(expected) != normalise(after):
        raise PreparationError('generated source context changes original scope, policy, facets, tiers or unrelated source')
    return versions


def prepare(root, output, source, github=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root in output.parents or output.exists():
        raise PreparationError('output must be new and outside the source repository')
    frozen_paths = ('platform-manifest.yaml', 'customer-install-manifest.json',
                    str(convergence.REVISIONS), str(convergence.CATALOG), POLICY, CHECKER)
    original = {path: (root / path).read_bytes() for path in frozen_paths}
    manifest = yaml.safe_load(original['platform-manifest.yaml'])
    if manifest.get('protocolCertification', {}).get('ledger', {}).get('status') != 'pending':
        raise PreparationError('source preparation requires an explicitly pending release ledger')
    github = github or resolver.GitHub()
    protected_head, snapshots = authenticated_source(github, source)
    before = json.loads(original[str(convergence.CATALOG)])
    sources = json.loads(original[str(convergence.REVISIONS)])
    if sources['sources']['geospatial-mcp']['repository'] != REPOSITORY:
        raise PreparationError('release source manifest has an unexpected MCP repository')
    sources['sources']['geospatial-mcp']['commit'] = source
    # Observe the ordinary strict verdict. A red verdict is retained in this review
    # and remains a refusal for the unchanged normal candidate selector.
    green, reason = github.green('geospatial-mcp', REPOSITORY, source)
    checks = list(github.pages(f'repos/{REPOSITORY}/commits/{source}/check-runs', 'check_runs'))
    check_context = []
    for check in checks:
        if check.get('head_sha') != source:
            raise PreparationError('observed check belongs to a different producer source')
        check_context.append({key: check.get(key) for key in
                              ('id', 'name', 'head_sha', 'status', 'conclusion', 'details_url')})
    payloads = {
        str(convergence.REVISIONS): (json.dumps(sources, indent=2) + '\n').encode(),
        **{f'certification/sources/geospatial-mcp/{Path(path).name}': data for path, data in snapshots.items()},
        POLICY: original[POLICY], CHECKER: original[CHECKER],
    }
    with tempfile.TemporaryDirectory(prefix='mcp-source-context-') as temporary:
        trial = Path(temporary) / 'repo'
        shutil.copytree(root, trial, ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv',
                                                                  'node_modules', 'artifacts'))
        for path, data in payloads.items():
            (trial / path).write_bytes(data)
        # Execute only the release-owned generator/validator, never producer code.
        convergence.run_catalog(trial, before['receipt_schema_min'])
        catalog = (trial / convergence.CATALOG).read_bytes()
        after = json.loads(catalog)
        versions = preserve_obligations(before, after, source)
        spec = importlib.util.spec_from_file_location('mcp_context_generator', trial / 'certification/generate-protocol-requirements.py')
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        clients = generator.mcp_clients()
        if {lane: version for _, lane, version in clients} != versions:
            raise PreparationError('generated client cohort differs from the literal package/lock')
        payloads[str(convergence.CATALOG)] = catalog
    if any((root / path).read_bytes() != data for path, data in original.items()):
        raise PreparationError('source release inputs changed during preparation')
    report = {
        'schema': 'honua.mcp-source-context-review/v1', 'state': 'review-required',
        'candidateAdmission': False, 'candidateQualification': 'pending',
        'producer': {'repository': REPOSITORY, 'sourceSha': source, 'protectedHead': protected_head,
                     'normalCandidateCi': {'pass': green, 'reason': reason}, 'checks': check_context},
        'sourceFiles': {path: digest(data) for path, data in snapshots.items()},
        'frozenInputs': {path: digest(data) for path, data in original.items()},
        'requirements': {'revision': after['revision'], 'originalRows': len(before['requirements']),
                         'proposedRows': len(after['requirements']), 'mcpRows': 82,
                         'clientVersions': versions, 'qualifiedRows': 0},
        'artifactVerification': 'literal declarations/lock only; installed registry bytes and actual client observations remain required',
        'next': ['review generated source/catalog payloads in a normal protected PR',
                 'replay canonical clients with that protected requirements source/revision and exact server inputs',
                 'retain normal resolver refusal until actual checks pass; normal NIGHTLY-STAGE/BIND remain required'],
    }
    payloads['mcp-source-context-review.json'] = (json.dumps(report, indent=2) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mcp-source-context-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'result'
        staging.mkdir()
        for path, data in payloads.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (staging / 'source-context-inventory.json').write_text(json.dumps(
            {path: digest(data) for path, data in sorted(payloads.items())}, indent=2) + '\n')
        if output.exists():
            raise PreparationError('output appeared during preparation')
        staging.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--producer-source', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.root, args.output, args.producer_source)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
