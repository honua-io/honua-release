#!/usr/bin/env python3
"""Build/check the deterministic terminal journey and control-plane roster receipts."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
ROOT = HERE.parents[1]


class GateError(RuntimeError):
    pass


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


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
    return {"status": "fail" if problems else "pass", "problems": problems, "counts": {"restCliOperations": len(set(rest_ids)), "mcpProjections": len(set(projected)), "mcpExclusions": len(set(excluded))}}


def build_receipt(manifest: dict[str, Any], journey: dict[str, Any], roster: dict[str, Any], evidence_uri: str) -> dict[str, Any]:
    server = manifest["components"]["honua-server"]
    artifacts = manifest.get("clientArtifacts", {})
    stages = []
    for stage in journey["stages"]:
        stages.append({
            "number": stage["number"], "stage": stage["id"], "command": stage["command"],
            "status": "blocked", "blockedBy": stage["blockedBy"],
            "operationId": None, "policyDecisionId": None, "approvalId": None,
            "actuatorId": None, "verificationId": None,
            "evidence": {"uri": evidence_uri, "source": "harness-build", "freshness": "unverified", "completeness": "incomplete"},
        })
    return {
        "schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "evidenceKey": journey["evidenceKey"], "release": manifest["platformRelease"],
        "clientArtifacts": {name: {k: pin.get(k) for k in ("package", "version", "integrity", "digest", "sourceSha")} for name, pin in artifacts.items() if name in {"honua-sdk-js", "honua-mcp-server"}},
        "server": {"sourceSha": server["sha"], "image": f"{server['image']}@{server['digest']}"},
        "fixtureRevision": "terminal-zero-to-map-v1", "configRevision": "terminal-profiles-v1", "authPolicyRevision": "terminal-separate-principals-v1",
        "roster": roster, "status": "blocked", "stages": stages,
        "linkedEvidence": {"awsProvisioning": "honua-release#129", "genuineModelCanary": "honua-release#161"}
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--policy", type=Path, default=HERE / "control-plane-roster.v1.json")
    parser.add_argument("--journey", type=Path, default=HERE / "journey.v1.json")
    parser.add_argument("--rest-roster", type=Path)
    parser.add_argument("--mcp-roster", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-uri", required=True)
    args = parser.parse_args()
    try:
        policy, journey = load(args.policy), load(args.journey)
        rest = load(args.rest_roster) if args.rest_roster else None
        mcp = load(args.mcp_roster) if args.mcp_roster else None
        roster = roster_verdict(policy, rest, mcp)
        manifest = yaml.safe_load(args.manifest.read_text())
        receipt = build_receipt(manifest, journey, roster, args.evidence_uri)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
        return 0 if roster["status"] in {"pass", "blocked"} else 1
    except GateError as exc:
        print(f"terminal journey gate error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
