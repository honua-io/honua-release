import json
from pathlib import Path

import resolver_dry_run_summary as summary


FIXTURES = Path(__file__).with_name('fixtures') / 'resolver-dry-run'


def test_all_resolved_writes_json_stdout_and_step_summary(tmp_path, monkeypatch, capsys):
    json_out, step_summary = tmp_path / 'result.json', tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(step_summary))
    assert summary.main([str(FIXTURES / 'all-resolved.txt'), '0', '--json-out', str(json_out)]) == 0
    report = json.loads(json_out.read_text())
    assert report == {'resolves': True, 'rows': [
        {'component': 'server', 'verdict': 'resolved', 'selected_sha': '1234567', 'age_days': 1.5,
         'newer_commits_skipped': 2, 'reason': None},
        {'component': 'sdk-python', 'verdict': 'resolved', 'selected_sha': 'abcdef0', 'age_days': 0.2,
         'newer_commits_skipped': 0, 'reason': None},
    ]}
    assert capsys.readouterr().out == step_summary.read_text()


def test_refusal_and_stale_rows_make_the_verdict_false(tmp_path, capsys):
    output = tmp_path / 'result.json'
    assert summary.main([str(FIXTURES / 'refused-stale.txt'), '1', '--json-out', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['resolves'] is False
    assert report['rows'][0]['verdict'] == 'stale'
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
    assert by_component['server']['verdict'] == 'stale'
    assert by_component['server']['selected_sha'] == '1234567'
    rendered = capsys.readouterr().out
    assert 'capability keys missing from the advertised set' in rendered
    assert 'package identity did not match the selected sha' in rendered


def test_org_token_is_passed_only_on_trunk():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/resolver-dry-run.yml').read_text()
    assert "if: github.ref == 'refs/heads/trunk'" in workflow
    assert "GH_TOKEN: ${{ github.ref == 'refs/heads/trunk' && secrets.RELEASE_GH_TOKEN || github.token }}" in workflow
    assert 'GH_TOKEN: ${{ secrets.RELEASE_GH_TOKEN }}' not in workflow
