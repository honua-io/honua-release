"""Real P10 A/B ECS development backend; no qualification or provisioning.

Uses an already IaC-provisioned isolated product/SQL target and a fresh immutable
per-attempt worker. All effects share the producer's fenced lease and retained
liabilities. Unknown setup, worker stop, SQL ownership or cost leaves liability
retained. No retry, guessed cleanup or model-authored oracle is accepted.
"""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import time

import agent_e2e_gate as gate
from agent_e2e_fixture_database import FixtureDatabase
from agent_e2e_knowledge import binding as knowledge_binding, verify as verify_knowledge
from agent_e2e_mcp_backend import McpActions
from agent_e2e_native_costs import fargate_compute, measured_settlement, reserve_overhead, serializable
from agent_e2e_p10_observer import P10Observer
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, selected_files
from agent_e2e_scenario_backend import ScenarioHttp, TownSetup, successful
from agent_e2e_terminal_backend import TerminalActions
from agent_e2e_worker import Worker
from agent_e2e_worker_evidence import open_evidence
from agent_e2e_worker_lifecycle import open_lifecycle

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT/'certification/agent-e2e/pack.v2.json'
PROTOCOL = 'honua.agent-e2e.backend/v1'
SESSIONS = {}


def require(value, reason):
    if not value:
        raise PackError(reason)


def sdk(service, region):
    import boto3
    from botocore.config import Config
    return boto3.client(service, region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 1}))


def identity(context):
    return {key: context[key] for key in ('campaignId', 'caseId', 'attempt')}


def worker_task_role(worker_config):
    """Bind the reviewed per-attempt unprivileged role, excluding control roles."""
    attempt = worker_config.get('attemptId')
    cluster = worker_config.get('lifecycle', {}).get('clusterArn', '')
    native = re.fullmatch(r'arn:aws:ecs:[a-z0-9-]+:([0-9]{12}):cluster/[A-Za-z0-9_-]+', cluster)
    require(native is not None and isinstance(attempt, str) and re.fullmatch(r'[a-z0-9-]{3,40}', attempt),
            'worker task role requires exact reviewed attempt and ECS account binding')
    expected = 'arn:aws:iam::'+native[1]+':role/honua-agent-'+attempt+'-worker'
    require(worker_config.get('taskRoleArn') == expected and
            expected != worker_config['lifecycle'].get('stateMachineRoleArn'),
            'worker task role is not its exact isolated attempt role; controller/fixture/oracle roles are forbidden')
    return expected


def verify_worker_definition(definition, worker_config):
    expected_role = worker_task_role(worker_config)
    require(definition.get('taskRoleArn') == expected_role and definition.get('executionRoleArn') != expected_role,
            'native worker task role differs from reviewed isolated identity or shares execution credentials')
    containers = definition.get('containerDefinitions', [])
    require(len(containers) == 1, 'P10 worker task must have one isolated container')
    container = containers[0]
    require(container.get('name') == 'worker' and container.get('image') == worker_config['imageUri'] and
            container.get('user') == '10001:10001' and container.get('readonlyRootFilesystem') is True and
            container.get('privileged') is False and not container.get('entryPoint') and
            container.get('command') == ['python', '-I', '/app/agent_e2e_container_entry.py'] and
            container.get('linuxParameters', {}).get('initProcessEnabled') is True and
            container.get('linuxParameters', {}).get('capabilities', {}).get('drop') == ['ALL'] and
            not container.get('linuxParameters', {}).get('capabilities', {}).get('add'),
            'P10 worker native image/command/user/isolation differs from reviewed runtime')
    env = container.get('environment', [])
    require(len(env) == 2 and {row['name'] for row in env} == {'HONUA_AGENT_TRANSPORT', 'AWS_DEFAULT_REGION'} and
            json.loads(next(row['value'] for row in env if row['name'] == 'HONUA_AGENT_TRANSPORT')) == worker_config['transport'],
            'P10 worker native transport/environment differs from exact reviewed binding')
    secrets = container.get('secrets', [])
    require(len(secrets) == 1 and secrets[0].get('name') == 'HONUA_AGENT_MCP_CONFIG' and
            re.fullmatch(r'arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:honua-agent-[a-z0-9-]+-mcp-[A-Za-z0-9]{6}:::[A-Za-z0-9-]{32,64}',
                         secrets[0].get('valueFrom', '')), 'P10 worker lacks its sole immutable agent-only MCP secret')
    return container


class Session:
    PROFILE = 'p10-native'
    CASE_PATTERN = r'P10:[AB]:aws-ecs-redis-on'
    CONFIG_FIELDS = {'http', 'observerMcp', 'connectionId', 'database', 'deployment', 'attempts', 'accounting'}
    OPTIONAL_FIELDS = {'ownershipTransfer'}
    DATABASE = FixtureDatabase

    def __init__(self, *, durable, campaign, bundle, configuration, context):
        require(campaign['campaignMode'] == 'development' and
                re.fullmatch(self.CASE_PATTERN, context['caseId']),
                self.PROFILE+' backend implements only explicit A/B ECS development; full 252 qualification remains unavailable')
        require(set(configuration) == {'knowledgeRoot', 'cases'} and context['caseId'] in configuration['cases'],
                'missing reviewed P10 native target configuration')
        self.durable, self.campaign, self.bundle = durable, copy.deepcopy(campaign), Path(bundle)
        self.context, self.config = copy.deepcopy(context), copy.deepcopy(configuration['cases'][context['caseId']])
        fields = self.CONFIG_FIELDS
        require(fields <= set(self.config) <= fields | self.OPTIONAL_FIELDS,
                'invalid P10 case configuration')
        accounting = self.config['accounting']
        require(set(accounting) == {'priceReceipt', 'overheadCoverage'} and
                set(accounting['priceReceipt']) == {'path', 'sha256'}, 'P10 lacks frozen pricing and overhead attribution')
        reference = accounting['priceReceipt']
        path = Path(reference['path'])
        require(path.is_file() and not path.is_symlink() and digest_bytes(path.read_bytes()) == reference['sha256'],
                'P10 frozen official price receipt unavailable or changed before effects')
        self.price_receipt = load_json(path)
        self.worker_config = self.config['attempts'].get(str(context['attempt']))
        require(isinstance(self.worker_config, dict) and set(self.worker_config) ==
                {'attemptId', 'imageUri', 'publicBundleDigest', 'taskRoleArn', 'lifecycle', 'transport', 'evidence'},
                'fresh IaC worker binding unavailable for exact P10 attempt')
        self.attempt_id = self.worker_config['attemptId']
        require(re.fullmatch(r'[a-z0-9-]{3,40}', self.attempt_id), 'invalid immutable worker attempt identity')
        worker_task_role(self.worker_config)
        self.key = canonical_digest(identity(context))
        self.namespace = 'ae_'+canonical_digest({'candidate': campaign['componentBindingDigest'],
            'attemptId': self.attempt_id, **identity(context)})[7:27]
        self.pack = load_json(PACK)
        self.case = gate.inventory(self.pack)[context['caseId']]
        self.corpus_path = PACK.parent/self.pack['corpusManifest']
        self.knowledge_root = Path(configuration['knowledgeRoot'])
        self.knowledge_manifest = load_json(self.knowledge_root/'knowledge-manifest.json')
        verify_knowledge(self.knowledge_root, self.knowledge_manifest, campaign['knowledge'])
        http = self.config['http']
        require(set(http) == {'endpoint', 'credentialKind', 'setupCredentialEnv', 'observerCredentialEnv'} and
                http['setupCredentialEnv'] != http['observerCredentialEnv'], 'P10 requires separate controller HTTP identities')
        secrets = [os.environ.get(http[key]) for key in ('setupCredentialEnv', 'observerCredentialEnv')]
        self.validate_observer(http, secrets)
        database = self.config['database']
        require(database['candidateKey'] == campaign['componentBindingDigest'] and database['target'] == self.case['target'],
                'P10 SQL target differs from frozen case')
        self.database = self.DATABASE(database)
        self.lifecycle = self.worker = self.launch = self.observer = None
        self.setup = self.http('setup', timeout=600)

    def validate_observer(self, http, secrets):
        observer = self.config['observerMcp']
        require(set(observer) == {'endpoint', 'credentialKind', 'credentialEnv', 'workflowView'} and
                all(secrets) and secrets[0] != secrets[1] and os.environ.get(observer['credentialEnv']) and
                os.environ.get(observer['credentialEnv']) != secrets[0],
                'P10 observer cannot use privileged setup credentials')
        require(observer['endpoint'].rstrip('/') == http['endpoint'].rstrip('/')+'/mcp',
                'P10 observer MCP origin differs from fixture product origin')

    def phase_key(self, name):
        return canonical_digest({'producer': self.PROFILE, 'attempt': self.key, 'phase': name})

    def phase(self, name):
        return self.durable.state.get('scenarioOperations', {}).get(self.phase_key(name))

    def retain(self, name, value):
        self.durable._require_live_lease()
        records = self.durable.state.setdefault('scenarioOperations', {})
        key = self.phase_key(name)
        require(key not in records, 'P10 phase already retained; reconcile without replay')
        record = {'producer': self.PROFILE, 'identity': identity(self.context),
                  'candidateKey': self.campaign['componentBindingDigest'], 'phase': name, 'value': copy.deepcopy(value)}
        records[key] = record
        self.durable._persist()
        return {'key': key, 'sha256': canonical_digest(record)}

    def value(self, name):
        record = self.phase(name)
        return None if record is None else copy.deepcopy(record['value'])

    def http(self, role, *, timeout=300):
        config = self.config['http']
        return ScenarioHttp(durable=self.durable, context=self.context, endpoint=config['endpoint'],
            credential_env=config['setupCredentialEnv' if role.startswith('setup') else 'observerCredentialEnv'],
            credential_kind=config['credentialKind'], attempt_id=self.attempt_id+'-'+role, timeout=timeout)

    def observe_deployment(self):
        """Independently bind actual product images and Redis before fixture effects."""
        config = self.config['deployment']
        require(set(config) == {'clusterArn', 'taskArns', 'containerName', 'imageUri', 'redisReplicationGroupId'} and
                isinstance(config['taskArns'], list) and 1 <= len(config['taskArns']) <= 4,
                'P10 ECS target lacks exact IaC product inventory')
        region = config['clusterArn'].split(':')[3]
        client = sdk('ecs', region)
        native = client.describe_tasks(cluster=config['clusterArn'], tasks=config['taskArns'])
        lock = load_json(self.bundle/'qualification-lock.json')
        require(gate.component_binding(lock) == self.campaign['componentBindingDigest'], 'P10 lock binding changed')
        artifacts = lock['components']['honua-server']['artifacts']
        images = [row for row in artifacts if row['kind'] == 'image']
        allowed = {digest for image in images for digest in [image.get('digest'), *image.get('platformDigests', {}).values()] if digest}
        require(config['imageUri'].rsplit('@', 1)[-1] in allowed, 'P10 product image is outside frozen candidate')
        definitions = []
        for task in native.get('tasks', []):
            require(task['taskArn'] in config['taskArns'] and task['clusterArn'] == config['clusterArn'],
                    'P10 native product task differs from reviewed inventory')
            containers = [row for row in task.get('containers', []) if row.get('name') == config['containerName']]
            require(len(containers) == 1 and containers[0].get('imageDigest') in allowed,
                    'P10 running product image differs from frozen candidate')
            definition = client.describe_task_definition(taskDefinition=task['taskDefinitionArn'])['taskDefinition']
            configured = [row for row in definition['containerDefinitions'] if row['name'] == config['containerName']]
            require(len(configured) == 1 and configured[0]['image'] == config['imageUri'] and
                    any('redis' in row.get('name', '').lower() and row.get('valueFrom') for row in configured[0].get('secrets', [])),
                    'P10 target lacks immutable candidate image and secret-bound Redis connection')
            # Do not retain plaintext environment values or credentials.
            definitions.append({'taskDefinitionArn': definition['taskDefinitionArn'], 'image': configured[0]['image'],
                'redisSecretNames': [row['name'] for row in configured[0].get('secrets', []) if 'redis' in row['name'].lower()]})
        require({row['taskArn'] for row in native.get('tasks', [])} == set(config['taskArns']),
                'P10 native product task inventory is incomplete')
        deployed = {'schemaVersion': 1, 'target': self.case['target'],
            'componentBindingDigest': self.campaign['componentBindingDigest'], 'endpoint': self.config['http']['endpoint'],
            'observedAt': datetime.now(timezone.utc).isoformat(), 'nativeResponse': serializable(native)}
        from agent_e2e_deployment import identity as deployment_identity
        deployment_identity(deployed, self.campaign, self.case['target'])
        redis = sdk('elasticache', region).describe_replication_groups(ReplicationGroupId=config['redisReplicationGroupId'])
        groups = redis.get('ReplicationGroups', [])
        require(len(groups) == 1 and groups[0].get('ReplicationGroupId') == config['redisReplicationGroupId'] and
                groups[0].get('Status') == 'available' and groups[0].get('TransitEncryptionEnabled') is True,
                'P10 Redis-on deployment is not independently available with TLS')
        cache = successful(self.setup.json('observe-cache', 'GET', '/api/v1/admin/cache/status'))
        require(cache.get('isHealthy') is True and cache.get('isUsingFallback') is False,
                'P10 product cache is unavailable or using fallback')
        self.retain('deployment', {'deployment': deployed, 'definitions': definitions, 'redis': serializable(redis)})

    def open_worker(self):
        if self.lifecycle is not None:
            return
        scoped = [row for row in self.durable.state['ledger']['entries'] if row['state'] == 'reserved' and
                  all(row.get(key) == self.context[key] for key in ('campaignId', 'caseId', 'attempt', 'campaignMode'))]
        rows = {kind: [row for row in scoped if row['kind'] == kind] for kind in ('infrastructure', 'cleanup')}
        require(all(len(values) == 1 for values in rows.values()), 'P10 worker requires unique retained attempt liabilities')
        infra, cleanup = rows['infrastructure'][0], rows['cleanup'][0]
        require(infra['basisDigest'] == cleanup['basisDigest'], 'P10 worker liability bases differ')
        config = {**self.worker_config['lifecycle'], 'candidateKey': self.campaign['componentBindingDigest'],
            'attemptId': self.attempt_id, 'context': {key: self.context[key] for key in
                ('campaignId', 'caseId', 'attempt', 'campaignMode')},
            'transportDigest': canonical_digest(self.worker_config['transport']),
            'liability': {'basisDigest': infra['basisDigest'], 'maximumInfrastructureUsd': infra['amountUsd'],
                          'maximumCleanupUsd': cleanup['amountUsd']}}
        self.lifecycle = open_lifecycle(config)
        self.worker = Worker(sdk('sqs', config['region']), self.worker_config['transport'])
        self.reservation_ids = {'infrastructure': infra['id'], 'cleanup': cleanup['id']}
        self.launch = self.durable.state.get('workerLaunches', {}).get(self.attempt_id)

    def await_worker(self, *, stopped=False):
        for _ in range(90):
            self.durable.require_worker_launch(self.launch)
            observed = self.lifecycle.observe(self.launch)
            if (observed['taskStatus'] == 'STOPPED' and observed['stopRecorded']) if stopped else (
                    observed['taskStatus'] == 'RUNNING' and observed['executionStatus'] == 'RUNNING'):
                return observed
            if not stopped and observed['executionStatus'] != 'RUNNING':
                break
            time.sleep(1)
        raise PackError('P10 worker state remains unobserved; retain liability without restart')

    def prepare(self):
        require(self.phase('prepare-start') is None, 'P10 prepare already started; reconcile without repeating setup')
        self.retain('prepare-start', {'namespace': self.namespace, 'attemptId': self.attempt_id,
                                     'startedAt': datetime.now(timezone.utc).isoformat()})
        self.retain('overhead-admission', reserve_overhead(self.durable, self.context,
            self.config['accounting']['overheadCoverage']))
        self.observe_deployment()
        baseline = self.database.baseline()
        self.durable.retain_fixture_baseline(self.context, baseline)
        bindings = TownSetup(self.setup, self.corpus_path).prepare(variant=self.case['variant'],
            namespace=self.namespace, starting_state='published-town', connection_id=self.config['connectionId'])
        self.retain('town', bindings)
        observed_tables = self.database.observe_new_tables(baseline, bindings)
        self.retain('observed-source-owners', observed_tables)
        if any(table['owner'] != observed_tables['expectedCleanupRole'] for table in observed_tables['tables']):
            require(isinstance(self.config.get('ownershipTransfer'), dict),
                    'P10 app-owned imports require reviewed private ownership-transfer provisioner')
            from hosted_pilot.fixture_handoff import transfer_ownership
            receipt = transfer_ownership(observed_tables, durable=self.durable, context=self.context,
                configuration=self.config['ownershipTransfer'], timeout=300)
            require(receipt.get('schema') == 'honua.fixture-ownership-receipt/v1' and
                    receipt.get('status') == 'completed' and
                    receipt.get('ownershipRequestDigest') == canonical_digest(observed_tables) and
                    receipt.get('candidateKey') == self.campaign['componentBindingDigest'] and
                    receipt.get('context') == {key: self.context[key] for key in
                        ('campaignId', 'caseId', 'attempt', 'campaignMode')} and
                    receipt.get('database') == observed_tables['database'] and receipt.get('before') == observed_tables['tables'],
                    'P10 ownership handoff receipt differs from exact observed request')
            self.retain('ownership-transfer-receipt', receipt)
        ownership = self.database.ownership(baseline, bindings)
        self.durable.retain_fixture_ownership(self.context, ownership)
        self.observer = P10Observer(http=self.http('observer'), mcp=self.config['observerMcp'], retain=self.retain,
                                    binding=bindings['layers']['shelters'])
        source = self.observer.source('prepared-source')
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']
        gate.oracle_predicates.verify('source-content.unchanged', {'profile': 'honua.protected-content/v1',
            'before': [source['snapshot']], 'after': [source['snapshot']]}, expected['source-content.unchanged'])
        self.retain('source-before', source)
        return self.start_worker(public_target=self.config['http']['endpoint']+'; isolated published services: '+', '.join(
            binding['service'] for binding in bindings['layers'].values()))

    def start_worker(self, *, public_target):
        """Start one fresh worker and verify only its selected public inputs."""
        self.open_worker()
        definition = self.lifecycle.ecs.describe_task_definition(
            taskDefinition=self.lifecycle.config['taskDefinitionArn'])['taskDefinition']
        verify_worker_definition(definition, self.worker_config)
        self.retain('worker-definition', serializable(definition))
        require(self.launch is None, 'P10 worker already launched; observe without restarting')
        self.launch = self.lifecycle.start(self.durable, reservation_ids=self.reservation_ids)
        self.retain('worker-running', self.await_worker())
        bootstrap_id = canonical_digest({'attempt': self.key, 'operation': 'public-input-startup'})[7:]
        command = '/usr/local/bin/python -I /app/agent_e2e_public_entry.py'
        self.retain('public-startup-intent', {'actionId': bootstrap_id, 'command': command})
        self.durable.require_worker_launch(self.launch, launching=True)
        reply = self.worker.execute(command, bootstrap_id, timeout=60)
        self.retain('public-startup-reply', reply)
        require(reply['status'] == 'completed' and reply['exitCode'] == 0, 'P10 worker public input verification failed')
        observed = json.loads(reply['stdout'])
        require(observed['knowledgeBinding'] == self.campaign['knowledge'] and
                observed['publicBundleDigest'] == self.worker_config['publicBundleDigest'] and
                observed['documentationRoot'] == '/opt/honua-public/knowledge', 'P10 worker knowledge/bundle differs from campaign')
        index = [{key: row[key] if key != 'sha256' else 'sha256:'+row[key] for key in ('path', 'sha256')}
                 for row in selected_files(self.pack, self.context['caseId'], load_json(self.corpus_path))]
        require(observed['files'] == index, 'P10 worker public inputs differ from selected-case corpus')
        self.retain('public-inputs', observed)
        return {'workerKnowledgeManifest': self.knowledge_manifest, 'publicInputs': {
            'files': observed['files'], 'documentation': ['/opt/honua-public/knowledge/okf/domain-graph.v1.json'],
            'target': public_target,
            'credentialReferences': ['Worker MCP identity is configured; discover tools with mcp_list_tools.']}}

    def observer_for_recovery(self):
        # Per-call HTTP deadlines must not expire merely because the preceding
        # model turns used their legitimate share of the 900-second case limit.
        town = self.value('town')
        require(town is not None, 'P10 fixture setup is unresolved')
        self.observer = P10Observer(http=self.http('observer'), mcp=self.config['observerMcp'],
            retain=self.retain, binding=town['layers']['shelters'])
        return self.observer

    def action(self, payload, timeout):
        require(self.value('public-inputs') is not None, 'P10 worker inputs are not independently verified')
        self.open_worker()
        action, dispatch = payload['action'], payload['dispatch']
        self.retain('model-action-'+str(dispatch['ordinal']), {'action': action, 'dispatch': dispatch})
        observer = self.observer_for_recovery()
        before = observer.source('before-'+dispatch['actionId']) if observer.eligible(action) else None
        observed = self.execute_action(action, dispatch, timeout)
        self.retain('model-result-'+str(dispatch['ordinal']), observed)
        if before is not None:
            result = observer.replay(action, dispatch, before, observed)
            self.retain('query-ordinal-'+str(dispatch['ordinal']), result)
        return observed

    def execute_action(self, action, dispatch, timeout):
        if action['name'] == 'terminal':
            router = TerminalActions(durable=self.durable, lifecycle=self.lifecycle, launch=self.launch,
                worker=self.worker, retain=self.retain)
            observed = router.execute(self.context, action, dispatch, timeout=timeout)
        else:
            router = McpActions(durable=self.durable, lifecycle=self.lifecycle, launch=self.launch, worker=self.worker,
                evidence_factory=lambda action_id: open_evidence(self.worker_config['evidence'],
                    self.campaign['componentBindingDigest'], self.attempt_id, action_id))
            observed = router.execute(self.context, action, dispatch, timeout=max(1, min(120, int(timeout))))
        return observed

    def verify(self):
        observer = self.observer_for_recovery()
        after, before = observer.source('completed-source'), self.value('source-before')
        queries = [record for record in self.durable.state.get('scenarioOperations', {}).values() if
            record.get('producer') == self.PROFILE and record.get('identity') == identity(self.context) and
            record.get('phase', '').startswith('query-ordinal-')]
        latest = max(queries, key=lambda record: int(record['phase'].rsplit('-', 1)[1]))['value'] if queries else None
        preservation = {'profile': 'honua.protected-content/v1', 'before': [before['snapshot']], 'after': [after['snapshot']]}
        observations = {'filter.exact': None if latest is None or latest['projectionError'] else latest['result'],
                        'source-content.unchanged': preservation}
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']
        failed = []
        for assertion, observation in observations.items():
            try:
                gate.oracle_predicates.verify(assertion, observation, expected[assertion])
            except (ValueError, TypeError):
                failed.append(assertion)
        result = {'status': 'fail' if failed else 'pass', 'failedAssertions': failed,
                  'observations': observations, 'selectedActionId': None if latest is None else latest['actionId']}
        self.retain('verification', result)
        return {key: result[key] for key in ('status', 'failedAssertions')}

    def cleanup(self):
        """Goal cleanup, worker termination and monetary settlement stay distinct."""
        failures = []
        stop = None
        try:
            self.open_worker()
            if self.launch is not None:
                self.retain('worker-stop-request', self.lifecycle.stop(self.durable, self.launch))
                stop = self.await_worker(stopped=True)
                self.retain('worker-stopped', stop)
            elif self.phase('worker-running') is not None:
                raise PackError('P10 launched worker intent is missing')
        except Exception as exc:
            failures.append({'component': 'worker-stop', 'errorType': type(exc).__name__})
        sql = None
        try:
            self.prepare_cleanup_ownership()
            ownership = self.cleanup_ownership()
            require(ownership is not None, 'P10 exact fixture ownership is unresolved')
            sql = self.cleanup_fixture(ownership)
            self.retain('sql-cleanup', sql)
        except Exception as exc:
            failures.append({'component': 'sql-cleanup', 'errorType': type(exc).__name__})
        cleanup = {'status': 'pass' if not failures and sql is not None and stop is not None else 'blocked',
                   'worker': stop, 'fixtureSql': sql, 'failures': failures,
                   'completedAt': datetime.now(timezone.utc).isoformat()}
        self.retain('cleanup', cleanup)
        if cleanup['status'] != 'pass':
            return cleanup
        task = self.lifecycle._task(stop['taskArn'])
        definition = self.value('worker-definition')
        self.retain('native-stopped-task', serializable(task))
        compute = fargate_compute(task, definition, self.price_receipt)
        self.retain('official-price-receipt', self.price_receipt)
        self.retain('native-compute-cost', compute)
        settlement = measured_settlement(self.durable, self.context, compute,
            self.config['accounting']['overheadCoverage'], retain=self.retain,
            recover=self.context.get('importCleanupPass', 1) > 1)
        return {**cleanup, 'accountingStatus': 'settled', 'observedInfrastructureUsd': settlement['infrastructureUsd'],
                'observedCleanupUsd': settlement['cleanupUsd']}

    def prepare_cleanup_ownership(self):
        """Case adapters may adopt final native outputs only after worker stop."""
        return None

    def cleanup_ownership(self):
        return self.durable.state.get('fixtureOwnership', {}).get(self.key)

    def cleanup_fixture(self, ownership):
        self.setup = self.http('setup-cleanup', timeout=300)
        def invalidate(services):
            answers = []
            for index, service in enumerate(services):
                answers.append(self.setup.json('invalidate-owned-'+str(index), 'POST', '/api/v1/admin/cache/invalidate',
                    {'scope': 'service', 'serviceId': service}))
                value = successful(answers[-1])
                require(value.get('success') is True, 'P10 exact service cache invalidation failed')
            self.retain('cache-invalidations', answers)
        return self.database.cleanup(ownership,
            authorize=lambda record: self.durable.require_fixture_ownership(self.context, record),
            retain=lambda kind, value: self.durable.retain_fixture_cleanup(self.context, kind, value), invalidate=invalidate)

    def export_attempt(self, payload):
        result = payload['controllerResult']
        self.retain('controller-result', result)
        ledger = [row for row in self.durable.state['ledger']['entries'] if
                  all(row.get(key) == self.context[key] for key in ('campaignId', 'caseId', 'attempt'))]
        require(ledger and all(row['state'] == 'settled' for row in ledger),
                'P10 native costs remain unknown; original result/evidence/liabilities retained, complete receipt unavailable')
        artifacts = {}
        prefix = self.PROFILE+'/'+self.key[7:]+'/'
        def evidence(name, value):
            raw = (json.dumps(value, sort_keys=True, allow_nan=False)+'\n').encode()
            path = prefix+name+'.json'
            artifacts[path] = base64.b64encode(raw).decode()
            return {'path': path, 'sha256': digest_bytes(raw)}
        rows = self.durable.state['journals'][self.key]
        transcript = evidence('transcript', {**identity(self.context), 'modelId': self.campaign['modelId'],
            'returnedModelId': self.campaign['returnedModelId'], 'turns': [row for row in rows if row['event'] == 'model'],
            'actions': [row for row in rows if row['event'] == 'action']})
        cleanup_value = self.value('cleanup')
        cleanup_ref = evidence('cleanup', {'resourceIds': cleanup_value['fixtureSql']['resourceIds']}) if (
            result['cleanup']['status'] == 'pass' and cleanup_value['fixtureSql'] is not None) else evidence('cleanup', cleanup_value)
        evidence('fixture-baseline', self.durable.state.get('fixtureBaselines', {}).get(self.key))
        evidence('fixture-ownership', self.cleanup_ownership())
        evidence('fixture-ownership-history', {key: value for key, value in self.durable.state.get('fixtureOwnership', {}).items()
            if key == self.key or key.startswith(self.key+'/import-cleanup-')})
        evidence('fixture-cleanup-stages', {key: value for key, value in self.durable.state.get('fixtureCleanup', {}).items()
                                          if key.startswith(self.key+'/')})
        oracle = None
        verification = self.value('verification')
        if verification is not None:
            assertions = [{'id': key, 'status': 'fail' if key in verification['failedAssertions'] else 'pass',
                           'evidence': evidence('assertion-'+key, value)} for key, value in verification['observations'].items()]
            report = {**identity(self.context), 'componentBindingDigest': self.campaign['componentBindingDigest'],
                'producer': 'independent-oracle', 'assertions': assertions}
            declarations = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']].get('nativeBindings', {})
            if declarations and result['status'] == 'pass':
                from agent_e2e_native_bindings import export as export_bindings
                report['nativeBindings'] = export_bindings(self.durable, {**self.campaign, **identity(self.context),
                    'startedAt': self.value('prepare-start')['startedAt'],
                    'completedAt': self.value('cleanup')['completedAt']}, declarations, evidence)
            if result['status'] == 'pass':
                oracle = evidence('oracle', report)
            else:
                # Failed or incomplete native observations remain original
                # evidence, never the passing oracle field of a failure receipt.
                reports = {key: value for key, value in self.durable.state.get('nativeBindings', {}).items()
                           if key.startswith(self.key+'/')}
                evidence('failed-native-bindings', reports)
                evidence('failed-oracle', report)
        worker_knowledge = evidence('worker-knowledge', self.knowledge_manifest)
        interfaces = []
        for record in self.durable.state.get('scenarioOperations', {}).values():
            if record.get('candidateKey') == self.campaign['componentBindingDigest'] and record.get('attemptId', '').startswith(self.attempt_id+'-'):
                evidence('http-'+canonical_digest(record)[7:], record)
            if record.get('producer') != self.PROFILE or record.get('identity') != identity(self.context):
                continue
            reference = evidence('phase-'+record['phase'], record)
            if record['phase'].startswith('model-result-') and record['value'].get('operationCategory') == 'discovery':
                interfaces.append({'kind': 'mcp', 'operation': 'tools/list', 'schema': reference})
        for action_id, record in self.durable.state.get('workerActions', {}).items():
            if record.get('attemptId') != self.attempt_id or record.get('candidateKey') != self.campaign['componentBindingDigest']:
                continue
            evidence('worker-action-'+action_id, record)
            if 'header' in record:
                reply = open_evidence(self.worker_config['evidence'], self.campaign['componentBindingDigest'],
                    self.attempt_id, action_id).recover(record['header'])
                require(canonical_digest(reply['reply']) == record['replyDigest'], 'P10 retained worker reply changed')
                evidence('worker-raw-reply-'+action_id, reply)
        failure = None if result['status'] == 'pass' else evidence('failure', {'controllerResult': result,
            'verification': verification, 'cleanup': self.value('cleanup')})
        costs = {kind: sum(Decimal(row['actualUsd']) for row in ledger if row['kind'] == kind)
                 for kind in ('model', 'infrastructure', 'cleanup')}
        receipt = {key: copy.deepcopy(self.campaign[key]) for key in ('campaignId', 'packDigest', 'corpusDigest',
            'qualificationLockDigest', 'componentBindingDigest', 'modelId', 'returnedModelId', 'controller', 'knowledge', 'campaignMode')}
        receipt.update({key: self.case[key] for key in ('scenarioId', 'variant', 'target', 'promptDigest')})
        receipt.update(schemaVersion=2, receiptSchema='agent-e2e-receipt.v2', **identity(self.context), mode='genuine-model',
            startedAt=self.value('prepare-start')['startedAt'], completedAt=self.value('cleanup')['completedAt'], status=result['status'],
            failureAttribution=result['failureAttribution'], namespace=self.namespace,
            costs={**{kind+'Usd': float(amount) for kind, amount in costs.items()},
                'cleanupReservedUsd': float(sum(Decimal(row['amountUsd']) for row in ledger if row['kind'] == 'cleanup'))},
            usage=result['usage'], cleanup={'status': result['cleanup']['status'], 'evidence': cleanup_ref},
            oracle=oracle, transcript=transcript, failureEvidence=failure, workerKnowledge=worker_knowledge,
            runtimeInterfaces=evidence('runtime-interfaces', {**{key: self.context[key] for key in ('caseId', 'attempt')},
                'interfaces': interfaces}))
        exported = {'protocol': PROTOCOL, 'receipt': receipt, 'evidence': artifacts}
        path = Path(payload['exportPath'])
        require(path.parent.resolve() == (self.bundle/'backend-exports').resolve() and not path.exists(),
                'P10 export path differs from controller-selected destination')
        raw = (json.dumps(exported, sort_keys=True, allow_nan=False)+'\n').encode()
        require(len(raw) <= 48*1024*1024, 'P10 private evidence export exceeds bound')
        with path.open('xb') as stream:
            stream.write(raw)
        return {'export': {'path': str(path), 'sha256': digest_bytes(raw)}}


def handle(operation, payload, *, durable, campaign, bundle, configuration, timeout):
    require(operation != 'export_campaign', 'P10 development backend cannot produce complete 252-case campaign evidence')
    context = ({**campaign, **identity(payload)} if operation == 'export_attempt' else payload)
    key = (id(durable), canonical_digest(identity(context)))
    if key not in SESSIONS:
        SESSIONS[key] = Session(durable=durable, campaign=campaign, bundle=bundle,
                                configuration=configuration, context=context)
    session = SESSIONS[key]
    if operation == 'prepare':
        return session.prepare()
    if operation == 'agent_action':
        return session.action(payload, timeout)
    if operation == 'verify':
        return session.verify()
    if operation == 'cleanup':
        return session.cleanup()
    if operation == 'export_attempt':
        return session.export_attempt(payload)
    raise PackError('unsupported P10 backend operation')
