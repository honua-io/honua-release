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


def seed_saved_map(stack, variant):
    """Use the already-reviewed local Studio fixture; no publication is made."""
    from agent_e2e_local_scenarios.P15 import scenario
    return scenario.seed(stack, variant)


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
