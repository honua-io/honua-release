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
        assert value == 'strict' or 'options' not in declared[name] or value in declared[name]['options']
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


def test_dispatch_runs_gh_on_trunk_and_fails_when_one_dispatch_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: 'sha256:' + 'c' * 64)
    commands = []

    def run(argv, **_):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 1 if argv[3] == 'gate-dr.yml' else 0, '', 'HTTP 422')

    summary = tmp_path / 'summary.md'
    manifest = write_manifest(tmp_path / 'm.yaml')
    code = dispatch.main(['--manifest', str(manifest), '--dispatch', '--summary', str(summary)], run=run)
    assert code == 1
    assert [argv[3] for argv in commands] == list(dispatch.DISPATCHED)
    assert all(argv[:3] == ['gh', 'workflow', 'run'] and argv[4:8] == ['--repo', dispatch.REPO, '--ref', 'trunk']
               for argv in commands)
    text = summary.read_text()
    assert '| `gate-dr.yml` | FAILED: HTTP 422 |' in text and '| `gate-dr.yml` | dispatched |' not in text
    assert 'gate-upgrade.yml' in text and 'sha256:' + 'c' * 64 in capsys.readouterr().out


def test_print_mode_dispatches_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, 'qualification_lock_digest', lambda manifest: DIGEST)
    manifest = write_manifest(tmp_path / 'm.yaml')
    assert dispatch.main(['--manifest', str(manifest)], run=lambda *a, **k: pytest.fail('dispatched')) == 0


def test_workflow_runs_only_on_trunk_with_actions_write_and_the_tool():
    document = yaml.safe_load((ROOT / '.github/workflows/dispatch-promise-producers.yml').read_text())
    triggers = document.get('on', document.get(True))
    assert set(triggers) == {'schedule', 'push', 'workflow_dispatch'}
    assert triggers['push']['branches'] == ['trunk']
    assert document['permissions'] == {'contents': 'read'}
    job = document['jobs']['dispatch']
    assert "github.ref == 'refs/heads/trunk'" in job['if']
    assert job['permissions'] == {'contents': 'read', 'actions': 'write'}
    run = '\n'.join(step.get('run', '') for step in job['steps'])
    assert 'tools/promise_dispatch.py --dispatch' in run
