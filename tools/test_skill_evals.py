"""Skill evaluation keep rule: a shipped skill passes only on a fresh with/without result of its own content."""
import datetime as dt
import json
import subprocess

import pytest

import skill_evals

POLICY = {'resultsDir': 'evals', 'models': ['opus', 'sol'], 'minRunsPerArm': 3, 'minCutPercent': 20,
          'maxAgeDays': 14}
NOW = dt.datetime.now(dt.timezone.utc)
STAMP = (NOW - dt.timedelta(days=1)).isoformat().replace('+00:00', 'Z')
ROW = {'skill': 'honua-ops', 'skillTreeSha256': 'sha256:x'}


def arm(successes=3, seconds=300, calls=30, runs=3):
    return {'runs': runs, 'successes': successes, 'medianSeconds': seconds, 'medianToolCalls': calls}


def result(digest='sha256:x', **models):
    base = {'opus': {'withSkill': arm(), 'withoutSkill': arm(successes=2)},
            'sol': {'withSkill': arm(), 'withoutSkill': arm()}}
    return {'schema': skill_evals.RESULT_SCHEMA, 'skill': 'honua-ops', 'skillTreeSha256': digest,
            'serverSha': 'a' * 40, 'observedAt': STAMP, 'evidence': 'https://example.test/evals/ops',
            'models': {**base, **models}}


def gap(record, root=None):
    context = skill_evals.SkillContext(POLICY, root or skill_evals.Path('/nonexistent'), NOW)
    return skill_evals.result_gap(record, ROW, context)


def commit_skills(checkout, files):
    checkout.mkdir(parents=True, exist_ok=True)
    git = ['git', '-C', str(checkout), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
    subprocess.run([*git, 'init', '-q'], check=True)
    for name, text in files.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', 'skills'], check=True)


def test_higher_success_on_one_model_and_no_loss_on_the_other_passes():
    assert gap(result()) is None


@pytest.mark.parametrize('treated,control', [
    (arm(seconds=240), arm(seconds=300)),
    (arm(calls=24), arm(calls=30)),
    (arm(successes=1, seconds=100), arm(successes=1)),
])
def test_each_kind_of_gain_satisfies_the_keep_rule(treated, control):
    same = {'withSkill': arm(), 'withoutSkill': arm()}
    assert gap(result(opus={'withSkill': treated, 'withoutSkill': control}, sol=same)) is None


ZERO = {'withSkill': arm(successes=0, seconds=10, calls=1), 'withoutSkill': arm(successes=0)}


@pytest.mark.parametrize('models,expected', [
    ({'opus': {'withSkill': arm(seconds=250), 'withoutSkill': arm()},
      'sol': {'withSkill': arm(calls=25), 'withoutSkill': arm()}}, 'no model shows'),
    ({'opus': ZERO, 'sol': ZERO}, 'no model shows'),
    ({'sol': {'withSkill': arm(successes=1), 'withoutSkill': arm(successes=2)}}, 'sol: success fell'),
    ({'sol': {'withSkill': arm(runs=2, successes=2), 'withoutSkill': arm()}}, 'sol: fewer than 3 runs'),
    ({'sol': None}, 'sol: arms'),
    ({'sol': {'withSkill': arm(successes=4), 'withoutSkill': arm()}}, 'sol: arms'),
    ({'sol': {'withSkill': {**arm(), 'medianSeconds': '200'}, 'withoutSkill': arm()}}, 'sol: arms'),
    ({'sol': {'withSkill': arm(), 'withoutSkill': arm(seconds=float('inf'))}}, 'sol: arms'),
    ({'sol': {'withSkill': arm(calls=float('nan')), 'withoutSkill': arm()}}, 'sol: arms'),
])
def test_a_result_short_of_the_keep_rule_fails(models, expected):
    assert expected in gap(result(**models))


def test_infinity_parsed_from_json_is_refused():
    record = json.loads(json.dumps(result()).replace('"medianSeconds": 300', '"medianSeconds": 1e309'))
    assert 'finite medians' in gap(record)


def test_a_missing_model_fails():
    record = result()
    del record['models']['sol']
    assert gap(record).startswith('sol: arms')


@pytest.mark.parametrize('field,value', [('schema', 'other/v1'), ('skill', 'honua-services')])
def test_a_record_for_something_else_fails(field, value):
    assert 'is not a' in gap({**result(), field: value})


def test_a_record_of_other_skill_content_fails():
    assert 'other skill content' in gap(result(digest='sha256:y'))


def days_ago(days):
    return (NOW - dt.timedelta(days=days)).isoformat().replace('+00:00', 'Z')


@pytest.mark.parametrize('field,value,expected', [
    ('serverSha', None, 'serverSha'), ('serverSha', 'abc', 'serverSha'), ('serverSha', 'A' * 40, 'serverSha'),
    ('observedAt', None, 'observedAt'), ('observedAt', 15, 'observedAt'), ('observedAt', -1, 'observedAt'),
    ('observedAt', '2026-11-22T00:00:00+00:00', 'observedAt'),
    ('evidence', None, 'evidence'), ('evidence', '', 'evidence'), ('evidence', 'http://x.test', 'evidence'),
    ('evidence', 'evals/missing.md', 'evidence'), ('evidence', '../outside.md', 'evidence'),
])
def test_a_record_without_provenance_fails(field, value, expected):
    value = days_ago(value) if type(value) is int else value
    assert expected in gap({**result(), field: value})


def test_evidence_may_be_a_file_in_the_repository(tmp_path):
    (tmp_path / 'evals').mkdir()
    (tmp_path / 'evals/ops.md').write_text('transcripts')
    assert gap({**result(), 'evidence': 'evals/ops.md'}, tmp_path) is None


def test_tree_digest_covers_committed_files_and_paths_only(tmp_path):
    commit_skills(tmp_path, {'skills/s/SKILL.md': '# s'})
    first = skill_evals.tree_digest(tmp_path, 's')
    (tmp_path / 'skills/s/scratch.swp').write_text('untracked')
    assert skill_evals.tree_digest(tmp_path, 's') == first
    commit_skills(tmp_path, {'skills/s/references/a.md': 'a'})
    second = skill_evals.tree_digest(tmp_path, 's')
    subprocess.run(['git', '-C', str(tmp_path), 'mv', 'skills/s/references/a.md', 'skills/s/references/b.md'], check=True)
    commit_skills(tmp_path, {})
    assert len({first, second, skill_evals.tree_digest(tmp_path, 's')}) == 3


def test_evaluate_skills_reads_each_committed_skill(tmp_path):
    checkout, release = tmp_path / 'server', tmp_path / 'release'
    commit_skills(checkout, {'skills/honua-ops/SKILL.md': 'ops', 'skills/honua-services/SKILL.md': 'svc',
                             'skills/notes/README.md': 'not a skill', 'skills/a/b/SKILL.md': 'nested'})
    digest = skill_evals.tree_digest(checkout, 'honua-ops')
    (release / 'evals').mkdir(parents=True)
    (release / 'evals/honua-ops.json').write_text(json.dumps(result(digest=digest)))
    (release / 'evals/honua-services.json').write_text('{not json')
    rows = skill_evals.evaluate_skills(checkout, skill_evals.SkillContext(POLICY, release, NOW))
    assert [(row['skill'], row['gap'] is None) for row in rows] == [('honua-ops', True), ('honua-services', False)]
    assert rows[1]['gap'].startswith('evaluation result is unreadable')


def test_a_candidate_without_skills_ships_none(tmp_path):
    commit_skills(tmp_path, {'README.md': 'x'})
    assert skill_evals.evaluate_skills(tmp_path, skill_evals.SkillContext(POLICY, tmp_path, NOW)) == []


@pytest.mark.parametrize('policy', [
    None, {**POLICY, 'extra': 1}, {**POLICY, 'models': []}, {**POLICY, 'models': ['opus']},
    {**POLICY, 'models': ['opus', 'sol', 3]}, {**POLICY, 'minRunsPerArm': 1}, {**POLICY, 'minRunsPerArm': 3.0},
    {**POLICY, 'minCutPercent': 0}, {**POLICY, 'minCutPercent': True}, {**POLICY, 'maxAgeDays': 0},
    {**POLICY, 'resultsDir': ''}, {**POLICY, 'resultsDir': '/tmp/evals'}, {**POLICY, 'resultsDir': '../evals'},
])
def test_a_malformed_or_weakened_policy_is_refused(policy):
    with pytest.raises(skill_evals.PolicyError):
        skill_evals.check_policy(policy)


def test_digest_command_prints_the_digest(tmp_path, capsys):
    commit_skills(tmp_path, {'skills/honua-ops/SKILL.md': '# ops'})
    assert skill_evals.main(['digest', str(tmp_path), 'honua-ops']) == 0
    assert capsys.readouterr().out.strip() == skill_evals.tree_digest(tmp_path, 'honua-ops')
    assert skill_evals.main(['digest', str(tmp_path), 'missing']) == 1
    assert skill_evals.main(['digest', str(tmp_path / 'nowhere'), 'honua-ops']) == 1
