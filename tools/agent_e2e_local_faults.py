"""Runner-owned fault injection on the model's request path.

Every model action reaches the candidate only through the runner: the in-process gateway
(``SessionContext.execute``) or the recording MCP proxy (CLI agents). The injector sits on
that path, in front of the server: once the plan's trigger action has succeeded, the next
matching request is answered with one HTTP 503 instead of being forwarded. Each firing is
recorded (plan id, action digest, synthetic response digest, time) and becomes
``RunRecord.fault_events``; an assertion that needs the fault fails if it never fired.

Plan (``Scenario.fault_plan(variant)``):
  {"kind": "http_503_once",
   "id": "<short id>",                       # optional; defaults to the kind
   "after": {"tool": "<tool name>"},         # trigger: first successful call of this tool
   "match": {"tool": "<tool name>"} | null}  # which later request fails; null = the next tool call
"""
from __future__ import annotations

import hashlib
import re
import threading
import time

KINDS = ('http_503_once',)
UNAVAILABLE = b'{"error":{"code":503,"message":"Service Unavailable"}}'


def validate_fault_plan(plan):
    if plan is None:
        return None
    if not isinstance(plan, dict) or plan.get('kind') not in KINDS or set(plan) - {'kind', 'id', 'after', 'match'}:
        raise ValueError('unsupported fault plan')
    after, match = plan.get('after'), plan.get('match')
    if not isinstance(after, dict) or set(after) != {'tool'} or not isinstance(after['tool'], str) or not after['tool']:
        raise ValueError('fault plan trigger must name one tool: {"after": {"tool": ...}}')
    if match is not None and (not isinstance(match, dict) or set(match) != {'tool'} or not isinstance(match['tool'], str)):
        raise ValueError('fault plan match must be null or {"tool": ...}')
    identity = plan.get('id', plan['kind'])
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9_.-]{1,40}', identity):
        raise ValueError('invalid fault plan id')
    return {'kind': plan['kind'], 'id': identity, 'after': dict(after), 'match': None if match is None else dict(match)}


class FaultInjector:
    def __init__(self, plan):
        self.plan = validate_fault_plan(plan)
        self.triggered = False
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _matches(self, spec, kind, action):
        if kind != 'tool_call':
            return False
        return spec is None or action.get('tool') == spec['tool']

    def intercept(self, kind, action):
        """The synthetic 503 for this request when the armed fault fires, else None."""
        from agent_e2e_local_recorder import action_bytes
        with self._lock:
            if not self.triggered or self.events or not self._matches(self.plan['match'], kind, action):
                return None
            event = {'kind': self.plan['kind'], 'id': self.plan['id'], 'at': {'after': self.plan['after']},
                     'actionDigest': 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest(),
                     'tool': action.get('tool'), 'httpStatus': 503,
                     'responseSha256': 'sha256:' + hashlib.sha256(UNAVAILABLE).hexdigest(),
                     'firedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            self.events.append(event)
            return event

    def observe(self, kind, action, execution):
        """Arm the fault once the trigger action has succeeded."""
        with self._lock:
            if (not self.triggered and self._matches(self.plan['after'], kind, action)
                    and execution.get('status') in ('ok', 'pass')):
                self.triggered = True

    def wrap(self, execute):
        def run(kind, action):
            event = self.intercept(kind, action)
            if event is not None:
                return {'status': 'fail', 'httpStatus': 503,
                        'result': {'error': {'code': 503, 'message': 'Service Unavailable'}}}
            execution = execute(kind, action)
            self.observe(kind, action, execution)
            return execution
        return run
