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
    assert report == {'resolves': True, 'no_candidate_yet': False, 'rows': [
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


def _resolve_arguments(name):
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows' / name).read_text()
    command = workflow.split('resolve_trunk_candidate.py', 1)[1].replace('\\\n', ' ')
    return command.split('>', 1)[0].split('||', 1)[0].split()


def test_dry_run_resolves_exactly_as_the_nightly():
    nightly = _resolve_arguments('nightly-certification.yml')
    assert '--protocol-ledger' in nightly and '--max-commits' in nightly
    assert _resolve_arguments('resolver-dry-run.yml') == nightly


def missing_images_log(reason='no published SHA-bound image'):
    return ('REFUSED: honua-server: no qualifying trunk commit in newest 2 commits; '
            f'{"a" * 40}: no published SHA-bound image; {"b" * 40}: {reason}\n')


@pytest.mark.parametrize('order', ['stderr-first', 'stdout-first'])
def test_missing_server_images_are_waiting_without_resolving(order):
    selection = 'RESOLVED honua-sdk-js abcdef0123456789\n'
    skips = ('SKIPS honua-server: no qualifying trunk commit; 2 commit(s) skipped\n'
             '  skipped aaaaaaa: no published SHA-bound image\n'
             '  skipped bbbbbbb: no published SHA-bound image\n')
    text = missing_images_log() + skips + selection if order == 'stderr-first' else skips + selection + missing_images_log()
    report = summary.parse(text, 1)
    assert report['no_candidate_yet'] is True
    assert report['resolves'] is False
    assert 'no candidate yet' in summary.markdown(report)


@pytest.mark.parametrize('extra', [
    'honua-sdk-python: package identity missing\n',
    'REFUSED: honua-helm: image digest missing\n',
    'unexpected exception\n',
])
def test_missing_images_do_not_hide_additional_failures(extra):
    assert summary.parse(missing_images_log() + extra, 1)['no_candidate_yet'] is False


@pytest.mark.parametrize('reason', ['CI failed', 'image provenance mismatch', 'registry unavailable'])
def test_missing_images_do_not_hide_older_commit_failures(reason):
    assert summary.parse(missing_images_log(reason), 1)['no_candidate_yet'] is False


@pytest.mark.parametrize('status', [0, 2, 75, 137])
def test_only_normal_refusal_can_wait_for_images(status):
    assert summary.parse(missing_images_log(), status)['no_candidate_yet'] is False


def test_incomplete_commit_listing_does_not_establish_missing_images():
    text = missing_images_log().replace('newest 2 commits', 'newest 3 commits')
    assert summary.parse(text, 1)['no_candidate_yet'] is False


def test_only_resolution_job_is_skipped_while_capture_preserves_evidence():
    import yaml
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/resolver-dry-run.yml').read_text()
    jobs = yaml.safe_load(workflow)['jobs']
    assert jobs['resolve']['needs'] == 'capture'
    assert jobs['resolve']['if'] == "needs.capture.outputs.no_candidate_yet != 'true'"
    assert jobs['capture']['outputs']['no_candidate_yet'] == '${{ steps.summary.outputs.no_candidate_yet }}'
    assert any(step.get('if') == 'always()' and 'upload-artifact@' in step.get('uses', '')
               for step in jobs['capture']['steps'])
    assert 'RESOLVES: ${{ needs.capture.outputs.resolves }}' in workflow
