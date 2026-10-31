import base64
import hashlib

import pytest

from agent_e2e_local_api import Observation
from agent_e2e_local_runner import aggregate, clopper_pearson, score_assertions


def test_score_requires_matching_raw_controller_evidence():
    body = b'{"resourceIds":["one"]}'
    evidence = {'requests': [{'method': 'GET', 'url': '/query', 'status': 200,
                              'body_b64': base64.b64encode(body).decode(),
                              'sha256': hashlib.sha256(body).hexdigest()}]}
    def score(ids, ev=evidence):
        return score_assertions(['filter.exact'], [Observation('filter.exact', {'resourceIds': ids}, ev)],
                                {'filter.exact': {'resourceIds': ['one']}})[0]['status']
    assert score(['one']) == 'pass'
    assert score(['wrong']) == 'fail'
    assert score(['one'], {'requests': [{**evidence['requests'][0], 'body_b64': 'e30='}]}) == 'fail'
    wrong = b'{"resourceIds":["wrong"]}'
    contradictory = {'requests': [{**evidence['requests'][0], 'body_b64': base64.b64encode(wrong).decode(), 'sha256': hashlib.sha256(wrong).hexdigest()}]}
    assert score(['one'], contradictory) == 'fail'
    assert score_assertions(['filter.exact'], [], {'filter.exact': {'resourceIds': ['one']}})[0]['status'] == 'fail'


def test_aggregate_rejects_incomplete_duplicate_and_unexpected_cases():
    cases = ['P10:A:local-docker', 'P10:B:local-docker']
    receipts = [{'caseId': cases[0], 'status': 'pass'}, {'caseId': cases[1], 'status': 'infrastructure'}]
    summary = aggregate(cases, receipts)
    assert summary['overall']['passed'] == 1
    assert summary['overall']['total'] == 2
    for rows in (receipts[:1], receipts + receipts[:1], receipts + [{'caseId': 'P11:A:local-docker', 'status': 'pass'}]):
        with pytest.raises(ValueError):
            aggregate(cases, rows)


def test_exact_binomial_boundaries_and_midpoint():
    assert clopper_pearson(0, 10) == pytest.approx((0, 0.3084971078))
    assert clopper_pearson(10, 10) == pytest.approx((0.6915028922, 1))
    assert clopper_pearson(5, 10) == pytest.approx((0.1870860284, 0.8129139716))


def test_final_answer_requires_observation_and_rejects_false_success():
    from agent_e2e_local_runner import score_final_answer
    passed = [{'status': 'pass'}]
    failed = [{'status': 'fail'}]
    assert score_final_answer('', {}, passed)['status'] == 'fail'
    assert score_final_answer('Done', None, passed)['status'] == 'fail'
    assert score_final_answer('Done', {}, failed)['status'] == 'fail'
    assert score_final_answer('I was unable to finish the task.', {}, failed)['status'] == 'pass'


def test_observe_accepts_old_and_new_scenario_interfaces():
    from agent_e2e_local_runner import observe_scenario
    class Old:
        def observe(self, stack, seeded, variant): return variant
    class New:
        def observe(self, stack, seeded, variant, run=None): return run
    assert observe_scenario(Old(), None, None, 'A', 'record') == 'A'
    assert observe_scenario(New(), None, None, 'A', 'record') == 'record'


def test_select_case_refuses_held_out_and_other_pack_paths(tmp_path):
    import json
    from agent_e2e_local_runner import select_case
    path = tmp_path / 'pack.v2.AB-only.json'
    path.write_text(json.dumps({'scenarios': [{'id': 'P10', 'prompts': {'A': 'A request', 'B': 'Another request'},
                                              'assertions': ['filter.exact']}]}))
    assert select_case(path, 'P10', 'A')['prompts']['A'] == 'A request'
    with pytest.raises(ValueError):
        select_case(path, 'P10', 'C')
    with pytest.raises(ValueError):
        select_case(tmp_path / 'pack.v2.json', 'P10', 'A')
    with pytest.raises(ValueError):
        select_case(path, 'missing', 'A')


def test_model_session_records_exact_visible_inputs_and_verified_actions(tmp_path):
    import hashlib
    import json
    from agent_e2e_local_runner import read_logged_action, run_model_session
    calls = []
    def complete(messages, certification):
        calls.append(json.dumps(messages).encode())
        answer = ({'kind': 'tool_call', 'tool': 'customer_tool', 'arguments': {'x': 1}} if len(calls) == 1
                  else {'kind': 'final', 'answer': 'Finished.'})
        return json.dumps(answer), {'total_tokens': 3}, 0, {'verified': True}
    result = run_model_session(prompt='Customer request', tool_catalog=[{'name': 'customer_tool', 'inputSchema': {}}],
        complete=complete, invoke=lambda name, args: {'content': [], 'isError': False}, log_dir=tmp_path,
        max_turns=3, max_actions=2, max_tokens=20, certification={'runNonce': 'one'})
    assert result['modelVisibleDigests'] == ['sha256:' + hashlib.sha256(raw).hexdigest() for raw in calls]
    assert json.loads(calls[0])[1]['content'] == 'Customer request'
    for private in ('requiredSequence', 'terminalSurface', 'currentState', 'taskOutcome'):
        assert private not in calls[0].decode()
    entry = result['actions'][0]
    assert read_logged_action(entry['logPath'], entry['digest'])['arguments'] == {'x': 1}
    with open(entry['logPath'], 'wb') as out:
        out.write(b'{}')
    with pytest.raises(ValueError):
        read_logged_action(entry['logPath'], entry['digest'])


def test_case_does_not_retry_infrastructure_failure_and_cleans_stack():
    from agent_e2e_local_runner import execute_case
    class Scenario:
        supported_variants = ('A', 'B')
        def fault_plan(self, variant): return None
        def needs_approver(self): return False
        def seed(self, stack, variant): return 'seed'
        def cleanup(self, stack, seeded): events.append('cleanup')
    class Runtime:
        def setup(self, *args): events.append('setup'); return 'stack'
        def session(self, *args): events.append('session'); raise RuntimeError('private detail')
        def teardown(self, stack): events.append('teardown')
    events = []
    result = execute_case(case_id='P10:A:local-docker', scenario=Scenario(), variant='A', prompt='request',
                          candidate_digest='sha256:abc', runtime=Runtime())
    assert result['status'] == 'infrastructure'
    assert events == ['setup', 'session', 'cleanup', 'teardown']
    assert result['infrastructure'] == [{'phase': 'execution', 'errorType': 'RuntimeError'}]
    assert 'private detail' not in str(result)


def test_uncertain_tool_execution_retains_attempt_without_retry(tmp_path):
    from agent_e2e_local_runner import SessionInterrupted, run_model_session
    import json
    count = []
    def complete(messages, certification):
        return json.dumps({'kind': 'tool_call', 'tool': 'x', 'arguments': {}}), {'total_tokens': 1}, 0, {}
    def invoke(name, arguments):
        count.append(name)
        raise TimeoutError()
    with pytest.raises(SessionInterrupted) as error:
        run_model_session(prompt='Request', tool_catalog=[{'name': 'x'}], complete=complete, invoke=invoke,
            log_dir=tmp_path, max_turns=3, max_actions=3, max_tokens=20, certification={})
    assert error.value.category == 'infrastructure'
    assert count == ['x']
    assert error.value.progress['actions'][0]['status'] == 'unknown'
    assert len(error.value.progress['modelVisibleDigests']) == 1
