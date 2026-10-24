"""Read-only tenant proof from original SQL rows and native authenticated HTTP.

A public map is intentionally public. This observer protects another, originally
private saved item. Neither tenant identity nor the denial is supplied by a model
or request header. Missing real second-tenant human custody stays a failure.
"""
from __future__ import annotations

import copy
import os
import re

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import data, body
from agent_e2e_studio_observer import guid, instant

OWNER_ENV = 'HONUA_STUDIO_PRIVATE_OWNER_TOKEN'
DENIAL_CODE = 'studio_authorization/cross_tenant_denied'


def require(value, reason):
    if not value:
        raise PackError(reason)


def text(value, name, limit=512):
    require(isinstance(value, str) and 0 < len(value) <= limit and
            all(ord(c) >= 32 and ord(c) != 127 for c in value), 'missing bounded native '+name)
    return value


def private_source(snapshot, *, item_id, version_id):
    """Select protected original metadata; tenant_id is intentionally not in HTTP DTOs."""
    guid(item_id)
    guid(version_id)
    rows = snapshot.get('studio', {})
    items = [r for r in rows.get('studio_content_items', []) if r.get('item_id') == item_id]
    versions = [r for r in rows.get('studio_content_versions', []) if r.get('item_id') == item_id and r.get('version_id') == version_id]
    require(len(items) == len(versions) == 1, 'private tenant source is absent or ambiguous in original SQL baseline')
    item, version = items[0], versions[0]
    owner, tenant = text(version.get('owner_id'), 'private owner'), text(version.get('tenant_id'), 'private tenant', 200)
    namespace = text(version.get('workspace_id'), 'private workspace', 200)
    package = text(version.get('package_key'), 'private package', 200)
    require(item.get('current_version_id') == version_id and item.get('published_version_id') is None and
            all(item.get(k) == version.get(k) for k in ('owner_id', 'tenant_id', 'workspace_id', 'package_key')),
            'protected tenant item is not its original private current saved version')
    envelope = version.get('envelope')
    require(isinstance(envelope, dict) and str(envelope.get('family', '')).lower() == 'map' and
            isinstance(envelope.get('body'), dict) and envelope['body'] and
            isinstance(version.get('content_hash'), str) and re.fullmatch('[a-f0-9]{64}', version['content_hash']),
            'private tenant source lacks its native immutable map content')
    return {'itemId': item_id, 'versionId': version_id, 'sourceDraftId': guid(version.get('source_draft_id')),
        'ownerPrincipalId': owner, 'tenantId': tenant, 'namespace': namespace, 'packageKey': package,
        'contentHash': version['content_hash'], 'envelope': copy.deepcopy(envelope)}


def validate_configuration(config):
    require(isinstance(config, dict) and set(config) == {'schemaVersion', 'itemId', 'versionId',
        'ownerCredentialEnv', 'credentialBinding'} and config['schemaVersion'] == 1 and
        type(config['schemaVersion']) is int and config['ownerCredentialEnv'] == OWNER_ENV,
        'incomplete source-reviewed private tenant observer configuration')
    guid(config['itemId'])
    guid(config['versionId'])
    binding = config['credentialBinding']
    require(isinstance(binding, dict) and set(binding) == {'secretArn', 'versionId'} and
        isinstance(binding['secretArn'], str) and re.fullmatch(
            r'arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:honua-pilot-[a-z0-9-]+/human-session/[A-Za-z0-9/_-]+-[A-Za-z0-9]{6}', binding['secretArn']) and
        isinstance(binding['versionId'], str) and re.fullmatch(r'[A-Za-z0-9-]{32,64}', binding['versionId']),
        'private tenant owner requires immutable actual human-session custody')
    return config


def verify_owner_injection(config, *, region, ecs):
    """Independently observe the exact controller secret version; no secret fetch/write."""
    validate_configuration(config)
    task = os.environ.get('PILOT_CONTROLLER_TASK_ARN', '')
    require(re.fullmatch('arn:aws:ecs:'+re.escape(region)+r':[0-9]{12}:task/honua-pilot-[a-z0-9-]+/[a-f0-9]{32}', task),
            'private tenant custody requires the actual controller task')
    require(config['credentialBinding']['secretArn'].split(':')[3:5] == task.split(':')[3:5],
            'private tenant credential differs from controller account/region')
    cluster = task.replace(':task/', ':cluster/').rsplit('/', 1)[0]
    result = ecs.describe_tasks(cluster=cluster, tasks=[task])
    require(not result.get('failures') and len(result.get('tasks', [])) == 1 and result['tasks'][0].get('taskArn') == task,
            'cannot independently observe private tenant credential controller')
    definition = ecs.describe_task_definition(taskDefinition=result['tasks'][0]['taskDefinitionArn'])['taskDefinition']
    containers = definition.get('containerDefinitions', [])
    require(len(containers) == 1 and containers[0].get('name') == 'campaign-controller', 'private tenant controller container differs')
    injected = [s.get('valueFrom') for s in containers[0].get('secrets', []) if s.get('name') == OWNER_ENV]
    binding = config['credentialBinding']
    require(injected == [binding['secretArn']+':::'+binding['versionId']], 'private tenant bearer is not its exact immutable injected version')
    return {'taskArn': task, 'taskDefinitionArn': result['tasks'][0]['taskDefinitionArn'],
            'credentialEnv': OWNER_ENV, 'credentialBinding': copy.deepcopy(binding)}


class StudioTenantObserver:
    def __init__(self, *, owner, outsider, task, source, public_binding, retain):
        require(owner.client.kind == 'bearer' and outsider.client.kind != 'anonymous' and task.client.kind != 'anonymous' and
            owner.client.endpoint == outsider.client.endpoint == task.client.endpoint and
            len({owner.client.token, outsider.client.token, task.client.token}) == 3,
            'tenant proof requires three distinct real credentials on the same native origin')
        require(source['itemId'] != public_binding['itemId'] and callable(retain),
                'tenant negative must protect unrelated private saved content')
        self.owner, self.outsider, self.task = owner, outsider, task
        self.source, self.public_binding, self.retain = copy.deepcopy(source), copy.deepcopy(public_binding), retain
        self.path = '/api/v1/studio/content-items/'+guid(source['itemId'])+'/versions/'+guid(source['versionId'])

    def observe(self, *, step, previous=None):
        require(isinstance(step, str) and re.fullmatch('[a-z0-9-]{1,40}', step), 'invalid tenant observation step')
        targets = {name: http.json(step+'-'+name+'-identity', 'GET', '/api/v1/agent/target')
                   for name, http in [('owner', self.owner), ('outsider', self.outsider), ('task', self.task)]}
        identities = {}
        for name, answer in targets.items():
            value = body(answer)
            principal = value.get('principal', {})
            identities[name] = {'targetId': text(value.get('targetId'), 'target'),
                **{key: text(principal.get(key), key) for key in ('actorId', 'studioOwnerId', 'tenantId')},
                'authenticationKind': principal.get('authenticationKind'), 'isHumanSession': principal.get('isHumanSession')}
        owner, outsider, task = (identities[name] for name in ('owner', 'outsider', 'task'))
        require(owner['isHumanSession'] is True and owner['authenticationKind'] in ('operator-bearer', 'interactive') and
            owner['studioOwnerId'] == self.source['ownerPrincipalId'] and owner['tenantId'] == self.source['tenantId'] and
            len({row['targetId'] for row in identities.values()}) == 1 and
            owner['tenantId'] != outsider['tenantId'] == task['tenantId'] and
            len({row['actorId'] for row in identities.values()}) == 3 and
            task['studioOwnerId'] == self.public_binding['requesterPrincipalId'],
            'native principals do not corroborate actual distinct tenant/human/task custody')
        if previous is not None:
            require(previous.get('identities') == identities and previous.get('sourceDigest') == canonical_digest(self.source),
                    'tenant credentials or original protected content changed during the worker attempt')
        control_path = '/api/v1/studio/content-items/'+guid(self.public_binding['itemId'])+'/versions/'+guid(self.public_binding['versionId'])
        control = self.outsider.json(step+'-outsider-positive', 'GET', control_path)
        value = data(control)
        require(all(value.get(k) == self.public_binding[k] for k in ('itemId', 'versionId', 'contentHash')) and
                isinstance(value.get('envelope'), dict) and
                str(value['envelope'].get('family', '')).lower() == 'map', 'cross-tenant observer lacks a same-family authorized positive control')
        positive = self.owner.json(step+'-owner-positive', 'GET', self.path)
        value = data(positive)
        require(all(value.get(k) == self.source[k] for k in ('itemId', 'versionId', 'contentHash', 'sourceDraftId')) and
                value.get('ownerId') == self.source['ownerPrincipalId'] and
                canonical_digest(value.get('envelope')) == canonical_digest(self.source['envelope']),
                'authorized private tenant read differs from independently protected original SQL content')
        negative = self.outsider.json(step+'-cross-tenant', 'GET', self.path)
        denied = negative['observation']['body']
        required = {'type', 'title', 'status', 'detail', 'instance', 'code'}
        require(negative['observation']['httpStatus'] == 404 and isinstance(denied, dict) and
            required <= set(denied) <= required | {'correlationId', 'timestamp'} and
            denied['code'] == DENIAL_CODE and denied['status'] == 404 and denied['title'] == 'Not Found' and
            denied['type'] == 'https://honua.io/problems/studio' and denied['instance'] == self.path and
            denied['detail'] == 'The Studio resource belongs to another tenant.',
            'private tenant read lacks a native non-disclosing cross-tenant denial')
        if 'timestamp' in denied:
            instant(denied['timestamp'])
        if 'correlationId' in denied:
            require(isinstance(denied['correlationId'], str) and re.fullmatch('[A-Za-z0-9:_-]{1,128}', denied['correlationId']),
                    'tenant denial carries unexpected correlation metadata')
        rows = [{ 'principalId': identity['actorId'], 'method': 'GET', 'resourceId': self.source['itemId'],
                  'status': answer['observation']['httpStatus'],
                  'responseSha256': answer['observation']['responseDigest'][7:], 'disclosedResourceIds': []}
                for identity, answer in [(owner, positive), (outsider, negative)]]
        observation = {'requests': rows}
        result = {'observation': observation, 'identities': identities, 'sourceDigest': canonical_digest(self.source),
            'references': [*(a['reference'] for a in targets.values()), control['reference'], positive['reference'], negative['reference']]}
        self.retain(step+'-observation', result)
        return result
