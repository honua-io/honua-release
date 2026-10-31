"""Runner-owned fault injection on the model's request path.

Every model action reaches the candidate only through the runner: the in-process gateway
(``SessionContext.execute``) or the recording MCP proxy (CLI agents). The injector sits on
that path, in front of the server: once the plan's trigger action has succeeded, the next
matching request is answered with one HTTP 503 instead of being forwarded. Each firing is
recorded (plan id, action digest, synthetic response digest, time) and becomes
``RunRecord.fault_events``; an assertion that needs the fault fails if it never fired.

Plans (``Scenario.fault_plan(variant)``):
  {"kind": "http_503_once",                  # the request never reaches the server
   "id": "<short id>",                       # optional; defaults to the kind
   "after": {"tool": "<tool name>"},         # trigger: first successful call of this tool
   "match": {"tool": "<tool name>"} | null}  # which later request fails; null = the next tool call
  {"kind": "response_lost_after_commit",     # the request IS forwarded and commits; the model gets 503
   "id": "<short id>",
   "after": {"tool": ...} | null,            # optional trigger; null = armed from the start
   "match": {"tool": "<tool name>"}}         # required: the call whose response is lost
The fired event records the upstream status, outcome and response digest next to the injected 503;
the upstream bytes stay in the action log as that action's captured response.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time

KINDS = ('http_503_once', 'response_lost_after_commit')
UNAVAILABLE = b'{"error":{"code":503,"message":"Service Unavailable"}}'


def validate_fault_plan(plan):
    if plan is None:
        return None
    if not isinstance(plan, dict) or plan.get('kind') not in KINDS or set(plan) - {'kind', 'id', 'after', 'match'}:
        raise ValueError('unsupported fault plan')
    after, match = plan.get('after'), plan.get('match')
    lost = plan['kind'] == 'response_lost_after_commit'
    if not (lost and after is None) and (not isinstance(after, dict) or set(after) != {'tool'}
                                         or not isinstance(after['tool'], str) or not after['tool']):
        raise ValueError('fault plan trigger must name one tool: {"after": {"tool": ...}}')
    if (match is None and lost) or (match is not None and (not isinstance(match, dict) or set(match) != {'tool'}
                                                            or not isinstance(match['tool'], str) or not match['tool'])):
        raise ValueError('fault plan match must be {"tool": ...} (null only for http_503_once)')
    identity = plan.get('id', plan['kind'])
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9_.-]{1,40}', identity):
        raise ValueError('invalid fault plan id')
    return {'kind': plan['kind'], 'id': identity, 'after': None if after is None else dict(after),
            'match': None if match is None else dict(match)}


class FaultInjector:
    def __init__(self, plan):
        self.plan = validate_fault_plan(plan)
        self.triggered = self.plan['after'] is None
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _matches(self, spec, kind, action):
        if kind != 'tool_call':
            return False
        return spec is None or action.get('tool') == spec['tool']

    def intercept(self, kind, action):
        """Pre-forward fault (http_503_once): the synthetic 503 for this request when it fires, else None."""
        from agent_e2e_local_recorder import action_bytes
        with self._lock:
            if (self.plan['kind'] != 'http_503_once' or not self.triggered or self.events
                    or not self._matches(self.plan['match'], kind, action)):
                return None
            event = {'kind': self.plan['kind'], 'id': self.plan['id'], 'at': {'after': self.plan['after']},
                     'actionDigest': 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest(),
                     'tool': action.get('tool'), 'httpStatus': 503,
                     'responseSha256': 'sha256:' + hashlib.sha256(UNAVAILABLE).hexdigest(),
                     'firedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            self.events.append(event)
            return event

    def lose(self, kind, action, execution, upstream_status=None, upstream_raw=None):
        """Post-forward fault (response_lost_after_commit): the request reached the server and committed;
        record the upstream outcome and return the event, after which the model is answered with 503."""
        from agent_e2e_local_recorder import action_bytes
        with self._lock:
            if (self.plan['kind'] != 'response_lost_after_commit' or not self.triggered or self.events
                    or not self._matches(self.plan['match'], kind, action)):
                return None
            event = {'kind': self.plan['kind'], 'id': self.plan['id'], 'at': {'after': self.plan['after']},
                     'actionDigest': 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest(),
                     'tool': action.get('tool'), 'forwarded': True,
                     'upstreamOutcome': execution.get('status'), 'upstreamStatus': upstream_status,
                     'upstreamResponseSha256': (None if upstream_raw is None
                                                else 'sha256:' + hashlib.sha256(upstream_raw).hexdigest()),
                     'httpStatus': 503, 'responseSha256': 'sha256:' + hashlib.sha256(UNAVAILABLE).hexdigest(),
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
            captured = execution.get('_rawResponse') if isinstance(execution, dict) else None
            lost = self.lose(kind, action, execution, *(captured or (None, None)))
            self.observe(kind, action, execution)
            if lost is not None:
                answer = {'status': 'fail', 'httpStatus': 503,
                          'result': {'error': {'code': 503, 'message': 'Service Unavailable'}}}
                if captured is not None:
                    answer['_rawResponse'] = captured  # the committed upstream reply, kept as evidence only
                return answer
            return execution
        return run
