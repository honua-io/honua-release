"""Nightly observations age at the gate, independently of republishing the matrix."""
import copy
import datetime as dt

import pytest

import check_capabilities as claims
import ga_capabilities as ga
from test_ga_capabilities import GOOD_TESTS, capability, catalog, lane

NOW = dt.datetime.now(dt.timezone.utc)


def stamp(days=0):
    return (NOW - dt.timedelta(days=days)).isoformat().replace('+00:00', 'Z')


def snapshot(days=0):
    entry = capability('serve.wfs', 'Serve', interop=[lane(stamp(2))], cite=[{
        'suite': 'WFS 2.0', 'passed': 9, 'total': 9, 'passRate': 100.0,
        'freshness': {'state': 'fresh', 'runDate': stamp(2)},
        'evidence': {'conclusion': 'success'}}], geobenchResults=[{
        'scenario': 'wfs-filtered', 'freshness': {'state': 'unknown', 'runDate': None}}])
    return {'capabilities': [entry], 'nightlyEvidence': {
        'collectedAt': stamp(days), 'scope': 'diagnostic; not candidate release proof'},
        'evidenceFreshness': {'anchor': stamp(days), 'maxAgeDays': 14}}


def judge(matrix):
    files = ga.CandidateFiles(matrix, catalog(GOOD_TESTS))
    return ga.evaluate(files, ga.load_ledger(), NOW)[0]


@pytest.mark.parametrize('days', [15, -1])
def test_invalid_collection_cannot_preserve_green_cite(days):
    matrix = snapshot(days)
    assert 'cite' in judge(matrix)['gaps']
    assert claims.resolve_capability_key('serve.wfs', matrix)[0] == 'fail'


def test_old_cite_is_not_refreshed_by_collection():
    matrix = snapshot()
    matrix['capabilities'][0]['cite'][0]['freshness']['runDate'] = stamp(15)
    assert 'cite' in judge(matrix)['gaps']


@pytest.mark.parametrize('run_date', [stamp(15), None])
def test_cite_without_suite_cannot_bypass_freshness(run_date):
    matrix = snapshot()
    row = matrix['capabilities'][0]['cite'][0]
    del row['suite']
    row['freshness']['runDate'] = run_date
    assert 'cite' in judge(matrix)['gaps']
    assert claims.resolve_capability_key('serve.wfs', matrix)[0] == 'fail'


@pytest.mark.parametrize('state', ['unknown', 'stale'])
def test_nonpassing_cite_freshness_cannot_pass_on_counters(state):
    matrix = snapshot()
    matrix['capabilities'][0]['cite'][0]['freshness']['state'] = state
    assert 'cite' in judge(matrix)['gaps']


def test_fresh_snapshot_exposes_all_observations_without_requiring_load():
    matrix = snapshot()
    original = copy.deepcopy(matrix)
    result = judge(matrix)
    assert result['gaps'] == {}
    assert result['nightlyObservations']['geobenchResults'][0]['freshness']['state'] == 'unknown'
    assert result['nightlyObservations']['cite'][0]['evidence']['conclusion'] == 'success'
    assert matrix == original


def test_invalid_anchor_cannot_preserve_green_interop():
    matrix = snapshot()
    matrix['evidenceFreshness']['anchor'] = 'invalid'
    assert 'interop' in judge(matrix)['gaps']


@pytest.mark.parametrize('limit', [True, 0, -1, 15, '14', None])
def test_invalid_age_policy_cannot_preserve_passing_evidence(limit):
    matrix = snapshot()
    matrix['evidenceFreshness']['maxAgeDays'] = limit
    assert {'interop', 'cite'} <= judge(matrix)['gaps'].keys()


def test_future_observation_cannot_pass():
    matrix = snapshot()
    matrix['capabilities'][0]['cite'][0]['freshness']['runDate'] = stamp(-1)
    assert 'cite' in judge(matrix)['gaps']


def test_legacy_matrix_uses_existing_gate_policy():
    matrix = snapshot()
    del matrix['nightlyEvidence']
    assert judge(matrix)['gaps'] == {}


def test_receipt_source_includes_cite_observation():
    matrix = snapshot()
    matrix['capabilities'][0]['cite'][0]['freshness']['runDate'] = stamp(5)
    result = judge(matrix)
    assert ga.source_observed_at(ga.Verdict([result], []), NOW) == stamp(5)
