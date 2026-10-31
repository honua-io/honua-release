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
                                {'filter.exact': {'resourceIds': ['one']}}, trusted_requests={('GET', '/query', 200, 'sha256:' + hashlib.sha256(body).hexdigest())})[0]['status']
    assert score(['one']) == 'pass'
    assert score(['wrong']) == 'fail'
    assert score(['one'], {'requests': [{**evidence['requests'][0], 'body_b64': 'e30='}]}) == 'fail'
    wrong = b'{"resourceIds":["wrong"]}'
    contradictory = {'requests': [{**evidence['requests'][0], 'body_b64': base64.b64encode(wrong).decode(), 'sha256': hashlib.sha256(wrong).hexdigest()}]}
    assert score(['one'], contradictory) == 'fail'
    assert score_assertions(['filter.exact'], [], {'filter.exact': {'resourceIds': ['one']}}, trusted_requests={('GET', '/query', 200, 'sha256:' + hashlib.sha256(body).hexdigest())})[0]['status'] == 'fail'


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
        return json.dumps(answer), {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}, 0, {'verified': True}
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
        return json.dumps({'kind': 'tool_call', 'tool': 'x', 'arguments': {}}), {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1}, 0, {}
    def invoke(name, arguments):
        count.append(name)
        raise TimeoutError()
    with pytest.raises(SessionInterrupted) as error:
        run_model_session(prompt='Request', tool_catalog=[{'name': 'x'}], complete=complete, invoke=invoke,
            log_dir=tmp_path, max_turns=3, max_actions=3, max_tokens=20, certification={})
    assert error.value.category == 'post-action-undetermined'
    assert count == ['x']
    assert error.value.progress['actions'][0]['status'] == 'unknown'
    assert len(error.value.progress['modelVisibleDigests']) == 1


def test_inventory_and_file_aggregation_reject_missing_duplicates(tmp_path):
    import json
    from agent_e2e_local_runner import aggregate_files, build_inventory
    pack = tmp_path / 'pack.v2.AB-only.json'
    pack.write_text(json.dumps({'scenarios': [{'id': 'P10', 'prompts': {'A': 'a', 'B': 'b'},
        'targets': ['local-docker'], 'assertions': ['filter.exact']}]}))
    cases = build_inventory(pack, ['P10'], ['A', 'B'])
    paths = []
    for index, case in enumerate(cases):
        path = tmp_path / f'{index}.json'
        path.write_text(json.dumps({'caseId': case, 'status': 'pass'}))
        paths.append(path)
    assert aggregate_files(cases, paths)['overall']['passed'] == 2
    for files in (paths[:1], paths + paths[:1]):
        with pytest.raises(ValueError): aggregate_files(cases, files)
    for variants in (['A', 'A'], ['C']):
        with pytest.raises(ValueError): build_inventory(pack, ['P10'], variants)


def test_fresh_stack_driver_uses_new_project_and_checks_candidate(tmp_path, monkeypatch):
    import json
    import yaml
    from agent_e2e_local_runner import LocalStackDriver
    digest = 'sha256:' + 'a' * 64
    (tmp_path / 'platform-manifest.yaml').write_text(yaml.safe_dump({'components': {'honua-server': {'digest': digest}}}))
    target = tmp_path / 'certification/terminal-journey/targets/local-docker.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'compose': {'project': 'original', 'file': 'compose.yml'}}))
    class Driver:
        def __init__(self): self.projects = []; self.calls = []
        def invoke(self, operation, request):
            self.calls.append(operation)
            if operation == 'setup':
                self.projects.append(json.loads(open(request['target']).read())['compose']['project'])
                return {'status': 'ready', 'workspaceId': 'workspace', 'baseUrl': 'http://127.0.0.1:8137'}
            return {'status': 'pass'}
    driver = Driver()
    runtime = LocalStackDriver(driver, root=tmp_path)
    with pytest.raises(ValueError): runtime.setup('P10:A:local-docker', 'sha256:' + 'b' * 64)
    first = runtime.setup('P10:A:local-docker', digest)
    runtime.teardown(first)
    second = runtime.setup('P10:B:local-docker', digest)
    runtime.teardown(second)
    assert driver.projects[0] != driver.projects[1]
    assert first.admin_key != second.admin_key
    assert driver.calls == ['setup', 'teardown', 'setup', 'teardown']


def test_candidate_lock_requires_matching_bytes_image_and_source(tmp_path, monkeypatch):
    import json
    import yaml
    import agent_e2e_local_runner as runner
    digest, revision = 'sha256:' + 'a' * 64, 'b' * 40
    manifest = {'platformRelease': '2026.1-rc.2', 'components': {'honua-server':
        {'image': 'ghcr.io/example/server:release', 'digest': digest, 'sha': revision}}}
    (tmp_path / 'platform-manifest.yaml').write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    lock = {'components': {'honua-server': {'source': {'revision': revision},
             'artifacts': [{'kind': 'image', 'digest': digest}]}}}
    lock_path = tmp_path / 'lock.json'
    raw = json.dumps(lock).encode()
    lock_path.write_bytes(raw)
    lock_digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert runner._candidate_binding(lock_digest=lock_digest, lock_path=lock_path)[0] == digest
    with pytest.raises(ValueError): runner._candidate_binding(lock_digest='sha256:' + 'c' * 64, lock_path=lock_path)
    lock['components']['honua-server']['source']['revision'] = 'c' * 40
    raw = json.dumps(lock).encode()
    lock_path.write_bytes(raw)
    with pytest.raises(ValueError):
        runner._candidate_binding(lock_digest='sha256:' + hashlib.sha256(raw).hexdigest(), lock_path=lock_path)


def test_case_scores_state_and_final_answer_without_publishing_action_args():
    from agent_e2e_local_runner import execute_case
    raw = b'{"resourceIds":["one"]}'
    evidence = {'requests': [{'method': 'GET', 'url': '/query', 'status': 200,
        'body_b64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest()}]}
    class Scenario:
        supported_variants = ('A', 'B')
        def fault_plan(self, variant): return None
        def needs_approver(self): return False
        def sessions_required(self): return 1
        def seed(self, stack, variant): return 'seed'
        def observe(self, stack, seeded, variant, run):
            assert run.sessions == 1 and run.final_answers == ('Here are the results.',)
            return [Observation('filter.exact', {'resourceIds': ['one']}, evidence)]
        def expected(self, variant): return {'filter.exact': {'resourceIds': ['one']}}
        def cleanup(self, stack, seeded): pass
    class Runtime:
        assertions = ['filter.exact']
        model = 'us.anthropic.claude-sonnet-4-6'
        def setup(self, *args): return 'stack'
        def session(self, *args):
            return {'finalAnswer': 'Here are the results.', 'modelVisibleDigests': ['sha256:call'],
                    'actions': [{'digest': 'sha256:action', 'logPath': '/private/action'}], 'nudges': 0,
                    'usage': [{'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}], 'provenance': []}
        def agent_principal(self, stack): return 'actor'
        def resource_hints(self, stack): return []
        def trusted_requests(self, stack): return {('GET', '/query', 200, 'sha256:' + hashlib.sha256(raw).hexdigest())}
        def handoff_observations(self, stack, observations): return {}
        def retain_observations(self, stack, observations): return {'sha256': 'sha256:evidence'}
        def teardown(self, stack): pass
    receipt = execute_case(case_id='P10:A:local-docker', scenario=Scenario(), variant='A', prompt='request',
                           candidate_digest='sha256:case', runtime=Runtime())
    assert receipt['status'] == 'pass'
    assert receipt['assertions'][0]['status'] == receipt['finalAnswerCheck']['status'] == 'pass'
    assert '/private/action' not in str(receipt)
    assert receipt['actions'] == [{'digest': 'sha256:action'}]


def test_case_counts_uncertain_dispatched_action_as_failure():
    from agent_e2e_local_runner import execute_case, SessionInterrupted
    class Scenario:
        supported_variants = ('A',)
        def fault_plan(self, variant): return None
        def needs_approver(self): return False
        def seed(self, stack, variant): return 'seed'
        def cleanup(self, stack, seeded): pass
    class Runtime:
        model = 'us.anthropic.claude-sonnet-4-6'
        def setup(self, *args): return 'stack'
        def session(self, *args):
            raise SessionInterrupted('post-action-undetermined', {'actions': [{'digest': 'sha256:action', 'status': 'unknown',
                'logPath': '/private/action'}], 'modelVisibleDigests': ['sha256:prompt'], 'usage': [],
                'provenance': [], 'nudges': 0})
        def teardown(self, stack): pass
    receipt = execute_case(case_id='P10:A:local-docker', scenario=Scenario(), variant='A', prompt='request',
                           candidate_digest='sha256:case', runtime=Runtime())
    assert receipt['status'] == 'fail'
    assert receipt['failure']['reason'] == 'action outcome undetermined'
    assert receipt['actions'] == [{'digest': 'sha256:action', 'status': 'unknown'}]
    assert not receipt['infrastructure']


def test_scorer_rejects_response_absent_from_runner_recorder():
    from agent_e2e_local_runner import score_assertions
    raw = b'{"resourceIds":["one"]}'
    evidence = {'requests': [{'method': 'GET', 'url': '/query', 'status': 200,
        'body_b64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest()}]}
    result = score_assertions(['filter.exact'], [Observation('filter.exact', {'resourceIds': ['one']}, evidence)],
        {'filter.exact': {'resourceIds': ['one']}}, trusted_requests=set())
    assert result[0]['status'] == 'fail'
    assert 'runner-owned recorder' in result[0]['reason']
