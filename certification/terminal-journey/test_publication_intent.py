"""Publication intent controls over independent HTTP reads; no model qualification."""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import executor
import stages
from transport import ActionRejected, ExecutionError, Transport

TOOL = 'honua_studio_propose_publication'
INTENT = {'itemId': 'saved-item', 'versionId': 'saved-version', 'contentHash': 'saved-hash',
          'route': '/authored-map', 'visibility': 'public'}
PROPOSAL = {'proposalId': 'native-proposal', 'status': 'Pending', 'requestedBy': 'author',
            'diff': [f'{key}={value}' for key, value in INTENT.items()]}
OPERATION = {'operationId': 'studio.content.create-publication-request',
             'operationInstanceId': 'native-operation', 'proposalId': 'native-proposal',
             'auditId': 'native-audit', 'correlationId': 'native-correlation'}


@pytest.fixture
def candidate(tmp_path):
    state = {'proposal': copy.deepcopy(PROPOSAL), 'seen': []}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['seen'].append((self.command, self.path, self.headers.get('X-API-Key')))
            if self.headers.get('X-API-Key') not in {'private-operator', 'private-approver'}:
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(state['proposal']).encode())

        def do_POST(self):
            state['seen'].append((self.command, self.path, self.headers.get('X-API-Key')))
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"self-approval forbidden"}')

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02))
    thread.start()
    transport = Transport(f'http://127.0.0.1:{server.server_port}', None, None, tmp_path,
                          {'proposer': 'private-author', 'approver': 'private-approver',
                           'operator': 'private-operator'})
    transport.tool = mock.Mock(return_value={'structuredContent': {
        'operation': OPERATION, 'proposalId': 'native-proposal'}})
    transport.cli_approve = mock.Mock(return_value=(1, None))
    observation = stages.Observation(setup_view_present=True, setup_discovery={
        'metadata': {'view': 'setup'}, 'tools': [{'name': TOOL, 'inputSchema': {'type': 'object'}}]})
    engine = executor.JourneyExecutor({}, {'execution': {'route': '/authored-map',
        'publishedPath': '/api/v1/studio/published/authored-map'}}, observation, transport)
    engine.resources.update({key: INTENT[key] for key in ('itemId', 'versionId', 'contentHash')})
    engine.resources['proposalId'] = 'native-proposal'
    engine.evidence['publicationIntent'] = copy.deepcopy(INTENT)
    try:
        yield engine, transport, state
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def select(engine, arguments):
    return engine.execute(7, {'kind': 'tool_call', 'tool': TOOL, 'arguments': arguments})


@pytest.mark.parametrize('key,value', [
    ('itemId', 'other-item'), ('versionId', 'other-version'), ('contentHash', 'other-hash'),
    ('route', '/different-map'), ('visibility', 'personal'), ('visibility', 'team'),
    ('visibility', 'organization'), ('visibility', None),
])
def test_off_task_proposal_refused_before_candidate_effect(candidate, key, value):
    engine, transport, state = candidate
    arguments = {**INTENT, key: value}
    original = copy.deepcopy(arguments)
    with pytest.raises(ActionRejected, match='publicationContract'):
        select(engine, arguments)
    transport.tool.assert_not_called()
    assert state['seen'] == []
    assert arguments == original


def test_publication_contract_exposes_the_authored_task_without_executing(candidate):
    engine, transport, state = candidate
    contract = engine.terminal_surface(7)['publicationContract']
    assert {key: contract[key] for key in INTENT} == INTENT
    assert 'without credentials' in contract['requirement']
    transport.tool.assert_not_called()
    assert state['seen'] == []


def test_exact_model_selection_is_not_rewritten_and_independent_proposal_is_bound(candidate):
    engine, transport, state = candidate
    arguments = {**INTENT, 'note': 'Model-selected publication note'}
    original = copy.deepcopy(arguments)
    assert select(engine, arguments)['accepted'] is True
    assert transport.tool.call_args.args[1] == original == arguments
    assert engine.check_proposal().status == 'pass'
    assert state['seen'] == [('GET', '/api/v1/admin/proposals/native-proposal', 'private-operator')] * 2


@pytest.mark.parametrize('change', ['route', 'visibility', 'itemId', 'versionId', 'contentHash',
                                  'missing', 'duplicate', 'conflicting', 'not-list', 'not-string'])
def test_durable_proposal_requires_exact_original_intent(candidate, change):
    engine, _, state = candidate
    rows = state['proposal']['diff']
    if change in INTENT:
        rows[:] = [f'{change}=different' if row.startswith(change+'=') else row for row in rows]
    elif change == 'missing':
        rows.pop()
    elif change == 'duplicate':
        rows.append('visibility=public')
    elif change == 'conflicting':
        rows.append('visibility=personal')
    elif change == 'not-list':
        state['proposal']['diff'] = {'visibility': 'public'}
    else:
        rows.append({'visibility': 'public'})
    observed = engine.check_proposal()
    assert observed.status == 'fail'
    assert observed.detail == 'publication proposal differs from the authored intent'
    assert 'durable-proposal' not in engine.evidence.get('proofs', {})


def test_candidate_response_cannot_rebind_the_model_selected_saved_content(candidate):
    engine, transport, _ = candidate
    transport.tool.return_value['structuredContent']['itemId'] = 'other-item'
    select(engine, dict(INTENT))
    check = engine.evidence['checks']['7']['durable-proposal']
    assert check['status'] == 'fail'
    assert check['detail'] == 'publication response changed its original content binding'
    assert engine.evidence['publicationIntent'] == INTENT


def test_changed_proposal_cannot_borrow_a_previous_stage_seven_pass(candidate):
    engine, transport, state = candidate
    assert engine.check_proposal().status == 'pass'
    state['proposal']['diff'][-1] = 'visibility=personal'
    with pytest.raises(ExecutionError, match='differs from the authored intent'):
        engine.approve('native-proposal')
    transport.cli_approve.assert_not_called()
    assert not any(method == 'POST' for method, *_ in state['seen'])
    assert 'approvalResolution' not in engine.evidence


@pytest.mark.parametrize('when', ['self-denial', 'separate-approval'])
def test_approval_rechecks_intent_after_each_principal(candidate, when):
    engine, transport, state = candidate
    def approve(_proposal_id, principal):
        if principal == 'proposer':
            if when == 'self-denial':
                state['proposal']['diff'][-1] = 'visibility=personal'
            return 1, None
        state['proposal'].update(status='Succeeded', resolvedBy='separate-human')
        state['proposal']['diff'][-1] = 'visibility=personal'
        return 0, None
    transport.cli_approve.side_effect = approve
    with pytest.raises(ExecutionError, match='differs from the authored intent'):
        engine.approve('native-proposal')
    assert transport.cli_approve.call_count == (1 if when == 'self-denial' else 2)
    assert 'approvalResolution' not in engine.evidence


def test_proposal_binding_requires_the_original_pre_request_snapshot(candidate):
    engine, transport, _ = candidate
    del engine.evidence['publicationIntent']
    assert engine.check_proposal().status == 'fail'
    with pytest.raises(ExecutionError, match='original publication intent is unavailable'):
        engine.approve('native-proposal')
    transport.cli_approve.assert_not_called()
