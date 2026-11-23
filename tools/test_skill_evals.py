"""Skill evaluation keep rule: a shipped skill passes only on a with/without result of its own content."""
import json

import pytest

import skill_evals

POLICY = {'resultsDir': 'evals', 'models': ['opus', 'sol'], 'minRunsPerArm': 3, 'minCutPercent': 20}


def arm(successes=3, seconds=300, calls=30, runs=3):
    return {'runs': runs, 'successes': successes, 'medianSeconds': seconds, 'medianToolCalls': calls}


def result(name='honua-ops', digest='sha256:x', **models):
    base = {'opus': {'withSkill': arm(), 'withoutSkill': arm(successes=2)},
            'sol': {'withSkill': arm(), 'withoutSkill': arm()}}
    return {'schema': skill_evals.RESULT_SCHEMA, 'skill': name, 'skillTreeSha256': digest,
            'models': {**base, **models}}


def gap(record):
    return skill_evals.result_gap(record, 'honua-ops', 'sha256:x', POLICY)


def test_higher_success_on_one_model_and_no_loss_on_the_other_passes():
    assert gap(result()) is None


@pytest.mark.parametrize('treated,control', [
    (arm(seconds=240), arm(seconds=300)),
    (arm(calls=24), arm(calls=30)),
    (arm(successes=3, runs=3), arm(successes=2, runs=3)),
])
def test_each_kind_of_gain_satisfies_the_keep_rule(treated, control):
    same = {'withSkill': arm(), 'withoutSkill': arm()}
    assert gap(result(opus={'withSkill': treated, 'withoutSkill': control}, sol=same)) is None


@pytest.mark.parametrize('models,expected', [
    ({'opus': {'withSkill': arm(seconds=250), 'withoutSkill': arm()},
      'sol': {'withSkill': arm(calls=25), 'withoutSkill': arm()}}, 'no model shows'),
    ({'sol': {'withSkill': arm(successes=1), 'withoutSkill': arm(successes=2)}}, 'sol: success fell'),
    ({'sol': {'withSkill': arm(runs=2, successes=2), 'withoutSkill': arm()}}, 'sol: fewer than 3 runs'),
    ({'sol': None}, 'sol: arms'),
    ({'sol': {'withSkill': arm(successes=4), 'withoutSkill': arm()}}, 'sol: arms'),
    ({'sol': {'withSkill': {**arm(), 'medianSeconds': '200'}, 'withoutSkill': arm()}}, 'sol: arms'),
])
def test_a_result_short_of_the_keep_rule_fails(models, expected):
    record = result(**models)
    if 'opus' not in models:
        record['models']['opus'] = {'withSkill': arm(), 'withoutSkill': arm(successes=2)}
    assert expected in gap(record)


def test_a_missing_model_fails():
    record = result()
    del record['models']['sol']
    assert gap(record).startswith('sol: arms')


@pytest.mark.parametrize('field,value', [('schema', 'other/v1'), ('skill', 'honua-services')])
def test_a_record_for_something_else_fails(field, value):
    assert 'is not a' in gap({**result(), field: value})


def test_a_record_of_other_skill_content_fails():
    assert 'other skill content' in gap(result(digest='sha256:y'))


def test_tree_digest_covers_every_file_and_its_path(tmp_path):
    skill = tmp_path / 'skill'
    (skill / 'references').mkdir(parents=True)
    (skill / 'SKILL.md').write_text('# s')
    first = skill_evals.tree_digest(skill)
    (skill / 'references' / 'a.md').write_text('a')
    second = skill_evals.tree_digest(skill)
    (skill / 'references' / 'a.md').rename(skill / 'references' / 'b.md')
    assert len({first, second, skill_evals.tree_digest(skill)}) == 3


def test_evaluate_skills_reads_each_shipped_skill(tmp_path):
    checkout, release = tmp_path / 'server', tmp_path / 'release'
    for name in ('honua-ops', 'honua-services'):
        (checkout / 'skills' / name).mkdir(parents=True)
        (checkout / 'skills' / name / 'SKILL.md').write_text(name)
    (checkout / 'skills' / 'notes').mkdir()
    digest = skill_evals.tree_digest(checkout / 'skills/honua-ops')
    (release / 'evals').mkdir(parents=True)
    (release / 'evals/honua-ops.json').write_text(json.dumps(result(digest=digest)))
    (release / 'evals/honua-services.json').write_text('{not json')
    rows = skill_evals.evaluate_skills(checkout, POLICY, release, lambda p: json.loads(p.read_text()))
    assert [(row['skill'], row['gap'] is None) for row in rows] == [('honua-ops', True), ('honua-services', False)]
    assert rows[1]['gap'].startswith('evaluation result is unreadable')


def test_a_candidate_without_skills_ships_none(tmp_path):
    assert skill_evals.evaluate_skills(tmp_path, POLICY, tmp_path, json.loads) == []


@pytest.mark.parametrize('policy', [None, {**POLICY, 'extra': 1}, {**POLICY, 'models': []},
                                    {**POLICY, 'minCutPercent': 0}, {**POLICY, 'resultsDir': ''}])
def test_a_malformed_policy_is_refused(policy):
    with pytest.raises(skill_evals.PolicyError):
        skill_evals.check_policy(policy)


def test_digest_command_prints_the_digest(tmp_path, capsys):
    (tmp_path / 'skills/honua-ops').mkdir(parents=True)
    (tmp_path / 'skills/honua-ops/SKILL.md').write_text('# ops')
    assert skill_evals.main(['digest', str(tmp_path), 'honua-ops']) == 0
    assert capsys.readouterr().out.strip() == skill_evals.tree_digest(tmp_path / 'skills/honua-ops')
    assert skill_evals.main(['digest', str(tmp_path), 'missing']) == 1
