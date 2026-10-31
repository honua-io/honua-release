"""Unavailable governance assertions fail closed; never provide synthetic evidence.

The current runner cannot freeze the dynamic identities needed by P14/P16/P17/
P20. Their modules retain this inventory while support is blocked. P15 implements
its own recorder observations. P18 is excluded by the frozen preregistration.
"""
from agent_e2e_local_api import Observation
from agent_e2e_local_expectations import load_expected, load_roles


def role_bindings(scenario_id, seeded):
    return load_roles(scenario_id)


def observe_partial(scenario_id, stack, seeded, variant):
    return [Observation(assertion, None, {'projection': 'extract/v1'})
            for assertion in load_expected(scenario_id, variant)]


def seed_town(stack, variant, namespace):
    """Import synchronous fixtures without relying on an unreviewed table inventory.

    The product's synchronous response names the physical table. A 202 is not
    silently adopted: TownSetup refuses it when no independently reviewed table
    discovery was retained. The fresh case stack owns every fixture mutation.
    """
    from pathlib import Path
    from agent_e2e_scenario_backend import TownSetup
    from agent_e2e_scenario_operations import connection_id, data, publication_request, submit_publication
    recorder = stack.extra['recorder']
    connection = stack.extra.get('connectionId')
    if connection is None:
        connection = stack.extra['ensureConnection']()
        stack.extra['connectionId'] = connection
    connection = connection_id(connection)
    observed = data(recorder.json('observe-connection', 'GET', '/api/v1/admin/connections/' + connection))
    if observed.get('connectionId', observed.get('id')) != connection:
        raise ValueError('observed connection differs from the fixture target')
    root = Path(__file__).resolve().parents[1]
    town = TownSetup(recorder, root / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
        variant=variant, namespace=namespace, starting_state='imported-town')
    for name, geometry in TownSetup.SOURCES.items():
        binding = town['layers'][name]
        service = namespace + '-' + name.replace('_', '-')
        result = submit_publication(recorder, publication_request(connection=connection,
            schema=binding['schema'], table=binding['table'], service=service, geometry_type=geometry, srid=3857),
            step='publish-' + name.replace('_', '-'))
        if result['status'] != 'completed':
            raise ValueError('fixture publication needs a separately resolved approval')
        binding.update(service=service, layerId=result['layerId'])
    return town


def seed_saved_map(stack, variant, *, package=None, namespace_prefix='g4'):
    """Create an unpublished, case-owned Studio map through recorded APIs."""
    import copy
    import json
    import re
    from pathlib import Path
    from agent_e2e_local_api import Seeded
    from agent_e2e_p10_observer import source_path
    from agent_e2e_scenario_operations import data
    from agent_e2e_studio_observer import guid
    cache = stack.extra.get('g4MapSeed')
    if cache is not None:
        if cache['variant'] != variant or cache['packageKey'] != (package or ('kestrel' if variant == 'A' else 'willow') + '-map'):
            raise ValueError('case already seeded for a different variant or package')
        return Seeded(copy.deepcopy(cache))
    if stack.extra.get('g4MapSeedStarted'):
        raise ValueError('incomplete seed; reconcile the original case without retry')
    stack.extra['g4MapSeedStarted'] = True
    rec = stack.extra['recorder']
    namespace = namespace_prefix + '_' + stack.compose_project.replace('-', '')[-16:]
    town = seed_town(stack, variant, namespace)
    stack.extra['addPrincipal']('operator', ['read:*'])
    binding = town['layers']['shelters']
    root = Path(__file__).resolve().parents[1]
    body = json.loads((root / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']
    name = package or ('kestrel' if variant == 'A' else 'willow') + '-map'
    body['mapPackageId'] = name
    body['sourceBindings'] = [{'sourceId': 'shelters', 'protocol': 'ogc_features',
                               'locator': {'collectionId': str(binding['layerId'])}}]
    body['mapSpec']['sources'] = {'shelters': {'type': 'geojson',
        'data': '/ogc/features/collections/' + str(binding['layerId']) + '/items'}}
    body['mapSpec']['layers'] = [{'id': 'shelters', 'source': 'shelters', 'type': 'circle',
                                 'paint': {'circle-color': '#ef2020'}}]
    envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1', 'body': body}
    draft = data(rec.json('p15-map-draft', 'POST', '/api/v1/studio/package-drafts', {
        'packageKey': name, 'workspaceId': namespace, 'ownerId': rec.principals['agent'][1],
        'envelope': envelope}), (201,))
    saved = data(rec.json('p15-map-saved', 'POST', '/api/v1/studio/package-drafts/'
                          + guid(draft['draftId']) + '/content-versions', {}), (201,))
    if (saved.get('itemId') != draft.get('itemId') or saved.get('ownerId') != rec.principals['agent'][1]
            or saved.get('packageKey') != name or not isinstance(saved.get('contentHash'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', saved['contentHash'])):
        raise ValueError('saved map does not match the seeded item, owner, package and content hash')
    rec.json('p15-source-before', 'GET', source_path(binding))
    seed = {'variant': variant, 'itemId': guid(saved['itemId']), 'versionId': guid(saved['versionId']),
            'contentHash': saved['contentHash'], 'packageKey': name, 'shelters': binding}
    stack.extra['g4MapSeed'] = copy.deepcopy(seed)
    return Seeded(seed)


def source_and_custody(stack, seeded):
    """Fresh reads for protected source and bootstrap revocation."""
    from agent_e2e_p10_observer import source_path
    recorder = stack.extra['recorder']
    recorder.json('p15-source-after', 'GET', source_path(seeded.state['shelters']))
    recorder.json('p15-bootstrap-probe', 'GET', '/api/v1/admin/api-keys/', principal='bootstrap')


def publication_actions(recorder, state):
    """Select only sealed requests for the exact independently seeded saved version."""
    return [action for action in recorder.actions()
            if action.kind == 'tool_call' and action.tool == 'honua_studio_propose_publication'
            and action.status == 'pass' and isinstance(action.arguments, dict)
            and all(action.arguments.get(k) == state[k] for k in ('itemId', 'versionId', 'contentHash'))]


def observe_native_proposal(stack, seeded, prefix):
    """Capture the server reply as a locator and read the exact native proposal afresh.

    This partial read does not bind a scored created role or assert approval. The
    recorder verifies the captured result's action occurrence and response digest.
    """
    import re
    rec = stack.extra['recorder']
    selected = publication_actions(rec, seeded.state)
    if len(selected) != 1:
        return None
    result = rec.action_result(prefix + '-action-result', selected[0])
    output = result.json.get('result', {}).get('structuredContent', {}) if isinstance(result.json, dict) else {}
    proposal_id = output.get('proposalId') if isinstance(output, dict) else None
    if not isinstance(proposal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', proposal_id):
        return None
    request = selected[0].arguments
    from agent_e2e_studio_observer import route_path
    route_path(request.get('route'))
    if request.get('visibility') != 'public':
        raise ValueError('native proposal action did not request public visibility')
    answer = rec.json(prefix + '-proposal', 'GET', '/api/v1/admin/proposals/' + proposal_id)
    value = answer['observation']['body']
    expected = [f'{key}={entry}' for key, entry in (
        ('itemId', seeded.state['itemId']), ('versionId', seeded.state['versionId']),
        ('contentHash', seeded.state['contentHash']), ('route', request['route']), ('visibility', 'public'))]
    if (answer['observation']['httpStatus'] != 200 or not isinstance(value, dict)
            or value.get('proposalId') != proposal_id or value.get('kind') != 'StudioDraftMutation'
            or value.get('requestedBy') != rec.principals['agent'][1] or value.get('diff') != expected):
        raise ValueError('native proposal differs from the sealed request and seeded map')
    return answer
