"""P10 controller-side query replay helpers.

The full local P10 lifecycle is not yet wired; these helpers do not produce a
case receipt. Replay accepts only a digest-bound harness action, never prose.
"""
from __future__ import annotations

import base64
import copy
import hashlib

from agent_e2e_mcp import parse

from agent_e2e_local_runner import read_logged_action
from agent_e2e_p10_observer import query_ids


def replay_query(action_path, action_digest, binding, client):
    """Replay the recorded query once through a controller-authenticated client.

    The caller supplies a fresh initialized controller client and must retain
    its raw observations. The model-selected arguments are preserved verbatim.
    """
    action = read_logged_action(action_path, action_digest)
    if (set(action) != {'kind', 'tool', 'arguments'} or action['kind'] != 'tool_call'
            or action['tool'] != 'honua_query_features' or not isinstance(action['arguments'], dict)):
        raise ValueError('recorded action is not a P10 feature query')
    arguments = copy.deepcopy(action['arguments'])
    if (arguments.get('serviceId') != binding.get('service')
            or arguments.get('layerId') != binding.get('layerId')):
        raise ValueError('recorded query does not target the seeded layer')
    before = len(client.observations)
    result = client.call_tool(action['tool'], arguments)
    rows = client.observations[before:]
    calls = [row for row in rows if row.get('rpcMethod') == 'tools/call']
    if len(calls) != 1 or calls[0].get('httpStatus') != 200:
        raise ValueError('independent query response is missing or ambiguous')
    raw = base64.b64decode(calls[0]['bodyBase64'], validate=True)
    if calls[0].get('bodyDigest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('independent query response digest mismatch')
    if raw.lstrip().startswith(b'{'):
        messages = [parse(raw)]
    else:
        frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
        if frames[-1].strip():
            raise ValueError('incomplete independent query response')
        messages = [parse(data) for frame in frames[:-1]
                    if (data := b'\n'.join(line[5:].lstrip(b' ') for line in frame.split(b'\n')
                                           if line.startswith(b'data:')))]
    responses = [message for message in messages if isinstance(message, dict) and 'result' in message]
    if len(responses) != 1 or responses[0]['result'] != result:
        raise ValueError('independent query projection differs from raw response')
    observed = query_ids(responses[0]['result'], arguments, binding)
    return observed, {'replayedRequestDigest': action_digest, 'controllerResponses': copy.deepcopy(calls)}


class P10:
    """Filter-boundary scenario; requires controller HTTP and MCP adapters in Stack.extra."""
    id = 'P10'
    supported_variants = ('A', 'B')

    def fault_plan(self, variant): return None
    def needs_approver(self): return False
    def sessions_required(self): return 1

    def seed(self, stack, variant):
        from pathlib import Path
        from agent_e2e_local_api import Seeded
        from agent_e2e_scenario_backend import TownSetup
        from agent_e2e_p10_observer import source_content, source_path
        if variant not in self.supported_variants:
            raise ValueError('unsupported P10 variant')
        if 'p10Seed' in stack.extra:
            if stack.extra['p10Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p10Seed'])
        http = stack.extra['controllerHttp']
        namespace = 'p10_' + stack.compose_project.replace('-', '')[-16:]
        manifest = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus/manifest.v1.json'
        town = TownSetup(http, manifest).prepare(variant=variant, namespace=namespace,
            starting_state='published-town', connection_id=stack.extra['connectionId'])
        binding = town['layers']['shelters']
        before = http.json('p10-source-before', 'GET', source_path(binding))
        snapshot = source_content(before)
        seed = {'variant': variant, 'town': town, 'sourceBefore': snapshot,
                'sourceBeforeResponse': before['observation']}
        stack.extra['p10Seed'] = seed
        return Seeded(seed)

    def expected(self, variant):
        from agent_e2e_local_runner import oracle
        if variant not in self.supported_variants:
            raise ValueError('unsupported P10 variant')
        fixture = oracle.fixture_expectation(variant, 'features.exact', layer='shelters')
        features = sorted(fixture['features'], key=lambda f: f['properties']['source_id'])
        resource = {'resourceId': 'shelters', 'content': {'srid': 3857, 'features': features}}
        return {'filter.exact': oracle.fixture_expectation(variant, 'filter.exact'),
                'source-content.unchanged': {'profile': 'honua.protected-content/v1', 'resources': [resource]}}

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_local_api import Observation
        from agent_e2e_p10_observer import source_content, source_path
        if run is None or run.sessions != 1:
            raise ValueError('missing P10 model run')
        binding = seeded.state['town']['layers']['shelters']
        http = stack.extra['controllerHttp']
        after = http.json('p10-source-after', 'GET', source_path(binding))
        after_snapshot = source_content(after)
        preservation = {'profile': 'honua.protected-content/v1', 'before': [seeded.state['sourceBefore']],
                        'after': [after_snapshot]}
        # Preserving a model-authored map/filter requires a controller artifact
        # inspector; until it exists, any non-read tool fails closed.
        logs = stack.extra.get('actionLogs', [])
        selected = None
        for row in logs:
            action = read_logged_action(row['logPath'], row['digest'])
            name = action.get('tool', '')
            if not name.startswith(('honua_get_', 'honua_list_', 'honua_query_', 'honua_search_')):
                raise ValueError('persisted artifact inspection is not implemented')
            if (name == 'honua_query_features' and action['arguments'].get('serviceId') == binding['service']
                    and action['arguments'].get('layerId') == binding['layerId']):
                selected = row
        if selected is None:
            raise ValueError('no model-selected query of the seeded layer')
        client = stack.extra['controllerMcpFactory']()
        client.initialize()
        try:
            projected, replay = replay_query(selected['logPath'], selected['digest'], binding, client)
        finally:
            client.close()
        def response_row(answer, url):
            return {'method': 'GET', 'url': url, 'status': answer['httpStatus'],
                    'sha256': answer['responseDigest'], 'body_b64': answer['rawBodyBase64']}
        before_row = response_row(seeded.state['sourceBeforeResponse'], source_path(binding))
        after_row = response_row(after['observation'], source_path(binding))
        call = replay['controllerResponses'][0]
        query_row = {'method': 'POST', 'url': '/mcp', 'status': call['httpStatus'],
                     'sha256': call['bodyDigest'], 'body_b64': call['bodyBase64']}
        return [Observation('filter.exact', projected,
                            {'requests': [query_row], 'projection': 'p10-query/v1', 'binding': binding,
                             'action_b64': base64.b64encode(open(selected['logPath'], 'rb').read()).decode(),
                             'replayedRequestDigest': replay['replayedRequestDigest']}),
                Observation('source-content.unchanged', preservation, {'requests': [before_row, after_row], 'projection': 'p10-source/v1'})]

    def cleanup(self, stack, seeded):
        # The fresh per-case stack owns all P10 fixtures and is removed by the runner.
        pass


scenario = P10()


def project_evidence(assertion, evidence, raw_bodies):
    """Recompute the oracle input from retained controller response bytes."""
    import json
    from agent_e2e_p10_observer import source_content
    if assertion == 'source-content.unchanged' and evidence.get('projection') == 'p10-source/v1':
        if len(raw_bodies) != 2 or any(r['status'] != 200 or r['method'] != 'GET' for r in evidence['requests']):
            raise ValueError('source reads are missing or failed')
        rows = [source_content({'observation': {'httpStatus': 200, 'body': json.loads(raw)}}) for raw in raw_bodies]
        return {'profile': 'honua.protected-content/v1', 'before': [rows[0]], 'after': [rows[1]]}
    if assertion == 'filter.exact' and evidence.get('projection') == 'p10-query/v1':
        if len(raw_bodies) != 1 or evidence['requests'][0]['status'] != 200:
            raise ValueError('replay response missing or failed')
        action_raw = base64.b64decode(evidence['action_b64'], validate=True)
        if 'sha256:' + hashlib.sha256(action_raw).hexdigest() != evidence['replayedRequestDigest']:
            raise ValueError('replayed action digest mismatch')
        action = json.loads(action_raw)
        from agent_e2e_local_runner import action_bytes
        if action_bytes(action) != action_raw or action.get('kind') != 'tool_call' or action.get('tool') != 'honua_query_features':
            raise ValueError('invalid replayed action')
        binding = evidence['binding']
        arguments = action.get('arguments')
        if (not isinstance(arguments, dict) or arguments.get('serviceId') != binding.get('service')
                or arguments.get('layerId') != binding.get('layerId')):
            raise ValueError('replayed action does not target seeded layer')
        raw = raw_bodies[0]
        if raw.lstrip().startswith(b'{'):
            messages = [parse(raw)]
        else:
            frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
            if frames[-1].strip():
                raise ValueError('incomplete query response')
            messages = [parse(data) for frame in frames[:-1]
                        if (data := b'\n'.join(line[5:].lstrip(b' ') for line in frame.split(b'\n')
                                               if line.startswith(b'data:')))]
        responses = [item for item in messages if isinstance(item, dict) and 'result' in item]
        if len(responses) != 1:
            raise ValueError('query response is ambiguous')
        return query_ids(responses[0]['result'], action['arguments'], evidence['binding'])
    raise ValueError('unrecognized P10 evidence projection')
