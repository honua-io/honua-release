"""Native P14 publication development producer and paid prefreeze fixture setup.

Preparation creates only P14's saved-map starting state. A checked private
workflow must retain its originals and review the actual IDs into source-frozen
expectations before dispatching the campaign. Runtime actions come only from the
model; a real distinct human approves. Unsupported custody/isolation observers
remain explicit failures, so this producer cannot qualify the complete campaign.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import os
import re

from agent_e2e_budget import validate as validate_budget
from agent_e2e_native_bindings import resolve
from agent_e2e_mcp import validate_agent_context
from agent_e2e_native_costs import reserve_overhead
from agent_e2e_p10_backend import Session as WorkerSession, identity, sdk
from agent_e2e_pack import PackError, canonical_digest, load_json
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_studio_database import StudioFixtureDatabase
from agent_e2e_studio_fixture import StudioSetup
from agent_e2e_studio_observer import StudioPublicationObserver, guid, route_path
from agent_e2e_studio_tenant_observer import (StudioTenantObserver, validate_configuration as validate_tenant,
    verify_owner_injection)
import agent_e2e_gate as gate

SESSIONS = {}


def require(condition, message):
    if not condition:
        raise PackError(message)


class BootstrapHttp(ScenarioHttp):
    """Native fixture HTTP under original candidate bootstrap liabilities."""
    def __init__(self, *, operation, **kwargs):
        self.operation = copy.deepcopy(operation)
        super().__init__(**kwargs)

    def authorize(self):
        self.durable._require_live_lease()
        ledger = self.durable.state['ledger']
        validate_budget(ledger)
        require(ledger['candidateKey'] == self.identity['candidateKey'], 'Studio bootstrap candidate changed')
        selected = []
        for kind in ('infrastructure', 'cleanup'):
            rows = [row for row in ledger.get('operations', []) if row['id'] == self.operation['reservationIds'][kind]]
            require(len(rows) == 1 and rows[0]['operationId'] == self.operation['operationId'] and
                    rows[0]['purpose'] == 'bootstrap' and rows[0]['kind'] == kind and
                    rows[0]['state'] == 'reserved' and rows[0]['basisDigest'] == self.operation['liabilityBasisDigest'],
                    'Studio prefreeze setup lacks original prepaid bootstrap and cleanup liabilities')
            selected.append(rows[0]['id'])
        require(len(set(selected)) == 2, 'Studio bootstrap liabilities are not independent reservations')


def prepare_initial_fixture(*, durable, configuration):
    """Called only by the checked private prefreeze workflow, never model tools.

    Returns an immutable state reference plus actual saved IDs for source review.
    A retained start/baseline prohibits retry after a lost response. All original
    charges remain liabilities; this function never substitutes a zero cost.
    """
    require(isinstance(configuration, dict) and set(configuration) ==
            {'candidateKey', 'operation', 'database', 'http', 'itemId', 'namespace', 'ownerPrincipalId', 'envelope'},
            'incomplete reviewed Studio prefreeze configuration')
    candidate, operation = configuration['candidateKey'], configuration['operation']
    require(candidate == durable.store.candidate and configuration['database']['candidateKey'] == candidate and
            set(operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            set(operation['reservationIds']) == {'infrastructure', 'cleanup'} and
            isinstance(operation['operationId'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', operation['operationId']),
            'Studio bootstrap operation differs from candidate-bound original liabilities')
    item_id = guid(configuration['itemId'])
    fixture_key = canonical_digest({'candidateKey': candidate, 'operationId': operation['operationId'], 'itemId': item_id})
    http = configuration['http']
    require(set(http) == {'endpoint', 'setupCredentialEnv', 'setupCredentialKind', 'requesterCredentialEnv', 'requesterCredentialKind', 'requesterAgentContext'},
            'Studio prefreeze setup requires separate native setup/task HTTP references')
    context = {'componentBindingDigest': candidate}
    def transport(role):
        return BootstrapHttp(operation=operation, durable=durable, context=context, endpoint=http['endpoint'],
            credential_env=http[role+'CredentialEnv'], credential_kind=http[role+'CredentialKind'],
            attempt_id='studio-bootstrap-'+fixture_key[7:31]+'-'+role, timeout=600,
            **({'agent_context': http['requesterAgentContext']} if role == 'requester' else {}))
    setup, requester = transport('setup'), transport('requester')
    require(requester.client.agent_context is not None, 'Studio prefreeze task reads require an actual native workspace and turn')
    setup.authorize()
    stages = durable.state.setdefault('studioBootstrapOperations', {})
    require(fixture_key+'/start' not in stages and fixture_key not in durable.state.get('studioBootstrapFixtures', {}),
            'Studio prefreeze setup already started; reconcile original observations without replay')
    def retain(stage, value):
        setup.authorize()
        key = fixture_key+'/'+stage
        require(key not in stages, 'Studio bootstrap evidence already retained')
        stages[key] = copy.deepcopy(value)
        durable._persist()
    retain('start', {'candidateKey': candidate, 'operation': operation, 'configurationDigest': canonical_digest(configuration),
                     'startedAt': datetime.now(timezone.utc).isoformat()})
    database = StudioFixtureDatabase(configuration['database'])
    baseline = database.baseline()
    retain('baseline', baseline)
    fixture = StudioSetup(setup=setup, requester=requester).prepare_saved_map(namespace=configuration['namespace'],
        owner_id=configuration['ownerPrincipalId'], envelope=configuration['envelope'], item_id=item_id, baseline=baseline)
    retain('fixture', fixture)
    ownership = database.ownership(baseline, fixture)
    retain('ownership', ownership)
    originals = {key: copy.deepcopy(record) for key, record in durable.state.get('scenarioOperations', {}).items()
                 if record.get('candidateKey') == candidate and record.get('attemptId', '').startswith('studio-bootstrap-'+fixture_key[7:31]+'-')}
    value = {'schema': 'honua.agent-e2e.studio-bootstrap/v1', 'candidateKey': candidate, 'operation': operation,
        'configurationDigest': canonical_digest(configuration), 'baseline': baseline, 'fixture': fixture, 'ownership': ownership,
        'nativeOriginals': originals, 'completedAt': datetime.now(timezone.utc).isoformat(), 'qualification': False}
    setup.authorize()
    durable.state.setdefault('studioBootstrapFixtures', {})[fixture_key] = copy.deepcopy(value)
    durable._persist()
    return {'key': fixture_key, 'sha256': canonical_digest(value), 'fixture': copy.deepcopy(fixture), 'qualification': False}


class Session(WorkerSession):
    PROFILE = 'studio-publication-native'
    CASE_PATTERN = r'P14:[AB]:aws-ecs-redis-on'
    CONFIG_FIELDS = {'http', 'database', 'deployment', 'attempts', 'accounting', 'studio'}
    OPTIONAL_FIELDS = set()
    DATABASE = StudioFixtureDatabase

    def validate_observer(self, http, secrets):
        studio = self.config['studio']
        require({'bootstrapFixture', 'route', 'requesterCredentialEnv', 'requesterCredentialKind', 'requesterAgentContext'} <= set(studio) <=
                {'bootstrapFixture', 'route', 'requesterCredentialEnv', 'requesterCredentialKind', 'requesterAgentContext', 'tenantIsolation', 'custody'} and
                set(studio['bootstrapFixture']) == {'key', 'sha256'} and
                studio['requesterCredentialKind'] in ('api-key', 'bearer') and all(secrets) and secrets[0] != secrets[1] and
                os.environ.get(studio['requesterCredentialEnv']) and os.environ[studio['requesterCredentialEnv']] != secrets[0],
                'P14 requires distinct setup and actual task credentials plus retained native bootstrap fixture')
        if 'tenantIsolation' in studio:
            config = validate_tenant(studio['tenantIsolation'])
            owner = os.environ.get(config['ownerCredentialEnv'])
            require(owner and owner not in (*secrets, os.environ[studio['requesterCredentialEnv']]),
                    'private tenant human owner must have separate controller-only custody')
        if 'custody' in studio:
            from agent_e2e_studio_custody_observer import validate
            validate(studio['custody'])
        route_path(studio['route'])
        require(validate_agent_context(studio['requesterAgentContext']) is not None,
                'P14 task reads require an actual native workspace and turn')

    def deployment_http(self):
        # Ordinary target reads continue with the independent active observer;
        # the retired setup credential is used only for its refusal probe.
        return self.http('observer')

    def custody_observer(self):
        from agent_e2e_studio_custody_observer import StudioCustodyObserver
        region = self.config['deployment']['clusterArn'].split(':')[3]
        return StudioCustodyObserver(self, **{name: sdk(service, region) for name, service in
            (('ecs', 'ecs'), ('s3', 's3'), ('states', 'stepfunctions'), ('iam', 'iam'))})

    def start_worker(self, *, public_target):
        result = super().start_worker(public_target=public_target)
        if self.config['studio'].get('custody') is not None:
            custody = self.custody_observer()
            revoked, original = custody.revoked(step='custody-started')
            replaced = custody.worker(step='custody-started', original=original)
            expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']
            for assertion, actual in (('custody.bootstrap-revoked', revoked), ('custody.worker-replaced', replaced)):
                gate.oracle_predicates.verify(assertion, actual, expected[assertion])
        return result

    def requester(self, *, role, timeout=300):
        studio = self.config['studio']
        return ScenarioHttp(durable=self.durable, context=self.context, endpoint=self.config['http']['endpoint'],
            credential_env=studio['requesterCredentialEnv'], credential_kind=studio['requesterCredentialKind'],
            attempt_id=self.attempt_id+'-'+role, timeout=timeout, agent_context=studio['requesterAgentContext'])

    def initial_fixture(self):
        reference = self.config['studio']['bootstrapFixture']
        record = self.durable.state.get('studioBootstrapFixtures', {}).get(reference['key'])
        require(isinstance(record, dict) and canonical_digest(record) == reference['sha256'] and
                record.get('schema') == 'honua.agent-e2e.studio-bootstrap/v1' and
                record['candidateKey'] == self.campaign['componentBindingDigest'] and
                record['ownership']['baseline'] == record['baseline'],
                'P14 initial fixture is not its exact retained prefreeze native bootstrap')
        # Bootstrap records and original HTTP pairs are immutable candidate state;
        # no caller can turn a config declaration or model response into custody.
        for key, original in record['nativeOriginals'].items():
            require(self.durable.state.get('scenarioOperations', {}).get(key) == original,
                    'P14 original bootstrap HTTP custody is missing or changed')
        return record

    def observer_for_recovery(self):
        binding = self.value('studio-binding')
        require(binding is not None, 'P14 initial saved version is unresolved')
        anonymous = ScenarioHttp(durable=self.durable, context=self.context, endpoint=self.config['http']['endpoint'],
            credential_env=None, credential_kind='anonymous', attempt_id=self.attempt_id+'-anonymous')
        self.observer = StudioPublicationObserver(observer=self.http('observer'), anonymous=anonymous,
            binding=binding, retain=self.retain)
        return self.observer

    def tenant_proof(self, *, step, observer, previous=None):
        config = self.config['studio'].get('tenantIsolation')
        if config is None:
            return None
        validate_tenant(config)
        region = self.config['deployment']['clusterArn'].split(':')[3]
        observer.observer.authorize()
        credential = verify_owner_injection(config, region=region, ecs=sdk('ecs', region))
        self.retain(step+'-credential', credential)
        protected = self.database.observe_private_tenant_source(self.initial_fixture()['baseline'],
            item_id=config['itemId'], version_id=config['versionId'], authorize=observer.observer.authorize)
        self.retain(step+'-source', protected)
        owner = ScenarioHttp(durable=self.durable, context=self.context, endpoint=self.config['http']['endpoint'],
            credential_env=config['ownerCredentialEnv'], credential_kind='bearer',
            attempt_id=self.attempt_id+'-private-owner', timeout=300)
        tenant = StudioTenantObserver(owner=owner, outsider=observer.observer,
            task=self.requester(role='tenant-task'), source=protected['source'],
            public_binding=observer.binding, retain=self.retain)
        return tenant.observe(step=step, previous=previous)

    def prepare(self):
        require(self.phase('prepare-start') is None, 'P14 prepare already started; reconcile without replay')
        self.retain('prepare-start', {'namespace': self.namespace, 'attemptId': self.attempt_id,
            'startedAt': datetime.now(timezone.utc).isoformat()})
        self.retain('overhead-admission', reserve_overhead(self.durable, self.context,
            self.config['accounting']['overheadCoverage']))
        self.observe_deployment()
        initial = self.initial_fixture()
        fixture = initial['fixture']
        # Reobserve actual SQL before adopting the independently retained original
        # absence and exact owned rows into this attempt's cleanup responsibility.
        ownership = self.database.ownership(initial['baseline'], fixture)
        require(ownership == initial['ownership'], 'P14 initial fixture changed before task worker launch')
        self.namespace = fixture['namespace']
        self.durable.retain_fixture_baseline(self.context, initial['baseline'])
        self.durable.retain_fixture_ownership(self.context, ownership)
        self.retain('initial-fixture', {'reference': self.config['studio']['bootstrapFixture'], 'fixture': fixture})
        binding = {key: fixture[key] for key in ('itemId', 'versionId', 'contentHash')}
        binding.update(route=self.config['studio']['route'], visibility='public',
            requesterPrincipalId=fixture['ownerPrincipalId'], notBefore=self.value('prepare-start')['startedAt'])
        expected_case = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]
        declarations = expected_case.get('nativeBindings', {})
        require(len(declarations) == 1 and next(iter(declarations.values()))['relationships'] ==
                {key: value for key, value in binding.items() if key != 'notBefore'},
                'P14 frozen relationships do not match actual prefreeze initial fixture')
        self.retain('studio-binding', binding)
        observer = self.observer_for_recovery()
        route_answers = [observer.observer.json('prepared-owner-route', 'GET', observer.path),
                         observer.anonymous.json('prepared-anonymous-route', 'GET', observer.path)]
        require(all(answer['observation']['httpStatus'] == 404 for answer in route_answers),
                'P14 publication route already belongs to existing content')
        self.retain('route-before', [answer['reference'] for answer in route_answers])
        source = observer.version('prepared-saved-version')
        gate.oracle_predicates.verify('source-content.unchanged', {'profile': 'honua.protected-content/v1',
            'before': [source['snapshot']], 'after': [source['snapshot']]}, expected_case['assertions']['source-content.unchanged'])
        self.retain('source-before', source)
        # The real task credential must independently reopen the exact saved
        # version; the setup principal never becomes the task worker credential.
        from agent_e2e_scenario_operations import data
        task_read = self.requester(role='task-read').json('task-saved-version', 'GET', observer.version_path())
        require(data(task_read) == fixture['initialVersion'], 'P14 task identity cannot reopen exact saved starting version')
        self.retain('task-initial-read', task_read['reference'])
        tenant = self.tenant_proof(step='tenant-before', observer=observer)
        if tenant is not None:
            gate.oracle_predicates.verify('tenant.denied', tenant['observation'], expected_case['assertions']['tenant.denied'])
        if self.config['studio'].get('custody') is not None:
            revoked, _ = self.custody_observer().revoked(step='custody-before')
            gate.oracle_predicates.verify('custody.bootstrap-revoked', revoked, expected_case['assertions']['custody.bootstrap-revoked'])
        return self.start_worker(public_target=self.config['http']['endpoint']+'; saved map '+fixture['itemId']+
            ', version '+fixture['versionId']+', content hash '+fixture['contentHash']+
            '; requested public route '+binding['route'])

    def action(self, payload, timeout):
        require(self.value('public-inputs') is not None, 'P14 worker public inputs are not verified')
        self.open_worker()
        action, dispatch = payload['action'], payload['dispatch']
        self.retain('model-action-'+str(dispatch['ordinal']), {'action': action, 'dispatch': dispatch})
        observed = self.execute_action(action, dispatch, timeout)
        self.retain('model-result-'+str(dispatch['ordinal']), observed)
        observer = self.observer_for_recovery()
        if observer.eligible(action):
            selected = observer.selected(action, dispatch, observed)
            self.retain('publication-ordinal-'+str(dispatch['ordinal']), selected)
        return observed

    def verify(self):
        observer = self.observer_for_recovery()
        after, before = observer.version('completed-saved-version'), self.value('source-before')
        observations = {assertion: None for assertion in self.case['assertions']}
        observations['source-content.unchanged'] = {'profile': 'honua.protected-content/v1',
            'before': [before['snapshot']], 'after': [after['snapshot']]}
        selections = [row['value'] for row in self.durable.state.get('scenarioOperations', {}).values()
            if row.get('producer') == self.PROFILE and row.get('identity') == identity(self.context) and
               row.get('phase', '').startswith('publication-ordinal-')]
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]
        if self.config['studio'].get('custody') is not None:
            try:
                require(self.value('custody-before-revocation') is not None and self.value('custody-started-worker') is not None,
                        'original before-model setup and separate worker proof is missing')
                custody = self.custody_observer()
                revoked, original = custody.revoked(step='custody-after')
                replaced = custody.worker(step='custody-after', original=original)
                require(revoked == self.value('custody-before-revocation')['observation'] and
                        replaced == self.value('custody-started-worker')['observation'],
                        'original credential or worker changed during model execution')
                observations.update({'custody.bootstrap-revoked': revoked, 'custody.worker-replaced': replaced})
            except (PackError, ValueError, KeyError, TypeError):
                self.retain('custody-verification-refused', {'reasonCode': 'original-native-custody-proof-refused'})
        if self.config['studio'].get('tenantIsolation') is not None:
            try:
                prior = self.value('tenant-before-observation')
                require(prior is not None, 'original before-worker tenant proof is missing')
                tenant = self.tenant_proof(step='tenant-after', observer=observer, previous=prior)
                observations['tenant.denied'] = tenant['observation']
            except PackError:
                self.retain('tenant-verification-refused', {'reasonCode': 'native-tenant-proof-refused'})
        declarations, reports = expected.get('nativeBindings', {}), {}
        completed = datetime.now(timezone.utc).isoformat()
        native_context = {**self.context, 'startedAt': self.value('prepare-start')['startedAt'], 'completedAt': completed}
        if len(selections) == 1:
            publication = observer.observe(selections[0], step='completed-publication')
            self.retain('publication-final', publication)
            for assertion in ('approval.separate', 'publication.public'):
                observations[assertion] = publication.get(assertion)
            name, declaration = next(iter(declarations.items()))
            reports[name] = observer.bind_native(publication, name=name, declaration=declaration, context=self.context,
                started_at=native_context['startedAt'], completed_at=completed)
        failed = []
        for assertion in self.case['assertions']:
            try:
                wanted = resolve(assertion, expected['assertions'][assertion], declarations, reports, native_context)
                gate.oracle_predicates.verify(assertion, observations[assertion], wanted)
            except (ValueError, TypeError, PackError):
                failed.append(assertion)
        result = {'status': 'fail' if failed else 'pass', 'failedAssertions': failed, 'observations': observations,
            'selectedActionId': selections[0]['actionId'] if len(selections) == 1 else None,
            'unsupportedAssertions': [name for name in self.case['assertions'] if name not in
                ({'approval.separate', 'publication.public', 'source-content.unchanged'} |
                 ({'tenant.denied'} if self.config['studio'].get('tenantIsolation') is not None else set()) |
                 ({'custody.bootstrap-revoked', 'custody.worker-replaced'} if self.config['studio'].get('custody') is not None else set()))]}
        self.retain('verification', result)
        return {key: result[key] for key in ('status', 'failedAssertions')}

    def cleanup_fixture(self, ownership):
        return self.database.cleanup(ownership,
            authorize=lambda record: self.durable.require_fixture_ownership(self.context, record),
            retain=lambda kind, value: self.durable.retain_fixture_cleanup(self.context, kind, value))


def handle(operation, payload, *, durable, campaign, bundle, configuration, timeout):
    require(operation != 'export_campaign', 'P14 development producer cannot produce complete 252-case campaign evidence')
    context = {**campaign, **identity(payload)} if operation == 'export_attempt' else payload
    key = (id(durable), canonical_digest(identity(context)))
    if key not in SESSIONS:
        SESSIONS[key] = Session(durable=durable, campaign=campaign, bundle=bundle, configuration=configuration, context=context)
    session = SESSIONS[key]
    if operation == 'prepare': return session.prepare()
    if operation == 'agent_action': return session.action(payload, timeout)
    if operation == 'verify': return session.verify()
    if operation == 'cleanup': return session.cleanup()
    if operation == 'export_attempt': return session.export_attempt(payload)
    raise PackError('unsupported P14 backend operation')
