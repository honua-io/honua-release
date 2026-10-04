import copy
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import fixture_revisions
import mint_nightly_lock as nightly
from candidate_binding import _sha256, verify_candidate_binding
from test_platform_lock_bundle import candidate


@pytest.fixture
def inputs(candidate, tmp_path):
    return build_inputs(candidate, tmp_path)


def build_inputs(candidate, tmp_path, *, gate_fixtures=None, missing_post_gate=False):
    """The certified candidate and its report. With `gate_fixtures` the candidate declares no
    fixtures itself; freeze always uses the original manifest before the gates run."""
    _, paths, _ = candidate
    manifest = yaml.safe_load(paths[0].read_text())
    manifest['platformRelease'] = '2026.1-rc.3'
    manifest['status'] = 'rc'
    manifest['disasterRecovery'] = {'topology': 'local-docker-single-tenant',
        'objectives': {'rpoMs': 300000, 'rtoMs': 900000},
        'substrates': {'postgresql': True, 'redis': True, 'object-storage': True,
            'job-queue': True, 'transactional-outbox': True, 'workflow-cursors': True}}
    # Use the filenames in the actual train binding.
    real_paths = [tmp_path / 'platform-manifest.yaml', tmp_path / 'compatibility-matrix.yaml']
    if gate_fixtures is not None:
        del manifest['platformLockEvidence']['fixtures']
    (tmp_path / 'publisher-references.json').write_text(json.dumps(
        {field: manifest['platformLockEvidence'][field] for field in nightly.POST_GATE_FIELDS}))
    if missing_post_gate:
        for field in nightly.POST_GATE_FIELDS:
            del manifest['platformLockEvidence'][field]
    for component in manifest['components'].values():
        component['attestationWorkflow'] = 'honua-io/publisher/.github/workflows/publish.yml'
    real_paths[0].write_text(yaml.safe_dump(manifest))
    real_paths[1].write_bytes(paths[1].read_bytes())
    report = {'dry_run': False, 'overallStatus': 'pass', 'platform_label': '2026.1-rc.3',
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'gates': [{'gate': name, 'status': 'pass'} for name in sorted(nightly.REQUIRED_NIGHTLY_GATES)],
        'candidate': {
            'schemaVersion': 1,
            'source': {'repository': 'honua-io/honua-release', 'sha': SOURCE, 'branch': 'trunk'},
            'train': {'workflowPath': '.github/workflows/nightly-certification.yml', 'runId': RUN,
                      'runUrl': f'https://github.com/honua-io/honua-release/actions/runs/{RUN}',
                      'runAttempt': 1, 'certificationMode': 'live'},
            'artifacts': {p.name: {'sha256': _sha256(p), 'size': p.stat().st_size} for p in real_paths}}}
    # Recorded gate observations: the four deterministic cells and the nightly genuine-model cell.
    stamp = report['generatedAt'].replace('+00:00', 'Z')
    journeys = [{'status': 'pass', 'generatedAt': stamp, 'runId': RUN, 'runAttempt': 1,
                 'candidateDigest': _sha256(real_paths[0]),
                 'cells': [{'cell': cell, 'status': 'pass', 'attempts': [
                     {'number': 1, 'status': 'pass', 'driver': mode, 'completedAt': stamp}]}]
                } for mode, cells in (
                    ('deterministic', ('aws-ecs/redis-off', 'aws-ecs/redis-on',
                                       'aws-serverless/redis-off', 'aws-serverless/redis-on')),
                    ('genuine-model', ('aws-ecs/redis-off',))) for cell in cells]
    # Real freeze path: never derive fixtures or references before qualification.
    from platform_lock_bundle import canonical_bytes, bind_qualification
    draft = nightly.generate(*real_paths, qualification=True)
    bind_qualification(draft.lock, *real_paths, '2026.1-rc.3')
    qualification_lock = tmp_path / 'qualification-lock.json'
    qualification_lock.write_bytes(canonical_bytes(draft.lock))
    report = nightly.declare_evidence(report, qualification_lock, journeys)
    nightly.snapshot(*real_paths, report, tmp_path / 'notes')
    return report, real_paths


SOURCE = 'f' * 40
RUN = '4242'
# The candidate fixture (test_platform_lock_bundle) declares this one fixture repository.
DECLARED = {'repository': 'https://github.com/honua-io/fixtures', 'revision': 'a' * 40}


def records(uses=None, run_id=RUN):
    """One record per fixture gate job of the train; `uses` overrides a job's checkouts."""
    uses = uses or {}
    return [{'schema': fixture_revisions.SCHEMA, 'gate': gate, 'job': job, 'runId': run_id, 'runAttempt': '1',
             'fixtures': uses.get((gate, job), [dict(DECLARED)])}
            for gate, jobs in fixture_revisions.FIXTURE_GATES.items() for job in jobs]
PROTECTED = [{'id': 7, 'target': 'tag', 'enforcement': 'active',
              'conditions': {'ref_name': {'include': ['refs/tags/nightly-lock/**'], 'exclude': []}},
              'rules': [{'type': 'deletion'}, {'type': 'update'}, {'type': 'non_fast_forward'}]}]


def mint(report, paths, history, output, *, signer, **overrides):
    """The production call shape: trusted identity, complete published history, protected lock refs."""
    declared = json.loads((paths[0].parent / 'publisher-references.json').read_text())
    options = {'rulesets': PROTECTED, 'published': {}, 'source_sha': SOURCE, 'run_id': RUN,
               'fixture_records': records(), 'qualification_lock': paths[0].parent / 'qualification-lock.json',
               'notes_bundle': paths[0].parent / 'notes/release-notes.bundle',
               'post_gate_evidence': {'candidate': copy.deepcopy(report['candidate']),
                                     'notesRevision': (paths[0].parent / 'notes/release-notes-revision.txt').read_text().strip(),
                                     'references': {field: declared[field] for field in nightly.POST_GATE_FIELDS}}}
    options['post_gate_evidence']['references']['notes'] = (paths[0].parent / 'notes/release-notes-ref.txt').read_text().strip()
    options.update(overrides)
    return nightly.mint(report, *paths, history, output, 'trusted', signer=signer, **options)


def signer(lock, signature, *_):
    # Unit seam: OIDC signing is exercised by the production cosign commands,
    # while these tests prove which exact canonical bytes reach that signer.
    assert json.loads(lock.read_bytes())['platform']['id'] == 'honua-2026.1-rc.3'
    signature.write_text('{"verified": true}')


def test_all_green_generates_binds_and_signs_lock(inputs, tmp_path):
    report, paths = inputs
    output = tmp_path / 'minted'
    assert mint(report, paths, tmp_path / 'history', output, signer=signer) == '2026.1-rc.3'
    lock = json.loads((output / 'platform-lock.json').read_bytes())
    assert lock['platform']['status'] == 'rc'
    assert lock['sourceInputs']['platformManifest']['sha256'] == 'sha256:' + _sha256(output / paths[0].name)
    assert (output / 'platform-lock.sigstore.json').exists()
    assert (output / 'bom.cdx.json').exists()
    assert nightly.CHANNEL_TAG.search((output / 'platform-lock.json').read_text()) is None


def fresh_references(report, paths):
    evidence = json.loads((paths[0].parent / 'publisher-references.json').read_text())
    references = copy.deepcopy({key: evidence[key] for key in nightly.POST_GATE_FIELDS})
    for field in ('sbom', 'provenance'):
        for row in references[field]:
            row['uri'] = f'oci://ghcr.io/honua-io/{row["component"]}/{field}@sha256:' + '9' * 64
            row['sha256'] = 'sha256:' + '9' * 64
    references['notes'] = (paths[0].parent / 'notes/release-notes-ref.txt').read_text().strip()
    return {'candidate': copy.deepcopy(report['candidate']), 'references': references,
            'notesRevision': (paths[0].parent / 'notes/release-notes-revision.txt').read_text().strip()}


def test_post_gate_lock_differs_from_freeze_only_in_three_fields(inputs, tmp_path):
    report, paths = inputs
    original_report = copy.deepcopy(report)
    refs = fresh_references(report, paths)
    frozen = json.loads((tmp_path / 'qualification-lock.json').read_bytes())
    output = tmp_path / 'minted'
    mint(report, paths, tmp_path / 'history', output, signer=signer, post_gate_evidence=refs)
    regenerated = json.loads((output / 'platform-lock.json').read_bytes())
    assert {key for key in frozen if frozen[key] != regenerated[key]} == nightly.POST_GATE_FIELDS | {'sourceInputs'}
    assert regenerated['sourceInputs']['platformManifest']['sha256'] == 'sha256:' + _sha256(output / paths[0].name)
    shipped = yaml.safe_load((output / paths[0].name).read_text())
    assert {field: shipped['platformLockEvidence'][field] for field in nightly.POST_GATE_FIELDS} == refs['references']
    assert set(regenerated) == set(frozen)
    assert {field: regenerated[field] for field in nightly.POST_GATE_FIELDS} == refs['references']
    assert report == original_report
    retained = json.loads((output / 'gate-report.json').read_bytes())
    assert nightly.evidence_failures(retained, 'sha256:' + _sha256(output / 'platform-lock.json')) == []
    for name, receipt in retained['evidenceReceipts'].items():
        old = original_report['evidenceReceipts'][name]
        assert receipt['status'] == old['status'] == 'pass'
        assert receipt['completedAt'] == old['completedAt']
    assert (output / 'qualification-lock.json').read_bytes() == (tmp_path / 'qualification-lock.json').read_bytes()


@pytest.mark.parametrize('field', ['sbom', 'provenance'])
def test_missing_post_gate_component_reference_refuses_before_signing(inputs, tmp_path, field):
    report, paths = inputs
    refs = fresh_references(report, paths)
    component = refs['references'][field][0]['component']
    refs['references'][field] = [row for row in refs['references'][field] if row['component'] != component]
    refuses_before_signing(report, paths, tmp_path, 'no .* reference covers|references and hashes are not declared',
                           post_gate_evidence=refs)


def test_mint_requires_post_gate_refs_even_when_freeze_declares_them(inputs, tmp_path):
    report, paths = inputs
    refuses_before_signing(report, paths, tmp_path, 'post-gate references are required', post_gate_evidence=None)


@pytest.mark.parametrize('mutation', [
    lambda refs: refs['candidate']['train'].update(runId='another-run'),
    lambda refs: refs['candidate']['source'].update(sha='b' * 40),
])
def test_post_gate_refs_cannot_cross_candidates(inputs, tmp_path, mutation):
    report, paths = inputs
    refs = fresh_references(report, paths)
    mutation(refs)
    refuses_before_signing(report, paths, tmp_path, 'another candidate or run', post_gate_evidence=refs)


def test_regeneration_refuses_non_evidence_drift(inputs, tmp_path):
    _, paths = inputs
    frozen = json.loads((tmp_path / 'qualification-lock.json').read_bytes())
    frozen['components']['sdk']['artifacts'][0]['version'] = '9.9.9'
    evidence = json.loads((paths[0].parent / 'publisher-references.json').read_text())
    with pytest.raises(ValueError, match='outside sbom, provenance and notes'):
        nightly.regenerate_post_gate(*paths, frozen, {field: evidence[field] for field in nightly.POST_GATE_FIELDS})


def valid_predicate(kind):
    if kind == 'https://spdx.dev/Document':
        return {'spdxVersion': 'SPDX-2.3', 'SPDXID': 'SPDXRef-DOCUMENT', 'name': 'package',
                'dataLicense': 'CC0-1.0', 'documentNamespace': 'https://publisher.test/bom/123',
                'creationInfo': {'creators': ['Tool: scanner'], 'created': '2026-10-02T00:00:00Z'},
                'packages': [{'SPDXID': 'SPDXRef-package', 'name': 'published-package',
                              'downloadLocation': 'NOASSERTION'}]}
    if kind == 'https://cyclonedx.org/bom':
        return {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
                'components': [{'type': 'library', 'name': 'published-package'}]}
    if kind.endswith('/v0.2'):
        return {'builder': {'id': 'https://builder.test'}, 'buildType': 'https://builder.test/build',
                'invocation': {'parameters': {}}, 'metadata': {'buildInvocationId': 'run-123'}}
    return {'buildDefinition': {'buildType': 'https://builder.test/build', 'externalParameters': {}},
            'runDetails': {'builder': {'id': 'https://builder.test'}}}


def publisher_bundles(repository, digest):
    return [{'bundle': {'dsseEnvelope': {'payload': base64.b64encode(json.dumps({
        'predicateType': predicate, 'subject': [{'name': 'published-package',
                                                'digest': {'sha256': digest.split(':')[1]}}],
        'predicate': valid_predicate(predicate)}).encode()).decode(), 'signatures': [{'sig': 'test-seam'}]}}}
        for predicate in ('https://slsa.dev/provenance/v1', 'https://spdx.dev/Document')]


def test_collection_snapshots_verified_packages_and_discards_hand_refs(inputs, candidate, tmp_path):
    report, paths = inputs
    _, _, package = candidate
    verified = []
    evidence = nightly.collect_post_gate(report, *paths, tmp_path / 'collected',
        attestations=publisher_bundles, artifact_bytes=lambda artifact: package,
        verifier=lambda *args: verified.append(args))
    published = {name for name, row in nightly.generate(*paths).lock['components'].items() if row['artifacts']}
    assert len(verified) == 2 * len(published)
    for field in ('sbom', 'provenance'):
        assert {row['component'] for row in evidence['references'][field]} == published
        assert all('@' + evidence['notesRevision'] + ':attestations/' in row['uri']
                   for row in evidence['references'][field])
        assert all(row['uri'].rsplit('#', 1)[1] == row['sha256'] for row in evidence['references'][field])
    nightly.verify_reference_bundle(evidence, tmp_path / 'collected/release-notes.bundle', *paths, report)
    assert evidence['candidate'] == report['candidate']


@pytest.mark.parametrize('missing', ['sbom', 'provenance'])
def test_collection_requires_both_publisher_predicates(inputs, candidate, tmp_path, missing):
    report, paths = inputs
    _, _, package = candidate
    def bundles(repo, digest):
        rows = publisher_bundles(repo, digest)
        return rows[:1] if missing == 'sbom' else rows[1:]
    with pytest.raises(ValueError, match=f'publisher has no {missing} attestation'):
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected', attestations=bundles,
                                  artifact_bytes=lambda artifact: package, verifier=lambda *args: None)
    assert not (tmp_path / 'collected').exists()


def test_collection_rejects_unverified_package_attestations(inputs, candidate, tmp_path):
    report, paths = inputs
    def refuse(*args):
        raise ValueError('wrong source revision or signature')
    with pytest.raises(ValueError, match='wrong source revision or signature'):
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected', attestations=publisher_bundles,
                                  artifact_bytes=lambda artifact: candidate[2], verifier=refuse)
    assert not (tmp_path / 'collected').exists()


def test_lambda_without_attestations_reports_the_exact_upstream_blocker(inputs, candidate, tmp_path):
    report, paths = inputs
    manifest = yaml.safe_load(paths[0].read_text())
    manifest['components']['honua-server'] = {
        **manifest['components']['sdk'], 'awsLambdaImage': 'ghcr.io/honua-io/honua-server:nightly-lambda-aot-abc-amd64',
        'awsLambdaDigest': 'sha256:' + '6' * 64}
    paths[0].write_text(yaml.safe_dump(manifest))
    report['candidate']['artifacts'][paths[0].name] = {'sha256': _sha256(paths[0]), 'size': paths[0].stat().st_size}
    class Registry:
        def document(self, repository, digest, **kwargs):
            assert digest == 'sha256:' + '6' * 64
            return {'schemaVersion': 2, 'layers': []}
    with pytest.raises(ValueError) as refused:
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected', registry=Registry(),
                                  attestations=lambda repo, digest: [] if digest == 'sha256:' + '6' * 64
                                      else publisher_bundles(repo, digest), artifact_bytes=lambda artifact: candidate[2],
                                  verifier=lambda *args: None)
    assert 'honua-server Lambda: missing sbom' in str(refused.value)
    assert 'honua-server Lambda: missing provenance' in str(refused.value)
    assert 'provenance: false at line 326; sbom: false at line 327' in str(refused.value)
    assert not (tmp_path / 'collected').exists()


def test_reference_bundle_rejects_changed_or_missing_notes_bytes(inputs, tmp_path):
    report, paths = inputs
    refs = fresh_references(report, paths)
    refs['references']['notes'] = refs['references']['notes'].rsplit('#', 1)[0] + '#sha256:' + 'f' * 64
    refuses_before_signing(report, paths, tmp_path, 'document hash differs', post_gate_evidence=refs)


def test_missing_notes_ref_never_falls_back_to_freeze_notes(inputs, tmp_path):
    report, paths = inputs
    refs = fresh_references(report, paths)
    refs['references']['notes'] = None
    refuses_before_signing(report, paths, tmp_path, 'immutable release-notes', post_gate_evidence=refs)


def oci_candidate(report, paths, *, partial=False):
    documents = {}
    def store(value):
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        documents[digest] = value
        return digest
    child = store({'schemaVersion': 2, 'layers': []})
    layers = []
    for predicate in ('https://slsa.dev/provenance/v1', 'https://spdx.dev/Document'):
        digest = store({'predicateType': predicate, 'predicate': valid_predicate(predicate),
                        'subject': [{'digest': {'sha256': child.split(':')[1]}}]})
        layers.append({'mediaType': 'application/vnd.in-toto+json', 'digest': digest})
    attestation = store({'schemaVersion': 2, 'layers': layers})
    children = [{'digest': child, 'platform': {'os': 'linux', 'architecture': 'amd64'}},
                {'digest': attestation, 'platform': {'os': 'unknown', 'architecture': 'unknown'},
                 'annotations': {'vnd.docker.reference.type': 'attestation-manifest'}}]
    if partial:
        children.append({'digest': store({'schemaVersion': 2, 'layers': [], 'arch': 'arm64'}),
                         'platform': {'os': 'linux', 'architecture': 'arm64'}})
    index = store({'schemaVersion': 2, 'manifests': children})
    data = yaml.safe_load(paths[0].read_text())
    data['components']['sdk'].update(image='ghcr.io/honua-io/sdk:test', digest=index,
                                     architectures=['amd64'], platformDigests={'amd64': child})
    paths[0].write_text(yaml.safe_dump(data))
    report['candidate']['artifacts'][paths[0].name] = {'sha256': _sha256(paths[0]), 'size': paths[0].stat().st_size}
    class Registry:
        def document(self, repository, digest, **kwargs):
            assert repository == 'honua-io/sdk'
            return documents[digest]
    return Registry(), index, attestation


def test_registry_refs_bind_the_hashed_attestation_manifest(inputs, candidate, tmp_path):
    report, paths = inputs
    registry, index, attestation = oci_candidate(report, paths)
    refs = nightly.collect_post_gate(report, *paths, tmp_path / 'collected', registry=registry,
        attestations=publisher_bundles, artifact_bytes=lambda artifact: candidate[2], verifier=lambda *args: None)
    for field in ('sbom', 'provenance'):
        assert {'component': 'sdk', 'uri': f'oci://ghcr.io/honua-io/sdk@{attestation}',
                'sha256': attestation} in refs['references'][field]
        assert not any(row['uri'] == f'oci://ghcr.io/honua-io/sdk@{index}' for row in refs['references'][field])


def test_registry_coverage_cannot_hide_an_unattested_architecture(inputs, candidate, tmp_path):
    report, paths = inputs
    registry, index, _ = oci_candidate(report, paths, partial=True)
    with pytest.raises(ValueError, match=r'sdk.artifacts\[0\]: missing'):
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected', registry=registry,
            attestations=lambda repo, digest: [] if digest == index else publisher_bundles(repo, digest),
            artifact_bytes=lambda artifact: candidate[2], verifier=lambda *args: None)
    assert not (tmp_path / 'collected').exists()


def test_signed_index_attestation_can_cover_all_architectures(inputs, candidate, tmp_path):
    report, paths = inputs
    registry, index, _ = oci_candidate(report, paths, partial=True)
    verified = []
    evidence = nightly.collect_post_gate(report, *paths, tmp_path / 'collected', registry=registry,
        attestations=publisher_bundles, artifact_bytes=lambda artifact: candidate[2],
        verifier=lambda *args: verified.append(args))
    assert any(args[0] == f'oci://ghcr.io/honua-io/sdk@{index}' for args in verified)
    nightly.verify_reference_bundle(evidence, tmp_path / 'collected/release-notes.bundle', *paths, report)


@pytest.mark.parametrize('coordinate', ['ghcr.io.evil.test/sdk', 'ghcr.io@evil.test/sdk',
                                       'ghcr.io/honua-io/../sdk', 'ghcr.io/honua-io/sdk?redirect=evil'])
def test_registry_reader_requires_exact_authority_and_repository_path(inputs, candidate, tmp_path, coordinate):
    report, paths = inputs
    registry, _, _ = oci_candidate(report, paths)
    data = yaml.safe_load(paths[0].read_text())
    data['components']['sdk']['image'] = coordinate + ':build'
    paths[0].write_text(yaml.safe_dump(data))
    report['candidate']['artifacts'][paths[0].name] = {'sha256': _sha256(paths[0]), 'size': paths[0].stat().st_size}
    with pytest.raises(ValueError, match='no attestation reader'):
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected', registry=registry,
            attestations=publisher_bundles, artifact_bytes=lambda artifact: candidate[2], verifier=lambda *args: None)


def test_promotion_bundle_verification_accepts_only_the_retained_delta(inputs, tmp_path):
    import platform_lock_bundle as bundle
    report, paths = inputs
    output = tmp_path / 'minted'
    mint(report, paths, tmp_path / 'history', output, signer=signer,
         post_gate_evidence=fresh_references(report, paths))
    lock = json.loads((output / 'platform-lock.json').read_bytes())
    bundle.bind_post_gate(lock, output / paths[0].name, paths[1], '2026.1-rc.3', output)
    frozen = json.loads((output / 'qualification-lock.json').read_bytes())
    frozen['provenance'] = []
    (output / 'qualification-lock.json').write_text(json.dumps(frozen))
    with pytest.raises(ValueError):
        bundle.bind_post_gate(lock, output / paths[0].name, paths[1], '2026.1-rc.3', output)


@pytest.mark.parametrize('kind', ['npm', 'nuget', 'wheel', 'spec', 'archive'])
def test_published_byte_reader_checks_the_locked_hash(monkeypatch, kind):
    import verify_client_artifacts as client
    package = b'locked published bytes'
    digest = 'sha256:' + hashlib.sha256(package).hexdigest()
    coordinate = 'Honua.Package' if kind in {'npm', 'nuget', 'wheel'} else \
        'https://github.com/honua-io/component/blob/' + 'a' * 40 + '/spec.json'
    artifact = {'kind': kind, 'coordinate': coordinate, 'version': '1.2.3', 'sha256': digest}
    if kind == 'npm':
        artifact['integrity'] = 'sha512-' + base64.b64encode(hashlib.sha512(package).digest()).decode()
    monkeypatch.setattr(client, '_request_json', lambda url: {
        'dist': {'tarball': 'https://registry.npmjs.org/package.tgz'},
        'urls': [{'digests': {'sha256': digest.split(':')[1]}, 'url': 'https://files.pythonhosted.org/package.whl'}]})
    monkeypatch.setattr(client, '_request', lambda url: package)
    assert nightly.published_artifact_bytes(artifact) == package
    monkeypatch.setattr(client, '_request', lambda url: b'different bytes')
    with pytest.raises(ValueError, match='does not match its locked hash'):
        nightly.published_artifact_bytes(artifact)


@pytest.mark.parametrize('status', ['fail', 'skipped', 'blocked', 'cancelled', 'unknown', ''])
def test_any_red_or_incomplete_gate_mints_nothing(inputs, tmp_path, status):
    report, paths = inputs
    report['gates'][0]['status'] = status
    calls = []
    with pytest.raises(ValueError, match='no lock minted'):
        mint(report, paths, tmp_path / 'history', tmp_path / 'minted',
             signer=lambda *args: calls.append(args))
    assert not calls
    assert not (tmp_path / 'minted').exists()


def test_missing_journey_is_red(inputs, tmp_path):
    report, paths = inputs
    report['gates'] = [r for r in report['gates'] if r['gate'] != 'journey']
    with pytest.raises(ValueError, match='journey: missing'):
        mint(report, paths, tmp_path / 'history', tmp_path / 'minted', signer=signer)
    assert not (tmp_path / 'minted').exists()


@pytest.mark.parametrize('field,value', [('dry_run', True), ('overallStatus', 'blocked')])
def test_only_strict_green_can_mint(inputs, tmp_path, field, value):
    report, paths = inputs
    report[field] = value
    with pytest.raises(ValueError):
        mint(report, paths, tmp_path / 'history', tmp_path / 'minted', signer=signer)
    assert not (tmp_path / 'minted').exists()


def test_increment_from_existing_lock_ids_not_lexical_order(tmp_path):
    assert nightly.next_label(tmp_path) == '2026.1-rc.3'
    for index, label in enumerate(['2026.1-rc.3', '2026.1-rc.9', '2026.1-rc.12', '2026.1.1-rc.40']):
        path = tmp_path / str(index)
        path.mkdir()
        (path / 'platform-lock.json').write_text(json.dumps({'platform': {'id': 'honua-' + label}}))
    assert nightly.next_label(tmp_path) == '2026.1-rc.13'


def test_candidate_byte_drift_cannot_be_signed(inputs, tmp_path):
    report, paths = inputs
    paths[0].write_text(paths[0].read_text() + '# drift\n')
    with pytest.raises(ValueError, match='not bound'):
        mint(report, paths, tmp_path / 'history', tmp_path / 'minted', signer=signer)
    assert not (tmp_path / 'minted').exists()


def test_stamp_records_the_next_label_and_creates_no_tag(inputs, tmp_path, monkeypatch):
    _, paths = inputs
    calls = []
    monkeypatch.setattr(nightly.subprocess, 'run', lambda *args, **kwargs: calls.append(args))
    nightly.stamp_release_label(paths[0], '2026.1-rc.3')
    manifest = yaml.safe_load(paths[0].read_text())
    assert manifest['platformRelease'] == '2026.1-rc.3'
    assert manifest['status'] == 'rc'
    assert calls == []


def test_signing_failure_leaves_nothing(inputs, tmp_path):
    report, paths = inputs
    def broken(*_):
        raise ValueError('signature verification failed')
    with pytest.raises(ValueError, match='signature verification failed'):
        mint(report, paths, tmp_path / 'history', tmp_path / 'minted', signer=broken)
    assert not (tmp_path / 'minted').exists()
    assert not list(tmp_path.glob('.nightly-*'))


def refuses_before_signing(report, paths, tmp_path, match, **overrides):
    calls = []
    with pytest.raises(ValueError, match=match):
        mint(report, paths, tmp_path / 'history', tmp_path / 'minted',
             signer=lambda *args: calls.append(args), **overrides)
    assert calls == [], 'cosign must never run for a refused lock'
    assert not (tmp_path / 'minted').exists()


@pytest.mark.parametrize('branch', ['release-386-nightly-train', 'main', '', None])
def test_a_report_from_any_branch_but_trunk_mints_nothing(inputs, tmp_path, branch):
    report, paths = inputs
    report['candidate']['source']['branch'] = branch
    refuses_before_signing(report, paths, tmp_path, 'source branch .* is not trunk')


@pytest.mark.parametrize('workflow', ['.github/workflows/release-train.yml',
                                      '.github/workflows/stubbed-nightly.yml', None])
def test_a_report_from_any_workflow_but_the_nightly_mints_nothing(inputs, tmp_path, workflow):
    report, paths = inputs
    report['candidate']['train']['workflowPath'] = workflow
    refuses_before_signing(report, paths, tmp_path, 'is not .github/workflows/nightly-certification.yml')


def test_a_report_from_another_repository_run_or_commit_mints_nothing(inputs, tmp_path):
    report, paths = inputs
    forked = copy.deepcopy(report)
    forked['candidate']['source']['repository'] = 'someone/honua-release'
    refuses_before_signing(forked, paths, tmp_path, 'source repository')
    refuses_before_signing(report, paths, tmp_path, 'source sha', source_sha='e' * 40)
    refuses_before_signing(report, paths, tmp_path, 'train run', run_id='9999')


def test_a_missing_binding_mints_nothing(inputs, tmp_path):
    report, paths = inputs
    del report['candidate']['source']
    refuses_before_signing(report, paths, tmp_path, 'source branch')


@pytest.mark.parametrize('rulesets', [
    None, [],
    [{**PROTECTED[0], 'enforcement': 'evaluate'}],
    [{**PROTECTED[0], 'target': 'branch'}],
    [{**PROTECTED[0], 'rules': [{'type': 'update'}]}],
    [{**PROTECTED[0], 'rules': [{'type': 'deletion'}]}],
    [{**PROTECTED[0], 'conditions': {'ref_name': {'include': ['refs/tags/v*'], 'exclude': []}}}],
    [{**PROTECTED[0], 'conditions': {'ref_name': {'include': ['~ALL'],
                                                  'exclude': ['refs/tags/nightly-lock/*']}}}],
])
def test_unprotected_lock_refs_mint_nothing(inputs, tmp_path, rulesets):
    report, paths = inputs
    refuses_before_signing(report, paths, tmp_path, 'no active tag ruleset', rulesets=rulesets)


def test_ruleset_patterns_cover_the_next_lock_ref():
    ref = 'refs/tags/nightly-lock/2026.1-rc.3'
    for include in (['~ALL'], ['refs/tags/nightly-lock/*'], ['refs/tags/nightly-lock/**'], ['refs/tags/**']):
        rules = [{**PROTECTED[0], 'conditions': {'ref_name': {'include': include, 'exclude': []}}}]
        assert nightly.lock_ref_protected(rules, ref), include
    narrow = [{**PROTECTED[0], 'conditions': {'ref_name': {'include': ['refs/tags/*'], 'exclude': []}}}]
    assert not nightly.lock_ref_protected(narrow, ref)


@pytest.mark.parametrize('published', [None, {'2026.1-rc.3': 'a' * 40}])
def test_unknown_or_already_published_label_mints_nothing(inputs, tmp_path, published):
    report, paths = inputs
    refuses_before_signing(report, paths, tmp_path, 'not known to be unpublished', published=published)


def test_channel_tag_refuses_before_signing(inputs, tmp_path, monkeypatch):
    report, paths = inputs
    real = nightly.bundle_files

    def tagged(lock):
        files = dict(real(lock))
        files['platform-lock.json'] = files['platform-lock.json'].replace(
            b'"honua-2026.1-rc.3"', b'"ghcr.io/honua-io/honua-server:latest"', 1)
        return files

    monkeypatch.setattr(nightly, 'bundle_files', tagged)
    refuses_before_signing(report, paths, tmp_path, 'channel tag')


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def publish_lock(work, label, platform_id=None):
    git(work, 'checkout', '-q', '--orphan', f'lock-{label}')
    git(work, 'rm', '-rfq', '--ignore-unmatch', '.')
    (work / 'platform-lock.json').write_text(json.dumps({'platform': {'id': platform_id or 'honua-' + label}}))
    git(work, 'add', 'platform-lock.json')
    git(work, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', label)
    git(work, 'push', '-q', 'origin', f'HEAD:refs/tags/nightly-lock/{label}')
    return git(work, 'rev-parse', 'HEAD')


@pytest.fixture
def remote(tmp_path):
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    work, clone = tmp_path / 'publisher', tmp_path / 'runner'
    for path in (work, clone):
        subprocess.run(['git', 'clone', '-q', str(origin), str(path)], check=True, capture_output=True)
    return work, clone


def test_sync_history_reads_every_published_lock(remote, tmp_path):
    work, clone = remote
    shas = {label: publish_lock(work, label) for label in ('2026.1-rc.3', '2026.1-rc.12')}
    assert nightly.sync_history(tmp_path / 'history', clone) == shas
    assert nightly.next_label(tmp_path / 'history') == '2026.1-rc.13'


def test_sync_history_with_no_locks_is_the_first_label(remote, tmp_path):
    _, clone = remote
    assert nightly.sync_history(tmp_path / 'history', clone) == {}
    assert nightly.next_label(tmp_path / 'history') == '2026.1-rc.3'


def test_unreachable_remote_refuses_instead_of_empty_history(remote, tmp_path):
    work, clone = remote
    publish_lock(work, '2026.1-rc.12')
    git(clone, 'remote', 'set-url', 'origin', str(tmp_path / 'gone.git'))
    with pytest.raises(ValueError, match='ls-remote'):
        nightly.sync_history(tmp_path / 'history', clone, sleep=lambda _: None)


def test_failed_fetch_after_listing_refuses(remote, tmp_path):
    work, clone = remote
    publish_lock(work, '2026.1-rc.12')

    def run(cmd, **kwargs):
        if cmd[1] == 'fetch':
            return subprocess.CompletedProcess(cmd, 128, b'', b'fatal: remote error: access denied')
        return subprocess.run(cmd, **kwargs)

    with pytest.raises(ValueError, match='fetch'):
        nightly.sync_history(tmp_path / 'history', clone, run=run, sleep=lambda _: None)


def test_transient_fetch_is_retried_then_refuses(remote, tmp_path):
    work, clone = remote
    publish_lock(work, '2026.1-rc.12')
    sleeps = []

    def run(cmd, **kwargs):
        if cmd[1] == 'fetch':
            return subprocess.CompletedProcess(cmd, 128, b'', b'fatal: unable to access: Could not resolve host')
        return subprocess.run(cmd, **kwargs)

    with pytest.raises(ValueError, match='Could not resolve host'):
        nightly.sync_history(tmp_path / 'history', clone, run=run, sleep=sleeps.append)
    assert sleeps == [10, 30, 60, 120, 60]


def test_lock_whose_id_differs_from_its_ref_refuses(remote, tmp_path):
    work, clone = remote
    publish_lock(work, '2026.1-rc.12', platform_id='honua-2026.1-rc.2')
    with pytest.raises(ValueError, match='records'):
        nightly.sync_history(tmp_path / 'history', clone)


def test_cli_refuses_to_stamp_or_mint_without_synced_history(inputs, tmp_path):
    _, paths = inputs
    script = str(Path(nightly.__file__))
    stamp = subprocess.run([sys.executable, script, '--stamp', str(paths[0]),
                            '--history', str(tmp_path / 'h')], capture_output=True, text=True)
    assert stamp.returncode == 1 and 'requires --sync-from' in stamp.stderr
    minted = subprocess.run([sys.executable, script, '--report', str(tmp_path / 'r.json'),
                             '--manifest', str(paths[0]), '--matrix', str(paths[1]),
                             '--certificate-identity', 'x'], capture_output=True, text=True)
    assert minted.returncode == 1 and 'rulesets' in minted.stderr


NIGHTLY_EXPECTED = ('build-test', 'contract', 'sbom', 'security', 'upgrade', 'capacity-soak', 'dr',
                    'lambda-certification', 'protocol-ledger', 'deterministic-journey', 'nightly-model-journey',
                    'executable-docs', 'installed-clients')
QUALIFYING_EXPECTED = ('genuine-model-journey', 'update-rollback', 'esri-bundle', 'cite')


def test_minted_layout_retains_every_declared_receipt_and_no_qualifying_receipt(inputs, tmp_path):
    report, paths = inputs
    output = tmp_path / 'minted'
    mint(report, paths, tmp_path / 'history', output, signer=signer)
    retained = json.loads((output / 'gate-report.json').read_text())
    digest = 'sha256:' + _sha256(output / 'platform-lock.json')
    assert set(retained['evidenceClasses']) == set(NIGHTLY_EXPECTED)
    assert set(retained['evidenceDeclarations']) == set(NIGHTLY_EXPECTED + QUALIFYING_EXPECTED)
    for name in NIGHTLY_EXPECTED:
        declaration = retained['evidenceDeclarations'][name]
        receipt = json.loads((output / declaration['receipt']).read_text())
        assert receipt['class'] == name and receipt['kind'] == declaration['kind'] == 'nightly'
        assert receipt['status'] == 'pass' and receipt['lockDigest'] == digest
        assert receipt['runId'] == RUN and receipt['runAttempt'] == 1
        assert receipt['completedAt'] == retained['generatedAt']
        assert receipt['freshUntil'] == declaration['freshUntil']
        assert (datetime.fromisoformat(receipt['freshUntil'].replace('Z', '+00:00')) -
                datetime.fromisoformat(receipt['completedAt'].replace('Z', '+00:00'))).days == 7
    for name in QUALIFYING_EXPECTED:
        assert retained['evidenceDeclarations'][name] == {'kind': 'qualifying', 'receipt': None, 'freshUntil': None}
        assert not (output / 'promotion-receipts' / name).exists()
    assert len(list((output / 'promotion-receipts').glob('*/receipt.json'))) == len(NIGHTLY_EXPECTED) == 13


@pytest.mark.parametrize('mutation', ['missing-class', 'wrong-lock', 'missing-model', 'forged-qualifying', 'expiry'])
def test_receipt_gaps_refuse_before_signing(inputs, tmp_path, mutation):
    report, paths = inputs
    if mutation == 'missing-class':
        del report['evidenceReceipts']['contract']
    elif mutation == 'wrong-lock':
        report['evidenceReceipts']['contract']['lockDigest'] = 'sha256:' + 'a' * 64
    elif mutation == 'missing-model':
        report['evidenceReceipts']['nightly-model-journey']['cells'] = []
    elif mutation == 'forged-qualifying':
        report['evidenceDeclarations']['cite']['receipt'] = 'forged.json'
    elif mutation == 'expiry':
        report['evidenceReceipts']['contract']['freshUntil'] = '2099-01-01T00:00:00Z'
    refuses_before_signing(report, paths, tmp_path, 'receipt|qualifying|declarations')


def test_missing_model_observation_cannot_be_turned_into_a_passing_receipt(inputs, tmp_path):
    report, paths = inputs
    lock = tmp_path / 'qualification-lock.json'
    declared = nightly.declare_evidence(report, lock, [])
    assert declared['evidenceReceipts']['deterministic-journey']['status'] == 'fail'
    assert declared['evidenceReceipts']['nightly-model-journey']['status'] == 'fail'
    refuses_before_signing(declared, paths, tmp_path, 'nightly receipt')


def test_installed_client_gate_must_pass_to_mint(inputs, tmp_path):
    # installed-clients is a nightly class (#381/#386): its receipt follows the train's
    # installed-clients gate, and a matrix-declared blocker (blocked) is not a pass.
    report, _ = inputs
    assert nightly.CLASS_GATES['installed-clients'] == 'installed-clients'
    assert 'installed-clients' in nightly.REQUIRED_NIGHTLY_GATES
    lock = tmp_path / 'qualification-lock.json'
    for verdict in ('blocked', 'fail'):
        changed = json.loads(json.dumps(report))
        for row in changed['gates']:
            if row['gate'] == 'installed-clients':
                row['status'] = verdict
        declared = nightly.declare_evidence(changed, lock, [])
        assert declared['evidenceReceipts']['installed-clients']['status'] == verdict
        assert 'installed-clients: invalid or missing nightly receipt' in nightly.evidence_failures(declared)
    missing = json.loads(json.dumps(report))
    missing['gates'] = [row for row in missing['gates'] if row['gate'] != 'installed-clients']
    declared = nightly.declare_evidence(missing, lock, [])
    assert declared['evidenceReceipts']['installed-clients']['status'] == 'missing'
    assert 'installed-clients: invalid or missing nightly receipt' in nightly.evidence_failures(declared)


# release#231 WI-8: the gates that check out fixture repositories say which revisions they used,
# and the mint declares the lock's $.fixtures from exactly those records.

GATE_USES = {
    ('certification', 'conformance-mcp'): [
        {'repository': 'https://github.com/honua-io/geospatial-mcp', 'revision': '1' * 40}],
    ('e2e-local-docker', 'seam'): [
        {'repository': 'https://github.com/honua-io/honua-sdk-python', 'revision': '2' * 40},
        {'repository': 'https://github.com/honua-io/honua-sdk-dotnet', 'revision': '3' * 40}],
    ('gate-dr', 'contract'): [{'repository': 'https://github.com/honua-io/honua-server', 'revision': '4' * 40}],
    ('gate-dr', 'receipt'): [{'repository': 'https://github.com/honua-io/honua-server', 'revision': '4' * 40}],
    ('gate-observability', 'slo'): [
        {'repository': 'https://github.com/honua-io/honua-devops', 'revision': '6' * 40}],
    ('terminal-journey-contract', 'terminal-contract'): [
        {'repository': 'https://github.com/honua-io/honua-release', 'revision': '5' * 40,
         'path': 'certification/terminal-journey/fixtures'}],
}


def test_mint_declares_fixtures_from_every_gate_record(candidate, tmp_path):
    gate_records = records(GATE_USES)
    declared = fixture_revisions.declare(gate_records, run_id=RUN)
    report, paths = build_inputs(candidate, tmp_path, gate_fixtures=declared)
    assert 'fixtures' not in yaml.safe_load(paths[0].read_text())['platformLockEvidence']
    output = tmp_path / 'minted'
    assert mint(report, paths, tmp_path / 'history', output, signer=signer,
                fixture_records=gate_records) == '2026.1-rc.3'
    lock = json.loads((output / 'platform-lock.json').read_bytes())
    assert lock['fixtures'] == declared
    # One entry per repository; the two gate-dr jobs agree, untouched jobs report the default repo.
    assert {f['repository'].rsplit('/', 1)[1]: f['revision'] for f in lock['fixtures']} == {
        'fixtures': 'a' * 40, 'geospatial-mcp': '1' * 40, 'honua-sdk-python': '2' * 40,
        'honua-sdk-dotnet': '3' * 40, 'honua-server': '4' * 40, 'honua-release': '5' * 40,
        'honua-devops': '6' * 40}
    assert {'repository': 'https://github.com/honua-io/honua-release', 'revision': '5' * 40,
            'path': 'certification/terminal-journey/fixtures'} in lock['fixtures']
    # Promotion checks the canonical manifest and the report binding after overlaying the bundle.
    shipped = output / 'platform-manifest.yaml'
    assert lock['sourceInputs']['platformManifest']['sha256'] == 'sha256:' + _sha256(shipped)
    assert yaml.safe_load(shipped.read_text())['platformLockEvidence']['fixtures'] == declared
    assert report['candidate']['artifacts']['platform-manifest.yaml']['sha256'] == _sha256(paths[0])
    retained_report = json.loads((output / 'gate-report.json').read_bytes())
    assert retained_report['candidate']['artifacts']['platform-manifest.yaml'] == {
        'sha256': _sha256(shipped), 'size': shipped.stat().st_size}
    expected_receipts = copy.deepcopy(report['evidenceReceipts'])
    for receipt in expected_receipts.values():
        receipt['lockDigest'] = 'sha256:' + _sha256(output / 'platform-lock.json')
        for cell in receipt.get('cells', []):
            for attempt in cell.get('attempts', []):
                attempt['lockDigest'] = receipt['lockDigest']
    assert retained_report['evidenceReceipts'] == expected_receipts
    assert json.loads((output / 'qualification-gate-report.json').read_bytes()) == report
    assert (output / 'qualification-inputs' / 'platform-manifest.yaml').read_bytes() == paths[0].read_bytes()
    assert json.loads((output / 'qualification-inputs' / 'gate-report.json').read_bytes()) == report
    (output / paths[1].name).write_bytes(paths[1].read_bytes())
    ok, why = verify_candidate_binding(retained_report, shipped, output / paths[1].name,
        source_repository='honua-io/honua-release', source_sha=SOURCE, source_branch='trunk',
        workflow_path='.github/workflows/nightly-certification.yml', train_run_id=RUN,
        train_run_attempt=1, train_run_url=report['candidate']['train']['runUrl'], certification_mode='live')
    assert ok, why
    checked = subprocess.run([sys.executable, str(Path(nightly.__file__).with_name('platform_lock_bundle.py')),
                              str(output / 'platform-lock.json'), '--manifest', str(shipped),
                              '--matrix', str(output / paths[1].name), '--label', '2026.1-rc.3',
                              '--out-dir', str(output), '--check'], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    retained = json.loads((output / 'fixture-revisions.json').read_bytes())
    assert retained['fixtures'] == declared and len(retained['records']) == 8


def test_a_candidate_that_declares_the_gate_fixtures_keeps_its_exact_bytes(inputs, tmp_path):
    report, paths = inputs
    output = tmp_path / 'minted'
    mint(report, paths, tmp_path / 'history', output, signer=signer)
    lock = json.loads((output / 'platform-lock.json').read_bytes())
    assert lock['fixtures'] == [DECLARED]
    assert lock['sourceInputs']['platformManifest']['sha256'] == 'sha256:' + _sha256(output / paths[0].name)
    assert not (output / 'fixture-declaration').exists()


@pytest.mark.parametrize('gate,job', [(gate, job) for gate, jobs in fixture_revisions.FIXTURE_GATES.items()
                                      for job in jobs])
def test_a_gate_that_emitted_no_fixture_revision_mints_nothing(inputs, tmp_path, gate, job):
    report, paths = inputs
    remaining = [r for r in records() if (r['gate'], r['job']) != (gate, job)]
    refuses_before_signing(report, paths, tmp_path, f'{gate}/{job}: fixture revisions not emitted',
                           fixture_records=remaining)


@pytest.mark.parametrize('fixtures', [
    [], [{'repository': 'https://github.com/honua-io/fixtures', 'revision': ''}],
    [{'repository': 'https://github.com/honua-io/fixtures', 'revision': 'trunk'}],
    [{'repository': 'https://github.com/honua-io/fixtures'}], [{'revision': 'a' * 40}]])
def test_a_missing_fixture_revision_mints_nothing(inputs, tmp_path, fixtures):
    report, paths = inputs
    gate_records = records({('e2e-local-docker', 'slice1'): fixtures})
    refuses_before_signing(report, paths, tmp_path, r'e2e-local-docker/slice1: (no fixture revisions emitted|'
                           r'fixture revision of .* is missing)', fixture_records=gate_records)


def test_two_gates_that_disagree_about_one_repository_mint_nothing(inputs, tmp_path):
    report, paths = inputs
    server = 'https://github.com/honua-io/honua-server'
    gate_records = records({('gate-dr', 'contract'): [{'repository': server, 'revision': '4' * 40}],
                            ('certification', 'conformance-mcp'): [{'repository': server, 'revision': '6' * 40}]})
    refuses_before_signing(report, paths, tmp_path,
                           'honua-server: gates disagree about the fixture revision: '
                           f'certification/conformance-mcp@{"6" * 40}, gate-dr/contract@{"4" * 40}',
                           fixture_records=gate_records)


def test_a_candidate_declaration_the_gates_did_not_use_mints_nothing(inputs, tmp_path):
    report, paths = inputs
    gate_records = records({('gate-dr', 'contract'): [
        {'repository': 'https://github.com/honua-io/honua-server', 'revision': '4' * 40}]})
    refuses_before_signing(report, paths, tmp_path, 'candidate fixture declaration differs',
                           fixture_records=gate_records)


def test_records_from_another_run_or_job_mint_nothing(inputs, tmp_path):
    report, paths = inputs
    refuses_before_signing(report, paths, tmp_path, 'not this run 4242', fixture_records=records(run_id='1'))
    stray = records() + [{**records()[0], 'job': 'not-a-fixture-job'}]
    refuses_before_signing(report, paths, tmp_path, 'not a fixture gate', fixture_records=stray)
    refuses_before_signing(report, paths, tmp_path, 'more than one', fixture_records=records() + records()[:1])


def _checkout(path, repository):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    git(path, 'remote', 'add', 'origin', f'https://github.com/{repository}')
    (path / 'README').write_text(repository)
    git(path, 'add', 'README')
    git(path, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'fixture')
    return git(path, 'rev-parse', 'HEAD')


def test_emit_records_the_revision_git_reports_not_the_requested_ref(tmp_path):
    sha = _checkout(tmp_path / 'sdk', 'honua-io/honua-sdk-python')
    _checkout(tmp_path / 'other', 'honua-io/honua-site')
    (tmp_path / 'sdk' / 'inside').mkdir()
    out = tmp_path / 'out' / fixture_revisions.RECORD
    done = subprocess.run([sys.executable, str(Path(fixture_revisions.__file__)), 'emit', '--gate', 'e2e-local-docker',
                           '--job', 'seam', '--run-id', RUN, '--run-attempt', '2', '--out', str(out),
                           f'honua-io/honua-sdk-python={tmp_path / "sdk"}=tests/fixtures',
                           f'honua-io/honua-sdk-dotnet={tmp_path / "never-checked-out"}',
                           f'honua-io/honua-console={tmp_path / "other"}',
                           f'honua-io/honua-sdk-python={tmp_path / "sdk" / "inside"}'],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    record = json.loads(out.read_text())
    assert record['gate'] == 'e2e-local-docker' and record['job'] == 'seam'
    assert record['runId'] == RUN and record['runAttempt'] == '2'
    assert record['fixtures'] == [
        {'repository': 'https://github.com/honua-io/honua-sdk-python', 'revision': sha, 'path': 'tests/fixtures'},
        # Not a checkout, a checkout of another repository, or a directory inside a checkout:
        # each is a missing revision, never a plausible one.
        {'repository': 'https://github.com/honua-io/honua-sdk-dotnet', 'revision': ''},
        {'repository': 'https://github.com/honua-io/honua-console', 'revision': ''},
        {'repository': 'https://github.com/honua-io/honua-sdk-python', 'revision': ''},
    ]
    with pytest.raises(ValueError, match='fixture revision of .*honua-sdk-dotnet is missing'):
        fixture_revisions.declare([record], run_id=RUN, gates={'e2e-local-docker': ('seam',)})


def test_emit_refuses_a_job_that_is_not_a_fixture_job(tmp_path):
    done = subprocess.run([sys.executable, str(Path(fixture_revisions.__file__)), 'emit', '--gate', 'gate-dr',
                           '--job', 'seam', '--run-id', RUN, '--run-attempt', '1',
                           '--out', str(tmp_path / 'r.json'), f'honua-io/honua-server={tmp_path}'],
                          capture_output=True, text=True)
    assert done.returncode == 1 and 'no fixture job' in done.stderr


def _workflow(name):
    return yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows' / name).read_text())


def test_every_fixture_gate_job_records_each_repository_it_checks_out():
    for gate, jobs in fixture_revisions.FIXTURE_GATES.items():
        workflow = _workflow(f'{gate}.yml')
        assert set(jobs) <= set(workflow['jobs']), gate
        for job in jobs:
            steps = workflow['jobs'][job]['steps']
            emit = [i for i, step in enumerate(steps) if 'tools/fixture_revisions.py" emit' in step.get('run', '')]
            assert len(emit) == 1, f'{gate}/{job}'
            step = steps[emit[0]]
            assert f'--gate {gate} --job {job}' in step['run']
            # Recording never reddens a gate, and a cancelled job records nothing.
            assert step['continue-on-error'] is True and step['if'] == '${{ !cancelled() }}'
            upload = steps[emit[0] + 1]
            assert upload['uses'].startswith('actions/upload-artifact@')
            assert upload['continue-on-error'] is True and upload['if'] == '${{ !cancelled() }}'
            assert upload['with']['name'] == f'fixture-revisions-{gate}-{job}'
            assert upload['with']['path'] == '${{ runner.temp }}/fixture-revisions/fixture-revisions.json'
            assert upload['with']['if-no-files-found'] == 'error'
            # Every external repository this job checks out is recorded from its checkout directory.
            for index, checkout in enumerate(steps):
                options = checkout.get('with') or {}
                if str(checkout.get('uses', '')).startswith('actions/checkout@') and options.get('repository'):
                    assert index < emit[0], f'{gate}/{job} records before checking out {options["path"]}'
                    assert f'={options["path"]}"' in step['run'], f'{gate}/{job}: {options["path"]}'
    # The two checkouts that happen inside shell steps, and the in-repository fixtures.
    certification = _workflow('certification.yml')['jobs']
    assert '"honua-io/geospatial-mcp=$RUNNER_TEMP/mcp/mcp"' in next(
        s['run'] for s in certification['conformance-mcp']['steps'] if 'fixture_revisions.py' in s.get('run', ''))
    assert 'checkout_component.sh" geospatial-mcp "$SHA" "$WORK/mcp"' in next(
        s['run'] for s in certification['conformance-mcp']['steps'] if s.get('id') == 'consume')
    terminal = _workflow('terminal-journey-contract.yml')['jobs']['terminal-contract']['steps']
    assert '"honua-io/honua-release=.=certification/terminal-journey/fixtures"' in next(
        s['run'] for s in terminal if 'fixture_revisions.py' in s.get('run', ''))


def test_observability_records_its_pinned_rules_and_contract_consumers():
    assert fixture_revisions.FIXTURE_GATES['gate-observability'] == ('slo',)
    steps = _workflow('gate-observability.yml')['jobs']['slo']['steps']
    clone = next(i for i, s in enumerate(steps) if s.get('name') == 'Clone the alert-rule consumers')
    emit = next(i for i, s in enumerate(steps) if 'fixture_revisions.py' in s.get('run', ''))
    contract = next(i for i, s in enumerate(steps) if s.get('id') == 'contract')
    assert clone < emit < contract
    assert fixture_revisions.SHA.fullmatch(steps[clone]['env']['DEVOPS_REVISION'])
    assert 'checkout_component.sh" honua-devops "$DEVOPS_REVISION"' in steps[clone]['run']
    for repo in ('honua-devops', 'honua-server', 'honua-helm'):
        assert f'"honua-io/{repo}=$REPOS_ROOT/{repo}"' in steps[emit]['run']


def test_terminal_concurrency_is_isolated_from_standalone_runs():
    concurrency = _workflow('terminal-journey-contract.yml')['concurrency']
    group = concurrency['group']
    # github.workflow is the top-level caller, including for nested reusable workflows.
    standalone = group.replace('${{ github.workflow }}', 'Terminal journey contract')
    nightly = group.replace('${{ github.workflow }}', _workflow('nightly-certification.yml')['name'])
    assert standalone != nightly
    assert '${{ github.ref }}' in group
    assert concurrency['cancel-in-progress'] is True


def test_the_nightly_train_runs_every_fixture_gate_and_mints_from_their_records():
    train = _workflow('release-train.yml')['jobs']
    called = {str(job.get('uses', '')).removeprefix('./.github/workflows/').removesuffix('.yml')
              for job in train.values()}
    assert set(fixture_revisions.FIXTURE_GATES) <= called
    assert 'workflow_call' in _workflow('terminal-journey-contract.yml')[True]
    mint_steps = _workflow('nightly-certification.yml')['jobs']['mint']['steps']
    download = next(i for i, s in enumerate(mint_steps)
                    if (s.get('with') or {}).get('pattern') == 'fixture-revisions-*')
    assert mint_steps[download]['with']['path'] == 'fixture-revisions'
    assert 'merge-multiple' not in mint_steps[download]['with']
    signing = next(i for i, s in enumerate(mint_steps) if s.get('id') == 'mint')
    assert download < signing
    assert '--fixture-revisions fixture-revisions' in mint_steps[signing]['run']


def test_post_gate_collection_is_live_green_and_precedes_qualified_bundle_upload():
    report_job = _workflow('release-train.yml')['jobs']['report']
    assert 'gate_sbom' in report_job['needs']
    steps = report_job['steps']
    collection = next(i for i, step in enumerate(steps) if '--collect-post-gate' in step.get('run', ''))
    binding = next(i for i, step in enumerate(steps) if '--declare-evidence' in step.get('run', ''))
    upload = next(i for i, step in enumerate(steps) if step.get('name') == 'Upload the certified candidate bundle')
    assert binding < collection < upload
    assert steps[collection]['if'] == "inputs.nightly && inputs.dry_run == false && steps.assemble.outputs.overall == 'pass'"
    assert 'qualification-lock.json' in steps[collection]['run']
    assert any('pip install' in step.get('run', '') and 'jsonschema' in step['run'] for step in steps)
    command = next(step['run'] for step in _workflow('nightly-certification.yml')['jobs']['mint']['steps']
                   if step.get('id') == 'mint')
    for argument in ('--lock certified/qualification-lock.json',
                     '--post-gate-references certified/post-gate-references.json',
                     '--notes-bundle certified/release-notes.bundle'):
        assert argument in command


def test_cli_mint_requires_fixture_revisions(inputs, tmp_path):
    _, paths = inputs
    minted = subprocess.run([sys.executable, str(Path(nightly.__file__)), '--report', str(tmp_path / 'r.json'),
                             '--manifest', str(paths[0]), '--matrix', str(paths[1]), '--certificate-identity', 'x',
                             '--rulesets', str(tmp_path / 'rules.json'), '--expected-source-sha', SOURCE,
                             '--expected-run-id', RUN], capture_output=True, text=True)
    assert minted.returncode == 1 and 'fixture revisions are required' in minted.stderr


@pytest.mark.parametrize('kind', list(nightly.PREDICATES))
def test_statement_requires_real_predicate_and_exact_subject(kind):
    subject = 'sha256:' + 'a' * 64
    statement = {'predicateType': kind, 'subject': [{'digest': {'sha256': 'a' * 64}}],
                 'predicate': valid_predicate(kind)}
    assert nightly.statement_field(statement, {subject}) == nightly.PREDICATES[kind]
    assert nightly.statement_field(statement, {'sha256:' + 'b' * 64}) is None
    for malformed in (None, {}, [], 'document', {'buildDefinition': {}, 'runDetails': {}},
                      {'bomFormat': 'CycloneDX', 'components': []}):
        statement['predicate'] = malformed
        assert nightly.statement_field(statement, {subject}) is None


@pytest.mark.parametrize('kind,mutation', [
    ('https://spdx.dev/Document', lambda p: p.pop('creationInfo')),
    ('https://spdx.dev/Document', lambda p: p.update(packages=[{}])),
    ('https://spdx.dev/Document', lambda p: p.update(documentNamespace='not-a-uri')),
    ('https://spdx.dev/Document', lambda p: p['packages'][0].pop('downloadLocation')),
    ('https://spdx.dev/Document', lambda p: p['packages'][0].update(downloadLocation='')),
    ('https://spdx.dev/Document', lambda p: p['packages'][0].update(filesAnalyzed=1)),
    ('https://cyclonedx.org/bom', lambda p: p.update(version=True)),
    ('https://cyclonedx.org/bom', lambda p: p.update(components=[{'name': 'missing-type'}])),
    ('https://slsa.dev/provenance/v1', lambda p: p['buildDefinition'].update(buildType='')),
    ('https://slsa.dev/provenance/v1', lambda p: p['buildDefinition'].update(externalParameters=[])),
    ('https://slsa.dev/provenance/v1', lambda p: p['runDetails'].update(builder={})),
    ('https://slsa.dev/provenance/v0.2', lambda p: p.update(invocation=[])),
])
def test_statement_rejects_malformed_document_build_and_run_structures(kind, mutation):
    predicate = valid_predicate(kind)
    mutation(predicate)
    assert nightly.statement_field({'predicateType': kind, 'predicate': predicate,
        'subject': [{'digest': {'sha256': 'a' * 64}}]}, {'sha256:' + 'a' * 64}) is None


def test_collection_refuses_empty_publisher_predicates(inputs, candidate, tmp_path):
    report, paths = inputs
    def empty_bundles(repository, digest):
        rows = publisher_bundles(repository, digest)
        for row in rows:
            envelope = row['bundle']['dsseEnvelope']
            statement = json.loads(base64.b64decode(envelope['payload']))
            statement['predicate'] = {}
            envelope['payload'] = base64.b64encode(json.dumps(statement).encode()).decode()
        return rows
    verified = []
    with pytest.raises(ValueError, match='publisher has no .* attestation'):
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected', attestations=empty_bundles,
            artifact_bytes=lambda artifact: candidate[2], verifier=lambda *args: verified.append(args))
    assert verified == []


@pytest.mark.parametrize('presented', ['publish-python-sdk.yml', 'unrelated.yml'])
def test_publisher_verification_enforces_producing_workflow(monkeypatch, presented):
    repository = 'honua-io/honua-sdk-python'
    artifact = {'sourceRevision': 'c' * 40, 'coordinate': 'honua-sdk'}
    artifact['signerWorkflow'] = nightly.signer_workflow({}, artifact, repository)
    calls = []
    def verify(command, **kwargs):
        calls.append(command)
        expected = command[command.index('--signer-workflow') + 1]
        if expected != f'{repository}/.github/workflows/{presented}':
            raise subprocess.CalledProcessError(1, command, stderr='certificate signer workflow mismatch')
        return subprocess.CompletedProcess(command, 0, stdout='[]')
    monkeypatch.setattr(nightly.subprocess, 'run', verify)
    if presented == 'unrelated.yml':
        with pytest.raises(subprocess.CalledProcessError, match='exit status 1'):
            nightly.verify_publisher_bundle(b'package', {}, artifact, repository, 'https://spdx.dev/Document')
    else:
        nightly.verify_publisher_bundle(b'package', {}, artifact, repository, 'https://spdx.dev/Document')
    assert calls[0][calls[0].index('--source-digest') + 1] == 'c' * 40
    assert calls[0][calls[0].index('--signer-workflow') + 1] == artifact['signerWorkflow']


def test_unlisted_publisher_requires_explicit_workflow():
    with pytest.raises(ValueError, match='not declared or allowlisted'):
        nightly.signer_workflow({}, {}, 'honua-io/unlisted')
    assert nightly.signer_workflow({'attestationWorkflow':
        'honua-io/trusted-builder/.github/workflows/publish.yml'}, {}, 'honua-io/unlisted') == (
        'honua-io/trusted-builder/.github/workflows/publish.yml')
    assert nightly.signer_workflow({}, {'coordinate': '@honua/mcp-server'}, 'honua-io/honua-sdk-js').endswith(
        '/publish-mcp-server.yml')


@pytest.mark.parametrize('missing', ['awsLambdaImage', 'awsLambdaDigest', 'both'])
def test_lambda_target_requires_image_even_when_field_is_absent(inputs, candidate, tmp_path, missing):
    report, paths = inputs
    data = yaml.safe_load(paths[0].read_text())
    data['components']['honua-server'] = {**data['components']['sdk'],
        'awsLambdaImage': 'ghcr.io/honua-io/honua-server@sha256:' + '6' * 64,
        'awsLambdaDigest': 'sha256:' + '6' * 64}
    for field in (['awsLambdaImage', 'awsLambdaDigest'] if missing == 'both' else [missing]):
        del data['components']['honua-server'][field]
    paths[0].write_text(yaml.safe_dump(data))
    paths[1].write_text('contracts: {}\ndeploy:\n  honua-server:\n    awsLambda:\n      target: aws-serverless\n')
    for path in paths:
        report['candidate']['artifacts'][path.name] = {'sha256': _sha256(path), 'size': path.stat().st_size}
    with pytest.raises(ValueError, match='declared deployment target requires an exact image and digest'):
        nightly.collect_post_gate(report, *paths, tmp_path / 'collected',
            attestations=publisher_bundles, artifact_bytes=lambda artifact: candidate[2], verifier=lambda *args: None)


def test_freeze_gates_mint_real_generator_clears_rows_36_to_38(candidate, tmp_path):
    from platform_lock_bundle import bind, bind_post_gate, canonical_bytes
    from generate_platform_lock import pending
    from validate_platform_lock import validate
    gate_records = records(GATE_USES)
    fixtures = fixture_revisions.declare(gate_records, run_id=RUN)
    report, paths = build_inputs(candidate, tmp_path, gate_fixtures=fixtures, missing_post_gate=True)
    original = yaml.safe_load(paths[0].read_text())
    assert not nightly.POST_GATE_FIELDS & original['platformLockEvidence'].keys()
    frozen = json.loads((tmp_path / 'qualification-lock.json').read_bytes())
    assert all(frozen[field] == pending(field) for field in nightly.POST_GATE_FIELDS | {'fixtures'})
    with pytest.raises(ValueError):
        bind(frozen, *paths, '2026.1-rc.3')
    references = nightly.collect_post_gate(report, *paths, tmp_path / 'collected',
        attestations=publisher_bundles, artifact_bytes=lambda artifact: candidate[2], verifier=lambda *args: None)
    output = tmp_path / 'minted'
    mint(report, paths, tmp_path / 'history', output, signer=signer, fixture_records=gate_records,
         post_gate_evidence=references, notes_bundle=tmp_path / 'collected/release-notes.bundle')
    shipped = output / paths[0].name
    final = nightly.generate(shipped, paths[1])
    assert final.unresolved == []
    assert not validate(final.lock).errors
    assert canonical_bytes(final.lock) == (output / 'platform-lock.json').read_bytes()
    assert {key for key in frozen if canonical_bytes(frozen[key]) != canonical_bytes(final.lock[key])} == (
        nightly.POST_GATE_FIELDS | {'fixtures', 'sourceInputs'})
    original['platformLockEvidence'].update(references['references'], fixtures=fixtures,
        evidenceDeclarations=report['evidenceDeclarations'])
    assert canonical_bytes(original) == canonical_bytes(yaml.safe_load(shipped.read_text()))
    assert final.lock['sourceInputs']['platformManifest']['sha256'] == 'sha256:' + _sha256(shipped)
    assert final.lock['sourceInputs']['compatibilityMatrix'] == frozen['sourceInputs']['compatibilityMatrix']
    assert (output / 'qualification-lock.json').read_bytes() == (tmp_path / 'qualification-lock.json').read_bytes()
    bind_post_gate(final.lock, shipped, paths[1], '2026.1-rc.3', output)


def test_boolean_integer_drift_in_qualification_is_refused_before_signing(inputs, tmp_path):
    from platform_lock_bundle import canonical_bytes
    report, paths = inputs
    lock_path = tmp_path / 'qualification-lock.json'
    lock = json.loads(lock_path.read_bytes())
    assert lock['disasterRecovery']['substrates']['postgresql'] is True
    lock['disasterRecovery']['substrates']['postgresql'] = 1
    lock_path.write_bytes(canonical_bytes(lock))
    refuses_before_signing(report, paths, tmp_path, 'qualification lock differs')


def test_boolean_integer_drift_outside_post_gate_fields_is_refused(inputs, tmp_path):
    report, paths = inputs
    frozen = json.loads((tmp_path / 'qualification-lock.json').read_bytes())
    frozen['disasterRecovery']['substrates']['postgresql'] = 1
    source = nightly.attach_post_gate(paths[0], fresh_references(report, paths)['references'],
                                      report['evidenceDeclarations'], tmp_path)
    with pytest.raises(ValueError, match='changed facts outside'):
        nightly.regenerate_post_gate(source, paths[1], frozen, fresh_references(report, paths)['references'])


def test_collection_token_and_unsigned_freeze_are_fail_closed():
    jobs = _workflow('release-train.yml')['jobs']
    freeze = next(s for s in jobs['freeze']['steps'] if s.get('name') == 'Generate the unsigned nightly qualification lock')
    assert freeze['run'].count('--qualification') == 2
    collection = next(s for s in jobs['report']['steps'] if '--collect-post-gate' in s.get('run', ''))
    assert collection['env']['GH_TOKEN'] == '${{ secrets.RELEASE_GH_TOKEN }}'
    assert '[ -z "${GH_TOKEN:-}" ]' in collection['run']
    assert 'exit 1' in collection['run']


@pytest.mark.parametrize('mutation', [
    lambda data: data['disasterRecovery']['substrates'].update(postgresql=1),
    lambda data: data['components']['sdk'].update(version='9.9.9'),
    lambda data: data['platformLockEvidence']['contentDigests'].clear(),
    lambda data: data['platformLockEvidence']['fixtures'][0].update(revision='b' * 40),
    lambda data: data['platformLockEvidence']['evidenceDeclarations']['cite'].update(receipt='hand.json'),
])
def test_promotion_rejects_any_manifest_change_outside_observed_declarations(inputs, tmp_path, mutation):
    from platform_lock_bundle import bind_post_gate
    report, paths = inputs
    output = tmp_path / 'minted'
    mint(report, paths, tmp_path / 'history', output, signer=signer)
    shipped = output / paths[0].name
    data = yaml.safe_load(shipped.read_text())
    mutation(data)
    shipped.write_text(yaml.safe_dump(data, sort_keys=True))
    with pytest.raises(ValueError, match='shipped manifest changed facts outside'):
        bind_post_gate(json.loads((output / 'platform-lock.json').read_bytes()), shipped,
                       paths[1], '2026.1-rc.3', output, image_inspector=None)


def test_buildkit_invocation_id_spelling_is_validated():
    kind = 'https://slsa.dev/provenance/v0.2'
    predicate = valid_predicate(kind)
    predicate['metadata']['buildInvocationID'] = predicate['metadata'].pop('buildInvocationId')
    assert nightly.predicate_valid(kind, predicate)
    predicate['metadata']['buildInvocationID'] = ''
    assert not nightly.predicate_valid(kind, predicate)
    predicate['metadata']['buildInvocationID'] = 'run-123'
    predicate['builder']['id'] = ''
    assert not nightly.predicate_valid(kind, predicate)


@pytest.mark.parametrize('mutation', [
    lambda lock: lock['sourceInputs']['platformManifest'].update(path='other.yaml'),
    lambda lock: lock['sourceInputs']['compatibilityMatrix'].update(sha256='sha256:' + '0' * 64),
    lambda lock: lock['sourceInputs'].update(undeclared={'sha256': 'sha256:' + '0' * 64}),
])
def test_regeneration_recomputes_only_the_manifest_hash(inputs, tmp_path, mutation):
    report, paths = inputs
    references = fresh_references(report, paths)['references']
    frozen = json.loads((tmp_path / 'qualification-lock.json').read_bytes())
    mutation(frozen)
    source = nightly.attach_post_gate(paths[0], references, report['evidenceDeclarations'], tmp_path)
    with pytest.raises(ValueError, match='changed facts outside'):
        nightly.regenerate_post_gate(source, paths[1], frozen, references)


def test_stamp_gives_bound_imaged_components_the_platform_version_and_never_an_sdk(tmp_path):
    """R22 (#231 WI-2): the label's platform version reaches the lock for bound images only."""
    from generate_platform_lock import generate
    revision, digest = 'a' * 40, 'sha256:' + 'b' * 64
    platforms = {'amd64': 'sha256:' + 'c' * 64, 'arm64': 'sha256:' + 'd' * 64}
    image = {'sha': revision, 'lifecycleStatus': 'GA', 'version': 'pre-release', 'digest': digest,
             'artifactSourceRevision': revision, 'architectures': ['amd64', 'arm64'], 'platformDigests': platforms}
    manifest = {'platformRelease': '2026.1-rc.2', 'status': 'rc', 'components': {
        'honua-server': {**image, 'repository': 'https://github.com/honua-io/honua-server',
                         'image': 'ghcr.io/honua-io/honua-server:nightly-aaaaaaa',
                         # Yesterday's stamp never survives into tonight's label.
                         'artifactVersion': '2026.1.0-rc.2', 'releaseVersion': '2026.1.0-rc.2'},
        'honua-console': {**image, 'repository': 'https://github.com/honua-io/honua-console',
                          'image': 'ghcr.io/honua-io/honua-console:candidate-aaaaaaaaaaaa-1-1'},
        # No chart digest is published yet (WI-6), so the chart keeps no platform version.
        'honua-helm': {'repository': 'https://github.com/honua-io/honua-helm', 'sha': revision,
                       'lifecycleStatus': 'Preview', 'version': 'pre-release', 'artifact': 'oci-chart:honua'},
        'honua-sdk-dotnet': {'repository': 'https://github.com/honua-io/honua-sdk-dotnet', 'sha': revision,
                             'lifecycleStatus': 'GA', 'artifact': 'nuget:Honua.Sdk', 'version': '1.6.2',
                             'artifactVersion': '1.6.2', 'artifactSourceRevision': revision,
                             'artifactSha256': digest},
    }}
    path, matrix = tmp_path / 'platform-manifest.yaml', tmp_path / 'compatibility-matrix.yaml'
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    matrix.write_text('contracts: {}\n')
    nightly.stamp_release_label(path, '2026.1-rc.3')
    stamped = yaml.safe_load(path.read_text())['components']
    assert stamped['honua-server']['artifactVersion'] == stamped['honua-server']['releaseVersion'] == '2026.1.0-rc.3'
    assert stamped['honua-console']['artifactVersion'] == '2026.1.0-rc.3'
    assert 'artifactVersion' not in stamped['honua-helm']
    assert stamped['honua-sdk-dotnet'] == manifest['components']['honua-sdk-dotnet']

    draft = generate(path, matrix)
    versions = {name: entry['artifacts'][0].get('version') for name, entry in draft.lock['components'].items()}
    assert versions == {'honua-server': '2026.1.0-rc.3', 'honua-console': '2026.1.0-rc.3',
                        'honua-helm': None, 'honua-sdk-dotnet': '1.6.2'}
    assert draft.lock['components']['honua-server']['releaseVersion'] == '2026.1.0-rc.3'
    assert [item for item in draft.unresolved if item.split(':')[0].endswith(('.version', '.releaseVersion'))] == [
        '[PUBLISH] $.components.honua-helm.artifacts[0].version: source snapshot/pre-release is not a '
        'released artifact version']
