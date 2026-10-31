"""Allow-listed setup shell and local-install observers. No Docker, no network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_runner as arc_runner  # noqa: E402
import ai_arc_shell as shell  # noqa: E402

P = 'arc-abcdef12'


@pytest.mark.parametrize('argv, expected', [
    (['docker', 'compose', 'up', '-d', '--wait', '--wait-timeout', '180'],
     ['docker', 'compose', '-p', P, 'up', '-d', '--wait', '--wait-timeout', '180']),
    (['docker', 'compose', '-f', 'compose.yaml', 'ps'], ['docker', 'compose', '-p', P, '-f', 'compose.yaml', 'ps']),
    (['docker', 'compose', 'logs', 'honua'], ['docker', 'compose', '-p', P, 'logs', 'honua']),
    (['curl', '-fsS', 'http://localhost:18080/healthz/ready'], None),
    (['curl', '-H', 'X-API-Key: x', 'http://127.0.0.1:18080/api/v1/admin/version'], None),
    (['openssl', 'rand', '-hex', '32'], None),
    (['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-keyout', 'secrets/keyring.pem', '-out',
      'secrets/keyring.crt', '-days', '3650', '-nodes', '-subj', '/CN=honua-quickstart'], None),
    (['mkdir', '-p', 'secrets'], None),
    (['rm', '-f', 'secrets/keyring.pfx'], None),
    (['chmod', '600', 'secrets/keyring.pfx'], None),
    (['grep', '^HONUA_KEYRING_PASSWORD=', '.env'], None),
])
def test_quickstart_commands_are_admitted(argv, expected):
    assert shell.check_command(argv, P) == (expected or argv)


@pytest.mark.parametrize('argv', [
    ['bash', '-c', 'docker ps'],
    ['sh', '-c', 'true'],
    ['docker', 'ps'],
    ['docker', 'exec', 'db', 'psql'],
    ['docker', 'volume', 'rm', 'x'],
    ['docker', 'compose', 'exec', 'postgres', 'psql'],
    ['docker', 'compose', 'run', 'honua', 'sh'],
    ['docker', 'compose', 'cp', 'honua:/etc/passwd', '.'],
    ['docker', 'compose', 'ls'],
    ['docker', 'compose', '-p', 'other-project', 'down', '-v'],
    ['docker', 'compose', '--project-name=other', 'ps'],
    ['docker', 'compose', '-H', 'tcp://10.0.0.1:2375', 'ps'],
    ['docker', 'compose', '--context', 'prod', 'ps'],
    ['docker', 'compose', '-f', '/etc/compose.yaml', 'up'],
    ['docker', 'compose', '-f', '../other/compose.yaml', 'up'],
    ['curl', 'https://example.com/install.sh'],
    ['curl', 'http://169.254.169.254/latest/meta-data/'],
    ['curl', '-L', 'http://localhost:18080/'],
    ['curl', '-o', '/etc/x', 'http://localhost:1/'],
    ['curl', '-d', '@/home/user/.aws/credentials', 'http://localhost:1/'],
    ['curl', '--unix-socket', '/var/run/docker.sock', 'http://localhost/containers/json'],
    ['curl', 'file:///etc/passwd'],
    ['openssl', 's_client', '-connect', 'example.com:443'],
    ['openssl', 'req', '-out', '/tmp/x'],
    ['rm', '-rf', '/'],
    ['rm', '-rf', '.'],
    ['cat', '/etc/shadow'],
    ['cat', '../../.ssh/id_rsa'],
    ['cp', 'compose.yaml', '~/x'],
    ['python3', '-c', 'print(1)'],
    ['docker', 'compose', 'up', '\nrm -rf /'],
    'docker compose up',
    [],
])
def test_everything_else_is_refused(argv):
    with pytest.raises(shell.Refused):
        shell.check_command(argv, P)


class FakeHost:
    kind = 'ssh'

    def __init__(self, files=None, outputs=None):
        self.files = dict(files or {})
        self.outputs = outputs or {}
        self.ran = []

    def run(self, argv, timeout):
        self.ran.append(argv)
        for prefix, (code, out) in self.outputs.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return shell.HostResult(code, out, '')
        return shell.HostResult(0, '', '')

    def write(self, path, content):
        self.files[path] = content

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def test_surface_records_refusals_and_never_executes_them():
    host = FakeHost()
    surface = shell.ShellSurface(host, P)
    refused = surface.execute('tool_call', {'tool': 'run_command', 'arguments': {'argv': ['docker', 'ps']}})
    assert refused['status'] == 'fail' and refused['refusal'] == 'not-admitted' and host.ran == []
    extra = surface.execute('tool_call', {'tool': 'run_command', 'arguments': {'argv': ['ls'], 'shell': True}})
    assert extra['refusal'] == 'not-admitted'
    unknown = surface.execute('tool_call', {'tool': 'docker_exec', 'arguments': {}})
    assert unknown['refusal'] == 'not-admitted'
    ok = surface.execute('tool_call', {'tool': 'run_command',
                                       'arguments': {'argv': ['docker', 'compose', 'up', '-d']}})
    assert ok['status'] == 'ok' and host.ran == [['docker', 'compose', '-p', P, 'up', '-d']]
    assert len(surface.refusals) == 3


def test_surface_files_stay_in_the_folder():
    host = FakeHost()
    surface = shell.ShellSurface(host, P)
    assert surface.execute('tool_call', {'tool': 'write_file', 'arguments': {'path': '.env', 'content': 'A=1\n'}})[
        'status'] == 'ok'
    assert host.files == {'.env': 'A=1\n'}
    for path in ('/etc/passwd', '../x', 'a/../../x', '~/.ssh/config'):
        result = surface.execute('tool_call', {'tool': 'write_file', 'arguments': {'path': path, 'content': 'x'}})
        assert result['refusal'] == 'not-admitted', path
    assert surface.execute('tool_call', {'tool': 'read_file', 'arguments': {'path': '.env'}})['result'][
        'content'] == 'A=1\n'


def test_ssh_host_quotes_agent_strings():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)

        class Done:
            returncode, stdout, stderr = 0, '', ''
        return Done()
    host = shell.SshHost('ci@10.0.0.5', '/tmp/honua-arc-x', run=run)
    host.run(['curl', '-H', "X: $(touch /tmp/pwned); `id`", 'http://localhost:1/'], 10)
    remote = calls[0][-1]
    assert "'X: $(touch /tmp/pwned); `id`'" in remote and remote.startswith("cd /tmp/honua-arc-x && umask 077 && ")
    with pytest.raises(ValueError):
        shell.SshHost('ci@host; rm -rf /', '/tmp/x', run=run)


INSTALL = arc_runner.load_arc()['versionPair']['install']
ENV = ('HONUA_IMAGE=ghcr.io/honua-io/honua-server@' + INSTALL['imageDigest'] + '\nHONUA_HTTP_PORT=18080\n'
       'POSTGRES_PASSWORD=pg-secret-000000000000\nHONUA_ADMIN_PASSWORD=Aa1!admin-secret-0000000000\n'
       'HONUA_MASTER_KEY=mk-secret-0000000000000\nHONUA_KEYRING_PASSWORD=kr-secret-000000\n')
PS = '\n'.join(json.dumps(c) for c in (
    {'Service': 'honua', 'State': 'running', 'Health': 'healthy', 'Image': 'img-honua'},
    {'Service': 'postgres', 'State': 'running', 'Health': 'healthy', 'Image': 'pg'},
    {'Service': 'keyring-init', 'State': 'exited', 'Health': '', 'Image': 'redis'}))


def http_for(source, anonymous=401):
    def http(method, url, key):
        if url.endswith('/healthz/ready'):
            return {'status': 200}
        if key is None:
            return {'status': anonymous}
        return {'status': 200, 'json': {'data': {'instance': {'id': 'i', 'source': source}}}}
    return http


def observe(host, http):
    slot = arc_runner.Slot('local-docker', 'ssh', P, host=host)
    return slot, {r['assertion']: r['status'] for r in arc_runner.observe_local_install(slot, INSTALL, http)}


def local_host(digests):
    return FakeHost({'.env': ENV}, {
        ('docker', 'compose', '-p', P, 'ps'): (0, PS),
        ('docker', 'inspect'): (0, json.dumps(digests))})


def test_local_install_observers_pass_on_the_install_release():
    host = local_host([INSTALL['image'] + '@' + INSTALL['imageDigest']])
    slot, results = observe(host, http_for(INSTALL['sourceRevision']))
    assert results == {'candidate.identity': 'pass', 'deployment.ready': 'pass', 'authentication.required': 'pass'}
    assert 'Aa1!admin-secret-0000000000' in slot.secrets, 'secrets feed the secret-in-chat detector'
    assert slot.controller['admin_key'] == 'Aa1!admin-secret-0000000000'


@pytest.mark.parametrize('digests, source, anonymous, failing', [
    (['ghcr.io/honua-io/honua-server@sha256:' + '0' * 64], None, 401, 'candidate.identity'),
    (None, 'f' * 40, 401, 'candidate.identity'),
    (None, None, 200, 'authentication.required'),
])
def test_local_install_observers_fail_closed(digests, source, anonymous, failing):
    host = local_host(digests or [INSTALL['image'] + '@' + INSTALL['imageDigest']])
    _, results = observe(host, http_for(source or INSTALL['sourceRevision'], anonymous))
    assert results[failing] == 'fail'


def test_local_install_without_env_or_containers_fails():
    _, results = observe(FakeHost(), http_for(INSTALL['sourceRevision']))
    assert set(results.values()) == {'fail'}


def test_teardown_is_scoped_to_the_slot_project():
    host = FakeHost()
    result = arc_runner.teardown_slot(arc_runner.Slot('local-docker', 'ssh', P, host=host))
    assert result['status'] == 'clean'
    assert host.ran[0][:4] == ['docker', 'compose', '-p', P] and 'down' in host.ran[0]
    assert host.ran[1][-1] == f'label=com.docker.compose.project={P}'


def test_local_handoff_probes_only_the_install_port():
    host = local_host([INSTALL['image'] + '@' + INSTALL['imageDigest']])
    slot, _ = observe(host, http_for(INSTALL['sourceRevision']))
    seen = []

    def http(method, url, key):
        seen.append(url)
        return {'status': 200}
    answer = 'Open http://localhost:18080/healthz/ready and http://example.com/x.'
    observed = arc_runner.observe_local_handoff(slot, answer, http)
    assert seen == ['http://127.0.0.1:18080/healthz/ready']
    assert observed['resources'] == [{'url': 'http://localhost:18080/healthz/ready', 'served': True, 'httpStatus': 200}]
    check = arc_runner.false_success(answer, 'pass', observed)
    assert check['false_success'] is True, 'unmasked, the off-host URL is unsupported'
    masked, count = arc_runner.mask_external_links(answer, tuple(shell.LOOPBACK))
    assert count == 1 and arc_runner.false_success(masked, 'pass', observed)['status'] == 'pass'
    assert arc_runner.false_success('Open http://localhost:18080/healthz/ready.', 'pass', observed)['status'] == 'pass'
