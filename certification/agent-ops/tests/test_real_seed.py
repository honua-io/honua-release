import pytest
from real_seed import prepare_town


def test_rejects_held_out_and_existing_setup_before_network(tmp_path):
    kwargs = dict(namespace='ops_test', base_url='http://127.0.0.1:1', credential_env='MISSING_KEY',
                  candidate_digest='sha256:' + 'a' * 64, connection_id='bad', work=tmp_path / 'town',
                  project='ops-' + 'a' * 32, compose_file=tmp_path / 'compose',
                  image_ref='server@sha256:' + 'a' * 64, authorize=lambda: None)
    with pytest.raises(ValueError, match='A/B'):
        prepare_town(variant='C', **kwargs)
    kwargs['work'].mkdir()
    with pytest.raises(ValueError, match='never replay'):
        prepare_town(variant='A', **kwargs)


def test_runtime_binding_rejects_other_container_or_port(tmp_path, monkeypatch):
    import subprocess
    from real_seed import _verify_runtime
    compose = tmp_path / 'compose.yml'
    compose.write_text('services: {}')
    image_id = 'sha256:' + 'b' * 64
    container = 'c' * 64
    def run(argv, **kwargs):
        if 'ps' in argv: output = container
        elif 'port' in argv: output = '0.0.0.0:8123'
        else: output = image_id
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(subprocess, 'run', run)
    digest = 'sha256:' + 'a' * 64
    result = _verify_runtime('ops-' + 'a' * 32, compose, 'server@' + digest, digest, 'http://127.0.0.1:8123')
    assert result['container'] == container
    with pytest.raises(ValueError, match='endpoint mismatch'):
        _verify_runtime('ops-' + 'a' * 32, compose, 'server@' + digest, digest, 'http://127.0.0.1:8124')
    with pytest.raises(ValueError, match='isolated'):
        _verify_runtime('production', compose, 'server@' + digest, digest, 'http://127.0.0.1:8123')
