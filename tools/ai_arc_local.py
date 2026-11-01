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

import base64
import hashlib
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
# The product's quickstart package (honua-server scripts/docker/package_quickstart.py) and the secrets its
# initializer generates (scripts/docker/compose_quickstart.py SECRET_KEYS and AUDIT_KEYS).
PACKAGE_FILES = frozenset(('compose.yaml', 'compose.executor.yaml', 'quickstart.py', 'images.json', 'README.md'))
INSTALLED_SECRET_KEYS = ('POSTGRES_PASSWORD', 'HONUA_ADMIN_PASSWORD', 'HONUA_MANAGER_ADMIN_PASSWORD',
                         'HONUA_MANAGER_POSTGRES_PASSWORD', 'HONUA_MASTER_KEY', 'HONUA_MANAGER_MASTER_KEY',
                         'HONUA_KEYRING_PASSWORD', 'HONUA_AUDIT_CHAIN_KEY', 'HONUA_MANAGER_AUDIT_CHAIN_KEY')


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


def slot_directory(user: str, project: str) -> str:
    """The slot's directory on the eval VM: /home/<user>/honua-arc/<project> (/root/... for root)."""
    home = '/root' if user == 'root' else '/home/' + user
    return f'{home}/honua-arc/{project}'


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
        # On the VM's real disk, under the run user's home (not /tmp: Studio rejects it, and the lane rules forbid
        # it). One directory per slot project; created mode 0700 and removed at teardown.
        self.remote_dir = slot_directory(self.user, self.project)
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
            f'mkdir -p -m 700 -- {shlex.quote(str(Path(self.remote_dir).parent))}\n'
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

    def package_env(self, install: dict, prior_max_schema: int) -> str:
        """The customer's own choices for the packaged quickstart, before its initializer runs: which release
        to serve first (the install release), the prior image and its schema for rollback (never guessed: the
        install release's), this slot's ports and Compose project. No secret: quickstart.py generates them."""
        if type(prior_max_schema) is not int or not 0 < prior_max_schema < 10000:
            raise ValueError('the prior image schema must be an exact migration number')
        image = f"{install['image']}@{install['imageDigest']}"
        rows = {'COMPOSE_PROJECT_NAME': self.project, 'HONUA_IMAGE': image, 'HONUA_PRIOR_IMAGE': image,
                'HONUA_PRIOR_MAX_SCHEMA': str(prior_max_schema), 'HONUA_HTTP_PORT': str(self.http_port),
                'HONUA_GRPC_PORT': str(self.grpc_port), 'HONUA_MANAGER_PORT': str(self.manager_port)}
        return ''.join(f'{k}={v}\n' for k, v in rows.items())

    @property
    def manager_port(self) -> int:
        return self.http_port + 2          # the package's own layout: serving, gRPC, management

    def stage_package(self, install: dict, package: dict[str, bytes], prior_max_schema: int) -> dict:
        """The customer downloads the product's quickstart package into an empty private directory and writes
        their choices into .env. Everything else (secrets, the key ring, starting the stack) is the agent's
        work through ``python3 quickstart.py``."""
        if set(package) != PACKAGE_FILES or any(not isinstance(v, bytes) for v in package.values()):
            raise ValueError('the quickstart package is incomplete')
        fence = 'HONUA_PKG_' + pysecrets.token_hex(8)
        files = dict(package, **{'.env': self.package_env(install, prior_max_schema).encode()})
        if any(fence.encode() in raw for raw in files.values()):
            raise ValueError('invalid package delimiter')
        script = ('set -eu\numask 077\n'
                  f'mkdir -p -m 700 -- {shlex.quote(str(Path(self.remote_dir).parent))}\n'
                  f'mkdir -m 700 -- {shlex.quote(self.remote_dir)}\ncd -- {shlex.quote(self.remote_dir)}\n'
                  + ''.join(f"base64 -d > {shlex.quote(name)} <<'{fence}'\n"
                            + base64.b64encode(raw).decode() + f'\n{fence}\n' for name, raw in sorted(files.items()))
                  + "docker info --format '{{.ServerVersion}}'\n")
        done = self.ssh(script)
        if done.returncode:
            raise RuntimeError('could not stage the quickstart package on the VM')
        return {'remoteDir': self.remote_dir, 'httpPort': self.http_port, 'grpcPort': self.grpc_port,
                'managerPort': self.manager_port, 'composition': 'quickstart-package',
                'packageSha256': {n: 'sha256:' + hashlib.sha256(r).hexdigest() for n, r in sorted(package.items())},
                'dockerServerVersion': done.stdout.decode().strip().splitlines()[-1][:40]}

    def read_installed_secrets(self) -> dict:
        """After the agent's install: the secrets quickstart.py generated in .env, read by the harness over its own
        SSH (never through the model's shell) so it can act as the operator and redact them everywhere."""
        done = self.ssh('set -eu\ncd -- ' + shlex.quote(self.remote_dir) + '\ncat .env\n', 60)
        if done.returncode:
            raise RuntimeError('the installed .env could not be read')
        values = dict(line.split('=', 1) for line in done.stdout.decode().splitlines()
                      if '=' in line and not line.startswith('#'))
        wanted = INSTALLED_SECRET_KEYS
        if any(not values.get(k) for k in ('HONUA_ADMIN_PASSWORD', 'HONUA_MANAGER_ADMIN_PASSWORD')):
            raise RuntimeError('the install did not generate its administrator credentials')
        self.secrets = {k: values[k] for k in wanted if values.get(k)}
        return dict(self.secrets)

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
        d = shlex.quote(self.remote_dir)
        # Files the product's executor wrote (uid 1002: backups, receipts) are handed back before removal.
        script = ('set -u\n' + self.compose('down', '--volumes', '--remove-orphans', '--timeout', '30')
                  + f'\nrc=$?\ndocker run --rm -v {d}:{d} redis:7.4-alpine chown -R "$(id -u):$(id -g)" {d} 2>/dev/null'
                  + '\ncd /\nrm -rf -- ' + d
                  + '\nleft="$(docker ps -aq --filter label=com.docker.compose.project=' + self.project + ')"'
                  + '\n[ -z "$left" ] && [ "$rc" = 0 ]\n')
        done = self.ssh(script)
        return {'status': 'clean' if done.returncode == 0 else 'dirty',
                'steps': [{'step': 'compose-down-and-remove', 'exitCode': done.returncode}]}


def honua_server_file(revision: str, path: str = 'docker-compose.yml') -> bytes:
    """The file's bytes at an exact honua-server commit, read from GitHub (not from the caller)."""
    done = subprocess.run(['gh', 'api', '-H', 'Accept: application/vnd.github.raw',
                           f'repos/honua-io/honua-server/contents/{path}?ref={revision}'],
                          capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    if done.returncode:
        raise RuntimeError('honua-server compose could not be read at that revision')
    return done.stdout


def stage_console(slot: 'VmSlot', source_compose: str, revision: str | None, expected: dict,
                  source_at=honua_server_file) -> dict:
    """Add Console to the slot the way honua-server's own compose does (its 'console' profile service),
    when the published package quickstart has none. Harness slot preparation for stage 4, recorded as
    such; the agent never deploys or configures it.

    Fail-closed bindings: the compose bytes must be honua-server's file at ``revision`` (an exact commit,
    re-read independently); its console service must select exactly the arc's Console release digest
    (``expected``: arc spec ``console``); and the running container must report that image."""
    import hashlib
    import yaml
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('the Console compose needs an exact honua-server commit')
    raw = source_compose.encode()
    if hashlib.sha256(source_at(revision)).digest() != hashlib.sha256(raw).digest():
        raise ValueError('the Console compose is not honua-server docker-compose.yml at that commit')
    service = (yaml.safe_load(source_compose).get('services') or {}).get('console')
    if not isinstance(service, dict):
        raise ValueError('the source compose defines no console service')
    image = str(service['image'])
    match = re.fullmatch(r'\$\{HONUA_CONSOLE_IMAGE:-([^}]+)\}', image)
    image = match.group(1) if match else image
    if not re.fullmatch(r'[a-z0-9./-]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('the Console image is not pinned by digest')
    want = f"{expected['image']}@{expected['imageDigest']}"
    if image != want:
        raise ValueError(f"the compose selects Console {image.rsplit('@', 1)[-1][:19]}..., not the arc's "
                         f"{expected['imageDigest'][:19]}...")
    port = slot.http_port + 2
    console = {k: v for k, v in service.items() if k not in ('profiles', 'ports', 'image', 'deploy')}
    console.update(image=image, ports=[f'127.0.0.1:{port}:8080'])
    override = yaml.safe_dump({'services': {'console': console}}, sort_keys=True)
    fence = 'HONUA_CONSOLE_' + pysecrets.token_hex(6)
    script = ('set -eu\numask 077\ncd -- ' + shlex.quote(slot.remote_dir) + '\n'
              + f"cat > console.yaml <<'{fence}'\n{override}{fence}\n"
              + slot.compose('-f', 'compose.yaml', '-f', 'console.yaml', 'up', '-d', '--wait', '--wait-timeout', '240',
                             'console') + '\n'
              + 'id=$(' + slot.compose('-f', 'compose.yaml', '-f', 'console.yaml', 'ps', '-q', 'console') + ')\n'
              + 'docker image inspect --format "{{json .RepoDigests}}" '
                '"$(docker inspect --format "{{.Image}}" "$id")"\n')
    done = slot.ssh(script, 600)
    if done.returncode:
        raise RuntimeError('Console did not start on the slot')
    try:
        out = done.stdout.decode() if isinstance(done.stdout, bytes) else (done.stdout or '')
        digests = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        digests = None
    if not isinstance(digests, list) or want not in digests:
        raise RuntimeError('the running Console container does not report the arc Console digest')
    return {'image': image, 'port': port, 'source': 'honua-server docker-compose.yml console profile',
            'revision': revision, 'composeSha256': 'sha256:' + hashlib.sha256(raw).hexdigest(),
            'observedRepoDigests': digests, 'deployedBy': 'harness (stage-4 slot preparation)'}


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


CONTAINER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')


def running_image(slot, container: dict) -> tuple[str | None, list, dict]:
    """(image id, repo digests, labels) of the image the CONTAINER runs: its immutable ``.Image`` id,
    never a tag lookup (a tag can move after the container started). Unknown is ``(None, [], {})``."""
    ref = container.get('ID') or container.get('Name')
    if not isinstance(ref, str) or not CONTAINER.fullmatch(ref):
        return None, [], {}
    inspected = slot.ssh('docker image inspect --format \'{{json .RepoDigests}}|{{json .Config.Labels}}|{{json .Id}}\' '
                         '"$(docker inspect --format \'{{.Image}}\' ' + shlex.quote(ref) + ')"', 60)
    try:
        parts = inspected.stdout.decode().strip().split('|')
        digests = json.loads(parts[0]) or []
        labels = json.loads(parts[1]) or {}
        image_id = json.loads(parts[2])
    except (ValueError, AttributeError, IndexError):
        return None, [], {}
    if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        return None, [], {}
    return image_id, digests if isinstance(digests, list) else [], labels if isinstance(labels, dict) else {}


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
    repo_digests, label_revision, image_id = [], None, None
    if server:
        image_id, repo_digests, labels = running_image(slot, server[0])
        label_revision = labels.get('org.opencontainers.image.revision')
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
    console_digests = []
    console_image_id = None
    if console:
        console_image_id, console_digests, _ = running_image(slot, console[0])
    evidence = {'console': bool(console), 'consoleRepoDigests': console_digests, 'consoleImageId': console_image_id,
                'containers': [{k: c.get(k) for k in ('Service', 'State', 'Health')} for c in containers],
                'repoDigestMatch': reference in repo_digests, 'servedRevision': served,
                'servedVersion': version, 'labelRevision': label_revision, 'expected': reference,
                'readyStatus': ready.get('status'), 'anonymousStatus': anonymous.get('status'),
                'adminStatus': admin.get('status')}
    # A development candidate (built from trunk on the hosts, never pushed) has no registry digest: its
    # identity is the local image id plus the revision label.
    digest_ok = reference in repo_digests or (bool(expected.get('development')) and image_id == expected['imageDigest'])
    evidence['imageId'] = image_id
    return {'identity': digest_ok and revision_ok, 'ready': healthy and ready.get('status') == 200,
            'authRequired': anonymous.get('status') in (401, 403) and admin.get('status') == 200,
            'console': bool(console), 'consolePort': console_port, 'consoleRepoDigests': console_digests,
            'evidence': evidence}


def build_quickstart_package(source: Path, pin: dict, out: Path, *, server: str, executor: str, worker: str,
                             version: str, run: Callable = subprocess.run) -> dict[str, bytes]:
    """The product's customer package, built by the product's own packager at the arc's pinned revision.

    ``source`` holds the pinned files (arc spec ``quickstartPackage.files``), each checked against its pin. They
    are laid out as in honua-server, plus an empty migration named for the candidate's maximum schema (the
    packager reads only the migration file names). The packager then runs isolated (``python3 -I``), with the
    candidate release lock."""
    out = Path(out)
    tree = out / 'honua-server'
    for rel, want in sorted(pin['files'].items()):
        raw = (Path(source) / rel).read_bytes()
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != want:
            raise ValueError(f'{rel} is not the pinned quickstart package source')
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    schema = pin['candidateMaxSchema']
    if type(schema) is not int or not 0 < schema < 10000:
        raise ValueError('invalid candidate schema')
    migrations = tree / 'src/Honua.Server/Migrations'
    migrations.mkdir(parents=True, exist_ok=True)
    (migrations / f'{schema:03d}_PinnedCandidateSchema.sql').write_bytes(b'')
    package = out / 'package'
    done = run([sys.executable, '-I', str(tree / 'scripts/docker/package_quickstart.py'), '--server-image', server,
                '--executor-image', executor, '--worker-image', worker, '--version', version, '--output', str(package)],
               capture_output=True, timeout=120, check=False, cwd=str(out))
    if done.returncode:
        raise RuntimeError('the product packager refused the candidate lock: '
                           + (done.stderr.decode('utf-8', 'replace') if isinstance(done.stderr, bytes) else '')[-200:])
    files = {name: (package / name).read_bytes() for name in sorted(PACKAGE_FILES)}
    lock = json.loads(files['images.json'])
    if (lock.get('server'), lock.get('executor'), lock.get('worker'), lock.get('version'), lock.get('maxSchema')) != \
            (server, executor, worker, version, schema):
        raise ValueError('the package lock is not the candidate release lock')
    return files
