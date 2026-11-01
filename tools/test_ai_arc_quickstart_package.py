"""ARC v2 B1/S4: setup on the product's shipped quickstart package, with the agent doing the install.

The harness builds the package with the product's own packager at the pinned revision from the candidate lock,
stages it plus the customer's .env choices (no secrets), and admits ``python3 quickstart.py``; the generated
credentials are read back by the harness afterwards."""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_local  # noqa: E402
import ai_arc_runner as R  # noqa: E402

SOURCE = Path(__file__).resolve().parent / 'test_ai_arc_fixtures' / 'quickstart-package'
PIN = R.load_arc()['quickstartPackage']
SERVER = 'sha256:' + 'a' * 64
EXECUTOR = 'sha256:' + 'b' * 64
INSTALL = {'image': 'ghcr.io/honua-io/honua-server', 'imageDigest': 'sha256:' + 'c' * 64}


def build(tmp_path, **kw):
    args = dict(server=SERVER, executor=EXECUTOR, worker=SERVER, version='2026.1-rc.2', **kw)
    return ai_arc_local.build_quickstart_package(SOURCE, PIN, tmp_path / 'build', **args)


def test_the_package_is_the_products_own_build_from_the_candidate_lock(tmp_path):
    files = build(tmp_path)
    assert set(files) == ai_arc_local.PACKAGE_FILES
    assert json.loads(files['images.json']) == {'server': SERVER, 'executor': EXECUTOR, 'worker': SERVER,
                                                'version': '2026.1-rc.2', 'maxSchema': PIN['candidateMaxSchema']}
    page = (SOURCE / 'docs/get-started/quickstart.md').read_text()
    # The package compose is the pinned guide's compose definition, byte for byte.
    assert files['compose.yaml'].decode() == ai_arc_local.quickstart_compose(page)
    assert files['quickstart.py'] == (SOURCE / 'scripts/docker/compose_quickstart.py').read_bytes()
    assert files['compose.executor.yaml'] == (SOURCE / 'docs/get-started/compose.executor.yaml').read_bytes()


def test_a_source_that_is_not_the_pin_is_refused(tmp_path):
    source = tmp_path / 'src'
    for rel in PIN['files']:
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes((SOURCE / rel).read_bytes())
    (source / 'scripts/docker/compose_quickstart.py').write_bytes(b'print("not the product")\n')
    with pytest.raises(ValueError, match='not the pinned quickstart package source'):
        ai_arc_local.build_quickstart_package(source, PIN, tmp_path / 'b', server=SERVER, executor=EXECUTOR,
                                              worker=SERVER, version='v')


def test_a_mutable_image_reference_is_refused_by_the_products_packager(tmp_path):
    with pytest.raises(RuntimeError, match='packager refused'):
        ai_arc_local.build_quickstart_package(SOURCE, PIN, tmp_path / 'b', server='honua:latest', executor=EXECUTOR,
                                              worker=SERVER, version='v')


def vm(tmp_path, run):
    identity, known = tmp_path / 'id', tmp_path / 'kh'
    identity.write_text('k')
    known.write_text('h')
    return ai_arc_local.VmSlot(host='honua-eval-1', user='ubuntu', identity_file=str(identity),
                               known_hosts=str(known), project='arc-abcdef123456', index=1, run=run)


def test_staging_writes_the_package_and_only_the_customers_choices(tmp_path):
    seen = {}

    def run(command, input=None, **kw):
        seen['script'] = input.decode()
        return subprocess.CompletedProcess(command, 0, b'27.3.1\n', b'')
    slot = vm(tmp_path, run)
    files = build(tmp_path)
    facts = slot.stage_package(INSTALL, files, PIN['priorMaxSchema'])
    script = seen['script']
    staged = {}
    for name, body in re.findall(r"base64 -d > (\S+) <<'HONUA_PKG_[0-9a-f]+'\n([A-Za-z0-9+/=]*)\n", script):
        staged[name.strip("'")] = base64.b64decode(body)
    assert set(staged) == ai_arc_local.PACKAGE_FILES | {'.env'}
    assert all(staged[n] == files[n] for n in files)
    env = dict(line.split('=', 1) for line in staged['.env'].decode().splitlines())
    image = INSTALL['image'] + '@' + INSTALL['imageDigest']
    assert env == {'COMPOSE_PROJECT_NAME': 'arc-abcdef123456', 'HONUA_IMAGE': image, 'HONUA_PRIOR_IMAGE': image,
                   'HONUA_PRIOR_MAX_SCHEMA': '140', 'HONUA_HTTP_PORT': '18180', 'HONUA_GRPC_PORT': '18181',
                   'HONUA_MANAGER_PORT': '18182'}
    assert not any('PASSWORD' in k or 'KEY' in k for k in env), 'the agent generates every secret'
    assert facts['composition'] == 'quickstart-package' and facts['managerPort'] == 18182
    assert '/home/ubuntu/honua-arc/arc-abcdef123456' in script and '/tmp' not in script


def test_the_products_initializer_completes_the_staged_choices(tmp_path):
    """The staged .env is what the product's quickstart.py accepts and completes (run here with --init-only)."""
    files = build(tmp_path)
    install = tmp_path / 'install'
    install.mkdir(mode=0o700)
    for name, raw in files.items():
        (install / name).write_bytes(raw)
    (install / '.env').write_text(vm(tmp_path, None).package_env(INSTALL, PIN['priorMaxSchema']))
    sock = Path('/var/run/docker.sock')
    if not sock.exists():
        pytest.skip('the product initializer reads the Docker socket group')
    done = subprocess.run([sys.executable, '-I', str(install / 'quickstart.py'), '--init-only'],
                          capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr
    env = dict(line.split('=', 1) for line in (install / '.env').read_text().splitlines())
    image = INSTALL['image'] + '@' + INSTALL['imageDigest']
    assert env['HONUA_IMAGE'] == image and env['HONUA_PRIOR_IMAGE'] == image and env['HONUA_PRIOR_MAX_SCHEMA'] == '140'
    assert env['HONUA_RELEASE_IMAGE'] == SERVER and env['HONUA_EXECUTOR_IMAGE'] == EXECUTOR
    assert env['COMPOSE_PROJECT_NAME'] == 'arc-abcdef123456' and env['HONUA_MANAGER_PORT'] == '18182'
    assert all(len(env[k]) >= 32 for k in ai_arc_local.INSTALLED_SECRET_KEYS)
    assert (install / 'secrets/keyring.pfx').is_file()


def test_the_harness_reads_the_generated_credentials_over_its_own_ssh(tmp_path):
    body = ''.join(f'{k}={"s" * 40}{i}\n' for i, k in enumerate(ai_arc_local.INSTALLED_SECRET_KEYS)) + 'HONUA_IMAGE=x\n'
    seen = {}

    def run(command, input=None, **kw):
        seen['script'] = input.decode()
        return subprocess.CompletedProcess(command, 0, body.encode(), b'')
    slot = vm(tmp_path, run)
    secrets = slot.read_installed_secrets()
    assert set(secrets) == set(ai_arc_local.INSTALLED_SECRET_KEYS) and 'HONUA_IMAGE' not in secrets
    assert slot.secrets == secrets and 'cat .env' in seen['script']


def test_an_install_that_generated_no_credentials_is_not_adopted(tmp_path):
    slot = vm(tmp_path, lambda c, input=None, **k: subprocess.CompletedProcess(c, 0, b'HONUA_IMAGE=x\n', b''))
    with pytest.raises(RuntimeError, match='did not generate'):
        slot.read_installed_secrets()


def test_package_setup_notes_say_only_what_the_customer_chose():
    notes = R.load_arc()['targets']['local-docker']['customerNotes']['setupPackage']
    assert '{release}' in notes and '{image}' in notes and '{port}' in notes and not R.forbidden_hits(notes)


def test_the_package_lock_comes_from_the_candidate_lock():
    import types
    arc = R.load_arc()
    dev = {'components': {'honua-server': {'artifacts': [
        {'kind': 'image', 'digest': SERVER, 'reference': 'honua-arc-dev/honua-server:0123456789ab'},
        {'kind': 'executor-image', 'digest': EXECUTOR, 'reference': 'honua-arc-dev/honua-compose-executor:0123456789ab',
         'local': True}]}}}
    assert R.executor_image(dev['components']['honua-server'], development=True) == {'executorImage': EXECUTOR}
    release = {'artifacts': [{'kind': 'executor-image', 'digest': EXECUTOR,
                              'reference': 'ghcr.io/honua-io/honua-compose-executor:2026.1-rc.2'}]}
    assert R.executor_image(release, development=False) == {
        'executorImage': 'ghcr.io/honua-io/honua-compose-executor@' + EXECUTOR}
    assert R.executor_image({'artifacts': []}, development=True) == {}
    ctx = types.SimpleNamespace(arc=arc, candidate={'imageDigest': SERVER, 'development': True, 'executorImage': EXECUTOR})
    assert R.package_lock(ctx) == {'server': SERVER, 'executor': EXECUTOR, 'worker': SERVER, 'version': '2026.1-rc.2'}
    with pytest.raises(ValueError, match='no immutable compose executor'):
        R.package_lock(types.SimpleNamespace(arc=arc, candidate={'imageDigest': SERVER, 'development': True}))


def test_teardown_hands_executor_files_back_before_removing_the_slot(tmp_path):
    seen = {}

    def run(command, input=None, **kw):
        seen['script'] = input.decode()
        return subprocess.CompletedProcess(command, 0, b'', b'')
    vm(tmp_path, run).teardown()
    script = seen['script']
    assert script.index('chown -R') < script.index('rm -rf') and '/home/ubuntu/honua-arc/arc-abcdef123456' in script
