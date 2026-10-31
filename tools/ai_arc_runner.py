#!/usr/bin/env python3
"""End-to-end AI arc runner (prereg ARC-v1): one agent arm, one deployment, four stages.

  setup     local-docker: the published quickstart through #1160's ``run_quickstart`` shell on a
            runner-owned VM slot (tools/ai_arc_local.py). AWS: the pinned honua-devops release
            (tools/ai_arc_operator.py) with the g43 approver/observer adapter.
  ops       the installed server's /mcp ops and proposal tools (adapter: certification/agent-ops/arc_ops.py).
  services  the #1152 corpus runner's cases on the arc's own stack (tools/ai_arc_services.py).
  studio    Studio's built-in assistant plus the arm in the Studio UI (adapter: tools/agent_e2e_studio_arc.py).

The model session for each stage is #1160's ``agent_cli_arc.run_stage`` for the arm, with only
that stage's product MCP surface. The runner owns slots, approvals, observers, the frozen stage
contract (required assertions, product-action provenance, candidate readback, both Studio
drivers, limits) and every receipt. A missing piece is ``stage-unavailable`` (the run is then
``undetermined``), never a pass and never the model's failure.

Receipts: per run ``honua.ai-arc-trial/v1``; per target x arm ``honua.ai-arc.aggregate/v1`` exactly
as the prereg specifies (plus ``stages.studio.assistant`` and ``certifying`` for the admission
validator), derived from the run receipts' retained stage evidence, never from their totals.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_safety as safety  # noqa: E402

ARC_PATH = ROOT / 'certification/ai-arc/arc.v1.json'
PREREG_PATH = ROOT / 'certification/ai-arc/PREREG-arc-v1.md'
SCOPE_PATH = ROOT / 'certification/ai-arc/gating-scope.json'
OPS_MODES = ('full', 'executed-subset', 'not-gating')
SEALED = {'primary': ROOT / 'certification/ai-arc/heldout.v1.sealed',
          'reserve': ROOT / 'certification/ai-arc/heldout-reserve.v1.sealed'}
TRIAL_SCHEMA = 'honua.ai-arc-trial/v1'
AGGREGATE_SCHEMA = 'honua.ai-arc.aggregate/v1'
STAGES = ('setup', 'ops', 'services', 'studio')
STAGE_NUMBER = {'setup': 1, 'ops': 2, 'services': 3, 'studio': 4}
OUTCOMES = ('pass', 'fail', 'blocked', 'stage-unavailable', 'undetermined')
TARGETS = ('local-docker', 'aws-ecs', 'aws-eks', 'aws-lambda')
TARGET_KIND = {'local-docker': 'remote-vm', 'aws-ecs': 'aws-ecs'}   # kinds #1160's stage adapter admits
DIGEST = re.compile(r'\Asha256:[0-9a-f]{64}\Z')
SHA = re.compile(r'\A[0-9a-f]{40}\Z')
FAMILIES = ('anthropic', 'openai')

# Everything whose bytes change behavior. Absent files contribute 'absent' (so landing a lane changes it).
RUNNER_FILES = ('tools/ai_arc_runner.py', 'tools/ai_arc_local.py', 'tools/ai_arc_operator.py',
                'tools/ai_arc_safety.py', 'tools/ai_arc_services.py', 'tools/ai_arc_campaign.py',
                'tools/agent_cli_arc.py', 'tools/agent_cli_backends.py', 'tools/agent_cli_sessions.py',
                'tools/agent_e2e_cli_sessions.py', 'tools/agent_e2e_local_runner.py',
                'tools/agent_e2e_local_recorder.py', 'tools/agent_e2e_local_api.py',
                'tools/agent_e2e_local_mcp_proxy.py', 'tools/agent_e2e_local_faults.py',
                'tools/g3_compose_quickstart.py', 'tools/ai_arc_devops.py',
                'certification/agent-ops/arc_ops.py', 'tools/agent_e2e_studio_arc.py')
SCORER_GLOBS = ('tools/agent_e2e_handoff.py', 'certification/agent-e2e/corpus/oracle.py',
                'tools/agent_e2e_local_projections.py', 'tools/agent_e2e_local_expectations.py',
                'certification/agent-e2e/pack.v2.AB-only.json',
                'certification/agent-e2e/expectations/local/*.json', 'tools/agent_e2e_local_scenarios/*.py')


def sha256(raw: bytes | str) -> str:
    return safety.sha256(raw)


def canonical(value: Any) -> bytes:
    return safety.canonical(value)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)


def assert_row(assertion: str, ok: bool, evidence: dict) -> dict:
    return {'assertion': assertion, 'status': 'pass' if ok else 'fail',
            'evidenceSha256': sha256(canonical(evidence)), 'evidence': evidence}


# --------------------------------------------------------------------------- arc definition

def load_arc(path: Path = ARC_PATH) -> dict:
    raw = Path(path).read_bytes()
    arc = json.loads(raw)
    validate_arc(arc)
    arc['_sha256'] = sha256(raw)
    return arc


def validate_arc(arc: dict) -> None:
    if arc.get('schemaVersion') != 1 or arc.get('developmentVariants') != ['A', 'B']:
        raise ValueError('arc definition must be schema 1 with development variants A and B only')
    if [s.get('id') for s in arc.get('stages') or []] != list(STAGES):
        raise ValueError('arc stages must be setup, ops, services, studio in order')
    seen = set()
    for stage in arc['stages']:
        if any(dep not in seen for dep in stage.get('dependsOn', [])):
            raise ValueError('stage dependencies must point at earlier stages')
        seen.add(stage['id'])
        required = stage.get('required')
        if not isinstance(required, dict) or set(required) != set(TARGETS) \
                or any(not isinstance(v, list) or not v for v in required.values()):
            raise ValueError(f"{stage['id']}: required assertions must be listed for every target")
    derived = derived_required(arc)
    for stage in arc['stages']:
        if stage['required'] != derived[stage['id']]:
            raise ValueError(f"{stage['id']}: required assertions differ from the spec's scenario data")
    if 'candidate.serving' in arc['opsScope']['catalog']:
        raise ValueError('candidate.serving is an invariant, never part of the ops scope catalog')
    if set(arc.get('targets', {})) != set(TARGETS):
        raise ValueError('arc must define every supported target')
    for name, target in arc['targets'].items():
        for stage in ('setup', 'ops', 'studio'):
            variants = target.get('goals', {}).get(stage)
            if not isinstance(variants, dict) or set(variants) != {'A', 'B'} \
                    or any(not isinstance(v, str) or len(v) < 40 for v in variants.values()):
                raise ValueError(f'{name}/{stage}: goals must be customer sentences for exactly A and B')
        bars = target.get('bars', {})
        if type(bars.get('runs')) is not int or type(bars.get('minArcPasses')) is not int \
                or not 0 < bars['minArcPasses'] <= bars['runs']:
            raise ValueError(f'{name}: invalid bars')
    if {(a['id'], a['agent'], a['model']) for a in arc.get('arms', []) if a.get('required')} != {
            ('A1', 'claude-cli', 'claude-opus-5-5'), ('A2', 'codex-cli', 'gpt-6.1-sol')}:
        raise ValueError('required arms must be A1 Claude Code + Opus 5.5 and A2 Codex + GPT-6.1 Sol')
    if any(a.get('family') not in FAMILIES for a in arc['arms']):
        raise ValueError('every arm names its model family')
    install = arc.get('versionPair', {}).get('install', {})
    if not SHA.fullmatch(install.get('sourceRevision', '')) or not DIGEST.fullmatch(install.get('imageDigest', '')):
        raise ValueError('install release must be bound by source revision and image digest')
    if arc.get('exclusions') != ['tenant.denied', 'P18', 'custody.worker-replaced@local-docker']:
        raise ValueError('exclusions must be exactly the prereg three')
    if arc.get('studioAssistant', {}).get('model') != 'us.anthropic.claude-sonnet-4-6':
        raise ValueError('the required Studio assistant is Bedrock Sonnet 4.6')


def arm_by_id(arc: dict, arm_id: str) -> dict:
    for arm in arc['arms']:
        if arm['id'] == arm_id:
            return arm
    raise ValueError('unknown arm: ' + str(arm_id))


CANDIDATE_INVARIANT = 'candidate.serving'
PACK_PATH = ROOT / 'certification/agent-e2e/pack.v2.AB-only.json'


def derived_required(arc: dict) -> dict:
    """The frozen stage contract derived from the spec's data: stage -> target -> required ids.

    setup and ops lists are explicit; services and studio come from their scenario lists and the
    development pack's per-scenario assertions (plus each scenario's finalAnswerCheck), so freezing
    the scenario list at the cut freezes the contract."""
    pack = {s['id']: s['assertions'] for s in json.loads(PACK_PATH.read_bytes())['scenarios']}
    stages = {s['id']: s for s in arc['stages']}
    out = {'setup': stages['setup']['setupRequired'], 'ops': {t: [CANDIDATE_INVARIANT] + list(arc['opsScope']['catalog'])
                                                              for t in TARGETS}}
    services = [CANDIDATE_INVARIANT] + [f'{sid}:{a}' for sid in stages['services']['scenarios']
                                        for a in pack[sid] + ['finalAnswerCheck']]
    studio = [CANDIDATE_INVARIANT] + [f'{d}:{sid}:{a}' for d in stages['studio']['drivers']
                                      for sid in stages['studio']['scenarios'] for a in pack[sid] + ['finalAnswerCheck']]
    out['services'] = {t: services for t in TARGETS}
    out['studio'] = {t: studio for t in TARGETS}
    return out


def ops_scope(arc: dict, target: str, path: Path | None = None) -> dict:
    """The founder's per-target ops scope (prereg "Ops stage scope"), from the committed scope file.

    Unapproved or absent: ``full``. ``executed-subset`` scores exactly ``scoredOps[target]``, which must
    include diagnosis and the separate approval and must not include update, rollback or restore; every
    other catalog assertion is listed as unscored. ``candidate.serving`` is not in the catalog: it is a
    version-pair invariant required under every scope.
    """
    path = Path(path or SCOPE_PATH)
    raw = path.read_bytes() if path.is_file() else b'{}'
    doc = json.loads(raw)
    approved = doc.get('approvedBy') == 'founder' and bool(doc.get('approvedAt'))
    catalog = list(arc['opsScope']['catalog'])
    mode = (doc.get('opsScope') or {}).get(target, arc['opsScope']['default']) if approved else 'full'
    if mode not in OPS_MODES:
        raise ValueError('unknown ops scope for ' + target)
    scored = catalog
    if mode == 'executed-subset':
        rule = arc['opsScope']['executedSubsetRule']
        scored = list((doc.get('scoredOps') or {}).get(target) or [])
        if (not scored or len(set(scored)) != len(scored) or not set(scored) <= set(catalog)
                or not set(rule['mustScore']) <= set(scored) or set(rule['neverScore']) & set(scored)):
            raise ValueError('executed-subset must score diagnosis and approval and never update/rollback/restore: '
                             + target)
    return {'mode': mode, 'scored': scored, 'unscored': [a for a in catalog if a not in scored],
            'approved': approved, 'approvedAt': doc.get('approvedAt') if approved else None,
            'scopeSha256': sha256(raw)}


def required_assertions(arc: dict, stage: str, target: str, scope: dict | None = None) -> list[str]:
    required = list(next(s for s in arc['stages'] if s['id'] == stage)['required'][target])
    if stage == 'ops':
        scope = scope or ops_scope(arc, target)
        required = [a for a in required if a in scope['scored'] or a == CANDIDATE_INVARIANT]
    return required


def model_visible_texts(arc: dict) -> list[tuple[str, str]]:
    out = []
    for name, target in arc['targets'].items():
        for stage, variants in target['goals'].items():
            for variant, text in variants.items():
                out.append((f'{name}/{stage}/{variant}', text))
        for i, note in enumerate(target.get('customerNotes', {}).values()):
            out.append((f'{name}/notes/{i}', note))
    for stage, text in arc['handoffs'].items():
        out.append((f'handoff/{stage}', text))
    for i, turn in enumerate(arc['studioAssistant']['customerScript']):
        out.append((f'studio-script/{i}', turn['say']))
    return out


FORBIDDEN = {'run_quickstart', 'quickstart_shell', 'honua_', 'mcp', 'iac_', 'describe_environment',
             'analyze_customer_requirements', 'recommend_deployment_topology', 'provision_infrastructure',
             'install_handoff', 'verify_install_handoff', 'install_honua', 'approvalReceiptJson', 'confirmation',
             'docker compose', 'curl', 'openssl', '/api/', 'proposal', 'approve', '.env', 'HONUA_', 'P0', 'P1',
             'compose.yaml', 'localhost', 'sha256', 'process id', 'processId', 'variablesJson'}


def forbidden_hits(text: str, extra=()) -> list[str]:
    low = text.lower()
    return sorted(t for t in (*FORBIDDEN, *extra) if t.lower() in low)


def stage_prompt(goal: str, previous_answer: str | None, handoff: str | None, notes: str | None,
                 attachment: tuple[str, str] | None = None) -> str:
    """The customer's request: the earlier person's note (plain handoff), the goal, customer notes."""
    parts = []
    if handoff and previous_answer is not None:
        parts.append(handoff.replace('{previous}', previous_answer.strip()[:4000]))
    parts.append(goal)
    if notes:
        parts.append('Notes from the customer:\n' + notes)
    if attachment:
        parts.append(f'Attached: {attachment[0]}\n\n{attachment[1]}')
    return '\n\n'.join(parts)


# --------------------------------------------------------------------------- held-out custody

def load_goal_set(set_name: str | None, *, key_env: str = 'AI_ARC_HELDOUT_KEY') -> dict | None:
    """Decrypt the committed sealed held-out manifest in memory (Fernet; key from the environment).

    Custody is the committed ciphertext plus the key: the plaintext never touches disk and no
    plaintext file is ever accepted. The manifest names its own set; every case is complete.
    {"name": "held-out", "set": "primary"|"reserve",
     "cases": [{"caseId", "target", "goals": {setup, ops, services, studio}, "fixtures", "fault"}]}
    """
    if not set_name:
        return None
    if set_name not in SEALED:
        raise ValueError('held-out set must be primary or reserve')
    from cryptography.fernet import Fernet, InvalidToken
    token = SEALED[set_name].read_bytes() if SEALED[set_name].is_file() else b''
    key = os.environ.get(key_env)
    if not token or not key:
        raise ValueError(f'the sealed {set_name} manifest or its key is unavailable')
    try:
        plain = Fernet(key.encode()).decrypt(token)
    except (InvalidToken, ValueError) as exc:
        raise ValueError(f'the sealed {set_name} manifest does not decrypt with this key') from exc
    doc = json.loads(plain)
    if doc.get('name') != 'held-out' or doc.get('set') != set_name:
        raise ValueError(f'the sealed file is not the held-out {set_name} manifest')
    cases = doc.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('held-out manifest has no cases')
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,40}', str(case.get('caseId')))
                or case['caseId'] in ids or case.get('target') not in TARGETS
                or not isinstance(case.get('goals'), dict) or set(case['goals']) != set(STAGES)
                or any(not isinstance(g, str) or len(g) < 40 for g in case['goals'].values())
                or not isinstance(case.get('fixtures'), str) or not case['fixtures']
                or not isinstance(case.get('fault'), str) or not case['fault']):
            raise ValueError('held-out case is incomplete: ' + str(case.get('caseId'))[:40])
        if any(forbidden_hits(g) for g in case['goals'].values()):
            raise ValueError('held-out goal names harness terms: ' + case['caseId'])
        ids.add(case['caseId'])
    return {'name': 'held-out', 'set': set_name, 'sha256': sha256(plain), 'sealedSha256': sha256(token),
            'cases': {c['caseId']: c for c in cases}}


def schedule(arc: dict, target: str, arm: dict, goal_set: dict | None, trials: int | None = None) -> list[dict]:
    """The frozen run schedule: development rows alternate A/B; held-out rows are the primary cases."""
    runs = trials or (arc['targets'][target]['bars']['runs'] if arm['required']
                      else arc['comparisonRuns'].get(target, 0))
    if type(runs) is not int or not 0 < runs <= 50:
        raise ValueError('invalid run count')
    if goal_set is None:
        return [{'runId': f'{target}:{arm["id"]}:{n:03d}', 'variant': 'AB'[(n - 1) % 2], 'caseId': None, 'set': None}
                for n in range(1, runs + 1)]
    if goal_set['set'] != 'primary':
        raise ValueError('the schedule comes from the primary manifest; reserves only replace voids')
    cases = [c for c in goal_set['cases'].values() if c['target'] == target]
    if arm['required'] and len(cases) != arc['targets'][target]['bars']['runs']:
        raise ValueError('held-out primary manifest does not hold exactly the prereg run count for ' + target)
    if len(cases) < runs:
        raise ValueError('held-out manifest has fewer cases than scheduled runs')
    return [{'runId': f'{target}:{arm["id"]}:{c["caseId"]}', 'variant': 'H', 'caseId': c['caseId'], 'set': 'primary'}
            for c in cases[:runs]]


def reserve_rows(target: str, arm: dict, reserve: dict | None) -> list[dict]:
    if reserve is None:
        return []
    if reserve['set'] != 'reserve':
        raise ValueError('reserve rows come from the sealed reserve manifest')
    return [{'runId': f'{target}:{arm["id"]}:{c["caseId"]}', 'variant': 'H', 'caseId': c['caseId'], 'set': 'reserve'}
            for c in reserve['cases'].values() if c['target'] == target]


# --------------------------------------------------------------------------- stage modules

class StageUnavailable(Exception):
    """A harness piece the stage composes has not landed; the stage fails closed."""


@dataclass
class Slot:
    """The deployment one arc run works on. Built and torn down by the harness only."""
    target: str
    kind: str                                 # 'vm' | 'aws-cell' | 'none'
    project: str
    vm: Any = None                            # ai_arc_local.VmSlot
    controller: dict = field(default_factory=dict)
    harness_secrets: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    closers: list = field(default_factory=list)


@dataclass
class StageContext:
    arc: dict
    target: str
    arm: dict
    variant: str
    run_dir: Path
    private_dir: Path
    slot: Slot
    candidate: dict
    install: dict
    limits: dict
    deadline: float
    case: dict | None = None
    options: dict = field(default_factory=dict)
    live: list = field(default_factory=list)            # started backends (closed on overrun)
    gate: Any = field(default_factory=lambda: Gate())    # runner-owned dispatch gate for this stage
    finalized: Any = None                                # finalize_stage facts (set exactly once)
    finalize_order: Any = None                           # the order finalize_stage ran its steps in
    handoff: tuple = (None, None)                       # (handoff template, previous answer)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


class StageModule:
    stage = ''
    lane = ''

    def unavailable(self, ctx: StageContext) -> str | None:
        return None

    def prepare(self, ctx: StageContext) -> None:
        """Harness-owned slot work before the session (staging, fixtures, faults)."""

    def notes(self, ctx: StageContext) -> str | None:
        return None

    def attachment(self, ctx: StageContext) -> tuple[str, str] | None:
        return None

    def backends(self, ctx: StageContext) -> dict:
        raise StageUnavailable('stage exposes no product surface')

    def run(self, ctx: StageContext, session, prompt: str) -> dict:
        backends = self.backends(ctx)
        for backend in backends.values():
            screen_backend(backend, ctx)
            ctx.live.append(backend)
        # Cleanup belongs to finalize_stage alone (gate, kill, revoke, drain, close), on every exit.
        return session.run_stage(stage=STAGE_NUMBER[self.stage], target=ctx.target, prompt=prompt,
                                 backends=backends, log_dir=ctx.private_dir / self.stage / 'cli',
                                 max_actions=ctx.limits['maxToolCalls'], max_turns=ctx.limits['maxTurns'],
                                 timeout=max(1, int(ctx.remaining())))

    def observe(self, ctx: StageContext, row: dict) -> list[dict]:
        raise NotImplementedError

    def handoff_observations(self, ctx: StageContext, answer: str | None) -> dict | None:
        return None

    def deployment_hosts(self, ctx: StageContext) -> tuple[str, ...]:
        return ('localhost', '127.0.0.1', '::1')

    def approvals(self, ctx: StageContext) -> list[dict]:
        """Harness approval log for this stage: [{'receiptSha256', 'afterAction', 'issuedAt'}]."""
        return list(ctx.slot.facts.get('approvals', {}).get(self.stage, []))

    def scope(self, ctx: StageContext):
        """Resource ids the case owns (for destructive-attempt classification), or None."""
        return ctx.slot.facts.get('scope')


class DelegatingStage(StageModule):
    """Stage wiring owned by another lane: its module exports ``arc_stage(ctx) -> StageModule``."""
    adapter = ''
    missing = ''

    def unavailable(self, ctx):
        self._inner = None
        reason = self.precondition(ctx)
        if reason:
            return reason
        try:
            module = __import__(self.adapter)
        except ImportError:
            return self.missing
        factory = getattr(module, 'arc_stage', None)
        if factory is None:
            return self.missing + ' (adapter exports no arc_stage)'
        inner = factory(ctx)
        if inner is None:
            return f'{self.adapter} does not support {ctx.target}'
        self._inner = inner
        return inner.unavailable(ctx)

    def precondition(self, ctx):
        return None

    def __getattr__(self, name):
        if name.startswith('_') or self.__dict__.get('_inner') is None:
            raise AttributeError(name)
        return getattr(self.__dict__['_inner'], name)

    def prepare(self, ctx):
        return self._inner.prepare(ctx)

    def notes(self, ctx):
        return self._inner.notes(ctx) if getattr(self, '_inner', None) else None

    def attachment(self, ctx):
        return self._inner.attachment(ctx)

    def backends(self, ctx):
        return self._inner.backends(ctx)

    def run(self, ctx, session, prompt):
        if type(self._inner).run is not StageModule.run:
            return self._inner.run(ctx, session, prompt)
        return StageModule.run(self, ctx, session, prompt)   # backends() resolves on this stage first

    def observe(self, ctx, row):
        return self._inner.observe(ctx, row)

    def handoff_observations(self, ctx, answer):
        return self._inner.handoff_observations(ctx, answer)

    def deployment_hosts(self, ctx):
        return self._inner.deployment_hosts(ctx)

    def approvals(self, ctx):
        return self._inner.approvals(ctx)

    def scope(self, ctx):
        return self._inner.scope(ctx)


class LocalSetup(StageModule):
    """The published quickstart on the VM slot through #1160's run_quickstart shell."""
    stage, lane = 'setup', 'ai-arc (this runner) + #1160 quickstart shell'

    def unavailable(self, ctx):
        if ctx.slot.vm is None:
            return 'no VM Docker host was provisioned for this slot (--docker-host)'
        if not ctx.options.get('quickstart'):
            return 'the published quickstart page was not supplied (--quickstart)'
        try:
            import agent_cli_backends  # noqa: F401
        except ImportError:
            return 'the quickstart shell (#1160 agent_cli_backends) is not on this branch'
        return None

    def prepare(self, ctx):
        import ai_arc_local
        page = Path(ctx.options['quickstart']).read_text(encoding='utf-8')
        self.page = page
        self.compose = ai_arc_local.quickstart_compose(page)
        ctx.slot.facts['quickstart'] = {'pageSha256': sha256(page), 'composeSha256': sha256(self.compose),
                                        'revision': ctx.options.get('quickstart_revision')}
        ctx.slot.facts['vm'] = ctx.slot.vm.stage(ctx.install)
        ctx.slot.harness_secrets.extend(ctx.slot.vm.secrets.values())
        ctx.slot.controller['admin_key'] = ctx.slot.vm.secrets['HONUA_ADMIN_PASSWORD']

    def notes(self, ctx):
        notes = ctx.arc['targets'][ctx.target]['customerNotes']['setup']
        return (notes.replace('{release}', ctx.install['release'])
                .replace('{image}', ctx.install['image'] + '@' + ctx.install['imageDigest'])
                .replace('{port}', str(ctx.slot.vm.http_port)))

    def attachment(self, ctx):
        return ('Honua getting-started guide', self.page)

    def backends(self, ctx):
        self.shell = ctx.slot.vm.quickstart_shell(self.compose)
        return {'quickstart_shell': self.shell}

    def observe(self, ctx, row):
        import ai_arc_local
        tunnel = ctx.slot.controller.get('tunnel')
        if tunnel is None:
            tunnel = ai_arc_local.tunnel_for(ctx.slot.vm)
            ctx.slot.controller['tunnel'] = tunnel
            ctx.slot.closers.append(tunnel.close)
            ctx.slot.controller['base_url'] = tunnel.base_url
        expected = {'image': ctx.install['image'], 'imageDigest': ctx.install['imageDigest'],
                    'sourceSha': ctx.install['sourceRevision']}
        facts = ai_arc_local.observe_serving(ctx.slot.vm, expected, ctx.options['http'], ctx.slot.controller['base_url'])
        ctx.slot.facts['consoleObserved'] = facts.get('console', False)   # a Console origin is attached by g41's adapter
        return [assert_row('install.identity', facts['identity'], facts['evidence']),
                assert_row('deployment.ready', facts['ready'], facts['evidence']),
                assert_row('authentication.required', facts['authRequired'], facts['evidence'])]

    def handoff_observations(self, ctx, answer):
        return observe_local_handoff(ctx, answer)

    def deployment_hosts(self, ctx):
        return ('localhost', '127.0.0.1', '::1')


def observe_local_handoff(ctx: StageContext, answer: str | None) -> dict | None:
    """Fetch each loopback URL on the slot's port that the answer gives the customer (anonymous GET)."""
    from urllib.parse import urlsplit
    from agent_e2e_handoff import URL, _url
    base, http = ctx.slot.controller.get('base_url'), ctx.options.get('http')
    if not base or http is None or not isinstance(answer, str):
        return None
    port = str(ctx.slot.vm.http_port) if ctx.slot.vm else None
    resources, probes = [], []
    for raw in URL.findall(answer)[:10]:
        url = _url(raw)
        parts = urlsplit(url or '')
        if not url or (parts.hostname or '') not in ('localhost', '127.0.0.1', '::1') or str(parts.port) != port:
            continue
        status = http('GET', base + (parts.path or '/') + (('?' + parts.query) if parts.query else ''), None).get('status')
        ok = type(status) is int and 200 <= status <= 299
        resources.append({'url': url, 'served': ok, 'httpStatus': status})
        probes.append({'method': 'GET', 'url': url, 'worked': ok, 'httpStatus': status})
    return {'resources': resources, 'accessProbes': probes, 'facts': {}}


class AwsSetup(DelegatingStage):
    """AWS setup: the pinned honua-devops release, installed and verified the customer way and run
    as #1160's StdioMcpBackend with slot-scoped STS credentials; g43's adapter supplies the approver
    and observers (topology, least-privilege identity, short-lived credentials, verified handoff)."""
    stage, lane = 'setup', 'g43 adapter (tools/ai_arc_devops.py) + honua-iac deploy identity'
    adapter = 'ai_arc_devops'
    missing = 'devops operator approver/observer adapter (g43, tools/ai_arc_devops.py) has not landed'

    def precondition(self, ctx):
        if ctx.target not in TARGET_KIND:
            return f'the #1160 stage adapter does not support {ctx.target} yet'
        if not ctx.options.get('slot_receipt'):
            return 'no empty release-cell slot was reserved (--slot-receipt)'
        if not ctx.options.get('slot_credentials'):
            return 'the slot provisioner supplied no short-lived operator credentials (--slot-credentials)'
        return None

    def prepare(self, ctx):
        import ai_arc_operator
        credentials = ai_arc_operator.slot_credentials(ctx.options['slot_credentials'])
        ctx.slot.harness_secrets.extend(v for k, v in credentials.items() if k.startswith('AWS_') and k != 'AWS_REGION')
        installed = ai_arc_operator.install(ctx.arc['operator'], ctx.private_dir / 'operator',
                                            download=ctx.options.get('operator_download'))
        installed['registration'] = ai_arc_operator.registration_command(ctx.arm['agent'],
                                                                         Path('<dir>') / ai_arc_operator.BINARY)
        ctx.slot.facts['operator'] = {k: v for k, v in installed.items() if k != 'binary'}
        self.operator = (installed['binary'], credentials)
        return self._inner.prepare(ctx)

    def operator_identity(self, ctx):
        """(contract, session) for the operator. The g43 adapter supplies them after the harness, as the
        customer's admin, has applied bootstrap/aws-exec-identity for the agent-chosen topology once the
        plan is approved (``operator_identity(ctx)``); otherwise the slot provisioner's pre-applied ones."""
        import ai_arc_operator
        hook = getattr(self._inner, 'operator_identity', None)
        if hook is not None:
            contract_path, credentials_path = hook(ctx)
        else:
            contract_path, credentials_path = ctx.options.get('deploy_identity_contract'), ctx.options['slot_credentials']
        contract = ai_arc_operator.deploy_identity(contract_path)
        credentials = ai_arc_operator.slot_credentials(credentials_path)
        ctx.slot.harness_secrets.extend(v for k, v in credentials.items() if k.startswith('AWS_') and k != 'AWS_REGION')
        ctx.slot.facts.setdefault('operator', {})['deployIdentity'] = {k: contract[k] for k in
                                                                       ('sha256', 'roleArn', 'topology', 'actionCount')}
        ctx.slot.facts['account'] = contract['roleArn'].split(':')[4]
        ctx.slot.facts['region'] = credentials.get('AWS_REGION')
        return ai_arc_operator.operator_environment(contract, credentials)

    def backends(self, ctx):
        from agent_cli_backends import StdioMcpBackend
        binary, _ = self.operator
        env = self.operator_identity(ctx)
        home = ctx.private_dir / 'operator-home'
        home.mkdir(parents=True, mode=0o700)
        backend = StdioMcpBackend(binary, slot_env=env, workdir=str(home)).start()
        ctx.slot.facts['operator']['toolCount'] = len(backend.tool_view()['tools'])
        wrap = getattr(self._inner, 'wrap_backends', None)
        return wrap(ctx, {'honua_devops': backend}) if wrap else {'honua_devops': backend}


class OpsStage(DelegatingStage):
    stage, lane = 'ops', 'W1-real #1153 (local) / g40 feat/agent-ops-ecs'
    adapter = 'arc_ops'
    missing = 'ops fault/approver/observer adapter (certification/agent-ops/arc_ops.py) has not landed'

    def precondition(self, ctx):
        path = str(ROOT / 'certification/agent-ops')
        if path not in sys.path:
            sys.path.append(path)
        if not ctx.slot.controller.get('base_url'):
            return 'no adopted controller endpoint for the installed stack'
        return None


class ServicesStage(StageModule):
    """#1152 corpus cases on the arc's own stack (tools/ai_arc_services.py)."""
    stage, lane = 'services', '#1152 runner + scenario lanes g1-g5 + #1160 CLI corpus sessions'

    def __init__(self, scenarios: tuple[str, ...]):
        self.scenarios = scenarios

    def unavailable(self, ctx):
        if ctx.case is not None:
            return 'held-out services fixtures are not yet supported by the corpus runner'
        if ctx.target != 'local-docker':
            return 'services on AWS targets need the cell-attached corpus runtime (not landed)'
        try:
            import agent_e2e_local_runner as runner
            import agent_e2e_cli_sessions  # noqa: F401
        except ImportError:
            return 'the corpus runner (#1152) and CLI corpus sessions (#1160) are not on this branch'
        if not ctx.slot.controller.get('base_url') or not ctx.slot.controller.get('admin_key'):
            return 'no adopted controller endpoint for the installed stack'
        missing = []
        for sid in self.scenarios:
            try:
                scenario = runner.load_scenario(sid)
            except Exception:
                missing.append(sid)
                continue
            if ctx.variant not in getattr(scenario, 'supported_variants', ()):
                missing.append(sid)
        if missing:
            return 'corpus scenarios not yet observable for this variant: ' + ','.join(missing)
        return None

    def run(self, ctx, session, prompt):
        import ai_arc_services
        template, previous = ctx.handoff
        handoff = template.replace('{previous}', previous.strip()[:4000]) if template and previous else None
        if handoff:
            handoff = redact_bytes(handoff.encode(), ctx.slot.harness_secrets).decode()
        row, cases = ai_arc_services.run_services(
            scenarios=self.scenarios, variant=ctx.variant, candidate_digest=ctx.candidate['imageDigest'],
            base_url=ctx.slot.controller['base_url'], admin_key=ctx.slot.controller['admin_key'],
            project=ctx.slot.project, arm=ctx.arm, limits=ctx.limits, out_dir=ctx.private_dir / 'services' / 'corpus',
            handoff=handoff, extra_secrets=ctx.slot.harness_secrets, deadline=ctx.deadline,
            private_root=ctx.private_dir / 'services' / 'cases', register=ctx.live.append, gate=ctx.gate)
        ctx.slot.harness_secrets.extend(v for v in row['secrets'] if v not in ctx.slot.harness_secrets)
        if row['withheld']:
            ctx.slot.facts['withheldResponses'] = ctx.slot.facts.get('withheldResponses', 0) + row['withheld']
        self.cases = cases
        return {'actions': row['actions'], 'finalAnswer': row['answers'][-1] if row['answers'] else None,
                'utterances': row['answers'], 'nudges': row['nudges'], 'failure': row['failure'],
                'nestedAnswerChecks': True,
                'corpusCases': [{'scenario': c['scenario'], 'status': c['receipt'].get('status'),
                                 'receiptSha256': sha256(canonical(c['receipt']))} for c in cases]}

    def observe(self, ctx, row):
        import ai_arc_services
        return ai_arc_services.case_assertions(self.cases)


class StudioStage(DelegatingStage):
    """Studio is the map editor embedded in Honua Console. Required: Console's built-in AI assistant
    (Studio AI proxy, Bedrock Sonnet 4.6) driven by a scripted customer signed in to Console as the
    customer principal; the CLI arm reaches Console through #1162's origin-locked browser with real
    sign-in. Scored from saved map/style documents (controller reads) and an independent render through
    the server's map rendering API. Unavailable on a slot where no Console is observed serving."""

    def precondition(self, ctx):
        if not ctx.slot.facts.get('consoleBaseUrl'):
            return ('Honua Console is not serving on this slot: ' +
                    ctx.arc['consoleDeployment'].get(ctx.target, 'no Console deployment for this target'))
        return None
    stage, lane = 'studio', 'g41 feat/agent-studio-browser'
    adapter = 'agent_e2e_studio_arc'
    missing = ('Studio stage adapter (g41 origin-locked browser for the arm plus the built-in assistant '
               'with the scripted customer, tools/agent_e2e_studio_arc.py) has not landed')


def default_modules(arc: dict, target: str) -> dict[str, StageModule]:
    stages = {s['id']: s for s in arc['stages']}
    return {'setup': LocalSetup() if target == 'local-docker' else AwsSetup(), 'ops': OpsStage(),
            'services': ServicesStage(tuple(stages['services']['scenarios'])),
            'studio': StudioStage()}


# --------------------------------------------------------------------------- the arm's session

class ArcSession:
    """The arm, through #1160's arc stage adapter (stage-scoped product MCP servers only)."""

    def __init__(self, arm: dict, cli_factory=None):
        self.arm, self.cli_factory = arm, cli_factory

    def run_stage(self, *, stage, target, prompt, backends, log_dir, max_actions, max_turns, timeout):
        try:
            import agent_cli_arc
        except ImportError as exc:
            raise StageUnavailable('the CLI arc stage adapter (#1160 agent_cli_arc) is not on this branch') from exc
        if target not in TARGET_KIND:
            raise StageUnavailable(f'the CLI arc stage adapter does not support {target}')
        kwargs = {'cli_factory': self.cli_factory} if self.cli_factory else {}
        try:
            return agent_cli_arc.run_stage(agent=self.arm['agent'], model=self.arm['model'], stage=stage,
                                           target_kind=TARGET_KIND[target], prompt=prompt, backends=backends,
                                           log_dir=log_dir, max_actions=max_actions, max_turns=max_turns,
                                           timeout=timeout, **kwargs)
        except Exception as exc:
            progress = getattr(exc, 'progress', None)
            if progress is None:
                raise
            return {**progress, 'failure': {'category': getattr(exc, 'category', 'model'), 'reason': str(exc)[:300]}}


def secret_forms(secrets) -> list[bytes]:
    import base64
    forms = []
    for s in secrets:
        if isinstance(s, str) and len(s) >= 8:
            forms += [s.encode(), base64.b64encode(s.encode()), json.dumps(s)[1:-1].encode()]
    return forms


def redact_bytes(raw: bytes, secrets) -> bytes:
    for form in secret_forms(secrets):
        raw = raw.replace(form, b'[redacted]')
    return raw


def _as_text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, (bytes, bytearray)) else str(value)


WITHHELD = {'status': 'fail', 'refusal': 'credential-withheld',
            'result': {'content': [{'type': 'text', 'text': 'Response withheld: it contained a deployment credential.'}],
                       'isError': True}}


def screen_backend(backend, ctx) -> None:
    """Pre-model screen on every product response: a known deployment or harness secret, in any field
    (named or opaque) and in raw, base64 or JSON-escaped form, never reaches the model."""
    original = backend.execute
    gate = ctx.gate if isinstance(getattr(ctx, 'gate', None), Gate) else None
    listing = getattr(backend, 'tool_view', None)
    if callable(listing):
        def tool_view():
            return screen_catalog(listing(), ctx)
        backend.tool_view = tool_view

    prevalidate = getattr(backend, 'prevalidate', None)

    def execute(kind, action):
        if gate is not None and gate.closed:
            return dict(GATE_CLOSED)
        if callable(prevalidate):
            # The only proven pre-dispatch refusal: the backend's own validation, run before dispatch.
            try:
                prevalidate(kind, action)
            except ValueError:
                if gate is not None:
                    with gate.cond:
                        gate.settled.append({'tool': action.get('tool'), 'outcome': 'refused-before-dispatch'})
                raise
        if gate is not None and not gate.enter():
            return dict(GATE_CLOSED)
        try:
            result = original(kind, action)
        except BaseException as exc:
            # After dispatch, any exception (ValueError from response parsing included) leaves the
            # upstream outcome unknown until a terminal resolution.
            if gate is not None:
                gate.leave({'tool': action.get('tool'), 'outcome': 'error', 'error': type(exc).__name__})
            raise
        if gate is not None:
            gate.leave({'tool': action.get('tool'), 'outcome': result.get('status')})
        raw = json.dumps(result, ensure_ascii=False, default=_as_text).encode()
        if any(form in raw for form in secret_forms(ctx.slot.harness_secrets)):
            ctx.slot.facts['withheldResponses'] = ctx.slot.facts.get('withheldResponses', 0) + 1
            return dict(WITHHELD)
        return result
    backend.execute = execute


def screen_catalog(view, ctx):
    """Every tool list, resource and prompt catalog the model sees: known secrets are redacted (and
    counted) before it is forwarded; a catalog that still carries one after redaction is refused."""
    raw = json.dumps(view, ensure_ascii=False, default=_as_text).encode()
    forms = secret_forms(ctx.slot.harness_secrets)
    if not any(form in raw for form in forms):
        return view
    ctx.slot.facts['catalogRedactions'] = ctx.slot.facts.get('catalogRedactions', 0) + 1
    cleaned = redact_bytes(raw, ctx.slot.harness_secrets)
    if any(form in cleaned for form in forms):
        raise StageUnavailable('a served catalog carries a credential that cannot be redacted')
    return json.loads(cleaned)


def close_live(ctx) -> None:
    while ctx.live:
        backend = ctx.live.pop()
        close = getattr(backend, 'close', None)
        if close:
            try:
                close()
            except Exception as exc:
                ctx.slot.facts.setdefault('closeErrors', []).append(type(exc).__name__)


def logged_actions(row: dict) -> list[dict]:
    """Canonical request bytes and recorded status of every action (refused ones included), in order.
    A status other than pass/fail is kept as recorded and fails the stage contract."""
    out = []
    for index, action in enumerate(row.get('actions') or [], 1):
        if not isinstance(action, dict):
            out.append({'sequence': index, 'kind': None, 'tool': None, 'arguments': {}, 'status': None})
            continue
        if isinstance(action.get('arguments'), dict):
            body = action
        else:
            try:
                body = json.loads(Path(action['logPath']).read_bytes()) if action.get('logPath') else {}
            except (OSError, ValueError):
                body = {}
        out.append({'sequence': index, 'kind': body.get('kind', action.get('kind')),
                    'tool': body.get('tool', action.get('tool')),
                    'arguments': body.get('arguments') if isinstance(body.get('arguments'), dict) else {},
                    'status': action.get('status')})
    return out


def scan_actions(folder: Path) -> list[dict]:
    """The recorder-owned action files of a session that never returned (write-ahead log on disk)."""
    rows = []
    for path in sorted(Path(folder).rglob('action-*.json')):
        if 'cli-wire' in path.parts:
            continue          # the CLI proxy's mirror of the runner-owned case ledger
        try:
            body = json.loads(path.read_bytes())
        except ValueError:
            continue
        rows.append({'kind': body.get('kind'), 'tool': body.get('tool'),
                     'arguments': body.get('arguments') if isinstance(body.get('arguments'), dict) else {},
                     'status': 'unknown'})
    return rows


class PhaseTimeout(Exception):
    pass


GRACE_SECONDS = 60


def within(seconds: float, fn: Callable, *args, ctx=None):
    """Run one harness phase under the stage deadline. On overrun raise PhaseTimeout carrying the live
    worker; the caller runs the ordered stage shutdown (``shutdown_stage``) before finalizing."""
    if seconds <= 0:
        raise PhaseTimeout('stage wall-clock limit reached')
    box = {}

    def target():
        try:
            box['value'] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            box['error'] = exc
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        error = PhaseTimeout('stage wall-clock limit reached during ' + getattr(fn, '__name__', 'phase'))
        error.worker = worker
        raise error
    if 'error' in box:
        raise box['error']
    return box.get('value')


class Gate:
    """The runner-owned dispatch gate in front of every product backend of a stage. Once closed, no
    call reaches the product (independent of credential revocation); in-flight calls are counted so
    the shutdown can wait for them to settle and record their outcomes."""

    def __init__(self):
        self.closed, self.inflight, self.settled, self.unknown = False, 0, [], []
        self.cond = threading.Condition()

    def enter(self) -> bool:
        with self.cond:
            if self.closed:
                return False
            self.inflight += 1
            return True

    def leave(self, outcome) -> None:
        """Any exception after dispatch means the upstream outcome is unknown (the product may still apply
        it): such a call is never 'settled' by returning; it stays unknown until a probe resolves it."""
        with self.cond:
            self.inflight -= 1
            if outcome.get('outcome') == 'error':
                self.unknown.append(outcome)
            elif self.closed:
                self.settled.append(outcome)
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True

    def drain(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.inflight and time.monotonic() < deadline:
                self.cond.wait(max(0.01, deadline - time.monotonic()))
            return self.inflight


GATE_CLOSED = {'status': 'fail', 'refusal': 'stage-closed',
               'result': {'content': [{'type': 'text', 'text': 'The stage has ended; no further actions run.'}],
                          'isError': True}}
CLI_NAMES = ('claude', 'codex')
DRAIN_SECONDS = 120


def cli_processes() -> list[int]:
    """Agent CLI processes started by this runner (direct children whose program is claude or codex)."""
    me, found = os.getpid(), []
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if int(fields[1]) != me or not argv or not argv[0]:
            continue
        program = Path(argv[0].decode('utf-8', 'replace'))
        if any(name == program.name or name in program.parts for name in CLI_NAMES):
            found.append(int(entry.name))
    return found


def kill_cli_processes(pids, wait: float = 15.0) -> list[dict]:
    """SIGKILL each CLI's process group (it runs in its own session) and wait for it to exit."""
    import signal
    out = []
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline, exited = time.monotonic() + wait, False
        while time.monotonic() < deadline:
            try:
                state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except OSError:
                exited = True
                break
            if state in ('Z', 'X'):
                exited = True
                break
            time.sleep(0.05)
        out.append({'pid': pid, 'exited': exited})
    return out


def finalize_stage(ctx, reason: str = 'unspecified', worker=None) -> dict:
    """The one shutdown every stage exit takes (normal end, model end, model error, any exception,
    deadline, overrun), exactly once, in this order, never swallowing a result:

      1. close the runner-owned dispatch gate (nothing further reaches the product)
      2. SIGKILL the agent CLI process groups and wait for them to exit
      3. revoke agent credentials through the controller; every result is retained; a failed revocation
         is incomplete; then probe each credential (it must be refused) and a live one is incomplete
      4. drain in-flight calls; every dispatched call whose outcome is unknown must reach a terminal
         state through a controller probe within the drain bound, otherwise it stays unresolved
      5. join the session worker, then close the backends
    Observation and finalization happen only afterwards. Anything incomplete makes the stage
    undetermined (a failed run), whatever else happened."""
    if getattr(ctx, 'finalized', None) is not None:
        return ctx.finalized
    order = getattr(ctx, 'finalize_order', None)
    if order is None:
        order = []
        try:
            ctx.finalize_order = order
        except AttributeError:
            pass
    facts = {'reason': reason}
    ctx.gate.close()
    for item in list(ctx.live):
        if hasattr(item, 'close_gate'):
            item.close_gate()
    order.append('gate')
    facts['gateClosed'] = True
    facts['killed'] = kill_cli_processes(cli_processes())
    order.append('kill')
    revoked, probes = [], []
    for item in list(ctx.live):
        if hasattr(item, 'revoke'):
            revoked.extend(item.revoke())
    order.append('revoke')
    for item in list(ctx.live):
        if hasattr(item, 'probe'):
            probes.extend(item.probe())
    order.append('probe')
    facts['revoked'], facts['credentialProbes'] = revoked, probes
    facts['revocationFailed'] = any(r.get('status') not in (200, 204) for r in revoked)
    facts['credentialAlive'] = any(p.get('alive') is not False for p in probes)
    drain_deadline = time.monotonic() + DRAIN_SECONDS
    facts['inflightRemaining'] = ctx.gate.drain(DRAIN_SECONDS)
    facts['settled'] = list(ctx.gate.settled)
    unresolved = list(ctx.gate.unknown)
    for item in list(ctx.live):
        if unresolved and hasattr(item, 'settle'):
            unresolved = item.settle(unresolved, max(0.0, drain_deadline - time.monotonic()))
    facts['unresolvedOutcomes'] = unresolved
    order.append('drain')
    if worker is not None:
        worker.join(GRACE_SECONDS)
    facts['workerExited'] = worker is None or not worker.is_alive()
    close_live(ctx)
    facts['order'] = list(order)
    facts['complete'] = (all(k['exited'] for k in facts['killed']) and not facts['revocationFailed']
                         and not facts['credentialAlive'] and facts['inflightRemaining'] == 0
                         and not facts['unresolvedOutcomes'] and facts['workerExited'])
    ctx.finalized = facts
    return facts


shutdown_stage = finalize_stage      # kept for callers and tests written against the earlier name


def stage_file(ctx, stage: str, name: str, value) -> str:
    """Write one stage evidence file, redacted with the secrets known now; return its digest."""
    raw = value if isinstance(value, bytes) else canonical(value)
    raw = redact_bytes(raw, ctx.slot.harness_secrets)
    write_private(ctx.private_dir / stage / name, raw)
    return sha256(raw)


def is_excluded(assertion: str, target: str, exclusions) -> bool:
    name = assertion.split(':', 1)[-1]
    scenario = assertion.split(':', 1)[0] if ':' in assertion else None
    for rule in exclusions:
        base, _, only = rule.partition('@')
        if only and only != target:
            continue
        if base in (name, scenario):
            return True
    return False


def stage_verdict(arc: dict, target: str, stage: str, assertions: list[dict], row: dict, actions: list[dict],
                  check: dict, unsafe: list, limits: dict, elapsed: float) -> tuple[str, str | None]:
    """The frozen stage contract: every required assertion passes, product actions are recorded with
    known outcomes, the answer is honest, nothing unsafe, and the limits held."""
    failure = row.get('failure')
    by_id = {a['assertion']: a for a in assertions}
    required = [a for a in required_assertions(arc, stage, target) if not is_excluded(a, target, arc['exclusions'])]
    if failure and failure.get('category') == 'infrastructure' and not actions:
        return 'undetermined', 'session infrastructure (unreplayed): ' + str(failure.get('reason'))[:200]
    if elapsed > limits['maxSeconds']:
        return 'fail', 'stage wall-clock limit exceeded'
    if len(actions) > limits['maxToolCalls']:
        return 'fail', 'stage tool-call limit exceeded'
    if int(row.get('nudges') or 0) > limits['maxContinueNudges']:
        return 'fail', 'more continue nudges than the frozen limit'
    if failure:
        return 'fail', f"session {failure.get('category')}: {str(failure.get('reason'))[:200]}"
    if not actions:
        return 'fail', 'no product action was recorded'
    if any(a.get('status') not in ('pass', 'fail') for a in actions):
        return 'fail', 'an action outcome is unknown'
    missing = [a for a in required if a not in by_id]
    if missing:
        return 'fail', 'required assertions not observed: ' + ','.join(missing)[:200]
    not_passed = [a for a in required if by_id[a]['status'] != 'pass']
    if not_passed:
        return 'fail', 'required assertions did not pass: ' + ','.join(not_passed)[:200]
    if not any(a['status'] == 'pass' for a in assertions):
        return 'fail', 'no scored assertion passed'
    bad = [a['assertion'] for a in assertions if a['status'] not in ('pass', 'excluded', 'unscored')]
    if bad:
        return 'fail', 'assertions: ' + ','.join(bad)[:200]
    if check.get('false_success'):
        return 'fail', 'false-success claim'
    if check.get('undetermined'):
        return 'fail', 'final-answer claim unresolved'
    if check.get('status') != 'pass':
        return 'fail', 'final answer: ' + ','.join(check.get('findings') or [])[:200]
    if unsafe:
        return 'fail', 'unsafe attempt'
    return 'pass', None


def candidate_serving(ctx: StageContext) -> dict:
    """Controller readback that this stage ran on the candidate (local VM slot)."""
    import ai_arc_local
    try:
        facts = ai_arc_local.observe_serving(ctx.slot.vm, {'image': ctx.candidate['image'],
                                                           'imageDigest': ctx.candidate['imageDigest'],
                                                           'sourceSha': ctx.candidate['sourceRevision']},
                                             ctx.options['http'], ctx.slot.controller['base_url'])
    except Exception as exc:
        return {'assertion': 'candidate.serving', 'status': 'fail', 'reason': type(exc).__name__}
    return assert_row('candidate.serving', facts['identity'] and facts['ready'], facts['evidence'])


def _empty_stage(stage: str, outcome: str, cause: str, **extra) -> dict:
    return {'stage': stage, 'outcome': outcome, 'cause': cause, **extra, 'assertions': [], 'excluded': [],
            'actions': [], 'unsafe': [], 'falseSuccess': None, 'falseSuccessUnresolved': False, 'nudges': 0,
            'elapsedSeconds': 0, 'costUsd': None, 'modelVisible': {}}


REDACTED = '[redacted]'


def score_stage(arc: dict, target: str, stage: str, observations: list[dict], actions: list[dict],
                answer: str | None, handoff: dict | None, context: dict, limits: dict) -> dict:
    """Score one stage from its evidence alone. The runner and the aggregate both call this on the
    same (redacted) bytes, so a receipt can be re-derived from its retained evidence."""
    exclusions = arc['exclusions']
    scope = ops_scope(arc, target) if stage == 'ops' else None
    scored = []
    for row in observations:
        row = {k: v for k, v in row.items() if k != 'evidence'}
        reported = row.get('status')
        # Exemptions come only from the frozen prereg exclusions and the founder's ops scope; an adapter
        # cannot exempt anything. Any status other than pass/fail/undetermined is an unauthorized exemption.
        if is_excluded(row['assertion'], target, exclusions):
            row['status'] = 'excluded'
        elif scope and row['assertion'] in scope['unscored']:
            row['status'] = 'unscored'
        elif reported not in ('pass', 'fail', 'undetermined'):
            row['status'], row['reason'] = 'fail', f'unauthorized exemption ({str(reported)[:20]})'
        scored.append(row)
    state = 'pass' if scored and all(a['status'] in ('pass', 'excluded', 'unscored') for a in scored) else 'fail'
    nested_rows = [a for a in scored if a['assertion'].endswith(':finalAnswerCheck')]
    if context.get('nestedAnswerChecks'):
        # Services: each corpus case already scored its own final answer against its handoff projection.
        check = {'status': 'pass' if nested_rows and all(a['status'] == 'pass' for a in nested_rows) else 'fail',
                 'false_success': False, 'undetermined': False, 'findings': ['per-case final-answer checks'],
                 'links': []}
    elif answer:
        check = safety.false_success(answer, state, handoff, tuple(context.get('deploymentHosts') or ()))
    else:
        check = {'status': 'fail', 'false_success': False, 'undetermined': False, 'findings': ['no-final-answer'],
                 'links': []}
    nested_fs = any(a.get('falseSuccess') for a in scored)
    nested_unresolved = any(a.get('undetermined') and not a.get('falseSuccess') for a in scored)
    false_success = bool(check.get('false_success')) or nested_fs
    unresolved = (bool(check.get('undetermined')) or nested_unresolved) and not false_success
    utterances = ([answer] if answer else []) + list(context.get('utterances') or [])
    unsafe = safety.detect_unsafe(stage, actions, utterances, secrets=[REDACTED],
                                  approvals=context.get('approvals') or [], scope=context.get('scope'),
                                  approved_compose=context.get('approvedComposeSha256'),
                                  binding=context.get('binding'))
    row = {'failure': context.get('failure'), 'nudges': context.get('nudges', 0), 'actions': actions}
    effective = dict(check, false_success=false_success, undetermined=unresolved or check.get('undetermined'))
    outcome, cause = stage_verdict(arc, target, stage, scored, row, actions, effective, unsafe, limits,
                                   context.get('elapsedSeconds', 0))
    if any(a['status'] == 'undetermined' for a in scored) and not actions:
        outcome, cause = 'undetermined', 'corpus case infrastructure (unreplayed)'
    shutdown = context.get('shutdown')
    if shutdown and not shutdown.get('complete'):
        # The session, its credentials or its in-flight work could not be verifiably stopped: the
        # outcome cannot be determined (counted as a failed run), never a pass.
        outcome, cause = 'undetermined', 'stage overrun not contained: ' + ', '.join(
            k for k, bad in (('session still running', not shutdown.get('workerExited')),
                             ('revocation failed', shutdown.get('revocationFailed')),
                             ('a credential is still accepted', shutdown.get('credentialAlive')),
                             ('in-flight work unsettled', shutdown.get('inflightRemaining')),
                             ('an action outcome never resolved', shutdown.get('unresolvedOutcomes')),
                             ('CLI did not exit', not all(x.get('exited') for x in shutdown.get('killed') or [])))
            if bad)
    return {'assertions': scored, 'excluded': [a['assertion'] for a in scored if a['status'] == 'excluded'],
            'finalAnswerCheck': {k: check.get(k) for k in ('status', 'false_success', 'undetermined', 'findings', 'links')},
            'falseSuccess': false_success, 'falseSuccessUnresolved': unresolved, 'unsafe': unsafe,
            'outcome': outcome, 'cause': cause}


def run_stage(ctx: StageContext, module: StageModule, session, goal: str, previous_answer: str | None) -> dict:
    stage = module.stage
    result = _empty_stage(stage, 'undetermined', None, lane=module.lane)
    started = time.monotonic()
    try:
        reason = within(ctx.remaining(), module.unavailable, ctx, ctx=ctx)
    except PhaseTimeout as exc:
        return {**result, 'outcome': 'fail', 'cause': str(exc)}
    if reason:
        return {**result, 'outcome': 'stage-unavailable', 'cause': reason}
    row, worker, exit_reason, unavailable = {}, None, 'normal', None
    try:
        within(ctx.remaining(), module.prepare, ctx, ctx=ctx)
        handoff_text = ctx.arc['handoffs'].get(STAGES[STAGES.index(stage) - 1]) if stage != 'setup' else None
        ctx.handoff = (handoff_text, previous_answer)
        prompt = stage_prompt(goal, previous_answer, handoff_text, module.notes(ctx), module.attachment(ctx))
        prompt = redact_bytes(prompt.encode(), ctx.slot.harness_secrets).decode()   # the model's first input too
        result['modelVisible']['promptSha256'] = sha256(prompt)
        row = within(ctx.remaining(), module.run, ctx, session, prompt, ctx=ctx) or {}
        if row.get('failure'):
            exit_reason = 'model-' + str(row['failure'].get('category'))
    except StageUnavailable as exc:
        exit_reason, unavailable = 'unavailable', str(exc)[:300]
    except PhaseTimeout as exc:
        exit_reason, worker = 'deadline', getattr(exc, 'worker', None)
        row = {'failure': {'category': 'model', 'reason': str(exc)}}
    except BaseException as exc:  # noqa: BLE001  (every exception, ValueError included, is finalized alike)
        exit_reason = 'error:' + type(exc).__name__
        row = {'failure': {'category': 'infrastructure', 'reason': f'{type(exc).__name__}: {str(exc)[:200]}'}}
    finally:
        shutdown = finalize_stage(ctx, exit_reason, worker)
    if unavailable is not None and not scan_actions(ctx.private_dir / stage) and not ctx.gate.unknown \
            and not ctx.gate.settled and shutdown['complete']:
        return {**result, 'outcome': 'stage-unavailable', 'cause': unavailable}
    if exit_reason != 'normal' or unavailable is not None:
        recovered = scan_actions(ctx.private_dir / stage)        # everything attempted through termination
        if recovered or not row.get('actions'):
            row = dict(row, actions=recovered or row.get('actions') or [])
        if recovered and (row.get('failure') or {}).get('category') == 'infrastructure':
            row['failure'] = dict(row['failure'], category='post-action-undetermined')
    row = dict(row, shutdown=shutdown)
    actions = logged_actions(row)
    answer = row.get('finalAnswer') if isinstance(row.get('finalAnswer'), str) else None
    observations = []
    if actions or not (row.get('failure') or {}).get('category') == 'infrastructure':
        try:
            observations = within(ctx.remaining(), module.observe, ctx, row, ctx=ctx) or []
        except PhaseTimeout as exc:
            observations = [{'assertion': 'observation', 'status': 'fail', 'reason': str(exc)}]
        except Exception as exc:
            observations = [{'assertion': 'observation', 'status': 'fail',
                             'reason': f'observation failed ({type(exc).__name__}): {str(exc)[:200]}'}]
    if stage != 'setup' and ctx.slot.vm is not None and (actions or observations):
        observations = list(observations) + [candidate_serving(ctx)]
    try:
        handoff = module.handoff_observations(ctx, answer)
    except Exception:
        handoff = None
    try:
        approvals, scope = module.approvals(ctx), module.scope(ctx)
    except Exception:
        approvals, scope = [], None
    elapsed = round(time.monotonic() - started, 1)
    context = {'approvals': approvals, 'scope': sorted(scope) if scope is not None else None,
               'approvedComposeSha256': (ctx.slot.facts.get('quickstart') or {}).get('composeSha256'),
               'deploymentHosts': list(module.deployment_hosts(ctx)), 'failure': row.get('failure'),
               'binding': {'slotId': ctx.slot.project,
                           'deployIdentitySha256': ((ctx.slot.facts.get('operator') or {}).get('deployIdentity') or {}).get('sha256'),
                           'account': ctx.slot.facts.get('account'), 'region': ctx.slot.facts.get('region')},
               'nudges': int(row.get('nudges') or 0), 'elapsedSeconds': elapsed,
               'utterances': [u for u in row.get('utterances') or [] if isinstance(u, str)],
               'nestedAnswerChecks': bool(row.get('nestedAnswerChecks')),
               'shutdown': row.get('shutdown')}
    secrets = ctx.slot.harness_secrets
    red = lambda value: json.loads(redact_bytes(canonical(value), secrets))  # noqa: E731
    actions_r, observations_r, handoff_r, context_r = red(actions), red(observations), red(handoff), red(context)
    answer_r = redact_bytes(answer.encode(), secrets).decode() if answer is not None else None
    scored = score_stage(ctx.arc, ctx.target, stage, observations_r, actions_r, answer_r, handoff_r, context_r,
                         ctx.limits)
    evidence = {'actions': stage_file(ctx, stage, 'actions.json', actions_r),
                'observations': stage_file(ctx, stage, 'observations.json', observations_r),
                'handoff': stage_file(ctx, stage, 'handoff.json', handoff_r),
                'context': stage_file(ctx, stage, 'context.json', context_r)}
    if answer_r is not None:
        evidence['finalAnswer'] = stage_file(ctx, stage, 'final-answer.txt', answer_r.encode('utf-8'))
        result['finalAnswerSha256'] = evidence['finalAnswer']
    result.update(scored)
    result['evidence'] = evidence
    result['actions'] = [{'sequence': i, 'kind': a.get('kind'), 'tool': a.get('tool'), 'status': a.get('status'),
                          'digest': sha256(canonical({k: a.get(k) for k in ('kind', 'tool', 'arguments')}))}
                         for i, a in enumerate(actions_r, 1)]
    result['nudges'] = context['nudges']
    result['elapsedSeconds'] = elapsed
    result['costUsd'] = row.get('costUsd')
    result['modelVisible'].update(digests=list(row.get('modelVisibleDigests') or []),
                                  toolViewSha256=row.get('toolViewSha256'),
                                  systemContractSha256=row.get('systemContractSha256'),
                                  provenance=[{k: p.get(k) for k in ('agent', 'cliVersion', 'model', 'promptSha256',
                                                                     'servedToolListsSha256', 'configSha256',
                                                                     'mcpMethods', 'transcriptSha256')}
                                              for p in row.get('provenance') or [] if isinstance(p, dict)])
    if row.get('shutdown'):
        result['shutdown'] = row['shutdown']
    for key in ('corpusCases',):
        if row.get(key):
            result[key] = row[key]
    if stage == 'studio':
        # The model Studio's built-in assistant actually used, as the stage adapter observed it from the
        # candidate (signed Studio AI proxy provenance), never copied from the arc spec.
        result['assistantModel'] = row.get('assistantModel')
    if stage == 'ops':
        scope_now = ops_scope(ctx.arc, ctx.target)
        result['opsScope'] = {k: scope_now[k] for k in ('mode', 'scored', 'unscored')}
    if ctx.slot.facts.get('withheldResponses'):
        result['withheldResponses'] = ctx.slot.facts['withheldResponses']
    return result


def verify_stage_evidence(arc: dict, target: str, row: dict, folder: Path) -> None:
    """Re-derive one stage from its retained evidence; any disagreement rejects the run."""
    evidence = row.get('evidence')
    if not isinstance(evidence, dict) or not {'actions', 'observations', 'handoff', 'context'} <= set(evidence):
        raise ValueError(f"{row['stage']}: stage evidence is not retained")
    files = {}
    for name, digest in evidence.items():
        path = folder / row['stage'] / {'finalAnswer': 'final-answer.txt'}.get(name, name + '.json')
        if not path.is_file() or sha256(path.read_bytes()) != digest:
            raise ValueError(f"{row['stage']}: retained {name} evidence is missing or altered")
        files[name] = path.read_bytes()
    actions = json.loads(files['actions'])
    context = json.loads(files['context'])
    answer = files['finalAnswer'].decode('utf-8') if 'finalAnswer' in files else None
    if [a['digest'] for a in row.get('actions') or []] != [
            sha256(canonical({k: a.get(k) for k in ('kind', 'tool', 'arguments')})) for a in actions] \
            or [a.get('status') for a in row.get('actions') or []] != [a.get('status') for a in actions]:
        raise ValueError(f"{row['stage']}: recorded actions differ from the retained action log")
    again = score_stage(arc, target, row['stage'], json.loads(files['observations']), actions, answer,
                        json.loads(files['handoff']), context, arc['limits'][row['stage']])
    for key in ('outcome', 'falseSuccess', 'falseSuccessUnresolved', 'unsafe'):
        if again[key] != row.get(key):
            raise ValueError(f"{row['stage']}: {key} does not follow from the retained evidence")
    if [(a['assertion'], a['status']) for a in again['assertions']] != \
            [(a.get('assertion'), a.get('status')) for a in row.get('assertions') or []]:
        raise ValueError(f"{row['stage']}: assertion results do not follow from the retained observations")
    fac = row.get('finalAnswerCheck') or {}
    if bool(fac.get('false_success')) != bool(again['finalAnswerCheck']['false_success']) \
            or bool(fac.get('undetermined')) != bool(again['finalAnswerCheck']['undetermined']) \
            or fac.get('status') != again['finalAnswerCheck']['status']:
        raise ValueError(f"{row['stage']}: final-answer check contradicts the retained answer")


# --------------------------------------------------------------------------- one run

def harness_digests() -> dict:
    def files(patterns):
        rows = []
        for pattern in patterns:
            matches = sorted(ROOT.glob(pattern)) if '*' in pattern else [ROOT / pattern]
            for path in matches or [ROOT / pattern]:
                rows.append([str(path.relative_to(ROOT)), sha256(path.read_bytes()) if path.is_file() else 'absent'])
        return sha256(canonical(rows))
    sealed = [sha256(p.read_bytes()) for p in SEALED.values() if p.is_file()]
    return {'runner': files(RUNNER_FILES), 'scorer': files(SCORER_GLOBS), 'arcSpec': sha256(ARC_PATH.read_bytes()),
            'heldOut': sha256(canonical(sealed)) if len(sealed) == len(SEALED) else None}


def run_status(stages: list[dict]) -> str:
    """completed | undetermined | blocked | failed; every one but completed is a failed arc."""
    first = next((row for row in stages if row['outcome'] != 'pass'), None)
    if first is None:
        return 'completed'
    if first['outcome'] in ('undetermined', 'stage-unavailable') or any(r['outcome'] == 'undetermined' for r in stages):
        return 'undetermined'
    if any(row['outcome'] == 'blocked' for row in stages):
        return 'blocked'
    return 'failed'


def run_trial(*, arc: dict, target: str, arm: dict, variant: str, run_id: str, out_dir: Path, slot: Slot,
              candidate: dict, session, modules: dict | None = None, options: dict | None = None,
              goal_set: dict | None = None, case_id: str | None = None, private_root: Path | None = None,
              evidence_dir: Path | None = None, evidence_artifact: str | None = None) -> dict:
    """Run the four stages once on one slot. ``out_dir`` must not exist (first attempt only)."""
    import shutil
    import tempfile
    if target not in TARGETS:
        raise ValueError('invalid target')
    case = None
    if goal_set is not None:
        case = goal_set['cases'].get(case_id)
        if case is None or case['target'] != target or variant != 'H':
            raise ValueError('held-out run must name one case of its target from its sealed manifest')
    elif variant not in ('A', 'B') or case_id is not None:
        raise ValueError('development runs use variants A or B')
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise FileExistsError('run directory exists; every attempt is kept and never replaced')
    out_dir.mkdir(parents=True, mode=0o700)
    private = Path(tempfile.mkdtemp(prefix='honua-arc-private-', dir=private_root))
    os.chmod(private, 0o700)
    modules = modules or default_modules(arc, target)
    install = arc['versionPair']['install']
    bound = {'imageDigest': candidate['imageDigest'], 'sourceSha': candidate['sourceRevision'],
             'platformLockDigest': candidate['platformLockDigest']}
    receipt = {'schema': TRIAL_SCHEMA, 'runId': run_id, 'attempt': 1, 'prereg': arc['prereg']['id'],
               'preregDigest': sha256(PREREG_PATH.read_bytes()), 'arcSha256': arc.get('_sha256'),
               'harnessDigests': harness_digests(), 'target': target, 'tier': arc['targets'][target]['tier'],
               'arm': arm['id'], 'armName': arm['name'], 'required': arm['required'], 'agent': arm['agent'],
               'modelId': arm['model'], 'family': arm['family'], 'variant': variant,
               'goalSet': {'name': (goal_set or {}).get('name', 'development'), 'set': (goal_set or {}).get('set'),
                           'manifestSha256': (goal_set or {}).get('sha256'),
                           'sealedSha256': (goal_set or {}).get('sealedSha256'), 'caseId': case_id,
                           'fixtures': (case or {}).get('fixtures'), 'fault': (case or {}).get('fault')},
               'temperature': None, 'temperaturePolicy': 'CLI default (not settable on subscription CLIs)',
               'hostKind': slot.kind, 'host': slot.vm.host if slot.vm else None,
               'phases': {'install': {'release': install['release'], 'sourceSha': install['sourceRevision'],
                                      'imageDigest': install['imageDigest']}, 'candidate': bound},
               'candidate': bound, 'exclusions': list(arc['exclusions']), 'opsScope': ops_scope(arc, target),
               'startedAt': now(), 'stages': []}
    outcomes, previous_answer = {}, None
    stage_defs = {s['id']: s for s in arc['stages']}
    run_deadline = time.monotonic() + arc['runLimits']['maxSeconds']
    try:
        for stage in STAGES:
            prerequisites = [d for d in stage_defs[stage]['dependsOn'] if outcomes.get(d) != 'pass']
            if prerequisites:
                first = prerequisites[0]
                receipt['stages'].append(_empty_stage(stage, 'blocked', f'prerequisite {first} was {outcomes.get(first)}',
                                                      blockedBy=first))
                outcomes[stage] = 'blocked'
                continue
            limits = arc['limits'][stage]
            deadline = min(run_deadline, time.monotonic() + limits['maxSeconds'])
            if deadline <= time.monotonic():
                receipt['stages'].append(_empty_stage(stage, 'fail', 'run wall-clock limit exhausted'))
                outcomes[stage] = 'fail'
                continue
            ctx = StageContext(arc=arc, target=target, arm=arm, variant=variant, run_dir=out_dir,
                               private_dir=private, slot=slot, candidate=candidate, install=install, limits=limits,
                               deadline=deadline, case=case, options=dict(options or {}))
            goal = (case['goals'][stage] if case else
                    arc['targets'][target]['goals'].get(stage, {}).get(variant, '(scenario requests)'))
            result = run_stage(ctx, modules[stage], session, goal, previous_answer)
            receipt['stages'].append(result)
            outcomes[stage] = result['outcome']
            answer_path = private / stage / 'final-answer.txt'
            if answer_path.is_file():
                previous_answer = answer_path.read_text(encoding='utf-8')
    finally:
        for close in reversed(slot.closers):
            try:
                close()
            except Exception:
                pass
        receipt['teardown'] = slot.vm.teardown() if slot.vm else {'status': 'clean', 'steps': []}
        # A runner-authored record exists for every run, so a run that never reached a session (for
        # example setup stage-unavailable) still has retained evidence and stays in the denominator.
        write_private(private / 'run-record.json', canonical(
            {'runId': run_id, 'stages': [{k: r.get(k) for k in ('stage', 'outcome', 'cause')} for r in receipt['stages']],
             'teardown': receipt['teardown']['status']}))
        evidence_dir = Path(evidence_dir or out_dir.parent / (out_dir.name + '.evidence'))
        export_private(private, evidence_dir, slot.harness_secrets)
        receipt['evidenceArtifact'] = {'name': evidence_artifact or evidence_dir.name,
                                       'files': [{'path': str(p.relative_to(evidence_dir)), 'sha256': sha256(p.read_bytes()),
                                                  'bytes': p.stat().st_size}
                                                 for p in sorted(evidence_dir.rglob('*')) if p.is_file()]}
        shutil.rmtree(private, ignore_errors=True)
    receipt['slotFacts'] = {k: v for k, v in slot.facts.items() if k in ('quickstart', 'vm', 'operator')}
    return finish_trial(receipt, out_dir)


EXPORT_SKIP = ('operator',)
MAX_RECEIPT_BYTES = 1_000_000


def export_private(private: Path, out_dir: Path, secrets) -> None:
    """Copy private evidence for upload, redacting every harness-held secret (raw, base64 and JSON
    escaped forms). A file whose redacted bytes still contain a secret is withheld."""
    import base64
    secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 8]
    forms = []
    for s in secrets:
        forms += [s.encode(), base64.b64encode(s.encode()), json.dumps(s)[1:-1].encode()]
    for path in sorted(Path(private).rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        if path.relative_to(private).parts[0] in EXPORT_SKIP:
            continue   # the installed operator release is recorded by tag and sha256, never uploaded
        raw = path.read_bytes()
        if path.suffix == '.json' and b'bodyBase64' in raw:
            try:
                doc = json.loads(raw)
                body = base64.b64decode(doc.get('bodyBase64') or b'')
                for form in forms:
                    body = body.replace(form, b'[redacted]')
                doc['bodyBase64'] = base64.b64encode(body).decode()
                raw = json.dumps(doc, sort_keys=True).encode()
            except (ValueError, TypeError):
                pass
        for form in forms:
            raw = raw.replace(form, b'[redacted]')
        target = out_dir / path.relative_to(private)
        if any(form in raw for form in forms):
            target = target.with_name(target.name + '.withheld')
            raw = b'withheld: credential could not be redacted\n'
        write_private(target, raw)


def finish_trial(receipt: dict, out_dir: Path) -> dict:
    stages = receipt['stages']
    reached = 0
    for row in stages:
        if row['outcome'] != 'pass':
            break
        reached += 1
    receipt['reachedStage'] = reached
    receipt['status'] = run_status(stages)
    first_bad = next((row for row in stages if row['outcome'] != 'pass'), None)
    receipt['arcCause'] = None if first_bad is None else {k: first_bad.get(k) for k in ('stage', 'outcome', 'cause')}
    receipt['unsafeAttempts'] = [u for row in stages for u in row.get('unsafe', [])]
    receipt['falseSuccess'] = any(row.get('falseSuccess') for row in stages)
    receipt['falseSuccessUnresolved'] = any(row.get('falseSuccessUnresolved') for row in stages)
    receipt['nudges'] = sum(int(row.get('nudges') or 0) for row in stages)
    receipt['teardownClean'] = receipt.get('teardown', {}).get('status') == 'clean'
    receipt['costUsd'] = None
    receipt['finishedAt'] = now()
    raw = json.dumps(receipt, indent=2, sort_keys=True).encode() + b'\n'
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('run receipt exceeds the 1 MB member bound the admission gate reads')
    write_private(out_dir / 'trial-receipt.json', raw)
    return receipt


# --------------------------------------------------------------------------- aggregate

def clopper_pearson(successes: int, trials: int) -> list[float]:
    """Two-sided exact 95% binomial interval (same construction as the #1152 corpus runner)."""
    if type(successes) is not int or type(trials) is not int or trials <= 0 or not 0 <= successes <= trials:
        raise ValueError('invalid binomial counts')

    def upper_tail(p, k):
        return sum(math.comb(trials, i) * p**i * (1 - p)**(trials - i) for i in range(k, trials + 1))

    def solve(k, target):
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if upper_tail(mid, k) < target:
                lo = mid
            else:
                hi = mid
        return round((lo + hi) / 2, 6)

    return [0.0 if successes == 0 else solve(successes, .025),
            1.0 if successes == trials else solve(successes + 1, .975)]


def verify_run(arc: dict, receipt: dict, *, target: str, arm_id: str, candidate: dict, digests: dict,
               row: dict, goal_sets: dict, evidence: Path | None) -> dict:
    """Re-derive a run's outcome from its retained evidence; reject anything contradictory or misbound."""
    if not isinstance(receipt, dict) or receipt.get('schema') != TRIAL_SCHEMA or receipt.get('attempt') != 1:
        raise ValueError('not a first-attempt arc run receipt')
    if receipt.get('runId') != row['runId'] or receipt.get('target') != target or receipt.get('arm') != arm_id:
        raise ValueError('run receipt belongs to another run, target or arm')
    if receipt.get('arcSha256') != arc.get('_sha256') or receipt.get('harnessDigests') != digests \
            or receipt.get('preregDigest') != sha256(PREREG_PATH.read_bytes()):
        raise ValueError('run receipt was produced by another harness, arc spec or prereg')
    goal = receipt.get('goalSet') or {}
    if receipt.get('variant') != row['variant'] or goal.get('caseId') != row['caseId'] or goal.get('set') != row['set']:
        raise ValueError('run receipt variant or case differs from its schedule row')
    if row['set'] is None:
        if goal.get('name') != 'development' or row['variant'] not in ('A', 'B') or goal.get('manifestSha256'):
            raise ValueError('development run carries held-out bindings')
    else:
        manifest = goal_sets.get(row['set'])
        case = (manifest or {}).get('cases', {}).get(row['caseId'])
        if manifest is None or case is None or case['target'] != target or goal.get('name') != 'held-out' \
                or goal.get('manifestSha256') != manifest['sha256'] or goal.get('sealedSha256') != manifest['sealedSha256'] \
                or goal.get('fixtures') != case['fixtures'] or goal.get('fault') != case['fault']:
            raise ValueError('held-out run is not bound to its sealed manifest case')
    install = arc['versionPair']['install']
    if receipt.get('phases', {}).get('install') != {'release': install['release'], 'sourceSha': install['sourceRevision'],
                                                    'imageDigest': install['imageDigest']}:
        raise ValueError('run receipt binds another install release')
    if receipt['phases'].get('candidate') != candidate or receipt.get('candidate') != candidate:
        raise ValueError('run receipt binds another candidate')
    if receipt.get('exclusions') != arc['exclusions'] or receipt.get('opsScope') != ops_scope(arc, target):
        raise ValueError('run receipt has other exclusions or another ops scope')
    if evidence is None or not Path(evidence).is_dir():
        raise ValueError('the run evidence is not retained')
    listed = (receipt.get('evidenceArtifact') or {}).get('files')
    if not isinstance(listed, list) or not listed:
        raise ValueError('run receipt lists no retained evidence')
    for item in listed:
        path = Path(evidence) / item['path']
        if not path.is_file() or sha256(path.read_bytes()) != item['sha256']:
            raise ValueError('retained evidence file is missing or altered: ' + str(item.get('path'))[:80])
    stages = receipt.get('stages')
    if [r.get('stage') for r in stages or []] != list(STAGES):
        raise ValueError('run receipt does not record every stage')
    unsafe, false_claims, unresolved = [], False, False
    for index, stage_row in enumerate(stages):
        outcome = stage_row.get('outcome')
        if outcome not in OUTCOMES:
            raise ValueError('unknown stage outcome')
        earlier = [prev['outcome'] for prev in stages[:index]]
        if outcome == 'blocked':
            blocker = stage_row.get('blockedBy')
            if blocker not in STAGES[:index] or earlier[STAGES.index(blocker)] == 'pass':
                raise ValueError('blocked stage without a non-passing prerequisite')
            if stage_row.get('actions') or stage_row.get('evidence'):
                raise ValueError('a blocked stage carries session evidence')
            continue
        if any(o != 'pass' for o in earlier):
            raise ValueError('a stage ran after an earlier stage did not pass')
        if outcome == 'stage-unavailable':
            if stage_row.get('actions') or stage_row.get('evidence'):
                raise ValueError('an unavailable stage carries session evidence')
            continue
        if outcome == 'fail' and not stage_row.get('evidence') and not stage_row.get('actions') and (
                stage_row.get('cause') == 'run wall-clock limit exhausted'
                or str(stage_row.get('cause', '')).startswith('stage wall-clock limit reached')):
            continue
        verify_stage_evidence(arc, target, stage_row, Path(evidence))
        if outcome == 'pass':
            if stage_row['stage'] == 'studio' and stage_row.get('assistantModel') != arc['studioAssistant']['model']:
                raise ValueError('studio passed without the required built-in assistant observed')
            if stage_row['stage'] == 'ops' and stage_row.get('opsScope') != {k: ops_scope(arc, target)[k]
                                                                           for k in ('mode', 'scored', 'unscored')}:
                raise ValueError('ops stage evidence does not list its scored and unscored assertions')
        unsafe.extend(stage_row.get('unsafe') or [])
        false_claims |= bool(stage_row.get('falseSuccess'))
        unresolved |= bool(stage_row.get('falseSuccessUnresolved'))
    status = run_status(stages)
    if receipt.get('status') != status or receipt.get('unsafeAttempts') != unsafe \
            or bool(receipt.get('falseSuccess')) != false_claims \
            or bool(receipt.get('falseSuccessUnresolved')) != unresolved:
        raise ValueError('run totals contradict its stage evidence')
    if status == 'completed' and not receipt.get('teardownClean'):
        raise ValueError('a completed run must also tear down cleanly')
    return {'status': status, 'unsafe': unsafe, 'falseSuccess': false_claims, 'unresolved': unresolved,
            'stagePass': {r['stage']: r['outcome'] == 'pass' for r in stages},
            'assistantModel': stages[3].get('assistantModel'),
            'studioRan': stages[3]['outcome'] not in ('blocked', 'stage-unavailable')}


MAX_VOIDS = 2


def check_adjudication(arc: dict, arm: dict, void: dict, original: dict | None) -> None:
    """One adjudication, as the campaign checks it before resuming and the aggregate checks it after."""
    family = void.get('reviewerFamily')
    if family not in FAMILIES or family == arm['family']:
        raise ValueError('void must be adjudicated by a reviewer of the other model family')
    if void.get('finding') != 'harness-fault' or not str(void.get('adjudication', '')).startswith('https://'):
        raise ValueError('void must cite an https adjudication finding a harness fault')
    if original is None:
        raise ValueError('the voided run receipt must be retained')
    if parse_time(void.get('adjudicatedAt')) < parse_time(original['finishedAt']):
        raise ValueError('void adjudicated before the voided run finished')


def verify_voids(arc: dict, arm: dict, voids: list[dict], scheduled: list[str], by_id: dict,
                 reserve_ids: set[str]) -> tuple[list[dict], list[str]]:
    """Each void: a retained original, a harness-fault finding by the other model family made before any
    later run started, and a replacement from the sealed reserve set that started afterwards. At most two."""
    if len(voids) > MAX_VOIDS:
        raise ValueError('more than two voids: the target fails under the prereg')
    rows, replacements, voided = [], [], set()
    for void in voids:
        run, reserve = void.get('runId'), void.get('replacementRunId')
        if run not in scheduled or run in voided:
            raise ValueError('void must name a scheduled run that is not already voided')
        check_adjudication(arc, arm, void, by_id.get(run))
        decided, finished = parse_time(void['adjudicatedAt']), parse_time(by_id[run]['finishedAt'])
        for other in by_id.values():
            started = parse_time(other['startedAt'])
            if other['runId'] != reserve and finished < started < decided:
                raise ValueError('a later run started before the void was adjudicated')
        if reserve == run or reserve in scheduled or reserve in replacements or reserve not in reserve_ids \
                or reserve not in by_id or parse_time(by_id[reserve]['startedAt']) < decided:
            raise ValueError('replacement must be a sealed reserve case run after the adjudication')
        voided.add(run)
        replacements.append(reserve)
        rows.append({'runId': run, 'adjudication': void['adjudication'], 'replacementRunId': reserve})
    return rows, replacements


def aggregate(*, arc: dict, target: str, arm_id: str, schedule_rows: list[dict], receipts: list[bytes],
              candidate: dict, evidence_for: Callable[[str], Path | None], voids: list[dict] | None = None,
              goal_sets: dict | None = None) -> tuple[dict, dict]:
    """(honua.ai-arc.aggregate/v1, companion report). Every count is re-derived from each counted run's
    retained evidence; a missing receipt counts as undetermined."""
    arm = arm_by_id(arc, arm_id)
    goal_sets = goal_sets or {}
    scheduled = [r['runId'] for r in schedule_rows]
    if not scheduled or len(set(scheduled)) != len(scheduled):
        raise ValueError('schedule is empty or duplicated')
    goal_name = 'held-out' if goal_sets.get('primary') else 'development'
    if goal_name == 'held-out':
        expected = schedule(arc, target, arm, goal_sets['primary'], len(scheduled))
        if expected != schedule_rows:
            raise ValueError('schedule is not the sealed primary manifest for this target and arm')
    elif any(r.get('set') is not None or r.get('variant') not in ('A', 'B') for r in schedule_rows):
        raise ValueError('a development schedule carries held-out rows')
    rows_by_id = {r['runId']: r for r in schedule_rows}
    for r in reserve_rows(target, arm, goal_sets.get('reserve')):
        rows_by_id.setdefault(r['runId'], r)
    if not SHA.fullmatch(candidate.get('sourceSha') or '') or not DIGEST.fullmatch(candidate.get('imageDigest') or '') \
            or not DIGEST.fullmatch(candidate.get('platformLockDigest') or ''):
        raise ValueError('candidate binding is incomplete')
    digests = harness_digests()
    by_id, raw_digest = {}, {}
    for raw in receipts:
        receipt = json.loads(raw)
        if receipt.get('runId') in by_id:
            raise ValueError('duplicate run receipt')
        if receipt.get('runId') not in rows_by_id:
            raise ValueError('unexpected run receipt: ' + str(receipt.get('runId')))
        by_id[receipt['runId']] = receipt
        raw_digest[receipt['runId']] = sha256(raw)
    reserve_ids = {r['runId'] for r in reserve_rows(target, arm, goal_sets.get('reserve'))}
    void_rows, replacements = verify_voids(arc, arm, list(voids or []), scheduled, by_id, reserve_ids)
    voided = {v['runId'] for v in void_rows}
    counted = [r for r in scheduled if r not in voided] + replacements
    if set(by_id) - set(counted) - voided:
        raise ValueError('unexpected run receipt: ' + sorted(set(by_id) - set(counted) - voided)[0])
    verified = {run: verify_run(arc, by_id[run], target=target, arm_id=arm_id, candidate=candidate, digests=digests,
                                row=rows_by_id[run], goal_sets=goal_sets, evidence=evidence_for(run))
                for run in counted if run in by_id}
    statuses = {'completed': 0, 'blocked': 0, 'failed': 0, 'undetermined': 0}
    stage_pass = {stage: 0 for stage in STAGES}
    unsafe = false_claims = unresolved = 0
    runs, cases = [], []
    for run in counted:
        runs.append({'runId': run, 'receiptDigest': raw_digest.get(run)})
        facts = verified.get(run)
        if facts is None:
            statuses['undetermined'] += 1
            cases.append({'runId': run, 'status': 'missing', 'reachedStage': 0})
            continue
        statuses[facts['status']] += 1
        for stage, passed in facts['stagePass'].items():
            stage_pass[stage] += passed
        unsafe += len(facts['unsafe'])
        false_claims += facts['falseSuccess']
        unresolved += facts['unresolved']
        receipt = by_id[run]
        cases.append({'runId': run, 'variant': receipt['variant'], 'caseId': receipt['goalSet'].get('caseId'),
                      'status': facts['status'], 'reachedStage': receipt['reachedStage'],
                      'arcCause': receipt.get('arcCause'),
                      'stages': [{k: r.get(k) for k in ('stage', 'outcome', 'cause')} for r in receipt['stages']],
                      'unsafeAttempts': facts['unsafe'], 'falseSuccess': facts['falseSuccess'],
                      'falseSuccessUnresolved': facts['unresolved'], 'host': receipt.get('host'),
                      'teardownClean': receipt.get('teardownClean')})
    n = len(counted)
    reasons = []
    if goal_name != 'held-out':
        reasons.append('development goals (A/B), not the sealed held-out set')
    if any(c['status'] in ('missing', 'undetermined') for c in cases):
        reasons.append('a counted run is missing or undetermined')
    stages = {stage: {'pass': stage_pass[stage], 'n': n} for stage in STAGES}
    ran = [verified[r] for r in counted if r in verified and verified[r]['studioRan']]
    observed = {facts['assistantModel'] for facts in ran}
    stages['studio']['assistant'] = observed.pop() if ran and len(observed) == 1 and None not in observed else None
    if stages['studio']['assistant'] is None:
        reasons.append('the Studio built-in assistant model was not observed identically in every run where it ran')
    present = [r['receiptDigest'] for r in runs if r['receiptDigest']]
    if len(set(present)) != len(present):
        raise ValueError('two counted runs share a receipt digest')
    if sum(statuses.values()) != len(scheduled) or n != len(scheduled):
        raise ValueError('aggregate counts do not add up to the schedule')
    result = {
        'schema': AGGREGATE_SCHEMA, 'prereg': arc['prereg']['id'], 'preregDigest': sha256(PREREG_PATH.read_bytes()),
        'candidate': dict(candidate), 'target': target, 'arm': arm_id, 'required': arm['required'],
        'scheduled': len(scheduled), 'completed': statuses['completed'], 'blocked': statuses['blocked'],
        'failed': statuses['failed'], 'undetermined': statuses['undetermined'],
        'voids': void_rows, 'unsafeAttempts': unsafe, 'falseSuccess': false_claims,
        'falseSuccessUnresolved': unresolved, 'stages': stages,
        'intervals': {'arc': clopper_pearson(statuses['completed'], n)},
        'exclusions': list(arc['exclusions']), 'runs': runs, 'harnessDigests': digests,
        'opsScope': ops_scope(arc, target),
    }
    if reasons:
        result['certifying'] = False
    reached = {str(k): 0 for k in range(len(STAGES) + 1)}
    for case in cases:
        reached[str(case.get('reachedStage', 0))] += 1
    report = {'schema': 'honua.ai-arc.report/v1', 'aggregateDigest': sha256(canonical(result)), 'target': target,
              'arm': arm, 'tier': arc['targets'][target]['tier'], 'goalSet': goal_name,
              'notCertifyingBecause': reasons,
              'bars': arc['targets'][target]['bars'] if arm['required'] else {'runs': arc['comparisonRuns'].get(target)},
              'denominator': arc['integrity']['denominator'],
              'stageIntervals': {stage: clopper_pearson(stage_pass[stage], n) for stage in STAGES},
              'reachedStageDistribution': reached, 'cases': cases,
              'note': 'Bars are acceptance rules, not reliability guarantees.'}
    return result, report


# --------------------------------------------------------------------------- CLI

def local_http(method: str, url: str, admin_key: str | None, timeout: int = 20) -> dict:
    """Controller read over the runner-owned tunnel end. Never follows redirects."""
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, method=method)
    if admin_key:
        request.add_header('X-API-Key', admin_key)
    try:
        with opener.open(request, timeout=timeout) as response:
            body, status = response.read(1_000_000), response.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(100_000), exc.code
    except (urllib.error.URLError, OSError):
        return {'status': None}
    try:
        document = json.loads(body)
    except ValueError:
        document = None
    return {'status': status, 'sha256': sha256(body), 'json': document}


def candidate_from_lock(lock_path: str, lock_digest: str, candidate_digest: str | None = None) -> dict:
    raw = Path(lock_path).read_bytes()
    if sha256(raw) != lock_digest:
        raise SystemExit('platform lock bytes differ from --lock-digest')
    component = json.loads(raw)['components']['honua-server']
    revision = (component.get('source') or {}).get('revision')
    digests = [a.get('digest') for a in component.get('artifacts', []) if a.get('kind') == 'image']
    digest = candidate_digest or (digests[0] if len(digests) == 1 else None)
    if digest not in digests or not SHA.fullmatch(revision or ''):
        raise SystemExit('platform lock does not bind one candidate image and source')
    return {'sourceRevision': revision, 'imageDigest': digest, 'platformLockDigest': lock_digest,
            'image': 'ghcr.io/honua-io/honua-server'}


def build_slot(target: str, args, run_index: int = 0) -> Slot:
    project = 'arc-' + uuid.uuid4().hex[:12]
    if target != 'local-docker':
        return Slot(target=target, kind='aws-cell', project=project)
    if not args.docker_host:
        return Slot(target=target, kind='none', project=project)
    match = re.fullmatch(r'ssh://([A-Za-z0-9][A-Za-z0-9_.-]{0,79})', args.docker_host)
    if not match:
        raise SystemExit('--docker-host must be ssh://<host alias> of an ephemeral VM')
    import ai_arc_local
    # #1160's shell runs ssh with HOME set beside the identity file, so it cannot read the controller's
    # SSH config: resolve the alias to its HostName here (the known_hosts entry is keyed by it).
    resolved = ai_arc_local.resolve_alias(match.group(1))
    vm = ai_arc_local.VmSlot(host=resolved, user=args.ssh_user, identity_file=args.ssh_identity,
                             known_hosts=args.known_hosts, project=project, index=args.slot_index)
    return Slot(target=target, kind='vm', project=project, vm=vm)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    inv = sub.add_parser('inventory', help='freeze the run schedule for one target x arm')
    inv.add_argument('--target', choices=TARGETS, required=True)
    inv.add_argument('--arm', required=True)
    inv.add_argument('--trials', type=int)
    inv.add_argument('--held-out', action='store_true', help='schedule the sealed primary manifest (key in env)')
    trial = sub.add_parser('trial', help='run one first-attempt arc run')
    trial.add_argument('--target', choices=TARGETS, required=True)
    trial.add_argument('--arm', required=True)
    trial.add_argument('--run-id', required=True)
    trial.add_argument('--variant', choices=('A', 'B', 'H'), required=True)
    trial.add_argument('--case-id')
    trial.add_argument('--goal-set', choices=('primary', 'reserve'), help='sealed manifest for a held-out case')
    trial.add_argument('--output', type=Path, required=True)
    trial.add_argument('--evidence-dir', type=Path, help='where redacted bulky evidence goes (its own artifact)')
    trial.add_argument('--evidence-artifact', help='name of the artifact that will carry --evidence-dir')
    trial.add_argument('--docker-host', help='ssh://<alias> of the ephemeral VM Docker host')
    trial.add_argument('--slot-index', type=int, default=0, help='0..2: up to three concurrent slots per host')
    trial.add_argument('--ssh-user', default='ubuntu')
    trial.add_argument('--ssh-identity', default=str(Path.home() / '.ssh/honua-eval'))
    trial.add_argument('--known-hosts', default=str(Path.home() / '.ssh/known_hosts'))
    trial.add_argument('--quickstart')
    trial.add_argument('--quickstart-revision')
    trial.add_argument('--slot-receipt')
    trial.add_argument('--slot-credentials')
    trial.add_argument('--deploy-identity-contract', help='honua-iac deploy_identity_contract export (absolute path)')
    trial.add_argument('--lock-digest', required=True)
    trial.add_argument('--lock-path', required=True)
    agg = sub.add_parser('aggregate', help='one receipt per target x arm')
    agg.add_argument('--target', choices=TARGETS, required=True)
    agg.add_argument('--arm', required=True)
    agg.add_argument('--schedule', type=Path, required=True)
    agg.add_argument('--trials-dir', type=Path, required=True)
    agg.add_argument('--evidence-root', type=Path, required=True, help='directory holding <run>/ evidence folders')
    agg.add_argument('--lock-digest', required=True)
    agg.add_argument('--lock-path', required=True)
    agg.add_argument('--voids', type=Path)
    agg.add_argument('--held-out', action='store_true', help='bind runs to the sealed primary and reserve manifests')
    agg.add_argument('--output', type=Path, required=True)
    agg.add_argument('--report', type=Path, required=True)
    sub.add_parser('check', help='validate the arc definition and its model-visible strings')
    args = parser.parse_args(argv)
    arc = load_arc()
    if args.command == 'check':
        hits = {name: forbidden_hits(text) for name, text in model_visible_texts(arc)}
        hits = {k: v for k, v in hits.items() if v}
        print(json.dumps({'arcSha256': arc['_sha256'], 'harnessDigests': harness_digests(), 'forbidden': hits}))
        return 1 if hits else 0
    if args.command == 'inventory':
        arm = arm_by_id(arc, args.arm)
        print(json.dumps(schedule(arc, args.target, arm, load_goal_set('primary') if args.held_out else None,
                                  args.trials), separators=(',', ':')))
        return 0
    if args.command == 'trial':
        arm = arm_by_id(arc, args.arm)
        candidate = candidate_from_lock(args.lock_path, args.lock_digest)
        if (args.variant == 'H') != bool(args.goal_set):
            raise SystemExit('a held-out run (variant H) names its sealed manifest with --goal-set primary|reserve')
        goal_set = load_goal_set(args.goal_set) if args.goal_set else None
        slot = build_slot(args.target, args)
        options = {'quickstart': args.quickstart, 'quickstart_revision': args.quickstart_revision,
                   'slot_receipt': args.slot_receipt, 'slot_credentials': args.slot_credentials,
                   'deploy_identity_contract': args.deploy_identity_contract, 'http': local_http}
        receipt = run_trial(arc=arc, target=args.target, arm=arm, variant=args.variant, run_id=args.run_id,
                            out_dir=args.output, slot=slot, candidate=candidate, session=ArcSession(arm),
                            options=options, goal_set=goal_set, case_id=args.case_id,
                            evidence_dir=args.evidence_dir, evidence_artifact=args.evidence_artifact)
        print(json.dumps({'runId': receipt['runId'], 'status': receipt['status'],
                          'reachedStage': receipt['reachedStage'], 'arcCause': receipt['arcCause']}))
        return 0
    candidate = candidate_from_lock(args.lock_path, args.lock_digest)
    bound = {'imageDigest': candidate['imageDigest'], 'sourceSha': candidate['sourceRevision'],
             'platformLockDigest': candidate['platformLockDigest']}
    rows = json.loads(args.schedule.read_text())
    receipts = [p.read_bytes() for p in sorted(args.trials_dir.rglob('trial-receipt.json'))]
    receipts = [raw for raw in receipts if json.loads(raw).get('target') == args.target
                and json.loads(raw).get('arm') == args.arm]
    voids = json.loads(args.voids.read_text()) if args.voids and args.voids.is_file() else []
    goal_sets = {'primary': load_goal_set('primary'), 'reserve': load_goal_set('reserve')} if args.held_out else {}
    result, report = aggregate(arc=arc, target=args.target, arm_id=args.arm, schedule_rows=rows, receipts=receipts,
                               candidate=bound, voids=voids, goal_sets=goal_sets,
                               evidence_for=lambda run: args.evidence_root / run.replace(':', '-'))
    for path, document in ((args.output, result), (args.report, report)):
        if path.exists():
            raise SystemExit(f'{path} exists; receipts are never replaced')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: result[k] for k in ('target', 'arm', 'scheduled', 'completed', 'blocked', 'failed',
                                             'undetermined', 'unsafeAttempts', 'falseSuccess',
                                             'falseSuccessUnresolved')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
