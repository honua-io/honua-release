#!/usr/bin/env python3
"""Deterministic terminal journey driver — honua-release#123.

Evidence key `release.e2e.terminal-zero-to-map`.

Two modes, one receipt schema:

* `--mode build` (default) materializes the eight-stage contract and the
  control-plane roster verdict without contacting a target. It can never report
  `pass`; the schema forbids it.
* `--mode live --target targets/local-docker.json` consumes the exact #136
  `clientArtifacts` pins from published registry bytes, brings up the pinned
  candidate stack, and executes the fixed probes for every numbered stage.

A stage that cannot run yet is `blocked` and names its missing dependency. There
is no skip state, and a failure names the numbered stage plus the command or tool
that broke it. No model is consulted anywhere in this driver; the genuine-model
canary is honua-release#161 and is linked, never embedded.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "e2e"))
from licensing import assert_disabled
sys.path.insert(0, str(HERE))

import pins  # noqa: E402
import probes  # noqa: E402
import stages as stagelib  # noqa: E402
import local_fixture  # noqa: E402


class GateError(RuntimeError):
    pass


# Target kinds whose live observations the receipt schema attributes (evidence.source live-<kind>).
LIVE_KINDS = ("local-docker", *local_fixture.CLOUD_KINDS)


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Control-plane roster gate (data-driven; operator ruling on honua-release#785)
# ---------------------------------------------------------------------------
# The server publishes its Admin parity exports at docs/gis/data/ for every source sha:
# admin-openapi-operation-ids.json (every REST/CLI operation) and admin-mcp-projection-manifest.json
# (catalog operations projected to MCP, plus the audited exclusions). The roster passes when both
# exports are present, declared ready, self-consistent, and every Admin operation this journey
# called is inside them. Counts are reported, never asserted: the hard-pinned 396/385/11 contract
# (control-plane-roster.v1.json) is a historical fixture and no longer gates.
ADMIN_BASE = "/api/v1/admin"
ADMIN_EXPORTS = ("docs/gis/data/admin-openapi-operation-ids.json",
                 "docs/gis/data/admin-mcp-projection-manifest.json")


def _admin_route(method: str, template: str):
    import re

    pattern = re.sub(r"\\{[^/{}]+\\}", "[^/]+", re.escape(template.rstrip("/") or "/"))
    return method.upper(), re.compile(pattern + "/?")


def roster_verdict(rest: dict[str, Any] | None, mcp: dict[str, Any] | None,
                   calls: list[dict[str, Any]] | None = None, *,
                   source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Judge the server's own parity exports and the journey's Admin calls against them."""
    binding = {"source": source} if source else {}
    if rest is None or mcp is None:
        missing = [name.rsplit("/", 1)[-1] for name, value in zip(ADMIN_EXPORTS, (rest, mcp)) if value is None]
        return {"status": "blocked",
                "reason": "authoritative candidate roster exports unavailable: " + ", ".join(missing),
                "requiredExports": list(ADMIN_EXPORTS), **binding}
    problems: list[str] = []
    try:
        rest_rows = rest["operations"]
        projections = mcp["operations"]
        exclusions = mcp["exclusions"]["operations"]
        rest_ids = [row["operationId"] for row in rest_rows]
        catalog = {row["catalogOperationId"]: row["operationId"] for row in rest_rows if row.get("catalogOperationId")}
        projected = [row["operationId"] for row in projections]
        tools = {row["toolName"] for row in projections}
        excluded = [row["OperationId"] for row in exclusions]
        excluded_rest = [row["OpenApiOperationId"] for row in exclusions]
        routes = [(_admin_route(row["method"], row["path"]), row["operationId"]) for row in rest_rows]
    except (KeyError, TypeError, AttributeError) as exc:
        return {"status": "fail", "problems": [f"parity exports are not in the server format: {exc!r}"], **binding}

    roster = mcp.get("roster") or {}
    if roster.get("status") != "ready" or list(roster.get("exports") or []) != list(ADMIN_EXPORTS):
        problems.append("MCP manifest does not declare the exact parity exports ready")
    if len(set(rest_ids)) != len(rest_ids):
        problems.append("REST/CLI export repeats an operationId")
    if len(set(projected)) != len(projected) or len(set(excluded)) != len(excluded):
        problems.append("MCP manifest repeats a projected or excluded operation")
    if not projected:
        problems.append("MCP manifest projects no operation")
    both = sorted(set(projected) & set(excluded))
    if both:
        problems.append(f"operations both projected and excluded: {both}")
    outside = sorted(set(projected) - set(catalog))
    if outside:
        problems.append(f"MCP projections without a REST catalog identity: {outside}")
    unaccounted = sorted(set(catalog) - set(projected) - set(excluded))
    if unaccounted:
        problems.append(f"catalogued REST operations neither projected nor excluded: {unaccounted}")
    unknown_exclusions = sorted(set(excluded_rest) - set(rest_ids))
    if unknown_exclusions:
        problems.append(f"MCP exclusions name unknown REST operations: {unknown_exclusions}")
    fields = [rest.get("generatedCliFields"), rest.get("generatedConsoleFields"), mcp.get("catalogFields"),
              mcp.get("generatedCliFields"), mcp.get("generatedConsoleFields")]
    if not all(isinstance(value, list) and value for value in fields) or any(value != fields[2] for value in fields):
        problems.append("CLI/Console/catalog field rosters are missing or drift")

    admin_calls: list[str] = []
    out_of_scope = 0
    for call in calls or []:
        surface, name = call.get("surface"), str(call.get("name") or "")
        if surface == "cli":
            admin_calls.append(f"cli {name}")
            if name not in rest_ids:
                problems.append(f"journey called CLI operation outside the roster: {name}")
        elif surface == "rest" and name.split(" ", 1)[-1].startswith(ADMIN_BASE):
            method, path = name.split(" ", 1)
            admin_calls.append(f"{method} {path}")
            relative = path[len(ADMIN_BASE):] or "/"
            if not any(m == method.upper() and rx.fullmatch(relative) for (m, rx), _ in routes):
                problems.append(f"journey called Admin REST route outside the roster: {method} {path}")
        elif surface == "mcp" and name.startswith("honua_admin_"):
            admin_calls.append(f"mcp {name}")
            if name not in tools:
                problems.append(f"journey called Admin MCP tool outside the projection: {name}")
        else:
            out_of_scope += 1
    return {
        "status": "fail" if problems else "pass",
        "problems": problems,
        "counts": {
            "restCliOperations": len(set(rest_ids)),
            "catalogOperations": len(catalog),
            "uncataloguedRestOperations": len(set(rest_ids)) - len(set(catalog.values())),
            "mcpProjections": len(set(projected)),
            "mcpExclusions": len(set(excluded)),
            "journeyAdminCalls": len(set(admin_calls)),
            "journeyCallsOutsideAdminSurface": out_of_scope,
        },
        "journeyAdminCalls": sorted(set(admin_calls)),
        **binding,
    }


def roster_source(rest_path: Path | None, mcp_path: Path | None, source_sha: str | None,
                  manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Bind the exports to the server source sha of the image under test, or refuse them."""
    if rest_path is None or mcp_path is None:
        return None
    server_sha = manifest["components"]["honua-server"]["sha"]
    if source_sha != server_sha:
        raise GateError(f"roster exports come from honua-server {source_sha}, not the image's source {server_sha}")
    return {"repository": "honua-io/honua-server", "sourceSha": source_sha,
            "exports": {name: _sha256_file(path) for name, path in zip(ADMIN_EXPORTS, (rest_path, mcp_path))}}


# ---------------------------------------------------------------------------
# Live observation against the pinned candidate
# ---------------------------------------------------------------------------
def observe(
    target: dict[str, Any],
    base_url: str,
    workspace: pins.ClientWorkspace,
    bindir: Path | None,
    image_ref: str | None,
    expected_revision: str,
) -> stagelib.Observation:
    """Run every live probe exactly once and share the result across stages."""
    import discovery

    discovery.mark_step("observe: readiness and licensing")
    endpoints = target["endpoints"]
    observation = stagelib.Observation(
        base_url=base_url, image_ref=image_ref, expected_revision=expected_revision
    )

    ready, detail = probes.wait_for_ready(
        base_url + endpoints["readiness"],
        timeout_seconds=int(target["budgets"]["readinessTimeoutSeconds"]),
    )
    observation.ready = ready
    observation.readiness_detail = detail
    if not ready:
        return observation

    try:
        assert_disabled(base_url, probes.resolve_env_default(
            target["adminPassword"]["env"], target["adminPassword"]["default"]))
        observation.licensing_disabled = True
        observation.licensing_detail = "admin license mode: disabled"
    except Exception as exc:
        observation.licensing_detail = str(exc)

    discovery.mark_step("observe: anonymous capability and admin probes")
    manifest_response = probes.http_get(base_url + endpoints["capabilityManifest"])
    if manifest_response.status == 200:
        try:
            document = manifest_response.json()
            observation.capability_manifest = document if isinstance(document, dict) else None
        except json.JSONDecodeError:
            observation.capability_manifest = None

    admin_response = probes.http_get(base_url + endpoints["adminVersion"])
    observation.anonymous_admin_status = admin_response.status
    api_keys_response = probes.http_get(base_url + endpoints["adminApiKeys"])
    observation.anonymous_api_keys_status = api_keys_response.status

    discovery.mark_step("observe: credential preflight")
    honua = bindir / "honua" if bindir else None
    admin_password = target.get("adminPassword") or {}
    if honua is None or not honua.exists():
        observation.credential_probe = probes.CredentialProbe(
            status="blocked",
            detail="the pinned honua executable was not installed, so credentialed admin calls were not sent",
            blocked_by=[stagelib.INSTALLED_CLIENTS],
        )
    else:
        probe_dir = Path(tempfile.mkdtemp(prefix="honua-credential-preflight-"))
        try:
            observation.credential_probe = probes.run_credential_preflight(
                honua=honua,
                base_url=base_url,
                admin_key=probes.resolve_env_default(admin_password.get("env", ""), admin_password.get("default", "")),
                workdir=probe_dir,
            )
        except Exception:
            observation.credential_probe = probes.CredentialProbe(
                status="fail",
                detail="credential preflight failed closed before producing an observation",
            )
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
    discovery.mark_step("observe: setup discovery")
    proxy = bindir / "honua-mcp-proxy" if bindir else None
    observation.proxy_available = proxy is not None and proxy.is_file()
    observation.setup_discovery = discovery.capture_setup_view(proxy, base_url + endpoints["mcp"],
        probes.resolve_env_default(target["adminPassword"]["env"], target["adminPassword"]["default"]))
    observation.setup_view_present = observation.setup_discovery["status"] == "pass"
    if observation.setup_view_present:
        observation.tool_names = tuple(observation.setup_discovery["catalogToolNames"])
    else:
        observation.tools_error = observation.setup_discovery.get("error", "setup discovery was not verified")
    return observation


# ---------------------------------------------------------------------------
# Receipt assembly
# ---------------------------------------------------------------------------
def build_receipt(
    *,
    manifest: dict[str, Any],
    journey: dict[str, Any],
    roster: dict[str, Any],
    evidence_uri: str,
    mode: str,
    target: dict[str, Any] | None,
    target_path: Path | None,
    target_base_url: str | None,
    workspace: pins.ClientWorkspace,
    stage_results: list[stagelib.StageResult] | None,
    notices: list[str],
) -> dict[str, Any]:
    server = manifest["components"]["honua-server"]
    revisions = (target or {}).get("revisions", {})
    # A live observation is attributed to the target kind that produced it.
    kind = (target or {}).get("kind")
    evidence_source = "harness-build" if mode == "build" else (
        f"live-{kind}" if kind in LIVE_KINDS else "live-local-docker")
    observed_at = None if mode == "build" else _now()

    stage_rows: list[dict[str, Any]] = []
    if stage_results is None:
        for stage in journey["stages"]:
            stage_rows.append(
                {
                    "number": stage["number"],
                    "stage": stage["id"],
                    "command": stage["command"],
                    "status": "blocked",
                    "blockedBy": list(stage["blockedBy"]),
                    "checks": [],
                    "operationId": None,
                    "operationInstanceId": None,
                    "correlationId": None,
                    "auditId": None,
                    "proposalId": None,
                    "jobId": None,
                    "resourceUri": None,
                    "jobStatus": None,
                    "jobCreatedAt": None,
                    "policyDecisionId": None,
                    "approvalId": None,
                    "actuatorId": None,
                    "verificationId": None,
                    "evidence": {
                        "uri": evidence_uri,
                        "source": "harness-build",
                        "freshness": "unverified",
                        "completeness": "incomplete",
                        "observedAt": None,
                    },
                }
            )
    else:
        for result in stage_results:
            if result.status == "pass":
                freshness, completeness = "verified-current", "complete"
            elif result.checks and any(c.status == "pass" for c in result.checks):
                freshness, completeness = "verified-current", "partial"
            else:
                freshness, completeness = "unverified", "incomplete"
            stage_rows.append(
                {
                    "number": result.number,
                    "stage": result.stage,
                    "command": result.command,
                    "status": result.status,
                    "blockedBy": result.blocked_by,
                    "checks": [c.as_receipt() for c in result.checks],
                    "operationId": result.operation_id,
                    "operationInstanceId": result.operation_instance_id,
                    "correlationId": result.correlation_id,
                    "auditId": result.audit_id,
                    "proposalId": result.proposal_id,
                    "jobId": result.job_id,
                    "resourceUri": result.resource_uri,
                    "jobStatus": result.job_status,
                    "jobCreatedAt": result.job_created_at,
                    "policyDecisionId": result.policy_decision_id,
                    "approvalId": result.approval_id,
                    "actuatorId": result.actuator_id,
                    "verificationId": result.verification_id,
                    **({"topologyRefusal": result.topology_refusal} if result.topology_refusal else {}),
                    "evidence": {
                        "uri": evidence_uri,
                        "source": evidence_source,
                        "freshness": freshness,
                        "completeness": completeness,
                        "observedAt": observed_at,
                    },
                }
            )

    approval = None
    if stage_results is not None:
        stage_eight = next((result for result in stage_results if result.number == 8), None)
        if (stage_eight is not None and stage_eight.proposal_id and stage_eight.resolved_by
                and stage_eight.audit_id):
            approval = {
                "proposalId": stage_eight.proposal_id,
                "resolvedBy": stage_eight.resolved_by,
                "auditId": stage_eight.audit_id,
            }

    if roster["status"] == "fail" or any(row["status"] == "fail" for row in stage_rows):
        status = "fail"
    elif all(row["status"] == "pass" for row in stage_rows) and roster["status"] == "pass" and workspace.status == "pass":
        status = "pass"
    else:
        status = "blocked"

    failure = None
    if status == "fail" and roster["status"] == "fail":
        failure = {
            "number": 0,
            "stage": "control-plane-roster",
            "command": "compare authoritative REST/CLI and MCP roster exports",
            "check": "control-plane-roster",
            "detail": "; ".join(roster.get("problems") or ["control-plane roster failed"]),
        }
    elif status == "fail" and stage_results is not None:
        broken = next(r for r in stage_results if r.status == "fail")
        check = broken.first_failure
        failure = {
            "number": broken.number,
            "stage": broken.stage,
            "command": check.invocation if check else broken.command,
            "check": check.id if check else broken.stage,
            "detail": (check.detail if check else "stage failed") or "stage failed",
        }

    from executor import declared_redis_topology

    redis_topology = declared_redis_topology(target)
    return {
        "schemaVersion": 1,
        "receiptSchema": journey["receiptSchema"],
        "evidenceKey": journey["evidenceKey"],
        "generatedAt": _now(),
        "mode": mode,
        "target": {
            "id": (target or {}).get("id", "none"),
            "kind": (target or {}).get("kind", "none"),
            "configPath": str(target_path.relative_to(ROOT)) if target_path else None,
            "configSha256": _sha256_file(target_path) if target_path else None,
            "baseUrl": target_base_url,
            "composeProject": ((target or {}).get("compose") or {}).get("project"),
            **({"redisTopology": redis_topology} if redis_topology else {}),
        },
        "status": status,
        "release": manifest["platformRelease"],
        "clientArtifacts": pins.receipt_pins(manifest),
        "clientWorkspace": workspace.as_receipt(),
        "server": {
            "sourceSha": server["sha"],
            "image": f"{server['image']}@{server['digest']}",
        },
        "fixtureRevision": revisions.get("fixtureRevision", "terminal-zero-to-map-v1"),
        "configRevision": revisions.get("configRevision", "terminal-profiles-v1"),
        "authPolicyRevision": revisions.get("authPolicyRevision", "terminal-separate-principals-v1"),
        "roster": roster,
        "stages": stage_rows,
        **({"approval": approval} if approval else {}),
        "failure": failure,
        "notices": notices,
        "linkedEvidence": {
            "awsProvisioning": "honua-release#129",
            "genuineModelCanary": "honua-release#161",
        },
    }


def validate_receipt(receipt: dict[str, Any], schema_path: Path) -> None:
    """Fail closed if the emitted receipt does not satisfy its own schema."""
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - CI installs jsonschema
        raise GateError("jsonschema is required to validate the terminal journey receipt") from exc
    jsonschema.validate(receipt, json.loads(schema_path.read_text()))


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------
def run_live(
    target: dict[str, Any],
    manifest: dict[str, Any],
    journey: dict[str, Any],
    workdir: Path,
    base_url_override: str | None,
    keep_stack: bool,
    *, tenant_signing_key: str | None = None,
) -> tuple[pins.ClientWorkspace, list[stagelib.StageResult], list[str], str | None]:
    notices: list[str] = []
    import discovery

    discovery.mark_step("resolve pinned client workspace")
    workdir = workdir.resolve()
    cloud = target.get("kind") in local_fixture.CLOUD_KINDS
    if cloud and base_url_override is None:
        raise GateError("a cloud target is externally provisioned; pass its endpoint as the base URL")
    compose_cfg = target.get("compose") or {}
    notices.append(compose_cfg.get("notes") or target.get("notes") or "externally managed target")

    workspace = pins.resolve_client_workspace(manifest, workdir / "clients")
    bindir: Path | None = None
    if workspace.status == "pass":
        bindir, install_detail, install_notes = pins.install_executables(workspace, workdir / "install")
        workspace.install_notes = install_notes
        notices.extend(install_notes)
        if bindir is None:
            notices.append(f"pinned client executables unavailable: {install_detail}")
    else:
        notices.append(f"pinned client artifacts could not be consumed: {workspace.reason}")

    def workspace_blockers(number: int) -> list[str]:
        if workspace.status != "pass":
            return [workspace.reason or "pinned client artifacts were not consumed"]
        return workspace.missing_for_stage(number)

    server = manifest["components"]["honua-server"]
    image_ref = f"{server['image']}@{server['digest']}"
    compose = None
    if base_url_override is None:
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
    else:
        base_url = base_url_override

    running_image: str | None = None
    discovery.mark_step("start or reach the candidate stack")
    try:
        if base_url_override is None:
            result = compose.up()
            if result.returncode != 0:
                notices.append(
                    "the pinned candidate stack did not come up: "
                    + (result.stderr or result.stdout).strip().splitlines()[-1][:300]
                )
            else:
                running_image = image_ref
        else:
            running_image = None
            notices.append(f"using an externally managed stack at {base_url_override}")

        observation = observe(target, base_url, workspace, bindir, running_image, server["sha"])
        if observation.setup_discovery is not None:
            discovery_path = workdir / "setup-discovery.json"
            discovery_path.parent.mkdir(parents=True, exist_ok=True)
            discovery_path.write_text(json.dumps(observation.setup_discovery, indent=2) + "\n", encoding="utf-8")
            notices.append(f"Setup discovery sidecar: {discovery_path}; SHA-256: {_sha256_file(discovery_path)}")
        if observation.proxy_note:
            notices.append(observation.proxy_note)
        discovery.mark_step("evaluate observed stages")
        results = stagelib.run_stages(journey, observation, workspace_blockers)
        import executor
        import sdk
        from transport import ExecutionError, Transport

        discovery.mark_step("resolve journey principals")
        try:
            # Local Docker mints against the stack it started; a cloud cell mints through its own
            # admin API with the cell's bootstrap credential (cloud_target).
            credentials = local_fixture.credentials(target, workdir, base_url,
                mint=(base_url_override is None or cloud) and observation.ready,
                **({"tenant_signing_key": tenant_signing_key} if tenant_signing_key is not None else {}))
            if cloud and observation.ready:
                notices.append("Journey principals: " + ", ".join(sorted(
                    name for name in local_fixture.GRANTS if credentials.get(name)
                    and not (target.get("tenantFixture") and name == "proposer")))
                    + " minted through the cell admin API (short-lived; revoked when the run ends)")
                if target.get("tenantFixture"):
                    notices.append("Proposer and other-tenant use the run-owned signed issuer with identical subject and role and distinct tenants")
        except (ExecutionError, KeyError, ValueError, TypeError, AttributeError) as exc:
            credentials = {"proposer": ""}
            notices.append("Journey principal fixture could not establish verified short-lived grants")
        if cloud:
            topology = local_fixture.replica_topology(target)
            if topology is not None:
                notices.append(f"Cross-replica read: serving topology {topology['id']}: "
                               f"{topology['description']}. The replica-map check re-reads the saved "
                               f"map through the cell endpoint and records each answering instance's "
                               f"{local_fixture.INSTANCE_HEADER} header (honua-server#5790); its "
                               "verdict and the instances it observed are in that check's detail.")
            for name, reason in sorted((target.get("unavailablePrincipals") or {}).items()):
                notices.append(f"Principal {name} unavailable on this target: {reason.get('reason')}")
        credentials.setdefault("proposer", probes.resolve_env_default(
            target["adminPassword"]["env"], target["adminPassword"]["default"]))
        state = {"workspaceId": workdir.name, "workdir": str(workdir)}
        if observation.setup_view_present and workspace.status == "pass":
            discovery.mark_step("prepare published SDK")
            try:
                state["sdkBinding"] = sdk.prepare(manifest, workdir)
            except ExecutionError as exc:
                notices.append(f"Published SDK preparation: {exc.command}: {exc.reason}")
        discovery.mark_step("open candidate transport")
        transport = Transport(base_url, bindir / "honua-mcp-proxy" if bindir else None,
                              bindir / "honua" if bindir else None, workdir, credentials)
        execution = executor.JourneyExecutor(state, target, observation, transport, manifest=manifest)
        results[2:] = [stagelib.merge_execution(original, executed)
                       for original, executed in zip(results[2:], execution.run_build(), strict=True)]
    finally:
        if cloud:
            # Best effort: the cell outlives this attempt, so revoke what this run minted. The
            # revocation exchanges must not replace the diagnostics of a step that raised.
            trace = dict(discovery.TRACE)
            try:
                revoked, failed = local_fixture.revoke(target, workdir, base_url)
            finally:
                discovery.TRACE.update(trace)
            if revoked or failed:
                notices.append(f"Journey principals revoked: {len(revoked)} of {len(revoked) + len(failed)}"
                               + (f"; not revoked (they expire within an hour): {', '.join(failed)}" if failed else ""))
            local_fixture.cleanup(workdir)
        if compose is not None and not keep_stack:
            compose.down()
            local_fixture.cleanup(workdir)

    return workspace, results, notices, running_image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--policy", type=Path, help="retired: the 396/385/11 roster fixture no longer gates (#785)")
    parser.add_argument("--journey", type=Path, default=HERE / "journey.v1.json")
    parser.add_argument("--schema", type=Path, default=HERE / "receipt.schema.json")
    parser.add_argument("--rest-roster", type=Path, help="server docs/gis/data/admin-openapi-operation-ids.json")
    parser.add_argument("--mcp-roster", type=Path, help="server docs/gis/data/admin-mcp-projection-manifest.json")
    parser.add_argument("--roster-source-sha", help="honua-server sha the exports were read at; must be the image's source")
    parser.add_argument("--mode", choices=["build", "live"], default="build")
    parser.add_argument("--target", type=Path, help="target config, e.g. certification/terminal-journey/targets/local-docker.json")
    parser.add_argument("--workdir", type=Path, help="scratch directory for pinned client materialization")
    parser.add_argument("--base-url", help="use an already-running candidate stack instead of managing compose")
    parser.add_argument("--keep-stack", action="store_true", help="leave the compose stack up after the run")
    parser.add_argument("--no-validate", action="store_true", help="skip receipt schema validation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-uri", required=True)
    args = parser.parse_args()

    try:
        import discovery

        journey = load(args.journey)
        rest = load(args.rest_roster) if args.rest_roster else None
        mcp = load(args.mcp_roster) if args.mcp_roster else None
        manifest = yaml.safe_load(args.manifest.read_text())
        source = roster_source(args.rest_roster, args.mcp_roster, args.roster_source_sha, manifest)
        discovery.reset_calls()

        if args.mode == "live":
            if args.target is None:
                raise GateError("--mode live requires --target")
            target = load(args.target)
            workdir = args.workdir or Path.cwd() / ".terminal-journey"
            workspace, results, notices, _image = run_live(
                target, manifest, journey, workdir, args.base_url, args.keep_stack
            )
            # Judged after the run: every Admin operation the journey called must be in the roster.
            roster = roster_verdict(rest, mcp, discovery.calls(), source=source)
            receipt = build_receipt(
                manifest=manifest,
                journey=journey,
                roster=roster,
                evidence_uri=args.evidence_uri,
                mode="live",
                target=target,
                target_path=args.target.resolve(),
                target_base_url=args.base_url or f"http://127.0.0.1:{int(target['compose']['port'])}",
                workspace=workspace,
                stage_results=results,
                notices=notices,
            )
        else:
            roster = roster_verdict(rest, mcp, [], source=source)
            receipt = build_receipt(
                manifest=manifest,
                journey=journey,
                roster=roster,
                evidence_uri=args.evidence_uri,
                mode="build",
                target=None,
                target_path=None,
                target_base_url=None,
                workspace=pins.ClientWorkspace(
                    status="blocked",
                    root=None,
                    reason="build mode does not consume the pinned client artifacts",
                ),
                stage_results=None,
                notices=["build mode materializes the stage contract; it never observes a target"],
            )

        if not args.no_validate:
            validate_receipt(receipt, args.schema)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))

        if receipt["status"] == "fail":
            failure = receipt["failure"]
            print(
                f"\nterminal journey FAILED at stage {failure['number']} "
                f"({failure['stage']}): {failure['check']} — {failure['detail']}\n"
                f"  command/tool: {failure['command']}",
                file=sys.stderr,
            )
            return 1
        return 0
    except GateError as exc:
        print(f"terminal journey gate error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
