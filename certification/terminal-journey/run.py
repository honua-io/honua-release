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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import pins  # noqa: E402
import probes  # noqa: E402
import stages as stagelib  # noqa: E402


class GateError(RuntimeError):
    pass


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Control-plane roster gate (unchanged contract from the #121 landing)
# ---------------------------------------------------------------------------
def validate_policy(policy: dict[str, Any]) -> None:
    expected = policy.get("expected", {})
    exclusions = policy.get("exclusions", [])
    if expected != {"restCliOperations": 396, "mcpProjections": 385, "mcpExclusions": 11}:
        raise GateError("control-plane cardinality policy drifted from 396 REST/CLI, 385 MCP, 11 exclusions")
    if len(exclusions) != 11 or len({row.get("id") for row in exclusions}) != 11:
        raise GateError("the 11 MCP exclusions must be individually named and unique")
    for row in exclusions:
        if row.get("class") not in {"one-time-secret", "session"} or row.get("sink") != "private-cli":
            raise GateError(f"{row.get('id')}: exclusion must be secret/session class with private-cli sink")
    if expected["restCliOperations"] - expected["mcpProjections"] != expected["mcpExclusions"]:
        raise GateError("REST/CLI minus MCP must equal the audited exclusion count")


def roster_verdict(policy: dict[str, Any], rest: dict[str, Any] | None, mcp: dict[str, Any] | None) -> dict[str, Any]:
    validate_policy(policy)
    if rest is None or mcp is None:
        upstream = policy["upstreamRoster"]
        return {"status": "blocked", "reason": "authoritative candidate roster exports unavailable", **upstream}
    rest_ids = rest.get("operationIds", [])
    projected = mcp.get("projectedOperationIds", [])
    excluded = mcp.get("exclusions", [])
    audited_exclusions = {row["id"] for row in policy["exclusions"]}
    problems = []
    if len(rest_ids) != 396 or len(set(rest_ids)) != 396:
        problems.append(f"REST/CLI roster has {len(set(rest_ids))} unique operations, expected 396")
    if len(projected) != 385 or len(set(projected)) != 385:
        problems.append(f"MCP roster has {len(set(projected))} unique projections, expected 385")
    if len(excluded) != 11 or len(set(excluded)) != 11:
        problems.append(f"MCP manifest has {len(set(excluded))} unique exclusions, expected 11")
    if set(excluded) != audited_exclusions:
        missing = sorted(audited_exclusions - set(excluded))
        unexpected = sorted(set(excluded) - audited_exclusions)
        problems.append(
            f"MCP exclusions differ from audited policy IDs (missing={missing}, unexpected={unexpected})"
        )
    if set(projected) & set(excluded):
        problems.append("an operation is both projected and excluded")
    if set(projected) | set(excluded) != set(rest_ids):
        problems.append("MCP projections plus exclusions do not exactly partition REST/CLI operations")
    return {
        "status": "fail" if problems else "pass",
        "problems": problems,
        "counts": {
            "restCliOperations": len(set(rest_ids)),
            "mcpProjections": len(set(projected)),
            "mcpExclusions": len(set(excluded)),
        },
    }


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

    proxy = bindir / "honua-mcp-proxy" if bindir else None
    if proxy is None or not proxy.exists():
        observation.proxy_detail = (
            "the pinned honua-mcp-proxy executable was not installable from the verified bytes"
        )
        observation.tools_error = observation.proxy_detail
        return observation

    observation.proxy_available = True
    names, error, note = probes.enumerate_tools(proxy, base_url + endpoints["mcp"])
    observation.proxy_note = note
    if error is not None:
        observation.tools_error = error
        return observation
    observation.tool_names = names

    # A bounded server-authored setup view is a named discovery view, not merely a
    # short tool list. Absent a negotiated view identity we must not claim one.
    observation.setup_view_present = False
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
    workspace: pins.ClientWorkspace,
    stage_results: list[stagelib.StageResult] | None,
    notices: list[str],
) -> dict[str, Any]:
    server = manifest["components"]["honua-server"]
    revisions = (target or {}).get("revisions", {})
    evidence_source = "harness-build" if mode == "build" else "live-local-docker"
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
                    "policyDecisionId": result.policy_decision_id,
                    "approvalId": result.approval_id,
                    "actuatorId": result.actuator_id,
                    "verificationId": result.verification_id,
                    "evidence": {
                        "uri": evidence_uri,
                        "source": evidence_source,
                        "freshness": freshness,
                        "completeness": completeness,
                        "observedAt": observed_at,
                    },
                }
            )

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
            "command": broken.command,
            "check": check.id if check else broken.stage,
            "detail": (check.detail if check else "stage failed") or "stage failed",
        }

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
            "baseUrl": None,
            "composeProject": ((target or {}).get("compose") or {}).get("project"),
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
) -> tuple[pins.ClientWorkspace, list[stagelib.StageResult], list[str], str | None]:
    notices: list[str] = []
    compose_cfg = target["compose"]
    notices.append(compose_cfg["notes"])

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
    port = int(compose_cfg["port"])
    base_url = base_url_override or f"http://127.0.0.1:{port}"
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

    running_image: str | None = None
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
        if observation.proxy_note:
            notices.append(observation.proxy_note)
        results = stagelib.run_stages(journey, observation, workspace_blockers)
    finally:
        if base_url_override is None and not keep_stack:
            compose.down()

    return workspace, results, notices, running_image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--policy", type=Path, default=HERE / "control-plane-roster.v1.json")
    parser.add_argument("--journey", type=Path, default=HERE / "journey.v1.json")
    parser.add_argument("--schema", type=Path, default=HERE / "receipt.schema.json")
    parser.add_argument("--rest-roster", type=Path)
    parser.add_argument("--mcp-roster", type=Path)
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
        policy, journey = load(args.policy), load(args.journey)
        rest = load(args.rest_roster) if args.rest_roster else None
        mcp = load(args.mcp_roster) if args.mcp_roster else None
        roster = roster_verdict(policy, rest, mcp)
        manifest = yaml.safe_load(args.manifest.read_text())

        if args.mode == "live":
            if args.target is None:
                raise GateError("--mode live requires --target")
            target = load(args.target)
            workdir = args.workdir or Path.cwd() / ".terminal-journey"
            workspace, results, notices, _image = run_live(
                target, manifest, journey, workdir, args.base_url, args.keep_stack
            )
            receipt = build_receipt(
                manifest=manifest,
                journey=journey,
                roster=roster,
                evidence_uri=args.evidence_uri,
                mode="live",
                target=target,
                target_path=args.target.resolve(),
                workspace=workspace,
                stage_results=results,
                notices=notices,
            )
        else:
            receipt = build_receipt(
                manifest=manifest,
                journey=journey,
                roster=roster,
                evidence_uri=args.evidence_uri,
                mode="build",
                target=None,
                target_path=None,
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
