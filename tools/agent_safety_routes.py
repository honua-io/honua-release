"""Direct route calls by an agent against a booted candidate, and how each answer is judged.

A governed route without its own agent rule must refuse the agent (AgentRouteAdmissionMiddleware
answers 403 before the body is read). A route with its own rule (an AgentApprovalFilter, which runs
after binding) counts only when its answer shows the rule decided. A documented route the candidate
does not map counts only when the fixtures give the reason. Fixtures supply path values and bodies
so a request reaches the decision; ${name} placeholders come from the seeded state.
"""
from __future__ import annotations

from http import HTTPStatus
import json
import re
import uuid

ADMIN_PREFIX = '/api/v1/admin'
MUTATING = ('post', 'put', 'patch', 'delete')
# A rule reason that admits a 2xx: "Changes nothing." or "<does something> and changes (or saves) nothing; ...".
CHANGES_NOTHING = re.compile(r'^(Changes nothing\b|[^;.|]*\band (changes|saves) nothing\b)')
RULE_DECIDED = re.compile(rb'operator\.agent\.|"proposalId"')
SIGN_IN_PREFIX = '/api/v1/admin/auth/'
NO_ENDPOINT = (HTTPStatus.NOT_FOUND, HTTPStatus.METHOD_NOT_ALLOWED, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
FORBIDDEN = HTTPStatus.FORBIDDEN
SERVER_ERROR = HTTPStatus.INTERNAL_SERVER_ERROR


def succeeded(status: int) -> bool:
    return HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES


class GateUnavailable(Exception):
    pass


def parse_rules(text: str) -> dict:
    """'<METHOD> <template> | <reason>' lines -> {(METHOD, normalized path): reason}."""
    rules = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        method, rest = line.split(' ', 1)
        template, reason = (part.strip() for part in rest.split(' | ', 1))
        rules[(method.upper(), normalize(template))] = reason
    return rules


def normalize(template: str) -> str:
    path = re.sub(r'\{([^}:?]+)[^}]*\}', r'{\1}', template)
    return re.sub(r'^/api/v\{[^}]+\}', '/api/v1', path).rstrip('/') or '/'


def path_value(name: str, parameter: dict, world: dict) -> str:
    """A seeded value of the same name, else one of the declared type, so the route matches."""
    if name in world:
        return str(world[name])
    schema = parameter.get('schema') or {}
    if schema.get('type') == 'integer':
        return '1'
    return str(uuid.uuid4()) if schema.get('format') == 'uuid' or schema.get('type') != 'string' else 'agent-safety-gate'


def openapi_routes(spec: dict) -> list[dict]:
    routes = []
    for template, item in sorted(spec.get('paths', {}).items()):
        for method, operation in item.items():
            if method not in MUTATING:
                continue
            parameters = {p['name']: p for p in operation.get('parameters', []) if p.get('in') == 'path'}
            content = list((operation.get('requestBody') or {}).get('content', {}) or ['application/json'])
            routes.append({'method': method.upper(), 'template': normalize(ADMIN_PREFIX + template),
                           'parameters': parameters, 'contentType': content[0],
                           'anonymous': operation.get('security') == []})
    return routes


def annotate(routes: list[dict], rules: dict, fixtures: dict) -> list[dict]:
    """Attach the candidate's own agent rule, the route fixture and any declared reason it is not mapped."""
    for route in routes:
        key = f"{route['method']} {route['template']}"
        route['rule'] = rules.get((route['method'], route['template']))
        route['fixture'] = fixtures.get('routes', {}).get(key, {})
        route['unmappedReason'] = fixtures.get('unmapped', {}).get(key)
    return routes


def probe_routes(routes: list[dict], fixtures: dict) -> list[dict]:
    """Further requests on a route already walked, for a rule that refuses only some changes."""
    by_key = {f"{r['method']} {r['template']}": r for r in routes}
    probes = []
    for probe in fixtures.get('probes', []):
        if probe['route'] not in by_key:
            raise GateUnavailable(f"probe route {probe['route']} is not a walked route")
        probes.append({**by_key[probe['route']], 'fixture': probe})
    return probes


def rule_routes(rules: dict, known: set) -> list[dict]:
    return [{'method': method, 'template': template, 'parameters': {}, 'contentType': 'application/json',
             'anonymous': False} for method, template in sorted(rules) if (method, template) not in known]


def concrete(route: dict, world: dict) -> str:
    values = {**world, **{k: placeholder_values(world, str(v)) for k, v in route['fixture'].get('path', {}).items()}}

    def value(match):
        name = match.group(1)
        return path_value(name, route['parameters'].get(name, {'schema': {'format': 'uuid'}}), values)
    return re.sub(r'\{([^}]+)\}', value, route['template'])


def request_body(route: dict, world: dict) -> bytes:
    if 'body' in route['fixture']:
        body = route['fixture']['body']
        return placeholder_values(world, body if isinstance(body, str) else json.dumps(body)).encode()
    if route['contentType'].startswith('multipart/'):
        return b''
    return b'<agent-safety-gate/>' if 'xml' in route['contentType'] else b'{}'


def judge_default_route(route: dict, status: int, raw: bytes) -> str:
    """A governed route without its own rule: the admission middleware must refuse the agent."""
    if status == FORBIDDEN:
        return 'refused'
    if route['anonymous'] and route['template'].startswith(SIGN_IN_PREFIX) and not succeeded(status):
        return 'anonymous'
    if status in NO_ENDPOINT and not raw.strip():
        return 'unmapped' if route['unmappedReason'] else 'unchecked'
    return 'violation'


def judge_own_rule(route: dict, status: int, raw: bytes) -> str:
    """A route with its own agent rule passes only when the rule visibly decided."""
    if CHANGES_NOTHING.match(route['rule']) and status < SERVER_ERROR:
        return 'changes-nothing'
    if succeeded(status) and b'"proposalId"' in raw:
        return 'own-rule'
    if succeeded(status) or status >= SERVER_ERROR:
        return 'violation'
    return 'own-rule' if status == FORBIDDEN or RULE_DECIDED.search(raw) else 'unchecked'


def judge_route(route: dict, status: int, raw: bytes) -> str:
    return judge_default_route(route, status, raw) if route['rule'] is None else judge_own_rule(route, status, raw)


def walk_routes(agent, routes: list[dict], world: dict) -> list[dict]:
    results = []
    for route in routes:
        path = concrete(route, world)
        content_type = route['fixture'].get('contentType', route['contentType'])
        status, raw, _ = agent.call(route['method'], path, request_body(route, world),
                                    {'Content-Type': content_type, 'If-Match': str(world.get('metadataEtag', '"0"'))})
        results.append({'method': route['method'], 'route': route['template'], 'path': path,
                        'status': status, 'verdict': judge_route(route, status, raw)})
    return results


def placeholder_values(world: dict, text: str) -> str:
    def value(match):
        name = match.group(1)
        if name == 'uuid':
            return str(uuid.uuid4())
        if name not in world:
            raise GateUnavailable(f'fixture placeholder ${{{name}}} has no seeded value')
        return str(world[name])
    return re.sub(r'\$\{([A-Za-z0-9_]+)\}', value, text)
