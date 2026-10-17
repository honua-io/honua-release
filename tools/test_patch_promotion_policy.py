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


def patch_fixture(tmp_path, age=12):
    fixture = _fixture(tmp_path, age=age)
    record, lock, evidence, _ = fixture
    selected = json.loads(lock.read_text())
    selected.update(platform={'id': 'honua-2026.1.1-rc.1'}, fixtures=[{'repository': 'test', 'revision': 'a' * 40}], contentDigests={'catalog': 'a'})
    selected['components']['honua-helm'] = {'sha': 'b' * 40}
    baseline = copy.deepcopy(selected)
    baseline['platform']['id'] = 'honua-2026.1-rc.3'
    baseline['components']['honua-helm']['sha'] = 'a' * 40
    root = evidence / 'baselines' / '900'
    _write(root / 'platform-lock.json', baseline)
    (root / 'platform-manifest.yaml').write_bytes((evidence / 'trains/101/platform-manifest.yaml').read_bytes())
    baseline_digest = 'sha256:' + hashlib.sha256((root / 'platform-lock.json').read_bytes()).hexdigest()
    _write(root / 'run.json', _metadata('900', _stamp(NOW - timedelta(days=2))))
    old = record['lock']['digest']
    selected.update(profile='patch', changedComponents=['honua-helm'], baselineLockDigest=baseline_digest)
    _write(lock, selected)
    new = 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()
    for path in evidence.rglob('*.json'):
        if 'baselines' not in path.parts:
            path.write_text(path.read_text().replace(old, new))
    (evidence / 'trains/101/platform-lock.json').write_bytes(lock.read_bytes())
    record = json.loads(json.dumps(record).replace(old, new))
    record.update(platformLabel='2026.1.1-rc.1', profile='patch', baseline={'runId': '900', 'lockDigest': baseline_digest})
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


@pytest.mark.parametrize('mutation', ['profile', 'baseline-digest', 'changed-omitted', 'changed-extra', 'baseline-foreign'])
def test_patch_declaration_and_baseline_identity_fail_closed(tmp_path, mutation):
    f = patch_fixture(tmp_path)
    if mutation == 'profile': f[0]['profile'] = 'ga'
    if mutation == 'baseline-digest': f[0]['baseline']['lockDigest'] = 'sha256:' + 'f' * 64
    if mutation in ('changed-omitted', 'changed-extra'):
        _edit(f[1], lambda d: d.update(changedComponents=[] if mutation == 'changed-omitted' else ['honua-server', 'honua-helm']))
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
    f = patch_fixture(tmp_path)
    row = next(row for row in f[0]['evidence'] if row['class'] == 'cite')
    row.update(reusedComponents=['honua-server'], lockDigest=f[0]['baseline']['lockDigest'])
    baseline = json.loads((f[2] / 'baselines/900/platform-lock.json').read_text())
    _edit(_receipt(f, 'cite'), lambda d: d.update(lockDigest=row['lockDigest'], componentIdentities={
        'honua-server': baseline['components']['honua-server']}))
    _write(_receipt(f, 'cite').with_name('run.json'), _metadata(row['runId'], row['completedAt'], '.github/workflows/qualify.yml'))
    monkeypatch.setitem(fetcher.RECEIPT_PRODUCERS, 'cite', (('.github/workflows/qualify.yml',), ('workflow_dispatch',)))
    return f


def test_unchanged_certificate_reuse_keeps_authenticated_identity_and_freshness(tmp_path, monkeypatch):
    assert _decision(reused_fixture(tmp_path, monkeypatch))['status'] == 'pass'


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
    assert _decision(f)['status'] == 'pass'
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
    out = tmp_path / 'fetched'
    fetcher.fetch(f[0], gh, out)
    assert (out / 'baselines/900/platform-lock.json').read_bytes() == (root / 'platform-lock.json').read_bytes()
    assert readiness.evaluate(f[0], lock_path=out / 'trains/101/platform-lock.json', evidence_dir=out, now=NOW)[0]['status'] == 'pass'
    gh.runs['900']['head_branch'] = 'untrusted'
    with pytest.raises(fetcher.FetchError): fetcher.fetch(f[0], gh, tmp_path / 'untrusted')
