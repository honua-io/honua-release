"""Controller-side container command transport. Infrastructure is Terraform-owned.

The same per-attempt queues connect Fargate or dedicated Docker workers. Replies
are untrusted terminal output, never independent qualification evidence.
"""
from __future__ import annotations

import hashlib
import base64
import json
import math
import re
import threading
import time

from agent_e2e_errors import PackError

PROTOCOL = "honua.agent-e2e.container/v1"
MCP_PROTOCOL = "honua.agent-e2e.mcp-action/v1"
CHUNK_PROTOCOL = "honua.agent-e2e.reply-chunk/v1"
MAX_REPLY_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 32768
MAX_STDOUT = 16384
MAX_STDERR = 4096
INSTALLED_WORKER_SQS_CALL_LIMIT = 32768
INSTALLED_CONTROLLER_SQS_CALL_LIMIT = 65536
_installed_sqs_calls = {}
_installed_sqs_lock = threading.Lock()


def installed_sqs_limits():
    """Source bounds, including failed calls; not a quote or incurred charge."""
    return {'workerCallsPerPhysicalExecution': INSTALLED_WORKER_SQS_CALL_LIMIT,
            'controllerCallsPerExecutionPerPhysicalProcess': INSTALLED_CONTROLLER_SQS_CALL_LIMIT}


class InstalledSqsBudget:
    """Finite SDK attempts shared across reconstructed installed transports.

    Worker identity is its actual physical task, including the unbound phase;
    controller identity is original E. The process never removes a counter.
    SDK clients must separately disable internal retries (total_max_attempts=1).
    """
    def __init__(self, client, *, kind, candidate_key, runtime_id, identity):
        if (kind not in ('worker', 'controller') or
                not isinstance(candidate_key, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', candidate_key) or
                not isinstance(runtime_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', runtime_id) or
                not isinstance(identity, str) or not 0 < len(identity) <= 2048 or
                any(ord(character) < 32 for character in identity)):
            raise PackError('original installed request-budget identity is required')
        self._key = (kind, candidate_key, runtime_id, identity)
        if not re.fullmatch(r'execution-[a-f0-9]{32}' if kind == 'controller' else
                r'arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/[A-Za-z0-9_-]+/[a-f0-9]{32}', identity):
            raise PackError('installed request budget requires the original native E or physical task')
        if isinstance(client, InstalledSqsBudget):
            if client._key != self._key:
                raise PackError('installed request budget cannot change its original identity')
            client = client._client
        self._client = client

    def _call(self, method, request):
        limit = (INSTALLED_WORKER_SQS_CALL_LIMIT if self._key[0] == 'worker'
                 else INSTALLED_CONTROLLER_SQS_CALL_LIMIT)
        with _installed_sqs_lock:
            used = _installed_sqs_calls.get(self._key, 0)
            if used >= limit:
                raise PackError('installed SQS request bound exhausted; preserve original outcome and liabilities')
            # Reserve before wire, including calls with ambiguous failures.
            _installed_sqs_calls[self._key] = used + 1
        return getattr(self._client, method)(**request)

    def send_message(self, **request):
        return self._call('send_message', request)

    def receive_message(self, **request):
        return self._call('receive_message', request)

    def delete_message(self, **request):
        return self._call('delete_message', request)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def command_digest(command):
    return "sha256:" + hashlib.sha256(encode(command).encode()).hexdigest()


def validate_command(value, attempt_id, expires, now):
    structured = isinstance(value, dict) and value.get('protocol') == MCP_PROTOCOL
    required = {"protocol", "attemptId", "actionId", "deadlineEpoch", "timeoutSeconds", 'action' if structured else 'command'}
    if (not isinstance(value, dict) or set(value) != required or value["protocol"] not in (PROTOCOL, MCP_PROTOCOL)
            or value["attemptId"] != attempt_id
            or not isinstance(value["actionId"], str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", value["actionId"])
            or type(value["timeoutSeconds"]) is not int or not 1 <= value["timeoutSeconds"] <= (120 if structured else 300)
            or type(value["deadlineEpoch"]) is not int
            or not now < value["deadlineEpoch"] <= expires
            or value["deadlineEpoch"] > now + value["timeoutSeconds"] + 1):
        raise PackError("invalid, expired or foreign container command")
    if structured:
        action = value['action']
        if not isinstance(action, dict) or set(action) != {'name','input'} or not isinstance(action['input'], dict):
            raise PackError('invalid MCP action')
        if action['name'] == 'mcp_list_tools':
            args = action['input']
            valid = (not args or set(args) == {'view'} and isinstance(args['view'], str)
                     and re.fullmatch(r'[a-z][a-z0-9-]{0,63}', args['view']) is not None)
        elif action['name'] == 'mcp_call_tool':
            args = action['input']
            valid = (set(args) == {'name','arguments'} and isinstance(args['name'], str)
                     and bool(args['name'].strip()) and len(args['name']) <= 256 and isinstance(args['arguments'], dict))
        else:
            valid = False
        if not valid:
            raise PackError('invalid MCP action')
    elif (not isinstance(value['command'], str) or not value['command'].strip()
          or len(value['command'].encode()) > 65536):
        raise PackError('invalid terminal command')
    if len(encode(value).encode()) > 100000:
        raise PackError('oversized container command')


def validate_reply(value, command):
    if command['protocol'] == MCP_PROTOCOL:
        validate_mcp_reply(value, {'protocol':MCP_PROTOCOL, 'attemptId':command['attemptId'],
                                  'actionId':command['actionId'], 'commandDigest':command_digest(command)})
        return
    required = {"protocol", "attemptId", "actionId", "commandDigest", "status", "exitCode", "stdout", "stderr"}
    if (not isinstance(value, dict) or set(value) != required or value["protocol"] != PROTOCOL
            or value["attemptId"] != command["attemptId"] or value["actionId"] != command["actionId"]
            or value["commandDigest"] != command_digest(command)
            or value["status"] not in ("completed", "timed-out", "interrupted")
            or type(value["exitCode"]) is not int
            or not isinstance(value["stdout"], str) or len(value["stdout"].encode()) > MAX_STDOUT
            or not isinstance(value["stderr"], str) or len(value["stderr"].encode()) > MAX_STDERR):
        raise PackError("unbound or malformed container reply")


def validate_mcp_reply(value, binding):
    required = {'protocol','attemptId','actionId','commandDigest','status','payload'}
    if (not isinstance(value, dict) or set(value) != required
            or any(value.get(key) != expected for key, expected in binding.items())
            or value['status'] not in ('completed','timed-out','interrupted')
            or (value['status'] == 'completed' and not isinstance(value['payload'], dict))
            or (value['status'] != 'completed' and value['payload'] is not None)
            or len(encode(value).encode()) > MAX_REPLY_BYTES):
        raise PackError('unbound or malformed MCP reply')


def reply_messages(reply, command):
    validate_reply(reply, command)
    if command['protocol'] != MCP_PROTOCOL:
        return [reply]
    raw = encode(reply).encode()
    digest = 'sha256:'+hashlib.sha256(raw).hexdigest()
    count = math.ceil(len(raw)/CHUNK_BYTES)
    return [{'protocol':CHUNK_PROTOCOL, 'attemptId':command['attemptId'], 'actionId':command['actionId'],
             'commandDigest':command_digest(command), 'payloadDigest':digest, 'byteLength':len(raw),
             'index':index, 'count':count,
             'data':base64.b64encode(raw[index*CHUNK_BYTES:(index+1)*CHUNK_BYTES]).decode()}
            for index in range(count)]


class ReplyChunks:
    """Bounded assembly; partial replies never become an action observation."""
    def __init__(self, command):
        self.expected = {'protocol':MCP_PROTOCOL, 'attemptId':command['attemptId'],
                         'actionId':command['actionId'], 'commandDigest':command_digest(command)}
        self.binding, self.parts = None, {}

    @classmethod
    def from_binding(cls, binding):
        """Recover from a controller-retained binding without retaining raw commands."""
        if (not isinstance(binding, dict) or set(binding) != {'protocol','attemptId','actionId','commandDigest'}
                or binding['protocol'] != MCP_PROTOCOL
                or any(not isinstance(binding[key], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', binding[key])
                       for key in ('attemptId','actionId'))
                or not isinstance(binding['commandDigest'], str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', binding['commandDigest'])):
            raise PackError('invalid retained command binding')
        instance = cls.__new__(cls)
        instance.expected, instance.binding, instance.parts = dict(binding), None, {}
        return instance

    def accept(self, value):
        required = {'protocol','attemptId','actionId','commandDigest','payloadDigest','byteLength','index','count','data'}
        if (not isinstance(value, dict) or set(value) != required or value['protocol'] != CHUNK_PROTOCOL
                or value['attemptId'] != self.expected['attemptId'] or value['actionId'] != self.expected['actionId']
                or value['commandDigest'] != self.expected['commandDigest']
                or type(value['byteLength']) is not int or not 0 < value['byteLength'] <= MAX_REPLY_BYTES
                or type(value['count']) is not int or value['count'] != math.ceil(value['byteLength']/CHUNK_BYTES)
                or type(value['index']) is not int or not 0 <= value['index'] < value['count']
                or not isinstance(value['payloadDigest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['payloadDigest'])
                or not isinstance(value['data'], str) or len(value['data']) > 4*math.ceil(CHUNK_BYTES/3)):
            raise PackError('unbound or oversized reply chunk')
        binding = (value['payloadDigest'], value['byteLength'], value['count'])
        if self.binding is not None and binding != self.binding:
            raise PackError('reply chunk binding changed')
        self.binding = binding
        try:
            part = base64.b64decode(value['data'], validate=True)
        except ValueError as exc:
            raise PackError('invalid reply chunk encoding') from exc
        expected = min(CHUNK_BYTES, value['byteLength']-value['index']*CHUNK_BYTES)
        if len(part) != expected or (value['index'] in self.parts and self.parts[value['index']] != part):
            raise PackError('reply chunk length or duplicate content changed')
        self.parts[value['index']] = part
        if len(self.parts) != value['count']:
            return None
        raw = b''.join(self.parts[index] for index in range(value['count']))
        if 'sha256:'+hashlib.sha256(raw).hexdigest() != value['payloadDigest']:
            raise PackError('reply payload digest mismatch')
        reply = json.loads(raw)
        validate_mcp_reply(reply, self.expected)
        return reply


class Worker:
    def __init__(self, sqs, config, *, clock=time.time):
        required = {"commandQueueUrl", "replyQueueUrl", "attemptId", "expiresEpoch"}
        if (not isinstance(config, dict) or set(config) != required
                or not isinstance(config["attemptId"], str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", config["attemptId"])
                or type(config["expiresEpoch"]) is not int
                or not clock() < config["expiresEpoch"] <= clock() + 6000
                or config["commandQueueUrl"] == config["replyQueueUrl"]):
            raise PackError("invalid container transport configuration")
        for field in ("commandQueueUrl", "replyQueueUrl"):
            if not isinstance(config[field], str) or not re.fullmatch(
                    r"https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+\.fifo", config[field]):
                raise PackError("container transport requires reviewed FIFO queues")
        self.sqs, self.config, self.clock = sqs, dict(config), clock
        self.sent = set()

    def execute(self, command, action_id, timeout=300):
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise PackError("invalid terminal timeout")
        now = self.clock()
        deadline = min(math.ceil(now + timeout), self.config["expiresEpoch"] - 5)
        request = {"protocol": PROTOCOL, "attemptId": self.config["attemptId"], "actionId": action_id,
                   "deadlineEpoch": deadline, "command": command, "timeoutSeconds": timeout}
        return self._exchange(request, now)

    def execute_mcp(self, action, action_id, *, retain_chunk, retain_dispatch, timeout=120):
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise PackError('invalid MCP timeout')
        if not callable(retain_chunk) or not callable(retain_dispatch):
            raise PackError('MCP transport requires durable dispatch and chunk retention')
        now = self.clock()
        request = {'protocol':MCP_PROTOCOL, 'attemptId':self.config['attemptId'], 'actionId':action_id,
                   'deadlineEpoch':min(math.ceil(now+timeout), self.config['expiresEpoch']-35),
                   'action':action, 'timeoutSeconds':timeout}
        return self._exchange(request, now, retain_chunk=retain_chunk, retain_dispatch=retain_dispatch)

    def _exchange(self, request, now, *, retain_chunk=None, retain_dispatch=None):
        action_id, deadline = request['actionId'], request['deadlineEpoch']
        validate_command(request, self.config["attemptId"], self.config["expiresEpoch"], now)
        if action_id in self.sent:
            raise PackError("action already dispatched; reconcile durable history before retry")
        # Mark before the API call: an ambiguous network failure must not resend.
        # The backend also persists dispatch before calling this transport.
        self.sent.add(action_id)
        if request['protocol'] == MCP_PROTOCOL:
            retain_dispatch(ReplyChunks(request).expected)
            validate_command(request, self.config['attemptId'], self.config['expiresEpoch'], self.clock())
        self.sqs.send_message(QueueUrl=self.config["commandQueueUrl"], MessageBody=encode(request),
            MessageGroupId=self.config["attemptId"], MessageDeduplicationId=command_digest(request)[7:])
        chunks = ReplyChunks(request) if request['protocol'] == MCP_PROTOCOL else None
        reply_deadline = min(self.config['expiresEpoch'], deadline+(30 if chunks else 5))
        while self.clock() < reply_deadline:
            response = self.sqs.receive_message(QueueUrl=self.config["replyQueueUrl"],
                MaxNumberOfMessages=1, WaitTimeSeconds=min(2, max(1, int(reply_deadline - self.clock()))),
                VisibilityTimeout=30)
            for message in response.get("Messages", []):
                if len(message["Body"].encode()) > (60000 if chunks else 40000):
                    raise PackError("oversized container reply")
                value = json.loads(message["Body"])
                reply = chunks.accept(value) if chunks else value
                if chunks is None:
                    validate_reply(reply, request)
                else:
                    # The backend must durably retain each bound raw chunk before
                    # the queue deletes it. A restarted controller can reassemble
                    # retained chunks without dispatching the action again.
                    retain_chunk(value)
                self.sqs.delete_message(QueueUrl=self.config["replyQueueUrl"],
                                        ReceiptHandle=message["ReceiptHandle"])
                if reply is not None:
                    return reply
        raise PackError("container response missing; outcome remains unverified")
