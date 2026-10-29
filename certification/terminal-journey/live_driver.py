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
* `execute` invokes the installed proxy for calls in its verified bounded view.
  Stage 3 can select the typed published .NET bridge for the observed ingest and
  publication capabilities. Discovery itself never grants call authority.
* Credential values never enter a response. Only environment-variable references
  are returned, per the protocol's first prohibition.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import pins  # noqa: E402
import probes  # noqa: E402
import stages as stagelib  # noqa: E402
import executor  # noqa: E402
import local_fixture  # noqa: E402
from transport import ExecutionError, Transport  # noqa: E402

PROTOCOL = "terminal-journey-driver-v1"
DEFAULT_TARGET = HERE / "targets" / "local-docker.json"
STATE_ROOT = Path.cwd() / ".terminal-journey" / "sessions"

# Blockers that stop a live model run before any action can be attempted.
EXECUTE_BLOCKERS = [stagelib.JOURNEY_DRIVER]
APPROVE_BLOCKERS = [stagelib.JOURNEY_DRIVER]


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


def _compose_for(target: dict[str, Any], manifest: dict[str, Any], workdir: Path) -> tuple[probes.Compose, str, str]:
    compose_cfg = target["compose"]
    server = manifest["components"]["honua-server"]
    image_ref = f"{server['image']}@{server['digest']}"
    port = int(compose_cfg["port"])
    base_url = f"http://127.0.0.1:{port}"
    compose = probes.Compose(
        compose_file=str(ROOT / compose_cfg["file"]),
        project=compose_cfg["project"],
        env={
            **local_fixture.compose_env(target, workdir),
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
        "blockedBy": [] if observation.setup_view_present else [stagelib.JOURNEY_DRIVER],
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
def _cloud_image(target, manifest, target_digest, proof, *, profile=None):
    """Reuse the same-job deployment readback; never infer an external image from manifest pins."""
    if not isinstance(proof, dict) or not proof.get("path") or not proof.get("sha256"):
        raise DriverError("cloud setup requires the verified deterministic deployment receipt")
    path = Path(proof["path"])
    data = (path if path.is_absolute() else ROOT / path).read_bytes()
    if hashlib.sha256(data).hexdigest() != proof["sha256"]:
        raise DriverError("deterministic deployment receipt digest changed")
    receipt = json.loads(data)
    server = manifest["components"]["honua-server"]
    image_name, digest_name = (("awsLambdaImage", "awsLambdaDigest") if target["kind"] == "aws-serverless"
                               else ("image", "digest"))
    if not server.get(image_name) or not server.get(digest_name):
        raise DriverError("the declared cell has no pinned candidate image")
    expected = {"sourceSha": server["sha"],
                "image": f"{server[image_name].split('@', 1)[0]}@{server[digest_name]}"}
    recorded = receipt.get("target") or {}
    first = next((stage for stage in receipt.get("stages", []) if stage.get("number") == 1), {})
    image = next((check for check in first.get("checks", []) if check.get("id") == "1.1-candidate-image"), {})
    bounded_off = proof.get("prerequisiteProfile") == "redis-off-serving-refusal"
    if bounded_off:
        if profile != "redis-off-serving-refusal":
            raise DriverError("bounded prerequisite requires the explicit Redis-off model profile")
        from tools.terminal_model_canary import validate_deterministic_receipt, load_journey, CanaryError
        try:
            validated = validate_deterministic_receipt(path, manifest=manifest,
                journey=load_journey(HERE / "journey.v1.json"), cell=target["id"],
                prerequisite_profile="redis-off-serving-refusal")
        except CanaryError as exc:
            raise DriverError("invalid bounded Redis-off deployment prerequisite") from exc
        if proof != validated:
            raise DriverError("bounded prerequisite metadata changed")
    if (receipt.get("status") not in ({"pass", "blocked"} if bounded_off else {"pass"}) or receipt.get("server") != expected
            or recorded.get("id") != target["id"] or recorded.get("configSha256") != target_digest
            or str(recorded.get("baseUrl", "")).rstrip("/") != target["replicaBaseUrl"].rstrip("/")
            or first.get("status") != "pass" or image.get("status") != "pass"
            or first.get("evidence", {}).get("source") != "live-" + target["kind"]
            or first.get("evidence", {}).get("freshness") != "verified-current"):
        raise DriverError("deployment receipt does not verify this cell, target and candidate image")
    return receipt["server"]["image"], f"same-job deployment readback; receipt SHA-256 {proof['sha256']}"


def op_setup(request: dict[str, Any]) -> dict[str, Any]:
    target_path = Path(request.get("target") or DEFAULT_TARGET)
    target_bytes = target_path.read_bytes()
    target = json.loads(target_bytes)
    cloud = target.get("kind") in local_fixture.CLOUD_KINDS
    if cloud:
        import cloud_target
        cloud_target.validate(target, template=json.loads(DEFAULT_TARGET.read_text()))
    manifest = _load_yaml(ROOT / "platform-manifest.yaml")
    deployment_image, deployment_detail = (None, None)
    if cloud:
        deployment_image, deployment_detail = _cloud_image(target, manifest,
            hashlib.sha256(target_bytes).hexdigest(), request.get("deploymentReceipt"), profile=request.get("profile"))

    serving_fixture = None
    if request.get("profile") == "redis-off-serving-refusal":
        import refusal_profile
        fixture_proof = request.get("servingFixture") or {}
        fixture_path = target_path.parent / "extended/seed-manifest.json"
        if (not cloud or Path(fixture_proof.get("path", "")).resolve() != fixture_path.resolve()
                or hashlib.sha256(fixture_path.read_bytes()).hexdigest() != fixture_proof.get("sha256")):
            raise DriverError("model serving fixture is not this cell's retained hashed seed manifest")
        serving_fixture = refusal_profile.fixture_binding(fixture_path)

    workspace_id = f"tj-{uuid.uuid4().hex[:12]}"
    workdir = STATE_ROOT.parent / workspace_id
    workspace = pins.resolve_client_workspace(manifest, workdir / "clients")

    bindir = None
    install_notes: list[str] = []
    if workspace.status == "pass":
        bindir, _detail, install_notes = pins.install_executables(workspace, workdir / "install")
        workspace.install_notes = install_notes

    compose = None
    if cloud:
        # The provisioner owns this endpoint. Never create or destroy local/cloud infrastructure.
        base_url, image_ref, stack_up = target["replicaBaseUrl"], deployment_image, True
    else:
        compose, base_url, image_ref = _compose_for(target, manifest, workdir)
        stack_up = compose.up().returncode == 0

    server_sha = manifest["components"]["honua-server"]["sha"]
    observation = stagelib.Observation(
        base_url=base_url,
        image_ref=image_ref if stack_up else None,
        expected_revision=server_sha,
        expected_image_digests=stagelib.candidate_image_digests(manifest),
    )
    if stack_up:
        import run as driver  # noqa: PLC0415 - shared observation logic, one implementation

        observation = driver.observe(target, base_url, workspace, bindir, image_ref, server_sha)
        observation.expected_image_digests = stagelib.candidate_image_digests(manifest)
        if deployment_detail:
            observation.image_detail = deployment_detail

    state = {
        "workspaceId": workspace_id,
        "targetPath": str(target_path),
        "targetSha256": hashlib.sha256(target_bytes).hexdigest(),
        "targetConfig": target,
        "servingFixtureBinding": serving_fixture,
        "servingFixturePath": str(fixture_path) if serving_fixture else None,
        "workdir": str(workdir),
        "baseUrl": base_url,
        "stackUp": stack_up,
        "external": cloud,
        "deploymentImage": deployment_image,
        "deploymentDetail": deployment_detail,
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
        blockers.append(stagelib.JOURNEY_DRIVER)
    if stack_up and observation.ready:
        try:
            credentials = local_fixture.credentials(target, workdir, base_url, mint=True,
                **({"tenant_signing_key": os.environ["HONUA_CANARY_TENANT_SIGNING_KEY"]}
                   if cloud and os.environ.get("HONUA_CANARY_TENANT_SIGNING_KEY") else {}))
            if serving_fixture is None:
                local_fixture.prepare_style_catalog(target,
                    Transport(base_url, None, None, workdir, credentials))
        except (ExecutionError, KeyError, ValueError, TypeError, AttributeError):
            blockers.append(stagelib.JOURNEY_DRIVER)

    if blockers and stack_up:
        if compose is not None:
            compose.down()
        stack_up = False
        state["stackUp"] = False
        _write_state(workspace_id, state)

    return {
        "status": "blocked" if blockers else "ready",
        "workspaceId": workspace_id,
        "baseUrl": base_url,
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
        ] + [{"id": name, "envVar": ref, "principal": name,
              "note": "short-lived fixture principal; resolved privately at call time"}
             for name, ref in (target.get("principals") or {}).items()],
        "blockedBy": list(dict.fromkeys(blockers)),
        "clientWorkspace": workspace.as_receipt(),
        "notices": install_notes,
    }


def _rehydrate(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], stagelib.Observation, pins.ClientWorkspace]:
    state = _read_state(str(request.get("workspaceId", "")))
    target_bytes = Path(state["targetPath"]).read_bytes()
    if state.get("targetSha256") and hashlib.sha256(target_bytes).hexdigest() != state["targetSha256"]:
        raise DriverError("target config changed after setup")
    if state.get("servingFixtureBinding"):
        import refusal_profile
        if refusal_profile.fixture_binding(Path(state["servingFixturePath"])) != state["servingFixtureBinding"]:
            raise DriverError("serving fixture source or retained manifest changed after setup")
    target = json.loads(target_bytes)
    manifest = _load_yaml(ROOT / "platform-manifest.yaml")
    workdir = Path(state["workdir"])
    workspace = pins.ClientWorkspace.from_receipt(state["clientWorkspace"], workdir / "clients")
    bindir = workdir / "install" / "node_modules" / ".bin"
    import run as driver  # noqa: PLC0415

    server = manifest["components"]["honua-server"]
    image_ref = (state.get("deploymentImage") if state.get("external") else
                 f"{server['image']}@{server['digest']}" if state.get("stackUp") else None)
    observation = driver.observe(
        target,
        state["baseUrl"],
        workspace,
        bindir if bindir.is_dir() else None,
        image_ref,
        server["sha"],
    )
    observation.expected_image_digests = stagelib.candidate_image_digests(manifest)
    if state.get("deploymentDetail"):
        observation.image_detail = state["deploymentDetail"]
    return state, target, manifest, observation, workspace


def op_observe(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("profile") is not None:
        import refusal_profile
        return refusal_profile.observe(sys.modules[__name__], request)
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
        observed_checks = [
            probes.Check(c["id"], c["kind"], c["invocation"], c["status"], c["detail"], c.get("blockedBy", []))
            for c in status["checks"]]
        prerequisites = stagelib.prerequisites(observed_checks)
        recorded = engine.evidence["checks"].get(str(number), {})
        # Missing, unexecuted assertions are pending work. Persisted blockers and
        # completed work lacking canonical evidence are actual stopping conditions.
        concrete = prerequisites + ([c for c in result.checks if c.id in {r["id"] for r in recorded.values()}]
                                    if result else [])
        if result is None:
            concrete = observed_checks
        unfinished = result and any(c.detail == "required live assertion has not executed" for c in result.checks)
        if result and not unfinished:
            concrete = prerequisites + result.checks
        # Keep the failed oracle visible while a model can repair its own
        # selected size within the unchanged action budget. Candidate defects
        # and every other failed assertion remain terminal. Completion still
        # requires engine.result() to pass the original pixel oracle.
        repairable = {c.id for c in concrete if engine.repairable_render_selection(number, c)}
        failed = any(c.status == "fail" and c.id not in repairable for c in concrete)
        blocked = any(c.status == "blocked" for c in concrete)
        blockers = list(dict.fromkeys(b for c in concrete if c.status == "blocked" for b in c.blocked_by))
        _write_state(state["workspaceId"], state)
        outcome = ("fail" if failed else "blocked" if blocked else "pass" if completed and acted
                   else "awaiting_approval" if pending else "ready")
        checks = [c.as_receipt() for c in concrete] if result else status["checks"]
        return {"status": outcome if outcome in {"fail", "blocked", "pass"} else "ready",
                "stageStatus": {"number": number, "id": status["id"], "command": status["command"],
                                "status": outcome, "checks": checks,
                                "blockedBy": blockers},
                "proposalId": proposal_id if pending else None,
                "observation": {**_observation_payload(observation), "resources": dict(engine.resources),
                                "fixture": _target.get("execution", {}),
                                "terminalSurface": engine.terminal_surface(number),
                                "renderDimensions": engine.evidence.get("renderDimensions"),
                                "mapReadbacks": engine.evidence.get("mapReadbacks", []),
                                "repairableChecks": sorted(repairable),
                                "evidence": checks},
                "toolView": _tool_view(observation),
                # The tool calls this stage admits (the executor refuses any other as an invalid action):
                # the stage's set intersected with the server-authored view.
                "stageTools": sorted(executor.STAGE_TOOLS.get(number, set())
                                     & {t.get("name") for t in _tool_view(observation).get("tools", [])
                                        if isinstance(t, dict)}),
                "blockedBy": blockers}
    return {
        "status": "blocked" if status["status"] != "pass" else "pass",
        "stageStatus": status,
        "observation": _observation_payload(observation),
        "toolView": _tool_view(observation),
        "blockedBy": status["blockedBy"],
    }


# Typed MCP tool error codes that describe the request, not a candidate fault: a well-formed call the
# candidate refused because of what was asked (missing resource, wrong state).
# invalid_argument, invalid_input, and validation_failed stay in that model-feedback set when the
# call was only schema-admitted. An unknown styleId, a bad bbox, or an unsupported encoding is a
# value the model can correct. The same codes are candidate-execution only when the executor proved
# the arguments against the authored fixture (problem["argumentsProven"]). Schema admission is not
# that proof. Server faults, transport failures, and permission or proof failures stay
# candidate-execution. ActionRejected, before any candidate write, stays invalid-action.
CANDIDATE_REFUSAL_CODES = frozenset({"not_found", "conflict", "already_exists", "failed_precondition"})
ADMITTED_CALL_DEFECT_CODES = frozenset({"invalid_argument", "invalid_input", "validation_failed"})


def op_execute(request: dict[str, Any]) -> dict[str, Any]:
    """Execute the selected bounded call and retain only observed identities."""
    if request.get("profile") is not None:
        import refusal_profile
        return refusal_profile.execute(sys.modules[__name__], request)
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
            from transport import ActionRejected  # noqa: PLC0415 - same module path as the executor
            code = exc.problem.get("code")
            # invalid-action: refused before the candidate write (model feedback).
            # candidate-refusal: a typed request error the model can correct, including a
            # schema-valid invalid_argument whose values were not fixture-proven.
            # candidate-execution: a fixture-proven call the candidate still rejected, or any
            # other failure of a call that already passed admission (never model feedback).
            if isinstance(exc, ActionRejected):
                refusal = "invalid-action"
            elif code in ADMITTED_CALL_DEFECT_CODES and exc.problem.get("argumentsProven") is True:
                refusal = "candidate-execution"
            elif code in CANDIDATE_REFUSAL_CODES or code in ADMITTED_CALL_DEFECT_CODES:
                refusal = "candidate-refusal"
            else:
                refusal = "candidate-execution"
            return {"status": "blocked" if exc.blocked else "fail", "stageStatus": status,
                    "refusal": refusal,
                    "result": {"accepted": False, "command": exc.command, "reason": exc.reason,
                               "injectedError": None, "recoveredError": None,
                               **({"mapReadbacks": engine.evidence.get("mapReadbacks", [])}
                                  if action.get("tool") == "honua_studio_save_version" else {})},
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
        engine = _executor(_state, target, _observation)
        try:
            try:
                result = engine.approve(request.get("proposalId"))
            finally:
                if engine.evidence.get("approvalResolution"):
                    engine.verify_final()
                    engine.verify_authority()
            _write_state(_state["workspaceId"], _state)
            return {"status": "approved", "principalProfile": "approver", **result, "blockedBy": []}
        except ExecutionError as exc:
            _write_state(_state["workspaceId"], _state)
            return {"status": "blocked" if exc.blocked else "fail", "principalProfile": "approver",
                    "proposalId": request.get("proposalId"), "approvalId": None,
                    "proposerSelfApproval": "denied-untested", "detail": f"{exc.command}: {exc.reason}",
                    "blockedBy": [stagelib.JOURNEY_DRIVER] if exc.blocked else []}
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
        "blockedBy": list(APPROVE_BLOCKERS),
    }


def op_verify(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("profile") is not None:
        import refusal_profile
        return refusal_profile.verify(sys.modules[__name__], request)
    from tools.terminal_model_canary import ASSERTION_NAMES

    state, target, _manifest, observation, workspace = _rehydrate(request)
    assertions = dict.fromkeys(ASSERTION_NAMES, "blocked")
    proofs, canonical = {}, {}
    if state.get("execution") and state.get("stackUp"):
        engine = _executor(state, target, observation)
        final = engine.verify_final()
        authority = engine.verify_authority()
        fake = engine.verify_fake_success()
        evidence = engine.evidence
        proofs = evidence.get("proofs", {})
        discovery = observation.setup_discovery or {}
        http, proxy = discovery.get("http", {}), discovery.get("proxy", {})
        assertions["toolProfilePresent"] = "pass" if observation.setup_view_present else "blocked"
        assertions["catalogExact"] = ("pass" if observation.setup_view_present
            and http.get("status") == proxy.get("status") == "pass"
            and http.get("fullCatalogComparisonSha256")
            and http["fullCatalogComparisonSha256"] == proxy.get("fullCatalogComparisonSha256") else "blocked")
        assertions["authentication"] = ("pass" if observation.anonymous_admin_status in {401, 403}
            and observation.anonymous_api_keys_status in {401, 403}
            and observation.credential_probe and observation.credential_probe.status == "pass" else "fail")
        journey = json.loads((HERE / "journey.v1.json").read_text())
        originals = stagelib.run_stages(journey, observation,
            lambda n: workspace.missing_for_stage(n) if workspace.status == "pass" else [stagelib.INSTALLED_CLIENTS])
        current = originals[:2] + [stagelib.merge_execution(original, engine.result(original.number))
                                  for original in originals[2:]]
        assertions["evidenceFresh"] = ("fail" if any(r.status == "fail" for r in current) else
                                      "pass" if all(r.status == "pass" for r in current) else "blocked")
        assertions["fakeSuccessRejected"] = fake.status
        assertions["finalUrlProof"] = final.status
        pixel = evidence["checks"].get("4", {}).get("pixel", {})
        assertions["pixelProof"] = (pixel.get("status", "blocked") if proofs.get("pixel")
                                    else "fail" if pixel.get("status") == "fail" else "blocked")
        for name in ("rbacDenial", "tenantIsolation", "currentAuthorityRevalidation"):
            assertions[name] = authority[name].status
        separation = evidence["checks"].get("8", {}).get("separation", {})
        assertions["proposerApproverSeparation"] = separation.get("status", "blocked")
        # The canonical join is required even though it has no separate canary assertion.
        if authority["canonicalIdJoin"].status != "pass":
            assertions["evidenceFresh"] = authority["canonicalIdJoin"].status
        canonical = evidence.get("publicationOperation", {})
        _write_state(state["workspaceId"], state)
    outcome = ("fail" if "fail" in assertions.values() else
               "pass" if all(value == "pass" for value in assertions.values()) else "blocked")
    return {"status": outcome, "assertions": assertions,
            "finalUrlProof": proofs.get("final-map"), "pixelProof": proofs.get("pixel"),
            "canonicalIds": {key: canonical.get(key) for key in executor.ID_FIELDS},
            "blockedBy": [stagelib.JOURNEY_DRIVER] if outcome == "blocked" else []}


def _executor(state, target, observation):
    workdir = Path(state["workdir"])
    bindir = workdir / "install" / "node_modules" / ".bin"
    local_fixture.restore_datasource_environment(target)
    credentials = local_fixture.credentials(target, workdir, state["baseUrl"],
        **({"tenant_signing_key": os.environ["HONUA_CANARY_TENANT_SIGNING_KEY"]}
           if state.get("external") and os.environ.get("HONUA_CANARY_TENANT_SIGNING_KEY") else {}))
    credentials.setdefault("proposer", probes.resolve_env_default(
        target["adminPassword"]["env"], target["adminPassword"]["default"]))
    transport = Transport(state["baseUrl"], bindir / "honua-mcp-proxy", bindir / "honua", workdir, credentials)
    return executor.JourneyExecutor(state, target, observation, transport,
                                    manifest=_load_yaml(ROOT / "platform-manifest.yaml"))


def op_teardown(request: dict[str, Any]) -> dict[str, Any]:
    workspace_id = str(request.get("workspaceId", ""))
    state = _read_state(workspace_id)
    # Cleanup uses the reviewed setup snapshot even if the retained target file changed.
    target = state.get("targetConfig") or json.loads(Path(state["targetPath"]).read_text())
    manifest = _load_yaml(ROOT / "platform-manifest.yaml")
    if state.get("external"):
        # Revoke only run-minted application principals. Cell teardown belongs to its provisioner.
        _revoked, failed = local_fixture.revoke(target, Path(state["workdir"]), state["baseUrl"])
        result_code = 1 if failed else 0
        detail = ("one or more run-owned application principals could not be revoked" if failed
                  else "run-owned application principals revoked; external cell retained")
    else:
        compose, _base_url, _image = _compose_for(target, manifest, Path(state["workdir"]))
        _retain_server_log(compose, target)
        result = compose.down()
        result_code, detail = result.returncode, (result.stderr or result.stdout).strip()[:300]
    if result_code == 0:
        local_fixture.cleanup(state["workdir"])
        _state_path(workspace_id).unlink(missing_ok=True)
    return {
        "status": "pass" if result_code == 0 else "fail",
        "workspaceId": workspace_id,
        "detail": detail or "isolated namespace removed",
    }


# Opt-in diagnostics: before the local stack is removed, keep the candidate's warning/error and
# AI-proxy log lines (the proxy reports only "Provider request failed"; the exception type is logged
# server-side). Lines are filtered and the run's credentials redacted, because the artifact is public.
SERVER_LOG_ENV = "HONUA_JOURNEY_SERVER_LOG"
_SERVER_LOG_LINE = re.compile(r"\b(WRN|ERR|FTL|EROR|fail|warn)\b|StudioAi|Bedrock|Provider|Exception|Admission", re.IGNORECASE)


def _retain_server_log(compose, target: dict[str, Any]) -> None:
    destination = os.environ.get(SERVER_LOG_ENV)
    if not destination:
        return
    try:
        raw = compose.logs(str((target.get("compose") or {}).get("serverService") or "server"))
    except Exception as exc:  # diagnostics must never block teardown
        raw = f"server log unavailable: {type(exc).__name__}"
    secrets = [value for value in (os.environ.get("HONUA_ADMIN_PASSWORD"), os.environ.get("TERMINAL_MODEL_API_KEY"),
                                   os.environ.get("HONUA_JOURNEY_SIGNING_KEY")) if value]
    kept = []
    for line in raw.splitlines():
        if _SERVER_LOG_LINE.search(line):
            for secret in secrets:
                line = line.replace(secret, "[redacted]")
            kept.append(line[:2000])
    try:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text("\n".join(kept[-1500:]) + "\n", encoding="utf-8")
    except OSError:
        pass  # diagnostics are best-effort; teardown must still remove the stack and its credentials


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
        return {"status": "fail", "operation": operation, "errorCategory": "DriverError", "error": str(exc)}
    except ExecutionError as exc:
        return {"status": "blocked" if exc.blocked else "fail", "operation": operation,
                "errorCategory": "ExecutionError", "error": f"{exc.command}: {exc.reason}"}
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
    with probes.allow_http_cell(os.environ.get("HONUA_CANARY_HTTP_CELL_HOST")):
        response = handle(request if isinstance(request, dict) else {})
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    # A verified recoverable tool refusal is a domain failure carried in the
    # protocol response. A process failure would prevent the canary from reading
    # that evidence and selecting a recovery action on its next turn.
    result = response.get("result") or {}
    fault = result.get("injectedError") or {}
    recoverable = (isinstance(request, dict) and request.get("operation") == "execute"
                   and fault.get("recoverable") is True)
    return 0 if response.get("status") != "fail" or recoverable else 1


if __name__ == "__main__":
    raise SystemExit(main())
