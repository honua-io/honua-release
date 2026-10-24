"""Trusted first binding for a fresh physical installed-slot worker.

The task starts without a turn/capability. Only its controller-owned FIFO can
provide that binding. Native independently validates it against the actual
physical task before any model-selected action can run. A task never rebinds.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import http.client
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse
import urllib.error
import urllib.request

from agent_e2e_execution_protocol import require, identifier, execution_id, digest, canonical_digest
from agent_e2e_mcp import validate_agent_context, validate_execution_context, NoRedirect, parse, execution_headers
from agent_e2e_worker import Worker, InstalledSqsBudget, command_digest, encode
from agent_e2e_worker import MCP_PROTOCOL
from agent_e2e_container import Executor

BIND_PROTOCOL = 'honua.installed-worker-bind/v1'
BOUND_PROTOCOL = 'honua.installed-worker-bound/v1'


def native_binding_read(origin, credential, value):
    """Reconcile an original acknowledgement with an authenticated GET only."""
    return _native_binding_request(origin, credential, value, method='GET')


def native_binding_ack(origin, credential, value):
    """One native task-authenticated acknowledgement; no setup/control key."""
    return _native_binding_request(origin, credential, value, method='POST')


def _native_binding_request(origin, credential, value, *, method):
    uri = urllib.parse.urlsplit(origin)
    require(not uri.username and not uri.password and not uri.query and not uri.fragment and uri.path == '' and
            (uri.scheme == 'https' or uri.scheme == 'http' and uri.hostname in ('127.0.0.1', 'localhost', '::1')),
            'installed native binding requires immutable TLS origin or test loopback')
    headers = {'X-API-Key': credential, 'Accept': 'application/json',
        'X-Honua-Workspace': value['workspaceId'], 'X-Honua-Turn': value['turnId'],
        **execution_headers({k: value[k] for k in ('executionId', 'claimGeneration', 'workerId', 'capability')})}
    if method == 'POST': headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(origin+'/api/v1/agent/executions/'+value['executionId']+'/worker-binding', headers=headers,
        data=b'{}' if method == 'POST' else None, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=10) as response:
        raw = response.read(32769)
        require(response.status == 200 and len(raw) <= 32768, 'native original worker binding unavailable')
    for secret in (credential, value['capability']):
        parse(raw, secret=secret)
    return {'body': parse(raw), 'rawBodyBase64': base64.b64encode(raw).decode(),
        'responseDigest': 'sha256:'+hashlib.sha256(raw).hexdigest(), 'httpStatus': 200}


def transport(value):
    fields = {'schemaVersion', 'mode', 'candidateKey', 'runtimeId', 'slotId', 'commandQueueUrl', 'replyQueueUrl',
              'originalFundingExpiresEpoch', 'maxExecutionSeconds'}
    require(isinstance(value, dict) and set(value) == fields and value['schemaVersion'] == 2 and
            value['mode'] == 'installed-slot', 'invalid installed worker transport')
    digest(value['candidateKey']); identifier(value['runtimeId'], 'runtime ID')
    require(isinstance(value['slotId'], str) and re.fullmatch(r'[a-z0-9-]{3,40}', value['slotId']) and
            type(value['originalFundingExpiresEpoch']) is int and value['originalFundingExpiresEpoch'] > 0 and
            type(value['maxExecutionSeconds']) is int and 1 <= value['maxExecutionSeconds'] <= 6000,
            'invalid installed worker lifetime/slot')
    # Reuse actual transport's finite FIFO/account shape validation, without a
    # synthetic attempt or a send. Its original slot ID is a real resource ID.
    Worker(None, {**{k: value[k] for k in ('commandQueueUrl', 'replyQueueUrl')},
        'attemptId': value['slotId'], 'expiresEpoch': int(time.time())+60})
    return copy.deepcopy(value)


def actual_task():
    endpoint = os.environ.get('ECS_CONTAINER_METADATA_URI_V4', '')
    uri = urllib.parse.urlsplit(endpoint)
    require(uri.scheme == 'http' and uri.hostname == '169.254.170.2' and not uri.port and
            not uri.username and not uri.password and not uri.query and not uri.fragment and
            re.fullmatch(r'/v4/[A-Za-z0-9-]+', uri.path), 'actual task metadata endpoint unavailable')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(endpoint+'/task', timeout=5) as response:
        raw = response.read(32769)
    require(len(raw) <= 32768, 'actual task metadata exceeds bound')
    value = json.loads(raw)
    arn = value.get('TaskARN')
    require(isinstance(arn, str) and re.fullmatch(r'arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9_-]+/[0-9a-f]{32}', arn),
            'actual physical worker identity unavailable')
    return arn


class InstalledBinding:
    def __init__(self, configuration, mcp, journal, *, worker_id, clock=time.time,
                 native_read=native_binding_read, native_ack=native_binding_ack):
        self.config, self.clock = transport(configuration), clock
        require(isinstance(mcp, dict) and set(mcp) == {'endpoint', 'credentialKind', 'credential', 'workflowView',
            'bindingMode', 'runtimeId', 'slotId'} and mcp['bindingMode'] == 'native-execution/v1' and
            mcp['runtimeId'] == self.config['runtimeId'] and mcp['slotId'] == self.config['slotId'] and
            mcp['credentialKind'] == 'api-key' and isinstance(mcp['credential'], str) and mcp['credential'],
            'installed MCP secret must be immutable and initially unbound')
        self.mcp, self.worker_id, self.native_read, self.native_ack = copy.deepcopy(mcp), worker_id, native_read, native_ack
        require(isinstance(worker_id, str) and re.fullmatch(r'arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9_-]+/[0-9a-f]{32}', worker_id),
                'installed worker requires actual ECS task identity')
        endpoint = urllib.parse.urlsplit(mcp['endpoint'])
        require(endpoint.path == '/mcp' and not endpoint.query and not endpoint.fragment, 'installed MCP endpoint must be exact native route')
        self.origin = urllib.parse.urlunsplit((endpoint.scheme, endpoint.netloc, '', '', ''))
        self.db = sqlite3.connect(journal)
        Path(journal).chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS installed_binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS binding_reads (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS native_ack_intent (id INTEGER PRIMARY KEY CHECK(id=1), request_digest TEXT NOT NULL)')
        self.db.commit()
        self.binding = None

    def validate(self, value):
        fields = {'protocol', 'candidateKey', 'runtimeId', 'slotId', 'executionId', 'workspaceId', 'turnId',
            'claimGeneration', 'expiresEpoch', 'workerId', 'nativeBindingDigest', 'capability'}
        require(isinstance(value, dict) and set(value) == fields and value['protocol'] == BIND_PROTOCOL and
                all(value[k] == self.config[k] for k in ('candidateKey', 'runtimeId', 'slotId')) and
                value['workerId'] == self.worker_id, 'installed binding differs from actual worker/runtime')
        execution_id(value['executionId']); digest(value['nativeBindingDigest'])
        validate_agent_context({k: value[k] for k in ('workspaceId', 'turnId')})
        validate_execution_context({k: value[k] for k in ('executionId', 'claimGeneration', 'workerId', 'capability')})
        require(type(value['expiresEpoch']) is int and self.clock() < value['expiresEpoch'] <=
                min(self.config['originalFundingExpiresEpoch'], self.clock()+self.config['maxExecutionSeconds']),
                'installed execution binding expired or exceeds original funding')

    def accept(self, value):
        self.validate(value)
        # A physical process never needs a reusable capability on disk. Keep an
        # unambiguous private binding verifier only; duplicate delivery still
        # supplies the actual capability and native independently revalidates it.
        persisted = {key: item for key, item in value.items() if key != 'capability'}
        persisted['capabilityVerifier'] = hmac.new(self.mcp['credential'].encode(),
            value['capability'].encode(), hashlib.sha256).hexdigest()
        raw = encode(persisted)
        row = self.db.execute('SELECT value FROM installed_binding WHERE id=1').fetchone()
        if row is None:
            self.db.execute('INSERT INTO installed_binding(id,value) VALUES(1,?)', (raw,))
            self.db.commit()  # Permanent before native read or any action.
        else:
            require(row[0] == raw, 'physical worker cannot rebind to another execution/claim')
        intent_digest = canonical_digest({'method': 'POST', 'path': '/api/v1/agent/executions/'+value['executionId']+
            '/worker-binding', 'body': {}, 'binding': persisted})
        prior = self.db.execute('SELECT request_digest FROM native_ack_intent WHERE id=1').fetchone()
        if prior is None:
            self.db.execute('INSERT INTO native_ack_intent(id,request_digest) VALUES(1,?)', (intent_digest,))
            self.db.commit()  # Permanent before the one acknowledgement call.
            try:
                acknowledged = self.native_ack(self.origin, self.mcp['credential'], value)
            except urllib.error.HTTPError:
                raise  # An actual authorization refusal is not an unknown reply.
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError):
                pass  # Recover the same native record by GET; never repeat POST.
            else:
                self.validate_observation(acknowledged, value)
                self.db.execute('INSERT INTO binding_reads(id,value) VALUES(?,?)', ('ack-response', encode(acknowledged)))
                self.db.commit()
        else:
            require(prior[0] == intent_digest, 'original native acknowledgement intent changed')
        index = self.db.execute('SELECT COUNT(*) FROM binding_reads').fetchone()[0]
        observed = self.native_read(self.origin, self.mcp['credential'], value)
        self.validate(value)
        expected = {k: value[k] for k in ('candidateKey', 'runtimeId', 'slotId', 'executionId',
            'workspaceId', 'turnId', 'claimGeneration', 'expiresEpoch', 'workerId', 'nativeBindingDigest')}
        self.validate_observation(observed, value)
        key = 'bind-read-'+str(index)
        self.db.execute('INSERT INTO binding_reads(id,value) VALUES(?,?)', (key, encode(observed)))
        self.db.commit()
        self.binding = copy.deepcopy(value)
        # Keep credentials in the private MCP child environment only. Model
        # terminal commands already inherit a minimal environment.
        private = {k: self.mcp[k] for k in ('endpoint', 'credentialKind', 'credential', 'workflowView')}
        private['agentContext'] = {k: value[k] for k in ('workspaceId', 'turnId')}
        private['executionContext'] = {k: value[k] for k in ('executionId', 'claimGeneration', 'workerId', 'capability')}
        os.environ['HONUA_AGENT_MCP_CONFIG'] = encode(private)
        return {'protocol': BOUND_PROTOCOL, **expected, 'nativeRead': {'key': key, 'sha256': canonical_digest(observed)}}

    def validate_observation(self, observed, value):
        expected = {k: value[k] for k in ('candidateKey', 'runtimeId', 'slotId', 'executionId',
            'workspaceId', 'turnId', 'claimGeneration', 'expiresEpoch', 'workerId', 'nativeBindingDigest')}
        body = observed.get('body', {})
        require(observed.get('httpStatus') == 200 and isinstance(body, dict) and set(body) ==
            {'schemaVersion', 'state', 'context', 'contextDigest', 'acknowledgement', *expected} and
            body['schemaVersion'] == 1 and body['state'] == 'running' and all(body[k] == v for k, v in expected.items()),
            'native did not independently authorize the original physical worker binding')
        context = body['context']
        require(isinstance(context, dict) and context.get('workspaceId') == value['workspaceId'] and
            canonical_digest(context) == body['contextDigest'], 'native task context digest differs from original bytes')
        acknowledgement = body['acknowledgement']
        require(isinstance(acknowledgement, dict) and set(acknowledgement) == {'actorId', 'credentialVersion'} and
            all(isinstance(acknowledgement[k], str) and 0 < len(acknowledgement[k]) <= 2048 and
                not any(ord(character) < 32 for character in acknowledgement[k]) for k in acknowledgement),
            'native task acknowledgement principal/version missing')
        prior = self.db.execute('SELECT value FROM binding_reads').fetchall()
        require(all(json.loads(row[0])['body'] == body for row in prior),
            'original acknowledged native task/context changed')

    def close(self):
        self.db.close()


def bind_message(sqs, config, message, binding):
    require(isinstance(message.get('Body'), str) and len(message['Body'].encode()) <= 100000, 'oversized installed command envelope')
    value = parse(message['Body'].encode())
    if not isinstance(value, dict) or value.get('protocol') != BIND_PROTOCOL:
        return False
    require(len(message['Body'].encode()) <= 32768, 'oversized installed bind')
    reply = binding.accept(value)
    sqs.send_message(QueueUrl=config['replyQueueUrl'], MessageBody=encode(reply),
        MessageGroupId=value['executionId'], MessageDeduplicationId=command_digest(reply)[7:])
    sqs.delete_message(QueueUrl=config['commandQueueUrl'], ReceiptHandle=message['ReceiptHandle'])
    return True


class McpOnlyExecutor(Executor):
    """Installed Console operation policy has no untrusted local shell.

    Legacy case workers retain their separate terminal protocol. An installed
    task cannot expose its private agent transport to a shell sharing its UID,
    environment or private journal, even through a crafted queue command.
    """
    def handle(self, request):
        require(isinstance(request, dict) and request.get('protocol') == MCP_PROTOCOL,
                'installed Console workers accept canonical native MCP actions only')
        return super().handle(request)

    def _execute_mcp(self, request, seconds):
        # The validated, durable transport command owns this identity. Tool
        # arguments cannot select or replace the native operation's action.
        return self.execute_mcp(request['action'], seconds, self.workspace, action_id=request['actionId'])


def serve_installed(sqs, configuration, *, journal='/journal/installed.sqlite', workspace='/workspace',
                    task_reader=actual_task, clock=time.time):
    from agent_e2e_container import serve
    config = transport(configuration)
    started = clock()
    # An unbound physical task must not wait for the entire funding window.
    deadline = min(started+120, config['originalFundingExpiresEpoch'])
    worker_id = task_reader()
    sqs = InstalledSqsBudget(sqs, kind='worker', candidate_key=config['candidateKey'],
        runtime_id=config['runtimeId'], identity=worker_id)
    binding = InstalledBinding(config, json.loads(os.environ.pop('HONUA_AGENT_MCP_CONFIG', '')), journal,
        worker_id=worker_id, clock=clock)
    try:
        while binding.binding is None and clock() < deadline:
            response = sqs.receive_message(QueueUrl=config['commandQueueUrl'], MaxNumberOfMessages=1,
                WaitTimeSeconds=1, VisibilityTimeout=40)
            for message in response.get('Messages', []):
                require(bind_message(sqs, config, message, binding), 'model action received before original native worker bind')
        require(binding.binding is not None, 'unbound worker startup deadline exceeded')
        worker = {**{k: config[k] for k in ('commandQueueUrl', 'replyQueueUrl')},
            'attemptId': binding.binding['executionId'], 'expiresEpoch': binding.binding['expiresEpoch']}
        executor = McpOnlyExecutor(worker, str(Path(journal).with_name('actions.sqlite')), workspace, clock=clock)
        serve(sqs, worker, executor, clock=clock,
            control_handler=lambda message: bind_message(sqs, config, message, binding))
    finally:
        binding.close()
