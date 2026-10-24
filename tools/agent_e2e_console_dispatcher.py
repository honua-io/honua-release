"""Installed MCP dispatch over the original E's fixed worker queues.

Each dispatch binding and raw reply chunk is permanent before wire/deletion.
A new controller process consumes the original reply; it never resends an
ambiguous model-selected operation. Physical/native authority is rechecked by
the installed driver's guard before every queue effect.
"""
from __future__ import annotations

import copy
import json

from agent_e2e_console_runtime import require, validate_execution_history
from agent_e2e_controller import validate_action
from agent_e2e_execution_protocol import validate_worker_bound
from agent_e2e_worker import ReplyChunks, Worker


class GuardedQueues:
    def __init__(self, client, guard, route):
        self.client, self.guard, self.route = client, guard, route

    def send_message(self, **request):
        self.guard()
        return self.client.send_message(**request)

    def receive_message(self, **request):
        self.guard()
        response = self.client.receive_message(**request)
        messages = [message for message in response.get('Messages', []) if not self.route(message)]
        return {**response, 'Messages': messages}

    def delete_message(self, **request):
        self.guard()
        return self.client.delete_message(**request)


class McpDispatcher:
    def __init__(self, *, journal, worker, capability, guard):
        require(isinstance(worker, Worker) and worker.config['attemptId'] == journal.record['executionId'],
                'installed MCP transport belongs to another original execution')
        require(callable(guard), 'installed MCP dispatch requires its current physical/native authority guard')
        require(isinstance(capability, str) and len(capability) == 64 and
                all(c in '0123456789abcdef' for c in capability), 'original native execution capability missing')
        self.journal, self.worker, self.capability, self.guard = journal, worker, capability, guard
        self.current_action = None
        self.queues = GuardedQueues(worker.sqs, guard, self._route_retained_duplicate)
        # Preserve the worker protocol and command-deadline implementation.
        self.transport = Worker(self.queues, worker.config, clock=worker.clock)

    def _intent(self, intent):
        validate_execution_history(self.journal.record)
        require(isinstance(intent, dict) and intent in self.journal.values('action-intent'),
                'MCP operation lacks its retained original provider action intent')
        action = intent.get('action')
        validate_action(action)
        require(action['name'] in ('mcp_list_tools', 'mcp_call_tool'),
                'installed worker accepts only native MCP actions')
        require(sum(row.get('actionId') == intent.get('actionId') for row in
                    self.journal.values('action-intent')) == 1, 'original action identity is ambiguous')
        return {'name': action['name'], 'input': copy.deepcopy(action['input'])}

    def _rows(self, kind, action_id):
        return [row for row in self.journal.values(kind) if row.get('actionId') == action_id]

    def _chunk(self, action_id, chunk):
        row = {'actionId': action_id, 'chunk': chunk}
        # A redelivered identical chunk is acknowledged after the original is
        # found. Contradictory duplicate bytes are refused by ReplyChunks first.
        if row not in self._rows('worker-command-chunk', action_id):
            self.journal.append('worker-command-chunk', row)

    def _collector(self, action_id):
        dispatches = self._rows('worker-command-dispatch', action_id)
        require(len(dispatches) == 1, 'original MCP dispatch identity is ambiguous')
        binding = dispatches[0]['binding']
        require(binding['actionId'] == action_id and binding['attemptId'] == self.journal.record['executionId'],
                'retained MCP dispatch belongs to another execution/action')
        collector, reply = ReplyChunks.from_binding(binding), None
        for row in self._rows('worker-command-chunk', action_id):
            reply = collector.accept(row['chunk'])
        return collector, reply

    def _route_retained_duplicate(self, message):
        require(isinstance(message.get('Body'), str) and len(message['Body'].encode()) <= 60000,
                'oversized recovered MCP reply')
        chunk = json.loads(message['Body'])
        require(isinstance(chunk, dict), 'invalid recovered MCP reply')
        if chunk.get('protocol') == 'honua.installed-worker-bound/v1':
            intents = self.journal.values('worker-binding-intent')
            require(len(intents) == 1 and self.journal.values('worker-binding-observation') and
                    self.journal.values('worker-binding-reply'), 'worker control reply lacks original acknowledged binding')
            original = intents[0]
            validate_worker_bound(chunk, original)
            if not any(row.get('reply') == chunk for row in self.journal.values('worker-binding-reply')):
                self.journal.append('worker-binding-reply', {'reply': chunk})
            self.queues.delete_message(QueueUrl=self.worker.config['replyQueueUrl'], ReceiptHandle=message['ReceiptHandle'])
            return True
        action_id = chunk.get('actionId')
        if action_id == self.current_action:
            return False  # The current bound Worker/ReplyChunks validates it.
        # SQS receive is not acknowledgement. An older completed result can have
        # a visible message after a crash between durable retention and delete.
        # Route it only through its original E/action collector; foreign or
        # contradictory bytes remain unacknowledged and stop this execution.
        collector, prior = self._collector(action_id)
        require(prior is not None and collector.accept(chunk) == prior,
                'another original MCP outcome remains unresolved or changed')
        self._chunk(action_id, chunk)
        self.queues.delete_message(QueueUrl=self.worker.config['replyQueueUrl'], ReceiptHandle=message['ReceiptHandle'])
        return True

    @staticmethod
    def _result(reply):
        require(reply is not None and reply['status'] == 'completed',
                'original native MCP operation did not return a complete result')
        return reply['payload']

    def execute(self, intent):
        action = self._intent(intent)
        action_id = intent['actionId']
        require(not self._rows('worker-command-dispatch', action_id),
                'original MCP dispatch already intended; recover without resending')
        self.current_action = action_id
        self.guard()
        def dispatch(binding):
            require(binding['actionId'] == action_id and binding['attemptId'] == self.journal.record['executionId'],
                    'MCP transport replaced the original provider action')
            self.journal.append('worker-command-dispatch', {'actionId': action_id, 'binding': binding})
        reply = self.transport.execute_mcp(action, action_id, retain_dispatch=dispatch,
            retain_chunk=lambda chunk: self._chunk(action_id, chunk))
        return self._result(reply)

    def recover(self, intent):
        self._intent(intent)
        action_id = intent['actionId']
        self.current_action = action_id
        dispatches = self._rows('worker-command-dispatch', action_id)
        if not dispatches:
            # No send can precede the permanent dispatch record. This resumes a
            # crash before dispatch, rather than replaying an unknown operation.
            require(not self._rows('worker-command-chunk', action_id), 'orphan MCP reply chunks lack original dispatch')
            return self.execute(intent)
        collector, reply = self._collector(action_id)
        if reply is not None:
            return self._result(reply)
        # Read-only recovery never sends a command, including after a lost
        # SendMessage acknowledgement. Expiry does not manufacture a result.
        deadline = min(self.worker.config['expiresEpoch'], self.worker.clock()+120)
        while self.worker.clock() < deadline:
            response = self.queues.receive_message(QueueUrl=self.worker.config['replyQueueUrl'],
                MaxNumberOfMessages=1, WaitTimeSeconds=min(2, max(1, int(deadline-self.worker.clock()))),
                VisibilityTimeout=30)
            for message in response.get('Messages', []):
                require(isinstance(message.get('Body'), str) and len(message['Body'].encode()) <= 60000,
                        'oversized recovered MCP reply')
                chunk = json.loads(message['Body'])
                reply = collector.accept(chunk)
                self._chunk(action_id, chunk)
                self.queues.delete_message(QueueUrl=self.worker.config['replyQueueUrl'],
                    ReceiptHandle=message['ReceiptHandle'])
                if reply is not None:
                    return self._result(reply)
        raise ValueError('original MCP reply remains unknown; do not resend or release its liabilities')
