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
