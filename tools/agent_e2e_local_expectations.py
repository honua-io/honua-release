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

# The frozen exclusion list of the AI arc preregistration, section "Exclusions (listed in every receipt,
# never counted as pass)". Nothing else can exclude. The document lands with honua-io/honua-release#1163
# (introduced in commit bb655ee1); until then PREREG_REFERENCE is the durable pointer, and
# test_prereg_exclusions_match_the_preregistration checks this list against the document once it is present.
PREREG_SOURCE = 'certification/ai-arc/PREREG-arc-v1.md'
PREREG_REFERENCE = ('honua-io/honua-release#1163, commit bb655ee1, certification/ai-arc/PREREG-arc-v1.md '
                    '"Exclusions": tenant.denied (P03, P07) and scenario P18 (founder ruling: multi-tenancy is not '
                    'a 2026.1 customer feature); custody.worker-replaced on local Docker (not observable there)')
PREREG_EXCLUSIONS = ('tenant.denied', 'P18', 'custody.worker-replaced@local-docker')
TARGET = 'local-docker'


def prereg_exclusion(scenario_id, assertion, target=TARGET):
    """The preregistered exclusion that covers this assertion of this case, or None."""
    if scenario_id == 'P18':
        return 'P18'
    if assertion == 'tenant.denied':
        return 'tenant.denied'
    if assertion == 'custody.worker-replaced' and target == 'local-docker':
        return 'custody.worker-replaced@local-docker'
    return None


def exclusion(value):
    """The exclusion id when an expectation value is an exclusion marker, else None."""
    if isinstance(value, dict) and '$excluded' in value:
        if set(value) != {'$excluded'} or not isinstance(value['$excluded'], str):
            raise ValueError('malformed exclusion marker')
        return value['$excluded']
    return None


def check_exclusion(scenario_id, assertion, value, target=TARGET):
    """Exclusion is decided by the preregistered list alone. An expectation marker must agree with it;
    an assertion the list covers is excluded even without a marker, and nothing else can be."""
    covered = prereg_exclusion(scenario_id, assertion, target)
    marked = exclusion(value)
    if marked is not None and marked != covered:
        raise ValueError(f'{assertion} in {scenario_id} is not covered by the preregistered exclusion {marked}')
    return covered


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
    for assertion in [a for a in assertions if prereg_exclusion(scenario_id, a)]:
        assertions = {**assertions, assertion: {'$excluded': prereg_exclusion(scenario_id, assertion)}}
    return _resolve(assertions, variant)


READ_FIELDS = {'kind', 'method', 'path', 'principal', 'phase', 'origin', 'replayOfModelAction', 'session', 'exactPath',
               'addresses'}


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
                or not isinstance(row['reads'], dict):
            raise ValueError('malformed frozen projection: ' + assertion)
        from agent_e2e_local_projections import referenced_steps
        if referenced_steps(row['spec']) and not row['reads']:
            raise ValueError('frozen projection reads records but declares no read rules: ' + assertion)
        for step, rule in row['reads'].items():
            if not isinstance(rule, dict) or set(rule) - READ_FIELDS or \
                    not {'kind', 'method', 'path', 'principal', 'phase'} <= set(rule):
                raise ValueError(f'frozen read {step} for {assertion} must pin kind, method, path, principal, phase')
            re.compile(rule['path'])
        out[assertion] = {'spec': row['spec'], 'reads': row['reads']}
    return out


ROLE_FIELDS = {'role', 'step', 'pointer', 'items', 'id', 'owner', 'before', 'total', 'next_page', 'page_limit',
               'created_by', 'parent', 'parent_link', 'owner_format', 'sources'}


def load_roles(scenario_id):
    """Frozen logical-role bindings ("roles": [{role, step, pointer | items, id, owner, before, total | next_page}])."""
    from agent_e2e_local_api import RoleBinding
    rows = load_document(scenario_id).get('roles') or []
    out = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - ROLE_FIELDS or not ({'role', 'step'} <= set(row)
                                                                        or (str(row.get('role', '')).startswith('scan:')
                                                                            and isinstance(row.get('sources'), list))):
            raise ValueError('malformed frozen role binding')
        out.append(RoleBinding(**{'step': '', **row, **({'sources': tuple(row['sources'])} if 'sources' in row else {})}))
    return out
