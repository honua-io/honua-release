import json
from pathlib import Path
import subprocess
import sys

import pytest

import resolver_dry_run_summary as summary


FIXTURES = Path(__file__).with_name('fixtures') / 'resolver-dry-run'


SELECTION = (
    'RESOLVED honua-server abcdef0123456789\n'
    'SKIPS honua-server: selected abcdef0 (5.0 days old); 4 newer trunk commit(s) skipped\n'
    'STALE-CANDIDATE: honua-server selected abcdef0 (4 commits, 4.1 days behind)\n'
)
REFUSAL = (
    'REFUSED: honua-server: capability keys missing from the advertised set\n'
    'honua-helm: image digest missing\n'
)


def test_successful_stale_candidate_remains_resolved(tmp_path, capsys):
    log, output = tmp_path / 'run.log', tmp_path / 'result.json'
    log.write_text(SELECTION + 'PASS: exact trunk candidate; dry_run=True\n')
    assert summary.main([str(log), '0', '--json-out', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['resolves'] is True
    assert report['rows'][0]['verdict'] == 'resolved'
    assert report['rows'][0]['stale'] is True
    assert '| honua-server | resolved | yes | abcdef0 |' in capsys.readouterr().out


@pytest.mark.parametrize('text', [REFUSAL + SELECTION, SELECTION + REFUSAL])
@pytest.mark.parametrize('status', [0, 1])
def test_refusal_survives_selection_markers_in_either_stream_order(text, status):
    report = summary.parse(text, status)
    assert report['resolves'] is False
    row = report['rows'][0]
    assert row['verdict'] == 'refused'
    assert row['stale'] is True
    assert row['selected_sha'] == 'abcdef0'
    assert row['reason'] == REFUSAL.removeprefix('REFUSED: ').rstrip()


def test_actual_redirected_stdout_preserves_earlier_stderr_refusal(tmp_path):
    log = tmp_path / 'buffered.log'
    # The workflow merges buffered stdout and stderr into the same regular file.
    code = ('import sys\n'
            f'sys.stdout.write({SELECTION!r})\n'
            f'sys.stderr.write({REFUSAL!r})\n'
            'sys.exit(1)\n')
    with log.open('w') as output:
        run = subprocess.run([sys.executable, '-E', '-c', code], stdout=output,
                             stderr=subprocess.STDOUT, check=False)
    text = log.read_text()
    assert text.startswith('REFUSED:')
    report = summary.parse(text, run.returncode)
    assert report['resolves'] is False
    assert report['rows'][0]['verdict'] == 'refused'
    assert report['rows'][0]['reason'] == REFUSAL.removeprefix('REFUSED: ').rstrip()


def test_stale_annotation_alone_does_not_establish_resolution():
    report = summary.parse(SELECTION.splitlines()[-1], 0)
    assert report['resolves'] is False


def test_nonzero_exit_never_resolves_selected_components():
    assert summary.parse(SELECTION, 1)['resolves'] is False


def test_all_resolved_writes_json_stdout_and_step_summary(tmp_path, monkeypatch, capsys):
    json_out, step_summary = tmp_path / 'result.json', tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(step_summary))
    assert summary.main([str(FIXTURES / 'all-resolved.txt'), '0', '--json-out', str(json_out)]) == 0
    report = json.loads(json_out.read_text())
    assert report == {'resolves': True, 'rows': [
        {'component': 'server', 'verdict': 'resolved', 'stale': False, 'selected_sha': '1234567', 'age_days': 1.5,
         'newer_commits_skipped': 2, 'reason': None},
        {'component': 'sdk-python', 'verdict': 'resolved', 'stale': False, 'selected_sha': 'abcdef0', 'age_days': 0.2,
         'newer_commits_skipped': 0, 'reason': None},
    ]}
    assert capsys.readouterr().out == step_summary.read_text()


def test_refusal_fails_while_preserving_staleness_annotation(tmp_path, capsys):
    output = tmp_path / 'result.json'
    assert summary.main([str(FIXTURES / 'refused-stale.txt'), '1', '--json-out', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['resolves'] is False
    assert report['rows'][0]['verdict'] == 'resolved'
    assert report['rows'][0]['stale'] is True
    assert report['rows'][1]['reason'] == 'sdk-python: no green trunk commit satisfied the package identity'
    assert '| sdk-python | refused |' in capsys.readouterr().out


def test_garbage_is_a_broken_run_and_writes_no_json(tmp_path):
    output = tmp_path / 'result.json'
    assert summary.main([str(FIXTURES / 'garbage.txt'), '1', '--json-out', str(output)]) == 2
    assert not output.exists()


def test_qualification_refusal_keeps_the_following_diagnostic_lines(tmp_path, capsys):
    output = tmp_path / 'result.json'
    assert summary.main([str(FIXTURES / 'qualification-refused.txt'), '1', '--json-out', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['resolves'] is False
    by_component = {row['component']: row for row in report['rows']}
    assert by_component['(resolver)']['reason'] == (
        'candidate qualification refused:\n'
        'honua-server: capability keys missing from the advertised set\n'
        'sdk-python: package identity did not match the selected sha')
    assert by_component['server']['verdict'] == 'resolved'
    assert by_component['server']['stale'] is True
    assert by_component['server']['selected_sha'] == '1234567'
    rendered = capsys.readouterr().out
    assert 'capability keys missing from the advertised set' in rendered
    assert 'package identity did not match the selected sha' in rendered


def test_org_token_is_passed_only_on_trunk():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/resolver-dry-run.yml').read_text()
    assert "if: github.ref == 'refs/heads/trunk'" in workflow
    assert "GH_TOKEN: ${{ github.ref == 'refs/heads/trunk' && secrets.RELEASE_GH_TOKEN || github.token }}" in workflow
    assert 'GH_TOKEN: ${{ secrets.RELEASE_GH_TOKEN }}' not in workflow
