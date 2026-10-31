"""Scenario interface for the local goal-driven corpus runner (v1.1).

v1.0 definitions (Stack, Seeded, Observation, Scenario) are unchanged. v1.1 adds,
backward compatibly: RunRecord, RoleBinding, the Scenario base-class defaults,
the runner-owned controller recorder contract, runner-applied projections and
logical-role expectations. The runner owns every evidence byte: a scenario
names WHAT to read and HOW to project it; it never supplies observed values or
response bodies. See certification/agent-e2e/expectations/local/README.md.

Recorder (``stack.extra['recorder']``; runner-owned, tools/agent_e2e_local_recorder.py):
  recorder.json(step, method, path, body=None, *, principal='controller') -> answer
  recorder.upload(step, raw, *, table, schema, srid, principal='controller') -> answer
  recorder.mcp_call(step, tool, arguments, *, principal='controller') -> MCP result
  recorder.mcp_read(step, uri, *, principal='controller') -> MCP result
  recorder.replay(step, action, *, principal='controller') -> MCP result
  recorder.actions() -> tuple[LoggedAction, ...]   # digest-verified harness action log
  recorder.phase -> 'seed' | 'session' | 'observe'
Every call is retained once under its unique ``step`` (``[a-z0-9-]{1,80}``) with the
principal, method, path, HTTP status, raw body bytes and their sha256. ``answer`` is the
agent_e2e_setup_http answer shape ({'observation': {...,'body':...}, 'reference': ...}).
Principals: 'controller' (bootstrap admin), 'agent' (the model's own credential, for
probes), 'anonymous', 'approver' (when needs_approver()), 'other-tenant' when the stack
provides one. A scenario never receives a credential value.

Observation (v1.1 form): ``Observation(assertion_id, None, {'projection': 'extract/v1',
'spec': <spec>})``. The runner evaluates ``spec`` over its own recorded responses
(tools/agent_e2e_local_projections.py lists the operations) and scores the result
with corpus/oracle.verify against the expectation after logical-role substitution.
``observed`` must be None (or equal the runner's projection). Any other evidence form
fails closed.

Roles: expectations may contain ``{"$role": "<name>"}``. ``seeded:*`` roles bind to a
value in a seed-phase response; ``created:*`` roles bind to the single live item
enumerated after the session that is owned by ``RunRecord.agent_principal`` and/or
absent from a seed-phase enumeration. Unresolved, ambiguous or colliding roles fail.

Custody facts (``RunRecord.custody``) are recorded by the runner, which controls the
credentials; e.g. ``agentPrincipalId``, ``agentCredentialIds``, ``bootstrapPrincipalId``,
``bootstrapRevokedCredentialIds``, ``postRevocationProbeStatus``. Projections read them with
``{"custody": name}``.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

API_VERSION = '1.1'


@dataclass(frozen=True)
class Stack:
    """A running local-docker candidate stack, as set up by the runner."""
    base_url: str                 # e.g. http://127.0.0.1:8147
    admin_key: str                # controller credential; never shown to the model
    approver_key: str | None      # separate approver principal (for P14/P17)
    compose_project: str
    compose_file: str
    workdir: str                  # per-case scratch dir (controller side)
    candidate_digest: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Seeded:
    """What seed() created; passed back to observe()/cleanup(). Never shown to the model."""
    state: dict[str, Any]


@dataclass(frozen=True)
class Observation:
    assertion_id: str             # pack assertion id, e.g. "filter.exact"
    observed: Any                 # v1.1: None; the runner projects recorded responses
    evidence: dict[str, Any]      # v1.1: {"projection": "extract/v1", "spec": {...}}


@dataclass(frozen=True)
class RunRecord:
    """Controller-recorded session facts; hints are never evidence by themselves.

    A model-selected tool request may be replayed only from the harness action
    log after verifying its digest. Replay uses controller credentials against
    live state, and the response is checked against fixture-derived expectations.
    Evidence must record the replayed request digest. Any persisted artifact
    produced by the task must also be read and checked against live state.
    Model prose is never a source of replay arguments or observation evidence.
    """
    agent_principal: str
    sessions: int
    fault_events: tuple[dict, ...]
    resource_hints: tuple[dict, ...]
    final_answers: tuple[str, ...]
    # v1.1 additions (defaults keep v1.0 constructors working).
    custody: dict[str, Any] = field(default_factory=dict)
    session_ids: tuple[str, ...] = ()            # runner-minted id per fresh model context
    clarifications: tuple[dict, ...] = ()        # {topic, sentAfterAction, messageId} sent by the runner
    action_count: int = 0


@dataclass(frozen=True)
class RoleBinding:
    """How the runner resolves one logical role to a live id from recorded responses.

    seeded:<kind>:<name>: ``step`` is a seed-phase record and ``pointer`` must select
    exactly one scalar.
    created:<kind>[:<name>]: ``step`` is an observe-phase enumeration, ``items`` selects its
    rows and ``id`` the identity inside a row. At least one of ``owner`` (row pointer whose
    value must equal RunRecord.agent_principal) or ``before`` (a seed-phase enumeration of
    the same rows; ids present there are excluded) is required. Exactly one row must remain.
    """
    role: str
    step: str
    pointer: str | None = None
    items: str | None = None
    id: str | None = None
    owner: str | None = None
    before: str | None = None


class Scenario:
    id: str                       # "P10"
    supported_variants: tuple[str, ...]   # subset of ("A","B","C")
    def seed(self, stack: Stack, variant: str) -> Seeded: ...
    def fault_plan(self, variant: str) -> dict | None: return None   # e.g. P21: {"kind":"http_503_once","after":"map.created"}
    def needs_approver(self) -> bool: return False
    def sessions_required(self) -> int: return 1
    def agent_permissions(self, variant: str) -> list[str]: return ['admin:write']
    def roles(self, stack: Stack, seeded: Seeded, variant: str, run: RunRecord | None = None) -> list[RoleBinding]: return []
    def observe(self, stack: Stack, seeded: Seeded, variant: str, run: RunRecord | None = None) -> list[Observation]: ...
    def expected(self, variant: str) -> dict[str, Any]:   # assertion_id -> expected value for oracle.verify
        from agent_e2e_local_expectations import load_expected
        return load_expected(self.id, variant)
    def cleanup(self, stack: Stack, seeded: Seeded) -> None: return None
