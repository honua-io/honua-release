#!/usr/bin/env python3
"""Fixed installed poller entry. Missing original deployment custody refuses.

The identity stage is not proof of a later controller deployment. Startup joins
the actual self task and native supervisor history to its separately authenticated
final-installation sidecar before heartbeat, native claim or paid model work.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_e2e_console_runtime import NativeControl, ExecutionJournal, CONTROL_ENV, require
from agent_e2e_execution_protocol import digest, identifier
from agent_e2e_installed_container import actual_task
from agent_e2e_installed_driver import InstalledExecutionDriver
from agent_e2e_interactive_budget import validate_retained_funding
from agent_e2e_pack import canonical_digest
from agent_e2e_runtime_configuration import load_declaration
from agent_e2e_semantics import parse_response
from agent_e2e_state import StateStore, DurableExecution
from controller_handoff import sdk
from controller_source import verify as verify_source

CONFIG_ENV = 'HONUA_INSTALLED_CONTROLLER_CONFIG'
COMMAND = ['python', 'tools/hosted_pilot/installed_controller.py']


def configuration(raw):
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= 65536, 'installed controller configuration missing or oversized')
    value = parse_response(raw.encode())
    fields = {'schemaVersion', 'candidateKey', 'runtimeId', 'state', 'generation', 'declaration', 'registration',
        'identity', 'overhead', 'imageSource', 'originalFundingExpiresEpoch', 'poll'}
    require(isinstance(value, dict) and set(value) == fields and type(value['schemaVersion']) is int and value['schemaVersion'] == 1,
            'installed controller requires its closed checked configuration')
    digest(value['candidateKey']); identifier(value['runtimeId'], 'runtime ID')
    require(isinstance(value['state'], dict) and set(value['state']) == {'region', 'bucket', 'table'} and
            re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', str(value['state']['region'])) and
            isinstance(value['generation'], dict) and set(value['generation']) == {'key', 'version', 'digest'},
            'installed controller original state/generation is missing')
    digest(value['generation']['digest'])
    require(isinstance(value['generation']['key'], str) and value['generation']['key'].startswith('agent-e2e/'+value['candidateKey'][7:]+'/') and
            isinstance(value['generation']['version'], str) and value['generation']['version'] not in ('', 'null') and
            re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', str(value['state']['bucket'])) and
            re.fullmatch(r'[A-Za-z0-9_.-]{3,255}', str(value['state']['table'])), 'original candidate state namespace differs')
    for name in ('declaration', 'registration'):
        expected = {'path', 'sha256'} if name == 'declaration' else {'key', 'sha256'}
        require(isinstance(value[name], dict) and set(value[name]) == expected, 'original '+name+' reference differs')
        digest(value[name]['sha256'])
    require(value['declaration']['path'] == 'certification/agent-e2e/installed-runtimes/'+value['runtimeId']+'.json' and
            isinstance(value['registration']['key'], str) and len(value['registration']['key']) <= 2048,
            'installed runtime source/registration path differs')
    ref = value['identity']
    require(isinstance(ref, dict) and set(ref) == {'bucket', 'key', 'versionId', 'sha256'} and
            all(isinstance(ref[k], str) and ref[k] and len(ref[k]) <= 2048 for k in ('bucket', 'key', 'versionId')) and
            ref['versionId'] != 'null', 'original immutable staged identity reference differs')
    digest(ref['sha256'])
    overhead = value['overhead']
    require(isinstance(overhead, dict) and set(overhead) == {'operationId', 'basisDigest', 'reservationIds'} and
            isinstance(overhead['reservationIds'], dict) and set(overhead['reservationIds']) == {'infrastructure', 'cleanup'} and
            all(isinstance(v, str) and 0 < len(v) <= 256 for v in overhead['reservationIds'].values()) and
            len(set(overhead['reservationIds'].values())) == 2, 'original installed window liabilities are missing')
    identifier(overhead['operationId'], 'overhead operation'); digest(overhead['basisDigest'])
    require(isinstance(value['poll'], dict) and all(type(v) is int for v in value['poll'].values()) and
            type(value['originalFundingExpiresEpoch']) is int and value['originalFundingExpiresEpoch'] > 0 and
            value['poll'] == {'intervalSeconds': 10, 'maxPending': 16, 'maxLifetimeSeconds': 6000},
            'installed controller poll/lifetime is not the fixed bounded protocol')
    image = value['imageSource']
    require(isinstance(image, dict) and set(image) == {'sourceSha', 'treeId', 'archiveSha256'} and
            all(isinstance(image[k], str) and re.fullmatch(r'[0-9a-f]{40}', image[k]) for k in ('sourceSha', 'treeId')) and
            re.fullmatch(r'[0-9a-f]{64}', str(image['archiveSha256'])), 'installed controller image source is not pinned')
    return value


def verify_network(task, identity, ec2):
    attachments = [item for item in task.get('attachments', []) if item.get('type') == 'ElasticNetworkInterface']
    require(len(attachments) == 1 and attachments[0].get('status') == 'ATTACHED', 'actual controller network attachment unavailable')
    details = attachments[0].get('details', [])
    ids = [item.get('value') for item in details if item.get('name') == 'networkInterfaceId']
    subnets = [item.get('value') for item in details if item.get('name') == 'subnetId']
    require(len(ids) == len(subnets) == 1 and isinstance(ids[0], str) and re.fullmatch(r'eni-[0-9a-f]{8,17}', ids[0]),
            'actual controller network identifiers are missing or ambiguous')
    response = ec2.describe_network_interfaces(NetworkInterfaceIds=ids)
    interfaces = response.get('NetworkInterfaces', [])
    require(not response.get('NextToken') and len(interfaces) == 1, 'current controller network read is incomplete')
    actual = interfaces[0]
    groups = [group.get('GroupId') for group in actual.get('Groups', [])]
    require(actual.get('NetworkInterfaceId') == ids[0] and actual.get('Status') == 'in-use' and
            actual.get('OwnerId') == task['taskArn'].split(':')[4] and actual.get('VpcId') == identity['vpcId'] and
            actual.get('SubnetId') == subnets[0] and actual['SubnetId'] in identity['subnetIds'] and
            len(groups) == len(set(groups)) and sorted(groups) == sorted(identity['securityGroupIds']),
            'current controller VPC/subnet/security groups differ from original staged identity')
    return response


def verify_task_overrides(task, container):
    overrides = task.get('overrides', {})
    require(isinstance(overrides, dict) and set(overrides) <= {'containerOverrides', 'inferenceAcceleratorOverrides'} and
            not overrides.get('inferenceAcceleratorOverrides'), 'installed task has unreviewed role/resource overrides')
    selected = overrides.get('containerOverrides', [])
    require(isinstance(selected, list) and len(selected) <= 1 and all(isinstance(row, dict) and set(row) == {'name'} and
            row['name'] == container['name'] for row in selected), 'installed task has command/environment overrides')
    actual = task.get('containers', [])
    require(task.get('enableExecuteCommand') is False and len(actual) == 1 and actual[0].get('name') == container['name'] and
            actual[0].get('image') == container.get('image'), 'actual installed container/image or execute-command authority differs')


class InstalledDurable(DurableExecution):
    """One state CAS stream for the model loop and independent native heartbeat."""
    def __init__(self, *args, **kwargs):
        self.runtime_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def _persist(self):
        with self.runtime_lock:
            return super()._persist()

    def update(self, *args, **kwargs):
        with self.runtime_lock:
            return super().update(*args, **kwargs)


class InstalledNative(NativeControl):
    def _call(self, *args, **kwargs):
        with self.durable.runtime_lock:
            return super()._call(*args, **kwargs)


class InstalledJournal(ExecutionJournal):
    def __init__(self, durable, original):
        with durable.runtime_lock:
            super().__init__(durable, original)

    def append(self, *args, **kwargs):
        with self.durable.runtime_lock:
            return super().append(*args, **kwargs)


def final_installation_reference(task, *, states, candidate, runtime):
    """Locate only the native original start input, never a caller witness."""
    tags = task.get('tags', [])
    wanted = {'honua-agent-candidate': candidate, 'honua-agent-runtime': runtime}
    require(all([row.get('value') for row in tags if row.get('key') == name] == [value] for name, value in wanted.items()),
            'actual installed controller tags differ from original candidate/runtime')
    arns = [row.get('value') for row in tags if row.get('key') == 'honua-agent-controller-execution']
    require(len(arns) == 1 and isinstance(arns[0], str) and re.fullmatch(
        r'arn:aws:states:[a-z0-9-]+:[0-9]{12}:execution:[A-Za-z0-9_-]{1,80}:[A-Za-z0-9_-]{1,80}', arns[0]),
        'actual installed supervisor execution tag is unavailable')
    actual = states.describe_execution(executionArn=arns[0])
    require(actual.get('executionArn') == arns[0] and actual.get('status') == 'RUNNING' and
            actual.get('redriveCount', 0) == 0 and actual.get('inputDetails', {}).get('included') is True,
            'installed controller is not its original no-redrive supervisor')
    original = parse_response(actual['input'].encode())
    require(isinstance(original, dict) and set(original) == {'schemaVersion', 'candidateKey', 'runtimeId',
        'registrationDigest', 'configurationDigest', 'installation'} and type(original['schemaVersion']) is int and original['schemaVersion'] == 1 and
        original['candidateKey'] == candidate and original['runtimeId'] == runtime,
        'original installed supervisor input differs')
    digest(original['registrationDigest']); digest(original['configurationDigest'])
    token, starts, seen = None, [], set()
    for _ in range(10):
        request = {'executionArn': arns[0], 'reverseOrder': False, 'maxResults': 1000, 'includeExecutionData': True}
        if token is not None: request['nextToken'] = token
        page = states.get_execution_history(**request)
        require(isinstance(page.get('events'), list), 'installed supervisor history unavailable')
        for event in page['events']:
            require(type(event.get('id')) is int and event['id'] not in seen, 'installed supervisor history overlaps')
            seen.add(event['id'])
            if event.get('type') == 'ExecutionStarted': starts.append(event)
        token = page.get('nextToken')
        if token is None: break
    else:
        raise ValueError('installed supervisor history exceeds retained read bounds')
    require(len(starts) == 1 and starts[0].get('id') == 1 and
            starts[0].get('executionStartedEventDetails', {}).get('inputDetails', {}).get('truncated') is False and
            parse_response(starts[0]['executionStartedEventDetails']['input'].encode()) == original,
            'actual installed supervisor original input is missing or truncated')
    return original, actual


class Heartbeat:
    def __init__(self, native, task, digest_value, guard):
        self.native, self.task, self.digest, self.guard = native, task, digest_value, guard
        self.stop, self.error = threading.Event(), None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def once(self):
        self.guard()
        self.native.heartbeat(self.task, self.digest)

    def _run(self):
        while not self.stop.wait(10):
            try: self.once()
            except Exception as error:
                self.error = type(error).__name__  # No credential/request text in diagnostics.
                return

    def start(self):
        self.once()
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None: self.thread.join(timeout=35)


def dependencies():
    # These source/receipt producers are separate installation prerequisites.
    # Missing modules cannot be replaced by caller-authored role/slot JSON.
    try:
        identity = importlib.import_module('installed_controller_identity').verify_controller_identity
        installation = importlib.import_module('installed_controller_installation').verify_current_installation
        registration = importlib.import_module('installed_evaluation_binding').registered_runtime
    except (ImportError, AttributeError) as error:
        raise ValueError('checked installed controller custody producer dependency is unavailable') from error
    return identity, installation, registration


def run(config, *, clients, task_id, source_receipt, identity_verifier, installation_verifier,
        registration_reader, clock=time.time, sleep=time.sleep):
    started = clock()
    deadline = min(started+6000, config['originalFundingExpiresEpoch'])
    require(started < deadline, 'original installed funding expired; no new activation')
    state = config['state']
    s3, ecs, states, iam, ec2 = (clients[name] for name in ('s3', 'ecs', 'stepfunctions', 'iam', 'ec2'))
    cluster = task_id.split(':', 5)[5].split('/')[1]
    with tempfile.TemporaryDirectory(prefix='installed-controller-', dir='/work') as directory:
        store = StateStore(s3=s3, dynamodb=clients['dynamodb'], bucket=state['bucket'], table=state['table'],
            candidate_key=config['candidateKey'], clock=clock)
        durable = InstalledDurable(store, Path(directory)/'budget.json')
        pulse = None
        try:
            require(store.generation == config['generation'], 'installed controller differs from original accounting generation')
            def funding_guard():
                durable._require_live_lease()
                validate_retained_funding(durable.state)
                require(clock() < deadline and durable.state['ledger'].get('interactiveAccounting', {}).get('originalExpiresEpoch') ==
                    config['originalFundingExpiresEpoch'], 'installed controller lifetime/original funding ended')
            funding_guard()
            verified = load_declaration(config['declaration'], durable=durable, s3=s3, ecs=ecs, states=states, guard=funding_guard)
            require(verified['runtimeId'] == config['runtimeId'] and verified['candidateKey'] == config['candidateKey'] and
                    verified['operation'] == {'operationId': config['overhead']['operationId'],
                        'liabilityBasisDigest': config['overhead']['basisDigest'], 'reservationIds': config['overhead']['reservationIds']},
                    'installed controller original shared-window liability or runtime differs')
            registration = registration_reader(durable, verified)
            prefix = 'native-runtime-registration:'+canonical_digest({'candidateKey': config['candidateKey'], 'runtimeId': config['runtimeId']})[7:]
            completed = durable.state.get('scenarioOperations', {}).get(config['registration']['key'])
            require(config['registration']['key'] == prefix+'/completed' and isinstance(completed, dict) and
                    canonical_digest(completed) == config['registration']['sha256'], 'original installed registration reference differs')
            def observe_installation():
                funding_guard()
                identity = identity_verifier(config['identity'], durable=durable, runtime_id=config['runtimeId'], s3=s3,
                    iam=iam, ec2=ec2, guard=funding_guard)
                require(identity['activation'] is False and identity['qualification'] is False, 'identity proof cannot assert activation')
                response = ecs.describe_tasks(cluster=cluster, tasks=[task_id], include=['TAGS'])
                require(not response.get('failures') and len(response.get('tasks', [])) == 1, 'actual installed self task unavailable')
                task = response['tasks'][0]
                require(task.get('taskArn') == task_id and task.get('lastStatus') == 'RUNNING', 'actual installed self task stopped or changed')
                verify_network(task, identity, ec2)
                original, execution = final_installation_reference(task, states=states, candidate=config['candidateKey'], runtime=config['runtimeId'])
                require(original['configurationDigest'] == canonical_digest(config) and
                        original['registrationDigest'] == registration['registrationDigest'], 'original supervisor configuration/registration changed')
                final = installation_verifier(original['installation'], durable=durable, runtime_id=config['runtimeId'],
                    registration_digest=registration['registrationDigest'], task=task, s3=s3, states=states, ecs=ecs, guard=funding_guard)
                require(final['activation'] is False and final['qualification'] is False and
                        final['configurationDigest'] == canonical_digest(config) and final['imageSource'] == config['imageSource'] and
                        final['taskDefinitionArn'] == task['taskDefinitionArn'] and final['taskRoleArn'] == identity['taskRoleArn'] and
                        final['executionRoleArn'] == identity['executionRoleArn'] and final['supervisor']['executionArn'] == execution['executionArn'],
                        'actual final controller deployment differs from original staged roles/source/supervisor')
                definition = ecs.describe_task_definition(taskDefinition=final['taskDefinitionArn'])['taskDefinition']
                containers = definition.get('containerDefinitions', [])
                require(definition.get('taskRoleArn') == identity['taskRoleArn'] and
                        definition.get('executionRoleArn') == identity['executionRoleArn'] and len(containers) == 1,
                        'actual controller task/credential roles differ')
                container = containers[0]
                verify_task_overrides(task, container)
                key = verified['keys']['control']['issued']
                require(container.get('command') == COMMAND and not container.get('entryPoint') and
                        container.get('user') == '10001' and container.get('readonlyRootFilesystem') is True and
                        container.get('secrets') == [{'name': CONTROL_ENV, 'valueFrom': key['secretArn']+':::'+key['versionId']}],
                        'installed controller command/identity/control injection differs')
                environment = container.get('environment', [])
                require(len(environment) == 2 and len({row.get('name') for row in environment}) == 2,
                        'installed controller has unreviewed or duplicate environment inputs')
                environment = {row['name']: row['value'] for row in environment}
                require(set(environment) == {'AWS_DEFAULT_REGION', CONFIG_ENV} and environment['AWS_DEFAULT_REGION'] == state['region'] and
                        configuration(environment[CONFIG_ENV]) == config, 'actual controller nonsecret configuration differs')
                verify_source(source_receipt, config['imageSource']['sourceSha'], config['imageSource']['treeId'], config['imageSource']['archiveSha256'])
                return identity, task
            observe_installation()
            native = InstalledNative(durable=durable, configuration={'endpoint': verified['endpoint'], 'runtimeId': config['runtimeId'],
                'targetId': verified['targetId'], 'runtimeBindingDigest': registration['registrationDigest'], 'overhead': config['overhead']}, clock=clock)
            pulse = Heartbeat(native, task_id, verified['configurationDigest'], observe_installation)
            def guard():
                funding_guard()
                require(pulse.error is None, 'installed native heartbeat/authority was lost')
                observe_installation()
            pulse.start()
            driver = InstalledExecutionDriver(native=native, verified=verified, registration=registration, states=states, ecs=ecs,
                db=clients['dynamodb'], sqs=clients['sqs'], iam=iam, bedrock=clients['bedrock-runtime'], guard=guard,
                clock=clock, sleep=sleep, journal_factory=InstalledJournal)
            while clock() < deadline:
                guard()
                rows = native.pending()['value']['executions']
                # Restart discovers original active journals independently of the
                # derived queued index. It never manufactures a replacement E.
                recovery = [row for row in durable.state.get('interactiveExecutions', {}).values()
                    if row['runtimeId'] == config['runtimeId'] and any(admission['executionId'] == row['executionId'] and
                        admission['state'] == 'active' for admission in durable.state['ledger']['interactivePaidAdmissions'])]
                identities = {row['executionId']: row for row in rows}
                identities.update({row['executionId']: row for row in recovery})
                for row in identities.values():
                    original = native.read(row['executionId'], row['workspaceId'], row['turnId'])
                    if original['value']['execution']['state'] in ('queued', 'claimed', 'running'):
                        driver.execute(original)
                sleep(min(10, max(0, deadline-clock())))
        finally:
            if pulse is not None: pulse.close()
            with durable.runtime_lock:
                durable.close()


def main():
    try:
        require(os.getuid() == 10001, 'installed controller requires the fixed nonroot task')
        config = configuration(os.environ.get(CONFIG_ENV))
        identity, installation, registration = dependencies()
        source = json.loads(Path('/opt/honua-controller-source.json').read_text())
        image = config['imageSource']
        verify_source(source, image['sourceSha'], image['treeId'], image['archiveSha256'])
        task = actual_task()
        clients = {name: sdk(name, config['state']['region']) for name in
            ('s3', 'dynamodb', 'ecs', 'stepfunctions', 'sqs', 'iam', 'ec2', 'bedrock-runtime')}
        run(config, clients=clients, task_id=task, source_receipt=source, identity_verifier=identity,
            installation_verifier=installation, registration_reader=registration)
        return 0
    except Exception:
        print('Installed controller stopped; original outcomes and liabilities require reconciliation.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
