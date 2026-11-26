#!/usr/bin/env python3
"""Walk every state-changing route of a booted candidate as an Agent (security-agent-default-deny).

The candidate is the exact image@digest the workflow booted. With the bootstrap administrator key
this tool mints an agent-kind API key, records the state an agent could change, and then acts only
as that agent:

- every operation the candidate's catalog marks other than read-only is submitted through
  POST /api/v1/operations/{id}/submit and must come back as a proposal awaiting one human approval;
- every mutating route in the candidate's admin OpenAPI document, every route on the candidate's
  committed agent route rule list and every route on its governed change-route inventory
  (agent-change-routes.txt) is called directly; an inventory route the walk never called, or a
  missing inventory, leaves the row unchecked. A route without its own agent
  rule must refuse the agent (403); a route with its own rule must not succeed unless the rule says
  it changes nothing.

Identity: every proposal must name the agent key as requestedByAgent, and every direct refusal must
appear in the audit trail with the agent as actor. The administrator state read before the walk must
be unchanged after it. Parameters an operation needs to reach the policy decision come from
certification/agent-safety/operation-fixtures.json; an operation that never reaches the decision is
unchecked, so the row fails closed. A changed snapshot is reported item by item
(agent-safety-report.json stateDiff).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus

import agent_safety_seed
from agent_safety_inventory import INVENTORY, inventory_routes, load_inventory, uncovered
from agent_safety_routes import (ADMIN_PREFIX, FORBIDDEN, GateUnavailable, annotate, openapi_routes, parse_rules,
                                 placeholder_values, probe_routes, rule_routes, succeeded, walk_routes)
from agent_safety_state import state_diff, summary
import rc_gate_window as gate

RECEIPT = Path('security-agent-default-deny.json')
REPORT = Path('agent-safety-report.json')
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'certification/agent-safety/operation-fixtures.json'
RULES = 'tests/dotnet/Honua.Server.Tests/Features/Security/agent-route-rules.txt'
SNAPSHOT_ROUTES = tuple(ADMIN_PREFIX + route for route in (
    '/api-keys', '/roles', '/folders', '/services', '/connections', '/rate-limits', '/oidc/providers',
    '/field-mask-policies', '/rls-policies', '/embed/keys', '/oauth-scopes',
    '/services/${serviceName}/layers/${serviceLayerIndex}/metadata')) + ('/ogc/styles', '/api/v1/console/content')
VOLATILE = ('lastUsedAt', 'updatedAt', 'timestamp')
EXECUTED = {'Succeeded', 'Completed', 'Running', 'Queued', 'Accepted'}
SHOWN = 40
SCOPE = ('operations API catalog; mutating routes in the candidate admin OpenAPI; every route on '
         'agent-route-rules.txt and on agent-change-routes.txt (every governed state-changing route: admin, '
         'Console and administrator-only). Protocol data-edit routes outside the governed set (OGC Features, '
         'GeoServices applyEdits, WFS-T) and GP job submission are out of scope')


class Client:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip('/'), key

    def call(self, method: str, path: str, body=None, headers=None):
        data = body if isinstance(body, bytes) else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(self.base + path, data=data, method=method.upper(), headers={
            'X-API-Key': self.key, 'Idempotency-Key': str(uuid.uuid4()),
            'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def json(self, method: str, path: str, body=None):
        status, raw, _ = self.call(method, path, body)
        if not succeeded(status):
            raise GateUnavailable(f'{method} {path} returned HTTP {status}')
        return json.loads(raw or b'null')


def unwrap(document):
    return document.get('data', document) if isinstance(document, dict) and 'success' in document else document


def mint_agent(admin: Client) -> tuple[str, str]:
    created = unwrap(admin.json('POST', ADMIN_PREFIX + '/api-keys/', {'name': 'agent-safety-gate', 'kind': 'agent'}))
    key, record = created.get('key'), created.get('apiKey') or {}
    if not key or record.get('kind') != 'agent' or 'agent' not in (record.get('permissions') or []):
        raise GateUnavailable('the candidate did not mint an agent-kind API key')
    return key, f"apikey:api-key:{record['id']}"


def snapshot(admin: Client, agent_actor: str, world: dict) -> dict:
    """Administrator view of the state an agent could change; the agent's own key is excluded."""
    state = {}
    for route in SNAPSHOT_ROUTES:
        path = placeholder_values(world, route)
        status, raw, headers = admin.call('GET', path)
        if status != HTTPStatus.OK:
            raise GateUnavailable(f'GET {path} returned HTTP {status}; the state check needs it')
        body = unwrap(json.loads(raw or b'null'))
        if isinstance(body, dict) and (body.get('nextCursor') or body.get('hasMore')):
            raise GateUnavailable(f'GET {path} is paginated; the state check reads complete lists only')
        if route.endswith('/api-keys') and isinstance(body, list):
            body = [row for row in body if f"apikey:api-key:{row.get('id')}" != agent_actor]
        state[route] = {'etag': headers.get('ETag'), 'body': stable(body)}
    return state


def stable(value):
    """Drop the timestamps a read itself refreshes, so only real changes compare unequal."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE}
    return [stable(v) for v in value] if isinstance(value, list) else value


def generated_value(parameter: dict, world: dict) -> str:
    name, kind = parameter['name'], (parameter.get('schema') or {}).get('type')
    enums = (parameter.get('schema') or {}).get('enumValues') or []
    if name == 'If-Match':
        return str(world.get('metadataEtag', ''))
    if enums:
        return str(enums[0])
    by_type = {'WholeNumber': '1', 'DecimalNumber': '1', 'Flag': 'true', 'List': '["agent-safety-gate"]'}
    if kind in by_type:
        return by_type[kind]
    if name == 'id' or name.endswith(('Id', 'ID')):
        return str(uuid.uuid4())
    return 'agent-safety-gate'


def operation_parameters(operation: dict, fixtures: dict, world: dict) -> dict:
    parameters = {p['name']: generated_value(p, world)
                  for p in operation.get('inputSchema', []) if p.get('required')}
    fixture = fixtures.get(operation['operationId'], {})
    parameters.update({name: placeholder_values(world, str(value)) for name, value in fixture.items()})
    return parameters


def judge_submission(status: int, data: dict) -> str:
    if status == FORBIDDEN:
        return 'refused'
    state = data.get('status')
    gated = data.get('policyDecision') == 'RequireApproval'
    if gated and state == 'RequiresApproval' and data.get('proposalId'):
        return 'proposal'
    if gated:
        return 'gated-without-proposal'
    return 'executed' if state in EXECUTED else 'unreached'


def walk_operations(agent: Client, catalog: list[dict], fixtures: dict, world: dict) -> list[dict]:
    results = []
    for operation in catalog:
        if (operation.get('policy') or {}).get('sideEffectClass') == 'ReadOnly':
            continue
        body = {'parameters': operation_parameters(operation, fixtures, world)}
        status, raw, _ = agent.call('POST', f"/api/v1/operations/{operation['operationId']}/submit", body)
        data = unwrap(json.loads(raw)) if raw.strip().startswith(b'{') else {}
        data = data if isinstance(data, dict) else {}
        results.append({'operationId': operation['operationId'], 'status': status,
                        'verdict': judge_submission(status, data), 'proposalId': data.get('proposalId'),
                        'reason': str(data.get('reason') or '')[:160]})
    return results


def proposals_audited(admin: Client, operations: list[dict], actor: str) -> list[str]:
    missing = []
    for result in operations:
        if result['verdict'] != 'proposal':
            continue
        proposal = unwrap(admin.json('GET', f"{ADMIN_PREFIX}/proposals/{result['proposalId']}"))
        if (proposal.get('requestedByAgent') != actor or proposal.get('initiation') != 'Agent'
                or proposal.get('status') != 'AwaitingApproval'):
            missing.append(result['operationId'])
    return missing


def audit_method(item: dict) -> str:
    try:
        details = json.loads(item.get('details') or '{}')
    except ValueError:
        details = {}
    method = details.get('method') if isinstance(details, dict) else None
    return str(method or str(item.get('action', '')).rsplit('.', 1)[-1]).upper()


def audit_entries(admin: Client, actor: str, limit: int = 200) -> set[tuple[str, str]]:
    """(METHOD, path) of every audit record whose agent actor is the gate's key."""
    seen, cursor = set(), ''
    for _ in range(limit):
        page = admin.json('GET', f"{ADMIN_PREFIX}/observability/audit?limit=50{cursor}")
        for item in page.get('items', []):
            if item.get('actorAgent') == actor:
                seen.add((audit_method(item), str(item.get('resourceId', '')).split('?', 1)[0]))
        if not page.get('nextCursor'):
            return seen
        cursor = '&cursor=' + urllib.request.quote(str(page['nextCursor']))
    raise GateUnavailable('the audit trail did not end within the page limit')


CREDENTIAL_PATH_RE = re.compile(
    r'((?:/api/v[0-9]+(?:\.[0-9]+)?/console/share/(?:link|embed)|/wps/conformance/results)/)[^/?#\s]+',
    re.IGNORECASE,
)


def redacted_audit_path(path: str) -> str:
    """Mirror Honua.Core CredentialPathRedaction so audited bearer-token refusals match."""
    return CREDENTIAL_PATH_RE.sub(r'\1[REDACTED]', path)


def refusals_unaudited(routes: list[dict], audited: set) -> list[str]:
    return [f"{r['method']} {r['route']}" for r in routes
            if r['status'] == FORBIDDEN and r['verdict'] in ('refused', 'own-rule')
            and (r['method'], r['path']) not in audited
            and (r['method'], redacted_audit_path(r['path'])) not in audited]


def world_values(admin: Client, seed: dict) -> dict:
    _, _, headers = admin.call('GET', ADMIN_PREFIX + '/folders')
    return {'metadataEtag': headers.get('ETag', ''), **seed}


def decide(operations: list[dict], routes: list[dict], identity: dict, changed: list[str]) -> tuple[dict, dict]:
    unchecked = [o['operationId'] for o in operations if o['verdict'] == 'unreached']
    unchecked += [f"{r['method']} {r['route']} -> {r['status']}" for r in routes if r['verdict'] == 'unchecked']
    direct = [o['operationId'] for o in operations if o['verdict'] in ('executed', 'refused', 'gated-without-proposal')]
    violations = [f"{r['method']} {r['route']} -> {r['status']}" for r in routes if r['verdict'] == 'violation']
    return {
        'live': True,
        'allMutatingRoutesChecked': bool(operations) and bool(routes) and not unchecked,
        'proposalOnly': not direct and not violations and not changed,
        'agentIdentityAudited': not identity['proposals'] and not identity['refusals'],
    }, {'unchecked': unchecked, 'notProposal': direct, 'routeViolations': violations, 'stateChanged': changed}


def receipt(candidate, assertions: dict, findings: dict) -> dict:
    passed = all(assertions.values())
    shown = [f'{kind}: {item}' for kind, items in findings.items() for item in items]
    return {
        'schema': 'honua.release-promise-receipt/v1',
        'row': 'security-agent-default-deny',
        'status': 'PASS' if passed else 'FAIL',
        'serverSha': candidate.server_sha,
        'imageDigest': candidate.server_digest,
        'platformLockDigest': candidate.platform_lock,
        'observedAt': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
        'assertions': assertions,
        'scope': SCOPE,
        'findings': shown[:SHOWN],
    }


def counts(results: list[dict]) -> dict:
    tally = {}
    for result in results:
        tally[result['verdict']] = tally.get(result['verdict'], 0) + 1
    return dict(sorted(tally.items()))


def candidate_from_environment():
    candidate = gate.Candidate(gate.normalize_sha(os.environ.get('SERVER_SHA')),
                               gate.normalize_digest(os.environ.get('SERVER_DIGEST')),
                               gate.normalize_digest(os.environ.get('LOCK_DIGEST')))
    if not all((candidate.server_sha, candidate.server_digest, candidate.platform_lock)):
        raise GateUnavailable('candidate identity is incomplete')
    return candidate


def run(options, candidate) -> tuple[dict, dict]:
    admin = Client(options.base, options.admin_key)
    rules = parse_rules(Path(options.rules).read_text(encoding='utf-8'))
    fixtures = json.loads(Path(options.fixtures).read_text(encoding='utf-8'))
    agent_key, actor = mint_agent(admin)
    agent = Client(options.base, agent_key)
    world = world_values(admin, agent_safety_seed.seed(admin, agent))
    before = snapshot(admin, actor, world)
    catalog = unwrap(admin.json('GET', '/api/v1/operations'))['operations']
    routes = openapi_routes(admin.json('GET', ADMIN_PREFIX + '/openapi.json'))
    routes += rule_routes(rules, {(r['method'], r['template']) for r in routes})
    inventory = load_inventory(Path(options.inventory))
    governed = inventory_routes(inventory or [], {(r['method'], r['template']) for r in routes})
    routes = annotate(routes + governed, rules, fixtures)
    routes += probe_routes(routes, fixtures)
    operations = walk_operations(agent, catalog, fixtures.get('operations', {}), world)
    walked = walk_routes(agent, routes, world)
    walked += uncovered(inventory, walked)
    identity = {'proposals': proposals_audited(admin, operations, actor),
                'refusals': refusals_unaudited(walked, audit_entries(admin, actor))}
    after = snapshot(admin, actor, world)
    diff = state_diff(before, after)
    changed = [summary(route, change) for route, change in diff.items()]
    assertions, findings = decide(operations, walked, identity, changed)
    findings = {**findings, 'proposalNotAudited': identity['proposals'], 'refusalNotAudited': identity['refusals']}
    report = {'operations': operations, 'routes': walked, 'operationCounts': counts(operations),
              'routeCounts': counts(walked), 'inventoryRoutes': len(inventory or []), 'stateDiff': diff,
              'findings': findings}
    return receipt(candidate, assertions, findings), report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('--base', default=os.environ.get('E2E_BASE', 'http://localhost:8080'))
    parser.add_argument('--admin-key', default=os.environ.get('E2E_API_KEY', 'honua-console-dev-key'))
    parser.add_argument('--rules', default=str(Path(os.environ.get('SERVER_CHECKOUT', 'server')) / RULES))
    parser.add_argument('--inventory', default=str(Path(os.environ.get('SERVER_CHECKOUT', 'server')) / INVENTORY))
    parser.add_argument('--fixtures', default=str(FIXTURES))
    options = parser.parse_args(argv)
    RECEIPT.unlink(missing_ok=True)
    try:
        document, report = run(options, candidate_from_environment())
    except (GateUnavailable, agent_safety_seed.SeedFailed, subprocess.CalledProcessError,
            OSError, ValueError, KeyError) as error:
        print(f'Agent safety walk unavailable: {error}', file=sys.stderr)
        return 1
    RECEIPT.write_text(json.dumps(document, indent=2) + '\n')
    REPORT.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'operations': report['operationCounts'], 'routes': report['routeCounts'],
                      'assertions': document['assertions']}))
    for finding in document['findings']:
        print(finding)
    return 0 if document['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
