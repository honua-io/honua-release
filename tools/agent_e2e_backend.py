"""Pinned in-process controller backend sharing the producer's fenced lease.

Preflight is deliberately local: parse the reviewed execution contract and quote
its maximum liability. It never imports backend code, calls a cloud SDK, opens a
database or sends HTTP. Real resource observations and effects occur in prepare,
after both infrastructure and cleanup are durably reserved. Qualification still
requires actual execution, independent oracles, accounting and the complete gate.
"""
from __future__ import annotations

import copy
import base64
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
from contextlib import contextmanager
import signal
import threading
import time

from agent_e2e_budget import money, validate as validate_budget
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, portable_path

PROTOCOL = 'honua.agent-e2e.backend/v1'
PROBE_SCHEMA = 'honua.agent-e2e.infrastructure-probe/v1'
# Only these probes prove an infrastructure fault. A diagnosis label, an evidence
# hash, or any other document leaves the cause undetermined, which counts as fail.
PROBE_FIELDS = {
    'provider-5xx': {'httpStatus', 'providerRequestId'},
    'transport-error': {'errorClass'},
    'harness-crash': {'component', 'exitCode'},
    'cell-provisioning-failure': {'provisioningStatus', 'errorClass'},
}
PROBE_COMMON = {'schemaVersion', 'probeKind', 'observedBeforeModelActed', 'modelStateChanged',
                'actionsDispatched', 'independentOfModel', 'replay'}
TRANSPORT_ERRORS = {'dns', 'tls', 'connection-refused', 'connection-reset', 'timeout-before-request'}
PROBE_REPLAY = {
    'provider-5xx': {'independentOfModel', 'reproduced', 'httpStatus'},
    'transport-error': {'independentOfModel', 'reproduced', 'errorClass'},
    'harness-crash': {'independentOfModel', 'reproduced'},
    'cell-provisioning-failure': {'independentOfModel', 'reproduced', 'errorClass'},
}


def infrastructure_probe_proves(raw):
    """True only when the retained bytes are a replayed probe of an exogenous fault.

    The probe must have been observed before the model acted and must not follow a
    model state change. A timeout or 5xx after the model dispatched work does not qualify.
    """
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return False
    if not isinstance(document, dict):
        return False
    kind = document.get('probeKind')
    replay = document.get('replay')
    if (kind not in PROBE_FIELDS or set(document) != PROBE_COMMON | PROBE_FIELDS[kind]
            or document.get('schemaVersion') != PROBE_SCHEMA
            or document.get('observedBeforeModelActed') is not True
            or document.get('modelStateChanged') is not False
            or document.get('independentOfModel') is not True
            or type(document.get('actionsDispatched')) is not int or document['actionsDispatched'] != 0
            or not isinstance(replay, dict) or set(replay) != PROBE_REPLAY[kind]
            or replay.get('independentOfModel') is not True or replay.get('reproduced') is not True):
        return False
    if kind == 'provider-5xx':
        status = document['httpStatus']
        return (type(status) is int and 500 <= status <= 599 and replay.get('httpStatus') == status
                and isinstance(document['providerRequestId'], str)
                and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', document['providerRequestId']) is not None)
    if kind == 'transport-error':
        return document['errorClass'] in TRANSPORT_ERRORS and replay.get('errorClass') == document['errorClass']
    if kind == 'harness-crash':
        code = document['exitCode']
        return document['component'] in {'controller', 'backend', 'driver'} and type(code) is int and code != 0
    status = document['provisioningStatus']
    error = document['errorClass']
    return (status == 'failed' and isinstance(error, str) and re.fullmatch(r'[a-z0-9-]{1,64}', error) is not None
            and replay.get('errorClass') == error)


def require(value, reason):
    if not value:
        raise PackError(reason)


@contextmanager
def deadline(seconds):
    require(type(seconds) in (int, float) and 0 < seconds <= 7200 and
            threading.current_thread() is threading.main_thread(), 'backend requires bounded main-thread execution')
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(*_args):
        raise PackError('backend deadline exceeded; retain unknown effects and liability')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous_timer[0]) if previous_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0]-(time.monotonic()-started)), previous_timer[1])


class InProcessBackend:
    """The reviewed module supplies handle(...), receiving this exact lease.

    The entry module must have a resource-free import. handle's prepare branch
    validates native resources before starting any worker; it uses ScenarioHttp,
    WorkerLifecycle/McpActions and FixtureDatabase with the supplied durable
    instance. No operation may acquire a competing state lease.
    """
    def __init__(self, config, *, source_root=None):
        require(set(config) == {'kind', 'script', 'scriptSha256', 'configuration'} and
                config['kind'] == 'in-process', 'campaign requires a reviewed in-process backend')
        self.config = copy.deepcopy(config)
        self.source_root = Path(source_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
        def source(value):
            path = Path(value)
            # Production configurations always use reviewed portable paths.
            # Unit callers may inject absolute paths inside an explicit root;
            # no JSON/environment option can enable that production escape.
            if path.is_absolute():
                require(source_root is not None, 'backend production source paths must be relative')
            else:
                portable_path(value)
                path = self.source_root/path
            require(path.is_file() and path.resolve(strict=True).is_relative_to(self.source_root) and
                    not any(parent.is_symlink() for parent in (path, *path.parents)
                            if parent == self.source_root or self.source_root in parent.parents),
                    'backend source path escapes immutable execution root or is symlinked')
            return path
        self.script = source(config['script'])
        require(digest_bytes(self.script.read_bytes()) == config['scriptSha256'], 'backend code differs from reviewed source')
        reference = config['configuration']
        require(set(reference) == {'path', 'sha256'}, 'backend configuration lacks exact source binding')
        path = source(reference['path'])
        require(path.is_file() and not path.is_symlink() and digest_bytes(path.read_bytes()) == reference['sha256'],
                'backend configuration differs from reviewed source')
        self.settings = load_json(path)
        require(set(self.settings) == {'schemaVersion', 'contract', 'configuration'} and self.settings['schemaVersion'] == 1,
                'unsupported backend execution configuration')
        contract = self.settings['contract']
        require(set(contract) == {'componentBindingDigest', 'qualificationLockDigest', 'capabilities', 'cases', 'expiresAt'} and
                isinstance(contract['capabilities'], list) and isinstance(contract['cases'], dict) and contract['cases'],
                'incomplete reviewed backend execution contract')
        require(len(set(contract['capabilities'])) == len(contract['capabilities']) and
                all(isinstance(capability, str) and capability for capability in contract['capabilities']),
                'invalid backend capability inventory')
        for case_id, quote in contract['cases'].items():
            require(isinstance(case_id, str) and set(quote) == {'maximumInfrastructureUsd', 'maximumCleanupUsd', 'liabilityBasisDigest'}
                    and money(quote['maximumInfrastructureUsd']) > 0 and money(quote['maximumCleanupUsd']) > 0 and
                    re.fullmatch(r'sha256:[0-9a-f]{64}', quote['liabilityBasisDigest']), 'invalid reviewed case liability quote')
        self.module = self.durable = self.campaign = self.bundle = None
        self.conversations = {}

    def inspect(self, campaign):
        contract = self.settings['contract']
        require(all(contract[key] == campaign[key] for key in ('componentBindingDigest', 'qualificationLockDigest')),
                'backend execution contract differs from candidate')
        expiry = datetime.fromisoformat(contract['expiresAt'].replace('Z', '+00:00'))
        require(expiry.tzinfo is not None and datetime.now(timezone.utc) < expiry,
                'backend execution contract expired or lacks timezone')
        return {'protocol': PROTOCOL, 'status': 'configured', 'readOnly': True,
                'capabilities': list(contract['capabilities']), 'caseIds': list(contract['cases']),
                'qualificationLockDigest': contract['qualificationLockDigest']}

    def preflight(self, context):
        self.inspect(context)
        quote = self.settings['contract']['cases'].get(context['caseId'])
        require(quote is not None, 'case lacks reviewed backend liability quote')
        result = {**copy.deepcopy(quote), 'target': context['caseId'].rsplit(':', 1)[1]}
        if self.durable is not None and self.durable.state['ledger']['schemaVersion'] == 3:
            worker = self.settings['configuration'].get('cases', {}).get(context['caseId'], {}).get('attempts', {}).get(str(context['attempt']))
            require(isinstance(worker, dict) and isinstance(worker.get('attemptId'), str) and
                    type(worker.get('lifecycle', {}).get('expiresEpoch')) is int,
                    'missing exact source worker paid-admission policy')
            result['paidAttempt'] = {'id': worker['attemptId'], 'expiresEpoch': worker['lifecycle']['expiresEpoch']}
        return result

    def bind_execution(self, durable, campaign, bundle):
        require(self.durable is None and durable.store.candidate == campaign['componentBindingDigest'],
                'backend already bound or belongs to another candidate')
        self.inspect(campaign)
        self.durable, self.campaign, self.bundle = durable, copy.deepcopy(campaign), Path(bundle)

    def _authorize(self, operation, payload):
        require(self.durable is not None, 'backend mutation requires the producer-owned durable lease')
        self.durable._require_live_lease()
        candidate = payload.get('componentBindingDigest', payload.get('campaign', {}).get('componentBindingDigest'))
        require(candidate == self.campaign['componentBindingDigest'], 'backend request belongs to another candidate')
        if operation in ('export_attempt', 'export_campaign'):
            require(payload.get('campaign') == self.campaign, 'backend export differs from frozen campaign')
        else:
            require(all(payload.get(key) == self.campaign[key] for key in
                ('campaignId', 'campaignMode', 'qualificationLockDigest', 'knowledge')),
                'backend request differs from frozen campaign context')
        if operation != 'export_campaign':
            require(payload.get('campaignId') == self.campaign['campaignId'] and
                    payload.get('caseId') in self.settings['contract']['cases'] and
                    type(payload.get('attempt')) is int and payload['attempt'] in (1, 2),
                    'backend request lacks reviewed case and attempt identity')
        if operation in ('prepare', 'agent_action', 'verify', 'cleanup', 'conversation_response'):
            ledger = self.durable.state['ledger']
            validate_budget(ledger)
            matching = [row for row in ledger['entries'] if row['state'] == 'reserved' and
                all(row.get(key) == payload[key] for key in ('campaignId', 'caseId', 'attempt', 'campaignMode'))]
            require({'infrastructure', 'cleanup'} <= {row['kind'] for row in matching},
                    'backend effects require retained infrastructure and cleanup liability')

    def call(self, operation, payload, *, timeout=300):
        if operation == 'campaign_preflight':
            # This is local contract inspection, never evidence of live readiness.
            return self.inspect(payload['campaign'])
        require(operation in ('prepare', 'agent_action', 'verify', 'cleanup', 'authorize_retry',
                              'export_attempt', 'export_campaign', 'conversation_response'), 'unknown controller backend operation')
        self._authorize(operation, payload)
        if operation == 'authorize_retry':
            # Do not load executable backend code before attempt-two admission.
            # The original independent diagnosis and its exact retained bytes
            # must already exist; interrupted/unknown attempts never qualify.
            identity = {key: payload[key] for key in ('campaignId', 'caseId')}
            identity['attempt'] = 1
            retained = self.durable.state.get('attemptArtifacts', {}).get(canonical_digest(identity))
            authorized, previous = False, None
            if retained is not None:
                receipt = retained['receipt']
                failure = receipt.get('failureEvidence')
                diagnoses = [row for row in self.durable.state.get('journals', {}).get(canonical_digest(identity), [])
                    if row.get('event') == 'diagnosis' and row.get('attribution') == 'infrastructure' and
                    row.get('basis') == 'independent-infrastructure-probe']
                if (receipt.get('failureAttribution') == 'infrastructure' and receipt.get('status') in ('fail', 'blocked') and
                        receipt.get('cleanup', {}).get('status') == 'pass' and isinstance(failure, dict) and
                        any(row.get('evidenceDigest') == failure.get('sha256') for row in diagnoses)):
                    encoded = retained['evidence'].get(failure.get('path'))
                    try:
                        raw = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
                    except (ValueError, TypeError):
                        raw = None
                    # The hash binds the bytes. The bytes must still be a causal probe;
                    # a matching hash of an unlabeled blob does not authorize a retry.
                    authorized = raw is not None and digest_bytes(raw) == failure['sha256'] and infrastructure_probe_proves(raw)
                    previous = digest_bytes((json.dumps(receipt, sort_keys=True, allow_nan=False)+'\n').encode()) if authorized else None
            return {'protocol': PROTOCOL, 'authorized': authorized, 'previousAttemptDigest': previous,
                    'previousFailureAttribution': 'infrastructure' if authorized else 'undetermined'}
        with deadline(timeout):
            configuration = copy.deepcopy(self.settings['configuration'])
            case = configuration.get('cases', {}).get(payload.get('caseId'), {})
            conversation_config = case.pop('consoleConversation', None)
            if conversation_config is not None and operation in ('prepare', 'conversation_response'):
                conversation_key = canonical_digest({name: payload[name] for name in ('campaignId', 'caseId', 'attempt')})
                from agent_e2e_console_conversation import Conversation
                self.conversations[conversation_key] = Conversation(durable=self.durable, context=payload,
                    configuration=conversation_config, case=case)
            if operation == 'conversation_response':
                require(conversation_config is not None, 'this case has no reviewed original Console turn delivery')
                return {'protocol': PROTOCOL, **self.conversations[conversation_key].append(payload['response'])}
            if self.module is None:
                artifacts = self.durable.state.get('attemptArtifacts', {})
                recover_export = (operation in ('export_attempt', 'export_campaign') and artifacts and
                    all(row['state'] == 'settled' for row in self.durable.state['ledger']['entries']))
                if operation == 'export_attempt':
                    identity = {key: payload[key] for key in ('campaignId', 'caseId', 'attempt')}
                    recover_export = recover_export and canonical_digest(identity) in artifacts
                require(operation == 'prepare' or recover_export,
                        'backend module can first load only after prepaid prepare or settled evidence recovery')
                require(digest_bytes(self.script.read_bytes()) == self.config['scriptSha256'], 'backend changed after inspection')
                spec = importlib.util.spec_from_file_location('honua_reviewed_backend_'+self.config['scriptSha256'][7:], self.script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                require(callable(getattr(module, 'handle', None)), 'reviewed backend entry lacks handle')
                self.module = module
            result = self.module.handle(operation, copy.deepcopy(payload), durable=self.durable,
                campaign=copy.deepcopy(self.campaign), bundle=self.bundle,
                configuration=configuration, timeout=timeout)
        require(isinstance(result, dict), 'backend returned a non-object response')
        require('consoleConversation' not in result, 'backend cannot invent Console turn delivery')
        if operation == 'prepare' and conversation_config is not None:
            result['consoleConversation'] = True
        result = {'protocol': PROTOCOL, **result}
        require(result['protocol'] == PROTOCOL and len(json.dumps(result, allow_nan=False).encode()) <= 1024*1024,
                'backend public response is unbound or exceeds observation limit')
        return result
