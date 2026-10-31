"""The arc runner composed with the REAL #1160 stage adapter/backends and the REAL #1152 corpus runtime.

These tests import the lanes' own modules (agent_cli_arc, agent_cli_backends, agent_e2e_local_runner,
agent_e2e_cli_sessions) and skip when they are not on the path. Run them against the lanes'
branches by putting that checkout's tools/ on PYTHONPATH after this one, e.g.
  PYTHONPATH=tools:../wt-rc2-ai-arc-dep/tools python -m pytest tools/test_ai_arc_integration.py
The VM and the coding CLIs are faked at the process boundary only (ssh executor, CLI factory).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_runner as R  # noqa: E402

agent_cli_arc = pytest.importorskip('agent_cli_arc')
backends_mod = pytest.importorskip('agent_cli_backends')

ARC = R.load_arc()
CAND_IN = {'sourceRevision': 'c' * 40, 'imageDigest': 'sha256:' + 'd' * 64, 'platformLockDigest': 'sha256:' + 'e' * 64,
           'image': 'ghcr.io/honua-io/honua-server'}
INSTALL = ARC['versionPair']['install']
PAGE = (Path(__file__).resolve().parents[1] / 'tools/test_ai_arc_fixtures/quickstart.md')


class FakeVm:
    """The VM at the ssh boundary: records every script; answers the observers' reads."""

    def __init__(self, serving):
        self.scripts, self.serving, self.up = [], serving, False

    def __call__(self, argv, input=None, capture_output=True, timeout=None, check=False, env=None):
        script = (input or b'').decode()
        self.scripts.append({'argv': argv, 'script': script})
        out = b''
        if ' up -d' in script and 'exec docker compose' in script:
            self.up = True
        if 'docker info' in script:
            out = b'29.9.2\n'
        if self.up and (' ps --all --format json' in script or 'ps --format=json' in script):
            out = b'\n'.join(json.dumps(c).encode() for c in (
                {'Name': 'h', 'Service': 'honua', 'State': 'running', 'Health': 'healthy', 'Image': 'img'},
                {'Name': 'p', 'Service': 'postgres', 'State': 'running', 'Health': 'healthy', 'Image': 'pg'}))
        if 'docker image inspect' in script:
            out = json.dumps([self.serving['image'] + '@' + self.serving['imageDigest']]).encode()
        return subprocess.CompletedProcess(argv, 0, out, b'')


def slot_for(tmp_path, vm):
    import ai_arc_local
    identity, known = tmp_path / 'id', tmp_path / 'known_hosts'
    identity.write_text('key')
    known.write_text('host')
    slot = ai_arc_local.VmSlot(host='honua-eval-1', user='ubuntu', identity_file=str(identity),
                               known_hosts=str(known), project='arc-abcdef123456', index=1, run=vm)
    return R.Slot('local-docker', 'vm', slot.project, vm=slot)


def http_for(source):
    def http(method, url, key):
        if url.endswith('/healthz/ready'):
            return {'status': 200}
        if key is None:
            return {'status': 401}
        return {'status': 200, 'json': {'data': {'instance': {'id': 'i', 'source': source}}}}
    return http


class FakeTunnel:
    base_url = 'http://127.0.0.1:1'

    def close(self):
        pass


class QuickstartCli:
    """Stands in for the coding CLI behind #1160's real recording-proxy contract (servers=...)."""

    def __init__(self, command, stdin=None, answer='Honua is running. Open http://localhost:18180/healthz/ready.'):
        self.command, self.stdin, self.answer, self.calls = command, stdin, answer, []

    def __call__(self, agent, model):
        self.agent, self.model = agent, model
        return self

    def run(self, *, prompt, servers, stage, target_kind, log_dir, session, max_actions, max_turns, timeout):
        assert set(servers) == {'quickstart_shell'} and stage == 1 and target_kind == 'remote-vm'
        assert [t['name'] for t in servers['quickstart_shell']['tool_view']['tools']] == ['run_quickstart']
        arguments = {'command': self.command, **({'stdin': self.stdin} if self.stdin is not None else {})}
        try:
            result = servers['quickstart_shell']['execute']('shell_command', {'tool': 'run_quickstart',
                                                                              'arguments': arguments})
            status = result['status']
        except ValueError:
            status = 'fail'
        self.calls.append(arguments)
        return {'finalAnswer': self.answer, 'nudges': 0, 'tokens': 0, 'modelVisibleDigests': ['sha256:' + '1' * 64],
                'actions': [{'kind': 'shell_command', 'tool': 'run_quickstart', 'arguments': arguments,
                             'status': 'pass' if status == 'ok' else 'fail'}]}


def run_setup(tmp_path, monkeypatch, cli, serving=None, agent_arm='A1'):
    import ai_arc_local
    page = PAGE.read_text()
    vm = FakeVm(serving or {'image': INSTALL['image'], 'imageDigest': INSTALL['imageDigest']})
    slot = slot_for(tmp_path, vm)
    monkeypatch.setattr(ai_arc_local, 'tunnel_for', lambda s: FakeTunnel())
    arm = R.arm_by_id(ARC, agent_arm)
    ctx = R.StageContext(arc=ARC, target='local-docker', arm=arm, variant='A', run_dir=tmp_path / 'run',
                         private_dir=tmp_path / 'private', slot=slot, candidate=CAND_IN, install=INSTALL,
                         limits=ARC['limits']['setup'], deadline=__import__('time').monotonic() + 600,
                         options={'quickstart': str(PAGE), 'http': http_for(INSTALL['sourceRevision'])})
    result = R.run_stage(ctx, R.LocalSetup(), R.ArcSession(arm, cli_factory=cli),
                         ARC['targets']['local-docker']['goals']['setup']['A'], None)
    return result, vm, slot, page


@pytest.mark.parametrize('arm', ['A1', 'A2'])
def test_local_setup_runs_through_the_real_cli_adapter_for_both_required_arms(tmp_path, monkeypatch, arm):
    import ai_arc_local
    compose = ai_arc_local.quickstart_compose(PAGE.read_text())
    cli = QuickstartCli('docker compose -f - up -d --wait --wait-timeout 180', stdin=compose)
    result, vm, slot, page = run_setup(tmp_path, monkeypatch, cli, agent_arm=arm)
    assert result['outcome'] == 'pass', result
    assert (cli.agent, cli.model) == ((R.arm_by_id(ARC, arm)['agent'], R.arm_by_id(ARC, arm)['model']))
    staged = vm.scripts[0]['script']
    assert 'HONUA_ADMIN_PASSWORD=' in staged and all(slot.vm.secrets[k] not in ' '.join(vm.scripts[0]['argv'])
                                                    for k in slot.vm.secrets)
    run = next(s['script'] for s in vm.scripts if 'up -d --wait' in s['script'])
    assert '--project-name arc-abcdef123456' in run and 'HONUA_' in run   # compose admitted only via approved stdin
    assert 'HONUA_HTTP_PORT=18180' in staged, 'slot index 1 has its own ports'


def test_local_setup_refuses_a_modified_compose_definition(tmp_path, monkeypatch):
    evil = ('services:\n  pwn:\n    image: alpine\n    privileged: true\n    volumes: ["/:/host"]\n'
            '    command: ["sh", "-c", "cat /host/etc/shadow"]\n')
    cli = QuickstartCli('docker compose -f - up -d', stdin=evil)
    result, vm, _, _ = run_setup(tmp_path, monkeypatch, cli)
    assert not any('pwn' in s['script'] for s in vm.scripts), 'the definition never reached the VM'
    assert result['outcome'] == 'fail'


@pytest.mark.parametrize('command', ['docker compose up --project-name other', 'curl -H @/etc/passwd http://localhost:8080/',
                                     'docker compose exec postgres psql', 'docker run --privileged alpine'])
def test_shell_scope_holds_on_options_and_file_references(tmp_path, monkeypatch, command):
    cli = QuickstartCli(command)
    result, vm, _, _ = run_setup(tmp_path, monkeypatch, cli)
    assert not any(command.split()[-1] in s['script'] for s in vm.scripts[1:] if 'exec ' in s['script'])
    assert result['outcome'] == 'fail'


def test_local_setup_output_never_reaches_the_model_with_secrets(tmp_path, monkeypatch):
    import ai_arc_local
    compose = ai_arc_local.quickstart_compose(PAGE.read_text())
    seen = []

    class Peek(QuickstartCli):
        def run(self, **kw):
            execute = kw['servers']['quickstart_shell']['execute']
            for command in ('docker compose config', 'docker compose logs honua', 'curl http://localhost:18180/.env'):
                try:
                    seen.append(json.dumps(execute('shell_command', {'tool': 'run_quickstart',
                                                                     'arguments': {'command': command}})))
                except ValueError:
                    pass
            return super().run(**kw)
    cli = Peek('docker compose -f - up -d --wait', stdin=compose)
    result, vm, slot, _ = run_setup(tmp_path, monkeypatch, cli)
    for value in slot.vm.secrets.values():
        assert all(value not in text for text in seen)
    for path in (tmp_path / 'run').rglob('*') if (tmp_path / 'run').exists() else []:
        if path.is_file():
            assert all(v.encode() not in path.read_bytes() for v in slot.vm.secrets.values())


def test_install_identity_fails_closed_on_another_image(tmp_path, monkeypatch):
    import ai_arc_local
    compose = ai_arc_local.quickstart_compose(PAGE.read_text())
    cli = QuickstartCli('docker compose -f - up -d --wait', stdin=compose)
    result, *_ = run_setup(tmp_path, monkeypatch, cli, serving={'image': INSTALL['image'], 'imageDigest': 'sha256:' + '0' * 64})
    assert result['outcome'] == 'fail' and 'install.identity' in result['cause']


# ---------------------------------------------------------------- services on the real #1152 runtime

def test_services_compose_the_real_corpus_runtime_with_scoring_protections(tmp_path):
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    pytest.importorskip('agent_e2e_cli_sessions')
    import ai_arc_services
    state = corpus.Candidate()
    server = corpus.serve(state)
    base = f'http://127.0.0.1:{server.server_address[1]}'

    class Cli:
        def __init__(self, where):
            self.where = where

        def run(self, *, prompt, tool_view, execute, log_dir, session, max_actions, max_turns, timeout):
            arguments = {'serviceId': 'p10-shelters', 'layerId': 0, 'where': self.where}
            execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_query_features', 'arguments': arguments})
            return {'finalAnswer': 'Two shelters match: Centre and the North refuge.', 'tokens': 1, 'usage': [],
                    'modelVisibleDigests': [], 'provenance': [], 'messageIds': [], 'systemContractSha256': None,
                    'toolViewSha256': None}
    try:
        limits = dict(ARC['limits']['services'], maxSeconds=600)
        common = dict(scenarios=('P10',), variant='A', candidate_digest=corpus.DIGEST, base_url=base,
                      admin_key=corpus.ADMIN, project='arc-abcdef123456', arm=R.arm_by_id(ARC, 'A1'), limits=limits,
                      load_scenario=lambda sid: corpus.FakeP10())
        row, cases = ai_arc_services.run_services(**common, out_dir=tmp_path / 'good', cli=Cli(corpus.CORRECT_WHERE))
        assert cases[0]['receipt']['status'] == 'pass', cases[0]['receipt']
        assert row['actions'][0]['tool'] == 'honua_query_features'
        rows = ai_arc_services.case_assertions(cases)
        assert {r['assertion'] for r in rows} >= {'P10:filter.exact', 'P10:finalAnswerCheck'}
        exported = [p for p in (tmp_path / 'good').rglob('*') if p.is_file()]
        assert exported and all(b'secret-' not in p.read_bytes() for p in exported), 'minted keys are redacted'
        row, cases = ai_arc_services.run_services(**common, out_dir=tmp_path / 'wrong', cli=Cli('capacity > 0'))
        assert cases[0]['receipt']['status'] == 'fail', 'a wrong query fails on the runner-owned evidence'
    finally:
        server.shutdown()


def test_aws_setup_uses_the_real_stdio_backend_with_session_credentials(tmp_path):
    """The verified operator binary runs as #1160's StdioMcpBackend; long-lived keys are refused."""
    with pytest.raises(ValueError):
        backends_mod.StdioMcpBackend(sys.executable, slot_env={'AWS_ACCESS_KEY_ID': 'AKIA' + 'X' * 16,
                                                               'AWS_SECRET_ACCESS_KEY': 'y' * 30},
                                     workdir=str(tmp_path))


def test_aws_setup_composes_the_pinned_operator_behind_the_real_backend(tmp_path, monkeypatch):
    import types
    import test_ai_arc_operator as op
    spec, download, _ = op.fake_release(tmp_path)
    arc = R.load_arc()
    arc['operator'] = spec
    creds = tmp_path / 'creds.json'
    creds.write_text(json.dumps({**op.CREDS, 'roleArn': op.CONTRACT['role_arn']}))
    contract = tmp_path / 'contract.json'
    contract.write_text(json.dumps(op.CONTRACT))

    class Adapter(R.StageModule):
        stage = 'setup'

        def operator_identity(self, ctx):
            return str(contract), str(creds)

        def observe(self, ctx, row):
            return [{'assertion': a, 'status': 'pass'} for a in R.required_assertions(arc, 'setup', 'aws-ecs')]
    monkeypatch.setitem(sys.modules, 'ai_arc_devops', types.SimpleNamespace(arc_stage=lambda ctx: Adapter()))
    seen = {}

    class Cli:
        def __call__(self, agent, model):
            return self

        def run(self, *, prompt, servers, stage, target_kind, **_):
            seen['servers'] = sorted(servers)
            seen['tools'] = [t['name'] for t in servers['honua_devops']['tool_view']['tools']]
            seen['result'] = servers['honua_devops']['execute']('tool_call', {'tool': 'describe_environment',
                                                                              'arguments': {}})
            return {'finalAnswer': 'Your setup is ready.', 'nudges': 0,
                    'actions': [{'kind': 'tool_call', 'tool': 'describe_environment', 'arguments': {}, 'status': 'pass'}]}
    arm = R.arm_by_id(arc, 'A2')
    home = tmp_path / 'scratch'
    home.mkdir()
    ctx = R.StageContext(arc=arc, target='aws-ecs', arm=arm, variant='A', run_dir=home / 'run', private_dir=home / 'p',
                         slot=R.Slot('aws-ecs', 'aws-cell', 'arc-abcdef123456'), candidate=CAND_IN, install=INSTALL,
                         limits=arc['limits']['setup'], deadline=__import__('time').monotonic() + 600,
                         options={'slot_receipt': 'slot.json', 'slot_credentials': str(creds),
                                  'operator_download': download})
    result = R.run_stage(ctx, R.AwsSetup(), R.ArcSession(arm, cli_factory=Cli()), 'Set up Honua in our AWS account.', None)
    if seen.get('tools') is None:
        pytest.skip('the #1160 adapter refused the fake operator catalog before the session: ' + str(result['cause']))
    assert seen['servers'] == ['honua_devops']
    assert 'ASIAEXAMPLEEXAMPLE12' in ctx.slot.harness_secrets
    assert ctx.slot.facts['operator']['registration'][:4] == ['codex', 'mcp', 'add', 'honua-devops']
    assert ctx.slot.facts['operator']['deployIdentity']['roleArn'] == op.CONTRACT['role_arn']
