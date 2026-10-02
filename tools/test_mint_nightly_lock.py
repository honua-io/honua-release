import copy
import json
from datetime import datetime, timezone

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
        'candidate': {'artifacts': {p.name: {'sha256': _sha256(p), 'size': p.stat().st_size}
                                    for p in real_paths}}}
    return report, real_paths


def signer(lock, signature, *_):
    # Unit seam: OIDC signing is exercised by the production cosign commands,
    # while these tests prove which exact canonical bytes reach that signer.
    assert json.loads(lock.read_bytes())['platform']['id'] == 'honua-2026.1-rc.3'
    signature.write_text('{"verified": true}')


def test_all_green_generates_binds_and_signs_lock(inputs, tmp_path):
    report, paths = inputs
    output = tmp_path / 'minted'
    assert nightly.mint(report, *paths, tmp_path / 'history', output, 'trusted', signer=signer) == '2026.1-rc.3'
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
        nightly.mint(report, *paths, tmp_path / 'history', tmp_path / 'minted', 'trusted',
                     signer=lambda *args: calls.append(args))
    assert not calls
    assert not (tmp_path / 'minted').exists()


def test_missing_journey_is_red(inputs, tmp_path):
    report, paths = inputs
    report['gates'] = [r for r in report['gates'] if r['gate'] != 'journey']
    with pytest.raises(ValueError, match='journey: missing'):
        nightly.mint(report, *paths, tmp_path / 'history', tmp_path / 'minted', 'trusted', signer=signer)
    assert not (tmp_path / 'minted').exists()


@pytest.mark.parametrize('field,value', [('dry_run', True), ('overallStatus', 'blocked')])
def test_only_strict_green_can_mint(inputs, tmp_path, field, value):
    report, paths = inputs
    report[field] = value
    with pytest.raises(ValueError):
        nightly.mint(report, *paths, tmp_path / 'history', tmp_path / 'minted', 'trusted', signer=signer)
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
        nightly.mint(report, *paths, tmp_path / 'history', tmp_path / 'minted', 'trusted', signer=signer)
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
        nightly.mint(report, *paths, tmp_path / 'history', tmp_path / 'minted', 'trusted', signer=broken)
    assert not (tmp_path / 'minted').exists()
    assert not list(tmp_path.glob('.nightly-*'))
