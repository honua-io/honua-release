"""ARC v2 B1: the Compose executor image travels manifest -> lock -> bundle -> arc candidate -> package.

The shipped quickstart package runs the product's Compose executor beside the server. The release lock carries
it as an explicitly typed ``executor-image`` artifact of honua-server, derived from
``components.honua-server.composeExecutor``; no handcrafted lock is used here.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

import ai_arc_runner as R
import platform_lock_bundle as bundle
from test_platform_lock_bundle import candidate  # noqa: F401  (the frozen-inputs fixture)
from validate_platform_lock import validate

REV = '1' * 40
SERVER_INDEX, EXEC_INDEX = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
SERVER_PLAT = {'amd64': 'sha256:' + 'c' * 64, 'arm64': 'sha256:' + 'd' * 64}
EXEC_PLAT = {'amd64': 'sha256:' + 'e' * 64, 'arm64': 'sha256:' + 'f' * 64}
SOURCE = Path(__file__).resolve().parent / 'test_ai_arc_fixtures' / 'quickstart-package'


def executor(**change):
    return {'image': 'ghcr.io/honua-io/honua-server:nightly-compose-executor-1111111', 'digest': EXEC_INDEX,
            'architectures': ['amd64', 'arm64'], 'platformDigests': dict(EXEC_PLAT), 'artifactSourceRevision': REV,
            **change}


def with_server(candidate, compose_executor):
    """The bundle fixture's frozen inputs plus a published honua-server image declaring its executor."""
    _, paths, _ = candidate
    data = yaml.safe_load(paths[0].read_text())
    data['disasterRecovery'] = {'substrates': {'postgresql': True}}
    for field in ('sbom', 'provenance', 'notes', 'fixtures'):
        data['platformLockEvidence'].pop(field, None)
    server = {'repository': 'https://github.com/honua-io/honua-server', 'sha': REV, 'lifecycleStatus': 'GA',
              'image': 'ghcr.io/honua-io/honua-server:nightly-1111111', 'digest': SERVER_INDEX,
              'artifactVersion': '2026.1.0-rc.1', 'artifactSourceRevision': REV,
              'architectures': ['amd64', 'arm64'], 'platformDigests': dict(SERVER_PLAT),
              'contractVersions': {'admin': 'v1'}, 'schemaVersions': {'metadata': '2.0.0-alpha.1'}, 'dbSchema': '143',
              'migrationJournalSha256': 'sha256:' + '9' * 64}
    if compose_executor is not None:
        server['composeExecutor'] = compose_executor
    data['components']['honua-server'] = server
    paths[0].write_text(yaml.safe_dump(data))
    return paths


def test_the_executor_flows_from_manifest_through_lock_and_bundle_into_the_package(candidate, tmp_path):
    paths = with_server(candidate, executor())
    draft = bundle.generate(*paths, qualification=True)
    server_rows = [r for r in draft.lock['components']['honua-server']['artifacts']]
    assert not [u for u in draft.unresolved if 'honua-server' in u], draft.unresolved
    assert [r['kind'] for r in server_rows] == ['image', 'executor-image']
    image, row = server_rows
    assert row == {'kind': 'executor-image', 'coordinate': 'ghcr.io/honua-io/honua-server', 'version': image['version'],
                   'sourceRevision': REV, 'digest': EXEC_INDEX, 'architectures': ['amd64', 'arm64'],
                   'platformDigests': EXEC_PLAT}
    assert not [e for e in validate(draft.lock).errors if 'honua-server' in e]
    assert draft.unresolved == []
    bundle.bind_qualification(draft.lock, *paths, '2026.1-rc.1', image_inspector=None)
    # The frozen-input re-derivation keeps the bundle exact: a lock that drops or alters the executor differs.
    for mutate in (lambda lock: lock['components']['honua-server']['artifacts'].pop(),
                   lambda lock: lock['components']['honua-server']['artifacts'][1].update(digest=SERVER_INDEX)):
        forged = copy.deepcopy(draft.lock)
        mutate(forged)
        with pytest.raises(ValueError, match='qualification lock differs'):
            bundle.bind_qualification(forged, *paths, '2026.1-rc.1', image_inspector=None)
    # The arc resolves the candidate from the same lock bytes, and builds the product package with that executor.
    lock_path = tmp_path / 'platform-lock.json'
    lock_path.write_bytes(bundle.canonical_bytes(draft.lock))
    digest = 'sha256:' + hashlib.sha256(lock_path.read_bytes()).hexdigest()
    cand = R.candidate_from_lock(str(lock_path), digest)
    assert cand['imageDigest'] == SERVER_INDEX and cand['sourceRevision'] == REV
    assert cand['executorImage'] == 'ghcr.io/honua-io/honua-server@' + EXEC_INDEX
    arc = R.load_arc()
    lock = R.package_lock_for(arc, cand)
    assert lock['server'] == 'ghcr.io/honua-io/honua-server@' + SERVER_INDEX
    assert lock['executor'] == cand['executorImage'] and lock['worker'] == lock['server']
    package = R.expected_package(arc, cand, SOURCE)
    assert set(package['sha256']) == {'compose.yaml', 'compose.executor.yaml', 'quickstart.py', 'images.json',
                                      'README.md'} and package['declaredRelease'] == lock['server']


@pytest.mark.parametrize('declared,reason', [
    (executor(digest=None), 'composeExecutor.digest: immutable registry digest is not declared'),
    (executor(digest='latest'), 'composeExecutor.digest: immutable registry digest is not declared'),
    (executor(artifactSourceRevision='2' * 40), "composeExecutor.artifactSourceRevision: '2222"),
    (executor(image='ghcr.io/honua-io/honua-server@' + EXEC_INDEX), 'composeExecutor.image'),
    (executor(platformDigests={'amd64': 'sha256:' + 'e' * 64}), 'composeExecutor.platformDigests'),
    ([executor(), executor()], 'exactly one Compose executor image'),
])
def test_a_wrong_executor_declaration_is_refused_and_never_signed(candidate, declared, reason):
    paths = with_server(candidate, declared)
    draft = bundle.generate(*paths, qualification=True)
    assert any(reason in u for u in draft.unresolved), draft.unresolved
    assert [r['kind'] for r in draft.lock['components']['honua-server']['artifacts']] == ['image']
    with pytest.raises(ValueError, match='composeExecutor'):
        bundle.bind_qualification(draft.lock, *paths, '2026.1-rc.1', image_inspector=None)


def test_the_signing_bind_treats_an_executor_refusal_as_fatal(candidate, monkeypatch):
    """platform_lock_bundle.bind tolerates some draft refusals; an executor refusal is never one of them."""
    paths = with_server(candidate, executor(digest=None))
    draft = bundle.generate(*paths, qualification=True)
    monkeypatch.setattr(bundle, 'validate', lambda lock: type('F', (), {'errors': []})())
    monkeypatch.setattr(bundle, 'stamp_errors', lambda manifest: [])
    with pytest.raises(ValueError, match='composeExecutor.digest'):
        bundle.bind(draft.lock, *paths, '2026.1-rc.1', image_inspector=None)


def test_without_a_declared_executor_the_lock_has_none_and_the_arc_package_fails_closed(candidate, tmp_path):
    paths = with_server(candidate, None)
    draft = bundle.generate(*paths, qualification=True)
    assert [r['kind'] for r in draft.lock['components']['honua-server']['artifacts']] == ['image']
    lock_path = tmp_path / 'platform-lock.json'
    lock_path.write_bytes(bundle.canonical_bytes(draft.lock))
    cand = R.candidate_from_lock(str(lock_path), 'sha256:' + hashlib.sha256(lock_path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match='no immutable compose executor image'):
        R.package_lock_for(R.load_arc(), cand)


def test_the_lock_schema_types_the_executor_artifact():
    schema = json.loads((Path(__file__).resolve().parents[1] / 'schemas/platform-lock.v1.schema.json').read_text())
    artifact = schema['$defs']['artifact']
    assert 'executor-image' in artifact['properties']['kind']['enum']
    rule = next(r for r in artifact['allOf'] if 'executor-image' in json.dumps(r['if']))
    assert set(rule['then']['required']) == {'digest', 'platformDigests', 'architectures'}
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'schemas/platform-manifest.schema.json').read_text())
    declared = manifest['$defs']['component']['properties']['composeExecutor']
    assert set(declared['required']) == {'image', 'digest', 'architectures', 'platformDigests', 'artifactSourceRevision'}
