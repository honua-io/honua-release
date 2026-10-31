"""Controller-recorded partial governance observations for the g4 scenarios.

These builders use only operations already implemented by the core projection
engine. They contain no observed values and do not mint identities or fixtures.
The cases remain unsupported until the missing native seed and custody evidence
is available; these projections can then be exercised without a new registry op.
"""
from __future__ import annotations

from agent_e2e_local_api import Observation, RoleBinding
from agent_e2e_local_expectations import load_expected

ITEM = 'seeded:map:item'
VERSION = 'seeded:map:version'
OPERATOR = 'seeded:principal:operator'
OWNER = 'seeded:principal:owner'
AGENT = 'seeded:principal:agent'


def permission_matrix(steps, identity_steps=()):
    """Bind each response to the actual requested map and its recorder principal."""
    result = {'object': {'requests': {'each': list(steps), 'to': {'object': {
        'principalId': {'principal': '$step'},
        'method': {'method': '$step'},
        'resourceId': {'target': '$step', 'role': ITEM},
        'versionId': {'target': '$step', 'role': VERSION},
        'status': {'status': '$step'},
        'responseSha256': {'sha256': '$step'},
        'disclosedResourceIds': {'disclosed': '$step', 'roles': [ITEM]},
    }}}}}
    if identity_steps:
        result['object']['nativeIdentities'] = {'list': [
            {'body': step, 'pointer': ''} for step in identity_steps]}
    return result


def require_exact_positive(answer, item, version):
    response = answer['observation']
    value = response.get('body')
    value = value.get('data') if isinstance(value, dict) else None
    if (response['httpStatus'] != 200 or not isinstance(value, dict) or
            value.get('itemId') != item or value.get('versionId') != version):
        raise ValueError('positive control did not return the exact saved map version')


def observation(assertion, spec):
    return Observation(assertion, None, {'projection': 'extract/v1', 'spec': spec})


def role_bindings(scenario_id, seeded):
    if scenario_id not in ('P15', 'P18', 'P20') or not seeded.state:
        return []
    out = [RoleBinding(ITEM, f'{scenario_id.lower()}-map-saved', pointer='/data/itemId'),
           RoleBinding(VERSION, f'{scenario_id.lower()}-map-saved', pointer='/data/versionId')]
    if scenario_id in ('P15', 'P20'):
        out.append(RoleBinding(OPERATOR, 'runner-principal-operator', pointer='/data/apiKey/id'))
    if scenario_id in ('P15', 'P18'):
        out.extend([RoleBinding(OWNER, 'runner-principal-owner', pointer='/data/apiKey/id'),
                    RoleBinding(AGENT, 'runner-custody-mint-agent', pointer='/data/apiKey/id')])
    return out


def observe_partial(scenario_id, stack, seeded, variant):
    expected = load_expected(scenario_id, variant)
    recorder = stack.extra.get('recorder')
    selected = {}
    if recorder is not None and seeded.state and scenario_id in ('P15', 'P18', 'P20'):
        from agent_e2e_studio_observer import guid
        item, version = guid(seeded.state['itemId']), guid(seeded.state['versionId'])
        path = f'/api/v1/studio/content-items/{item}/versions/{version}'
        prefix = scenario_id.lower()
        if scenario_id in ('P15', 'P20'):
            for suffix, principal in (('operator', 'operator'), ('anonymous', 'anonymous')):
                answer = recorder.json(f'{prefix}-{suffix}', 'GET', path, principal=principal)
                if principal == 'operator':
                    require_exact_positive(answer, item, version)
            selected['publication.private'] = permission_matrix((f'{prefix}-operator', f'{prefix}-anonymous'))
        if scenario_id in ('P15', 'P18'):
            # A generic 404 cannot establish distinct native tenants.
            identities = []
            for principal in ('owner', 'agent'):
                answer = recorder.json(f'{prefix}-{principal}-identity', 'GET',
                                       '/api/v1/agent/target', principal=principal)
                value = answer['observation']['body']
                if answer['observation']['httpStatus'] != 200 or not isinstance(value, dict):
                    raise ValueError('native tenant principal unavailable')
                identity = value.get('principal')
                if not isinstance(identity, dict) or not identity.get('tenantId') or not identity.get('actorId'):
                    raise ValueError('native tenant identity unavailable')
                if identity['actorId'] != recorder.principals[principal][1]:
                    raise ValueError('recorder principal differs from native identity')
                identities.append((value.get('targetId'), identity))
            owner, agent = identities
            if (not owner[0] or owner[0] != agent[0] or
                    owner[1]['tenantId'] == agent[1]['tenantId'] or
                    owner[1]['actorId'] == agent[1]['actorId'] or
                    owner[1].get('isHumanSession') is not True or
                    owner[1].get('authenticationKind') not in ('operator-bearer', 'interactive')):
                raise ValueError('distinct native tenant and human-owner custody unavailable')
            for suffix, principal in (('owner', 'owner'), ('agent', 'agent')):
                answer = recorder.json(f'{prefix}-{suffix}', 'GET', path, principal=principal)
                if principal == 'owner':
                    require_exact_positive(answer, item, version)
            selected['tenant.denied'] = permission_matrix((f'{prefix}-owner', f'{prefix}-agent'),
                (f'{prefix}-owner-identity', f'{prefix}-agent-identity'))
        if scenario_id == 'P15':
            from agent_e2e_p10_observer import source_path
            recorder.json('p15-source-after', 'GET', source_path(seeded.state['shelters']))
            selected['source-content.unchanged'] = {'protectedContent': [{
                'label': 'shelters', 'before': 'p15-source-before', 'after': 'p15-source-after'}]}
    # Missing controller evidence must fail projection rather than yield a fabricated value.
    return [observation(assertion, selected.get(assertion, {
        'body': scenario_id.lower() + '-unavailable-' + assertion.replace('.', '-'), 'pointer': ''}))
            for assertion in expected]
