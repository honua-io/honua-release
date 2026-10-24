"""Authored map-to-app draft composition and read-only review, never deployment.

The positive uses a real canonical map response and the actual generated buffer
artifact from the existing independently rejudged execution journey. The app
factory preserves opaque artifact references; package review checks declared
requirements, not execution or resolution of that opaque package payload.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_execute_plan_validation as execution
import mcp_map_package_validation as composition
from mcp_render_validation import negative_output

OPERATIONS = ["tools/call:create_app_package", "resources/read:App package",
              "tools/call:validate_package", "tools/call:preview_package"]
SCHEMA = "honua.mcp-app-package-scenario/v1"


def prerequisites(fixture, bootstrap, value):
    if not isinstance(value, dict) or set(value) != {"execution", "map"}:
        raise ValueError("app requires actual execution and map prerequisites")
    judged = execution.judge(fixture, bootstrap, value["execution"])
    mapping = composition.scenario(fixture, bootstrap)
    case = value["map"]
    expected = mapping["cases"][0]
    if (not isinstance(case, dict) or set(case) != {"id", "arguments", "authenticated", "result", "wire", "client"}
            or any(not composition.same_json(case[k], expected[k]) for k in expected)):
        raise ValueError("app map prerequisite differs from authored real-layer composition")
    frame, _ = composition.observed_wire(case["wire"], "tools/call",
        {"name": mapping["tool"], "arguments": expected["arguments"]}, True)
    if "error" in frame or not composition.same_json(case["result"], frame["result"]):
        raise ValueError("map prerequisite differs from actual response")
    composition.client_observation(case["client"], frame, case["wire"], judged["canonicalClient"], judged["clientVersion"])
    mapped = composition.created_package(case["result"], expected["arguments"])
    results = [c for c in value["execution"]["calls"] if c["id"] == "restarted-results"]
    if len(results) != 1:
        raise ValueError("app requires exactly one rejudged real execution artifact")
    package = composition.strict_json(results[0]["result"]["contents"][0]["text"])
    return judged, mapped["packageId"], package["artifacts"][0]["artifactId"]


def scenario(fixture, bootstrap, value):
    judged, map_id, artifact_id = prerequisites(fixture, bootstrap, value)
    arguments = {"templateId": "sdkreg-authored-app-template", "targetSdk": "honua-sdk-js",
        "mapPackageId": map_id, "boundArtifactIds": [artifact_id],
        "runtimeConfig": {"title": "Authored map and buffer", "showLegend": True,
            "initialPanel": "map", "bufferArtifactId": artifact_id}}
    invalid = copy.deepcopy(arguments); invalid["runtimeConfig"] = ["not-an-object"]
    duplicate = copy.deepcopy(arguments); duplicate["boundArtifactIds"].append(artifact_id)
    return {k: judged[k] for k in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256")} | {
        "schema": SCHEMA, "operations": OPERATIONS, "canonicalClient": judged["canonicalClient"],
        "clientVersion": judged["clientVersion"], "cases": [
            {"id": "authored-app", "arguments": arguments, "authenticated": True},
            {"id": "invalid-runtime-config", "arguments": invalid, "authenticated": True},
            {"id": "duplicate-artifact", "arguments": duplicate, "authenticated": True},
            {"id": "anonymous-app", "arguments": copy.deepcopy(arguments), "authenticated": False}]}


def created_package(result, arguments):
    output = composition.json_tool_output(result)
    identifier = output.get("packageId")
    if (output.get("status") != "created" or not isinstance(identifier, str)
            or not re.fullmatch(r"app_[A-Za-z0-9_-]{1,128}", identifier)
            or output.get("resourceUri") != "honua://app-packages/" + identifier or output.get("warnings") != []):
        raise ValueError("app creation lacks addressable created draft identity")
    package = output.get("package")
    if (not isinstance(package, dict) or package.get("appPackageId") != identifier
            or package.get("format") != "honua_app_package.v1" or package.get("status") != "Draft"
            or any(not composition.same_json(package.get(k), arguments[k]) for k in ("templateId", "targetSdk", "mapPackageId", "runtimeConfig"))
            or package.get("boundArtifacts") != arguments["boundArtifactIds"]
            or not composition.same_json(package.get("sharePolicy"), {"visibility": "private", "embed": False, "reviewed": False})
            or package.get("generatedFiles") != [] or package.get("assetManifest") != []
            or any(package.get(k) is not None for k in ("entryPoint", "bundleArtifactId", "manifestArtifactId"))):
        raise ValueError("app ignored authored composition or widened closed draft posture")
    aware_timestamp(package.get("createdAt"))
    return output


def aware_timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
        raise ValueError("package result lacks aware timestamp")


def review_cases(created):
    # Exact actual package bytes are sent for review. The canonical generic
    # adapter does not resolve/execute them; assertions concern its declared
    # requirements, approval decisions and read-only plan only.
    base = {"contractVersion": "honua.package_review.v1", "packageFamily": "app_package",
        "packageId": created["packageId"], "format": "honua_app_package.v1",
        "requestedAction": "publish", "packagePayload": created["package"],
        "resourceRefs": {"app": created["resourceUri"]}, "requirements": {"dataBindings": [
            {"id": "authored-map", "sourceId": created["package"]["mapPackageId"], "path": "/mapPackageId", "required": True, "isResolved": True},
            {"id": "authored-buffer", "sourceId": created["package"]["boundArtifacts"][0], "path": "/boundArtifacts/0", "required": True, "isResolved": True}]},
        "includePassFindings": False}
    blocked = copy.deepcopy(base)
    blocked["requirements"]["approvals"] = [{"required": True, "approved": False,
        "appliesTo": "publish", "path": "/sharePolicy", "policyRef": "sdkreg-explicit-approval"}]
    return [{"id": name, "tool": tool, "arguments": copy.deepcopy(args), "authenticated": auth}
        for tool in ("honua_validate_package", "honua_preview_package")
        for name, args, auth in (("ready", base, True), ("approval-required", blocked, True), ("anonymous-review", base, False))]


def review_output(result, expected):
    value = composition.json_tool_output(result)
    args = expected["arguments"]; blocked = expected["id"] == "approval-required"
    if (value.get("contractVersion") != "honua.package_review.v1" or value.get("packageFamily") != "app_package"
            or value.get("packageId") != args["packageId"] or not re.fullmatch(r"prv_[a-f0-9]{32}", value.get("reviewId", ""))
            or value.get("status") != ("blocked" if blocked else "ready")
            or value.get("canExecute") is not True or value.get("canPublish") is not (not blocked)
            or value.get("requiresApproval") is not blocked or value.get("resourceRefs") != args["resourceRefs"]
            or value.get("links") != {} or value.get("estimate") is not None):
        raise ValueError("package review changed identity, declared requirement or approval decision")
    aware_timestamp(value.get("checkedAt"))
    findings = value.get("findings")
    if not blocked:
        if findings != []:
            raise ValueError("ready review contains unexpected findings")
    else:
        if not isinstance(findings, list) or len(findings) != 1:
            raise ValueError("approval review lacks exact blocker")
        finding = findings[0]
        if (any(finding.get(k) != v for k, v in {"code": "approval_required", "severity": "blocker",
                "category": "approval", "disposition": "requires_approval", "appliesTo": "publish"}.items())
                or finding.get("requiredAction", {}).get("actionKind") != "obtain_approval"
                or finding.get("requiredAction", {}).get("targetPath") != "/sharePolicy"
                or not isinstance(finding.get("message"), str) or not finding["message"]
                or not isinstance(finding.get("evidence"), list) or len(finding["evidence"]) != 1
                or any(finding["evidence"][0].get(k) != v for k, v in {"kind": "approval", "expected": "approved",
                    "actual": "not_approved", "path": "/sharePolicy", "policyRef": "sdkreg-explicit-approval"}.items())):
            raise ValueError("approval blocker lacks exact actionable semantic evidence")
    plan = value.get("previewPlan")
    expected_plan = {"planId": "preview:app_package:" + args["packageId"], "mayMutatePublishedState": False,
        "operations": [{"operationId": "review", "operationKind": "app_package",
            "inputRefs": [item["sourceId"] for item in args["requirements"]["dataBindings"]],
            "outputArtifactKinds": ["app_package"], "dependsOn": [], "mayMutatePublishedState": False, "requiredCapabilities": []}]}
    if expected["tool"] == "honua_preview_package":
        if not composition.same_json(plan, expected_plan):
            raise ValueError("preview is not the exact read-only generic review plan")
    elif plan is not None:
        raise ValueError("validate unexpectedly produced a preview plan")


def judge(fixture, bootstrap, observed):
    if not isinstance(observed, dict) or "prerequisites" not in observed:
        raise ValueError("app observations missing prerequisites")
    authored = scenario(fixture, bootstrap, observed["prerequisites"])
    fields = set(authored) | {"prerequisites", "runtimeBefore", "runtimeAfter", "reads", "reviews"}
    if set(observed) != fields or any(not composition.same_json(observed[k], v) for k, v in authored.items() if k != "cases"):
        raise ValueError("app observations differ from authored candidate/client/scenario")
    runtime = observed["prerequisites"]["execution"]["runtimeAfter"]
    if observed["runtimeBefore"] != runtime or observed["runtimeAfter"] != runtime:
        raise ValueError("candidate changed after execution prerequisite or during app journey")
    canonical, version = authored["canonicalClient"], authored["clientVersion"]
    sessions = {c["wire"]["initialize"]["response"]["sessionSha256"] for c in observed["prerequisites"]["execution"]["calls"]}
    mapping = observed["prerequisites"]["map"]
    map_session = mapping["wire"]["initialize"]["response"]["sessionSha256"]
    if map_session in sessions:
        raise ValueError("map prerequisite reused execution session")
    sessions.add(map_session)
    fingerprints = {mapping["wire"]["expectedCredentialFingerprint"]}
    # Execution has its own private random salt; compare credentials within this
    # journey by its salt-bound fingerprint, never infer identity across salts.
    def call(actual, method, params, auth):
        frame, session = composition.observed_wire(actual["wire"], method, params, auth)
        if session in sessions or not composition.same_json(actual["result"], frame if "error" in frame else frame["result"]):
            raise ValueError("app response/session differs from actual raw wire")
        sessions.add(session); fingerprints.add(actual["wire"]["expectedCredentialFingerprint"])
        composition.client_observation(actual["client"], frame, actual["wire"], canonical, version)
        return actual["result"]
    if not isinstance(observed["cases"], list) or len(observed["cases"]) != len(authored["cases"]):
        raise ValueError("missing app composition cases")
    created = None
    for expected, actual in zip(authored["cases"], observed["cases"], strict=True):
        if set(actual) != set(expected) | {"wire", "client", "result"} or any(not composition.same_json(actual[k], v) for k, v in expected.items()):
            raise ValueError("app invocation differs from authored arguments")
        result = call(actual, "tools/call", {"name": "honua_create_app_package", "arguments": expected["arguments"]}, expected["authenticated"])
        if expected["id"] == "authored-app": created = created_package(result, expected["arguments"])
        elif expected["authenticated"]: negative_output(result, "invalid_argument")
        else: composition.anonymous_tool_refusal(result)
    uri, identifier = created["resourceUri"], created["packageId"]
    expected_reads = [("fresh-created", uri, True), ("unknown-package", uri + "-uncreated", True), ("anonymous-created", uri, False)]
    if not isinstance(observed["reads"], list) or len(observed["reads"]) != 3:
        raise ValueError("app requires fresh, unknown and anonymous resource reads")
    for actual, (name, selected, auth) in zip(observed["reads"], expected_reads, strict=True):
        if set(actual) != {"id", "uri", "authenticated", "wire", "client", "result"} or (actual["id"], actual["uri"], actual["authenticated"]) != (name, selected, auth):
            raise ValueError("app resource invocation differs")
        result = call(actual, "resources/read", {"uri": selected}, auth)
        if name == "fresh-created":
            composition.resource_output(result, uri, identifier, "app_package")
        else: composition.resource_refusal(result, -32002 if auth else -32000, "not_found" if auth else "unauthenticated")
    expected_reviews = review_cases(created)
    if not isinstance(observed["reviews"], list) or len(observed["reviews"]) != len(expected_reviews):
        raise ValueError("app requires ready, blocked and anonymous reviews for each tool")
    for expected, actual in zip(expected_reviews, observed["reviews"], strict=True):
        if set(actual) != set(expected) | {"wire", "client", "result"} or any(not composition.same_json(actual[k], v) for k, v in expected.items()):
            raise ValueError("review invocation did not use actual created package and authored requirements")
        result = call(actual, "tools/call", {"name": expected["tool"], "arguments": expected["arguments"]}, expected["authenticated"])
        if expected["authenticated"]: review_output(result, expected)
        else: composition.anonymous_tool_refusal(result)
    if len(fingerprints) != 1:
        raise ValueError("app/map probes changed expected private credential")
    return {k: authored[k] for k in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient", "clientVersion")} | {
        "operations": OPERATIONS, "positive": True, "negative": True, "mediaSchema": True, "freshReadback": True,
        "scratchOnly": True, "anonymousInvocationRefusal": True, "readOnlyPreview": True, "realMapAndArtifact": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--prerequisites", type=Path); parser.add_argument("--created", type=Path); parser.add_argument("--observations", type=Path)
    parser.add_argument("--map-scenario", action="store_true")
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), composition.strict_json(args.bootstrap.read_bytes())
        if args.map_scenario: output = composition.scenario(fixture, bootstrap)["cases"][0]
        elif args.observations: output = judge(fixture, bootstrap, composition.strict_json(args.observations.read_bytes()))
        else:
            authored = scenario(fixture, bootstrap, composition.strict_json(args.prerequisites.read_bytes()))
            output = review_cases(created_package(composition.strict_json(args.created.read_bytes()), authored["cases"][0]["arguments"])) if args.created else authored
        print(json.dumps(output, sort_keys=True, allow_nan=False))
    except (KeyError, TypeError, ValueError, OSError):
        parser.exit(1, "app package evidence refused\n")


if __name__ == "__main__":
    main()
