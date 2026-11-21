"""The promise dispatcher sends every stackless producer the digest its receipt step verifies."""
import hashlib
from pathlib import Path
import subprocess

import pytest
import yaml

import kept_gate_receipt
import promise_dispatch as dispatch

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def write_manifest(path: Path, **server) -> Path:
    pins = {'sha': SHA, 'digest': DIGEST, **server}
    path.write_text(yaml.safe_dump({'platformRelease': '2026.1-rc.2', 'components': {'honua-server': pins}}))
    return path


def dispatch_inputs(workflow: str) -> dict:
    document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    return (document.get('on', document.get(True))['workflow_dispatch'] or {}).get('inputs') or {}


def test_digest_is_sha256_of_the_lock_bytes_the_receipt_step_regenerates(tmp_path):
    manifest = write_manifest(tmp_path / 'platform-manifest.yaml')
    seen = []

    def generate(manifest_path, matrix_path):
        seen.append((manifest_path, matrix_path))
        return b'{"lock": "exact bytes"}\n'

    digest = dispatch.qualification_lock_digest(manifest, generate)
    assert digest == 'sha256:' + hashlib.sha256(b'{"lock": "exact bytes"}\n').hexdigest()
    assert seen == [(manifest.resolve(), ROOT / 'compatibility-matrix.yaml')]


def test_digest_uses_ensure_lock_so_both_sides_regenerate_identically(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(kept_gate_receipt, 'ensure_lock',
                        lambda lock, manifest, generate=None: (calls.append(manifest), lock.write_bytes(b'x')))
    manifest = write_manifest(tmp_path / 'platform-manifest.yaml')
    assert dispatch.qualification_lock_digest(manifest) == 'sha256:' + hashlib.sha256(b'x').hexdigest()
    assert calls == [manifest.resolve()]


def test_every_promise_workflow_is_dispatched_or_excluded_with_a_reason():
    workflows = dispatch.contract_workflows()
    assert {'gate-release-outcomes.yml', 'gate-contract-live.yml', 'gate-docs.yml'} <= workflows
    assert workflows <= dispatch.DISPATCHED.keys() | dispatch.EXCLUDED.keys()
    assert not dispatch.DISPATCHED.keys() & dispatch.EXCLUDED.keys()
    assert all(reason.strip() for reason in dispatch.EXCLUDED.values())


@pytest.mark.parametrize('workflow', sorted(dispatch.DISPATCHED))
def test_dispatched_inputs_are_declared_and_required_inputs_supplied(workflow):
    declared = dispatch_inputs(workflow)
    inputs = dict(dispatch.plan({'server_sha': SHA, 'server_digest': DIGEST, 'label': 'rc'}, DIGEST))[workflow]
    assert set(inputs) <= set(declared), workflow
    assert {name for name, spec in declared.items() if spec.get('required')} <= set(inputs)
    for name, value in inputs.items():
        assert value in declared[name].get('options', [value]), (workflow, name)
    assert inputs['platform_lock_digest'] == DIGEST


def test_plan_fills_the_manifest_pins(tmp_path):
    pins = dispatch.candidate(write_manifest(tmp_path / 'm.yaml'))
    planned = dict(dispatch.plan(pins, 'sha256:' + 'c' * 64))
    assert planned['gate-source-facts.yml'] == {'platform_lock_digest': 'sha256:' + 'c' * 64,
                                                'server_sha': SHA, 'server_digest': DIGEST}
    assert planned['gate-contract.yml']['platform_label'] == '2026.1-rc.2'
    assert planned['gate-contract.yml']['enforcement'] == 'strict'


@pytest.mark.parametrize('server', [{'sha': 'de512e8'}, {'digest': 'pending'}, {'sha': ''}])
def test_manifest_without_a_full_server_pin_is_refused(tmp_path, server):
    with pytest.raises(ValueError, match='must pin honua-server'):
        dispatch.candidate(write_manifest(tmp_path / 'm.yaml', **server))


def fake_gh(commands, busy=(), failing=()):
    def run(argv, **_):
        commands.append(argv)
        if argv[:3] == ['gh', 'run', 'list']:
            status = 'in_progress' if argv[6] in busy else 'completed'
            return subprocess.CompletedProcess(argv, 0, f'[{{"databaseId": 7, "status": "{status}"}}]', '')
        return subprocess.CompletedProcess(argv, 1 if argv[3] in failing else 0, '', 'HTTP 422')
    return run


def test_dispatch_runs_gh_on_trunk_and_fails_when_one_dispatch_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: 'sha256:' + 'c' * 64)
    commands = []
    run = fake_gh(commands, failing={'gate-dr.yml'})

    summary = tmp_path / 'summary.md'
    manifest = write_manifest(tmp_path / 'm.yaml')
    code = dispatch.main(['--manifest', str(manifest), '--dispatch', '--summary', str(summary)], run=run)
    assert code == 1
    dispatched = [argv for argv in commands if argv[:3] == ['gh', 'workflow', 'run']]
    assert [argv[3] for argv in dispatched] == list(dispatch.DISPATCHED)
    assert all(argv[4:8] == ['--repo', dispatch.REPO, '--ref', 'trunk'] for argv in dispatched)
    text = summary.read_text()
    assert '| `gate-dr.yml` | FAILED: HTTP 422 |' in text and '| `gate-dr.yml` | dispatched |' not in text
    assert 'gate-upgrade.yml' in text and 'sha256:' + 'c' * 64 in capsys.readouterr().out


def test_print_mode_dispatches_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: DIGEST)
    manifest = write_manifest(tmp_path / 'm.yaml')
    assert dispatch.main(['--manifest', str(manifest)], run=lambda *a, **k: pytest.fail('dispatched')) == 0


def test_a_producer_with_a_trunk_run_in_flight_is_skipped_not_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: DIGEST)
    commands, summary = [], tmp_path / 'summary.md'
    manifest = write_manifest(tmp_path / 'm.yaml')
    argv = ['--manifest', str(manifest), '--dispatch', '--summary', str(summary), '--only', 'gate-contract.yml']
    assert dispatch.main(argv, run=fake_gh(commands, busy={'gate-contract.yml'})) == 0
    assert [command[6] for command in commands] == [*dispatch.TRAINS, 'gate-contract.yml']
    assert '| `gate-contract.yml` | skipped: run 7 in flight |' in summary.read_text()


def test_a_release_train_in_flight_holds_back_only_the_producers_it_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: DIGEST)
    commands, summary = [], tmp_path / 'summary.md'
    manifest = write_manifest(tmp_path / 'm.yaml')
    argv = ['--manifest', str(manifest), '--dispatch', '--summary', str(summary)]
    assert dispatch.main(argv, run=fake_gh(commands, busy={'nightly-certification.yml'})) == 0
    dispatched = {command[3] for command in commands if command[:3] == ['gh', 'workflow', 'run']}
    assert dispatched == {'gate-source-facts.yml'}
    assert dispatch.train_called() >= set(dispatch.DISPATCHED) - {'gate-source-facts.yml'}
    assert 'skipped: a release train is in flight (nightly-certification.yml run 7 in flight)' in summary.read_text()


def test_an_unresolvable_manifest_still_runs_the_scans_without_receipts(tmp_path):
    commands, summary = [], tmp_path / 'summary.md'
    manifest = write_manifest(tmp_path / 'm.yaml', digest='pending')
    argv = ['--manifest', str(manifest), '--dispatch', '--summary', str(summary)]
    assert dispatch.main(argv, run=fake_gh(commands)) == 1
    dispatched = {command[3]: command[8:] for command in commands if command[:3] == ['gh', 'workflow', 'run']}
    assert set(dispatched) == set(dispatch.DISPATCHED) - {'gate-source-facts.yml'}
    assert not any('platform_lock_digest' in field for fields in dispatched.values() for field in fields)
    assert dispatched['gate-contract.yml'] == ['-f', 'enforcement=strict']
    text = summary.read_text()
    assert 'none: no promise receipts' in text and '| `gate-source-facts.yml` | FAILED: needs the candidate pins' in text


def test_gate_dr_reads_the_drill_run_that_triggered_the_dispatch():
    pins = {'server_sha': SHA, 'server_digest': DIGEST, 'label': 'rc'}
    planned = dict(dispatch.plan(pins, DIGEST, {'gate-dr.yml'}, '4242'))
    assert planned == {'gate-dr.yml': {'platform_lock_digest': DIGEST, 'producer_run_id': '4242'}}
    assert 'producer_run_id' in dispatch_inputs('gate-dr.yml')


def test_only_and_skip_select_dispatched_producers():
    assert dispatch.select(['gate-dr.yml'], []) == {'gate-dr.yml'}
    assert dispatch.select([], ['gate-dr.yml']) == set(dispatch.DISPATCHED) - {'gate-dr.yml'}
    with pytest.raises(ValueError, match='gate-upgrade.yml'):
        dispatch.select(['gate-upgrade.yml'], [])


@pytest.mark.parametrize('workflow', sorted(dispatch.DISPATCHED))
def test_dispatched_producers_have_no_trunk_trigger_of_their_own(workflow):
    document = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
    assert not {'schedule', 'workflow_run', 'push'} & set(document.get('on', document.get(True))), workflow


def test_workflow_runs_only_on_trunk_with_actions_write_and_the_tool():
    document = yaml.safe_load((ROOT / '.github/workflows/dispatch-promise-producers.yml').read_text())
    triggers = document.get('on', document.get(True))
    assert set(triggers) == {'schedule', 'push', 'workflow_run', 'workflow_dispatch'}
    assert triggers['push']['branches'] == triggers['workflow_run']['branches'] == ['trunk']
    assert triggers['workflow_run']['workflows'] == ['dr-drill-local-docker']
    assert document['permissions'] == {'contents': 'read'}
    job = document['jobs']['dispatch']
    assert "github.ref == 'refs/heads/trunk'" in job['if']
    assert job['permissions'] == {'contents': 'read', 'actions': 'write'}
    assert '"success","failure"' in job['if'] and "github.event.workflow_run.event != 'pull_request'" in job['if']
    assert "!startsWith(github.event.workflow_run.display_title, 'dr-drill candidate ')" in job['if']
    run = '\n'.join(step.get('run', '') for step in job['steps'])
    assert 'tools/promise_dispatch.py --dispatch' in run
    assert 'workflow_run) args+=(--only gate-dr.yml --dr-producer-run "$DRILL_RUN")' in run and '*) args+=(--skip gate-dr.yml)' in run


def test_candidate_drills_are_titled_so_gate_dr_and_the_dispatcher_can_refuse_them():
    drill = yaml.safe_load((ROOT / '.github/workflows/dr-drill-local-docker.yml').read_text())
    assert "format('dr-drill candidate {0}', inputs.candidate_ref)" in drill['run-name']
    gate = (ROOT / '.github/workflows/gate-dr.yml').read_text()
    assert gate.count('startswith("dr-drill candidate ")') == 2
    assert '[ "$conclusion" != success ]' in gate


def test_a_skipped_drill_gate_fails_so_it_can_be_rerun(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: DIGEST)
    commands, manifest = [], write_manifest(tmp_path / 'm.yaml')
    argv = ['--manifest', str(manifest), '--dispatch', '--only', 'gate-dr.yml', '--dr-producer-run', '9']
    assert dispatch.main(argv, run=fake_gh(commands, busy={'release-train.yml'})) == 1
    assert not [command for command in commands if command[:3] == ['gh', 'workflow', 'run']]


def test_unreadable_manifest_and_failed_train_lookup_fail_the_dispatcher(tmp_path):
    (tmp_path / 'empty.yaml').write_text('')
    assert dispatch.resolve(tmp_path / 'empty.yaml')[2] and dispatch.resolve(tmp_path / 'missing.yaml')[2]
    manifest = write_manifest(tmp_path / 'm.yaml')
    failing = lambda argv, **_: subprocess.CompletedProcess(argv, 1, '', 'HTTP 502')  # noqa: E731
    assert dispatch.main(['--manifest', str(manifest), '--dispatch'], run=failing) == 1


def test_without_a_digest_gate_dr_still_reads_the_drill_it_was_given():
    assert dict(dispatch.plan({}, '', {'gate-dr.yml'}, '9')) == {'gate-dr.yml': {'producer_run_id': '9'}}
