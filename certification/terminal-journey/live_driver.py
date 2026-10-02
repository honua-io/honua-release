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
  Verified discovery does not implement action execution or grant call authority.
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
import executor  # noqa: E402
from transport import ExecutionError, Transport  # noqa: E402

PROTOCOL = "terminal-journey-driver-v1"
DEFAULT_TARGET = HERE / "targets" / "local-docker.json"
STATE_ROOT = Path.cwd() / ".terminal-journey" / "sessions"

# Blockers that stop a live model run before any action can be attempted.
EXECUTE_BLOCKERS = [stagelib.JOURNEY_DRIVER]
APPROVE_BLOCKERS = [stagelib.PROPOSAL_AUTHZ, stagelib.SCOPE_NARROWING]


class DriverError(RuntimeError):
    pass


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text())


def _state_path(workspace_id: str) -> Path:
    if not workspace_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in workspace_id):
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
    path.parent.chmod(0o700)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
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
    discovery = observation.setup_discovery or {}
    return {
        "bounded": observation.setup_view_present,
        "viewId": discovery.get("metadata", {}).get("view") if observation.setup_view_present else None,
        "tools": discovery.get("tools", []) if observation.setup_view_present else [],
        "metadata": discovery.get("metadata") if observation.setup_view_present else None,
        "catalogToolCount": len(observation.tool_names),
        "blockedBy": [] if observation.setup_view_present else [stagelib.SETUP_VIEW],
        "detail": (
            "the candidate negotiates a bounded server-authored setup view"
            if observation.setup_view_present
            else discovery.get("error") or "initialize-bound setup discovery has not been verified through both transports"
        ),
    }


def _stage_status(journey: dict[str, Any], observation: stagelib.Observation, workspace: pins.ClientWorkspace, stage_ref: Any) -> dict[str, Any]:
    def workspace_blockers(number: int) -> list[str]:
        if workspace.status != "pass":
            return [workspace.reason or "pinned client artifacts were not consumed"]
        return workspace.missing_for_stage(number)

    if isinstance(stage_ref, dict):
        # Accept the imported descriptor only if every binding matches. Never
        # substitute an arbitrary command supplied by a model or caller.
        matched = next((s for s in journey["stages"] if s["id"] == stage_ref.get("id")), None)
        if matched is None or any(stage_ref.get(k) != matched[k] for k in ("id", "number", "command")):
            raise DriverError("stage descriptor does not match the journey contract")
        stage_ref = matched["id"]
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
        "executionEnabled": bool(observation.setup_view_present and bindir is not None),
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
    if state.get("executionEnabled") and state.get("stackUp") and observation.setup_view_present:
        engine = _executor(state, _target, observation)
        number = status["number"]
        result = engine.result(number) if number >= 3 else None
        completed = (result.status == "pass" if result else status["status"] == "pass")
        acted = bool(engine.evidence["actions"].get(str(number)))
        proposal_id = engine.resources.get("proposalId")
        pending = number == 8 and proposal_id and not engine.evidence.get("approval")
        failed = result is not None and result.status == "fail"
        _write_state(state["workspaceId"], state)
        outcome = "fail" if failed else "pass" if completed and acted else "awaiting_approval" if pending else "ready"
        checks = [c.as_receipt() for c in result.checks] if result else status["checks"]
        return {"status": "fail" if failed else "pass" if completed and acted else "ready",
                "stageStatus": {"number": number, "id": status["id"], "command": status["command"],
                                "status": outcome, "checks": checks,
                                "blockedBy": [] if outcome in {"ready", "awaiting_approval", "pass"} else result.blocked_by},
                "proposalId": proposal_id if pending else None,
                "observation": {**_observation_payload(observation), "resources": dict(engine.resources),
                                "fixture": _target.get("execution", {}),
                                "evidence": checks},
                "toolView": _tool_view(observation), "blockedBy": []}
    return {
        "status": "blocked" if status["status"] != "pass" else "pass",
        "stageStatus": status,
        "observation": _observation_payload(observation),
        "toolView": _tool_view(observation),
        "blockedBy": status["blockedBy"],
    }


def op_execute(request: dict[str, Any]) -> dict[str, Any]:
    """Execute exactly the model-selected action — but only from a bounded view.

    Discovery does not grant authority or implement execution. Even a verified
    bounded view must remain non-executable until the release driver performs
    the real authenticated operation and records its canonical identities.
    """
    state, _target, _manifest, observation, workspace = _rehydrate(request)
    journey = json.loads((HERE / "journey.v1.json").read_text())
    stage_ref = request.get("stage") or request.get("stageId") or request.get("stageNumber")
    status = _stage_status(journey, observation, workspace, stage_ref)
    action = request.get("action") or {}
    view = _tool_view(observation)

    if observation.setup_view_present and state.get("stackUp"):
        engine = _executor(state, _target, observation)
        try:
            result = engine.execute(status["number"], action)
        except ExecutionError as exc:
            _write_state(state["workspaceId"], state)
            return {"status": "blocked" if exc.blocked else "fail", "stageStatus": status,
                    "result": {"accepted": False, "command": exc.command, "reason": exc.reason,
                               "injectedError": None, "recoveredError": None},
                    "canonicalIds": {k: None for k in executor.ID_FIELDS},
                    "blockedBy": [stagelib.JOURNEY_DRIVER] if exc.blocked else []}
        _write_state(state["workspaceId"], state)
        return {"status": result["status"], "stageStatus": status, "result": result,
                "canonicalIds": {k: result.get("canonicalIds", {}).get(k) for k in executor.ID_FIELDS}, "blockedBy": []}

    return {
        "status": "blocked",
        "stageStatus": status,
        "result": {
            "accepted": False,
            "reason": (
                "the release driver has not implemented authenticated action execution; "
                "verified discovery alone cannot establish call authority or execution success"
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
    error_id = request.get("errorId")
    if state.get("stackUp") and state.get("executionEnabled"):
        executor.identity(error_id, "inject_error")
        stage_ref = request.get("stage")
        journey = json.loads((HERE / "journey.v1.json").read_text())
        stage = next((s for s in journey["stages"] if s["id"] == stage_ref), None)
        if not stage or stage["number"] != 4 or state.get("armedError"):
            raise DriverError("inject_error requires one unarmed style-render stage")
        state["armedError"] = {"id": error_id, "stageNumber": 4, "status": "armed"}
        _write_state(state["workspaceId"], state)
        return {"status": "armed", "errorId": error_id, "recoverable": True, "blockedBy": []}
    return {
        "status": "blocked",
        "errorId": error_id,
        "recoverable": True,
        "detail": (
            "the requested error cannot be armed against a real "
            "action while execute is blocked; arming it against a mocked action "
            "would make the recovery evidence fictional"
        ),
        "blockedBy": list(EXECUTE_BLOCKERS),
    }


def op_approve(request: dict[str, Any]) -> dict[str, Any]:
    _state, target, _manifest, _observation, _workspace = _rehydrate(request)
    if _state.get("execution") and _state.get("stackUp"):
        try:
            result = _executor(_state, target, _observation).approve(request.get("proposalId"))
            _write_state(_state["workspaceId"], _state)
            return {"status": "approved", "principalProfile": "approver", **result, "blockedBy": []}
        except ExecutionError as exc:
            _write_state(_state["workspaceId"], _state)
            return {"status": "blocked" if exc.blocked else "fail", "principalProfile": "approver",
                    "proposalId": request.get("proposalId"), "approvalId": None,
                    "proposerSelfApproval": "denied-untested", "detail": f"{exc.command}: {exc.reason}",
                    "blockedBy": [stagelib.APPROVAL_COMMAND] if exc.blocked else []}
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
    if _state.get("execution") and _state.get("stackUp"):
        engine = _executor(_state, _target, observation)
        final = engine.verify_final()
        evidence = engine.evidence
        proofs = evidence.get("proofs", {})
        authority = engine.verify_authority()
        assertions = {"finalUrl": {"status": final.status, "detail": final.detail},
                      "pixelProof": {"status": "pass" if proofs.get("pixel") else "blocked",
                                     "detail": "independent pixel assertion"},
                      **{name: {"status": check.status, "detail": check.detail} for name, check in authority.items()},
                      "proposerApproverSeparation": {"status": "pass" if evidence.get("approvalResolution") else "blocked",
                                                     "detail": "typed separate-principal approval"},
                      }
        _write_state(_state["workspaceId"], _state)
        outcome = "fail" if any(a["status"] == "fail" for a in assertions.values()) else (
            "pass" if all(a["status"] == "pass" for a in assertions.values()) else "blocked")
        return {"status": outcome, "assertions": assertions,
                "finalUrlProof": proofs.get("final-map"), "pixelProof": proofs.get("pixel"),
                "canonicalIds": {k: evidence.get("publicationOperation", {}).get(k) for k in executor.ID_FIELDS},
                "blockedBy": [] if outcome != "blocked" else [stagelib.PROPOSAL_AUTHZ, stagelib.SCOPE_NARROWING]}
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


def _executor(state, target, observation):
    workdir = Path(state["workdir"])
    bindir = workdir / "install" / "node_modules" / ".bin"
    credentials = {name: probes.resolve_env_default(reference, "")
                   for name, reference in (target.get("principals") or {}).items()}
    credentials.setdefault("proposer", probes.resolve_env_default(
        target["adminPassword"]["env"], target["adminPassword"]["default"]))
    transport = Transport(state["baseUrl"], bindir / "honua-mcp-proxy", bindir / "honua", workdir, credentials)
    return executor.JourneyExecutor(state, target, observation, transport)


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
    except ExecutionError as exc:
        return {"status": "blocked" if exc.blocked else "fail", "operation": operation,
                "error": f"{exc.command}: {exc.reason}"}
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
