"""Real producer/aggregate fixture bytes; synthetic inputs never qualify a live candidate."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_protocol_certification as cert

FIXTURES = Path(__file__).parent / 'fixtures' / 'protocol-execution-v2'
SCHEMA = json.loads((Path(__file__).parents[1] / 'certification' / 'protocol-certification.v1.schema.json').read_text())
CELL_SCHEMA = {'$ref': '#/$defs/cell', '$defs': SCHEMA['$defs']}


def fixture(name):
    return tuple(json.loads((FIXTURES / f'{name}-{kind}.json').read_text()) for kind in ('ledger', 'requirements'))


def evaluate(ledger, requirements):
    return cert.evaluate(
        ledger, 'nightly', requirements=requirements,
        expected_source_sha=ledger['candidate']['source_sha'],
        expected_image_digest=ledger['candidate']['image_digest'],
        expected_cut_at=ledger['candidate']['cut_at'],
        expected_component_source_shas={'server-certification': ledger['candidate']['source_sha']},
        now=datetime.fromisoformat(ledger['generated_at'].replace('Z', '+00:00')),
    )


def rebind(cell):
    cell['evidence_digest'] = cert._receipt_digest(cell['evidence_receipt'])
    cell['evidence_uri'] = 'https://evidence.honua.io/data/sha256/' + cell['evidence_digest'][7:]
    for facet in cell['facet_results'].values():
        facet['evidence_digest'] = cell['evidence_digest']


def mutate_payload(cell, change):
    payload = json.loads(base64.b64decode(cell['evidence_receipt']['payload_base64']))
    change(payload)
    cell['evidence_receipt']['payload_base64'] = base64.b64encode(
        json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    ).decode()
    rebind(cell)


@pytest.mark.parametrize('name,count', [('cite', 9), ('harness', 42), ('sdk', 67)])
def test_actual_producer_aggregate_fixtures_pass_strict_consumer(name, count):
    ledger, requirements = fixture(name)
    assert len(ledger['cells']) == count
    for cell in ledger['cells']:
        Draft202012Validator(CELL_SCHEMA).validate(cell)
        assert cert._receipt_digest(cell['evidence_receipt']) == cell['evidence_digest']
    report = evaluate(ledger, requirements)
    assert report['overall_status'] == 'pass', report


def test_retained_fixture_files_are_byte_bound_to_provenance():
    metadata = json.loads((FIXTURES / 'metadata.json').read_text())
    assert metadata['scope'].startswith('SYNTHETIC TEST ONLY')
    for name, digest in metadata['files'].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('field', sorted(cert.EXECUTION_FIELDS))
def test_partial_metadata_group_refused_by_schema_and_consumer(field):
    ledger, requirements = fixture('cite')
    del ledger['cells'][0][field]
    assert list(Draft202012Validator(CELL_SCHEMA).iter_errors(ledger['cells'][0]))
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


@pytest.mark.parametrize('field,value', [
    ('client_id', 'other'), ('performed_by', 'other'), ('runner_lane', 'other'),
    ('protocol_version', ''), ('protocol_profile', ' '), ('protocol_profile', 'x' * 257),
    ('protocol_version', 'line\nbreak'), ('exercised_capabilities', ['positive']),
    ('exercised_capabilities', ['positive', 'positive']), ('exercised_capabilities', [True]),
    ('request_url', None), ('request_url', '/relative'), ('request_url', 'file:///tmp/receipt'),
    ('request_url', 'http://user:password@localhost/path'), ('request_url', 'https://localhost:bad/path'),
    ('request_url', 'https://localhost/a b'), ('request_url', 'https://[invalid/path'),
    ('request_url', 'https://localhost/' + 'x' * 8192),
])
def test_invalid_network_execution_claims_are_refused(field, value):
    ledger, requirements = fixture('cite')
    ledger['cells'][0][field] = value
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


def test_unknown_eighth_field_is_still_refused():
    ledger, requirements = fixture('cite')
    ledger['cells'][0]['execution_override'] = True
    assert list(Draft202012Validator(CELL_SCHEMA).iter_errors(ledger['cells'][0]))
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


@pytest.mark.parametrize('mutation', [
    lambda c: c.update(protocol_profile='other'),
    lambda c: c.update(request_url='http://localhost/invented'),
    lambda c: c.update(image_digest='sha256:' + 'f' * 64),
    lambda c: c.update(producer_source_sha='f' * 40),
    lambda c: c.update(client_id='other'),
    lambda c: c.update(performed_by='other'),
    lambda c: c.update(runner_lane='other'),
    lambda c: c.update(protocol_version='other'),
    lambda c: c['evidence_receipt']['identity'].pop('producer_run'),
    lambda c: c['evidence_receipt']['identity']['producer_run'].update(workflow='other.yml'),
    lambda c: c['evidence_receipt']['identity']['producer_run'].update(repository='other/server'),
    lambda c: c['evidence_receipt']['identity']['producer_run'].update(run_id=True),
    lambda c: c['evidence_receipt']['identity']['producer_run'].update(dispatch_id='arbitrary'),
])
def test_source_host_exception_requires_exact_bound_identity_even_when_rehashed(mutation):
    ledger, requirements = fixture('harness')
    cell = ledger['cells'][0]
    mutation(cell)
    rebind(cell)
    assert not cert._valid_execution_metadata(cell, ledger['candidate']['cut_at'], requirements['revision'])
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


@pytest.mark.parametrize('change', [
    lambda p: p['execution'].update(protocol_profile='other'),
    lambda p: p['execution'].update(request_url='http://localhost/invented'),
    lambda p: p['execution'].update(extra=True),
    lambda p: p['test_outcomes'].clear(),
    lambda p: p['test_outcomes'].update(extra='pass'),
    lambda p: p['test_outcomes'].update({next(iter(p['test_outcomes'])): 'fail'}),
    lambda p: p.update(contract_revision='other'),
    lambda p: p.update(extra=True),
])
def test_source_host_rehashed_payload_mutations_are_refused(change):
    ledger, requirements = fixture('harness')
    mutate_payload(ledger['cells'][0], change)
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


def test_source_host_exception_recomputes_digest_before_admitting_null_url():
    ledger, requirements = fixture('harness')
    cell = ledger['cells'][0]
    cell['evidence_receipt']['identity']['producer_run']['run_id'] += 1
    assert not cert._valid_execution_metadata(cell, ledger['candidate']['cut_at'], requirements['revision'])


def test_source_host_duplicate_payload_keys_and_oversized_payload_are_refused():
    ledger, requirements = fixture('harness')
    for raw in (b'{"execution":{},"execution":{}}', b' ' * 65537):
        cell = copy.deepcopy(ledger['cells'][0])
        cell['evidence_receipt']['payload_base64'] = base64.b64encode(raw).decode()
        rebind(cell)
        assert not cert._valid_execution_metadata(cell, ledger['candidate']['cut_at'], requirements['revision'])


def test_source_host_failure_stays_failure_with_honest_execution_metadata():
    ledger, requirements = fixture('harness')
    cell = ledger['cells'][0]
    cell['result'] = cell['evidence_receipt']['result'] = 'fail'
    for facet in cell['scenario_facets']:
        cell['evidence_receipt']['facets'][facet] = cell['facet_results'][facet]['result'] = 'fail'
    mutate_payload(cell, lambda p: p['test_outcomes'].update({next(iter(p['test_outcomes'])): 'fail'}))
    assert cert._valid_execution_metadata(cell, ledger['candidate']['cut_at'], requirements['revision'])
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


def test_historical_absent_metadata_retains_original_strict_receipt_checks():
    ledger, requirements = fixture('cite')
    for cell in ledger['cells']:
        for field in cert.EXECUTION_FIELDS:
            del cell[field]
    assert evaluate(ledger, requirements)['overall_status'] == 'pass'
    ledger['cells'][0]['evidence_receipt']['identity']['source_sha'] = 'f' * 40
    rebind(ledger['cells'][0])
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


def test_source_host_oversized_payload_is_refused_before_any_base64_decode(monkeypatch):
    ledger, requirements = fixture('harness')
    cell = ledger['cells'][0]
    cell['evidence_receipt']['payload_base64'] = 'a' * (4 * ((65536 + 2) // 3) + 4)
    rebind(cell)
    ledger['cells'] = [cell]
    requirements['requirements'] = [row for row in requirements['requirements']
                                    if cert._requirement_signature(row) == cert._requirement_signature(cell)]
    def unexpected_decode(*args, **kwargs):
        pytest.fail('oversized source-host receipt was decoded before its bound')
    monkeypatch.setattr(cert.base64, 'b64decode', unexpected_decode)
    # Exercise the complete entrypoint: a later pass-receipt check must not decode it either.
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


def test_nonpassing_network_subset_keeps_actual_failure():
    ledger, requirements = fixture('cite')
    cell = ledger['cells'][0]
    cell['result'] = cell['evidence_receipt']['result'] = 'fail'
    failed = cell['scenario_facets'][-1]
    cell['facet_results'][failed]['result'] = cell['evidence_receipt']['facets'][failed] = 'fail'
    cell['exercised_capabilities'] = cell['scenario_facets'][:-1]
    rebind(cell)
    assert cert._valid_execution_metadata(cell, ledger['candidate']['cut_at'], requirements['revision'])
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'


def test_sdk_licensed_skips_remain_nonexecuted_and_required_tier_still_refuses():
    ledger, requirements = fixture('sdk')
    skipped = [c for c in ledger['cells'] if c['result'] == 'skip']
    assert len(skipped) == 2
    assert all(c['licensed'] and c['required_tier'] == 'release' for c in skipped)
    assert all(c['request_url'] is None and c['exercised_capabilities'] == [] for c in skipped)
    assert evaluate(ledger, requirements)['overall_status'] == 'pass'
    for cell in skipped:
        cell['required_tier'] = 'nightly'
    for row in requirements['requirements']:
        if row['licensed']:
            row['required_tier'] = 'nightly'
    report = evaluate(ledger, requirements)
    assert report['overall_status'] == 'fail'
    assert any("result is 'skip', expected 'pass'" in finding['why'] for finding in report['findings'])


@pytest.mark.parametrize('field,value', [
    ('exercised_capabilities', ['positive']), ('evidence_receipt', {}),
    ('evidence_digest', 'sha256:' + 'f' * 64), ('evidence_uri', 'https://evidence.honua.io/data/sha256/' + 'f' * 64),
    ('facet_results', {}), ('skip_reason', ''),
])
def test_nonexecuted_skip_cannot_claim_execution_evidence(field, value):
    ledger, requirements = fixture('sdk')
    cell = next(c for c in ledger['cells'] if c['result'] == 'skip')
    cell[field] = value
    assert not cert._valid_execution_metadata(cell, ledger['candidate']['cut_at'], requirements['revision'])
    assert evaluate(ledger, requirements)['overall_status'] == 'fail'
