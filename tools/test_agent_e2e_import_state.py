"""Exact handoff replay over actual retained task/submission HTTP originals."""
import copy

import pytest

from agent_e2e_import_database import PROFILE
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution, validate_retained_import_transfer, preserve_history
from test_agent_e2e_import_observer import imported, CREATE
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def retained(imported, adapter):
    imported.execute(CREATE, imported.create)
    selected = imported.submit()
    imported.complete()
    observed = imported.observer.observe(selected, step='owned-output')
    durable = adapter.durable
    context = {**adapter.http.context, 'campaignMode': 'qualification'}
    key = canonical_digest({field: context[field] for field in ('campaignId', 'caseId', 'attempt')})
    baseline = {'candidateKey': durable.store.candidate, 'database': {'database': 'honua_agent_fixture',
        'databaseOid': 71, 'serverAddress': '127.0.0.1', 'serverPort': 5432}, 'snapshot': {'relations': []}}
    durable.state['fixtureBaselines'] = {key: copy.deepcopy(baseline)}
    durable.state['workerLaunches'] = {'import': {'context': {field: context[field] for field in
        ('campaignId', 'caseId', 'attempt', 'campaignMode')}}}
    durable._fixture_identity = lambda scope: DurableExecution._fixture_identity(durable, scope)
    job, task = observed['submission']['job'], selected['task']
    request = {'schemaVersion': 1, 'profile': PROFILE, 'candidateKey': durable.store.candidate,
        'database': baseline['database'], 'baselineDigest': canonical_digest(baseline), 'expectedCleanupRole': 'fixture_cleanup',
        'tables': [{'oid': 31415, 'schema': 'honua_data', 'name': job['output']['physicalTableName'], 'owner': 'fixture_app'}],
        'nativeImports': [{'taskId': task['taskId'], 'taskRevision': task['revision'], 'taskContentHash': task['contentHash'],
            'jobId': job['jobId'], 'leaseId': job['leaseId'], 'tableOid': 31415,
            'taskReference': observed['nativeReferences']['task'], 'submissionReference': observed['nativeReferences']['submission']}]}
    return durable, context, key, request


def test_private_handoff_can_replay_exact_retained_originals_and_controller_cannot_redispatch(retained):
    durable, context, key, request = retained
    assert validate_retained_import_transfer(durable.state, context, request, durable.store.candidate) == key
    reference = DurableExecution.retain_import_transfer_request(durable, context, request)
    assert reference == {'key': key, 'sha256': canonical_digest(request)}
    with pytest.raises(PackError, match='already retained'):
        DurableExecution.retain_import_transfer_request(durable, context, request)
    before = copy.deepcopy(durable.state)
    after = copy.deepcopy(before)
    after['importTransferRequests'][key]['nativeImports'][0]['jobId'] = 'rebound-job'
    with pytest.raises(PackError, match='cannot rewrite or erase'):
        preserve_history(before, after)


@pytest.mark.parametrize('change', ['missing-original', 'different-worker', 'wrong-output', 'wrong-lease', 'preexisting-oid'])
def test_rehashed_declarations_cannot_replace_actual_original_scope(retained, change):
    durable, context, key, request = retained
    state, request = copy.deepcopy(durable.state), copy.deepcopy(request)
    if change == 'missing-original': del state['scenarioOperations'][request['nativeImports'][0]['submissionReference']['key']]
    elif change == 'different-worker': state['workerLaunches']['import']['context']['caseId'] = 'other-case'
    elif change == 'wrong-output': request['tables'][0]['name'] = 'unrelated_physical_table'
    elif change == 'wrong-lease': request['nativeImports'][0]['leaseId'] = 'foreign-lease'
    else:
        state['fixtureBaselines'][key]['snapshot']['relations'].append(copy.deepcopy(request['tables'][0]))
        request['baselineDigest'] = canonical_digest(state['fixtureBaselines'][key])
    with pytest.raises(PackError): validate_retained_import_transfer(state, context, request, durable.store.candidate)
