"""Stage 3 (services) on the arc's own installed stack, through the #1152 corpus runner.

Each services scenario runs as one #1152 case, unchanged: ``execute_case`` seeds through the
runner-owned recorder, stages the scenario's customer files, runs one fresh model session with the
scenario's pack prompt on the server /mcp (the #1160 CLI session for the arm), then scores every
pack assertion with the vault's sealed records, the model action log, the frozen projections and
logical roles, and scores the final answer against the scenario's handoff projection. The only
difference from a corpus case is the stack: ``ArcCorpusRuntime.setup`` adopts the arc's installed
deployment (through the runner-owned tunnel) instead of composing a fresh one, and ``teardown``
leaves it for the next stage.

Private evidence (minted keys in recorded bodies, action bytes) stays in a 0700 directory outside
the uploaded trial tree; only ``export_case_evidence``'s redacted copy is exported.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path


def runtime_class():
    import agent_e2e_local_runner as runner
    from agent_e2e_local_api import Stack

    class ArcCorpusRuntime(runner.LocalRuntime):
        """#1152 LocalRuntime on an adopted arc stack."""

        def __init__(self, *, base_url, admin_key, project, candidate_digest, agent, model, limits, private_root):
            super().__init__(None, model=model, release_id='honua-ai-arc', agent=agent,
                             max_model_calls=limits['maxTurns'], max_actions=limits['maxToolCalls'])
            self.arc = {'base_url': base_url, 'admin_key': admin_key, 'project': project,
                        'candidate': candidate_digest, 'private_root': private_root}
            self.timeout = limits['maxSeconds']

        def setup(self, case_id, candidate_digest, needs_approver=False, agent_permissions=('admin:write',)):
            if candidate_digest != self.arc['candidate']:
                raise ValueError('services case does not bind the arc candidate')
            if (not isinstance(agent_permissions, (list, tuple)) or not agent_permissions
                    or any(not isinstance(p, str) or not re.fullmatch(r'[a-z]+:[a-z*]+', p) for p in agent_permissions)):
                raise ValueError('invalid agent permission set')
            case_dir = Path(tempfile.mkdtemp(prefix='case-', dir=self.arc['private_root']))
            os.chmod(case_dir, 0o700)
            self.workdirs.append(str(case_dir))
            os.environ[self.ADMIN_ENV] = self.arc['admin_key']
            if self.arc['admin_key'] not in self._secrets:
                self._secrets.append(self.arc['admin_key'])
            project = self.arc['project'] + '-' + case_id.split(':', 1)[0].lower()
            stack = Stack(base_url=self.arc['base_url'], admin_key=self.arc['admin_key'], approver_key=None,
                          compose_project=project, compose_file='', workdir=str(case_dir),
                          candidate_digest=candidate_digest, extra={'needsApprover': needs_approver})
            self._prepare(stack, list(agent_permissions))
            if needs_approver:
                stack = self._mint_approver(stack)
            return stack

        def restart_server(self, stack, prefix):
            raise ValueError('a server restart is not available on an adopted arc stack')

        def teardown(self, stack):
            for name in (self.AGENT_ENV, self.BOOTSTRAP_ENV):
                os.environ.pop(name, None)
    return ArcCorpusRuntime


def private_actions(case_dir: Path) -> list[dict]:
    """The canonical request bytes of every model-selected action, from the private case log."""
    rows = []
    for path in sorted(Path(case_dir).glob('session-*/action-*.json')):
        try:
            action = json.loads(path.read_bytes())
        except ValueError:
            continue
        rows.append({'sequence': len(rows) + 1, 'kind': action.get('kind'), 'tool': action.get('tool'),
                     'arguments': action.get('arguments') if isinstance(action.get('arguments'), dict) else {}})
    return rows


def run_services(*, scenarios, variant, candidate_digest, base_url, admin_key, project, arm, limits, out_dir,
                 cli=None, load_scenario=None):
    """Run each services scenario once on the adopted stack. Returns (row, cases)."""
    import agent_e2e_local_runner as runner
    try:
        import agent_e2e_cli_sessions  # noqa: F401  (registers claude-cli / codex-cli with #1152)
    except ImportError as exc:
        raise RuntimeError('CLI corpus sessions (#1160) are not installed') from exc
    private_root = tempfile.mkdtemp(prefix='honua-arc-services-')
    os.chmod(private_root, 0o700)
    cls = runtime_class()
    per_case = dict(limits, maxSeconds=max(60, int(limits['maxSeconds'] / max(1, len(scenarios)))))
    runtime = cls(base_url=base_url, admin_key=admin_key, project=project, candidate_digest=candidate_digest,
                  agent=arm['agent'], model=arm['model'], limits=per_case, private_root=private_root)
    if cli is not None:
        runtime.cli = cli    # test seam: #1160's CorpusCliSession uses runtime.cli when present
    load_scenario = load_scenario or runner.load_scenario
    cases, actions, answers = [], [], []
    try:
        for sid in scenarios:
            scenario = load_scenario(sid)
            case = runner.select_case(runner.PACK, sid, variant)
            before = set(runtime.workdirs)
            receipt = runner.execute_case(case_id=f'{sid}:{variant}:local-docker', scenario=scenario,
                                          variant=variant, case=case, candidate_digest=candidate_digest,
                                          runtime=runtime)
            for case_dir in [d for d in runtime.workdirs if d not in before]:
                for row in private_actions(Path(case_dir)):
                    actions.append({**row, 'sequence': len(actions) + 1, 'scenario': sid})
                try:
                    runner.export_case_evidence(case_dir, Path(out_dir) / sid, runtime._secrets)
                except ValueError as exc:
                    receipt['status'] = 'fail'
                    receipt['exportFailure'] = str(exc)[:200]
            answers.extend(s.get('finalAnswer') for s in receipt.get('sessions', []) if s.get('finalAnswer'))
            cases.append({'scenario': sid, 'receipt': receipt})
    finally:
        secrets = list(runtime._secrets)
        shutil.rmtree(private_root, ignore_errors=True)
    return {'actions': actions, 'answers': answers, 'secrets': secrets}, cases


def case_assertions(cases: list[dict]) -> list[dict]:
    rows = []
    for item in cases:
        receipt, sid = item['receipt'], item['scenario']
        status = receipt.get('status')
        if status == 'excluded':
            rows.extend({'assertion': f"{sid}:{a['assertion']}", 'status': 'excluded'}
                        for a in receipt.get('assertions', []))
            continue
        if status == 'infrastructure' and not receipt.get('sessions'):
            rows.append({'assertion': f'{sid}:case', 'status': 'undetermined',
                         'reason': str(receipt.get('infrastructure'))[:200]})
            continue
        rows.extend({'assertion': f"{sid}:{a['assertion']}", 'status': a['status'],
                     **({'reason': a['reason']} if a.get('reason') else {})} for a in receipt.get('assertions', []))
        check = receipt.get('finalAnswerCheck') or {}
        rows.append({'assertion': f'{sid}:finalAnswerCheck', 'status': 'pass' if check.get('status') == 'pass' else 'fail',
                     'falseSuccess': bool(check.get('false_success')), 'undetermined': bool(check.get('undetermined'))})
    return rows
