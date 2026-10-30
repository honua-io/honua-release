"""One-time local Docker worker launch. No image build and no host provisioning.

The claim is persisted before `docker run`. The process is the image's serving
entry, `agent_e2e_container_entry.py`, with the reviewed transport environment.
`agent_e2e_public_entry.py` is a later transport command, not the container
command. A lost reply cannot start a second container. Recovery inspects the
retained name, adopts its id, and does not run again.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_worker import Worker
from agent_e2e_worker_lifecycle import validate_reservations

COMMAND = ['python', '-I', '/app/agent_e2e_container_entry.py']
QUEUE = re.compile(r'https://sqs\.([a-z0-9-]+)\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+\.fifo')


def require(value, reason):
    if not value:
        raise PackError(reason)


def docker(argv, *, timeout=60):
    """Run one reviewed docker argv. Callers cannot append flags."""
    require(isinstance(argv, list) and argv and argv[0] == 'docker', 'local-docker launch is not a docker command')
    refuse_unreviewed(argv)
    result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    require(result.returncode == 0, 'local-docker command failed; retain the claim and observe')
    return result.stdout


def reviewed_environment(transport):
    """The serving entry reads this transport. No credential or MCP secret is included."""
    Worker(None, transport)
    regions = []
    for field in ('commandQueueUrl', 'replyQueueUrl'):
        match = QUEUE.fullmatch(transport[field])
        require(match, 'local-docker transport queue is not a regional FIFO')
        regions.append(match.group(1))
    require(regions[0] == regions[1], 'local-docker transport queues are in different regions')
    raw = json.dumps(transport, sort_keys=True, separators=(',', ':'), allow_nan=False)
    require('docker.sock' not in raw and 'AWS_SECRET' not in raw and 'AWS_SESSION_TOKEN' not in raw,
            'local-docker transport carries a credential or host socket')
    return ['HONUA_AGENT_TRANSPORT='+raw, 'AWS_DEFAULT_REGION='+regions[0]]


def refuse_unreviewed(argv, *, allowed_env=()):
    forbidden = {'--privileged', '--pid=host', '--network=host', '-v', '--volume', '--mount',
                 '--cap-add', '--device', '--add-host', '--env-file'}
    require(not forbidden.intersection(argv), 'local-docker launch accepts no host, volume, device or credential flag')
    supplied = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ('-e', '--env'):
            require(index+1 < len(argv), 'local-docker environment flag has no value')
            supplied.append(argv[index+1])
            index += 2
            continue
        require('docker.sock' not in item and item != 'host', 'local-docker launch cannot mount the host socket')
        index += 1
    require(tuple(supplied) == tuple(allowed_env), 'local-docker launch environment differs from the reviewed transport')


class LocalDockerLifecycle:
    """Start one prebuilt worker image on an existing Docker network."""

    def __init__(self, config, *, runner=docker):
        fields = {'candidateKey', 'attemptId', 'context', 'image', 'network', 'reservationIds', 'liability', 'transport'}
        require(isinstance(config, dict) and set(config) == fields, 'invalid local-docker worker configuration')
        value = copy.deepcopy(config)
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', value['candidateKey']), 'local-docker worker lacks a candidate')
        require(re.fullmatch(r'[a-z0-9-]{3,40}', value['attemptId']), 'invalid local-docker attempt identity')
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', value['image']), 'local-docker worker image must be an immutable digest')
        require(re.fullmatch(r'[a-z][a-z0-9_.-]{0,60}', value['network']) and value['network'] not in ('host', 'bridge', 'none'),
                'local-docker worker requires an existing non-host network')
        context = value['context']
        require(isinstance(context, dict) and set(context) == {'campaignId', 'caseId', 'attempt', 'campaignMode'} and
                context['campaignMode'] == 'development' and type(context['attempt']) is int and context['attempt'] in (1, 2) and
                isinstance(context['caseId'], str) and context['caseId'].endswith(':local-docker'),
                'local-docker worker is bound to one development local-docker case')
        require(value['transport'].get('attemptId') == value['attemptId'],
                'local-docker transport belongs to another attempt')
        self.config, self.runner = value, runner
        self.name = 'honua-agent-'+value['attemptId']
        self.command = list(COMMAND)
        self.environment = reviewed_environment(value['transport'])

    def _record(self):
        return {'candidateKey': self.config['candidateKey'], 'attemptId': self.config['attemptId'],
                'context': copy.deepcopy(self.config['context']), 'image': self.config['image'],
                'network': self.config['network'], 'name': self.name, 'command': list(self.command),
                'reservationIds': copy.deepcopy(self.config['reservationIds']),
                'liability': copy.deepcopy(self.config['liability']), 'started': False, 'containerId': None}

    def claim(self, durable):
        """Persist the only launch intent before any container exists."""
        durable._require_live_lease()
        record = self._record()
        validate_reservations(record, durable.state['ledger'])
        launches = durable.state.setdefault('dockerLaunches', {})
        require(record['attemptId'] not in launches, 'local-docker worker already claimed; observe without restarting')
        launches[record['attemptId']] = record
        durable._persist()
        return copy.deepcopy(record)

    def _retained(self, durable):
        durable._require_live_lease()
        retained = durable.state.get('dockerLaunches', {}).get(self.config['attemptId'])
        expected = self._record()
        require(isinstance(retained, dict), 'local-docker worker has no retained claim')
        for key, value in expected.items():
            if key in ('started', 'containerId'):
                continue
            require(retained.get(key) == value, 'local-docker worker claim differs from reviewed configuration')
        return retained

    def _launch_argv(self):
        argv = ['docker', 'run', '--detach', '--name', self.name, '--pull', 'never', '--read-only',
                '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--network', self.config['network']]
        for assignment in self.environment:
            argv.extend(['-e', assignment])
        argv.extend([self.config['image'], *self.command])
        return argv

    def _adopt(self, durable, retained, container):
        """Bind an inspected container to the claim. This never calls docker run."""
        container_id = container.get('Id')
        require(re.fullmatch(r'[0-9a-f]{64}', container_id or ''), 'local-docker inspect has no container id')
        require(container.get('Image') == self.config['image'], 'local-docker inspect image differs')
        require(container.get('Name') in (self.name, '/'+self.name), 'local-docker inspect name differs')
        config = container.get('Config') or {}
        require(config.get('Cmd') == self.command, 'local-docker inspect is not the serving entry')
        env = config.get('Env') or []
        require(all(assignment in env for assignment in self.environment),
                'local-docker inspect environment differs from the reviewed transport')
        require(not any(isinstance(item, str) and item.startswith(('AWS_SECRET', 'AWS_SESSION_TOKEN', 'AWS_ACCESS_KEY_ID', 'HONUA_AGENT_MCP'))
                        for item in env), 'local-docker inspect carries a credential or MCP secret')
        host = container.get('HostConfig') or {}
        require(host.get('Privileged') is not True and host.get('NetworkMode') == self.config['network'],
                'local-docker inspect network or privilege differs')
        binds = host.get('Binds') or []
        require(all(isinstance(item, str) and 'docker.sock' not in item for item in binds),
                'local-docker inspect mounts the host socket')
        if retained['containerId'] is None:
            retained['containerId'] = container_id
            durable._persist()
        require(retained['containerId'] == container_id, 'local-docker inspect differs from the retained launch')
        return container

    def start(self, durable):
        retained = self._retained(durable)
        if retained['started'] is True and retained['containerId'] is not None:
            raise PackError('local-docker worker already started; inspect the retained container')
        if retained['started'] is False:
            require(retained['containerId'] is None, 'local-docker worker identity exists without a launch claim')
            retained['started'] = True
            durable._persist()
            argv = self._launch_argv()
            refuse_unreviewed(argv, allowed_env=self.environment)
            stdout = self.runner(argv)
            container_id = stdout.decode().strip()
            require(re.fullmatch(r'[0-9a-f]{64}', container_id), 'local-docker start did not return a container id')
            retained['containerId'] = container_id
            durable._persist()
        container = self.inspect(durable)
        require(container.get('State', {}).get('Running') is True,
                'local-docker worker is not running the serving entry')
        return copy.deepcopy(self._retained(durable))

    def inspect(self, durable):
        retained = self._retained(durable)
        require(retained['started'] is True, 'local-docker worker was not started')
        raw = self.runner(['docker', 'inspect', self.name])
        observed = json.loads(raw)
        require(isinstance(observed, list) and len(observed) == 1, 'local-docker inspect is not one container')
        return self._adopt(durable, retained, observed[0])

    def stop(self, durable):
        retained = self._retained(durable)
        require(retained['started'] is True, 'local-docker stop requires the retained container')
        if retained['containerId'] is None:
            self.inspect(durable)
            retained = self._retained(durable)
        require(isinstance(retained['containerId'], str), 'local-docker stop could not recover the container')
        self.runner(['docker', 'stop', '--time', '10', self.name])
        container = self.inspect(durable)
        require(container.get('State', {}).get('Running') is False, 'local-docker worker is still running')
        return {'name': self.name, 'containerId': retained['containerId'], 'running': False,
                'digest': canonical_digest(retained)}
