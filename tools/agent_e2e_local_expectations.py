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
DIRECTORY = ROOT / 'certification/agent-e2e/expectations/local'


def _oracle():
    from agent_e2e_local_runner import oracle
    return oracle


def _resolve(value, variant):
    if isinstance(value, dict):
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
    return _resolve(assertions, variant)
