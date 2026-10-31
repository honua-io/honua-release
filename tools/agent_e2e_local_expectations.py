"""Load frozen local-corpus expectations (certification/agent-e2e/expectations/local/<ID>.json).

Values are literal JSON derived from the pinned fixtures, a ``{"$fixture": {...}}``
reference resolved through corpus/oracle.fixture_expectation, or contain
``{"$role": name}`` references that the runner substitutes with live ids at scoring
time. Nothing here is derived from a model run.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Release-session scope rulings that take an assertion out of the measured arc. An excluded
# assertion is listed in every receipt as excluded (never dropped, never a pass). Only a
# registered ruling can exclude, and only the assertions or scenarios it names.
EXCLUSION_RULINGS = {
    'founder-ruling-no-multi-tenant-2026.1': {
        'summary': 'Multi-tenancy is not a 2026.1 customer feature (founder ruling); tenant isolation is out of the arc.',
        'assertions': frozenset({'tenant.denied'}),
        'scenarios': frozenset({'P18'}),
    },
}


def exclusion(value):
    """The ruling id when an expectation value is an exclusion marker, else None."""
    if isinstance(value, dict) and '$excluded' in value:
        if set(value) != {'$excluded'} or not isinstance(value['$excluded'], str):
            raise ValueError('malformed exclusion marker')
        return value['$excluded']
    return None


def check_exclusion(scenario_id, assertion, value):
    ruling = exclusion(value)
    if ruling is None:
        return None
    rule = EXCLUSION_RULINGS.get(ruling)
    if rule is None or (assertion not in rule['assertions'] and scenario_id not in rule['scenarios']):
        raise ValueError(f'{assertion} in {scenario_id} is not covered by exclusion ruling {ruling}')
    return ruling
DIRECTORY = ROOT / 'certification/agent-e2e/expectations/local'


def _oracle():
    from agent_e2e_local_runner import oracle
    return oracle


def _resolve(value, variant):
    if isinstance(value, dict):
        if '$excluded' in value:
            return dict(value)
        if '$fixture' in value:
            if set(value) != {'$fixture'} or not isinstance(value['$fixture'], dict):
                raise ValueError('malformed fixture reference')
            args = dict(value['$fixture'])
            assertion = args.pop('assertion')
            sort_by = args.pop('sortFeaturesBy', None)
            resolved = _oracle().fixture_expectation(variant, assertion, **args)
            if sort_by:
                resolved['features'] = sorted(resolved['features'], key=lambda f: f['properties'][sort_by])
            return resolved
        return {key: _resolve(item, variant) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, variant) for item in value]
    return value


def load_document(scenario_id):
    if not re.fullmatch(r'P\d\d', str(scenario_id)):
        raise ValueError('invalid scenario id')
    path = DIRECTORY / f'{scenario_id}.json'
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schemaVersion') != 1 or document.get('scenario') != scenario_id:
        raise ValueError('expectation document identity differs')
    if not isinstance(document.get('variants'), dict) or 'C' in document['variants']:
        raise ValueError('local expectations cover development variants only')
    return document


def load_expected(scenario_id, variant):
    if variant not in ('A', 'B'):
        raise ValueError('only development variants A and B are enabled')
    document = load_document(scenario_id)
    assertions = document['variants'].get(variant)
    if not isinstance(assertions, dict) or not assertions:
        raise ValueError('no frozen expectation for this variant')
    for assertion, value in assertions.items():
        check_exclusion(scenario_id, assertion, value)
    return _resolve(assertions, variant)


READ_FIELDS = {'kind', 'method', 'path', 'principal', 'phase', 'origin', 'replayOfModelAction'}


def load_projections(scenario_id, variant):
    """Frozen per-assertion projections: {assertion: {"spec": ..., "reads": {step: rule}}}.

    The document's top-level "projections" applies to every variant; a variant may override an
    assertion under "variantProjections". Every read rule pins kind, method, a full-match path
    regex, principal and phase (origin defaults to controller).
    """
    if variant not in ('A', 'B'):
        raise ValueError('only development variants A and B are enabled')
    document = load_document(scenario_id)
    rows = dict(document.get('projections') or {})
    rows.update((document.get('variantProjections') or {}).get(variant) or {})
    out = {}
    for assertion, row in rows.items():
        if not isinstance(row, dict) or set(row) != {'spec', 'reads'} or not isinstance(row['spec'], dict) \
                or not isinstance(row['reads'], dict) or not row['reads']:
            raise ValueError('malformed frozen projection: ' + assertion)
        for step, rule in row['reads'].items():
            if not isinstance(rule, dict) or set(rule) - READ_FIELDS or \
                    not {'kind', 'method', 'path', 'principal', 'phase'} <= set(rule):
                raise ValueError(f'frozen read {step} for {assertion} must pin kind, method, path, principal, phase')
            re.compile(rule['path'])
        out[assertion] = {'spec': row['spec'], 'reads': row['reads']}
    return out
