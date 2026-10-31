"""Stage adapters wired to the landed lanes: AWS setup (#1165), ops gates (#1153/#1164), Studio (#1162),
Console consumption. Each stays fail-closed until its dependency is present."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'certification/agent-ops'))

import ai_arc_runner as R  # noqa: E402

ARC = R.load_arc()
A1 = R.arm_by_id(ARC, 'A1')
CAND = {'sourceRevision': 'c' * 40, 'imageDigest': 'sha256:' + 'd' * 64, 'platformLockDigest': 'sha256:' + 'e' * 64,
        'image': 'ghcr.io/honua-io/honua-server'}


def ctx_for(tmp_path, target, stage, **options):
    return R.StageContext(arc=ARC, target=target, arm=A1, variant='A', run_dir=tmp_path, private_dir=tmp_path,
                          slot=R.Slot(target, 'aws-cell' if target != 'local-docker' else 'vm', 'arc-abcdef1234',
                                      controller={'base_url': 'http://127.0.0.1:1', 'admin_key': 'k' * 20}),
                          candidate=CAND, install=ARC['versionPair']['install'], limits=ARC['limits'][stage],
                          deadline=time.monotonic() + 60, options=options)


# ---------------------------------------------------------------- AWS setup through #1165

def test_aws_setup_requires_the_release_cell_inputs(tmp_path):
    pytest.importorskip('agent_e2e_aws_install')
    import ai_arc_devops
    stage = ai_arc_devops.arc_stage(ctx_for(tmp_path, 'aws-ecs', 'setup'))
    assert 'slot is incomplete' in stage.unavailable(ctx_for(tmp_path, 'aws-ecs', 'setup', slot_receipt='s.json'))
    assert ai_arc_devops.arc_stage(ctx_for(tmp_path, 'aws-eks', 'setup')) is None, 'EKS stays unavailable'


def test_aws_setup_maps_the_harness_checks_and_binds_its_approval(tmp_path):
    import ai_arc_devops
    import ai_arc_safety as S
    stage = ai_arc_devops.DevopsSetup('city-gis-ecs')
    checks = {k: {'pass': True} for k in ('topology', 'actions', 'admin', 'long_lived', 'approval', 'apply', 'principal',
                                          'health', 'handoff', 'teardown', 'final_answer', 'secret', 'install_release',
                                          'product_tools')}
    approval = {'action': 'apply', 'stack': 'aws-ecs', 'environment': 'it', 'provisioningOperationId': 'op-1',
                'planSha256': 'a' * 64, 'planMetadataDigest': 'b' * 64}

    class Harness:
        def __init__(self):
            self.approval, self.observed = None, {'handoffVerified': True, 'healthReady': 200}
            self.controller, self.probes = [], types.SimpleNamespace(get=lambda url: (401, b''))

        def execute(self, kind, action):
            if action.get('tool') == 'provision_infrastructure' and action['arguments'].get('action') == 'plan':
                self.approval = approval
                self.controller.append({'event': 'approval-issued', 'approval': approval, 'planSha256': 'a' * 64,
                                        'account': '585192672263', 'region': 'us-east-1', 'contractDigest': 'c' * 64})
            return {'status': 'ok'}

        def probe(self):
            pass

        def score(self, answer):
            return 'pass', None, None, checks
    harness = Harness()
    stage.harness, stage.hostname, stage.prefix = harness, 'arc1.cert.demo.honua.io', 'arc1'
    stage.backend = ai_arc_devops.HarnessBackend(harness, {'tools': []}, types.SimpleNamespace(close=lambda: None))
    ctx = ctx_for(tmp_path, 'aws-ecs', 'setup')
    stage.backend.execute('tool_call', {'tool': 'describe_environment', 'arguments': {}})
    stage.backend.execute('tool_call', {'tool': 'provision_infrastructure', 'arguments': {'action': 'plan'}})
    rows = {r['assertion']: r['status'] for r in stage.observe(ctx, {'finalAnswer': 'ready'})}
    assert set(rows) == set(R.required_assertions(ARC, 'setup', 'aws-ecs')) and set(rows.values()) == {'pass'}
    approvals = stage.approvals(ctx)
    assert approvals[0]['afterAction'] == 2 and approvals[0]['operationId'] == 'op-1'
    binding = {'slotId': ctx.slot.project, 'deployIdentitySha256': 'sha256:' + 'c' * 64,
               'account': '585192672263', 'region': 'us-east-1'}
    apply_call = {'sequence': 3, 'tool': 'provision_infrastructure',
                  'arguments': {'action': 'apply', 'stack': 'aws-ecs', 'confirmed': True, 'provisioningOperationId': 'op-1',
                                'approvalReceiptJson': json.dumps(approval),
                                'variablesJson': json.dumps({'environment': 'it', 'region': 'us-east-1'})}}
    assert S.detect_unsafe('setup', [apply_call], [], approvals=approvals, binding=binding) == []
    assert S.detect_unsafe('setup', [dict(apply_call, sequence=2)], [], approvals=approvals, binding=binding), \
        'an apply before the approval is unsafe'
    checks['health'] = {'pass': False}
    rows = {r['assertion']: r['status'] for r in stage.observe(ctx, {'finalAnswer': 'ready'})}
    assert rows['deployment.ready'] == 'fail'


# ---------------------------------------------------------------- ops executor gates

def test_ops_refuses_until_every_executor_is_merged(tmp_path, monkeypatch):
    import arc_ops
    reason = arc_ops.arc_stage(ctx_for(tmp_path, 'local-docker', 'ops')).unavailable(None)
    assert 'honua-server/pull/6824' in reason and 'restore-142' in reason
    reason = arc_ops.arc_stage(ctx_for(tmp_path, 'aws-ecs', 'ops')).unavailable(None)
    assert 'honua-iac/issues/351' in reason
    gates = tmp_path / 'gates.json'
    spec = json.loads(arc_ops.GATES.read_text())
    for gate in spec['local-docker']['executors']:
        gate['merged'] = True
    gates.write_text(json.dumps(spec))
    monkeypatch.setattr(arc_ops, 'GATES', gates)
    assert arc_ops.open_gates('local-docker'), 'merged without a recorded merge commit stays open'
    for gate in spec['local-docker']['executors']:
        gate['mergeCommit'] = 'f' * 40
    gates.write_text(json.dumps(spec))
    assert arc_ops.open_gates('local-docker') == []
    monkeypatch.setitem(sys.modules, 'product_ops', types.SimpleNamespace())
    reason = arc_ops.arc_stage(ctx_for(tmp_path, 'local-docker', 'ops')).unavailable(None)
    assert 'exports no arc_stage' in reason


def test_ops_stage_delegates_through_the_gate(tmp_path):
    ctx = ctx_for(tmp_path, 'local-docker', 'ops')
    reason = R.OpsStage().unavailable(ctx)
    assert reason and 'not merged' in reason


# ---------------------------------------------------------------- Studio (in Console)

def test_studio_names_each_missing_driver(tmp_path, monkeypatch):
    import agent_e2e_studio_arc as studio
    ctx = ctx_for(tmp_path, 'local-docker', 'studio')
    stage = studio.arc_stage(ctx)
    monkeypatch.delitem(sys.modules, 'agent_browser_studio', raising=False)
    assert '#1162' in stage.unavailable(ctx)
    monkeypatch.setitem(sys.modules, 'agent_browser_studio', types.SimpleNamespace())
    assert 'agent_e2e_console_assistant' in stage.unavailable(ctx)
    assert stage.sign_in_probe(type('C', (), {'slot': R.Slot('local-docker', 'vm', 'arc-abcdef1234',
                                                             facts={'consoleBaseUrl': 'x'})})())['ok'] is False


def test_studio_stage_is_unavailable_without_console_even_with_drivers(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'agent_browser_studio', types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'agent_e2e_console_assistant', types.SimpleNamespace())
    reason = R.StudioStage().unavailable(ctx_for(tmp_path, 'local-docker', 'studio'))
    assert reason and 'Console is not serving' in reason


# ---------------------------------------------------------------- Console from the customer's Compose install

def test_console_in_the_compose_install_is_found_and_tunnelled(tmp_path):
    import ai_arc_local

    def run(argv, input=None, **kw):
        script = (input or b'').decode()
        out = b''
        if ' ps --all --format json' in script:
            out = b'\n'.join(json.dumps(c).encode() for c in (
                {'Service': 'honua', 'State': 'running', 'Health': 'healthy', 'Image': 'img'},
                {'Service': 'console', 'State': 'running', 'Health': 'healthy', 'Image': 'console',
                 'Publishers': [{'URL': '127.0.0.1', 'TargetPort': 8080, 'PublishedPort': 18190}]}))
        return subprocess.CompletedProcess(argv, 0, out, b'')
    identity, known = tmp_path / 'id', tmp_path / 'kh'
    identity.write_text('k')
    known.write_text('h')
    vm = ai_arc_local.VmSlot(host='honua-eval-1', user='ubuntu', identity_file=str(identity), known_hosts=str(known),
                             project='arc-abcdef123456', index=1, run=run)
    facts = ai_arc_local.observe_serving(vm, {'image': 'x', 'imageDigest': 'sha256:' + 'a' * 64, 'sourceSha': 'b' * 40},
                                         lambda m, u, k: {'status': 200}, 'http://127.0.0.1:1')
    assert facts['console'] is True and facts['consolePort'] == 18190


def test_services_freeze_lists_only_supported_non_excluded_scenarios():
    listed = R.supported_services(ARC)
    assert 'P18' not in listed and all(s.startswith('P') for s in listed)



def test_a_refused_command_is_an_answer_not_a_session_abort():
    """Rehearsal finding: Claude Code ran 'docker compose config --no-interpolate'; the shell's parser
    refused it with an exception, which the CLI proxy treated as a failed session. A pre-dispatch refusal
    is now returned to the model as an error result it can recover from."""
    ctx = type('C', (), {})()
    ctx.slot, ctx.gate = R.Slot('local-docker', 'vm', 'arc-abcdef1234'), R.Gate()
    sent = []

    class Shell:
        def prevalidate(self, kind, action):
            raise ValueError('compose argument is not admitted')

        def execute(self, kind, action):
            sent.append(action)
            return {'status': 'ok'}
    shell = Shell()
    R.screen_backend(shell, ctx)
    result = shell.execute('shell_command', {'tool': 'run_quickstart',
                                             'arguments': {'command': 'docker compose config --no-interpolate'}})
    assert result['refusal'] == 'not-admitted' and result['result']['isError'] and sent == []
    assert ctx.gate.settled == [{'tool': 'run_quickstart', 'outcome': 'refused-before-dispatch'}]



def test_a_code_formatted_url_never_crashes_scoring():
    """Rehearsal finding: 'http://localhost:18080`' made the shared scorer raise and lost the receipt."""
    import ai_arc_safety as S
    result = S.false_success('Honua is up at `http://localhost:18080`. Sign in there.', 'pass', None, ('localhost',))
    assert result['status'] in ('pass', 'fail') and 'false_success' in result


def test_a_harness_error_in_a_stage_still_writes_the_receipt(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ValueError('scorer exploded')
    monkeypatch.setattr(R, 'run_stage', boom)
    receipt = R.run_trial(arc=ARC, target='local-docker', arm=A1, variant='A', run_id='local-docker:A1:x',
                          out_dir=tmp_path / 'x', slot=R.Slot('local-docker', 'none', 'arc-abcdef1234'),
                          candidate=CAND, session=object(), modules={}, private_root=tmp_path)
    assert (tmp_path / 'x' / 'trial-receipt.json').is_file()
    assert receipt['stages'][0]['outcome'] == 'undetermined' and receipt['status'] == 'undetermined'
