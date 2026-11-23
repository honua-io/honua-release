#!/usr/bin/env python3
"""Skill evaluation criterion of the GA capability gate.

Every skill the candidate ships (`skills/<name>/SKILL.md` committed at the candidate SHA) needs a
recorded with/without evaluation in `certification/skill-evals/<name>.json`. The record is bound to
the skill by `skillTreeSha256`, a digest of the committed files under `skills/<name>/`, and to its
run by `serverSha`, `observedAt` (no older than `maxAgeDays`) and an `evidence` reference that is an
https URL or a file in this repository. Equivalence rule: a record run on another server commit
counts for the candidate only when the candidate commits the same skill content (equal digest) and
the record is still inside the freshness window; the receipt carries the oldest record's stamp so
admission rechecks that window. It must meet the keep rule: on every policy model at least
`minRunsPerArm` runs per arm and no lower success rate with the skill, and on at least one model a
higher success rate, or a median wall time or tool-call count over successful runs cut by
`minCutPercent` or more when both arms have successful runs (medians are null for an arm with none). A skill without such a record fails.

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
MIN_CUT_FLOOR = 10
MAX_AGE_CEILING = 7
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


def positive_int(value) -> bool:
    return type(value) is int and value > 0


def repo_file(root: Path, relative) -> bool:
    """A regular file inside `root` after symlinks resolve; never an absolute or escaping path."""
    if not isinstance(relative, str) or relative == '' or PurePosixPath(relative).is_absolute():
        return False
    path = (root / relative).resolve()
    return path.is_relative_to(root.resolve()) and path.is_file()


def _bounded(policy: dict, key: str, low: int, high: int | None = None) -> None:
    value = policy[key]
    if not positive_int(value) or value < low or (high is not None and value > high):
        bounds = f'between {low} and {high}' if high else f'at least {low}'
        raise PolicyError(f'ledger skillEvaluations.{key} must be an integer {bounds}')


def check_policy(policy) -> dict:
    """The ledger's `skillEvaluations` block; a malformed or weakened block fails the gate closed."""
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        raise PolicyError(f'ledger skillEvaluations keys must be exactly {sorted(POLICY_KEYS)}')
    models = policy['models']
    if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
        raise PolicyError('ledger skillEvaluations.models must be a list of strings')
    if not REQUIRED_MODELS <= set(models):
        raise PolicyError(f'ledger skillEvaluations.models must include {sorted(REQUIRED_MODELS)}')
    _bounded(policy, 'minRunsPerArm', MIN_RUNS_FLOOR)
    _bounded(policy, 'minCutPercent', MIN_CUT_FLOOR, 100)
    _bounded(policy, 'maxAgeDays', 1, MAX_AGE_CEILING)
    results = policy['resultsDir']
    path = PurePosixPath(results) if isinstance(results, str) and results else None
    if path is None or path.is_absolute() or '..' in path.parts:
        raise PolicyError('ledger skillEvaluations.resultsDir must be a relative path inside the repository')
    return policy


def _git(checkout: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(checkout), *args], text=True,
                                   stderr=subprocess.DEVNULL)


def _tree(checkout: Path, prefix: str) -> list[str]:
    """`git ls-tree -r -z` records at HEAD: unquoted, so any byte in a path survives."""
    return [record for record in _git(checkout, 'ls-tree', '-r', '-z', 'HEAD', '--', prefix).split('\0') if record]


def shipped_skills(checkout: Path) -> list[str]:
    """Skills committed at HEAD: each `skills/<name>/SKILL.md`."""
    paths = [PurePosixPath(record.split('\t', 1)[1]) for record in _tree(checkout, SKILLS_DIR)]
    return sorted(path.parts[1] for path in paths if len(path.parts) == 3 and path.name == 'SKILL.md')


def tree_digest(checkout: Path, name: str) -> str:
    """sha256 over each committed file's path, mode and blob id under `skills/<name>/` at HEAD."""
    entries = sorted(_tree(checkout, f'{SKILLS_DIR}/{name}/'), key=lambda record: record.split('\t', 1)[1])
    return 'sha256:' + hashlib.sha256('\n'.join(entries).encode()).hexdigest()


def _median(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _arm(value) -> bool:
    """Counts, and medians over successful runs: finite numbers, or null when no run succeeded."""
    if not isinstance(value, dict):
        return False
    if not all(type(value.get(field)) is int and value[field] >= 0 for field in ARM_COUNTS):
        return False
    if value['successes'] > value['runs']:
        return False
    if value['successes'] == 0:
        return all(field in value and (value[field] is None or _median(value[field])) for field in ARM_MEDIANS)
    return all(_median(value.get(field)) for field in ARM_MEDIANS)


def _cut(without: float, with_skill: float, percent: int) -> bool:
    return without > 0 and (without - with_skill) * 100 >= percent * without


def model_verdict(arms, policy: dict) -> tuple[str | None, bool]:
    """(gap, gain) for one model: a gap fails the skill; a gain is what the keep rule asks for."""
    if not isinstance(arms, dict) or not _arm(arms.get('withSkill')) or not _arm(arms.get('withoutSkill')):
        return 'arms withSkill and withoutSkill must each carry runs, successes and finite medians of successful runs', False
    treated, control = arms['withSkill'], arms['withoutSkill']
    if min(treated['runs'], control['runs']) < policy['minRunsPerArm']:
        return f"fewer than {policy['minRunsPerArm']} runs per arm", False
    if treated['successes'] * control['runs'] < control['successes'] * treated['runs']:
        return f"success fell with the skill ({treated['successes']}/{treated['runs']} vs {control['successes']}/{control['runs']})", False
    percent = policy['minCutPercent']
    faster = min(treated['successes'], control['successes']) > 0 and (
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
    if not (isinstance(evidence, str) and evidence.startswith('https://')) and not repo_file(context.root, evidence):
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
        row = {'skill': name, 'skillTreeSha256': tree_digest(checkout, name), 'observedAt': None, 'gap': None}
        path = context.root / context.policy['resultsDir'] / f'{name}.json'
        try:
            record = _read(path) if path.is_file() else None
            row['gap'] = 'no with/without evaluation result recorded' if record is None else \
                result_gap(record, row, context)
        except (OSError, ValueError) as error:
            row['gap'] = f'evaluation result is unreadable: {error}'
        else:
            row['observedAt'] = None if row['gap'] else record['observedAt']
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
