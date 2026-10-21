import json
import runpy
from pathlib import Path

import pytest

import convergence_rebind as convergence


PRODUCTION = 'certification/sources/protocol-certification-production.v1.json'
MATRIX = 'certification/sources/server/capability-matrix.v1.json'
HARNESS = 'certification/sources/server/protocol-harness-assignments.v1.json'


def source(root, capability=None, production=None):
    capability = capability or {'key': 'analytics.h3', 'status': None, 'maturity': {'implemented': 4}}
    documents = {PRODUCTION: production or {'revision': '2026-10-13.1', 'producers': [], 'unproduced': []},
                 MATRIX: {'capabilities': [capability]}, HARNESS: {'assignments': []}}
    for path, document in documents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document))
    return documents


def test_h3_projection_creates_only_reviewed_unproduced_http_ownership(tmp_path):
    original = source(tmp_path)
    path, data = convergence.project_h3_production_gap(tmp_path)
    projected = json.loads(data)
    [entry] = projected['unproduced']
    assert not projected['producers']
    assert entry['client_lanes'] == ['protocol-harness-gap-analytics-h3']
    assert entry['owner'] == 'honua-io/honua-release'
    assert entry['issue'] == 'https://github.com/honua-io/honua-release/issues/360'
    assert '/honua-server/issues/6042' in entry['missing_producer']
    assert 'not execution against this candidate image' in entry['missing_producer']
    assert 'does not add h3-pg computation' in entry['missing_producer']
    assert json.loads((tmp_path / path).read_bytes()) == original[PRODUCTION]
    (tmp_path / path).write_bytes(data)
    assert convergence.project_h3_production_gap(tmp_path) == (path, data)


@pytest.mark.parametrize('status', ['preview', 'roadmap', 'experimental', 'internal'])
def test_future_maturity_cannot_create_new_supported_gate(tmp_path, status):
    source(tmp_path, {'key': 'analytics.h3', 'status': status, 'maturity': {'implemented': 4}})
    original = (tmp_path / PRODUCTION).read_bytes()
    assert convergence.project_h3_production_gap(tmp_path)[1] == original


def test_absent_or_unimplemented_h3_leaves_policy_bytes_unchanged(tmp_path):
    source(tmp_path, {'key': 'analytics.h3', 'status': None, 'maturity': {}})
    original = (tmp_path / PRODUCTION).read_bytes()
    assert convergence.project_h3_production_gap(tmp_path)[1] == original
    (tmp_path / MATRIX).write_text(json.dumps({'capabilities': []}))
    assert convergence.project_h3_production_gap(tmp_path)[1] == original


@pytest.mark.parametrize('kind', ['producers', 'unproduced'])
def test_existing_or_broad_ownership_cannot_be_silently_replaced(tmp_path, kind):
    production = {'revision': 'fixture', 'producers': [], 'unproduced': []}
    production[kind].append({'client_lanes': ['protocol-harness-gap-*'], 'owner': 'other'})
    source(tmp_path, production=production)
    with pytest.raises(convergence.Finding, match='conflicts with reviewed unproduced'):
        convergence.project_h3_production_gap(tmp_path)


def test_duplicate_source_capability_is_refused(tmp_path):
    documents = source(tmp_path)
    (tmp_path / MATRIX).write_text(json.dumps({'capabilities': documents[MATRIX]['capabilities'] * 2}))
    with pytest.raises(convergence.Finding, match='repeats analytics.h3'):
        convergence.project_h3_production_gap(tmp_path)


def test_actual_production_evaluator_counts_h3_as_unproduced_and_unknown_lanes_still_fail(tmp_path):
    source(tmp_path)
    _, data = convergence.project_h3_production_gap(tmp_path)
    namespace = runpy.run_path(str(Path(convergence.ROOT) / 'certification/generate-protocol-requirements.py'))
    evaluate = namespace['production']
    evaluate.__globals__['load'] = lambda path: json.loads(data)
    row = {'client_lane': 'protocol-harness-gap-analytics-h3', 'deployment_target': 'local-docker',
           'addressable_by_client': True}
    result = evaluate([row], {})
    assert result['cells'] == {'produced': 0, 'unproduced': 1, 'not_addressable': 0}
    assert result['unproduced'][0]['cells'] == 1
    with pytest.raises(ValueError, match='has 0 production dispositions'):
        evaluate([{**row, 'client_lane': 'protocol-harness-gap-unreviewed-future'}], {})


def test_actual_harness_assignment_does_not_gain_a_gap_disposition(tmp_path):
    source(tmp_path)
    (tmp_path / HARNESS).write_text(json.dumps({'assignments': [{'capability_key': 'analytics.h3'}]}))
    original = (tmp_path / PRODUCTION).read_bytes()
    assert convergence.project_h3_production_gap(tmp_path)[1] == original


def test_unknown_source_maturity_requires_review(tmp_path):
    source(tmp_path, {'key': 'analytics.h3', 'status': 'unreviewed-future', 'maturity': {'implemented': 4}})
    with pytest.raises(convergence.Finding, match='no reviewed production disposition'):
        convergence.project_h3_production_gap(tmp_path)


def test_wps_historical_gap_has_exact_reviewed_owner_and_no_producer_credit(tmp_path):
    original = source(tmp_path, {'key': 'serve.wps', 'status': None, 'maturity': {'implemented': 3}})
    path, data = convergence.project_wps_production_gap(tmp_path)
    [entry] = json.loads(data)['unproduced']
    assert entry['client_lanes'] == ['protocol-harness-gap-serve-wps']
    assert entry['issue'] == 'https://github.com/honua-io/honua-release/issues/360'
    assert '/honua-server/issues/6036' in entry['missing_producer']
    assert '/honua-server/issues/6043' in entry['missing_producer']
    assert 'Neither is execution against this candidate image' in entry['missing_producer']
    assert not json.loads(data)['producers']
    assert json.loads((tmp_path / path).read_bytes()) == original[PRODUCTION]
    (tmp_path / path).write_bytes(data)
    assert convergence.project_wps_production_gap(tmp_path) == (path, data)


def test_wps_current_five_real_assignments_cannot_inherit_historical_gap(tmp_path):
    source(tmp_path, {'key': 'serve.wps', 'status': None, 'maturity': {'implemented': 3}})
    (tmp_path / HARNESS).write_text(json.dumps({'assignments': [
        {'capability_key': 'serve.wps', 'operation': operation}
        for operation in ('GetCapabilities', 'DescribeProcess', 'Execute', 'GetStatus', 'GetResult')]}))
    original = (tmp_path / PRODUCTION).read_bytes()
    assert convergence.project_wps_production_gap(tmp_path)[1] == original


def test_wps_unknown_or_conflicting_ownership_requires_review(tmp_path):
    production = {'revision': 'fixture', 'producers': [], 'unproduced': [
        {'client_lanes': ['protocol-harness-gap-serve-wps'], 'owner': 'other'}]}
    source(tmp_path, {'key': 'serve.wps', 'status': None, 'maturity': {'implemented': 3}}, production)
    with pytest.raises(convergence.Finding, match='WPS HTTP gap conflicts'):
        convergence.project_wps_production_gap(tmp_path)
    source(tmp_path, {'key': 'serve.wps', 'status': 'unreviewed-future', 'maturity': {'implemented': 3}})
    with pytest.raises(convergence.Finding, match='WPS HTTP source maturity'):
        convergence.project_wps_production_gap(tmp_path)
