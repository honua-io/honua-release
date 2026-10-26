import copy
import hashlib
import json
from datetime import timedelta

import pytest

import check_promotion_readiness as readiness
import fetch_promotion_evidence as fetcher
from test_check_promotion_readiness import NOW, _fixture, _decision, _edit, _stamp, _write, _receipt


def _metadata(run, completed, workflow='.github/workflows/nightly-certification.yml'):
    return {'id': int(run), 'repository': {'full_name': 'honua-io/honua-release'},
            'head_repository': {'full_name': 'honua-io/honua-release'}, 'head_branch': 'trunk',
            'path': workflow, 'event': 'workflow_dispatch', 'status': 'completed',
            'conclusion': 'success', 'updated_at': completed}


def patch_fixture(tmp_path, age=12, *, server_changed=True):
    fixture = _fixture(tmp_path, age=age)
    record, lock, evidence, _ = fixture
    selected = json.loads(lock.read_text())
    selected.update(platform={'id': 'honua-2026.1.1-rc.1'}, fixtures=[{'repository': 'test', 'revision': 'a' * 40}], contentDigests={'catalog': 'a'})
    selected['components']['honua-helm'] = {'sha': 'b' * 40}
    baseline = copy.deepcopy(selected)
    baseline['platform']['id'] = 'honua-2026.1-rc.3'
    baseline['components']['honua-helm']['sha'] = 'a' * 40
    root = evidence / 'baselines' / '900'
    import yaml
    baseline_manifest = yaml.safe_load((evidence / 'trains/101/platform-manifest.yaml').read_bytes())
    if server_changed:
        baseline['components']['honua-server']['artifacts'][0]['digest'] = 'sha256:' + 'a' * 64
        baseline_manifest['components']['honua-server'].update(digest='sha256:' + 'a' * 64, awsLambdaEcrDigest='sha256:' + 'e' * 64)
    payload = yaml.safe_dump(baseline_manifest).encode()
    (root / 'platform-manifest.yaml').parent.mkdir(parents=True, exist_ok=True)
    (root / 'platform-manifest.yaml').write_bytes(payload)
    baseline['sourceInputs']['platformManifest']['sha256'] = 'sha256:' + hashlib.sha256(payload).hexdigest()
    _write(root / 'platform-lock.json', baseline)
    (root / 'published-platform-lock.json').write_bytes((root / 'platform-lock.json').read_bytes())
    _write(root / 'publication.json', {'id': 91, 'tag_name': 'honua-2026.1.0', 'draft': False, 'prerelease': False,
        'html_url': 'https://github.com/honua-io/honua-release/releases/tag/honua-2026.1.0',
        'published_at': _stamp(NOW - timedelta(days=1))})
    baseline_digest = 'sha256:' + hashlib.sha256((root / 'platform-lock.json').read_bytes()).hexdigest()
    _write(root / 'run.json', _metadata('900', _stamp(NOW - timedelta(days=2))))
    old = record['lock']['digest']
    selected.update(profile='patch', changedComponents=['honua-server', 'honua-helm'] if server_changed else ['honua-helm'], baselineLockDigest=baseline_digest)
    _write(lock, selected)
    new = 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()
    for path in evidence.rglob('*.json'):
        if 'baselines' not in path.parts:
            path.write_text(path.read_text().replace(old, new))
    (evidence / 'trains/101/platform-lock.json').write_bytes(lock.read_bytes())
    record = json.loads(json.dumps(record).replace(old, new))
    record.update(platformLabel='2026.1.1-rc.1', profile='patch', baseline={'runId': '900', 'lockDigest': baseline_digest, 'releaseTag': 'honua-2026.1.0'})
    rollback = evidence / 'evidence/update-rollback' / next(row['runId'] for row in record['evidence'] if row['class'] == 'update-rollback') / 'receipt.json'
    value = json.loads(rollback.read_text())
    value['fromLockDigest'] = baseline_digest
    for cell in value['cells']:
        source = baseline_manifest['components']['honua-server']['awsLambdaEcrDigest' if cell['cell'].startswith('aws-serverless/') else 'digest']
        cell['fromDigest'] = cell['schemaBoundary']['imageDigest'] = source
        cell['prior'].update(lockDigest=baseline_digest,
            manifestDigest='sha256:' + hashlib.sha256(payload).hexdigest(),
            imageDigest=baseline_manifest['components']['honua-server']['digest'],
            lambdaImageDigest=baseline_manifest['components']['honua-server']['awsLambdaEcrDigest'])
        for observation in cell['observations']:
            if observation['phase'] in ('baseline', 'rollback'):
                observation['native']['imageDigest'] = source
        if cell['actuator'] == 'terraform-exact-apply':
            cell['rollbackOperation']['imageDigest'] = source
    _write(rollback, value)
    # This fixture changes the candidate after the initial RC fixture was built.
    # Its synthetic campaign must bind that final patch cohort, including Helm.
    # Production receipts are never rebound by the promotion checker.
    from test_mint_nightly_lock import agent_summary
    agent_receipt = evidence / 'evidence/agent-e2e' / next(
        row['runId'] for row in record['evidence'] if row['class'] == 'agent-e2e') / 'receipt.json'
    agent_value = json.loads(agent_receipt.read_text())
    agent_value['agentPack'] = agent_summary(lock, agent_value['completedAt'])
    _write(agent_receipt, agent_value)
    return record, lock, evidence, fixture[3]


def test_patch_requires_twelve_hours_two_canaries_and_same_bytes(tmp_path):
    f = patch_fixture(tmp_path)
    d = _decision(f)
    assert d['status'] == 'pass'
    assert d['profile'] == 'patch'
    assert d['lockDigest'] == f[0]['lock']['digest']
    assert len(f[0]['demoCanaries']) == 2


def test_patch_short_burn_and_missing_canary_refuse(tmp_path):
    f = patch_fixture(tmp_path, age=11)
    assert _decision(f)['checks']['burn-duration']['status'] == 'fail'
    f[0]['demoCanaries'].pop()
    assert _decision(f)['checks']['demo-canaries']['status'] == 'fail'


def test_patch_rejects_agent_campaign_for_a_different_component_set(tmp_path):
    f = patch_fixture(tmp_path)
    _edit(_receipt(f, 'agent-e2e'), lambda d: d['agentPack'].update(componentBindingDigest='sha256:' + 'f' * 64))
    assert _decision(f)['checks']['evidence:agent-e2e']['status'] == 'fail'


@pytest.mark.parametrize('mutation', ['profile', 'baseline-digest', 'changed-omitted', 'changed-extra', 'baseline-foreign'])
def test_patch_declaration_and_baseline_identity_fail_closed(tmp_path, mutation):
    f = patch_fixture(tmp_path)
    if mutation == 'profile': f[0]['profile'] = 'ga'
    if mutation == 'baseline-digest': f[0]['baseline']['lockDigest'] = 'sha256:' + 'f' * 64
    if mutation in ('changed-omitted', 'changed-extra'):
        _edit(f[1], lambda d: d.update(changedComponents=[] if mutation == 'changed-omitted' else ['honua-server', 'honua-helm', 'honua-sdk-js']))
    if mutation == 'baseline-foreign':
        _edit(f[2] / 'baselines/900/run.json', lambda d: d.update(head_branch='untrusted'))
    with pytest.raises(readiness.ReadinessError): _decision(f)


def test_patch_still_requires_current_rollback_and_every_class(tmp_path):
    f = patch_fixture(tmp_path)
    _receipt(f, 'update-rollback').unlink()
    assert _decision(f)['checks']['evidence:update-rollback']['status'] == 'fail'
    _receipt(f, 'security').unlink()
    assert _decision(f)['checks']['evidence:security']['status'] == 'fail'


def reused_fixture(tmp_path, monkeypatch):
    f = patch_fixture(tmp_path, server_changed=False)
    row = next(row for row in f[0]['evidence'] if row['class'] == 'cite')
    row.update(reusedComponents=['honua-server'], lockDigest=f[0]['baseline']['lockDigest'])
    baseline = json.loads((f[2] / 'baselines/900/platform-lock.json').read_text())
    _edit(_receipt(f, 'cite'), lambda d: d.update(lockDigest=row['lockDigest'], componentIdentities={
        'honua-server': baseline['components']['honua-server']}))
    _write(_receipt(f, 'cite').with_name('run.json'), _metadata(row['runId'], row['completedAt'], '.github/workflows/qualify.yml'))
    monkeypatch.setitem(fetcher.RECEIPT_PRODUCERS, 'cite', (('.github/workflows/qualify.yml',), ('workflow_dispatch',)))
    return f


def test_unchanged_certificate_reuse_keeps_authenticated_identity_and_freshness(tmp_path, monkeypatch):
    decision = _decision(reused_fixture(tmp_path, monkeypatch))
    assert decision['checks']['evidence:cite']['status'] == 'pass'
    assert decision['checks']['evidence:update-rollback']['status'] == 'fail'
    assert decision['status'] == 'refused'


@pytest.mark.parametrize('mutation', ['stale', 'wrong-component', 'wrong-lock', 'foreign-producer', 'changed-component', 'shared-fixture', 'manifest-artifact', 'missing-manifest'])
def test_reused_certificate_refuses_missing_or_changed_identity(tmp_path, monkeypatch, mutation):
    f = reused_fixture(tmp_path, monkeypatch)
    path = _receipt(f, 'cite')
    if mutation == 'stale': _edit(path, lambda d: d.update(freshUntil='2026-10-01T00:00:00Z'))
    if mutation == 'wrong-component': _edit(path, lambda d: d.update(componentIdentities={'honua-server': {'artifacts': []}}))
    if mutation == 'wrong-lock': _edit(path, lambda d: d.update(lockDigest='sha256:' + 'f' * 64))
    if mutation == 'foreign-producer': _edit(path.with_name('run.json'), lambda d: d.update(head_branch='feature'))
    if mutation == 'changed-component':
        _edit(f[1], lambda d: (d['components']['honua-server'].update(sha='f' * 40), d.update(changedComponents=['honua-server', 'honua-helm'])))
    if mutation == 'shared-fixture': _edit(f[1], lambda d: d.update(fixtures=[{'revision': 'changed'}]))
    if mutation == 'manifest-artifact':
        path = f[2] / 'trains/101/platform-manifest.yaml'
        path.write_text(path.read_text().replace('b' * 64, 'f' * 64))
    if mutation == 'missing-manifest': (f[2] / 'baselines/900/platform-manifest.yaml').unlink()
    assert _decision(f)['checks']['evidence:cite']['status'] == 'fail'


def test_patch_profile_cannot_shorten_initial_rc_burn(tmp_path):
    f = patch_fixture(tmp_path)
    f[0]['platformLabel'] = '2026.1-rc.3'
    with pytest.raises(readiness.ReadinessError): _decision(f)


def test_reuse_may_precede_burn_but_never_refresh_its_original_expiry(tmp_path, monkeypatch):
    f = reused_fixture(tmp_path, monkeypatch)
    row = next(r for r in f[0]['evidence'] if r['class'] == 'cite')
    row['completedAt'] = _stamp(NOW - timedelta(hours=24))
    _edit(_receipt(f, 'cite'), lambda d: d.update(completedAt=row['completedAt'], freshUntil=_stamp(NOW + timedelta(hours=1))))
    _edit(_receipt(f, 'cite').with_name('run.json'), lambda d: d.update(updated_at=row['completedAt']))
    assert _decision(f)['checks']['evidence:cite']['status'] == 'pass'
    assert _decision(f)['status'] == 'refused'  # unchanged-runtime rollback is not yet supported
    assert _decision(f, now=NOW + timedelta(hours=2))['checks']['evidence:cite']['status'] == 'fail'


def test_patch_schema_and_checker_agree_on_canary_count(tmp_path):
    from jsonschema import Draft202012Validator
    from pathlib import Path
    schema = json.loads((Path(__file__).resolve().parents[1] / 'certification/promotion-evidence.v1.schema.json').read_text())
    f = patch_fixture(tmp_path)
    assert not list(Draft202012Validator(schema).iter_errors(f[0]))
    f[0]['demoCanaries'].pop()
    assert list(Draft202012Validator(schema).iter_errors(f[0]))


def test_fetcher_authenticates_and_retains_patch_baseline(tmp_path, monkeypatch):
    from test_fetch_promotion_evidence import FakeGitHub, QUALIFY, QUALIFYING_CLASSES
    f = patch_fixture(tmp_path / 'source')
    gh = FakeGitHub(f)
    for name in QUALIFYING_CLASSES:
        monkeypatch.setitem(fetcher.RECEIPT_PRODUCERS, name, ((QUALIFY,), ('workflow_dispatch',)))
    root = f[2] / 'baselines/900'
    metadata = json.loads((root / 'run.json').read_text())
    gh.add_run('900', metadata['updated_at'], path=metadata['path'], event=metadata['event'])
    gh.artifacts[('900', 'certified-candidate')] = [root / 'platform-lock.json', root / 'platform-manifest.yaml']
    original_api = gh.api
    gh.api = lambda path='': json.loads((root / 'publication.json').read_text()) if path.startswith('releases/tags/') else original_api(path)
    def download_release(tag, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / 'platform-lock.json').write_bytes((root / 'platform-lock.json').read_bytes())
        return True
    gh.download_release_lock = download_release
    out = tmp_path / 'fetched'
    fetcher.fetch(f[0], gh, out)
    assert (out / 'baselines/900/platform-lock.json').read_bytes() == (root / 'platform-lock.json').read_bytes()
    assert readiness.evaluate(f[0], lock_path=out / 'trains/101/platform-lock.json', evidence_dir=out, now=NOW)[0]['status'] == 'pass'
    gh.runs['900']['head_branch'] = 'untrusted'
    with pytest.raises(fetcher.FetchError): fetcher.fetch(f[0], gh, tmp_path / 'untrusted')


@pytest.mark.parametrize('mutation', ['missing-publication', 'draft', 'prerelease', 'different-series', 'same-patch', 'unpublished-bytes', 'wrong-family-lock', 'wrong-rollback'])
def test_patch_needs_actual_same_series_published_baseline(tmp_path, mutation):
    f = patch_fixture(tmp_path)
    root = f[2] / 'baselines/900'
    if mutation == 'missing-publication': (root / 'publication.json').unlink()
    if mutation in ('draft', 'prerelease'): _edit(root / 'publication.json', lambda d: d.update({mutation: True}))
    if mutation == 'different-series': f[0]['baseline']['releaseTag'] = 'honua-2026.2.0'
    if mutation == 'same-patch': f[0]['baseline']['releaseTag'] = 'honua-2026.1.1'
    if mutation == 'unpublished-bytes': (root / 'published-platform-lock.json').write_text('{}')
    if mutation == 'wrong-family-lock': _edit(root / 'platform-lock.json', lambda d: d.update(platform={'id': 'honua-2026.2-rc.3'}))
    if mutation == 'wrong-rollback':
        _edit(_receipt(f, 'update-rollback'), lambda d: d.update(fromLockDigest='sha256:' + 'f' * 64))
        assert _decision(f)['checks']['evidence:update-rollback']['status'] == 'fail'
    else:
        with pytest.raises((readiness.ReadinessError, OSError)): _decision(f)


@pytest.mark.parametrize('cell', ['aws-ecs/redis-off', 'aws-ecs/redis-on'])
def test_patch_rollback_must_use_each_baseline_image_pin(tmp_path, cell):
    f = patch_fixture(tmp_path)
    def forge(d):
        row = next(r for r in d['cells'] if r['cell'] == cell)
        # Internally consistent claimed source/schema image still must match the authenticated baseline.
        forged = 'sha256:' + 'f' * 64
        row['fromDigest'] = row['schemaBoundary']['imageDigest'] = forged
        row['prior']['lambdaImageDigest' if cell.startswith('aws-serverless/') else 'imageDigest'] = forged
        for observation in row['observations']:
            if observation['phase'] in ('baseline', 'rollback'):
                observation['native']['imageDigest'] = forged
        if row['actuator'] == 'terraform-exact-apply':
            row['rollbackOperation']['imageDigest'] = forged
        # The stronger trunk observation contract must still pass without baseline binding.
        # Otherwise this negative would no longer prove the patch-specific requirement.
        assert readiness._update_rollback(d, {r['cell']: r['toDigest'] for r in d['cells']})
    _edit(_receipt(f, 'update-rollback'), forge)
    assert _decision(f)['checks']['evidence:update-rollback']['status'] == 'fail'
