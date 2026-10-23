"""Actual retained HTTP contract controls; not real IdP/tenant qualification."""
import copy
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_studio_tenant_observer import (DENIAL_CODE, OWNER_ENV, StudioTenantObserver,
    private_source, validate_configuration, verify_owner_injection)
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server

PRIVATE_ITEM = '11111111-1111-4111-8111-111111111111'
PRIVATE_VERSION = '22222222-2222-4222-8222-222222222222'
PRIVATE_DRAFT = '33333333-3333-4333-8333-333333333333'
PUBLIC_ITEM = '44444444-4444-4444-8444-444444444444'
PUBLIC_VERSION = '55555555-5555-4555-8555-555555555555'


def source():
    return {'itemId': PRIVATE_ITEM, 'versionId': PRIVATE_VERSION, 'sourceDraftId': PRIVATE_DRAFT,
        'ownerPrincipalId': 'native-human-owner-b', 'tenantId': 'tenant-b', 'namespace': 'private-map',
        'packageKey': 'private-map', 'contentHash': 'b'*64,
        'envelope': {'family': 'map', 'body': {'protected': 'source-original-content', 'flag': True}}}


@pytest.fixture
def tenant(adapter, server, monkeypatch):
    monkeypatch.setenv(OWNER_ENV, 'actual-contract-owner-b-token')
    monkeypatch.setenv('CONTRACT_TASK_KEY', 'actual-contract-task-a-key')
    owner = ScenarioHttp(durable=adapter.durable, context=adapter.http.context, endpoint=server.url,
        credential_env=OWNER_ENV, credential_kind='bearer', attempt_id='tenant-owner')
    task = ScenarioHttp(durable=adapter.durable, context=adapter.http.context, endpoint=server.url,
        credential_env='CONTRACT_TASK_KEY', credential_kind='api-key', attempt_id='tenant-task')
    public = {'itemId': PUBLIC_ITEM, 'versionId': PUBLIC_VERSION, 'contentHash': 'a'*64,
              'requesterPrincipalId': 'native-task-owner-a'}
    records, state = {}, SimpleNamespace(change=None)
    def retain(name, value):
        assert name not in records
        records[name] = copy.deepcopy(value)
        return {'key': name, 'sha256': canonical_digest(value)}
    observer = StudioTenantObserver(owner=owner, outsider=adapter.http, task=task,
        source=source(), public_binding=public, retain=retain)
    private_path = observer.path
    own_path = '/api/v1/studio/content-items/'+PUBLIC_ITEM+'/versions/'+PUBLIC_VERSION
    identities = {'owner': {'actorId': 'native-human-actor-b', 'studioOwnerId': 'native-human-owner-b',
        'tenantId': 'tenant-b', 'isHumanSession': True, 'authenticationKind': 'operator-bearer'},
        'outsider': {'actorId': 'native-observer-actor-a', 'studioOwnerId': 'native-observer-owner-a',
        'tenantId': 'tenant-a', 'isHumanSession': False, 'authenticationKind': 'api-key'},
        'task': {'actorId': 'native-task-actor-a', 'studioOwnerId': public['requesterPrincipalId'],
        'tenantId': 'tenant-a', 'isHumanSession': False, 'authenticationKind': 'api-key'}}
    def respond(method, path, raw):
        assert method == 'GET', 'tenant observer must never mutate or approve'
        headers = {k.lower(): v for k, v in server.seen[-1][2].items()}
        role = ('owner' if headers.get('authorization') == 'Bearer actual-contract-owner-b-token' else
            'task' if headers.get('x-api-key') == 'actual-contract-task-a-key' else 'outsider')
        if path == '/api/v1/agent/target':
            principal = copy.deepcopy(identities[role])
            target = 'actual-native-target'
            if state.change == 'same-tenant' and role == 'owner': principal['tenantId'] = 'tenant-a'
            if state.change == 'no-tenant': principal['tenantId'] = None
            if state.change == 'machine-owner' and role == 'owner': principal['isHumanSession'] = False
            if state.change == 'foreign-task' and role == 'task': principal['tenantId'] = 'tenant-c'
            if state.change == 'wrong-owner' and role == 'owner': principal['studioOwnerId'] = 'other'
            if state.change == 'foreign-target' and role == 'owner': target = 'different-native-target'
            if state.change == 'expired-owner' and role == 'owner': return 401, {}, b'{"status":401}'
            return 200, {}, json.dumps({'targetId': target, 'principal': principal}).encode()
        if path == own_path:
            if state.change == 'blanket-denial': return 403, {}, b'{"status":403}'
            return 200, {}, json.dumps({**public, 'envelope': {'family': 'map'}}).encode()
        assert path == private_path
        if role == 'owner' or state.change == 'disclosed':
            value = {key: copy.deepcopy(item) for key, item in source().items()
                     if key in {'itemId', 'versionId', 'contentHash', 'sourceDraftId', 'envelope'}}
            value['ownerId'] = source()['ownerPrincipalId']
            if state.change == 'altered-content': value['envelope']['body'] = {'changed': True}
            if state.change == 'json-type-confusion': value['envelope']['body']['flag'] = 1
            return 200, {}, json.dumps({'success': True, 'data': value}).encode()
        value = {'type': 'https://honua.io/problems/studio', 'title': 'Not Found', 'status': 404,
            'detail': 'The Studio resource belongs to another tenant.', 'instance': private_path, 'code': DENIAL_CODE}
        if state.change == 'bare-404': value = {'status': 404}
        if state.change == 'wrong-denial': value['code'] = 'studio_authorization/agent_item_permission_required'
        if state.change == 'denial-content': value['envelope'] = source()['envelope']
        if state.change == 'denial-detail': value['detail'] = 'source-original-content'
        return (403 if state.change == 'generic-403' else 404), {}, json.dumps(value).encode()
    server.respond = respond
    return SimpleNamespace(observer=observer, state=state, records=records, identities=identities,
        private_path=private_path, server=server)


def test_native_identity_joins_and_same_resource_positive_and_denial_have_original_raw_bindings(tenant):
    before = tenant.observer.observe(step='tenant-before')
    after = tenant.observer.observe(step='tenant-after', previous=before)
    rows = after['observation']['requests']
    assert [(r['principalId'], r['resourceId'], r['status']) for r in rows] == [
        ('native-human-actor-b', PRIVATE_ITEM, 200), ('native-observer-actor-a', PRIVATE_ITEM, 404)]
    assert all(len(r['responseSha256']) == 64 and r['disclosedResourceIds'] == [] for r in rows)
    assert len(after['references']) == 6
    assert all(method == 'GET' and 'x-tenant' not in {key.lower() for key in headers}
        for method, _, headers, _ in tenant.server.seen)
    assert before['sourceDigest'] == after['sourceDigest']
    import agent_e2e_gate
    agent_e2e_gate.oracle_predicates.verify('tenant.denied', after['observation'], {'requests': [
        {key: row[key] for key in ('principalId', 'method', 'resourceId', 'status')} for row in rows]})


@pytest.mark.parametrize('change', ['same-tenant', 'no-tenant', 'machine-owner', 'foreign-task', 'wrong-owner',
    'foreign-target', 'expired-owner', 'blanket-denial', 'altered-content', 'disclosed', 'bare-404',
    'wrong-denial', 'denial-content', 'denial-detail', 'generic-403', 'json-type-confusion'])
def test_no_fabricated_tenant_generic_refusal_or_disclosed_private_content_can_pass(tenant, change):
    tenant.state.change = change
    with pytest.raises(PackError): tenant.observer.observe(step='tenant-refused')
    assert 'tenant-refused-observation' not in tenant.records


def test_current_target_identity_cannot_be_rebound_between_before_and_after(tenant):
    before = tenant.observer.observe(step='tenant-before')
    tenant.identities['outsider']['actorId'] = 'replaced-observer'
    with pytest.raises(PackError, match='credentials or original protected content changed'):
        tenant.observer.observe(step='tenant-after', previous=before)


def custody_config():
    return {'schemaVersion': 1, 'itemId': PRIVATE_ITEM, 'versionId': PRIVATE_VERSION,
        'ownerCredentialEnv': OWNER_ENV, 'credentialBinding': {
            'secretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-test/human-session/owner-b-Ab12Cd',
            'versionId': 'a'*32}}


def test_extra_declared_tenant_or_human_flags_cannot_replace_actual_native_projection():
    for key, value in [('tenantId', 'tenant-b'), ('isHuman', True), ('ownerCredentialEnv', 'OTHER_ENV')]:
        config = custody_config()
        config[key] = value
        with pytest.raises(PackError): validate_configuration(config)


@pytest.mark.parametrize('change', [None, 'latest-version', 'wrong-container', 'extra-secret', 'wrong-task', 'wrong-region'])
def test_private_owner_injection_uses_actual_task_and_definition_readback_only(monkeypatch, change):
    config = custody_config()
    task = 'arn:aws:ecs:us-east-1:123456789012:task/honua-pilot-test/'+'b'*32
    monkeypatch.setenv('PILOT_CONTROLLER_TASK_ARN', task)
    calls = []
    class Ecs:
        def describe_tasks(self, **kwargs):
            calls.append(('describe_tasks', kwargs))
            assert kwargs == {'cluster': 'arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-test', 'tasks': [task]}
            return {'tasks': [{'taskArn': task if change != 'wrong-task' else 'different', 'taskDefinitionArn': 'actual-definition'}]}
        def describe_task_definition(self, **kwargs):
            calls.append(('describe_task_definition', kwargs))
            secret = {'name': OWNER_ENV, 'valueFrom': config['credentialBinding']['secretArn']+':::'+config['credentialBinding']['versionId']}
            if change == 'latest-version': secret['valueFrom'] = config['credentialBinding']['secretArn']
            return {'taskDefinition': {'containerDefinitions': [{'name': 'other' if change == 'wrong-container' else 'campaign-controller',
                'secrets': [secret, secret] if change == 'extra-secret' else [secret]}]}}
    if change == 'wrong-region': config['credentialBinding']['secretArn'] = config['credentialBinding']['secretArn'].replace('us-east-1', 'us-west-2')
    if change is None:
        observed = verify_owner_injection(config, region='us-east-1', ecs=Ecs())
        assert observed['taskArn'] == task and observed['credentialBinding'] == config['credentialBinding']
        assert len(calls) == 2
    else:
        with pytest.raises(PackError): verify_owner_injection(config, region='us-east-1', ecs=Ecs())


def test_real_session_calls_credential_sql_and_native_http_under_original_fences(tenant, adapter, monkeypatch):
    import agent_e2e_studio_backend as backend
    value = backend.Session.__new__(backend.Session)
    value.durable, value.context = adapter.durable, adapter.http.context
    value.attempt_id = 'tenant-session-contract'
    config = custody_config()
    value.config = {'studio': {'tenantIsolation': config}, 'http': {'endpoint': tenant.server.url},
        'deployment': {'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-test'}}
    value.initial_fixture = lambda: {'baseline': {'original': 'sql-contract-baseline'}}
    value.retain = tenant.observer.retain
    value.requester = lambda **kwargs: tenant.observer.task
    sql_calls, credential_calls = [], []
    def sql_observe(baseline, *, item_id, version_id, authorize):
        authorize()
        sql_calls.append((baseline, item_id, version_id))
        return {'source': source(), 'observerPrincipal': 'read-only-contract-role'}
    value.database = SimpleNamespace(observe_private_tenant_source=sql_observe)
    monkeypatch.setattr(backend, 'sdk', lambda *args: 'read-only-ecs-contract')
    def injected(config, **kwargs):
        credential_calls.append((config, kwargs))
        return {'credentialBinding': config['credentialBinding']}
    monkeypatch.setattr(backend, 'verify_owner_injection', injected)
    observer = SimpleNamespace(observer=tenant.observer.outsider, binding=tenant.observer.public_binding)
    result = value.tenant_proof(step='tenant-before', observer=observer)
    assert [row['status'] for row in result['observation']['requests']] == [200, 404]
    assert sql_calls == [({'original': 'sql-contract-baseline'}, PRIVATE_ITEM, PRIVATE_VERSION)]
    assert credential_calls[0][1] == {'region': 'us-east-1', 'ecs': 'read-only-ecs-contract'}
    assert {'tenant-before-source', 'tenant-before-credential', 'tenant-before-observation'} <= tenant.records.keys()
