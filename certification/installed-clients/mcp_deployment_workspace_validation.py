"""Authored primary deployment/workspace requests and independent wire semantics.

The production lifecycle writer, actual prior workflow history and a real GP
workspace are prerequisites. No synthetic succeeded record or approval is made.
"""
from __future__ import annotations
import argparse
import copy
import base64
import hashlib
import struct
from urllib.parse import parse_qs, quote
import json
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_map_package_validation as wire
from mcp_render_validation import negative_output
from mcp_execute_plan_validation import resource
from mcp_deployment_workspace_store_oracle import instant, ticks, observation, rollback_selection, deployment, workspace, proposal

OPERATIONS = ["tools/call:deploy_operations", "tools/call:propose_rollback", "resources/read:Deployment", "resources/read:Workspace"]
SCHEMA = "honua.mcp-deployment-workspace-scenario/v1"


def guid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("actual lifecycle UUID missing")
    return value


def tool(name, arguments, *, id, authenticated=True):
    return {"id": id, "method": "tools/call", "params": {"name": name, "arguments": arguments}, "authenticated": authenticated}


def read(uri, *, id, authenticated=True):
    return {"id": id, "method": "resources/read", "params": {"uri": uri}, "authenticated": authenticated}


def scenario(fixture, bootstrap, nonce, canonical):
    binding = wire.scenario(fixture, bootstrap)
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12}", nonce) or canonical not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("lifecycle requires authored nonce and actual canonical client")
    slug = "sdk" if canonical == "Official MCP TypeScript SDK" else "inspector"
    key = f"mcp-lifecycle-{slug}-{nonce}"
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "schema": SCHEMA, "operations": OPERATIONS, "nonce": nonce, "canonicalClient": canonical,
        "packageKey": key, "route": "/maps/" + key, "workspaceLabel": key}


def initial_calls(fixture, bootstrap, authored):
    mapping = wire.scenario(fixture, bootstrap)
    return [tool("honua_create_map_package", mapping["cases"][0]["arguments"], id="create-map"),
            tool("honua_studio_create_draft", {"packageKey": authored["packageKey"], "family": "map", "schemaVersion": "1.0"}, id="create-studio-draft")]


def studio_update(fixture, bootstrap, authored, mapped, created):
    mapping = wire.scenario(fixture, bootstrap)
    output = wire.created_package(mapped, mapping["cases"][0]["arguments"])
    draft = wire.json_tool_output(created)
    draft_id = guid(draft.get("draftId"))
    if type(draft.get("generation")) is not int or draft["generation"] != 1:
        raise ValueError("actual Studio draft generation differs")
    body = copy.deepcopy(output["package"])
    body.update(status="Ready", title="Authored persisted map", description="Independent published-layer composition.",
                layers=[{"id": "authored-area", "title": "Owned published area"}], basemap="streets")
    return tool("honua_studio_update_draft", {"body": body, "draftId": draft_id, "generation": 1,
                "packageKey": authored["packageKey"], "schemaVersion": "1.0"}, id="update-studio-draft")


def studio_save(updated, draft_id):
    value = wire.json_tool_output(updated)
    if value.get("draftId") != draft_id or type(value.get("generation")) is not int or value["generation"] != 2:
        raise ValueError("real update generation/readback differs")
    return [tool("honua_studio_validate_draft", {"draftId": draft_id}, id="validate-studio-draft"),
            tool("honua_studio_save_version", {"draftId": draft_id, "generation": value["generation"]}, id="save-studio-version")]


def studio_publish(saved, authored):
    value = wire.json_tool_output(saved)
    version = value.get("version")
    if not isinstance(version, dict):
        raise ValueError("actual immutable Studio version missing")
    item_id, version_id = guid(version.get("itemId")), guid(version.get("versionId"))
    content_hash = version.get("contentHash")
    if not isinstance(content_hash, str) or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", content_hash):
        raise ValueError("actual Studio version content hash missing")
    return tool("honua_studio_propose_publication", {"itemId": item_id, "versionId": version_id,
                "contentHash": content_hash, "route": authored["route"], "visibility": "public"}, id="publish-studio-version")


def published_identity(published, publication_call, map_body):
    value = wire.json_tool_output(published)
    identifier = value.get("deploymentId")
    if (value.get("status") != "Published" or not isinstance(identifier, str) or not re.fullmatch(r"dep-[a-f0-9]{32}", identifier)
            or value.get("deploymentResourceUri") != "honua://deployments/" + identifier
            or value.get("shareUrl") != "/api/v1/studio/published" + publication_call["params"]["arguments"]["route"]):
        raise ValueError("real publication returned no canonical deployment")
    arguments = publication_call["params"]["arguments"]
    return {"deploymentId": identifier, "studioItemId": arguments["itemId"], "studioVersionId": arguments["versionId"],
            "studioContentHash": arguments["contentHash"], "route": arguments["route"], "mapBody": map_body}


def workspace_prerequisite(value, authored, bootstrap, stores, *, strong=False):
    expected_fields = {"schema", "serviceId", "label", "workspaceId", "jobId", "ownerId", "scopeId", "submitted", "polls", "output", "durableJobSha256"}
    if strong:expected_fields|={"submitterSecurityContext","durableJobBytesBase64"}
    if (not isinstance(value, dict) or set(value) != expected_fields or value["schema"] != "honua.mcp-gp-workspace-prerequisite/v1"
            or value["label"] != authored["workspaceLabel"] or value["serviceId"] != bootstrap["ownedArea"]["serviceId"]
            or not isinstance(value["durableJobSha256"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["durableJobSha256"])):
        raise ValueError("real canonical GP workspace prerequisite missing")
    base = "/rest/services/" + quote(value["serviceId"], safe="") + "/GPServer/geometry.area"
    def response(entry, path, method):
        fields = {"path", "method", "requestBodyBase64", "requestBodySha256", "credentialPresent", "responseStatus", "responseBodyBase64", "responseBodySha256"}
        if strong:fields|={"credentialFingerprint","tenantId","startedAt","completedAt"}
        if not isinstance(entry, dict) or set(entry) != fields or entry["path"] != path or entry["method"] != method or entry["credentialPresent"] is not True or type(entry["responseStatus"]) is not int or entry["responseStatus"] != 200:
            raise ValueError("actual GP setup method/path/media differs")
        for body in ("request", "response"):
            raw = base64.b64decode(entry[body + "BodyBase64"], validate=True)
            if len(raw) > 2 * 1024 * 1024 or entry[body + "BodySha256"] != "sha256:" + hashlib.sha256(raw).hexdigest():
                raise ValueError("actual GP setup raw bytes differ")
        if method == "GET" and base64.b64decode(entry["requestBodyBase64"], validate=True) != b"":
            raise ValueError("unexpected GP GET payload")
        output = wire.strict_json(base64.b64decode(entry["responseBodyBase64"], validate=True))
        if not isinstance(output, dict) or "error" in output:
            raise ValueError("actual GP setup returned an error")
        return output
    submitted = response(value["submitted"], base + "/submitJob", "POST")
    encoded = base64.b64encode(struct.pack("<BIII10d", 1, 3, 1, 5, 0, 0, 3, 0, 3, 4, 0, 4, 0, 0)).decode()
    form = parse_qs(base64.b64decode(value["submitted"]["requestBodyBase64"], validate=True).decode(), strict_parsing=True, keep_blank_values=True)
    if form != {"f": ["json"], "wkb": [encoded], "srid": ["3857"], "env:workspace": [authored["workspaceLabel"]], "env:overwriteOutput": ["false"]} or submitted.get("jobId") != value["jobId"]:
        raise ValueError("actual GP setup did not execute the authored area/workspace")
    polls = value["polls"]
    if not isinstance(polls, list) or not 1 <= len(polls) <= 30:
        raise ValueError("actual GP setup lifecycle missing")
    for index, entry in enumerate(polls):
        status = response(entry, base + "/jobs/" + value["jobId"] + "?f=json", "GET")
        allowed = ("esriJobSucceeded",) if index == len(polls) - 1 else ("esriJobSubmitted", "esriJobWaiting", "esriJobExecuting", "esriJobNew")
        if status.get("jobId") != value["jobId"] or status.get("jobStatus") not in allowed:
            raise ValueError("actual GP setup lifecycle contradicts completion")
    result = response(value["output"], base + "/jobs/" + value["jobId"] + "/results/outputScalar?f=json", "GET")
    expected = {key: value[key] for key in ("workspaceId", "jobId", "ownerId", "scopeId", "label")}
    _, artifact = workspace(stores, expected)
    if result != {"paramName": "outputScalar", "dataType": "GPString", "value": artifact["uri"]}:
        raise ValueError("actual GP response and durable workspace artifact disagree")
    return expected


def primary_calls(authored, expected, history, workspace_id):
    selection = rollback_selection(history, expected["targetId"])
    deployment_uri, workspace_uri = "honua://deployments/" + expected["deploymentId"], "honua://workspaces/" + workspace_id
    rollback = {"targetId": expected["targetId"], "reason": "Authored forward rollback proposal; no approval or actuation", "idempotencyKey": authored["packageKey"] + "-rollback"}
    return [tool("honua_deploy_operations", {"operationId": selection["currentOperationId"]}, id="current-deploy"),
            tool("honua_deploy_operations", {"operationId": selection["priorOperationId"]}, id="prior-deploy"),
            tool("honua_propose_rollback", rollback, id="rollback-proposal"),
            tool("honua_propose_rollback", copy.deepcopy(rollback), id="rollback-replay"),
            read(deployment_uri, id="deployment"), read(workspace_uri, id="workspace"),
            read(deployment_uri, id="restarted-deployment"), read(workspace_uri, id="restarted-workspace"),
            read(deployment_uri, id="foreign-deployment") | {"credentialRole": "foreign"},
            read(workspace_uri, id="foreign-workspace") | {"credentialRole": "foreign"},
            tool("honua_deploy_operations", {"operationId": "uncreated-" + authored["nonce"]}, id="unknown-operation"),
            tool("honua_propose_rollback", {}, id="invalid-rollback"),
            read(deployment_uri + "-uncreated", id="unknown-deployment"), read(workspace_uri + "-uncreated", id="unknown-workspace"),
            tool("honua_deploy_operations", {"operationId": selection["currentOperationId"]}, id="anonymous-deploy", authenticated=False),
            tool("honua_propose_rollback", rollback, id="anonymous-rollback", authenticated=False),
            read(deployment_uri, id="anonymous-deployment", authenticated=False), read(workspace_uri, id="anonymous-workspace", authenticated=False) ]


def actual_call(actual, expected, canonical, version, sessions, fingerprints, *, tenant_id=None, timed=False):
    timing={"startedAt","completedAt"} if timed else set()
    if (not isinstance(actual, dict) or set(actual) != set(expected) | {"wire", "client", "result"} | timing
            or any(not wire.same_json(actual.get(key), value) for key, value in expected.items())):
        raise ValueError("actual lifecycle request differs from authored request")
    if timed and instant(actual["completedAt"])<instant(actual["startedAt"]):
        raise ValueError("actual lifecycle call time order differs")
    frame, session = wire.observed_wire(actual["wire"], expected["method"], expected["params"], expected["authenticated"],tenant_id=tenant_id)
    if session in sessions or not wire.same_json(actual["result"], frame if "error" in frame else frame["result"]):
        raise ValueError("lifecycle response altered or session reused")
    wire.client_observation(actual["client"], frame, actual["wire"], canonical, version)
    sessions.add(session)
    if expected["authenticated"]:
        fingerprints.setdefault(expected.get("credentialRole", "primary"), set()).add(actual["wire"]["expectedCredentialFingerprint"])
    return actual["result"]


class Pending(Exception):
    def __init__(self, value):
        self.value = value


def operation_completed(value):
    operation = value.get("operation") if isinstance(value, dict) else None
    if (not isinstance(operation, dict) or operation.get("status") != "Completed"
            or any(not isinstance(operation.get(key), str) or not operation[key] for key in ("operationInstanceId", "auditId", "correlationId"))):
        raise ValueError("normal durable Studio operation did not complete")


def deployment_read(result, expected, stores):
    value = resource(result, "honua://deployments/" + expected["deploymentId"])
    document = deployment(stores, expected)
    source, target = document["source"], document["target"]
    fields = {"deploymentId": expected["deploymentId"], "resourceUri": "honua://deployments/" + expected["deploymentId"],
              "status": "Active", "publicationState": "Published", "rolloutState": "Promoted", "sourceKind": "StudioPublishedVersion", "runtimeHealth": "Unknown",
              "sourceId": source["sourceId"], "targetId": target["targetId"], "targetKind": target["kind"], "hostingMode": target["hostingMode"],
              "studioItemId": source["studioItemId"], "studioVersionId": source["studioVersionId"], "studioContentHash": source["studioContentHash"],
              "studioOwnerId": source["studioOwnerId"], "studioPublicationRequestId": source["studioPublicationRequestId"],
              "routePrefix": target.get("routePrefix"), "createdAt": document["createdAt"], "updatedAt": document["updatedAt"]}
    for key, wanted in fields.items():
        if not wire.same_json(value.get(key), wanted):
            raise ValueError("Deployment primary read differs from independent publication lifecycle")
    anchor = max(ticks(document["updatedAt"]), ticks(document["transitions"][-1]["at"]))
    if value.get("etag") != f'W/"{anchor:x}-Active"':
        raise ValueError("Deployment ETag differs from persisted lifecycle ticks")
    expected_transitions = [{key: step[key] for key in ("from", "to", "at", "rolloutState", "reason") if key in step} for step in document["transitions"]]
    if not wire.same_json(value.get("transitions"), expected_transitions):
        raise ValueError("Deployment transition audit readback differs")
    return value


def workspace_read(result, expected, stores):
    value = resource(result, "honua://workspaces/" + expected["workspaceId"])
    row, _ = workspace(stores, expected)
    fields = {"workspaceId": expected["workspaceId"], "kind": "scratch", "label": expected["label"],
              "uri": row["uri"], "expiresAt": row["expires_at"], "lifecycleState": "active",
              "cleanupScheduledAt": None, "resultsUri": None, "notImplementedReason": None}
    for key, wanted in fields.items():
        if key == "expiresAt" and wanted is not None:
            if ticks(value.get(key)) != ticks(wanted):
                raise ValueError("Workspace expiry readback differs")
        elif not wire.same_json(value.get(key), wanted):
            raise ValueError("Workspace primary read is degraded or differs from actual GP lifecycle")
    return value


def deploy_tool_output(result):
    if (not isinstance(result, dict) or result.get("isError") is not False
            or set(result) - {"isError", "structuredContent", "content"}
            or not isinstance(result.get("structuredContent"), dict)
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1):
        raise ValueError("deploy operations lacks actual structured payload")
    value, item = result["structuredContent"], result["content"][0]
    if not isinstance(item, dict) or set(item) != {"type", "text"} or item["type"] != "text" or not isinstance(item["text"], str):
        raise ValueError("deploy operations has unrelated media")
    # Match SuccessJsonElement's informational large-payload projection. The
    # complete structured payload is mandatory and independently judged below.
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=True)) >= 1024:
        if item["text"] != "Deploy operations returned in structuredContent.":
            raise ValueError("deploy large-payload summary differs from actual server")
    elif not wire.same_json(wire.strict_json(item["text"]), value):
        raise ValueError("deploy inline and structured payloads differ")
    return value


def deploy_posture(value, observed_at, window):
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "status", "sources"} or value["schemaVersion"] != "1.0" or not isinstance(value["sources"], list) or len(value["sources"]) != 1:
        raise ValueError("actual deploy evidence posture missing")
    source = value["sources"][0]
    if (not isinstance(source, dict) or source.get("sourceId") != "honua_deploy_operations"
            or source.get("backendKind") != "durableStore" or source.get("backendId") != "workflow-operation-store"
            or source.get("maximumAgeSeconds") != 300
            or ticks(source.get("observedAt")) != ticks(observed_at) or ticks(source.get("lastSuccessfulAt")) != ticks(observed_at)
            or ticks(source.get("validUntil")) != ticks(observed_at) + 300 * 10_000_000):
        raise ValueError("deploy evidence source/timestamps differ from real durable observation")
    reasons, coverage = source.get("reasonCodes"), source.get("coverage")
    if not isinstance(reasons, list) or reasons != sorted(set(reasons)) or set(reasons) - {"stale", "futureObservationTime"}:
        raise ValueError("deploy evidence reason vocabulary differs")
    wanted = "unavailable" if reasons else "complete"
    if source.get("completeness") != wanted or value["status"] != wanted:
        raise ValueError("deploy evidence fabricated actionability or hid actual stale state")
    if (not isinstance(coverage, dict) or coverage.get("page") != 1 or coverage.get("pageSize") != 1
            or coverage.get("hasMore") is not False or coverage.get("truncated") is not False
            or ticks(coverage.get("returnedFrom")) != ticks(observed_at) or ticks(coverage.get("returnedTo")) != ticks(observed_at)
            or any(item is not None for key, item in coverage.items() if key not in {"page", "pageSize", "hasMore", "truncated", "returnedFrom", "returnedTo"})):
        raise ValueError("deploy lookup coverage differs from actual one-record read")
    if window is not None:
        begin, end = map(ticks, window)
        stamp, expiry = ticks(observed_at), ticks(source["validUntil"])
        for reason, mandatory, possible in (("stale", expiry < begin, expiry < end),
                                            ("futureObservationTime", stamp > end + 60 * 10_000_000, stamp > begin + 60 * 10_000_000)):
            if mandatory and reason not in reasons or reason in reasons and not possible:
                raise ValueError("deploy posture contradicts actual observation window/server skew")


def deploy_read(result, operation_id, history, window=None):
    output = deploy_tool_output(result)
    if (not isinstance(output.get("items"), list) or len(output["items"]) != 1
            or any(output.get(key) != wanted for key, wanted in (("page", 1), ("pageSize", 1), ("totalCount", 1)))
            or output.get("hasMore") is not False):
        raise ValueError("exact operation lookup media/page envelope differs")
    actual = output["items"][0]
    expected = next((item for item in history["operations"] if item["operationId"] == operation_id), None)
    if not isinstance(actual, dict) or expected is None:
        raise ValueError("primary operation was not independently observed")
    record = expected["record"]
    if any(actual.get(key) != value for key, value in {"operationId": operation_id, "kind": "Deploy", "status": "Succeeded", "providerOperationId": record["providerOperationId"]}.items()):
        raise ValueError("primary operation lifecycle/provider identity differs")
    for key in ("createdAt", "updatedAt", "completedAt"):
        if ticks(actual.get(key)) != ticks(record[key]):
            raise ValueError("primary operation lifecycle time differs")
    target = actual.get("target")
    if not isinstance(target, dict) or any(target.get(key) != value for key, value in expected["deploy"].items()):
        raise ValueError("primary operation target/revisions differ")
    deploy_posture(output.get("evidencePosture"), record["updatedAt"], window)
    return output


def advance(fixture, bootstrap, observed, *, tenant_id=None):
    fields = {"nonce", "canonicalClient", "clientVersion", "targetId", "calls", "workspacePrerequisite", "storeBefore", "storeAfter", "proposalBefore", "proposalAfter", "runtimeBefore", "runtimeRestarted", "runtimeAfter"}
    if not isinstance(observed, dict) or set(observed) != fields:
        raise ValueError("lifecycle observations missing or unknown")
    before, restarted, current = (observed[key] for key in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
    for runtime in (before, current):
        selected = dict(bootstrap); selected["runtimeBinding"] = runtime; wire.scenario(fixture, selected)
    if restarted is None:
        if current != before:
            raise ValueError("candidate changed before an authored lifecycle action")
    elif (not isinstance(restarted, dict) or before != {**restarted, "startedAt": before["startedAt"]}
          or current != restarted or instant(restarted["startedAt"]) <= instant(before["startedAt"])):
        raise ValueError("unexpected candidate replacement/restart before lifecycle action")
    authored = scenario(fixture, bootstrap, observed.get("nonce"), observed.get("canonicalClient"))
    if tenant_id is not None and tenant_id!="mcp-deployment-"+authored["nonce"]:
        raise ValueError("normal primary tenant must bind the original cohort nonce")
    version = observed.get("clientVersion")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("actual installed lifecycle client version missing")
    calls = observed.get("calls")
    if not isinstance(calls, list):
        raise ValueError("actual lifecycle calls missing")
    position, sessions, fingerprints = 0, set(), {}
    def take(expected):
        nonlocal position
        if position == len(calls):
            raise Pending({"action": "invoke", "call": expected})
        selected_tenant=tenant_id if expected["authenticated"] and expected.get("credentialRole","primary")=="primary" else None
        value = actual_call(calls[position], expected, authored["canonicalClient"], version, sessions, fingerprints,
            tenant_id=selected_tenant,timed=tenant_id is not None)
        position += 1
        return value
    try:
        initial = initial_calls(fixture, bootstrap, authored)
        mapped, created = take(initial[0]), take(initial[1])
        update = studio_update(fixture, bootstrap, authored, mapped, created)
        updated = take(update)
        validate_call, save_call = studio_save(updated, update["params"]["arguments"]["draftId"])
        validated = wire.json_tool_output(take(validate_call))
        if validated.get("status") != "valid" or validated.get("unsupportedCapabilities") != []:
            raise ValueError("real Studio map draft did not validate")
        saved = take(save_call)
        operation_completed(wire.json_tool_output(saved))
        publication_call = studio_publish(saved, authored)
        published = take(publication_call)
        published_output = wire.json_tool_output(published)
        operation_completed(published_output)
        if published_output.get("humanConfirmationRequired") is not False:
            raise ValueError("Studio publication did not use actual authorized admin-direct policy")
        expected = published_identity(published, publication_call, update["params"]["arguments"]["body"])
        if not isinstance(observed.get("workspacePrerequisite"), dict):
            raise Pending({"action": "create-workspace", "serviceId": bootstrap["ownedArea"]["serviceId"], "label": authored["workspaceLabel"]})
        prerequisite = observed["workspacePrerequisite"]
        if prerequisite.get("label") != authored["workspaceLabel"] or prerequisite.get("serviceId") != bootstrap["ownedArea"]["serviceId"]:
            raise ValueError("real workspace setup differs from owned authored fixture")
        target = observed.get("targetId")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", target):
            raise ValueError("actual owned rollback target missing")
        expected["targetId"] = target
        workspace_expected = {key: prerequisite[key] for key in ("workspaceId", "jobId", "ownerId", "scopeId", "label")}
        if not isinstance(observed.get("storeBefore"), dict):
            raise Pending({"action": "observe-stores", "slot": "storeBefore", "targetId": target,
                           "deploymentId": expected["deploymentId"], "workspaceId": workspace_expected["workspaceId"]})
        baseline = observation(observed["storeBefore"], target, expected["deploymentId"], workspace_expected["workspaceId"])
        deployment(baseline, expected); workspace_expected = workspace_prerequisite(prerequisite, authored, bootstrap, baseline,strong=tenant_id is not None)
        history = observed["storeBefore"]["succeededWorkflowHistory"]
        selection = rollback_selection(history, target)
        specs = primary_calls(authored, expected, history, workspace_expected["workspaceId"])
        positives = {}
        for spec in specs:
            if spec["id"].startswith("restarted-") and observed.get("runtimeRestarted") is None:
                raise Pending({"action": "restart"})
            value = take(spec)
            if spec["id"] in ("current-deploy", "prior-deploy"):
                deploy_read(value, spec["params"]["arguments"]["operationId"], history)
            elif spec["id"] in ("rollback-proposal", "rollback-replay"):
                output = wire.json_tool_output(value)
                if (output.get("outcome") != "ProposalCreated" or output.get("requiresApproval") is not True
                        or not isinstance(output.get("proposalId"), str) or output.get("executionOperationId") is not None
                        or output.get("resourceUri") != "honua://proposals/" + output["proposalId"]):
                    raise ValueError("primary rollback did not create only an approval-required forward proposal")
                slot = "proposalBefore" if spec["id"] == "rollback-proposal" else "proposalAfter"
                if not isinstance(observed.get(slot), dict):
                    raise Pending({"action": "observe-proposal", "slot": slot, "proposalId": output["proposalId"]})
                proposal_id = proposal(observed[slot], selection, spec["params"]["arguments"]["idempotencyKey"])
                if proposal_id != output["proposalId"]:
                    raise ValueError("returned rollback proposal differs from actual durable gateway record")
                if slot == "proposalAfter" and not wire.same_json(observed[slot], observed["proposalBefore"]):
                    raise ValueError("idempotent rollback replay changed its durable proposal")
            elif spec["id"] in ("deployment", "restarted-deployment"):
                positives[spec["id"]] = deployment_read(value, expected, baseline)
            elif spec["id"] in ("workspace", "restarted-workspace"):
                positives[spec["id"]] = workspace_read(value, workspace_expected, baseline)
            elif spec["id"].startswith("foreign-"):
                wire.resource_refusal(value, -32002, "not_found")
            elif spec["id"].startswith("anonymous-"):
                if spec["method"] == "tools/call":
                    wire.anonymous_tool_refusal(value)
                else:
                    wire.resource_refusal(value, -32000, "unauthenticated")
            elif spec["id"] == "invalid-rollback":
                negative_output(value, "invalid_argument")
            elif spec["method"] == "tools/call":
                negative_output(value, "not_found")
            else:
                wire.resource_refusal(value, -32002, "not_found")
        if position != len(calls):
            raise ValueError("unexpected extra lifecycle call")
        if not isinstance(observed.get("storeAfter"), dict):
            raise Pending({"action": "observe-stores", "slot": "storeAfter", "targetId": target,
                           "deploymentId": expected["deploymentId"], "workspaceId": workspace_expected["workspaceId"]})
        final = observation(observed["storeAfter"], target, expected["deploymentId"], workspace_expected["workspaceId"])
        for actual in calls:
            if actual["id"] in ("current-deploy", "prior-deploy"):
                deploy_read(actual["result"], actual["params"]["arguments"]["operationId"], history,
                            (observed["storeBefore"]["completedAt"], observed["storeAfter"]["startedAt"]))
        if not wire.same_json(final, baseline) or not wire.same_json(observed["storeAfter"]["succeededWorkflowHistory"], history):
            raise ValueError("persisted lifecycle/history changed across owned restart")
        before, restarted, after = (observed.get(key) for key in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
        for runtime in (before, restarted, after):
            selected = dict(bootstrap); selected["runtimeBinding"] = runtime; wire.scenario(fixture, selected)
        if before != {**restarted, "startedAt": before["startedAt"]} or after != restarted or instant(restarted["startedAt"]) <= instant(before["startedAt"]):
            raise ValueError("exact same-container deployment/workspace restart not observed")
        if not (instant(before["startedAt"]) < instant(observed["storeBefore"]["startedAt"])
                <= instant(observed["storeBefore"]["completedAt"]) <= instant(restarted["startedAt"])
                <= instant(observed["storeAfter"]["startedAt"])):
            raise ValueError("persisted store observations do not bracket actual owned restart")
        if (set(fingerprints) != {"primary", "foreign"} or any(len(values) != 1 for values in fingerprints.values())
                or fingerprints["primary"] == fingerprints["foreign"] or positives["deployment"] != positives["restarted-deployment"] or positives["workspace"] != positives["restarted-workspace"]):
            raise ValueError("persisted primary resource/caller changed or cross-tenant identity not distinct")
        proof = {key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient")} | {
            "clientVersion": version, "operations": OPERATIONS, "positive": True, "negative": True, "mediaSchema": True,
            "independentStores": True, "persistedAfterRestart": True, "anonymousInvocationRefusal": True,
            "approvalRequiredForwardProposal": True, "deploymentActuationProven": False, "runtimeHealthProven": False, "semanticOnly": True}
        return {"action": "complete", "proof": proof}
    except Pending as pending:
        return pending.value


def judge(fixture, bootstrap, observed, *, tenant_id=None):
    value = advance(fixture, bootstrap, observed,tenant_id=tenant_id)
    if value.get("action") != "complete":
        raise ValueError("actual lifecycle journey incomplete")
    return value["proof"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True); parser.add_argument("--advance", action="store_true")
    parser.add_argument("--tenant-id")
    args = parser.parse_args()
    try:
        fixture, bootstrap, observed = args.fixture.read_bytes(), wire.strict_json(args.bootstrap.read_bytes()), wire.strict_json(args.observations.read_bytes())
        value = advance(fixture, bootstrap, observed,tenant_id=args.tenant_id) if args.advance else judge(fixture, bootstrap, observed,tenant_id=args.tenant_id)
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        print("authored deployment/workspace oracle refused evidence", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
