"""Stage 3 (services) on the arc's own installed stack, through the #1152 corpus runner.

Each services scenario (the spec's data list) runs as one #1152 case: ``execute_case`` seeds through
the runner-owned recorder, stages the scenario's customer files, runs one fresh model session with
the plain handoff plus the scenario's pack prompt on the server /mcp (the #1160 CLI session for the
arm), then scores every pack assertion with the vault's sealed records, the model action log, the
frozen projections and logical roles, and scores the final answer against the scenario's handoff
projection. ``ArcCorpusRuntime`` adopts the arc's installed deployment (through the runner-owned
tunnel) instead of composing a fresh one, leaves it for the next stage, screens every product
response for known credentials before the model sees it, captures the private final answer, and
shares ONE stage budget (tool calls and wall-clock) across the cases.

Private evidence stays in a 0700 directory outside the upload. Before it is removed the runner
keeps, in the stage's private evidence: each full case receipt, the action log and the CLI wire log
(redacted again on export), plus #1152's own redacted controller-record export.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path


def runtime_class():
    import agent_e2e_local_runner as runner
    from agent_e2e_local_api import Stack

    class ArcCorpusRuntime(runner.LocalRuntime):
        """#1152 LocalRuntime on an adopted arc stack."""

        def __init__(self, *, base_url, admin_key, project, candidate_digest, agent, model, limits, private_root,
                     extra_secrets=()):
            super().__init__(None, model=model, release_id='honua-ai-arc', agent=agent,
                             max_model_calls=limits['maxTurns'], max_actions=limits['maxToolCalls'])
            self.arc = {'base_url': base_url, 'admin_key': admin_key, 'project': project,
                        'candidate': candidate_digest, 'private_root': private_root}
            self.timeout = limits['maxSeconds']
            self.extra_secrets = extra_secrets if isinstance(extra_secrets, list) else list(extra_secrets)
            self.answers, self.withheld, self.catalog_redactions = {}, 0, 0
            self.stacks, self.cancelled, self.agent_keys = [], [], []
            self.minted, self.outstanding = [], set()
            self.lock = threading.Lock()
            self.gate = None

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
            self.stacks.append(stack)
            self.agent_keys.append(os.environ.get(self.AGENT_ENV))
            # Live secret set: minted case keys join the arc's screen and redaction at once.
            for value in self._secrets:
                if value not in self.extra_secrets:
                    self.extra_secrets.append(value)
            return stack

        def _recorder(self, stack, principals):
            """Credential custody at the acquisition boundary. Before a mint is dispatched it is recorded as
            outstanding. The response is registered with the finalizer inside the recorder's own store
            hook, before the recorder persists or returns anything, so a later recording or hand-off failure
            cannot hide it. A mint that never reaches registration (transport, read or persistence failure,
            or an uninterpretable success) stays unaccounted, which keeps the shutdown incomplete. After the
            stage gate closes no further credential can be minted."""
            recorder = super()._recorder(stack, principals)
            original_json, original_store = recorder.json, recorder._store

            def minting(method, path):
                return method == 'POST' and path.rstrip('/') == '/api/v1/admin/api-keys'

            def store(record):
                if record.kind == 'http' and minting(record.method, record.path):
                    try:
                        if record.status == 201:
                            data = json.loads(record.raw)['data']
                            self.register(data['apiKey']['id'], data['key'])
                            self.outstanding.discard(record.step)
                        elif record.status < 500:
                            self.outstanding.discard(record.step)    # a definite refusal minted nothing
                    except Exception:  # noqa: BLE001  (uninterpretable success stays outstanding)
                        pass
                return original_store(record)

            def json_call(step, method, path, body=None, **kw):
                if minting(method, path):
                    if self.gate is not None and self.gate.closed:
                        raise RuntimeError('credential acquisition refused: the stage has ended')
                    with self.lock:
                        self.outstanding.add(step)
                return original_json(step, method, path, body, **kw)
            recorder._store, recorder.json = store, json_call
            return recorder

        def register(self, key_id, key):
            with self.lock:
                if key_id and key and key_id not in {m['id'] for m in self.minted}:
                    self.minted.append({'id': key_id, 'key': key})
                    if key not in self.extra_secrets:
                        self.extra_secrets.append(key)

        def _http(self, method, path, key):
            import urllib.error
            import urllib.request
            request = urllib.request.Request(self.arc['base_url'] + path, method=method, headers={'X-API-Key': key})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return response.status
            except urllib.error.HTTPError as exc:
                return exc.code
            except Exception:  # noqa: BLE001
                return None

        def _list_keys(self):
            import urllib.request
            request = urllib.request.Request(self.arc['base_url'] + '/api/v1/admin/api-keys/',
                                             headers={'X-API-Key': self.arc['admin_key']})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = json.loads(response.read(4_000_000)).get('data')
            except Exception:  # noqa: BLE001
                return []
            rows = data if isinstance(data, list) else (data or {}).get('items', []) if isinstance(data, dict) else []
            return [r for r in rows if isinstance(r, dict)]

        def cancel(self):
            """Revoke every registered case credential not yet revoked (idempotent; reconciles late mints)."""
            results = []
            with self.lock:
                pending = [m for m in self.minted if m['id'] not in self.cancelled]
            for minted in pending:
                if self._http('GET', '/api/v1/admin/api-keys/', minted['key']) in (401, 403):
                    # Already refused (e.g. the bootstrap key the custody step revoked): nothing to revoke.
                    results.append({'principal': minted['id'], 'status': 204, 'note': 'already refused'})
                    self.cancelled.append(minted['id'])
                    continue
                status = self._http('POST', f"/api/v1/admin/api-keys/{minted['id']}/revoke", self.arc['admin_key'])
                results.append({'principal': minted['id'], 'status': status})
                if status in (200, 204):
                    self.cancelled.append(minted['id'])
            if self.outstanding:
                # Recover accepted-but-unregistered mints from the controller's own list and revoke them.
                # The outstanding acquisition still keeps the shutdown incomplete (it was never accounted).
                listing = self._list_keys()
                for row in listing:
                    name, key_id = str(row.get('name', '')), row.get('id')
                    known = {m['id'] for m in self.minted}
                    if key_id and key_id not in known and name.startswith(self.arc['project'] + '-') \
                            and not row.get('revoked') and key_id not in self.cancelled:
                        status = self._http('POST', f"/api/v1/admin/api-keys/{key_id}/revoke", self.arc['admin_key'])
                        results.append({'principal': key_id, 'status': status, 'note': 'recovered unregistered mint'})
                        if status in (200, 204):
                            self.cancelled.append(key_id)
            for step in sorted(self.outstanding):
                results.append({'principal': None, 'status': None, 'error': 'mint outcome unknown: ' + step})
            return results

        def probe(self):
            """Every registered credential must now be refused by the product (401/403)."""
            with self.lock:
                minted = list(self.minted)
            return [{'principal': m['id'], 'status': (status := self._http('GET', '/api/v1/admin/api-keys/', m['key'])),
                     'alive': status not in (401, 403)} for m in minted]

        def raw_execute(self, stack, names):
            execute = super().raw_execute(stack, names)

            def screened(kind, action):
                from ai_arc_runner import GATE_CLOSED, WITHHELD, _as_text, secret_forms
                gate = self.gate
                if gate is not None and not gate.enter():
                    return dict(GATE_CLOSED)       # the stage has ended: nothing reaches the product
                try:
                    result = execute(kind, action)
                except BaseException as exc:
                    if gate is not None:
                        gate.leave({'tool': action.get('tool'), 'outcome': 'error', 'error': type(exc).__name__})
                    raise
                if gate is not None:
                    gate.leave({'tool': action.get('tool'), 'outcome': result.get('status')})
                raw = json.dumps(result, ensure_ascii=False, default=_as_text).encode()
                if any(form in raw for form in secret_forms([*self._secrets, *self.extra_secrets])):
                    self.withheld += 1
                    return dict(WITHHELD)
                return result
            return screened

        def tool_view(self, stack):
            from ai_arc_runner import _as_text, redact_bytes, secret_forms
            view = super().tool_view(stack)
            secrets = [*self._secrets, *self.extra_secrets]
            raw = json.dumps(view, ensure_ascii=False, default=_as_text).encode()
            if any(form in raw for form in secret_forms(secrets)):
                self.catalog_redactions += 1
                view = json.loads(redact_bytes(raw, secrets))
            return view

        def session(self, stack, prompt, number, **kwargs):
            try:
                output = super().session(stack, prompt, number, **kwargs)
            except runner.SessionInterrupted as exc:
                self.answers[stack.compose_project] = None
                raise
            self.answers[stack.compose_project] = output.get('finalAnswer')
            return output

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
        rows.append({'kind': action.get('kind'), 'tool': action.get('tool'),
                     'arguments': action.get('arguments') if isinstance(action.get('arguments'), dict) else {}})
    return rows


class Canceller:
    """Registered in the stage's live set. Only finalize_stage drives it: ``revoke`` after the gate is
    closed and the CLI killed (every result retained), then ``probe`` that each credential is refused."""

    def __init__(self, runtime):
        self.runtime, self.results = runtime, []

    def revoke(self):
        results = self.runtime.cancel()
        self.results.extend(results)
        return results

    def probe(self):
        return self.runtime.probe()


def run_fixture_services(*, goal, fixture_id, fault, **kwargs):
    """ARC held-out entry point; a missing fixture field is a named stage failure."""
    if not isinstance(goal, str) or not goal.strip() or not isinstance(fixture_id, str) or not isinstance(fault, str):
        return {'actions': [], 'answers': [], 'secrets': [], 'nudges': 0,
                'failure': {'category': 'model', 'reason': 'services goal, fixture ID or fault is missing'},
                'withheld': 0, 'catalogRedactions': 0}, []
    # These are ignored by fixture mode; the committed catalog supplies both.
    kwargs.pop('scenarios', None)
    kwargs.pop('variant', None)
    return run_services(scenarios=(), variant=None, goal=goal, fixture_id=fixture_id, fault=fault, **kwargs)


def run_services(*, scenarios, variant, candidate_digest, base_url, admin_key, project, arm, limits, out_dir,
                 handoff=None, extra_secrets=(), deadline=None, cli=None, load_scenario=None, private_root=None,
                 register=None, gate=None, goal=None, fixture_id=None, fault=None):
    """Run each services scenario once on the adopted stack under one shared stage budget.

    Returns (row, cases). ``row`` carries every action (with its recorded status), the private final
    answers, nudges and the first case failure; ``out_dir`` receives the retained case evidence."""
    import agent_e2e_local_runner as runner
    fixture_cases = None
    if any(value is not None for value in (goal, fixture_id, fault)):
        try:
            fixture_variant, fixture_cases = runner.select_fixture_cases(goal=goal, fixture_id=fixture_id)
            scenarios, variant = tuple(fixture_cases), fixture_variant
            plans = [runner.load_scenario(sid).fault_plan(variant) for sid in scenarios]
            if not isinstance(fault, str) or not any(p and p.get('kind') == fault for p in plans):
                raise ValueError('services fault is incompatible with fixture scenarios')
            # Existing P10/P15 seeds publish services as controller. Do not credit
            # those cases until the raw-input publication observer is implemented.
            raise ValueError('services agent publication from raw inputs is not yet supported')
        except (ImportError, OSError, ValueError) as exc:
            reason = str(exc)[:200] or type(exc).__name__
            return {'actions': [], 'answers': [], 'secrets': [], 'nudges': 0,
                    'failure': {'category': 'model', 'reason': reason}, 'withheld': 0,
                    'catalogRedactions': 0}, []
    try:
        import agent_e2e_cli_sessions  # noqa: F401  (registers claude-cli / codex-cli with #1152)
    except ImportError as exc:
        raise RuntimeError('CLI corpus sessions (#1160) are not installed') from exc
    deadline = deadline or (time.monotonic() + limits['maxSeconds'])
    owned = private_root is None
    # The write-ahead ledger lives in the stage's private evidence, so a timeout recovers it.
    if private_root is None:
        from agent_cli_sessions import arc_scratch_root
        private_root = tempfile.mkdtemp(prefix='honua-arc-services-', dir=arc_scratch_root())
    private_root = Path(private_root)
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cls = runtime_class()
    runtime = cls(base_url=base_url, admin_key=admin_key, project=project, candidate_digest=candidate_digest,
                  agent=arm['agent'], model=arm['model'], limits=limits, private_root=private_root,
                  extra_secrets=extra_secrets)
    runtime.gate = gate
    if register is not None:
        register(Canceller(runtime))
    if cli is None:
        # #1160's corpus CLI session, pinned to the arm's CLI version (refuses before dispatch otherwise).
        import agent_e2e_cli_sessions as cli_sessions
        cli = cli_sessions.CliModelSession(arm['agent'], arm['model'])
        cli.expected_version = arm.get('cliVersion')
    runtime.cli = cli        # test seam: #1160's CorpusCliSession uses runtime.cli when present
    load_scenario = load_scenario or runner.load_scenario
    out_dir = Path(out_dir)
    cases, actions, answers, nudges, failure = [], [], [], 0, None
    try:
        for index, sid in enumerate(scenarios):
            remaining_actions = limits['maxToolCalls'] - len(actions)
            remaining_time = int(deadline - time.monotonic())
            if remaining_actions <= 0 or remaining_time <= 0:
                cases.append({'scenario': sid, 'receipt': {'status': 'fail', 'assertions': [],
                                                           'failure': {'reason': 'stage budget exhausted'}}})
                failure = failure or {'category': 'model', 'reason': 'stage budget exhausted before ' + sid}
                continue
            runtime.max_actions, runtime.timeout = remaining_actions, remaining_time
            scenario = load_scenario(sid)
            case = fixture_cases[sid] if fixture_cases is not None else runner.select_case(runner.PACK, sid, variant)
            if handoff and index == 0:
                # The plain customer handoff from the earlier stages, then the scenario's own request.
                case = dict(case, prompts={**case['prompts'], variant: handoff + '\n\n' + case['prompts'][variant]})
            before = set(runtime.workdirs)
            receipt = runner.execute_case(case_id=f'{sid}:{variant}:local-docker', scenario=scenario,
                                          variant=variant, case=case, candidate_digest=candidate_digest,
                                          runtime=runtime)
            new_dirs = [d for d in runtime.workdirs if d not in before]
            statuses = [a.get('status') for s in receipt.get('sessions', []) for a in s.get('actions', [])]
            for case_dir in new_dirs:
                rows = private_actions(Path(case_dir))
                for offset, row in enumerate(rows):
                    actions.append({**row, 'scenario': sid,
                                    'status': statuses[offset] if offset < len(statuses) else 'unknown'})
                keep = out_dir / sid
                if owned:
                    for pattern in ('session-*/action-*.json', 'session-*/cli-wire/*'):
                        for path in Path(case_dir).glob(pattern):
                            target = keep / 'private' / path.relative_to(case_dir)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(path, target)
                try:
                    runner.export_case_evidence(case_dir, keep / 'export', runtime._secrets)
                except ValueError as exc:
                    receipt['status'] = 'fail'
                    receipt['exportFailure'] = str(exc)[:200]
            (out_dir / sid).mkdir(parents=True, exist_ok=True)
            from ai_arc_runner import redact_bytes
            # Redacted at write time (minted case keys and the controller credential never stored).
            (out_dir / sid / 'case-receipt.json').write_bytes(
                redact_bytes(json.dumps(receipt, sort_keys=True).encode(), [*runtime._secrets, *runtime.extra_secrets]))
            answer = runtime.answers.get(f'{project}-{sid.lower()}')
            if isinstance(answer, str):
                answers.append(answer)
            nudges += int(receipt.get('nudges') or 0)
            if receipt.get('failure') and failure is None:
                failure = {'category': receipt['failure'].get('category', 'model'),
                           'reason': f"{sid}: {receipt['failure'].get('reason')}"}
            if receipt.get('status') == 'infrastructure' and not receipt.get('sessions') and failure is None:
                failure = {'category': 'infrastructure', 'beforeCaseActions': True,
                           'reason': f'{sid}: setup or seed could not complete'}
            custody = next((a for a in receipt.get('assertions', [])
                            if sid == 'P15' and a.get('assertion') == 'custody.bootstrap-revoked'), None)
            if custody and custody.get('status') != 'pass' and failure is None:
                failure = {'category': 'harness-error', 'reason': 'P15 bootstrap custody evidence failed'}
            cases.append({'scenario': sid, 'receipt': receipt})
    finally:
        secrets = list(runtime._secrets)
        if owned:
            shutil.rmtree(private_root, ignore_errors=True)
    return {'actions': actions, 'answers': answers, 'secrets': secrets, 'nudges': nudges, 'failure': failure,
            'withheld': runtime.withheld, 'catalogRedactions': runtime.catalog_redactions}, cases


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
                     **({'reason': a['reason']} if a.get('reason') else {})} for a in receipt.get('assertions', [])
                    if not (sid == 'P15' and a.get('assertion') == 'custody.bootstrap-revoked'))
        check = receipt.get('finalAnswerCheck') or {}
        rows.append({'assertion': f'{sid}:finalAnswerCheck', 'status': 'pass' if check.get('status') == 'pass' else 'fail',
                     'falseSuccess': bool(check.get('false_success')), 'undetermined': bool(check.get('undetermined'))})
    return rows
