"""Authenticate retained worker publication bytes using public producer metadata.

The checked installer downloads the original Actions ZIP with actions:read and
retains it unchanged. Runtime verification needs only public GitHub metadata and
those retained bytes, never an Actions download credential or a mutable feed.
This proof establishes publication identity, not execution or qualification.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import stat
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest, digest_bytes
from bundle_agent_e2e_container import verify_runtime_index
from fixture_handoff import require
from human_memberships import github_json

REPOSITORY = 'honua-io/honua-release'
WORKFLOW = '.github/workflows/hosted-pilot-worker-image.yml'
BUILD_STEP = 'Build public-only A/B exports, run real container controls and publish immutable image'
RETAIN_STEP = 'Retain actual immutable worker image metadata'
METADATA_FILE = 'hosted-pilot-worker-image.json'
INDEX_FILE = 'runtime-index.json'
LIMIT = 2*1024*1024
SHA = r'sha256:[a-f0-9]{64}'
BINDING_FIELDS = {'sourceSha', 'runId', 'runAttempt', 'artifactId', 'artifactDigest'}
METADATA_FIELDS = {'schema', 'sourceSha', 'runId', 'runAttempt', 'image', 'imageBytes',
    'platform', 'caseId', 'pack', 'knowledge', 'publicBundleDigest', 'runtimeBundleDigest',
    'ownerBudget', 'retention', 'publicationClaim', 'qualification', 'modelExecution'}


def validate_binding(binding):
    require(isinstance(binding, dict) and set(binding) == BINDING_FIELDS and
        isinstance(binding['sourceSha'], str) and re.fullmatch(r'[a-f0-9]{40}', binding['sourceSha']) and
        all(type(binding[k]) is int and binding[k] > 0 for k in ('runId', 'runAttempt', 'artifactId')) and
        isinstance(binding['artifactDigest'], str) and re.fullmatch(SHA, binding['artifactDigest']),
        'worker publication requires an immutable source/run/attempt/artifact binding')


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'worker publication JSON contains duplicate keys')
            value[key] = item
        return value

    def invalid_constant(_):
        require(False, 'worker publication JSON contains a non-finite value')

    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('worker publication JSON is invalid') from error


def publication_originals(archive):
    """Read only the two bounded regular members; never extract to disk."""
    require(type(archive) is bytes and 0 < len(archive) <= LIMIT, 'worker publication ZIP exceeds its byte bound')
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            members = package.infolist()
            require(len(members) == 2 and {row.filename for row in members} == {METADATA_FILE, INDEX_FILE},
                'worker publication ZIP must contain exactly the two original files')
            require(sum(row.file_size for row in members) <= LIMIT and all(
                0 < row.file_size <= LIMIT and not row.is_dir() and not row.flag_bits & 1 and
                stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG) and
                row.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for row in members),
                'worker publication ZIP contains an unsafe or oversized member')
            return {row.filename: package.read(row) for row in members}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError('worker publication ZIP is invalid') from error


def verify_publication(binding, *, archive: bytes, index_raw: bytes | None,
                       image, public_bundle_digest, github=github_json, guard):
    """Authenticate one exact successful publisher before admitting its index.

    ``github`` is the existing GET-only ``github(endpoint)->JSON`` adapter.
    ``guard`` checks original funding and authority around every external read.
    Source ancestry permits a compatible protected ancestor; finite runtime
    files still have to match this controller's maintained verifier exactly.
    """
    validate_binding(binding)
    guard()
    require(isinstance(image, str) and re.fullmatch(
        r'585192672263\.dkr\.ecr\.us-east-1\.amazonaws\.com/honua-pilot-[a-z0-9-]+-worker@sha256:[a-f0-9]{64}', image) and
        isinstance(public_bundle_digest, str) and re.fullmatch(SHA, public_bundle_digest),
        'worker publication requires the installed immutable image and selected public bundle')
    require(type(archive) is bytes and 0 < len(archive) <= LIMIT and digest_bytes(archive) == binding['artifactDigest'],
        'retained worker publication differs from the original artifact digest')

    def read(endpoint):
        guard()
        value = github('repos/'+REPOSITORY+'/'+endpoint)
        guard()
        require(isinstance(value, dict), 'worker publisher metadata is unavailable')
        return value

    attempt = 'actions/runs/'+str(binding['runId'])+'/attempts/'+str(binding['runAttempt'])
    run = read(attempt)
    repository = run.get('repository') or {}
    head_repository = run.get('head_repository') or {}
    require(run.get('id') == binding['runId'] and run.get('run_attempt') == binding['runAttempt'] and
        run.get('head_sha') == binding['sourceSha'] and run.get('head_branch') == 'trunk' and
        run.get('event') == 'workflow_dispatch' and run.get('path', '').split('@')[0] == WORKFLOW and
        run.get('status') == 'completed' and run.get('conclusion') == 'success' and
        repository.get('full_name') == REPOSITORY and type(repository.get('id')) is int and repository['id'] > 0 and
        head_repository.get('full_name') == REPOSITORY and head_repository.get('id') == repository['id'],
        'worker publication was not the exact successful protected publisher')
    comparison = read('compare/'+binding['sourceSha']+'...trunk')
    require(comparison.get('status') in ('identical', 'ahead') and
        comparison.get('merge_base_commit', {}).get('sha') == binding['sourceSha'],
        'worker publication source is not a protected trunk ancestor')
    jobs = read(attempt+'/jobs?per_page=100')
    rows = jobs.get('jobs')
    require(isinstance(rows, list) and type(jobs.get('total_count')) is int and
        0 < jobs['total_count'] <= 100 and jobs['total_count'] == len(rows) and all(isinstance(row, dict) for row in rows),
        'worker publication jobs are incomplete or ambiguous')
    builds = [row for row in rows if row.get('name') == 'build']
    require(len(builds) == 1, 'worker publication requires one original build job')
    job = builds[0]
    require(type(job.get('id')) is int and job['id'] > 0 and job.get('run_id') == binding['runId'] and
        job.get('run_attempt') == binding['runAttempt'] and job.get('head_sha') == binding['sourceSha'] and
        job.get('status') == 'completed' and job.get('conclusion') == 'success',
        'worker publisher build job did not succeed on the exact source/attempt')
    steps = job.get('steps')
    require(isinstance(steps, list) and all(isinstance(row, dict) for row in steps), 'worker publisher steps missing')
    positions = []
    for name in (BUILD_STEP, RETAIN_STEP):
        matches = [row for row in steps if row.get('name') == name]
        require(len(matches) == 1 and matches[0].get('status') == 'completed' and matches[0].get('conclusion') == 'success' and
            type(matches[0].get('number')) is int and matches[0]['number'] > 0,
            'worker publisher build or original artifact retention did not succeed')
        positions.append(matches[0]['number'])
    require(positions[0] < positions[1], 'worker publisher artifact predates its build')
    artifact = read('actions/artifacts/'+str(binding['artifactId']))
    origin = artifact.get('workflow_run') or {}
    require(artifact.get('id') == binding['artifactId'] and artifact.get('name') ==
        'hosted-pilot-worker-image-'+str(binding['runId'])+'-'+str(binding['runAttempt']) and
        artifact.get('digest') == binding['artifactDigest'] and artifact.get('expired') is False and
        type(artifact.get('size_in_bytes')) is int and artifact['size_in_bytes'] == len(archive) and
        origin.get('id') == binding['runId'] and origin.get('head_sha') == binding['sourceSha'] and
        origin.get('head_branch') == 'trunk' and origin.get('repository_id') == repository['id'] and
        origin.get('head_repository_id') == repository['id'], 'worker artifact producer, digest or original bytes differ')
    originals = publication_originals(archive)
    require(index_raw is None or type(index_raw) is bytes and index_raw == originals[INDEX_FILE],
        'retained runtime index differs from the original publication ZIP member')
    metadata, index = _json(originals[METADATA_FILE]), _json(originals[INDEX_FILE])
    require(isinstance(metadata, dict) and set(metadata) == METADATA_FIELDS and
        metadata['schema'] == 'honua.hosted-worker-image/v1' and metadata['sourceSha'] == binding['sourceSha'] and
        metadata['runId'] == str(binding['runId']) and metadata['runAttempt'] == str(binding['runAttempt']) and
        metadata['image'] == image and metadata['platform'] == 'linux/amd64' and
        type(metadata['imageBytes']) is int and 0 < metadata['imageBytes'] <= 512*1024*1024 and
        metadata['publicBundleDigest'] == public_bundle_digest and metadata['qualification'] is False and
        metadata['modelExecution'] == 'not-run' and isinstance(metadata['caseId'], str) and bool(metadata['caseId']) and
        all(isinstance(metadata[name], dict) and metadata[name] for name in ('pack', 'knowledge', 'ownerBudget', 'retention')),
        'worker publication receipt differs from the selected original image/source/public bundle')
    claim = metadata['publicationClaim']
    require(isinstance(claim, dict) and set(claim) == {'bucket', 'key', 'versionId', 'sha256'} and
        all(isinstance(value, str) and 0 < len(value) <= 1024 for value in claim.values()) and
        re.fullmatch(r'hosted-pilot/reservations/worker-image/[A-Za-z0-9][A-Za-z0-9._-]{2,119}\.json', claim['key']) and
        claim['versionId'] != 'null' and re.fullmatch(SHA, claim['sha256']),
        'worker publication lacks its original single-build claim')
    verify_runtime_index(index)
    require(canonical_digest(index) == metadata['runtimeBundleDigest'] and
        index['files'].get('public/public-manifest.json') == public_bundle_digest,
        'worker publication runtime/public bytes differ from the original receipt')
    producer = {'repository': REPOSITORY, 'workflow': WORKFLOW, 'sourceSha': binding['sourceSha'],
        'runId': binding['runId'], 'runAttempt': binding['runAttempt'], 'jobId': job['id'],
        'jobKey': 'build', 'ref': 'refs/heads/trunk', 'event': 'workflow_dispatch',
        'artifactId': binding['artifactId'], 'artifactDigest': binding['artifactDigest']}
    guard()
    return {'metadata': metadata, 'index': index, 'producer': producer}
