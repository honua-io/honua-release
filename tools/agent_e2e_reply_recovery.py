"""Retain pending worker replies after controller loss; never redispatch work.

Run with controller-owned dispatch bindings and queue configuration. SDK calls
must use bounded timeouts and no automatic retries. All requests and retained
storage need prepaid liability in the existing candidate ledger. No cloud
resource, model invocation, retry allowance or success receipt is created here.
"""
from __future__ import annotations

import json
import re
import time

from agent_e2e_worker import CHUNK_BYTES, MAX_REPLY_BYTES, PackError, ReplyChunks


def drain_mcp_replies(sqs, *, reply_queue_url, binding, evidence,
                      timeout_seconds=60, max_polls=256, clock=time.monotonic):
    """Combine retained and queued chunks without a command queue or Worker.

    Expired attempts may be reconciled while their reply queues still exist.
    A timeout/malformed/foreign message or storage failure stays unacknowledged.
    Complete output remains untrusted and must still be independently verified.
    """
    expected = ReplyChunks.from_binding(binding).expected
    if (not isinstance(reply_queue_url, str) or not re.fullmatch(
            r'https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+\.fifo', reply_queue_url)
            or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300
            or type(max_polls) is not int or not 1 <= max_polls <= 2*MAX_REPLY_BYTES//CHUNK_BYTES):
        raise PackError('invalid pending reply recovery configuration')
    deadline = clock()+timeout_seconds
    if evidence.resume(expected) is not None:
        return evidence.recover()
    for _ in range(max_polls):
        remaining = deadline-clock()
        if remaining <= 0:
            break
        response = sqs.receive_message(QueueUrl=reply_queue_url, MaxNumberOfMessages=1,
            WaitTimeSeconds=min(2, max(1, int(remaining))), VisibilityTimeout=30)
        messages = response.get('Messages', [])
        if not isinstance(messages, list) or len(messages) > 1:
            raise PackError('invalid pending worker reply batch')
        if clock() >= deadline:
            break
        for message in messages:
            if (not isinstance(message, dict) or not isinstance(message.get('Body'), str)
                    or len(message['Body'].encode()) > 60000
                    or not isinstance(message.get('ReceiptHandle'), str) or not message['ReceiptHandle']):
                raise PackError('invalid pending worker reply message')
            chunk = json.loads(message['Body'])
            # The binding came from controller history. Validate each message
            # against it even when no header had been published before the crash.
            ReplyChunks.from_binding(expected).accept(chunk)
            evidence(chunk)
            sqs.delete_message(QueueUrl=reply_queue_url, ReceiptHandle=message['ReceiptHandle'])
            if len(evidence.references) == evidence.header['count']:
                return evidence.recover()
    raise PackError('pending worker reply incomplete; retain liability and reconcile without redispatch')
