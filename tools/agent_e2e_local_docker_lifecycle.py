"""One-time local Docker worker launch. No image build and no host provisioning.

The claim is persisted before `docker run`. A lost reply cannot start a second
container. Recovery inspects the retained name and does not run again.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_worker_lifecycle import validate_reservations


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


def refuse_unreviewed(argv):
    forbidden = {'--privileged', '--pid=host', '--network=host', '-v', '--volume', '--mount',
                 '--cap-add', '--device', '--add-host', '--env', '-e', '--env-file'}
    require(not forbidden.intersection(argv), 'local-docker launch accepts no host, volume, device or credential flag')
    for item in argv:
        require('docker.sock' not in item and item != 'host', 'local-docker launch cannot mount the host socket')


class LocalDockerLifecycle:
    """Start one prebuilt worker image on an existing Docker network."""

    def __init__(self, config, *, runner=docker):
        fields = {'candidateKey', 'attemptId', 'context', 'image', 'network', 'reservationIds', 'liability'}
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
        self.config, self.runner = value, runner
        self.name = 'honua-agent-'+value['attemptId']
        self.command = ['python', '-I', '/app/agent_e2e_public_entry.py']

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

    def start(self, durable):
        retained = self._retained(durable)
        require(retained['started'] is False and retained['containerId'] is None,
                'local-docker worker already started; inspect the retained container')
        retained['started'] = True
        durable._persist()
        argv = ['docker', 'run', '--detach', '--name', self.name, '--pull', 'never', '--read-only',
                '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--network', self.config['network'], self.config['image'], *self.command]
        refuse_unreviewed(argv)
        stdout = self.runner(argv)
        container_id = stdout.decode().strip()
        require(re.fullmatch(r'[0-9a-f]{64}', container_id), 'local-docker start did not return a container id')
        retained['containerId'] = container_id
        durable._persist()
        return copy.deepcopy(retained)

    def inspect(self, durable):
        retained = self._retained(durable)
        require(retained['started'] is True, 'local-docker worker was not started')
        raw = self.runner(['docker', 'inspect', self.name])
        observed = json.loads(raw)
        require(isinstance(observed, list) and len(observed) == 1, 'local-docker inspect is not one container')
        container = observed[0]
        require(container.get('Id') == retained['containerId'] and container.get('Image') == self.config['image'],
                'local-docker inspect differs from the retained launch')
        return container

    def stop(self, durable):
        retained = self._retained(durable)
        require(retained['started'] is True and isinstance(retained['containerId'], str),
                'local-docker stop requires the retained container')
        self.runner(['docker', 'stop', '--time', '10', self.name])
        container = self.inspect(durable)
        require(container.get('State', {}).get('Running') is False, 'local-docker worker is still running')
        return {'name': self.name, 'containerId': retained['containerId'], 'running': False,
                'digest': canonical_digest(retained)}
