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
