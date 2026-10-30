"""P10 A/B on an already running local-docker candidate. Not qualification.

The product container is observed, not created. The worker container is one
reviewed image started only after its claim is durable and town seeding returns.
Variant C, other targets, and a passing receipt without oracle evidence are refused.
"""
from __future__ import annotations

import copy
import re

import agent_e2e_gate as gate
from agent_e2e_deployment import identity as deployment_identity
from agent_e2e_local_docker_lifecycle import LocalDockerLifecycle
from agent_e2e_pack import PackError, canonical_digest, load_json

CASE_PATTERN = re.compile(r'P10:[AB]:local-docker')


def require(value, reason):
    if not value:
        raise PackError(reason)


def case_identity(context):
    return {key: context[key] for key in ('campaignId', 'caseId', 'attempt')}


class Session:
    def __init__(self, *, durable, campaign, bundle, configuration, context, runner=None):
        require(campaign.get('campaignMode') == 'development' and CASE_PATTERN.fullmatch(context['caseId']),
                'local-docker P10 implements only explicit A/B development')
        require(isinstance(configuration, dict) and context['caseId'] in configuration.get('cases', {}),
                'missing reviewed local-docker P10 configuration')
        self.durable = durable
        self.campaign = copy.deepcopy(campaign)
        self.bundle = bundle
        self.context = copy.deepcopy(context)
        self.config = copy.deepcopy(configuration['cases'][context['caseId']])
        require(set(self.config) >= {'deployment', 'worker'}, 'local-docker P10 lacks deployment and worker bindings')
        self.deployment_id = deployment_identity(self.config['deployment'], campaign, 'local-docker')
        worker = self.config['worker']
        require(worker['context'] == case_identity(context) | {'campaignMode': 'development'},
                'local-docker worker context differs from the case')
        self.lifecycle = LocalDockerLifecycle(worker, **({} if runner is None else {'runner': runner}))
        self.key = canonical_digest(case_identity(context))

    def retain(self, phase, value):
        self.durable._require_live_lease()
        records = self.durable.state.setdefault('scenarioOperations', {})
        record = {'producer': 'p10-local-docker', 'identity': case_identity(self.context), 'phase': phase, 'value': value}
        key = canonical_digest({'phase': phase, **record['identity']})
        require(key not in records, 'local-docker P10 phase already retained')
        records[key] = record
        self.durable._persist()
        return record

    def value(self, phase):
        for record in self.durable.state.get('scenarioOperations', {}).values():
            if (record.get('producer') == 'p10-local-docker' and record.get('identity') == case_identity(self.context) and
                    record.get('phase') == phase):
                return record['value']
        return None

    def prepare(self, *, seed):
        """Claim, seed the published town, then start the worker once."""
        require(callable(seed), 'local-docker P10 town seed is not bound')
        self.retain('deployment', {'deploymentId': self.deployment_id})
        self.lifecycle.claim(self.durable)
        try:
            seeded = seed()
        except Exception:
            raise
        require(isinstance(seeded, dict) and seeded.get('startingState') == 'published-town',
                'local-docker P10 requires an observed published town')
        self.retain('town', seeded)
        launch = self.lifecycle.start(self.durable)
        self.retain('worker-running', {'containerId': launch['containerId'], 'image': launch['image']})
        return {'qualification': False, 'deploymentId': self.deployment_id, 'containerId': launch['containerId']}

    def verify(self):
        require(self.value('town') is not None, 'local-docker P10 town was not seeded')
        before = self.value('source-before')
        require(isinstance(before, dict) and 'snapshot' in before, 'local-docker P10 has no independent source snapshot')
        queries = [record['value'] for record in self.durable.state.get('scenarioOperations', {}).values()
                   if record.get('producer') == 'p10-local-docker' and record.get('identity') == case_identity(self.context) and
                   str(record.get('phase', '')).startswith('query-ordinal-')]
        latest = max(queries, key=lambda item: item['ordinal']) if queries else None
        after = self.value('source-after') or before
        observations = {'filter.exact': None if latest is None else latest.get('result'),
                        'source-content.unchanged': {'profile': 'honua.protected-content/v1',
                                                     'before': [before['snapshot']], 'after': [after['snapshot']]}}
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']
        failed = []
        for assertion, observation in observations.items():
            try:
                gate.oracle_predicates.verify(assertion, observation, expected[assertion])
            except (ValueError, TypeError):
                failed.append(assertion)
        result = {'status': 'fail' if failed else 'pass', 'failedAssertions': failed, 'qualification': False}
        self.retain('verification', result)
        return {key: result[key] for key in ('status', 'failedAssertions', 'qualification')}

    def cleanup(self):
        failures = []
        stopped = None
        try:
            stopped = self.lifecycle.stop(self.durable)
            self.retain('worker-stopped', stopped)
        except Exception as exc:
            failures.append({'component': 'worker-stop', 'errorType': type(exc).__name__})
        if self.value('sql-cleanup') is None:
            failures.append({'component': 'sql-cleanup', 'errorType': 'PackError'})
        status = 'pass' if not failures and stopped is not None else 'blocked'
        cleanup = {'status': status, 'worker': stopped, 'failures': failures, 'qualification': False}
        self.retain('cleanup', cleanup)
        return cleanup


def handle(operation, payload, *, durable, campaign, bundle, configuration, timeout, runner=None, seed=None):
    del timeout
    require(operation != 'export_campaign', 'local-docker P10 cannot export a 252-case campaign')
    context = payload if operation != 'export_attempt' else {**campaign, **case_identity(payload)}
    session = Session(durable=durable, campaign=campaign, bundle=bundle, configuration=configuration,
                      context=context, runner=runner)
    if operation == 'prepare':
        require(seed is not None, 'local-docker P10 prepare requires the published-town seeder')
        return session.prepare(seed=seed)
    if operation == 'verify':
        return session.verify()
    if operation == 'cleanup':
        return session.cleanup()
    raise PackError('unsupported local-docker P10 operation')
