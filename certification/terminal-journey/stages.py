#!/usr/bin/env python3
"""Deterministic stage implementations for the eight numbered journey stages.

Discipline enforced here, and asserted by `test_run.py`:

* Every stage is always materialized. There is no skip state.
* A stage that cannot execute yet returns `blocked` and names the missing
  dependency by ticket or PR URL. It can never return `pass`.
* A stage passes only when every one of its checks passed, so a `pass` always
  rests on real observations of the live candidate.
* Checks that *can* run still run inside a blocked stage. Observing that
  `honua_studio_create_draft` is present is useful evidence even while the write
  path it belongs to is blocked upstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from probes import Check, HttpResult, McpError, blocked

# ---------------------------------------------------------------------------
# Upstream dependency identities. These are the only things a stage may cite as
# a reason it cannot run.
# ---------------------------------------------------------------------------
SERVER = "https://github.com/honua-io/honua-server/issues"
RELEASE = "https://github.com/honua-io/honua-release/issues"

OPERATION_RUNTIME = f"{SERVER}/3411"          # single typed operation envelope/runtime
AUTH_SESSION = f"{SERVER}/3430"               # auth-before-tenant, session binding
SCOPE_NARROWING = f"{SERVER}/3431"            # bearer scope narrowing through approval replay
PROPOSAL_AUTHZ = f"{SERVER}/3474"             # proposal/resource authorization
EVIDENCE_POSTURE = f"{SERVER}/3475"           # source evidence freshness/completeness
ROLLBACK_TRUTH = f"{SERVER}/3301"             # rollback capability truth
ROSTER_EXPORTS = f"{SERVER}/3363"             # authoritative Admin roster exports
APPROVAL_COMMAND = f"{SERVER}/3599"            # separate-principal approval command
SETUP_VIEW = f"{SERVER}/3428"                 # bounded server-authored terminal setup view
REDIS_POSTURE = f"{SERVER}/3583"              # typed refusal when Redis is absent
INSTALLED_CLIENTS = f"{RELEASE}/7"            # installed-client execution engine
NO_REDIS_VARIANT = f"{RELEASE}/202"           # Redis-optional local install variant

# In-flight PRs that would unblock the corresponding contract.
UNBLOCKING_PR = {
    OPERATION_RUNTIME: "https://github.com/honua-io/honua-server/pull/3579",
    REDIS_POSTURE: "https://github.com/honua-io/honua-server/pull/3583",
    SETUP_VIEW: "https://github.com/honua-io/honua-server/pull/3591",
}

# Tool names each stage needs from the server-authored MCP surface.
STYLE_TOOLS = ("honua_get_style", "honua_apply_style_preset", "honua_render_map")
GP_TOOLS = ("honua_plan_analysis", "honua_execute_plan", "honua_cancel_job")
STUDIO_DRAFT_TOOLS = (
    "honua_studio_create_draft",
    "honua_studio_update_draft",
    "honua_studio_validate_draft",
)
STUDIO_COMPOSITION_TOOLS = (
    "honua_studio_add_layer",
    "honua_studio_set_layer_style",
    "honua_studio_set_view",
    "honua_studio_add_widget",
    "honua_studio_add_control",
)
PUBLICATION_TOOLS = ("honua_studio_propose_publication",)


@dataclass
class Observation:
    """Everything the live probes learned once, shared across stages."""

    base_url: str | None = None
    ready: bool = False
    readiness_detail: str = ""
    capability_manifest: dict[str, Any] | None = None
    anonymous_admin_status: int | None = None
    anonymous_api_keys_status: int | None = None
    tool_names: tuple[str, ...] = ()
    tools_error: str | None = None
    proxy_available: bool = False
    proxy_detail: str = ""
    proxy_note: str | None = None
    image_ref: str | None = None
    expected_revision: str | None = None
    setup_view_present: bool = False


@dataclass
class StageResult:
    number: int
    stage: str
    command: str
    status: str
    blocked_by: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    operation_id: str | None = None
    policy_decision_id: str | None = None
    approval_id: str | None = None
    actuator_id: str | None = None
    verification_id: str | None = None

    @property
    def first_failure(self) -> Check | None:
        return next((c for c in self.checks if c.status == "fail"), None)


def _resolve(checks: list[Check], number: int, stage_id: str, command: str) -> StageResult:
    """Derive the stage outcome from its checks. fail > blocked > pass."""
    status = "pass"
    if any(c.status == "fail" for c in checks):
        status = "fail"
    elif any(c.status == "blocked" for c in checks):
        status = "blocked"
    blockers: list[str] = []
    for check in checks:
        if check.status == "blocked":
            blockers.extend(check.blocked_by)
    return StageResult(
        number=number,
        stage=stage_id,
        command=command,
        status=status,
        blocked_by=list(dict.fromkeys(blockers)),
        checks=checks,
    )


def _tool_presence(observation: Observation, names: tuple[str, ...], check_id: str) -> Check:
    """Prove the server publishes the named tools. Discovery only, no authority."""
    invocation = f"mcp tools/list ∋ {', '.join(names)}"
    if observation.tools_error is not None:
        return blocked(
            check_id,
            "mcp-tool",
            invocation,
            f"the candidate tool surface could not be enumerated: {observation.tools_error}",
            [INSTALLED_CLIENTS],
        )
    missing = [name for name in names if name not in observation.tool_names]
    if missing:
        return Check(
            check_id,
            "mcp-tool",
            invocation,
            "fail",
            f"the candidate does not publish {missing}",
        )
    return Check(
        check_id,
        "mcp-tool",
        invocation,
        "pass",
        f"all {len(names)} tools present in the {len(observation.tool_names)}-tool candidate surface",
    )


# ---------------------------------------------------------------------------
# Stage 1 — installed client handoff
# ---------------------------------------------------------------------------
def stage_1(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    checks: list[Check] = []

    for blocker in workspace_blockers(1):
        checks.append(
            blocked(
                "1.0-pinned-clients",
                "artifact",
                "consume exact clientArtifacts pins (honua-release#136)",
                blocker,
                [INSTALLED_CLIENTS],
            )
        )

    if observation.image_ref:
        checks.append(
            Check(
                "1.1-candidate-image",
                "compose",
                "docker compose up -d --wait (server image pinned from platform-manifest.yaml)",
                "pass",
                f"stack running the pinned candidate image {observation.image_ref}",
            )
        )
    else:
        checks.append(
            blocked(
                "1.1-candidate-image",
                "compose",
                "docker compose up -d --wait",
                "the pinned candidate stack was not brought up",
                [INSTALLED_CLIENTS],
            )
        )

    checks.append(
        Check(
            "1.2-readiness",
            "http",
            "GET /healthz/ready",
            "pass" if observation.ready else "fail",
            observation.readiness_detail,
        )
    )

    manifest = observation.capability_manifest
    if manifest is None:
        checks.append(
            Check(
                "1.3-candidate-identity",
                "http",
                "GET /api/v1/capabilities/manifest",
                "fail",
                "the anonymous capability manifest did not return server identity",
            )
        )
    else:
        server = manifest.get("server") or manifest.get("Server") or {}
        revision = server.get("deploymentRevision") or server.get("DeploymentRevision")
        identity_matches = bool(revision) and revision == observation.expected_revision
        checks.append(
            Check(
                "1.3-candidate-identity",
                "http",
                "GET /api/v1/capabilities/manifest",
                "pass" if identity_matches else "fail",
                f"candidate identity reported: revision={revision!r} "
                f"source={server.get('deploymentRevisionSource') or server.get('DeploymentRevisionSource')!r}; "
                f"expected manifest revision={observation.expected_revision!r}",
            )
        )

    if observation.anonymous_admin_status is None:
        checks.append(
            Check(
                "1.4-auth-enforced",
                "http",
                "GET /api/v1/admin/version without credentials",
                "fail",
                "the admin endpoint could not be probed",
            )
        )
    elif observation.anonymous_admin_status in (401, 403):
        checks.append(
            Check(
                "1.4-auth-enforced",
                "http",
                "GET /api/v1/admin/version without credentials",
                "pass",
                f"anonymous admin access refused with HTTP {observation.anonymous_admin_status} "
                f"(authentication precedes tenant resolution, {AUTH_SESSION})",
            )
        )
    else:
        checks.append(
            Check(
                "1.4-auth-enforced",
                "http",
                "GET /api/v1/admin/version without credentials",
                "fail",
                f"anonymous admin access returned HTTP {observation.anonymous_admin_status}; "
                "authentication is not enforced ahead of the admin surface",
            )
        )

    if observation.proxy_available and observation.tools_error is None:
        checks.append(
            Check(
                "1.5-proxy-tools-list",
                "mcp-tool",
                "honua-mcp-proxy → initialize + paginated tools/list",
                "pass",
                f"pinned proxy enumerated {len(observation.tool_names)} tools "
                "(paginated independently of any expected count)",
            )
        )
    else:
        checks.append(
            blocked(
                "1.5-proxy-tools-list",
                "mcp-tool",
                "honua-mcp-proxy → initialize + paginated tools/list",
                observation.tools_error or observation.proxy_detail or "the pinned proxy was unavailable",
                [INSTALLED_CLIENTS],
            )
        )

    if observation.setup_view_present:
        checks.append(
            Check(
                "1.6-bounded-setup-view",
                "mcp-tool",
                "server-authored bounded setup/profile tool view",
                "pass",
                "the candidate negotiates a bounded server-authored setup view",
            )
        )
    else:
        checks.append(
            blocked(
                "1.6-bounded-setup-view",
                "mcp-tool",
                "server-authored bounded setup/profile tool view",
                "the candidate exposes no named server-authored setup view, so the "
                "bounded profile/tool view required by stage 1 cannot be verified",
                [SETUP_VIEW],
            )
        )
    return checks


# ---------------------------------------------------------------------------
# Stages 2, 3 and 8 — the `honua admin` command surface
# ---------------------------------------------------------------------------
def _admin_cli_check(check_id: str, invocation: str, workspace_blockers: Callable[[int], list[str]], number: int) -> Check:
    reasons = workspace_blockers(number)
    reason = reasons[0] if reasons else (
        "the pinned clientArtifacts ship no `honua admin` command surface"
    )
    return blocked(check_id, "cli", invocation, reason, [INSTALLED_CLIENTS])


def stage_2(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    checks = [
        _admin_cli_check(
            "2.1-admin-cli",
            "honua admin apiKeys list; honua admin apiKeys effective-permissions",
            workspace_blockers,
            2,
        )
    ]
    # The server-side contract is separately observable, and worth recording: it
    # shows the blocker is the client surface, not the server.
    api_key_status = observation.anonymous_api_keys_status
    checks.append(
        Check(
            "2.2-admin-endpoint-present",
            "http",
            "GET /api/v1/admin/api-keys/ (credentialed surface exists on the candidate)",
            "pass" if api_key_status in (401, 403) else "fail",
            (
                f"the candidate serves the authenticated Admin API-key surface (HTTP {api_key_status})"
                if api_key_status in (401, 403)
                else f"the Admin API-key endpoint returned HTTP {api_key_status}"
            ),
        )
    )
    return checks


def stage_3(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    return [
        _admin_cli_check(
            "3.1-admin-cli",
            "honua admin connection create/test; import; publish/configure layer; access policy",
            workspace_blockers,
            3,
        ),
        blocked(
            "3.2-operation-envelope",
            "mcp-tool",
            "unified typed operation envelope for every mutating step",
            "the candidate has no single durable actuation spine emitting "
            "operation/policy/actuator/verification identities, so a write cannot be "
            "certified as entering the canonical runtime",
            [OPERATION_RUNTIME],
        ),
    ]


# ---------------------------------------------------------------------------
# Stages 4-7 — server tool surface plus the write spine
# ---------------------------------------------------------------------------
def stage_4(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    return [
        _tool_presence(observation, STYLE_TOOLS, "4.1-style-tools-present"),
        blocked(
            "4.2-published-layer",
            "mcp-tool",
            "honua_get_style → honua_apply_style_preset → honua_render_map on a published layer",
            "stage 3 could not publish a layer, so the canonical published-layer style "
            "path has no subject; the style mutation additionally needs the typed "
            "operation envelope",
            [OPERATION_RUNTIME],
        ),
        blocked(
            "4.3-decoded-png",
            "artifact",
            "decode the rendered PNG and prove the style change is visible",
            "no render artifact exists while the style path is blocked",
            [OPERATION_RUNTIME],
        ),
    ]


def stage_5(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    return [
        _tool_presence(observation, GP_TOOLS, "5.1-gp-tools-present"),
        blocked(
            "5.2-bounded-buffer-job",
            "mcp-tool",
            "discover and run bounded geometry.buffer; wait/cancel via the canonical job state machine",
            "the local target composes PostGIS and Honua Server only. Without Redis the "
            "job runner refuses submission, and the typed refusal contract that would "
            "make that refusal certifiable is not on the candidate",
            [REDIS_POSTURE, NO_REDIS_VARIANT, OPERATION_RUNTIME],
        ),
    ]


def stage_6(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    return [
        _tool_presence(observation, STUDIO_DRAFT_TOOLS, "6.1-studio-draft-tools-present"),
        _tool_presence(observation, STUDIO_COMPOSITION_TOOLS, "6.2-studio-composition-tools-present"),
        blocked(
            "6.3-immutable-versions",
            "mcp-tool",
            "create/mutate/validate/version map+dashboard; restart; reopen; compare content identity",
            "saving an immutable version is a mutation and must enter the canonical "
            "operation runtime; the restart/reopen identity comparison additionally "
            "needs the source-evidence posture that marks a read complete and current",
            [OPERATION_RUNTIME, EVIDENCE_POSTURE],
        ),
    ]


def stage_7(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    return [
        _tool_presence(observation, PUBLICATION_TOOLS, "7.1-proposal-tool-present"),
        blocked(
            "7.2-durable-awaiting-approval",
            "mcp-tool",
            "submit publication; require a durable AwaitingApproval proposal bound to item/version/content hash; poll it",
            "proposal and resource authorization (tenant, owner, scope, nondisclosure) "
            "is not on the candidate, and the local target has no Redis-backed control "
            "plane to make the proposal durable",
            [PROPOSAL_AUTHZ, REDIS_POSTURE, OPERATION_RUNTIME],
        ),
    ]


def stage_8(observation: Observation, workspace_blockers: Callable[[int], list[str]]) -> list[Check]:
    return [
        blocked(
            "8.1-admin-cli",
            "cli",
            "honua admin operate approveOperationProposal --path id=<proposal-id> --profile approver --yes",
            "the installed separate-principal approval command is not available on the candidate",
            [APPROVAL_COMMAND],
        ),
        blocked(
            "8.2-separate-principal",
            "cli",
            "approve from a separate human principal; proposer self-approval must be denied; poll to published URL",
            "there is no durable proposal to approve, and approved replay must "
            "revalidate the proposer's current authority under narrowed bearer scopes",
            [APPROVAL_COMMAND, PROPOSAL_AUTHZ, SCOPE_NARROWING],
        ),
    ]


STAGE_IMPLEMENTATIONS: dict[int, Callable[[Observation, Callable[[int], list[str]]], list[Check]]] = {
    1: stage_1,
    2: stage_2,
    3: stage_3,
    4: stage_4,
    5: stage_5,
    6: stage_6,
    7: stage_7,
    8: stage_8,
}


def run_stages(
    journey: dict[str, Any],
    observation: Observation,
    workspace_blockers: Callable[[int], list[str]],
) -> list[StageResult]:
    """Execute every numbered stage in order. Always returns all eight."""
    results = []
    for stage in journey["stages"]:
        number = int(stage["number"])
        implementation = STAGE_IMPLEMENTATIONS.get(number)
        if implementation is None:  # pragma: no cover - journey contract drift
            checks = [
                blocked(
                    f"{number}.0-unimplemented",
                    "artifact",
                    stage["command"],
                    "no deterministic implementation is registered for this numbered stage",
                    list(stage.get("blockedBy") or []),
                )
            ]
        else:
            checks = implementation(observation, workspace_blockers)
        result = _resolve(checks, number, stage["id"], stage["command"])
        # A stage never loses a blocker the contract already knew about.
        if result.status == "blocked":
            result.blocked_by = list(dict.fromkeys(result.blocked_by))
        results.append(result)
    return results


def unblocking_prs(blockers: list[str]) -> dict[str, str]:
    """Map cited blockers to the in-flight PR that would close them."""
    return {b: UNBLOCKING_PR[b] for b in blockers if b in UNBLOCKING_PR}
