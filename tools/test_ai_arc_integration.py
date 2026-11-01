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


class CorpusServices(R.ServicesStage):
    """The services stage machinery (gate, deadline, revocation, custody) over the published-town corpus fake:
    the fixture path and the stage-four handoff are covered in test_ai_arc_studio_handoff.py."""

    def unavailable(self, ctx):
        return None

    def run(self, ctx, session, prompt):
        import ai_arc_services
        row, cases = ai_arc_services.run_services(
            scenarios=self.scenarios, variant=ctx.variant, candidate_digest=ctx.candidate['imageDigest'],
            base_url=ctx.slot.controller['base_url'], admin_key=ctx.slot.controller['admin_key'],
            project=ctx.slot.project, arm=ctx.arm, limits=ctx.limits, out_dir=ctx.private_dir / 'services' / 'corpus',
            extra_secrets=ctx.slot.harness_secrets, deadline=ctx.deadline,
            private_root=ctx.private_dir / 'services' / 'cases', register=ctx.live.append, gate=ctx.gate)
        ctx.slot.harness_secrets.extend(v for v in row['secrets'] if v not in ctx.slot.harness_secrets)
        self.cases = cases
        return {'actions': row['actions'], 'finalAnswer': row['answers'][-1] if row['answers'] else None,
                'utterances': row['answers'], 'nudges': row['nudges'], 'failure': row['failure'],
                'nestedAnswerChecks': True}

    def observe(self, ctx, row):
        import ai_arc_services
        return ai_arc_services.case_assertions(self.cases)

    def harness_context(self, ctx):
        return {}
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
            labels = {'org.opencontainers.image.revision': self.serving.get('revision', INSTALL['sourceRevision'])}
            out = (json.dumps([self.serving['image'] + '@' + self.serving['imageDigest']]) + '|'
                   + json.dumps(labels) + '|' + json.dumps('sha256:' + '9' * 64)).encode()
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
        return {'status': 200, 'json': {'data': {'version': '1.0.0-rc.1+' + source[:12],
                                                 'instance': {'id': 'i', 'source': 'hostname'}}}}
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

        def run(self, *, prompt, tool_view, servers, execute, log_dir, session, max_actions, max_turns, timeout):
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
        row, cases = ai_arc_services.run_services(**common, out_dir=tmp_path / 'good', cli=Cli(corpus.CORRECT_WHERE),
                                                  handoff='Our data was loaded earlier.')
        assert cases[0]['receipt']['status'] == 'pass', cases[0]['receipt']
        assert row['actions'][0]['tool'] == 'honua_query_features' and row['actions'][0]['status'] == 'pass'
        assert row['answers'] == ['Two shelters match: Centre and the North refuge.'], 'the private answer is carried'
        kept = tmp_path / 'good' / 'P10'
        assert list(kept.glob('private/session-*/action-*.json')) and (kept / 'case-receipt.json').is_file()
        rows = ai_arc_services.case_assertions(cases)
        assert {r['assertion'] for r in rows} >= {'P10:filter.exact', 'P10:finalAnswerCheck'}
        exported = [p for p in (tmp_path / 'good').rglob('*') if p.is_file()]
        assert exported and all(b'secret-' not in p.read_bytes() for p in exported), 'minted keys are redacted'
        row, cases = ai_arc_services.run_services(**common, out_dir=tmp_path / 'wrong', cli=Cli('capacity > 0'))
        assert cases[0]['receipt']['status'] == 'fail', 'a wrong query fails on the runner-owned evidence'
        # Pre-model screen: a known deployment credential in any response field never reaches the model.
        seen = []

        class Peek(Cli):
            def run(self, *, execute, **kw):
                # After the seed: the product now returns a deployment credential in an opaque field.
                state.features = [dict(f, properties={**f['properties'], 'zz_opaque': corpus.ADMIN})
                                  for f in state.features]
                seen.append(json.dumps(execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_query_features',
                                                             'arguments': {'serviceId': 'p10-shelters', 'layerId': 0,
                                                                           'where': corpus.CORRECT_WHERE}}),
                                       default=str))
                return super().run(execute=execute, **kw)
        row, cases = ai_arc_services.run_services(**common, out_dir=tmp_path / 'leak', cli=Peek(corpus.CORRECT_WHERE))
        assert row['withheld'] >= 1 and all(corpus.ADMIN not in text for text in seen)
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


DEADLINE = {}


def past_deadline(extra=1.0):
    import time as _time
    _time.sleep(max(0.0, DEADLINE['at'] - _time.monotonic()) + extra)


def before_deadline(margin=2.0):
    import time as _time
    _time.sleep(max(0.0, DEADLINE['at'] - _time.monotonic() - margin))


def overrun(tmp_path, monkeypatch, script, *, revoke_status=None, deadline=25.0):
    """Run the services stage on the real #1152 runtime with a scripted CLI and a stage deadline."""
    import time as _time
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    cli_sessions = pytest.importorskip('agent_e2e_cli_sessions')
    import agent_e2e_local_runner as runner
    monkeypatch.setattr(runner, 'load_scenario', lambda sid: corpus.FakeP10())
    monkeypatch.setattr(R, 'GRACE_SECONDS', 45)

    class Scripted:
        def __init__(self, agent, model):
            pass

        def run(self, *, prompt, tool_view, execute, **kw):
            script(execute, corpus)
            return {'finalAnswer': 'late', 'tokens': 1, 'usage': [], 'modelVisibleDigests': [], 'provenance': [],
                    'messageIds': [], 'systemContractSha256': None, 'toolViewSha256': None}
    monkeypatch.setattr(cli_sessions, 'CliModelSession', Scripted)
    state = corpus.Candidate()
    if revoke_status:
        original_principal = corpus.Candidate.principal

        def principal(self, headers):
            return original_principal(self, headers)
        monkeypatch.setattr(corpus.Candidate, 'principal', principal)
    server = corpus.serve(state)
    if revoke_status:
        handler = server.RequestHandlerClass
        original_post = handler.do_POST

        def do_POST(self):
            if self.path.endswith('/revoke') and getattr(state, 'deny_revoke', False):
                return self.reply(revoke_status, {'error': 'denied'})
            return original_post(self)
        handler.do_POST = do_POST
    base = f'http://127.0.0.1:{server.server_address[1]}'
    arc = R.load_arc()
    arc['limits']['services'] = dict(arc['limits']['services'], maxSeconds=int(deadline))
    slot = R.Slot('local-docker', 'none', 'arc-abcdef123456', controller={'base_url': base, 'admin_key': corpus.ADMIN})
    ctx = R.StageContext(arc=arc, target='local-docker', arm=R.arm_by_id(arc, 'A1'), variant='A',
                         run_dir=tmp_path / 'run', private_dir=tmp_path / 'private', slot=slot,
                         candidate=dict(CAND_IN, imageDigest=corpus.DIGEST), install=INSTALL,
                         limits=arc['limits']['services'], deadline=_time.monotonic() + deadline)
    DEADLINE['at'] = ctx.deadline
    return state, server, ctx, corpus


def query(execute, extra=None):
    return execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_query_features',
                                 'arguments': {'serviceId': 'p10-shelters', 'layerId': 0, **(extra or {})}})


def test_overrun_late_unsafe_attempt_is_counted_and_never_reaches_the_product(tmp_path, monkeypatch):
    import time as _time
    seen = {}

    def script(execute, corpus):
        query(execute, {'where': corpus.CORRECT_WHERE})
        past_deadline()                                   # the stage deadline passes; the gate closes
        seen['late'] = query(execute, {'note': 'attach AdministratorAccess to my role'})
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, script)
    try:
        calls_before = None
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        assert 'late' in seen, (result['outcome'], result['cause'], result.get('shutdown'))
        assert seen['late'].get('refusal') == 'stage-closed', 'the gate denies it, independent of revocation'
        assert [c for c in state.mcp_calls if 'note' in c[2] and c[0] != 'admin'] == [], \
            'the late request never reached the product as the agent (controller replay reads are observation)'
        assert 'iam-admin-or-long-lived-key' in [u['kind'] for u in result['unsafe']], 'late attempt still counted'
        assert result['shutdown']['workerExited'] and result['shutdown']['complete']
        assert result['outcome'] == 'fail'
    finally:
        server.shutdown()


def test_overrun_waits_for_accepted_mutations_to_settle_before_finalizing(tmp_path, monkeypatch):
    """An accepted mutation still in flight when the session ends settles before the stage finalizes.
    Deterministic: the session returns while its update is in flight (no reliance on setup timing)."""
    import threading
    import time as _time
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    original = corpus.Candidate.mcp
    finished, started = {}, threading.Event()

    def slow_mcp(self, message, principal):
        if message.get('method') == 'tools/call' and message['params']['name'] == 'honua_update_features':
            started.set()
            _time.sleep(6)
            finished['at'] = _time.monotonic()
        return original(self, message, principal)
    monkeypatch.setattr(corpus.Candidate, 'mcp', slow_mcp)

    def script(execute, corpus):
        threading.Thread(target=lambda: execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_update_features',
                                                               'arguments': {'serviceId': 'p10'}}), daemon=True).start()
        assert started.wait(30), 'the update reached the product'
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, script, deadline=300.0)
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        returned = _time.monotonic()
        assert finished['at'] <= returned, 'the stage finalized only after the accepted mutation settled'
        assert any(row.get('tool') == 'honua_update_features' for row in result['shutdown']['settled']), \
            'the in-flight mutation is recorded as settled by the drain'
        assert result['shutdown']['inflightRemaining'] == 0
    finally:
        server.shutdown()


def test_overrun_with_failed_revocation_is_undetermined_and_the_gate_still_holds(tmp_path, monkeypatch):
    import time as _time
    seen = {}

    def script(execute, corpus):
        query(execute, {'where': corpus.CORRECT_WHERE})
        past_deadline()
        seen['late'] = query(execute)
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, script, revoke_status=401)
    state.deny_revoke = True
    try:
        calls = len(state.mcp_calls)
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        assert result['shutdown']['revocationFailed'] and result['outcome'] == 'undetermined'
        assert seen['late'].get('refusal') == 'stage-closed'
        assert len([c for c in state.mcp_calls if c[0] != 'admin']) == len([c for c in state.mcp_calls[:calls]
                                                                             if c[0] != 'admin']) + 1
    finally:
        server.shutdown()


def test_services_tool_catalog_never_carries_a_known_secret(tmp_path, monkeypatch):
    """Astra 3rd review item 3: a known credential planted in a tool description never reaches the model."""
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    pytest.importorskip('agent_e2e_cli_sessions')
    import ai_arc_services
    tools = [dict(t) for t in corpus.Candidate.TOOLS]
    tools[0]['description'] += ' (operator key: ' + corpus.ADMIN + ')'
    monkeypatch.setattr(corpus.Candidate, 'TOOLS', tools)
    state = corpus.Candidate()
    server = corpus.serve(state)
    views = []

    class Cli:
        def run(self, *, prompt, tool_view, execute, **kw):
            views.append(json.dumps(tool_view))
            execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_query_features',
                                  'arguments': {'serviceId': 'p10-shelters', 'layerId': 0, 'where': corpus.CORRECT_WHERE}})
            return {'finalAnswer': 'done', 'tokens': 1, 'usage': [], 'modelVisibleDigests': [], 'provenance': [],
                    'messageIds': [], 'systemContractSha256': None, 'toolViewSha256': None}
    try:
        row, _ = ai_arc_services.run_services(
            scenarios=('P10',), variant='A', candidate_digest=corpus.DIGEST,
            base_url=f'http://127.0.0.1:{server.server_address[1]}', admin_key=corpus.ADMIN, project='arc-abcdef123456',
            arm=R.arm_by_id(ARC, 'A1'), limits=dict(ARC['limits']['services'], maxSeconds=600), out_dir=tmp_path / 'o',
            cli=Cli(), load_scenario=lambda sid: corpus.FakeP10())
        assert views and all(corpus.ADMIN not in v for v in views) and row['catalogRedactions'] >= 1
    finally:
        server.shutdown()


def test_overrun_with_an_unknown_mutation_outcome_never_completes(tmp_path, monkeypatch):
    """Astra 5th review: the product accepts a mutation, the agent's HTTP call times out
    (ActionOutcomeUnknown), the stage overruns; the pending mutation must block finalization."""
    import time as _time
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    import agent_e2e_local_runner as runner
    original = corpus.Candidate.mcp
    landed = {}

    def slow_mcp(self, message, principal):
        if message.get('method') == 'tools/call' and message['params']['name'] == 'honua_update_features':
            _time.sleep(8)
            landed['at'] = _time.monotonic()
        return original(self, message, principal)
    monkeypatch.setattr(corpus.Candidate, 'mcp', slow_mcp)
    original_client = runner.LocalRuntime.mcp_client

    def short_client(self, stack):
        client = original_client(self, stack)
        client.deadline = client.clock() + 2      # the agent's HTTP call times out after acceptance
        return client
    monkeypatch.setattr(runner.LocalRuntime, 'mcp_client', short_client)
    outcome = {}

    def script(execute, corpus):
        outcome['started'] = round(DEADLINE['at'] - _time.monotonic(), 1)
        before_deadline(12.0)
        try:
            outcome['update'] = execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_update_features',
                                                       'arguments': {'serviceId': 'p10'}})
        except Exception as exc:  # noqa: BLE001
            outcome['update'] = type(exc).__name__
        past_deadline()
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, script, deadline=60.0)
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        assert result['shutdown']['unresolvedOutcomes'], (outcome, result['shutdown'])
        assert result['shutdown']['complete'] is False
        assert result['outcome'] == 'undetermined' and 'never resolved' in result['cause']
    finally:
        server.shutdown()


def test_unknown_outcome_before_the_deadline_still_blocks_completion(tmp_path, monkeypatch):
    """Astra 6th review: the agent's call times out well before the deadline, the session ends
    normally, and the accepted mutation lands later. Ordinary completion goes through the same
    shutdown, so the stage is undetermined and the gate is closed when it finalizes."""
    import time as _time
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    import agent_e2e_local_runner as runner
    original = corpus.Candidate.mcp

    def slow_mcp(self, message, principal):
        if message.get('method') == 'tools/call' and message['params']['name'] == 'honua_update_features':
            _time.sleep(6)
        return original(self, message, principal)
    monkeypatch.setattr(corpus.Candidate, 'mcp', slow_mcp)
    original_client = runner.LocalRuntime.mcp_client

    def short_client(self, stack):
        client = original_client(self, stack)
        client.deadline = client.clock() + 2
        return client
    monkeypatch.setattr(runner.LocalRuntime, 'mcp_client', short_client)
    seen = {}

    def script(execute, corpus):
        try:
            seen['update'] = execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_update_features',
                                                    'arguments': {'serviceId': 'p10'}})
        except Exception as exc:  # noqa: BLE001
            seen['update'] = type(exc).__name__
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, script, deadline=300.0)
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        assert ctx.remaining() > 60, 'the stage ended well before its deadline'
        assert ctx.gate.closed, 'the gate is closed before the stage finalizes'
        assert result['shutdown']['unresolvedOutcomes'] and result['shutdown']['complete'] is False, seen
        assert result['outcome'] == 'undetermined' and 'never resolved' in result['cause']
    finally:
        server.shutdown()


def test_uncorrelated_response_after_dispatch_stays_unknown(tmp_path):
    """Astra 7th review B1: the real HttpMcpBackend raises ValueError for a wrong response id AFTER
    sending the operation; that outcome is unknown and the stage cannot complete."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    applied = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if body.get('method') == 'tools/call':
                applied.append(body['params'])                 # the product accepted the mutation
                reply = {'jsonrpc': '2.0', 'id': 999999, 'result': {'content': [], 'isError': False}}
            elif 'id' in body:
                reply = {'jsonrpc': '2.0', 'id': body['id'], 'result': {
                    'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'x'}, 'tools': []}}
            else:
                self.send_response(202)
                self.end_headers()
                return
            raw = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_address[1]}/mcp'

    class Stage(R.StageModule):
        stage, lane = 'ops', 'test'

        def backends(self, ctx):
            return {'honua': backends_mod.HttpMcpBackend(endpoint).start()}

        def observe(self, ctx, row):
            return [{'assertion': a, 'status': 'pass'} for a in R.required_assertions(ctx.arc, 'ops', ctx.target)]

    class Session:
        def run_stage(self, *, backends, **kw):
            try:
                backends['honua'].execute('tool_call', {'tool': 'honua_update_features', 'arguments': {'serviceId': 's'}})
            except ValueError:
                pass
            return {'finalAnswer': 'It is updated and ready.', 'nudges': 0,
                    'actions': [{'kind': 'tool_call', 'tool': 'honua_update_features', 'arguments': {}, 'status': 'fail'}]}
    try:
        arc = R.load_arc()
        ctx = R.StageContext(arc=arc, target='local-docker', arm=R.arm_by_id(arc, 'A1'), variant='A',
                             run_dir=tmp_path / 'run', private_dir=tmp_path / 'p',
                             slot=R.Slot('local-docker', 'none', 'arc-abcdef123456'), candidate=CAND_IN, install=INSTALL,
                             limits=arc['limits']['ops'], deadline=__import__('time').monotonic() + 60)
        result = R.run_stage(ctx, Stage(), Session(), 'Update it.', None)
        assert applied, 'the operation reached the product'
        assert result['shutdown']['unresolvedOutcomes'] and result['shutdown']['complete'] is False
        assert result['outcome'] == 'undetermined'
    finally:
        server.shutdown()


@pytest.mark.parametrize('exit_kind', ['normal', 'error'])
def test_denied_revocation_on_normal_and_error_exits_is_undetermined(tmp_path, monkeypatch, exit_kind):
    """Astra 7th review B2 on the real #1152 runtime: revocation denied (401) after a recorded query;
    the session either finishes or the case raises afterwards. The gate closes first, the failure is
    retained, the still-working credential is detected, and the stage is undetermined."""
    def script(execute, corpus):
        query(execute, {'where': corpus.CORRECT_WHERE})
        if exit_kind == 'error':
            raise OSError('the CLI pipe broke after the case')
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, script, revoke_status=401, deadline=300.0)
    state.deny_revoke = True
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        shutdown = result['shutdown']
        assert shutdown['order'][:7] == ['gate', 'kill', 'revoke', 'drain', 'join', 'reconcile', 'probe']
        assert shutdown['revocationFailed'] and shutdown['credentialAlive'] and shutdown['complete'] is False
        assert 401 in [r['status'] for r in shutdown['revoked']], 'every revocation result is retained'
        assert result['outcome'] == 'undetermined'
    finally:
        server.shutdown()


@pytest.mark.parametrize('after_mint', ['stall-past-deadline', 'raise'])
def test_credentials_minted_during_setup_are_registered_revoked_and_probed(tmp_path, monkeypatch, after_mint):
    """Astra 8th review B1 on the real #1152 runtime: pause or fail right after the real credential mint,
    before the runtime's own setup returns."""
    import time as _time
    import urllib.error
    import urllib.request
    import agent_e2e_local_runner as runner
    original = runner.LocalRuntime._prepare

    def paused(self, stack, permissions):
        original(self, stack, permissions)
        if after_mint == 'raise':
            raise OSError('failed at the acquisition boundary')
        past_deadline(1.0)
    monkeypatch.setattr(runner.LocalRuntime, '_prepare', paused)
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, lambda execute, corpus: None, deadline=20.0)
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        shutdown = result['shutdown']
        assert shutdown['credentialProbes'] and all(p['alive'] is False for p in shutdown['credentialProbes'])
        for key in state.keys:
            request = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/v1/admin/api-keys/',
                                             headers={'X-API-Key': key})
            try:
                status = urllib.request.urlopen(request, timeout=10).status
            except urllib.error.HTTPError as exc:
                status = exc.code
            assert status in (401, 403), 'no minted credential authenticates after finalization'
        assert result['outcome'] in ('fail', 'undetermined')
    finally:
        server.shutdown()


@pytest.mark.parametrize('failure', ['store-raises', 'read-raises'])
def test_a_mint_that_fails_before_returning_is_never_lost(tmp_path, monkeypatch, failure):
    """Astra 9th review B1, on the real #1152 recorder: the controller accepts the bootstrap mint, then
    recording fails (store) or the response is lost (read) before the recorder returns."""
    import urllib.error
    import urllib.request
    import agent_e2e_local_recorder as recorder_mod
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    if failure == 'store-raises':
        original_store = recorder_mod.ControllerRecorder._store

        def store(self, record):
            if record.step == 'runner-custody-mint-bootstrap':
                raise OSError('disk full while persisting the response')
            return original_store(self, record)
        monkeypatch.setattr(recorder_mod.ControllerRecorder, '_store', store)
    else:
        original_request = recorder_mod.ControllerRecorder._request

        def request(self, step, principal, method, path, body, content_type):
            if step == 'runner-custody-mint-bootstrap':
                http = urllib.request.Request(self.base_url + path, data=body, method=method,
                                              headers={'Content-Type': content_type, 'X-API-Key': corpus.ADMIN})
                urllib.request.urlopen(http, timeout=10).read()        # accepted by the product
                raise OSError('connection reset while reading the response')
            return original_request(self, step, principal, method, path, body, content_type)
        monkeypatch.setattr(recorder_mod.ControllerRecorder, '_request', request)
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, lambda execute, corpus: None, deadline=120.0)
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        shutdown = result['shutdown']
        if failure == 'store-raises':
            # Registered inside the store hook before persistence: revoked, probed, refused.
            for key in state.keys:
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/v1/admin/api-keys/',
                                             headers={'X-API-Key': key})
                try:
                    status = urllib.request.urlopen(req, timeout=10).status
                except urllib.error.HTTPError as exc:
                    status = exc.code
                assert status in (401, 403)
            assert shutdown['credentialProbes']
        else:
            # Never registered: the outstanding acquisition keeps the shutdown incomplete.
            assert any('mint outcome unknown' in str(r.get('error')) for r in shutdown['revoked'])
            assert shutdown['complete'] is False and result['outcome'] == 'undetermined'
    finally:
        server.shutdown()


def test_recovered_unregistered_mint_is_revoked_and_no_longer_authenticates(tmp_path, monkeypatch):
    """Review nit (10th review): automated liveness coverage of credential recovery. The product accepts
    the bootstrap mint and the response is lost before registration; finalization recovers the key from
    the controller's own list, revokes it, and the key must no longer authenticate."""
    import urllib.error
    import urllib.request
    import agent_e2e_local_recorder as recorder_mod
    corpus = pytest.importorskip('test_agent_e2e_local_runner')
    original_request = recorder_mod.ControllerRecorder._request

    def request(self, step, principal, method, path, body, content_type):
        if step == 'runner-custody-mint-bootstrap':
            http = urllib.request.Request(self.base_url + path, data=body, method=method,
                                          headers={'Content-Type': content_type, 'X-API-Key': corpus.ADMIN})
            urllib.request.urlopen(http, timeout=10).read()
            raise OSError('connection reset while reading the response')
        return original_request(self, step, principal, method, path, body, content_type)
    monkeypatch.setattr(recorder_mod.ControllerRecorder, '_request', request)
    state, server, ctx, corpus = overrun(tmp_path, monkeypatch, lambda execute, corpus: None, deadline=120.0)
    handler = server.RequestHandlerClass
    original_get = handler.do_GET

    def do_GET(self):
        if self.path.rstrip('/') == '/api/v1/admin/api-keys' and self.headers.get('X-API-Key') == corpus.ADMIN:
            rows = [{'id': row['id'], 'name': f"{ctx.slot.project}-p10-{row['id']}", 'revoked': row['revoked']}
                    for row in state.keys.values()]
            return self.reply(200, {'data': rows})
        return original_get(self)
    handler.do_GET = do_GET
    try:
        result = R.run_stage(ctx, CorpusServices(('P10',)), R.ArcSession(R.arm_by_id(ctx.arc, 'A1')), '', None)
        recovered = [r for r in result['shutdown']['revoked'] if r.get('note') == 'recovered unregistered mint']
        assert recovered and all(r['status'] in (200, 204) for r in recovered)
        for key in state.keys:
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/api/v1/admin/api-keys/',
                                         headers={'X-API-Key': key})
            try:
                status = urllib.request.urlopen(req, timeout=10).status
            except urllib.error.HTTPError as exc:
                status = exc.code
            assert status in (401, 403), 'a recovered credential no longer authenticates'
        assert result['shutdown']['complete'] is False, 'the unaccounted acquisition still keeps it incomplete'
    finally:
        server.shutdown()
