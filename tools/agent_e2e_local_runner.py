"""Controller-side scoring primitives for the local goal-driven corpus.

The live runner is not yet implemented. These functions do not start a stack or
produce a qualifying receipt.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
from pathlib import Path

from agent_e2e_local_api import Observation

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('local_corpus_oracle', ROOT / 'certification/agent-e2e/corpus/oracle.py')
oracle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oracle)


def score_assertions(assertions: list[str], observations: list[Observation], expected: dict) -> list[dict]:
    """Require exactly one independently evidenced observation per pack assertion."""
    if len(set(assertions)) != len(assertions) or set(expected) != set(assertions):
        raise ValueError('pack assertions and frozen expectations differ')
    by_id = {}
    for observation in observations:
        if observation.assertion_id not in assertions or observation.assertion_id in by_id:
            raise ValueError('unexpected or duplicate observation')
        by_id[observation.assertion_id] = observation
    results = []
    for assertion in assertions:
        obs = by_id.get(assertion)
        if obs is None:
            results.append({'assertion': assertion, 'status': 'fail', 'reason': 'missing observation'})
            continue
        try:
            requests = obs.evidence['requests']
            if not isinstance(requests, list) or not requests:
                raise ValueError('missing raw controller evidence')
            hashes = []
            raw_bodies = []
            for request in requests:
                raw = base64.b64decode(request['body_b64'], validate=True)
                digest = hashlib.sha256(raw).hexdigest()
                if request['sha256'].removeprefix('sha256:') != digest:
                    raise ValueError('controller evidence digest mismatch')
                if not isinstance(request['method'], str) or not isinstance(request['url'], str) or type(request['status']) is not int:
                    raise ValueError('invalid controller response metadata')
                hashes.append('sha256:' + digest)
                raw_bodies.append(raw)
            # Until a controller-owned projection is registered for an assertion,
            # evidence hashes alone cannot establish that the projection is true.
            if assertion != 'filter.exact' or len(raw_bodies) != 1:
                raise ValueError('no validated evidence projection for assertion')
            projected = json.loads(raw_bodies[0])
            if projected != obs.observed:
                raise ValueError('observation differs from evidence projection')
            oracle.verify(assertion, projected, expected[assertion])
        except (ValueError, KeyError, TypeError) as exc:
            results.append({'assertion': assertion, 'status': 'fail', 'reason': str(exc)})
        else:
            results.append({'assertion': assertion, 'status': 'pass', 'evidenceHashes': hashes})
    return results


def clopper_pearson(successes: int, trials: int) -> tuple[float, float]:
    """Two-sided exact 95% binomial interval, including boundary cases."""
    if type(successes) is not int or type(trials) is not int or trials <= 0 or not 0 <= successes <= trials:
        raise ValueError('invalid binomial counts')

    def upper_tail(p: float, k: int) -> float:
        return sum(math.comb(trials, i) * p**i * (1-p)**(trials-i) for i in range(k, trials+1))

    def solve(k: int, target: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if upper_tail(mid, k) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    return (0.0 if successes == 0 else solve(successes, .025),
            1.0 if successes == trials else solve(successes + 1, .975))


def aggregate(expected_cases: list[str], receipts: list[dict]) -> dict:
    """Reject missing, duplicate, unexpected and malformed case receipts."""
    if not expected_cases or len(set(expected_cases)) != len(expected_cases):
        raise ValueError('case inventory is empty or duplicated')
    rows = {}
    for receipt in receipts:
        case = receipt.get('caseId')
        if case not in expected_cases or case in rows or receipt.get('status') not in ('pass', 'fail', 'infrastructure'):
            raise ValueError('unexpected, duplicate or malformed case receipt')
        rows[case] = receipt
    if set(rows) != set(expected_cases):
        raise ValueError('missing case receipt')

    def summary(items):
        n = len(items)
        passed = sum(row['status'] == 'pass' for row in items)
        return {'passed': passed, 'total': n, 'interval95': clopper_pearson(passed, n)}

    groups = {}
    for case, row in rows.items():
        groups.setdefault(case.split(':', 1)[0], []).append(row)
    return {'overall': summary(list(rows.values())),
            'scenarios': {name: summary(items) for name, items in sorted(groups.items())},
            'cases': [rows[case] for case in expected_cases]}


def observe_scenario(scenario, stack, seeded, variant, run):
    """Support both the frozen three-argument interface and its v1.1 extension."""
    import inspect
    parameters = inspect.signature(scenario.observe).parameters
    if 'run' in parameters:
        return scenario.observe(stack, seeded, variant, run=run)
    return scenario.observe(stack, seeded, variant)


def score_final_answer(answer: str, observations: dict | None, assertions: list[dict]) -> dict:
    """Score the customer handoff against controller observations, failing closed."""
    from agent_e2e_handoff import score_final_answer as check
    if not isinstance(answer, str) or not answer.strip() or not isinstance(observations, dict):
        return {'status': 'fail', 'reason': 'missing final answer or independent handoff observations'}
    if not assertions:
        return {'status': 'fail', 'reason': 'missing assertion scores'}
    status = 'pass' if all(row.get('status') == 'pass' for row in assertions) else 'fail'
    return check(answer, observations, state_status=status)


def select_case(pack_path: Path, scenario_id: str, variant: str) -> dict:
    """Read only the development pack; this release cannot enable qualification."""
    import json
    if variant not in ('A', 'B'):
        raise ValueError('only development variants A and B are enabled')
    path = Path(pack_path)
    if path.name != 'pack.v2.AB-only.json':
        raise ValueError('the local runner accepts only the A/B-only pack')
    pack = json.loads(path.read_text(encoding='utf-8'))
    matches = [row for row in pack['scenarios'] if row.get('id') == scenario_id]
    if len(matches) != 1:
        raise ValueError('unknown or duplicate scenario')
    case = matches[0]
    if not isinstance(case.get('prompts', {}).get(variant), str) or not case['prompts'][variant]:
        raise ValueError('missing development prompt')
    if not isinstance(case.get('assertions'), list) or not case['assertions']:
        raise ValueError('missing assertions')
    return case
