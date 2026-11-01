"""Scripted customer for the Console's own Studio assistant.

The browser gateway owns and records every interaction. This module does not
send a request to Bedrock or provide credentials to the chat.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request

from agent_e2e_local_api import SessionResult
from agent_studio_customer import CustomerScript, CustomerScriptError

MODEL = 'us.anthropic.claude-sonnet-4-6'
DONE = re.compile(r'\b(done|completed|finished|saved)\b', re.IGNORECASE)
NOT_DONE = re.compile(r"\b(?:not|never|cannot|can't|unable\s+to)\b(?:\s+\w+){0,3}\s+"
                      r"(?:done|completed|finished|saved)\b", re.IGNORECASE)


def rpc(endpoint, name, arguments):
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': name, 'arguments': arguments}}).encode()
    request = urllib.request.Request(endpoint.url, data=payload,
        headers={endpoint.header: endpoint.token, 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=45) as response:
        message = json.load(response)
    result = message.get('result')
    if message.get('error') or not isinstance(result, dict) or result.get('isError'):
        raise CustomerScriptError('recorded Studio browser action failed')
    content = result.get('content')
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0].get('text'), str):
        raise CustomerScriptError('recorded Studio browser reply is malformed')
    return content[0]['text']


def run_customer(*, invoke, prompt, script, timeout=2700, clock=time.monotonic, sleep=time.sleep):
    """Drive only the customer's frozen turns; chat state is never scoring evidence."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8192:
        raise CustomerScriptError('customer request is missing or exceeds the browser input bound')
    policy = CustomerScript(script)
    end = clock() + min(timeout, 2700)
    turns, nudges, seen = 0, 0, 0
    digests = []

    def send(text, continuation=False):
        nonlocal turns, nudges
        if turns >= 60 or clock() >= end:
            raise CustomerScriptError('customer turn or time bound exceeded')
        invoke('studio_customer_type', {'selector': '[data-testid="studio-chat-input"]', 'text': text})
        invoke('studio_click', {'selector': '[data-testid="studio-chat-send"]'})
        turns += 1
        nudges += int(continuation)
        digests.append('sha256:' + hashlib.sha256(text.encode()).hexdigest())

    invoke('studio_navigate', {'url': '/studio'})
    try:
        initial = json.loads(invoke('studio_chat_observe', {}))
    except (ValueError, TypeError) as exc:
        raise CustomerScriptError('initial Studio chat state is malformed') from exc
    if (not isinstance(initial, dict) or type(initial.get('assistantMessages')) is not int
            or initial.get('streaming') is not False):
        raise CustomerScriptError('Studio chat is unavailable or already busy')
    seen = initial['assistantMessages']
    send(prompt)
    last_progress = clock()
    while clock() < end:
        try:
            state = json.loads(invoke('studio_chat_observe', {}))
        except (ValueError, TypeError) as exc:
            raise CustomerScriptError('Studio chat state is malformed') from exc
        if (not isinstance(state, dict) or type(state.get('assistantMessages')) is not int
                or type(state.get('modelCalls')) is not int or type(state.get('dispatchedToolCalls')) is not int
                or type(state.get('streaming')) is not bool or type(state.get('boundHit')) is not bool
                or not isinstance(state.get('text'), str)):
            raise CustomerScriptError('Studio chat state is incomplete')
        if state['boundHit'] or state['modelCalls'] > 60 or state['dispatchedToolCalls'] > 60:
            raise CustomerScriptError('assistant model or tool call bound exceeded')
        if state['assistantMessages'] > seen and not state['streaming']:
            if state.get('status') != 'complete':
                raise CustomerScriptError('Studio assistant turn did not complete')
            seen = state['assistantMessages']
            text = state['text'].strip()
            if not text:
                raise CustomerScriptError('Studio assistant returned an empty turn')
            event = 'question' if '?' in text else 'done' if DONE.search(text) and not NOT_DONE.search(text) else 'progress'
            decision = policy.reply(event)
            last_progress = clock()
            if decision['kind'] == 'done':
                return {'answer': text, 'turns': turns, 'nudges': nudges, 'inputDigests': digests,
                        'modelCalls': state['modelCalls'], 'toolCalls': state['dispatchedToolCalls']}
            if decision['kind'] == 'send':
                send(decision['text'], decision['continuation'])
        elif not state['streaming'] and clock() - last_progress >= 60:
            decision = policy.reply('stall')
            send(decision['text'], decision['continuation'])
            last_progress = clock()
        sleep(0.5)
    raise CustomerScriptError('Studio customer session reached its time limit')


class StudioCustomerSession:
    agent_type = 'console-assistant'

    def __init__(self, runtime, stack):
        self.runtime = runtime

    def run(self, context):
        if context.studio_browser is None:
            return SessionResult(self.agent_type, '', None, failure={
                'category': 'infrastructure', 'reason': 'Studio browser is unavailable'})
        try:
            endpoint = context.studio_browser()
            result = run_customer(invoke=lambda name, args: rpc(endpoint, name, args), prompt=context.prompt,
                                  script=self.runtime.studio_customer_script,
                                  timeout=min(getattr(self.runtime, 'timeout', 2700), 2700))
        except (CustomerScriptError, OSError, ValueError) as exc:
            return SessionResult(self.agent_type, '', None, failure={'category': 'model', 'reason': str(exc)})
        # The model and token usage must be filled from controller audit records, not this declaration.
        return SessionResult(self.agent_type, '', result['answer'], nudges=result['nudges'],
                             model_visible_digests=tuple(result['inputDigests']),
                             extra={'observedModelCalls': result['modelCalls'], 'observedToolCalls': result['toolCalls']})
