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
   "match": {"tool": "<tool name>"},         # required: the call whose response is lost
   "commit": {"pointer": "/result/structuredContent/mapId",
              "confirm": {"path": "/api/v1/studio/content-items/{id}", "attempts": 30, "intervalSeconds": 1}}}
  It fires only on a VERIFIED commit: the upstream reply is a non-error JSON-RPC result (2xx) naming the
  resource at the commit pointer, AND a runner controller read of the confirm path (with {id} substituted)
  answers 200, polled up to "attempts" times so an accepted-but-pending (e.g. 202) creation is only counted
  once it exists. A rejected or erroring reply, or one whose resource never appears, is forwarded to the model
  unchanged, listed in receipt.faultSkipped with the reason, and the fault stays armed.
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
    if not isinstance(plan, dict) or plan.get('kind') not in KINDS or set(plan) - {'kind', 'id', 'after', 'match', 'commit'}:
        raise ValueError('unsupported fault plan')
    after, match = plan.get('after'), plan.get('match')
    lost = plan['kind'] == 'response_lost_after_commit'
    if not (lost and after is None) and (not isinstance(after, dict) or set(after) != {'tool'}
                                         or not isinstance(after['tool'], str) or not after['tool']):
        raise ValueError('fault plan trigger must name one tool: {"after": {"tool": ...}}')
    if (match is None and lost) or (match is not None and (not isinstance(match, dict) or set(match) != {'tool'}
                                                            or not isinstance(match['tool'], str) or not match['tool'])):
        raise ValueError('fault plan match must be {"tool": ...} (null only for http_503_once)')
    commit = plan.get('commit')
    if lost != (commit is not None) or (lost and (not isinstance(commit, dict) or set(commit) != {'pointer', 'confirm'}
                                                  or not isinstance(commit['pointer'], str)
                                                  or not commit['pointer'].startswith('/'))):
        raise ValueError('response_lost_after_commit needs {"commit": {"pointer": "/result/...", "confirm": {...}}}')
    if lost:
        confirm = commit['confirm']
        if (not isinstance(confirm, dict) or set(confirm) - {'path', 'attempts', 'intervalSeconds'}
                or not isinstance(confirm.get('path'), str) or not confirm['path'].startswith('/api/')
                or confirm['path'].count('{id}') != 1
                or not (type(confirm.get('attempts', 30)) is int and 1 <= confirm.get('attempts', 30) <= 120)
                or not (type(confirm.get('intervalSeconds', 1)) in (int, float) and 0 <= confirm.get('intervalSeconds', 1) <= 10)):
            raise ValueError('commit.confirm needs a controller read path with one {id}, attempts 1-120, interval 0-10 s')
    identity = plan.get('id', plan['kind'])
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9_.-]{1,40}', identity):
        raise ValueError('invalid fault plan id')
    out = {'kind': plan['kind'], 'id': identity, 'after': None if after is None else dict(after),
           'match': None if match is None else dict(match)}
    if lost:
        out['commit'] = {'pointer': commit['pointer'],
                         'confirm': {'path': commit['confirm']['path'], 'attempts': commit['confirm'].get('attempts', 30),
                                     'intervalSeconds': commit['confirm'].get('intervalSeconds', 1)}}
    return out


class FaultInjector:
    def __init__(self, plan, confirm=None):
        self.plan = validate_fault_plan(plan)
        # confirm(path) -> (http status, recorded step): a runner controller read, recorded in the vault.
        self.confirm = confirm
        self.triggered = self.plan['after'] is None
        self.events: list[dict] = []
        self.skipped: list[dict] = []
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
            digest = 'sha256:' + hashlib.sha256(action_bytes(action)).hexdigest()

            def skip(reason, **extra):
                # Not a verified commit: deliver the original reply and leave the fault armed.
                self.skipped.append({'actionDigest': digest, 'upstreamStatus': upstream_status,
                                     'upstreamOutcome': execution.get('status'), 'reason': reason, **extra})
                return None
            committed = self._committed_resource(execution, upstream_status, upstream_raw)
            if committed is None:
                return skip('reply is an error or names no resource at the commit pointer')
            if self.confirm is None:
                return skip('no runner controller read is available to verify the commit')
            from urllib.parse import quote
            confirm = self.plan['commit']['confirm']
            path = confirm['path'].replace('{id}', quote(str(committed), safe=''))
            checks = []
            for attempt in range(confirm['attempts']):
                status, step = self.confirm(path)
                checks.append({'step': step, 'status': status})
                if status == 200:
                    break
                if attempt + 1 < confirm['attempts']:
                    time.sleep(confirm['intervalSeconds'])
            if not checks or checks[-1]['status'] != 200:
                return skip('the resource never appeared to a controller read', confirmation={'path': path, 'checks': checks})
            event = {'kind': self.plan['kind'], 'id': self.plan['id'], 'at': {'after': self.plan['after']},
                     'actionDigest': digest,
                     'tool': action.get('tool'), 'forwarded': True, 'committedResourceId': committed,
                     'confirmation': {'path': path, 'checks': checks},
                     'upstreamOutcome': execution.get('status'), 'upstreamStatus': upstream_status,
                     'upstreamResponseSha256': (None if upstream_raw is None
                                                else 'sha256:' + hashlib.sha256(upstream_raw).hexdigest()),
                     'httpStatus': 503, 'responseSha256': 'sha256:' + hashlib.sha256(UNAVAILABLE).hexdigest(),
                     'firedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            self.events.append(event)
            return event

    def _committed_resource(self, execution, status, raw):
        from agent_e2e_local_projections import pointer_get
        from agent_e2e_local_recorder import ProjectionError, parse_jsonrpc
        if execution.get('status') != 'ok' or type(status) is not int or not 200 <= status < 300 or raw is None:
            return None
        try:
            message = parse_jsonrpc(raw)
            result = message.get('result')
            if 'error' in message or not isinstance(result, dict) or result.get('isError') is True:
                return None
            value = pointer_get(message, self.plan['commit']['pointer'])
        except (ProjectionError, ValueError):
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == '':
            return None
        return value

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
