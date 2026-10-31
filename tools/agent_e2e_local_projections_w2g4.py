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
FOREIGN = 'seeded:map:foreign'
FOREIGN_VERSION = 'seeded:map:foreign-version'
OPERATOR = 'seeded:principal:operator'
OWNER = 'seeded:principal:owner'
AGENT = 'seeded:principal:agent'


def permission_matrix(steps, identity_steps=(), item_role=ITEM, version_role=VERSION):
    """Bind each response to the actual requested map and its recorder principal."""
    result = {'object': {'requests': {'each': list(steps), 'to': {'object': {
        'principalId': {'principal': '$step'},
        'method': {'method': '$step'},
        'resourceId': {'target': '$step', 'role': item_role},
        'versionId': {'target': '$step', 'role': version_role},
        'status': {'status': '$step'},
        'responseSha256': {'sha256': '$step'},
        'disclosedResourceIds': {'disclosed': '$step', 'roles': [item_role]},
    }}}}}
    if identity_steps:
        result['object']['nativeIdentities'] = {'list': [
            {'body': step, 'pointer': ''} for step in identity_steps]}
    return result


def require_exact_positive(answer, item, version, owner_id=None):
    response = answer['observation']
    value = response.get('body')
    value = value.get('data') if isinstance(value, dict) else None
    if (response['httpStatus'] != 200 or not isinstance(value, dict) or
            value.get('itemId') != item or value.get('versionId') != version or
            (owner_id is not None and value.get('ownerId') != owner_id)):
        raise ValueError('positive control did not return the exact saved map version')


def require_seeded_map(recorder, step, item, version):
    record = recorder.records.get(step)
    value = record.json if record is not None else None
    value = value.get('data') if isinstance(value, dict) else None
    if (record is None or record.phase != 'seed' or record.status not in (200, 201) or
            not record.intact() or not isinstance(value, dict) or
            value.get('itemId') != item or value.get('versionId') != version):
        raise ValueError('map identity is not bound to the seed-recorded version')


def observation(assertion, spec):
    return Observation(assertion, None, {'projection': 'extract/v1', 'spec': spec})


def role_bindings(scenario_id, seeded):
    if scenario_id not in ('P15', 'P18', 'P20') or not seeded.state:
        return []
    out = [RoleBinding(ITEM, f'{scenario_id.lower()}-map-saved', pointer='/data/itemId'),
           RoleBinding(VERSION, f'{scenario_id.lower()}-map-saved', pointer='/data/versionId')]
    if scenario_id in ('P15', 'P20'):
        out.append(RoleBinding(OPERATOR, 'runner-principal-operator', pointer='/data/apiKey/id'))
    if scenario_id == 'P15':
        out.extend([RoleBinding(FOREIGN, 'p15-foreign-saved', pointer='/data/itemId'),
                    RoleBinding(FOREIGN_VERSION, 'p15-foreign-saved', pointer='/data/versionId')])
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
        require_seeded_map(recorder, f'{prefix}-map-saved', item, version)
        if scenario_id in ('P15', 'P20'):
            for suffix, principal in (('operator', 'operator'), ('anonymous', 'anonymous')):
                answer = recorder.json(f'{prefix}-{suffix}', 'GET', path, principal=principal)
                if principal == 'operator':
                    require_exact_positive(answer, item, version)
            selected['publication.private'] = permission_matrix((f'{prefix}-operator', f'{prefix}-anonymous'))
        if scenario_id in ('P15', 'P18'):
            if scenario_id == 'P15':
                foreign_item = guid(seeded.state['foreignItemId'])
                foreign_version = guid(seeded.state['foreignVersionId'])
                if (foreign_item, foreign_version) == (item, version) or foreign_item == item:
                    raise ValueError('tenant control must protect a distinct foreign map')
                require_seeded_map(recorder, 'p15-foreign-saved', foreign_item, foreign_version)
                tenant_item, tenant_version = foreign_item, foreign_version
                tenant_path = f'/api/v1/studio/content-items/{foreign_item}/versions/{foreign_version}'
                item_role, version_role = FOREIGN, FOREIGN_VERSION
            else:
                tenant_item, tenant_version, tenant_path = item, version, path
                item_role, version_role = ITEM, VERSION
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
                    not owner[1].get('studioOwnerId') or
                    owner[1].get('isHumanSession') is not True or
                    owner[1].get('authenticationKind') not in ('operator-bearer', 'interactive')):
                raise ValueError('distinct native tenant and human-owner custody unavailable')
            for suffix, principal in (('owner', 'owner'), ('agent', 'agent')):
                answer = recorder.json(f'{prefix}-{suffix}', 'GET', tenant_path, principal=principal)
                if principal == 'owner':
                    require_exact_positive(answer, tenant_item, tenant_version, owner[1]['studioOwnerId'])
                elif answer['observation']['httpStatus'] == 404:
                    value = answer['observation']['body']
                    if not isinstance(value, dict) or value.get('code') != 'studio_authorization/cross_tenant_denied':
                        raise ValueError('denial does not identify the cross-tenant boundary')
                elif answer['observation']['httpStatus'] == 404:
                    body = answer['observation']['body']
                    if not isinstance(body, dict) or body.get('code') != 'studio_authorization/cross_tenant_denied':
                        raise ValueError('denial does not identify the cross-tenant boundary')
            selected['tenant.denied'] = permission_matrix((f'{prefix}-owner', f'{prefix}-agent'),
                (f'{prefix}-owner-identity', f'{prefix}-agent-identity'), item_role, version_role)
        if scenario_id == 'P15':
            from agent_e2e_p10_observer import source_path
            recorder.json('p15-source-after', 'GET', source_path(seeded.state['shelters']))
            selected['source-content.unchanged'] = {'protectedContent': [{
                'label': 'shelters', 'before': 'p15-source-before', 'after': 'p15-source-after'}]}
        if scenario_id == 'P18':
            # This covers the selected map envelope only. A frozen baseline and
            # complete map/draft/pointer inventory are still needed to enable
            # mutations.absent for the case as a whole.
            before = recorder.records.get('p18-map-before')
            if (before is None or before.phase != 'seed' or before.path != path or
                    before.principal != 'controller' or before.status != 200):
                raise ValueError('foreign map baseline is not the same controller-read version')
            recorder.json('p18-map-after', 'GET', path)
            selected['mutations.absent'] = {'protectedContent': [{
                'label': 'foreign-map', 'before': 'p18-map-before', 'after': 'p18-map-after',
                'pointer': '/data/envelope'}]}
    # Missing controller evidence must fail projection rather than yield a fabricated value.
    return [observation(assertion, selected.get(assertion, {
        'body': scenario_id.lower() + '-unavailable-' + assertion.replace('.', '-'), 'pointer': ''}))
            for assertion in expected]
