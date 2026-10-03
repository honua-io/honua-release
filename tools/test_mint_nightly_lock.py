import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import mint_nightly_lock as nightly
from candidate_binding import _sha256
from test_platform_lock_bundle import candidate


@pytest.fixture
def inputs(candidate, tmp_path):
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
    real_paths[0].write_text(yaml.safe_dump(manifest))
    real_paths[1].write_bytes(paths[1].read_bytes())
    report = {'dry_run': False, 'overallStatus': 'pass', 'platform_label': '2026.1-rc.3',
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'gates': [{'gate': name, 'status': 'pass'} for name in sorted(nightly.REQUIRED_NIGHTLY_GATES)],
        'candidate': {
            'source': {'repository': 'honua-io/honua-release', 'sha': SOURCE, 'branch': 'trunk'},
            'train': {'workflowPath': '.github/workflows/nightly-certification.yml', 'runId': RUN,
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
    draft = nightly.generate(*real_paths)
    nightly.bind(draft.lock, *real_paths, '2026.1-rc.3')
    qualification_lock = tmp_path / 'qualification-lock.json'
    qualification_lock.write_bytes(nightly.bundle_files(draft.lock)['platform-lock.json'])
    report = nightly.declare_evidence(report, qualification_lock, journeys)
    return report, real_paths


SOURCE = 'f' * 40
RUN = '4242'
PROTECTED = [{'id': 7, 'target': 'tag', 'enforcement': 'active',
              'conditions': {'ref_name': {'include': ['refs/tags/nightly-lock/**'], 'exclude': []}},
              'rules': [{'type': 'deletion'}, {'type': 'update'}, {'type': 'non_fast_forward'}]}]


def mint(report, paths, history, output, *, signer, **overrides):
    """The production call shape: trusted identity, complete published history, protected lock refs."""
    options = {'rulesets': PROTECTED, 'published': {}, 'source_sha': SOURCE, 'run_id': RUN}
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
    assert lock['sourceInputs']['platformManifest']['sha256'] == 'sha256:' + _sha256(paths[0])
    assert (output / 'platform-lock.sigstore.json').exists()
    assert (output / 'bom.cdx.json').exists()
    assert nightly.CHANNEL_TAG.search((output / 'platform-lock.json').read_text()) is None


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
                    'lambda-certification', 'protocol-ledger', 'deterministic-journey', 'nightly-model-journey')
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
    assert len(list((output / 'promotion-receipts').glob('*/receipt.json'))) == 11


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
