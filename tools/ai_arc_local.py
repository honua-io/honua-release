"""Local-Docker arc slot on an ephemeral VM Docker host (runner-owned SSH).

The slot is one compose project in one private directory on one VM (up to three concurrent slots
per VM, each with its own ports). The harness:

  * stages the quickstart's secret step itself: ``.env`` with generated secrets, the rc.1 image by
    digest and the slot's ports, and the key ring (the quickstart's openssl commands, run on the
    VM). The agent never sees those values;
  * gives the agent #1160's ``run_quickstart`` shell (RemoteQuickstartShell): docker compose for
    this project only, with the compose definition admitted only when it is byte-identical to the
    published quickstart's (approved digest), curl to localhost, outputs withheld or reduced;
  * observes through its own SSH and a runner-owned ``ssh -L`` tunnel: container health, the
    served image's repo digest, the served revision, readiness and that admin reads need sign-in;
  * tears the project and directory down.

Identity: ``ssh`` with ``-i <identity>`` and an explicit known_hosts file, so a host alias such as
``honua-eval-1`` resolves through the controller's SSH config.
"""
from __future__ import annotations

import json
import re
import secrets as pysecrets
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

HOST = re.compile(r'\A[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z')
SLOT = re.compile(r'\Aarc-[a-z0-9]{8,40}\Z')
PORT_BASE, PORT_STRIDE, SLOTS_PER_HOST = 18080, 100, 3
SECRET_KEYS = ('POSTGRES_PASSWORD', 'HONUA_ADMIN_PASSWORD', 'HONUA_MASTER_KEY', 'HONUA_KEYRING_PASSWORD')


def resolve_alias(alias: str, run: Callable = subprocess.run) -> str:
    """The HostName an SSH alias points at (``ssh -G``), so ssh without the user's config reaches it."""
    if not HOST.fullmatch(alias):
        raise ValueError('invalid host alias')
    done = run(['ssh', '-G', alias], capture_output=True, text=True, timeout=20, check=False,
               stdin=subprocess.DEVNULL)
    for line in (done.stdout or '').splitlines():
        key, _, value = line.partition(' ')
        if key == 'hostname' and HOST.fullmatch(value.strip()):
            return value.strip()
    raise ValueError('could not resolve SSH alias ' + alias)


def quickstart_compose(page: str) -> str:
    """The published quickstart's compose definition, exactly as the page prints it."""
    import g3_compose_quickstart as g3
    fences = [f for f in g3.parse_document(page) if f.block_id == 'install.compose']
    if len(fences) != 1 or fences[0].language != 'yaml':
        raise ValueError('quickstart page does not carry exactly one compose definition')
    return fences[0].body.rstrip('\n') + '\n'


def slot_ports(index: int) -> tuple[int, int]:
    if type(index) is not int or not 0 <= index < SLOTS_PER_HOST:
        raise ValueError('slot index must be 0..2 (three concurrent arc runs per host)')
    http = PORT_BASE + PORT_STRIDE * index
    return http, http + 1


@dataclass
class VmSlot:
    host: str
    user: str
    identity_file: str
    known_hosts: str
    project: str
    index: int
    run: Callable = subprocess.run
    secrets: dict = field(default_factory=dict)

    def __post_init__(self):
        if not HOST.fullmatch(self.host) or not HOST.fullmatch(self.user) or not SLOT.fullmatch(self.project):
            raise ValueError('invalid VM slot identity')
        self.remote_dir = '/tmp/honua-arc-' + self.project
        self.http_port, self.grpc_port = slot_ports(self.index)

    # -- controller SSH (never handed to the model) ------------------------------------------
    def ssh(self, script: str, timeout: int = 300, stdin_extra: str = '') -> subprocess.CompletedProcess:
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'UserKnownHostsFile=' + self.known_hosts, '-i', self.identity_file,
                   '--', self.user + '@' + self.host, 'sh', '-s']
        return self.run(command, input=(script + stdin_extra).encode(), capture_output=True, timeout=timeout,
                        check=False)

    def compose(self, *args: str) -> str:
        return ('cd -- ' + shlex.quote(self.remote_dir) + ' && docker compose --project-directory '
                + shlex.quote(self.remote_dir) + ' --project-name ' + self.project + ' ' + shlex.join(args))

    def env_file(self, install: dict) -> str:
        self.secrets = {'POSTGRES_PASSWORD': pysecrets.token_hex(32),
                        'HONUA_ADMIN_PASSWORD': 'Aa1!' + pysecrets.token_hex(32),
                        'HONUA_MASTER_KEY': pysecrets.token_hex(32),
                        'HONUA_KEYRING_PASSWORD': pysecrets.token_hex(16)}
        lines = [f"HONUA_IMAGE={install['image']}@{install['imageDigest']}", f'HONUA_HTTP_PORT={self.http_port}',
                 f'HONUA_GRPC_PORT={self.grpc_port}'] + [f'{k}={v}' for k, v in self.secrets.items()]
        return '\n'.join(lines) + '\n'

    def stage(self, install: dict, compose: str | None = None) -> dict:
        """The guide's file-creation steps, done by the harness on the VM: compose.yaml (the published
        definition, byte for byte), .env with generated secrets and the slot's ports, and the key ring.
        The admitted shell cannot write files, so a customer following the guide would have made these
        before running it; the agent still has to bring the install up, check it and hand it over."""
        env = self.env_file(install)
        fence = 'HONUA_ENV_' + pysecrets.token_hex(8)
        script = (
            'set -eu\numask 077\n'
            f'mkdir -m 700 -- {shlex.quote(self.remote_dir)}\ncd -- {shlex.quote(self.remote_dir)}\n'
            f"cat > .env <<'{fence}'\n{env}{fence}\n"
            + (f"cat > compose.yaml <<'{fence}C'\n{compose}{fence}C\n" if compose else '')
            + 'mkdir -p secrets\n'
            'KP="$(grep \'^HONUA_KEYRING_PASSWORD=\' .env | cut -d= -f2)"\n'
            'openssl req -x509 -newkey rsa:2048 -keyout secrets/keyring.pem -out secrets/keyring.crt '
            '-days 3650 -nodes -subj "/CN=honua-quickstart" 2>/dev/null\n'
            'openssl pkcs12 -export -inkey secrets/keyring.pem -in secrets/keyring.crt '
            '-out secrets/keyring.pfx -passout "pass:${KP}"\n'
            'rm -f secrets/keyring.pem secrets/keyring.crt\nchmod 600 secrets/keyring.pfx\n'
            "docker info --format '{{.ServerVersion}}'\n")
        done = self.ssh(script)
        if done.returncode:
            raise RuntimeError('could not stage the slot on the VM')
        return {'remoteDir': self.remote_dir, 'httpPort': self.http_port, 'grpcPort': self.grpc_port,
                'dockerServerVersion': done.stdout.decode().strip().splitlines()[-1][:40]}

    def quickstart_shell(self, approved_compose: str):
        from agent_cli_backends import RemoteQuickstartShell, parse_quickstart
        from agent_cli_sessions import digest
        approved = digest(approved_compose.encode())

        def prevalidate(kind, action):
            """RemoteQuickstartShell.execute's own checks, all of which run before anything is sent to the
            VM: a failure here is a proven pre-dispatch refusal."""
            if kind != 'shell_command' or action.get('tool') != 'run_quickstart':
                raise ValueError('invalid shell action')
            args = action.get('arguments', {})
            if not isinstance(args, dict) or 'command' not in args or set(args) - {'command', 'stdin'}:
                raise ValueError('invalid shell arguments')
            command = parse_quickstart(args['command'])
            inline = any(command[i] in ('-f', '--file') and command[i + 1] == '-' for i in range(len(command) - 1))
            source = args.get('stdin')
            if inline and (not isinstance(source, str) or digest(source.encode()) != approved):
                raise ValueError('inline compose must match the runner-approved published quickstart')
            if not inline and source is not None:
                raise ValueError('stdin is only allowed for an approved inline compose file')
        shell = RemoteQuickstartShell(host=self.host, user=self.user, identity_file=self.identity_file,
                                     known_hosts=self.known_hosts, remote_dir=self.remote_dir, slot=self.project,
                                     approved_compose_sha256=approved, executor=self.run)
        shell.prevalidate = prevalidate
        return shell

    def tunnel(self, shell, port: int | None = None):
        from agent_cli_backends import SshTunnel
        return SshTunnel(shell, remote_port=port or self.http_port).start()

    def teardown(self) -> dict:
        script = ('set -u\n' + self.compose('down', '--volumes', '--remove-orphans', '--timeout', '30')
                  + '\nrc=$?\ncd /\nrm -rf -- ' + shlex.quote(self.remote_dir)
                  + '\nleft="$(docker ps -aq --filter label=com.docker.compose.project=' + self.project + ')"'
                  + '\n[ -z "$left" ] && [ "$rc" = 0 ]\n')
        done = self.ssh(script)
        return {'status': 'clean' if done.returncode == 0 else 'dirty',
                'steps': [{'step': 'compose-down-and-remove', 'exitCode': done.returncode}]}


class Tunnel:
    """Runner-owned ``ssh -L`` from a free loopback port here to the slot's port on the VM."""

    def __init__(self, slot: VmSlot, remote_port: int, popen=subprocess.Popen):
        import socket
        import time
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.process = popen(['ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                              '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + slot.known_hosts,
                              '-i', slot.identity_file, '-L', f'127.0.0.1:{port}:127.0.0.1:{int(remote_port)}',
                              '--', slot.user + '@' + slot.host],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('SSH tunnel exited before readiness')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                time.sleep(.1)
        else:
            self.close()
            raise TimeoutError('SSH tunnel did not become ready')
        self.base_url = f'http://127.0.0.1:{port}'

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def tunnel_for(slot: VmSlot) -> Tunnel:
    return Tunnel(slot, slot.http_port)


def observe_serving(slot: VmSlot, expected: dict, http: Callable, base_url: str) -> dict:
    """Controller reads: containers healthy, the server image's repo digest and served revision
    equal ``expected``, readiness, and that admin reads need sign-in."""
    ps = slot.ssh(slot.compose('ps', '--all', '--format', 'json'), 120)
    containers = []
    for line in ps.stdout.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        try:
            if line.startswith('['):
                containers.extend(json.loads(line))
            elif line.startswith('{'):
                containers.append(json.loads(line))
        except ValueError:
            continue
    server = [c for c in containers if c.get('Service') == 'honua']
    running = [c for c in containers if c.get('Service') != 'keyring-init']
    healthy = bool(server) and all(c.get('State') == 'running' and c.get('Health') in ('healthy', '', None)
                                   for c in running)
    repo_digests, label_revision = [], None
    if server and re.fullmatch(r'[A-Za-z0-9@:/._-]{1,300}', server[0].get('Image', '')):
        inspected = slot.ssh('docker image inspect --format \'{{json .RepoDigests}}|{{json .Config.Labels}}\' '
                             + shlex.quote(server[0]['Image']), 60)
        try:
            digests_raw, labels_raw = inspected.stdout.decode().strip().split('|', 1)
            repo_digests = json.loads(digests_raw) or []
            labels = json.loads(labels_raw) or {}
            label_revision = labels.get('org.opencontainers.image.revision')
        except (ValueError, AttributeError):
            repo_digests = []
    reference = expected['image'] + '@' + expected['imageDigest']
    ready = http('GET', base_url + '/healthz/ready', None)
    anonymous = http('GET', base_url + '/api/v1/admin/version', None)
    admin = http('GET', base_url + '/api/v1/admin/version', slot.secrets.get('HONUA_ADMIN_PASSWORD'))
    document = admin.get('json') if isinstance(admin.get('json'), dict) else {}
    document = document.get('data', document) if isinstance(document, dict) else {}
    instance = document.get('instance') if isinstance(document.get('instance'), dict) else {}
    # The served build: the server's informational version carries the commit as build metadata
    # (``x.y.z-pre+<sha>``); the image's OCI revision label is the controller-side cross-check.
    version = str(document.get('version') or '')
    metadata = version.split('+', 1)[1] if '+' in version else ''
    served = next((part for part in re.split(r'[.+-]', metadata) if re.fullmatch(r'[0-9a-f]{7,40}', part)), None)
    sha = expected['sourceSha']
    label_ok = isinstance(label_revision, str) and len(label_revision) >= 7 and sha.startswith(label_revision)
    served_ok = served is None or sha.startswith(served)
    revision_ok = label_ok and served_ok
    console = [c for c in containers if c.get('Service') == 'console' and c.get('State') == 'running']
    console_port = None
    for publisher in (console[0].get('Publishers') or []) if console else []:
        if isinstance(publisher, dict) and publisher.get('PublishedPort') and \
                str(publisher.get('URL', '127.0.0.1')) in ('127.0.0.1', '0.0.0.0', '::', ''):
            console_port = int(publisher['PublishedPort'])
            break
    evidence = {'console': bool(console),
                'containers': [{k: c.get(k) for k in ('Service', 'State', 'Health')} for c in containers],
                'repoDigestMatch': reference in repo_digests, 'servedRevision': served,
                'servedVersion': version, 'labelRevision': label_revision, 'expected': reference,
                'readyStatus': ready.get('status'), 'anonymousStatus': anonymous.get('status'),
                'adminStatus': admin.get('status')}
    return {'identity': reference in repo_digests and revision_ok, 'ready': healthy and ready.get('status') == 200,
            'authRequired': anonymous.get('status') in (401, 403) and admin.get('status') == 200,
            'console': bool(console), 'consolePort': console_port, 'evidence': evidence}
