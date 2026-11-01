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
    # A release lock (schemas/platform-lock.v1.schema.json) names the executor as a schema-valid image artifact of
    # the honua-server component, published under a compose-executor tag; the unsupported kind is ignored.
    plat = {'amd64': SERVER}
    release = {'artifacts': [
        {'kind': 'image', 'coordinate': 'ghcr.io/honua-io/honua-server', 'version': '2026.1.0-rc.2',
         'sourceRevision': 'a' * 40, 'digest': SERVER, 'platformDigests': plat, 'architectures': ['amd64']},
        {'kind': 'image', 'coordinate': 'ghcr.io/honua-io/honua-server', 'version': '2026.1.0-rc.2-compose-executor',
         'sourceRevision': 'a' * 40, 'digest': EXECUTOR, 'platformDigests': {'amd64': EXECUTOR},
         'architectures': ['amd64']}]}
    assert R.executor_image(release, development=False) == {'executorImage': 'ghcr.io/honua-io/honua-server@' + EXECUTOR}
    assert R.executor_image({'artifacts': [{'kind': 'executor-image', 'digest': EXECUTOR,
                                            'reference': 'ghcr.io/x/y:1'}]}, development=False) == {}
    assert R.executor_image({'artifacts': release['artifacts'] + [dict(release['artifacts'][1], digest=SERVER)]},
                            development=False) == {}         # two executors: none is chosen
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


def test_ops_adopts_the_installed_package_without_adding_to_the_stack(tmp_path):
    import types
    import ai_arc_ops_local as O
    scripts = []

    def run(command, input=None, **kw):
        text = input.decode()
        scripts.append(text)
        if 'ps' in text and 'json' in text:
            out = '\n'.join(json.dumps(r) for r in (
                {'Service': 'honua', 'State': 'running', 'Health': 'healthy'},
                {'Service': 'compose-manager', 'State': 'running', 'Health': 'healthy'},
                {'Service': 'compose-executor', 'State': 'running', 'Health': 'healthy'}))
            return subprocess.CompletedProcess(command, 0, out.encode(), b'')
        if 'HONUA_RELEASE_IMAGE' in text:
            return subprocess.CompletedProcess(command, 0, ('HONUA_RELEASE_IMAGE=' + SERVER + '\n').encode(), b'')
        return subprocess.CompletedProcess(command, 0, b'27.3.1\n', b'')
    slot = vm(tmp_path, run)
    slot.stage_package(INSTALL, build(tmp_path), PIN['priorMaxSchema'])
    assert slot.packaged and ' -f ' not in O.slot_compose(slot, 'ps')
    ctx = types.SimpleNamespace(slot=types.SimpleNamespace(vm=slot, facts={'quickstart': {'packageSha256': {'x': 1}}},
                                                           controller={'admin_key': 'a', 'manager_admin_key': 'm'}),
                                arc=R.load_arc())
    composition = O.LocalOps.adopt_package(object(), ctx)
    assert composition['kind'] == 'quickstart-package' and composition['managerPort'] == 18182
    assert composition['declaredRelease'] == SERVER
    assert not any('compose.executor.yaml <<' in s or ' up ' in s for s in scripts[1:]), 'nothing is added'
    assert O.manager_key(ctx) == 'm'


def test_ops_refuses_a_package_without_its_running_manager(tmp_path):
    import types
    import ai_arc_ops_local as O

    def run(command, input=None, **kw):
        return subprocess.CompletedProcess(command, 0, json.dumps(
            {'Service': 'honua', 'State': 'running', 'Health': 'healthy'}).encode(), b'')
    slot = vm(tmp_path, run)
    slot.packaged = True
    ctx = types.SimpleNamespace(slot=types.SimpleNamespace(vm=slot, facts={}, controller={}), arc=R.load_arc())
    with pytest.raises(RuntimeError, match='no running compose-manager'):
        O.LocalOps.adopt_package(object(), ctx)


def test_a_release_lock_with_the_executor_image_binds_the_server_image_as_the_candidate(tmp_path):
    """Codex review on #1208 (P1): the executor image beside the server image in a release lock is never taken
    as the candidate's serving image, and the candidate carries the executor for the package."""
    import hashlib
    lock = {'schema': 'honua.platform-lock/v1', 'components': {'honua-server': {
        'source': {'revision': 'a' * 40},
        'artifacts': [
            {'kind': 'image', 'coordinate': 'ghcr.io/honua-io/honua-server', 'version': '2026.1.0-rc.2-compose-executor',
             'sourceRevision': 'a' * 40, 'digest': EXECUTOR, 'platformDigests': {'amd64': EXECUTOR},
             'architectures': ['amd64']},
            {'kind': 'image', 'coordinate': 'ghcr.io/honua-io/honua-server', 'version': '2026.1.0-rc.2',
             'sourceRevision': 'a' * 40, 'digest': SERVER, 'platformDigests': {'amd64': SERVER},
             'architectures': ['amd64']}]}}}
    path = tmp_path / 'platform-lock.json'
    path.write_text(json.dumps(lock))
    digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    cand = R.candidate_from_lock(str(path), digest)
    assert cand['imageDigest'] == SERVER and cand['executorImage'] == 'ghcr.io/honua-io/honua-server@' + EXECUTOR


def test_the_local_campaign_wrapper_forwards_the_package_sources_to_campaign_and_aggregate(tmp_path):
    """Codex review on #1208 (P2): tools/ai_arc_local.sh accepts --quickstart-package-source (and the Console
    compose) and forwards the package sources to both the campaign and the aggregate."""
    import subprocess
    log = tmp_path / 'calls.txt'
    fake = tmp_path / 'py'
    fake.write_text('#!/usr/bin/env bash\necho "$@" >> ' + str(log) + '\n')
    fake.chmod(0o755)
    lock = tmp_path / 'lock.json'
    lock.write_text('{}')
    wrapper = Path(__file__).resolve().parent / 'ai_arc_local.sh'
    done = subprocess.run(['bash', str(wrapper), '--arm', 'A1', '--target', 'local-docker', '--lock-path', str(lock),
                           '--quickstart', 'q.md', '--quickstart-package-source', '/in/quickstart-package',
                           '--console-compose', '/in/docker-compose.yml', '--hosts', 'h1', '--out', str(tmp_path / 'c')],
                          env={'PYTHON': str(fake), 'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    campaign, aggregate = log.read_text().splitlines()
    assert 'ai_arc_campaign.py' in campaign and '--quickstart-package-source /in/quickstart-package' in campaign
    assert '--console-compose /in/docker-compose.yml' in campaign
    assert 'aggregate' in aggregate and '--quickstart-package-source /in/quickstart-package' in aggregate
