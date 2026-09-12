"""2026.1 licensing contract: reject enabled, absent, malformed and unauthenticated status."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'e2e'))
from licensing import assert_disabled, validate_disabled
import canonical_checks as cc


@pytest.mark.parametrize('document', [None, [], {}, {'mode': 'disabled'}, {'data': None},
    {'data': {}}, {'data': {'mode': 'enabled'}}, {'data': {'mode': False}},
    {'data': {'mode': 'Disabled'}}, {'data': {'mode': 'unknown'}}])
def test_mode_requires_the_runtime_contract_value(document):
    with pytest.raises(ValueError, match='requires admin license mode: disabled'):
        validate_disabled(document)


def test_accepts_contract_not_an_edition_grant():
    validate_disabled({'data': {'mode': 'disabled', 'edition': 'Unlicensed-2026.1'}})
    with pytest.raises(ValueError):
        validate_disabled({'data': {'edition': 'Enterprise', 'isValid': True}})


@pytest.mark.parametrize('status,body', [
    (200, '{"data":{"mode":"disabled"}}'),
    (200, '{"data":{"mode":"enabled"}}'),
    (200, '{"data":{"edition":"Enterprise"}}'),
    (401, '{"data":{"mode":"disabled"}}'),
    (200, 'not json'), (0, '')])
def test_cloud_checks_authenticated_status_without_bootstrap_exemption(status, body):
    urls = []
    def authenticated(url):
        urls.append(url)
        return cc.HttpResponse(status, body)
    results = cc.run_canonical('http://candidate', lambda _: cc.HttpResponse(404, '{}'),
                               authenticated_fetch=authenticated, enforcement='bootstrap')
    result = next(r for r in results if r.name == 'licensing-disabled')
    assert result.status == ('pass' if status == 200 and body == '{"data":{"mode":"disabled"}}' else 'fail')
    assert 'http://candidate/api/v1/admin/license' in urls


def test_http_probe_authenticates_and_records_only_the_asserted_fact():
    import io
    response = io.BytesIO(b'{"data":{"mode":"disabled","unrelated":"not in receipt"}}')
    response.status = 200
    with patch('urllib.request.urlopen', return_value=response) as fetch:
        receipt = assert_disabled('http://candidate/', 'private-key')
    request = fetch.call_args.args[0]
    assert request.full_url == 'http://candidate/api/v1/admin/license'
    assert request.get_header('X-api-key') == 'private-key'
    assert receipt == {'mode': 'disabled', 'status': 'pass', 'surface': '/api/v1/admin/license'}


@pytest.mark.parametrize('compose', ['local-docker/docker-compose.yml', 'harness/compose.candidate.yml',
                                   'dr-drill/compose.full-platform.yml'])
def test_every_release_compose_sets_supported_mode_without_a_development_grant(compose):
    env = yaml.safe_load((ROOT / 'e2e' / compose).read_text())['services']['server']['environment']
    assert env['Licensing__Mode'] == 'Disabled'
    assert not any(k.startswith('Licensing__') and k != 'Licensing__Mode' for k in env)


def test_train_and_certification_have_no_honua_license_inputs():
    for path in (ROOT / '.github/workflows').glob('*.yml'):
        workflow = yaml.safe_load(path.read_text())
        triggers = workflow.get('on', workflow.get(True, {}))
        for kind in ('workflow_dispatch', 'workflow_call'):
            config = triggers.get(kind) or {}
            for field in ('inputs', 'secrets'):
                assert not any('license' in key.lower() for key in config.get(field, {})), path


@pytest.mark.parametrize('release', ['2026.2-rc.1', '2026.10.1'])
def test_later_releases_do_not_inherit_the_2026_1_runtime_rule(release):
    urls = []
    def fetch(url):
        urls.append(url)
        return cc.HttpResponse(200, '{}')
    results = cc.run_canonical('http://candidate', fetch, platform_release=release)
    assert 'licensing-disabled' not in [r.name for r in results]
    assert not any(url.endswith('/admin/license') for url in urls)


def test_cloud_runner_uses_the_credential_installed_by_its_target(monkeypatch):
    import run_cloud
    from targets.base import Availability
    class Target:
        admin_api_key = 'deployed-target-key'
        def availability(self):
            return Availability(True, 'fixture')
        def provision(self, **_):
            return 'http://deployed-target'
        def teardown(self, **_):
            pass
    target = Target()
    monkeypatch.setattr(run_cloud, 'REGISTRY', {'fixture': lambda **_: target})
    monkeypatch.setattr(run_cloud, '_READY_ATTEMPTS', 1)
    monkeypatch.setattr(run_cloud, '_READY_DELAY_SECONDS', 0)
    def make_fetch(headers=None, **_):
        def fetch(url):
            if url.endswith('/admin/license'):
                authorized = headers == {'X-API-Key': 'deployed-target-key'}
                return cc.HttpResponse(200 if authorized else 401, '{"data":{"mode":"disabled"}}')
            return cc.HttpResponse(200, '{}')
        return fetch
    monkeypatch.setattr(run_cloud, 'make_fetch', make_fetch)
    report = run_cloud.run('fixture', require_real=False, reference_endpoint=None)
    licensing = next(check for check in report['checks'] if check['name'] == 'licensing-disabled')
    assert licensing['status'] == 'pass'
    assert 'deployed-target-key' not in json.dumps(report)


def test_boot_forwards_the_compose_api_key_not_an_unrelated_host_key(tmp_path):
    import os
    import shutil
    import subprocess
    harness = tmp_path / 'e2e/harness'
    harness.mkdir(parents=True)
    shutil.copyfile(ROOT / 'e2e/harness/boot.sh', harness / 'boot.sh')
    (tmp_path / 'e2e/licensing.py').write_text(
        'import os\nassert os.environ["HONUA_ADMIN_PASSWORD"] == "custom-harness-key"\n')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for name, script in {'curl': '#!/bin/sh\nprintf 200', 'docker': '#!/bin/sh\nexit 0'}.items():
        executable = bindir / name
        executable.write_text(script)
        executable.chmod(0o755)
    result = subprocess.run(['bash', str(harness / 'boot.sh'), 'wait'], capture_output=True,
                            text=True, env={**os.environ, 'PATH': f'{bindir}:{os.environ["PATH"]}',
                                           'E2E_API_KEY': 'custom-harness-key',
                                           'HONUA_ADMIN_PASSWORD': 'unrelated-host-key',
                                           'E2E_OUT': str(tmp_path / 'out')})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / 'out/boot.json').read_text())['booted'] is True
