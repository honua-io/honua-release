"""The night ends as "no candidate yet" while no trunk commit has a published server image."""
import pytest

from test_nightly_certification import evaluate, github, step, workflow  # noqa: F401


# While no trunk commit has a published SHA-bound server image there is nothing to certify: the night
# says "no candidate yet" and skips the rest instead of going red. Any other refusal still fails.
MISSING_IMAGES = (
    'REFUSED: honua-server: no qualifying trunk commit in newest 2 commits; '
    f'{"a" * 40}: no published SHA-bound image; {"b" * 40}: no published SHA-bound image\n'
    'SKIPS honua-server: no qualifying trunk commit; 2 commit(s) skipped\n'
    '  skipped aaaaaaa: no published SHA-bound image\n'
    '  skipped bbbbbbb: no published SHA-bound image\n'
    'RESOLVED honua-sdk-js abcdef0123456789\n')


def fake_resolver(text, status):
    return {'tools/resolve_trunk_candidate.py':
            f'import sys\nsys.stdout.write({text!r})\nsys.exit({status})\n'}


def test_missing_server_images_end_the_night_as_no_candidate_yet(github, tmp_path):
    result, outputs = github.run_step('resolve', 'Resolve the newest qualifying trunk candidate',
                                      {'GH_TOKEN': 'x', 'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary.md')}, fake_resolver(MISSING_IMAGES, 1))
    assert result.returncode == 0, result.stdout + result.stderr
    assert outputs['no_candidate_yet'] == 'true'
    assert 'no published SHA-bound image' in result.stdout
    assert 'no-candidate-yet' in result.stdout
    assert 'no candidate yet' in (tmp_path / 'summary.md').read_text()


@pytest.mark.parametrize('text,status', [
    (MISSING_IMAGES + 'REFUSED: honua-helm: image digest missing\n', 1),
    ('REFUSED: honua-server: capability keys missing from the advertised set\n', 1),
    (MISSING_IMAGES, 2),
    ('garbage\n', 1),
])
def test_other_refusals_still_fail_the_night(github, tmp_path, text, status):
    result, outputs = github.run_step('resolve', 'Resolve the newest qualifying trunk candidate',
                                      {'GH_TOKEN': 'x', 'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary.md')}, fake_resolver(text, status))
    assert result.returncode == status, result.stdout + result.stderr
    assert outputs.get('no_candidate_yet') != 'true'


def test_registry_outage_keeps_its_own_exit_status(github, tmp_path):
    result, _ = github.run_step('resolve', 'Resolve the newest qualifying trunk candidate',
                                {'GH_TOKEN': 'x', 'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary.md')}, fake_resolver('registry down\n', 75))
    assert result.returncode == 75
    assert 'transient-registry-unavailable' in result.stdout


def test_no_candidate_yet_skips_every_later_step_and_job():
    jobs = workflow()['jobs']
    resolve = jobs['resolve']
    resolver_step = step(resolve, 'Resolve the newest qualifying trunk candidate')
    assert resolver_step['id'] == 'resolver'
    assert resolve['outputs']['no_candidate_yet'] == '${{ steps.resolver.outputs.no_candidate_yet }}'
    later = resolve['steps'][resolve['steps'].index(resolver_step) + 1:]
    waiting = {'steps.resolver.outputs.no_candidate_yet': 'true'}
    for later_step in later:
        evidence = later_step.get('with', {}).get('name') in ('resolved-candidate-skips', 'resolved-candidate-outcome')
        assert evaluate(later_step.get('if', 'true'), waiting) is evidence, later_step.get('name') or later_step['with']
        assert evaluate(later_step.get('if', 'true'), {'steps.resolver.outputs.no_candidate_yet': 'false'})
    for name in ('deployment_qualification', 'expected_ga', 'capacity'):
        assert evaluate(jobs[name]['if'], {'needs.resolve.outputs.no_candidate_yet': 'true'}) is False, name
        assert evaluate(jobs[name]['if'], {'needs.resolve.outputs.no_candidate_yet': 'false'}) is True, name
