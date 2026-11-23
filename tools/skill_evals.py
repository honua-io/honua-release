#!/usr/bin/env python3
"""Skill evaluation criterion of the GA capability gate.

Every skill the candidate ships (`skills/<name>/SKILL.md` committed at the candidate SHA) needs a
recorded with/without evaluation in `certification/skill-evals/<name>.json`. The record is bound to
the skill by `skillTreeSha256`, a digest of the committed files under `skills/<name>/`, and to its
run by `serverSha`, `observedAt` (no older than `maxAgeDays`) and an `evidence` reference that is an
https URL or a file in this repository. It must meet the keep rule: on every policy model at least
`minRunsPerArm` runs per arm and no lower success rate with the skill, and on at least one model a
higher success rate, or a median wall time or tool-call count cut by `minCutPercent` or more while
the with-skill arm succeeds. A skill without such a record fails.

`python tools/skill_evals.py digest <server checkout> <skill>` prints the digest to record.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import NamedTuple

import source_facts  # noqa: F401  puts certification/ on sys.path
from execution_order import instant  # noqa: E402  the admission parser: UTC 'Z' stamps only

SKILLS_DIR = 'skills'
RESULT_SCHEMA = 'honua.skill-eval/v1'
POLICY_KEYS = frozenset({'resultsDir', 'models', 'minRunsPerArm', 'minCutPercent', 'maxAgeDays'})
REQUIRED_MODELS = frozenset({'opus', 'sol'})
MIN_RUNS_FLOOR = 3
ARM_COUNTS = ('runs', 'successes')
ARM_MEDIANS = ('medianSeconds', 'medianToolCalls')
SHA = re.compile(r'[0-9a-f]{40}')


class PolicyError(Exception):
    pass


class SkillContext(NamedTuple):
    """The ledger policy, the release repository root the records live under, and the check time."""
    policy: dict
    root: Path
    now: dt.datetime


def _positive(policy: dict, key: str) -> int:
    if type(policy[key]) is not int or policy[key] <= 0:
        raise PolicyError(f'ledger skillEvaluations.{key} must be a positive integer')
    return policy[key]


def check_policy(policy) -> dict:
    """The ledger's `skillEvaluations` block; a malformed or weakened block fails the gate closed."""
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        raise PolicyError(f'ledger skillEvaluations keys must be exactly {sorted(POLICY_KEYS)}')
    models = policy['models']
    if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
        raise PolicyError('ledger skillEvaluations.models must be a list of strings')
    if not REQUIRED_MODELS <= set(models):
        raise PolicyError(f'ledger skillEvaluations.models must include {sorted(REQUIRED_MODELS)}')
    if _positive(policy, 'minRunsPerArm') < MIN_RUNS_FLOOR:
        raise PolicyError(f'ledger skillEvaluations.minRunsPerArm must be at least {MIN_RUNS_FLOOR}')
    _positive(policy, 'minCutPercent')
    _positive(policy, 'maxAgeDays')
    results = policy['resultsDir']
    if not isinstance(results, str) or not _inside(results):
        raise PolicyError('ledger skillEvaluations.resultsDir must be a relative path inside the repository')
    return policy


def _inside(relative: str) -> bool:
    path = PurePosixPath(relative)
    return relative != '' and not path.is_absolute() and '..' not in path.parts


def _git(checkout: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(checkout), *args], text=True,
                                   stderr=subprocess.DEVNULL)


def shipped_skills(checkout: Path) -> list[str]:
    """Skills committed at HEAD: each `skills/<name>/SKILL.md`."""
    paths = _git(checkout, 'ls-tree', '-r', '--name-only', 'HEAD', '--', SKILLS_DIR).splitlines()
    return sorted(PurePosixPath(p).parts[1] for p in paths
                  if len(PurePosixPath(p).parts) == 3 and PurePosixPath(p).name == 'SKILL.md')


def tree_digest(checkout: Path, name: str) -> str:
    """sha256 over each committed file's path and blob id under `skills/<name>/` at HEAD."""
    listing = _git(checkout, 'ls-tree', '-r', 'HEAD', '--', f'{SKILLS_DIR}/{name}/')
    entries = sorted(line.split('\t', 1)[1] + '\0' + line.split()[2] for line in listing.splitlines())
    return 'sha256:' + hashlib.sha256('\n'.join(entries).encode()).hexdigest()


def _arm(value) -> bool:
    if not isinstance(value, dict):
        return False
    counts = all(type(value.get(field)) is int and value[field] >= 0 for field in ARM_COUNTS)
    medians = all(type(value.get(field)) in (int, float) and math.isfinite(value[field])
                  and value[field] >= 0 for field in ARM_MEDIANS)
    return counts and medians and value['successes'] <= value['runs']


def _cut(without: float, with_skill: float, percent: int) -> bool:
    return without > 0 and (without - with_skill) * 100 >= percent * without


def model_verdict(arms, policy: dict) -> tuple[str | None, bool]:
    """(gap, gain) for one model: a gap fails the skill; a gain is what the keep rule asks for."""
    if not isinstance(arms, dict) or not _arm(arms.get('withSkill')) or not _arm(arms.get('withoutSkill')):
        return 'arms withSkill and withoutSkill must each carry runs, successes and finite medians', False
    treated, control = arms['withSkill'], arms['withoutSkill']
    if min(treated['runs'], control['runs']) < policy['minRunsPerArm']:
        return f"fewer than {policy['minRunsPerArm']} runs per arm", False
    if treated['successes'] * control['runs'] < control['successes'] * treated['runs']:
        return f"success fell with the skill ({treated['successes']}/{treated['runs']} vs {control['successes']}/{control['runs']})", False
    percent = policy['minCutPercent']
    faster = treated['successes'] > 0 and (
        _cut(control['medianSeconds'], treated['medianSeconds'], percent)
        or _cut(control['medianToolCalls'], treated['medianToolCalls'], percent))
    return None, treated['successes'] * control['runs'] > control['successes'] * treated['runs'] or faster


def provenance_gap(result: dict, context: SkillContext) -> str | None:
    if not isinstance(result.get('serverSha'), str) or not SHA.fullmatch(result['serverSha']):
        return 'serverSha must be the 40-character server commit the runs used'
    observed = instant(result.get('observedAt'))
    limit = context.policy['maxAgeDays']
    if observed is None or observed > context.now or context.now - observed > dt.timedelta(days=limit):
        return f'observedAt must be a UTC stamp no older than {limit} days'
    evidence = result.get('evidence')
    if not isinstance(evidence, str) or not (
            evidence.startswith('https://') or (_inside(evidence) and (context.root / evidence).is_file())):
        return 'evidence must be an https URL or a file in this repository'
    return None


def keep_gap(result: dict, policy: dict) -> str | None:
    models = result.get('models') if isinstance(result.get('models'), dict) else {}
    gains = []
    for model in policy['models']:
        gap, gain = model_verdict(models.get(model), policy)
        if gap:
            return f'{model}: {gap}'
        gains.append(gain)
    if not any(gains):
        return (f"no model shows higher success, or a {policy['minCutPercent']}% cut in time or tool calls "
                'with successful runs')
    return None


def result_gap(result, row: dict, context: SkillContext) -> str | None:
    if not isinstance(result, dict) or result.get('schema') != RESULT_SCHEMA or result.get('skill') != row['skill']:
        return f"evaluation result is not a {RESULT_SCHEMA} record for {row['skill']}"
    if result.get('skillTreeSha256') != row['skillTreeSha256']:
        return f"evaluation ran against other skill content (candidate {row['skillTreeSha256']})"
    return provenance_gap(result, context) or keep_gap(result, context.policy)


def _read(path: Path):
    return json.loads(path.read_text())


def evaluate_skills(checkout: Path, context: SkillContext) -> list[dict]:
    """One row per shipped skill, with the reason it fails or None."""
    rows = []
    for name in shipped_skills(checkout):
        row = {'skill': name, 'skillTreeSha256': tree_digest(checkout, name), 'gap': None}
        path = context.root / context.policy['resultsDir'] / f'{name}.json'
        try:
            row['gap'] = result_gap(_read(path), row, context) if path.is_file() else \
                'no with/without evaluation result recorded'
        except (OSError, ValueError) as error:
            row['gap'] = f'evaluation result is unreadable: {error}'
        rows.append(row)
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != 'digest':
        print('usage: skill_evals.py digest <server checkout> <skill>', file=sys.stderr)
        return 2
    checkout, name = Path(argv[1]), argv[2]
    try:
        shipped = shipped_skills(checkout)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f'{checkout} is not a git checkout: {error}', file=sys.stderr)
        return 1
    if name not in shipped:
        print(f'{name} has no committed SKILL.md at HEAD of {checkout}', file=sys.stderr)
        return 1
    print(tree_digest(checkout, name))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
