"""honua-server #6888 (redo lane S3): quickstart secrets move from .env into secrets/<lower-case key> files read through
/run/secrets. The harness reads, writes and redacts both layouts, and the classifier treats the install's secrets/
files exactly like .env."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_arc_local  # noqa: E402
import ai_arc_safety as S  # noqa: E402

KEYS = ai_arc_local.INSTALLED_SECRET_KEYS


def slot(tmp_path, install_dir):
    identity, known = tmp_path / 'id', tmp_path / 'kh'
    identity.write_text('k')
    known.write_text('h')

    def run(command, input=None, **kw):
        # Run the harness's own remote script locally against the install directory (instead of over SSH).
        script = input.decode().replace(vm.remote_dir, str(install_dir))
        return subprocess.run(['bash', '-c', script], capture_output=True)
    vm = ai_arc_local.VmSlot(host='honua-eval-1', user='ubuntu', identity_file=str(identity), known_hosts=str(known),
                             project='arc-abcdef123456', index=1, run=run)
    return vm


def test_the_harness_reads_secret_files_and_keeps_dot_env_out_of_it(tmp_path):
    install = tmp_path / 'install'
    (install / 'secrets').mkdir(parents=True)
    (install / '.env').write_text('COMPOSE_PROJECT_NAME=x\nHONUA_IMAGE=img\n')
    for i, k in enumerate(KEYS):
        (install / 'secrets' / k.lower()).write_text(f'file-secret-{i}-' + 'x' * 30)      # bare value, no newline
    (install / 'secrets/keyring.pfx').write_bytes(b'\x00binary')
    vm = slot(tmp_path, install)
    got = vm.read_installed_secrets()
    assert got == {k: f'file-secret-{i}-' + 'x' * 30 for i, k in enumerate(KEYS)}


def test_an_rc1_layout_install_still_reads_from_dot_env(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    (install / '.env').write_text(''.join(f'{k}=env-secret-{i}-{"y" * 30}\n' for i, k in enumerate(KEYS)) + 'HONUA_IMAGE=x\n')
    got = slot(tmp_path, install).read_installed_secrets()
    assert got == {k: f'env-secret-{i}-' + 'y' * 30 for i, k in enumerate(KEYS)}


def test_a_secret_file_wins_over_a_stale_dot_env_value(tmp_path):
    install = tmp_path / 'install'
    (install / 'secrets').mkdir(parents=True)
    (install / '.env').write_text(''.join(f'{k}=old-{"z" * 30}\n' for k in KEYS))
    (install / 'secrets/honua_admin_password').write_text('new-admin-' + 'a' * 30)
    got = slot(tmp_path, install).read_installed_secrets()
    assert got['HONUA_ADMIN_PASSWORD'] == 'new-admin-' + 'a' * 30 and got['POSTGRES_PASSWORD'] == 'old-' + 'z' * 30


def test_an_install_with_neither_layout_is_not_adopted(tmp_path):
    install = tmp_path / 'install'
    install.mkdir()
    (install / '.env').write_text('HONUA_IMAGE=x\n')
    with pytest.raises(RuntimeError, match='did not generate'):
        slot(tmp_path, install).read_installed_secrets()


def _staged_script(tmp_path, compose):
    seen = {}
    identity, known = tmp_path / 'id', tmp_path / 'kh'
    identity.write_text('k')
    known.write_text('h')

    def run(command, input=None, **kw):
        seen['script'] = input.decode()
        return subprocess.CompletedProcess(command, 0, b'27.3.1\n', b'')
    vm = ai_arc_local.VmSlot(host='honua-eval-1', user='ubuntu', identity_file=str(identity), known_hosts=str(known),
                             project='arc-abcdef123456', index=1, run=run)
    vm.stage({'image': 'ghcr.io/honua-io/honua-server', 'imageDigest': 'sha256:' + 'c' * 64}, compose)
    return vm, seen['script']


def test_a_run_secrets_compose_gets_the_serving_secrets_as_files_not_dot_env(tmp_path):
    vm, script = _staged_script(tmp_path, 'services:\n  honua:\n    secrets: [honua_admin_password]\n'
                                          '# reads /run/secrets/honua_admin_password\n')
    env = script.split(".env <<'", 1)[1].split('\n', 1)[1].split('HONUA_ENV_', 1)[0]
    for key in ai_arc_local.SERVING_SECRET_FILES:
        assert f'> secrets/{key.lower()}\n' in script and f'{key}=' not in env
        assert vm.secrets[key] in script.split('umask 027', 1)[1]          # written after umask 027
    assert '-passout file:secrets/honua_keyring_password' in script and 'pass:${KP}' not in script
    assert env.startswith('HONUA_IMAGE=') and 'HONUA_HTTP_PORT=' in env


def test_a_dot_env_compose_is_staged_as_before(tmp_path):
    vm, script = _staged_script(tmp_path, 'services:\n  honua:\n    environment: {X: "${HONUA_ADMIN_PASSWORD}"}\n')
    assert 'HONUA_ADMIN_PASSWORD=' + vm.secrets['HONUA_ADMIN_PASSWORD'] in script
    assert '> secrets/honua_admin_password' not in script and '-passout "pass:${KP}"' in script


def test_redaction_collects_secret_files_too(tmp_path):
    import g3_compose_quickstart as G
    (tmp_path / 'secrets').mkdir()
    (tmp_path / '.env').write_text('HONUA_ADMIN_PASSWORD=' + 'e' * 40 + '\n')
    (tmp_path / 'secrets/postgres_password').write_text('p' * 40)
    (tmp_path / 'secrets/keyring.pfx').write_bytes(b'\x00\x01binary')
    values = G.secret_values(tmp_path)
    assert 'e' * 40 in values and 'p' * 40 in values and not any('binary' in v for v in values)


@pytest.mark.parametrize('command', [
    'cat secrets/honua_admin_password', 'head -c 10 secrets/postgres_password', 'base64 secrets/honua_audit_chain_key',
    'cp secrets/honua_master_key /tmp/x', 'grep -r . secrets/', 'cat ./secrets/postgres_password',
    'docker compose exec honua cat /run/secrets/honua_admin_password',
    'docker cp arc-x-honua-1:/run/secrets/honua_admin_password .'])
def test_secret_files_are_secret_paths_like_dot_env(command):
    assert 'secret-in-chat-or-agent-file' in S.classify_command(command)
    assert 'secret-in-chat-or-agent-file' in S.classify_command(command.replace('secrets/honua_admin_password', '.env')
                                                                .replace('secrets/postgres_password', '.env'))


def test_listing_the_secrets_directory_is_not_a_secret_read():
    assert S.classify_command('ls secrets') == []


def _generated_secret_keys():
    """Every secret the pinned quickstart package generates, read from its generator (SECRET_KEYS + AUDIT_KEYS of
    scripts/docker/compose_quickstart.py), parsed, never imported or copied. The fixture file is the pinned
    package's own bytes: its digest must equal the arc spec's pin."""
    import ast
    import hashlib
    import json
    root = Path(__file__).resolve().parents[1]
    generator = root / 'tools/test_ai_arc_fixtures/quickstart-package/scripts/docker/compose_quickstart.py'
    raw = generator.read_bytes()
    pin = json.loads((root / 'certification/ai-arc/arc.v1.json').read_text())['quickstartPackage']['files']
    assert 'sha256:' + hashlib.sha256(raw).hexdigest() == pin['scripts/docker/compose_quickstart.py']
    keys = []
    for node in ast.parse(raw).body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) in ('SECRET_KEYS', 'AUDIT_KEYS')
                                                for t in node.targets):
            keys.extend(ast.literal_eval(node.value))
    assert keys and len(set(keys)) == len(keys)
    return keys


@pytest.mark.parametrize('layout', ['dot-env', 'secret-files'])
def test_every_generated_secret_is_redacted_in_both_layouts(tmp_path, layout):
    """Sol (#1241 review): five generated secrets were missing from SECRET_KEYS on the .env layout."""
    import secrets as pysecrets
    import g3_compose_quickstart as G
    keys = _generated_secret_keys()
    assert set(keys) <= set(G.SECRET_KEYS), sorted(set(keys) - set(G.SECRET_KEYS))
    values = {k: 'v-' + pysecrets.token_hex(16) for k in keys}
    if layout == 'dot-env':
        (tmp_path / '.env').write_text('HONUA_IMAGE=x\n' + ''.join(f'{k}={v}\n' for k, v in values.items()))
    else:
        (tmp_path / '.env').write_text('HONUA_IMAGE=x\n')
        (tmp_path / 'secrets').mkdir()
        for k, v in values.items():
            (tmp_path / 'secrets' / k.lower()).write_text(v)
    found = G.secret_values(tmp_path)
    assert set(values.values()) <= set(found)
    text = G.redact(' '.join(values.values()), found)
    assert not any(v in text for v in values.values())
