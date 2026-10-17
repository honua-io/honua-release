"""Negative controls: inventory must never impersonate execution or qualification."""
import copy
import json
from pathlib import Path

import pytest
import coverage_audit as audit

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs():
    return (
        audit.load(ROOT / 'certification/protocol-certification-requirements.v1.json'),
        audit.load(ROOT / 'certification/sources/protocol-certification-production.v1.json'),
        audit.load(ROOT / 'certification/coverage-audit.v1.json'),
    )


def test_every_governed_row_is_retained_without_inventing_qualification(inputs):
    catalog, _, _ = inputs
    report = audit.build(*inputs)
    assert len(report['rows']) == len(catalog['requirements'])
    assert len({row['id'] for row in report['rows']}) == len(report['rows'])
    assert all(row['execution'] == row['qualification'] == 'not-evaluated' for row in report['rows'])
    assert sum(report['summary']['non_licensed_registration'].values()) == report['summary']['non_licensed_rows']
    assert report['summary']['non_licensed_assertion_review']['not-reviewed'] > 0
    assert all(row['audit_scope'] == 'external-licensed-lane' for row in report['rows'] if row['licensed'])


def test_inventory_preserves_test_ids_budgets_entitlements_and_future_contract_fields(inputs):
    catalog, production, findings = copy.deepcopy(inputs)
    requirement = catalog['requirements'][0]
    requirement.update(test_ids=['required-negative-case'], budget_expectations={'max_requests': 2},
                       entitlement_policy_revision='signed-editing-v1', future_required_dimension='retained')
    identity = audit.digest({key: requirement[key] for key in audit.IDENTITY})
    row = next(row for row in audit.build(catalog, production, findings)['rows'] if row['id'] == identity)
    assert all(row[key] == value for key, value in requirement.items())


def test_registration_does_not_imply_current_trunk_or_assertion_review(inputs):
    report = audit.build(*inputs)
    assert any(not source['same_revision'] for source in report['summary']['source_comparison'])
    assert any(row['producer_status'] == 'registered' and row['assertion_review'] == 'not-reviewed'
               for row in report['rows'])


@pytest.mark.parametrize('operation', ['delete', 'duplicate'])
def test_missing_or_ambiguous_producer_disposition_is_rejected(inputs, operation):
    catalog, production, findings = copy.deepcopy(inputs)
    if operation == 'delete':
        production['producers'].pop(0)
    else:
        production['producers'].append(copy.deepcopy(production['producers'][0]))
    with pytest.raises(ValueError, match='exactly one producer disposition'):
        audit.build(catalog, production, findings)


def test_duplicate_required_cell_is_rejected(inputs):
    catalog, production, findings = copy.deepcopy(inputs)
    catalog['requirements'].append(copy.deepcopy(catalog['requirements'][0]))
    with pytest.raises(ValueError, match='duplicate requirement identity'):
        audit.build(catalog, production, findings)


def test_non_addressable_requires_reason(inputs):
    catalog, production, findings = copy.deepcopy(inputs)
    catalog['requirements'][0].update(addressable_by_client=False, addressability_reason=None)
    with pytest.raises(ValueError, match='governed reason'):
        audit.build(catalog, production, findings)


@pytest.mark.parametrize('edit, message', [
    (lambda f: f.update(issue=''), 'missing issue'),
    (lambda f: f.update(sources=['https://github.com/honua-io/geospatial-mcp/blob/trunk/x#L1']), 'immutable commit'),
    (lambda f: f.update(status='pass'), 'invalid status'),
    (lambda f: f.update(status='implementation-landed'), 'requires a PR'),
    (lambda f: f.update(status='verified', delivery_pr='https://github.com/honua-io/geospatial-mcp/pull/1'), 'receipt required'),
    (lambda f: f.update(selectors={'unknown': ['*']}), 'unknown selector'),
    (lambda f: f.update(selectors={'surface': []}), 'invalid selector'),
    (lambda f: f.update(selectors={'surface': ['does-not-exist']}), 'selects no catalog rows'),
])
def test_false_or_untraceable_finding_dispositions_fail(inputs, edit, message):
    catalog, production, findings = copy.deepcopy(inputs)
    edit(findings['findings'][0])
    with pytest.raises(ValueError, match=message):
        audit.build(catalog, production, findings)


def test_target_handoff_exclusions_match_governed_selection():
    entry = {'deployment_targets': ['windows'], 'except_client_lanes': ['excluded-*']}
    assert audit.selected(entry, {'client_lane': 'sdk', 'deployment_target': 'windows'})
    assert not audit.selected(entry, {'client_lane': 'excluded-sdk', 'deployment_target': 'windows'})


def test_duplicate_json_keys_fail(tmp_path):
    source = tmp_path / 'input.json'
    source.write_text('{"result":"fail","result":"pass"}')
    with pytest.raises(ValueError, match='duplicate JSON key'):
        audit.load(source)


def test_committed_summary_matches_inputs(inputs):
    assert audit.build(*inputs)['summary'] == audit.load(ROOT / 'certification/coverage-audit-summary.v1.json')


def test_changed_requirements_invalidate_summary(inputs):
    catalog, production, findings = copy.deepcopy(inputs)
    prior = audit.build(*inputs)['summary']
    catalog['requirements'][0]['scenario_facets'].append('new-required-negative')
    changed = audit.build(catalog, production, findings)['summary']
    assert prior['requirements_digest'] != changed['requirements_digest']
    assert prior['rows_digest'] != changed['rows_digest']
