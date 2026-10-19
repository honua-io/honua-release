"""Authored MCP plan inputs and pure oracles shared by installed client producers.

This module has no transport, environment, server, or SDK dependency. Its CLI
prints requests or judges retained tool results; it never executes a plan.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct

SCHEMA = "honua.mcp-plan-validation-scenario/v1"


def buffer_plan(processes):
    """The existing installed-client metric buffer, without a submission key."""
    x, y = processes["point"]
    wkb = base64.b64encode(struct.pack("<BIdd", 1, 1, x, y)).decode()
    return {"planId": "sdkreg-mcp-buffer", "intentId": "sdkreg-mcp-buffer", "outputs": ["FeatureLayer"],
            "steps": [{"stepId": "buffer", "kind": "Geoprocess", "processId": processes["processId"], "inputs": {
                "wkb": wkb, "srid": str(processes["srid"]), "distance": str(processes["distance"]),
                "geodesic": "false"}}]}


def oracle_mcp_plan_valid(observed):
    ok = (observed.get("isExecutable") is True and observed.get("requiresApproval") is False
          and observed.get("violations") == [])
    return ok, "bounded buffer must validate without violations or approval"


def oracle_mcp_plan_invalid(observed):
    violations = observed.get("violations")
    actionable = isinstance(violations, list) and any(
        isinstance(v, dict) and v.get("code") == "INVALID_PARAMETER_VALUE"
        and v.get("fieldPath") == "steps[buffer].inputs.wkb"
        and isinstance(v.get("message"), str) and "base64" in v["message"].lower()
        for v in violations)
    return observed.get("isExecutable") is False and actionable, "invalid WKB must be refused with an actionable parameter violation"


def scenario(fixture_bytes):
    fixture = json.loads(fixture_bytes)
    plan = buffer_plan(fixture["processes"])
    invalid = copy.deepcopy(plan)
    invalid["steps"][0]["inputs"]["wkb"] = "not-base64"
    return {"schema": SCHEMA, "fixtureSha256": "sha256:" + hashlib.sha256(fixture_bytes).hexdigest(),
            "operation": "tools/call:validate_plan", "tool": "honua_validate_plan", "cases": [
                {"id": "valid", "arguments": {"plan": plan}},
                {"id": "invalid-wkb", "arguments": {"plan": invalid}}]}


def tool_output(result):
    if not isinstance(result, dict) or result.get("isError", False) is not False:
        raise ValueError("plan validation call did not return a successful tool envelope")
    outputs = []
    if "structuredContent" in result:
        outputs.append(result["structuredContent"])
    if "content" in result:
        content = result["content"]
        if not isinstance(content, list):
            raise ValueError("plan validation content is not an array")
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise ValueError("plan validation contains unrelated output media")
            outputs.append(json.loads(item["text"]))
    if not outputs:
        raise ValueError("plan validation lacks structured or JSON text output")
    for output in outputs:
        # Matches ValidatePlanOutputSchema and ValidationViolationDef, including
        # required fields on both the executable and rejected-plan responses.
        if (not isinstance(output, dict) or type(output.get("isExecutable")) is not bool
                or type(output.get("requiresApproval")) is not bool
                or not isinstance(output.get("violations"), list)
                or not isinstance(output.get("warnings"), list)
                or any(not isinstance(warning, str) for warning in output["warnings"])
                or any(not isinstance(violation, dict)
                       or not isinstance(violation.get("code"), str)
                       or not isinstance(violation.get("message"), str)
                       or (violation.get("fieldPath") is not None and not isinstance(violation["fieldPath"], str))
                       for violation in output["violations"])):
            raise ValueError("plan validation output has an invalid schema")
    canonical = [json.dumps(output, sort_keys=True, allow_nan=False) for output in outputs]
    if any(value != canonical[0] for value in canonical[1:]):
        raise ValueError("plan validation output representations conflict")
    return outputs[0]


def judge(fixture_bytes, observations):
    authored = scenario(fixture_bytes)
    base_fields = {"schema", "fixtureSha256", "cases"}
    if not isinstance(observations, dict) or set(observations) not in (base_fields, base_fields | {"operation", "tool"}):
        raise ValueError("plan observations must contain the bound scenario and both cases")
    if observations["schema"] != SCHEMA or observations["fixtureSha256"] != authored["fixtureSha256"]:
        raise ValueError("plan observations differ from the authored fixture")
    if any(key in observations and observations[key] != authored[key] for key in ("operation", "tool")):
        raise ValueError("plan observation metadata differs from the authored operation")
    cases = observations["cases"]
    if not isinstance(cases, list) or len(cases) != 2:
        raise ValueError("plan observations are missing or duplicated")
    for expected, actual, oracle in zip(authored["cases"], cases, (oracle_mcp_plan_valid, oracle_mcp_plan_invalid), strict=True):
        if not isinstance(actual, dict) or set(actual) != {"id", "arguments", "result"} or actual["id"] != expected["id"] or actual["arguments"] != expected["arguments"]:
            raise ValueError("observed plan request differs from the independent authored case")
        output = tool_output(actual["result"])
        passed, summary = oracle(output)
        if not passed:
            raise ValueError(summary)
        # The authored pair changes only WKB: an additional diagnosis of an
        # unchanged field must not hide behind the expected WKB violation.
        if expected["id"] == "invalid-wkb" and any(
                not oracle_mcp_plan_invalid({**output, "violations": [violation]})[0]
                for violation in output["violations"]):
            raise ValueError("invalid WKB produced an unrelated validation violation")
    return {"schema": SCHEMA, "fixtureSha256": authored["fixtureSha256"],
            "positive": True, "negative": True, "mediaSchema": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture = args.fixture.read_bytes()
        output = judge(fixture, json.loads(args.observations.read_bytes())) if args.observations else scenario(fixture)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(1, f"plan validation refused: {type(exc).__name__}\n")
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
