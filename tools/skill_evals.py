#!/usr/bin/env python3
"""Skill evaluation criterion of the GA capability gate (SKILL-REFRESH-PLAN section 5).

Every skill the candidate ships (`skills/<name>/SKILL.md` at the candidate SHA) needs a recorded
with/without evaluation in `certification/skill-evals/<name>.json` that was run against the same
skill content (`skillTreeSha256`, the digest of every file under `skills/<name>/`) and that meets
the keep rule: on every evaluated model at least `minRunsPerArm` runs per arm and no loss in
success with the skill, and on at least one model a higher success count, or a median wall time or
median tool-call count cut by `minCutPercent` or more. A skill without such a result fails.

`python tools/skill_evals.py digest <server checkout> <skill>` prints the digest to record.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

SKILLS_DIR = 'skills'
RESULT_SCHEMA = 'honua.skill-eval/v1'
POLICY_KEYS = frozenset({'resultsDir', 'models', 'minRunsPerArm', 'minCutPercent'})
ARM_FIELDS = ('runs', 'successes', 'medianSeconds', 'medianToolCalls')


class PolicyError(Exception):
    pass


def check_policy(policy) -> dict:
    """The ledger's `skillEvaluations` block; a malformed block fails the gate closed."""
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        raise PolicyError(f'ledger skillEvaluations keys must be exactly {sorted(POLICY_KEYS)}')
    models = policy['models']
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m for m in models):
        raise PolicyError('ledger skillEvaluations.models must be a non-empty list of strings')
    for key in ('minRunsPerArm', 'minCutPercent'):
        if type(policy[key]) is not int or policy[key] <= 0:
            raise PolicyError(f'ledger skillEvaluations.{key} must be a positive integer')
    if not isinstance(policy['resultsDir'], str) or not policy['resultsDir']:
        raise PolicyError('ledger skillEvaluations.resultsDir must be a path')
    return policy


def shipped_skills(checkout: Path) -> list[str]:
    root = checkout / SKILLS_DIR
    if not root.is_dir():
        return []
    return sorted(path.parent.name for path in root.glob('*/SKILL.md') if path.is_file())


def tree_digest(directory: Path) -> str:
    """sha256 over each file's relative path and content digest, in path order."""
    outer = hashlib.sha256()
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        outer.update(f'{relative}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n'.encode())
    return 'sha256:' + outer.hexdigest()


def _arm(value) -> bool:
    if not isinstance(value, dict):
        return False
    counts = all(type(value.get(field)) is int and value[field] >= 0 for field in ARM_FIELDS[:2])
    medians = all(type(value.get(field)) in (int, float) and value[field] >= 0 for field in ARM_FIELDS[2:])
    return counts and medians and value['successes'] <= value['runs']


def _cut(without: float, with_skill: float, percent: int) -> bool:
    return without > 0 and (without - with_skill) * 100 >= percent * without


def model_verdict(arms, policy: dict) -> tuple[str | None, bool]:
    """(gap, gain) for one model: a gap fails the skill; a gain is what the keep rule asks for."""
    if not isinstance(arms, dict) or not _arm(arms.get('withSkill')) or not _arm(arms.get('withoutSkill')):
        return 'arms withSkill and withoutSkill must each carry runs, successes and medians', False
    treated, control = arms['withSkill'], arms['withoutSkill']
    if min(treated['runs'], control['runs']) < policy['minRunsPerArm']:
        return f"fewer than {policy['minRunsPerArm']} runs per arm", False
    if treated['successes'] * control['runs'] < control['successes'] * treated['runs']:
        return f"success fell with the skill ({treated['successes']}/{treated['runs']} vs {control['successes']}/{control['runs']})", False
    percent = policy['minCutPercent']
    gain = (treated['successes'] * control['runs'] > control['successes'] * treated['runs']
            or _cut(control['medianSeconds'], treated['medianSeconds'], percent)
            or _cut(control['medianToolCalls'], treated['medianToolCalls'], percent))
    return None, gain


def result_gap(result, name: str, digest: str, policy: dict) -> str | None:
    if not isinstance(result, dict) or result.get('schema') != RESULT_SCHEMA or result.get('skill') != name:
        return f'evaluation result is not a {RESULT_SCHEMA} record for {name}'
    if result.get('skillTreeSha256') != digest:
        return f'evaluation ran against other skill content (candidate {digest})'
    models = result.get('models') if isinstance(result.get('models'), dict) else {}
    gains = []
    for model in policy['models']:
        gap, gain = model_verdict(models.get(model), policy)
        if gap:
            return f'{model}: {gap}'
        gains.append(gain)
    if not any(gains):
        return f"no model shows higher success or a {policy['minCutPercent']}% cut in time or tool calls"
    return None


def evaluate_skills(checkout: Path, policy: dict, results_root: Path, read) -> list[dict]:
    """One row per shipped skill; `read` loads a result file and raises ValueError/OSError."""
    rows = []
    for name in shipped_skills(checkout):
        digest = tree_digest(checkout / SKILLS_DIR / name)
        path = results_root / policy['resultsDir'] / f'{name}.json'
        if not path.is_file():
            gap = 'no with/without evaluation result recorded'
        else:
            try:
                gap = result_gap(read(path), name, digest, policy)
            except (OSError, ValueError) as error:
                gap = f'evaluation result is unreadable: {error}'
        rows.append({'skill': name, 'skillTreeSha256': digest, 'gap': gap})
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != 'digest':
        print('usage: skill_evals.py digest <server checkout> <skill>', file=sys.stderr)
        return 2
    directory = Path(argv[1]) / SKILLS_DIR / argv[2]
    if not (directory / 'SKILL.md').is_file():
        print(f'{directory} has no SKILL.md', file=sys.stderr)
        return 1
    print(tree_digest(directory))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
