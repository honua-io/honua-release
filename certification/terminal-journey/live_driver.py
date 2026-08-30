#!/usr/bin/env python3
"""`terminal-journey-driver-v1` adapter — the live action surface honua-release#161 calls.

Contract: `certification/terminal-model-canary/driver-protocol.v1.json`.
Transport: exactly one JSON request on stdin, exactly one JSON response on stdout
per invocation. State between invocations lives in a workspace directory keyed by
`workspaceId`; nothing is held in memory across calls.

This adapter is deliberately honest about what the candidate can do:

* It really brings the pinned candidate stack up, really consumes the exact #136
  client artifacts, and really enumerates the server-authored tool view through
  the pinned `honua-mcp-proxy`.
* Every operation that would require a contract the candidate does not implement
  returns `blocked` and names the missing dependency. No operation can return
  `pass` from mocked, replayed or assumed state, which is the protocol's fourth
  prohibition.
* `execute` refuses any action outside the server-authored bounded tool view.
  Because that bounded view does not exist on the candidate yet, `execute` is
  blocked for every action rather than silently falling back to the full catalog.
* Credential values never enter a response. Only environment-variable references
  are returned, per the protocol's first prohibition.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import pins  # noqa: E402
import probes  # noqa: E402
import stages as stagelib  # noqa: E402

PROTOCOL = "terminal-journey-driver-v1"
DEFAULT_TARGET = HERE / "targets" / "local-docker.json"
STATE_ROOT = Path.cwd() / ".terminal-journey" / "sessions"

# Blockers that stop a live model run before any action can be attempted.
EXECUTE_BLOCKERS = [stagelib.SETUP_VIEW, stagelib.OPERATION_RUNTIME]
APPROVE_BLOCKERS = [stagelib.PROPOSAL_AUTHZ, stagelib.SCOPE_NARROWING]


class DriverError(RuntimeError):
    pass


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text())


def _state_path(workspace_id: str) -> Path:
    if not workspace_id or "/" in workspace_id or ".." in workspace_id:
        raise DriverError("invalid workspaceId")
    return STATE_ROOT / f"{workspace_id}.json"


def _read_state(workspace_id: str) -> dict[str, Any]:
    path = _state_path(workspace_id)
    if not path.is_file():
        raise DriverError(f"unknown workspaceId {workspace_id!r}; call setup first")
    return json.loads(path.read_text())


def _write_state(workspace_id: str, state: dict[str, Any]) -> None:
    path = _state_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def _compose_for(target: dict[str, Any], manifest: dict[str, Any]) -> tuple[probes.Compose, str, str]:
    compose_cfg = target["compose"]
    server = manifest["components"]["honua-server"]
    image_ref = f"{server['image']}@{server['digest']}"
    port = int(compose_cfg["port"])
    base_url = f"http://127.0.0.1:{port}"
    compose = probes.Compose(
        compose_file=str(ROOT / compose_cfg["file"]),
        project=compose_cfg["project"],
        env={
            compose_cfg["imageEnv"]: image_ref,
            compose_cfg["portEnv"]: str(port),
            target["adminPassword"]["env"]: probes.resolve_env_default(
                target["adminPassword"]["env"], target["adminPassword"]["default"]
            ),
        },
    )
    return compose, base_url, image_ref


def _observation_payload(observation: stagelib.Observation) -> dict[str, Any]:
    """Server-authored state only. Never any credential material."""
    manifest = observation.capability_manifest or {}
    server = manifest.get("server") or manifest.get("Server") or {}
    return {
        "ready": observation.ready,
        "readinessDetail": observation.readiness_detail,
        "candidateIdentity": {
            "serverVersion": server.get("serverVersion") or server.get("ServerVersion"),
            "deploymentRevision": server.get("deploymentRevision") or server.get("DeploymentRevision"),
            "deploymentRevisionSource": (
                server.get("deploymentRevisionSource") or server.get("DeploymentRevisionSource")
            ),
        },
        "anonymousAdminStatus": observation.anonymous_admin_status,
        "proxyConnected": observation.proxy_available,
        "toolCount": len(observation.tool_names),
    }


def _tool_view(observation: stagelib.Observation) -> dict[str, Any]:
    """The bounded server-authored view, or an explicit statement that none exists.

    Returning the full catalog as if it were a bounded setup view would be a lie
    the model could act on, so `bounded` stays false and `tools` stays empty until
    the candidate negotiates a real view.
    """
    return {
        "bounded": observation.setup_view_present,
        "viewId": None,
        "tools": [],
        "catalogToolCount": len(observation.tool_names),
        "blockedBy": [] if observation.setup_view_present else [stagelib.SETUP_VIEW],
        "detail": (
            "the candidate negotiates a bounded server-authored setup view"
            if observation.setup_view_present
            else "the candidate exposes no named server-authored setup view; the full "
            "catalog is deliberately withheld rather than presented as a bounded view"
        ),
    }


def _stage_status(journey: dict[str, Any], observation: stagelib.Observation, workspace: pins.ClientWorkspace, stage_ref: Any) -> dict[str, Any]:
    def workspace_blockers(number: int) -> list[str]:
        if workspace.status != "pass":
            return [workspace.reason or "pinned client artifacts were not consumed"]
        return workspace.missing_for_stage(number)

    results = stagelib.run_stages(journey, observation, workspace_blockers)
    selected = None
    for result in results:
        if stage_ref in (result.number, result.stage):
            selected = result
            break
    if selected is None:
        raise DriverError(f"unknown journey stage {stage_ref!r}")
    return {
        "number": selected.number,
        "id": selected.stage,
        "command": selected.command,
        "status": selected.status,
        "blockedBy": selected.blocked_by,
        "checks": [c.as_receipt() for c in selected.checks],
    }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def op_setup(request: dict[str, Any]) -> dict[str, Any]:
    target_path = Path(request.get("target") or DEFAULT_TARGET)
    target = json.loads(target_path.read_text())
    manifest = _load_yaml(ROOT / "platform-manifest.yaml")

    workspace_id = f"tj-{uuid.uuid4().hex[:12]}"
    workdir = STATE_ROOT.parent / workspace_id
    workspace = pins.resolve_client_workspace(manifest, workdir / "clients")

    bindir = None
    install_notes: list[str] = []
    if workspace.status == "pass":
        bindir, _detail, install_notes = pins.install_executables(workspace, workdir / "install")
        workspace.install_notes = install_notes

    compose, base_url, image_ref = _compose_for(target, manifest)
    up = compose.up()
    stack_up = up.returncode == 0

    server_sha = manifest["components"]["honua-server"]["sha"]
    observation = stagelib.Observation(
        base_url=base_url,
        image_ref=image_ref if stack_up else None,
        expected_revision=server_sha,
    )
    if stack_up:
        import run as driver  # noqa: PLC0415 - shared observation logic, one implementation

        observation = driver.observe(target, base_url, workspace, bindir, image_ref, server_sha)

    state = {
        "workspaceId": workspace_id,
        "targetPath": str(target_path),
        "workdir": str(workdir),
        "baseUrl": base_url,
        "stackUp": stack_up,
        "armedError": None,
        "clientWorkspace": workspace.as_receipt(),
    }
    _write_state(workspace_id, state)

    blockers: list[str] = []
    if not stack_up:
        blockers.append(stagelib.INSTALLED_CLIENTS)
    if workspace.status != "pass" or bindir is None:
        blockers.append(stagelib.INSTALLED_CLIENTS)
    if not observation.setup_view_present:
        blockers.append(stagelib.SETUP_VIEW)

    if blockers and stack_up:
        compose.down()
        stack_up = False
        state["stackUp"] = False
        _write_state(workspace_id, state)

    return {
        "status": "blocked" if blockers else "ready",
        "workspaceId": workspace_id,
        "observation": _observation_payload(observation),
        "toolView": _tool_view(observation),
        # Protocol prohibition 1: references only, never values.
        "credentialReferences": [
            {
                "id": "installer-bootstrap-admin",
                "envVar": target["adminPassword"]["env"],
                "principal": "installer-provisioned admin",
                "note": "resolved from the environment at call time; never serialized",
            }
        ],
        "blockedBy": list(dict.fromkeys(blockers)),
        "clientWorkspace": workspace.as_receipt(),
        "notices": install_notes,
    }


def _rehydrate(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], stagelib.Observation, pins.ClientWorkspace]:
    state = _read_state(str(request.get("workspaceId", "")))
    target = json.loads(Path(state["targetPath"]).read_text())
    manifest = _load_yaml(ROOT / "platform-manifest.yaml")
    workdir = Path(state["workdir"])
    workspace = pins.ClientWorkspace.from_receipt(state["clientWorkspace"], workdir / "clients")
    bindir = workdir / "install" / "node_modules" / ".bin"
    import run as driver  # noqa: PLC0415

    server = manifest["components"]["honua-server"]
    image_ref = f"{server['image']}@{server['digest']}" if state.get("stackUp") else None
    observation = driver.observe(
        target,
        state["baseUrl"],
        workspace,
        bindir if bindir.is_dir() else None,
        image_ref,
        server["sha"],
    )
    return state, target, manifest, observation, workspace


def op_observe(request: dict[str, Any]) -> dict[str, Any]:
    state, _target, _manifest, observation, workspace = _rehydrate(request)
    journey = json.loads((HERE / "journey.v1.json").read_text())
    stage_ref = request.get("stage") or request.get("stageId") or request.get("stageNumber")
    status = _stage_status(journey, observation, workspace, stage_ref)
    return {
        "status": "blocked" if status["status"] != "pass" else "pass",
        "stageStatus": status,
        "observation": _observation_payload(observation),
        "toolView": _tool_view(observation),
        "blockedBy": status["blockedBy"],
    }


def op_execute(request: dict[str, Any]) -> dict[str, Any]:
    """Execute exactly the model-selected action — but only from a bounded view.

    Protocol prohibition 2 requires rejecting anything outside the server-authored
    bounded tool view. The candidate publishes no such view, so every action is
    outside it and nothing may execute. Falling back to the full catalog here would
    hand a model authority the server never granted.
    """
    state, _target, _manifest, observation, workspace = _rehydrate(request)
    journey = json.loads((HERE / "journey.v1.json").read_text())
    stage_ref = request.get("stage") or request.get("stageId") or request.get("stageNumber")
    status = _stage_status(journey, observation, workspace, stage_ref)
    action = request.get("action") or {}
    view = _tool_view(observation)

    return {
        "status": "blocked",
        "stageStatus": status,
        "result": {
            "accepted": False,
            "reason": (
                "the requested action is outside the server-authored bounded tool view; "
                "no bounded view is published by the candidate, and the full catalog is "
                "not a substitute for one"
            ),
            "requested": {
                "kind": action.get("kind"),
                "name": action.get("name"),
            },
            "boundedView": view,
            "injectedError": None,
            "recoveredError": None,
        },
        # No mutation entered a durable spine, so there are no identities to report.
        "canonicalIds": {
            "operationId": None,
            "operationInstanceId": None,
            "proposalId": None,
            "jobId": None,
            "correlationId": None,
            "auditId": None,
        },
        "blockedBy": list(dict.fromkeys(EXECUTE_BLOCKERS + status["blockedBy"])),
    }


def op_inject_error(request: dict[str, Any]) -> dict[str, Any]:
    state = _read_state(str(request.get("workspaceId", "")))
    error_id = f"err-{uuid.uuid4().hex[:12]}"
    state["armedError"] = error_id
    _write_state(state["workspaceId"], state)
    return {
        "status": "blocked",
        "errorId": error_id,
        "recoverable": True,
        "detail": (
            "an error identity is reserved, but it cannot be armed against a real "
            "action while execute is blocked; arming it against a mocked action "
            "would make the recovery evidence fictional"
        ),
        "blockedBy": list(EXECUTE_BLOCKERS),
    }


def op_approve(request: dict[str, Any]) -> dict[str, Any]:
    _state, target, _manifest, _observation, _workspace = _rehydrate(request)
    return {
        "status": "blocked",
        "principalProfile": "approver",
        "proposalId": request.get("proposalId"),
        "approvalId": None,
        # The separation rule is stated, not exercised: no proposal exists to approve.
        "proposerSelfApproval": "denied-untested",
        "detail": (
            "no durable AwaitingApproval proposal exists on the candidate: proposal and "
            "resource authorization is not implemented, and the local target composes no "
            "Redis-backed control plane to make a proposal durable"
        ),
        "blockedBy": list(dict.fromkeys(APPROVE_BLOCKERS + [stagelib.REDIS_POSTURE])),
    }


def op_verify(request: dict[str, Any]) -> dict[str, Any]:
    _state, _target, _manifest, observation, _workspace = _rehydrate(request)
    not_run = {"status": "blocked", "detail": "the journey did not reach a published artifact"}
    return {
        "status": "blocked",
        "assertions": {
            "finalUrl": not_run,
            "pixelProof": not_run,
            "canonicalIdJoin": not_run,
            "tenantIsolation": not_run,
            "rbacDenial": not_run,
            "proposerApproverSeparation": not_run,
            "currentAuthorityRevalidation": not_run,
        },
        "finalUrlProof": None,
        "pixelProof": None,
        "canonicalIds": {
            "operationId": None,
            "operationInstanceId": None,
            "proposalId": None,
            "jobId": None,
            "correlationId": None,
            "auditId": None,
        },
        "blockedBy": list(dict.fromkeys(EXECUTE_BLOCKERS + APPROVE_BLOCKERS)),
    }


def op_teardown(request: dict[str, Any]) -> dict[str, Any]:
    workspace_id = str(request.get("workspaceId", ""))
    state = _read_state(workspace_id)
    target = json.loads(Path(state["targetPath"]).read_text())
    manifest = _load_yaml(ROOT / "platform-manifest.yaml")
    compose, _base_url, _image = _compose_for(target, manifest)
    result = compose.down()
    _state_path(workspace_id).unlink(missing_ok=True)
    return {
        "status": "pass" if result.returncode == 0 else "fail",
        "workspaceId": workspace_id,
        "detail": (
            "isolated namespace removed"
            if result.returncode == 0
            else (result.stderr or result.stdout).strip()[:300]
        ),
    }


OPERATIONS = {
    "setup": op_setup,
    "observe": op_observe,
    "execute": op_execute,
    "inject_error": op_inject_error,
    "approve": op_approve,
    "verify": op_verify,
    "teardown": op_teardown,
}


def handle(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("protocol") != PROTOCOL:
        return {
            "status": "fail",
            "error": f"unsupported protocol {request.get('protocol')!r}; this adapter speaks {PROTOCOL}",
        }
    operation = request.get("operation")
    handler = OPERATIONS.get(str(operation))
    if handler is None:
        return {"status": "fail", "error": f"unsupported operation {operation!r}"}
    try:
        response = handler(request)
    except DriverError as exc:
        return {"status": "fail", "operation": operation, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a driver crash must never read as pass
        return {"status": "fail", "operation": operation, "error": f"{type(exc).__name__}: {exc}"}
    response.setdefault("protocol", PROTOCOL)
    response.setdefault("operation", operation)
    return response


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        json.dump({"status": "fail", "error": f"invalid JSON request: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    response = handle(request if isinstance(request, dict) else {})
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    return 0 if response.get("status") != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())
