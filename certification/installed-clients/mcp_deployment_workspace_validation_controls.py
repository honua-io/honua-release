"""Complete explicitly offline lifecycle vectors; never actual candidate proof."""
from __future__ import annotations
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from urllib.parse import urlencode

import mcp_deployment_workspace_validation as validation
import mcp_deployment_workspace_state as state
import mcp_deployment_workspace_store_oracle as store
from mcp_map_package_validation_controls import control_wire, bind_launch, CONTROL_SALT, responses as map_responses
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime


FOREIGN_FINGERPRINT = "scrypt:" + CONTROL_SALT.hex() + ":" + hashlib.scrypt(
    b"controlled-foreign-key", salt=CONTROL_SALT, n=16384, r=8, p=1, dklen=32).hex()

def stamp(seconds):
    return (datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def tool_output(value, error=False):
    text = json.dumps(value)
    if "items" in value and len(json.dumps(value, separators=(",", ":"))) >= 1024:
        text = "Deploy operations returned in structuredContent."
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": text}]}


def encoded(raw):
    return base64.b64encode(raw).decode(), "sha256:" + hashlib.sha256(raw).hexdigest()


def gp_wire(path, method, response, body=b""):
    req, req_hash = encoded(body); res, res_hash = encoded(json.dumps(response).encode())
    return {"path": path, "method": method, "requestBodyBase64": req, "requestBodySha256": req_hash,
            "credentialPresent": True, "responseStatus": 200, "responseBodyBase64": res, "responseBodySha256": res_hash}


def prerequisite(authored):
    identifier = "workspace-controlled"
    uri = "data:application/json;base64," + base64.b64encode(json.dumps({"type": "MeasureResult", "processId": "geometry.area", "measure": "area", "value": 12,
        "unit": "input-crs-units-squared", "inputSrid": 3857, "inputGeometryType": "Polygon"}).encode()).decode()
    job = "gp-controlled"
    base = "/rest/services/allocated-service-id/GPServer/geometry.area"
    import mcp_deployment_workspace_prerequisites as setup
    form = {"f": "json", "wkb": setup.polygon(), "srid": "3857", "env:workspace": authored["workspaceLabel"], "env:overwriteOutput": "false"}
    return {"schema": "honua.mcp-gp-workspace-prerequisite/v1", "serviceId": "allocated-service-id", "label": authored["workspaceLabel"],
        "workspaceId": identifier, "jobId": job, "ownerId": "controlled-owner", "scopeId": "controlled-tenant",
        "submitted": gp_wire(base + "/submitJob", "POST", {"jobId": job, "jobStatus": "esriJobSubmitted"}, urlencode(form).encode()),
        "polls": [gp_wire(base + "/jobs/" + job + "?f=json", "GET", {"jobId": job, "jobStatus": "esriJobSucceeded"})],
        "output": gp_wire(base + "/jobs/" + job + "/results/outputScalar?f=json", "GET", {"paramName": "outputScalar", "dataType": "GPString", "value": uri}),
        "durableJobSha256": "sha256:" + "1" * 64}


def history():
    values = []
    for index in (1, 2):
        identifier = "deploy-" + str(index)
        values.append({"operationId": identifier, "recordSha256": "sha256:" + str(index) * 64,
            "record": {"operationId": identifier, "kind": 0, "status": 4, "version": 3,
                "createdAt": stamp(index), "updatedAt": stamp(index + 1), "completedAt": stamp(index + 1),
                "providerOperationId": "controlled-provider-" + str(index), "observedState": "controlled-observation"},
            "deploy": {"targetId": "owned-target", "desiredRevision": "revision-" + str(index), "currentRevision": None}})
    return {"terminalCount": 2, "visibleCount": 2, "isTruncated": False, "operations": values}


def stores(authored, expected, workspace_value, seconds):
    envelope = {"family": "map", "body": expected["mapBody"]}
    raw = json.dumps(envelope, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    # The immutable save/publication values are independently constructed by
    # the response fixture with these exact canonical controlled bytes.
    assert expected["studioContentHash"] == digest
    source = {"kind": "StudioPublishedVersion", "sourceId": expected["studioVersionId"], "studioItemId": expected["studioItemId"],
        "studioVersionId": expected["studioVersionId"], "studioContentHash": digest, "studioOwnerId": "controlled-owner",
        "studioPublicationRequestId": "33333333-3333-4333-8333-333333333333", "studioFamily": "map"}
    transitions = [{"from": left, "to": right, "at": stamp(index + 10), "rolloutState": "Promoted" if right == "Active" else "InProgress", "reason": "Controlled real-writer shape"}
        for index, (left, right) in enumerate((("Draft", "Provisioning"), ("Provisioning", "RollingOut"), ("RollingOut", "Active")))]
    document = {"deploymentId": expected["deploymentId"], "source": source,
        "target": {"targetId": "studio-route:" + authored["route"], "kind": "StudioPublishedRoute", "hostingMode": "ManagedService", "routePrefix": "/api/v1/studio/published" + authored["route"]},
        "status": "Active", "publicationState": "Published", "rolloutState": "Promoted", "runtime": {"health": "Unknown"}, "createdAt": stamp(9), "updatedAt": stamp(12),
        "activatedAt": stamp(12), "transitions": transitions}
    workspace_id, job_id = workspace_value["workspaceId"], workspace_value["jobId"]
    artifact_identity = f"{len(workspace_id)}:{workspace_id}{len(job_id)}:{job_id}:0"
    scalar = json.loads(base64.b64decode(workspace_value["output"]["responseBodyBase64"]))["value"]
    return {"schema": "honua.mcp-deployment-workspace-state/v1", "startedAt": stamp(seconds), "completedAt": stamp(seconds + 1),
        "targetId": "owned-target", "deploymentId": expected["deploymentId"], "workspaceId": workspace_id,
        "succeededWorkflowHistory": history(), "stores": {"databaseIdentity": {"clusterId": "12345678", "database": "honua_test", "user": "honua"},
        "deployment": {"deploymentId": expected["deploymentId"], "sourceKind": "StudioPublishedVersion", "sourceId": source["sourceId"], "targetId": document["target"]["targetId"], "status": "Active", "document": document, "createdAt": stamp(9), "updatedAt": stamp(12)},
        "studioItem": {"itemId": expected["studioItemId"], "family": "map", "packageKey": authored["packageKey"], "ownerId": "controlled-owner", "tenantId": "controlled-tenant", "currentVersionId": expected["studioVersionId"], "publishedVersionId": expected["studioVersionId"]},
        "studioVersion": {"itemId": expected["studioItemId"], "versionId": expected["studioVersionId"], "contentHash": digest, "contentHashInputBase64": base64.b64encode(raw).decode(), "envelope": envelope, "ownerId": "controlled-owner", "tenantId": "controlled-tenant"},
        "studioPublication": {"requestId": source["studioPublicationRequestId"], "itemId": expected["studioItemId"], "versionId": expected["studioVersionId"], "intent": {"route": authored["route"], "visibility": "public"}, "status": "accepted", "deploymentId": expected["deploymentId"], "governsStudioRoutes": True},
        "workspace": {"workspace_id": workspace_id, "kind": 0, "label": authored["workspaceLabel"], "owner_id": "controlled-owner", "scope_id": "controlled-tenant", "state": 0, "uri": "honua://workspace-data/controlled", "created_at": stamp(20), "expires_at": stamp(86400)},
        "artifacts": [{"artifact_id": "output-" + hashlib.sha256(artifact_identity.encode()).hexdigest(), "workspace_id": workspace_id, "kind": 0, "label": "outputScalar", "label_key": "OUTPUTSCALAR", "state": 1, "uri": scalar, "content_type": None, "size_bytes": len(scalar.encode()), "created_at": stamp(21), "metadata": {}}]}}


def proposal_state(key):
    tenant = "controlled-tenant"
    identifier = "proposal-" + hashlib.sha256(f"{len(tenant)}:{tenant}:Deploy:control-plane.deploy:{key}".encode()).hexdigest()[:32]
    return {"schema": "honua.mcp-rollback-proposal-state/v1", "proposalId": identifier, "recordSha256": "sha256:" + "4" * 64, "planBytesSha256": "5" * 64,
        "record": {"proposalId": identifier, "kind": 1, "status": 1, "version": 2, "tenantId": tenant, "operationId": "control-plane.deploy", "actionDiscriminator": "deploy.rollback", "requestedBy": "controlled-owner", "requestedByAgent": "mcp:controlled-owner", "sealedPlanHash": "5" * 64, "executionOperationId": None, "resolvedBy": None, "resolvedAt": None, "createdAt": stamp(31), "updatedAt": stamp(32)},
        "audit": {"operationInstanceId": "operation-controlled", "auditId": "audit-controlled", "correlationId": "correlation-controlled", "idempotencyKey": key, "requestedBy": "controlled-owner", "tenantId": tenant},
        "payload": {"targetId": "owned-target", "desiredRevision": "revision-1", "currentRevision": "revision-2", "priority": 1}}


def operation():
    return {"status": "Completed", "operationInstanceId": "studio-operation-controlled", "auditId": "studio-audit-controlled", "correlationId": "studio-correlation-controlled"}


def rebind(value, selector="sdk"):
    value["canonicalClient"], value["clientVersion"] = ("Official MCP TypeScript SDK", "1.32.1") if selector == "sdk" else ("MCP Inspector", "2.10.1")
    for index, call in enumerate(value["calls"]):
        call["wire"], call["client"], call["result"] = control_wire(call["method"], call["params"], call["result"], index, call["authenticated"])
        if call.get("credentialRole") == "foreign":
            fingerprint = FOREIGN_FINGERPRINT
            call["wire"]["expectedCredentialFingerprint"] = fingerprint
            for phase in ("initialize", "invocation"):
                call["wire"][phase]["request"]["credentialFingerprint"] = fingerprint
        if selector == "inspector":
            frame, error = call["result"], call["result"].get("error")
            auth = bool(error and error.get("data", {}).get("code") == "unauthenticated")
            call["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 456,
                "requestUrl": call["wire"]["listener"], "exitCode": 3 if auth else 1 if error else 5 if frame.get("isError") else 0,
                "exitSignal": None, "timedOut": False, "stdout": "" if error else json.dumps({"result": frame}),
                "stderr": json.dumps({"error": {"code": "auth_required" if auth else "error", "message": error["message"]}}) if error else ""}
            bind_launch(call["client"], call["wire"], "inspector")
    return value


def controls(selector="sdk", nonce="abcdef123456"):
    value = {"nonce": nonce, "canonicalClient": "Official MCP TypeScript SDK", "clientVersion": "1.32.1", "targetId": "owned-target", "calls": [],
        "workspacePrerequisite": None, "storeBefore": None, "storeAfter": None, "proposalBefore": None, "proposalAfter": None,
        "runtimeBefore": runtime(), "runtimeRestarted": None, "runtimeAfter": runtime()}
    value["canonicalClient"], value["clientVersion"] = ("Official MCP TypeScript SDK", "1.32.1") if selector == "sdk" else ("MCP Inspector", "2.10.1")
    authored = validation.scenario(FIXTURE, bootstrap(), value["nonce"], value["canonicalClient"])
    map_body = None; expected = None
    for _ in range(128):
        action = validation.advance(FIXTURE, bootstrap(), value)
        if action["action"] == "complete":
            return value
        if action["action"] == "create-workspace":
            value["workspacePrerequisite"] = prerequisite(authored)
        elif action["action"] == "observe-stores":
            value[action["slot"]] = stores(authored, expected, value["workspacePrerequisite"], 40 if action["slot"] == "storeBefore" else 700)
        elif action["action"] == "observe-proposal":
            value[action["slot"]] = proposal_state(authored["packageKey"] + "-rollback")
        elif action["action"] == "restart":
            value["runtimeRestarted"] = runtime(); value["runtimeRestarted"]["startedAt"] = stamp(300)
            value["runtimeAfter"] = copy.deepcopy(value["runtimeRestarted"])
        elif action["action"] == "invoke":
            call = copy.deepcopy(action["call"]); identifier = call["id"]
            if identifier == "create-map": result = map_responses()[0]
            elif identifier == "create-studio-draft": result = tool_output({"draftId": "11111111-1111-4111-8111-111111111111", "generation": 1, "operation": operation()})
            elif identifier == "update-studio-draft":
                map_body = copy.deepcopy(call["params"]["arguments"]["body"])
                result = tool_output({"draftId": "11111111-1111-4111-8111-111111111111", "generation": 2, "operation": operation()})
            elif identifier == "validate-studio-draft": result = tool_output({"status": "valid", "unsupportedCapabilities": [], "diagnostics": [], "generatedAt": stamp(5)})
            elif identifier == "save-studio-version":
                raw = json.dumps({"family": "map", "body": map_body}, separators=(",", ":")).encode()
                result = tool_output({"operation": operation(), "version": {"itemId": "22222222-2222-4222-8222-222222222222", "versionId": "44444444-4444-4444-8444-444444444444", "contentHash": hashlib.sha256(raw).hexdigest()}})
            elif identifier == "publish-studio-version":
                expected = {"deploymentId": "dep-" + "6" * 32, "studioItemId": call["params"]["arguments"]["itemId"], "studioVersionId": call["params"]["arguments"]["versionId"], "studioContentHash": call["params"]["arguments"]["contentHash"], "route": authored["route"], "mapBody": map_body}
                result = tool_output({"status": "Published", "humanConfirmationRequired": False, "operation": operation(), "deploymentId": expected["deploymentId"], "deploymentResourceUri": "honua://deployments/" + expected["deploymentId"], "shareUrl": "/api/v1/studio/published" + authored["route"]})
            elif identifier in ("current-deploy", "prior-deploy"):
                item = next(row for row in history()["operations"] if row["operationId"] == call["params"]["arguments"]["operationId"])
                record = item["record"]; at = record["updatedAt"]
                source = {"sourceId": "honua_deploy_operations", "backendKind": "durableStore", "backendId": "workflow-operation-store", "observedAt": at, "lastSuccessfulAt": at,
                    "completeness": "complete", "reasonCodes": [], "maximumAgeSeconds": 300, "validUntil": stamp(303 if identifier == "current-deploy" else 302),
                    "coverage": {"page": 1, "pageSize": 1, "hasMore": False, "truncated": False, "returnedFrom": at, "returnedTo": at}}
                result = tool_output({"items": [{"operationId": item["operationId"], "kind": "Deploy", "status": "Succeeded", "providerOperationId": record["providerOperationId"],
                    "createdAt": record["createdAt"], "updatedAt": at, "completedAt": record["completedAt"], "target": item["deploy"]}],
                    "page": 1, "pageSize": 1, "totalCount": 1, "hasMore": False, "evidencePosture": {"schemaVersion": "1.0", "status": "complete", "sources": [source]}})
            elif identifier in ("rollback-proposal", "rollback-replay"):
                proposal_id = proposal_state(authored["packageKey"] + "-rollback")["proposalId"]
                result = tool_output({"outcome": "ProposalCreated", "requiresApproval": True, "proposalId": proposal_id, "resourceUri": "honua://proposals/" + proposal_id})
            elif identifier in ("deployment", "restarted-deployment"):
                document = value["storeBefore"]["stores"]["deployment"]["document"]; source, target = document["source"], document["target"]
                body = {"deploymentId": expected["deploymentId"], "resourceUri": "honua://deployments/" + expected["deploymentId"], "status": "Active", "publicationState": "Published", "rolloutState": "Promoted", "runtimeHealth": "Unknown",
                    "sourceKind": "StudioPublishedVersion", "sourceId": source["sourceId"], "targetId": target["targetId"], "targetKind": target["kind"], "hostingMode": target["hostingMode"], "routePrefix": target["routePrefix"],
                    **{key: source[key] for key in ("studioItemId", "studioVersionId", "studioContentHash", "studioOwnerId", "studioPublicationRequestId")},
                    "createdAt": document["createdAt"], "updatedAt": document["updatedAt"], "etag": f'W/"{store.ticks(document["updatedAt"]):x}-Active"', "transitions": document["transitions"]}
                result = {"contents": [{"uri": call["params"]["uri"], "mimeType": "application/json", "text": json.dumps(body)}]}
            elif identifier in ("workspace", "restarted-workspace"):
                row = value["storeBefore"]["stores"]["workspace"]
                body = {"workspaceId": row["workspace_id"], "kind": "scratch", "label": row["label"], "uri": row["uri"], "expiresAt": row["expires_at"], "lifecycleState": "active"}
                result = {"contents": [{"uri": call["params"]["uri"], "mimeType": "application/json", "text": json.dumps(body)}]}
            elif identifier.startswith("anonymous-") and call["method"] == "tools/call":
                result = tool_output({"status": "error", "code": "unauthenticated", "message": "Controlled denial", "requiresReauthentication": True, "error": {"kind": "AuthorizationDenied", "message": "Controlled denial"}}, True)
            elif call["method"] == "resources/read":
                anonymous = identifier.startswith("anonymous-")
                result = {"error": {"code": -32000 if anonymous else -32002, "message": "Controlled refusal", "data": {"code": "unauthenticated", "requiresReauthentication": True} if anonymous else {"code": "not_found"}}}
            else: result = tool_output({"status": "error", "code": "invalid_argument" if identifier == "invalid-rollback" else "not_found", "message": "Controlled refusal"}, True)
            call["result"] = result; value["calls"].append(call); rebind(value, selector)
        else: raise AssertionError("unexpected controlled lifecycle action")
    raise AssertionError("controlled lifecycle action bound exceeded")


def inspector_controls():
    """Independent complete Inspector vector; no actual client/candidate credit."""
    return controls("inspector")


class DeploymentWorkspaceControls(unittest.TestCase):
    def refused(self, value):
        with self.assertRaises((ValueError, TypeError, KeyError, AttributeError)):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_complete_sdk_and_inspector_vectors_reach_all_four_primary_operations(self):
        for selected in ("sdk", "inspector"):
            value = controls(selected)
            result = validation.judge(FIXTURE, bootstrap(), value)
            self.assertEqual(result["operations"], validation.OPERATIONS)
            self.assertTrue(result["persistedAfterRestart"])
            self.assertTrue(result["approvalRequiredForwardProposal"])
            self.assertFalse(result["deploymentActuationProven"])
            self.assertFalse(result["runtimeHealthProven"])

    def test_missing_or_reordered_primary_calls_refuse_even_with_success_flags(self):
        for identifier in ("current-deploy", "prior-deploy", "rollback-proposal", "rollback-replay", "deployment", "workspace", "restarted-deployment", "restarted-workspace", "foreign-deployment", "foreign-workspace", "anonymous-deploy", "anonymous-rollback", "anonymous-deployment", "anonymous-workspace"):
            with self.subTest(identifier=identifier):
                value = controls(); value["calls"] = [call for call in value["calls"] if call["id"] != identifier]
                self.refused(value)

    def test_raw_session_client_and_anonymous_credentials_are_not_optional(self):
        for mutation in ("raw", "session", "client", "anonymous", "response"):
            value = controls()
            if mutation == "raw": value["calls"][0].pop("wire")
            elif mutation == "session": value["calls"][1]["wire"]["initialize"]["response"]["sessionSha256"] = value["calls"][0]["wire"]["initialize"]["response"]["sessionSha256"]
            elif mutation == "client": value["calls"][0]["client"]["launch"]["launcherSha256"] = "sha256:" + "0" * 64
            elif mutation == "anonymous": next(call for call in value["calls"] if call["id"] == "anonymous-deploy")["wire"]["invocation"]["request"]["credentialPresent"] = True
            else: value["calls"][0]["result"]["structuredContent"]["packageId"] += "-altered"
            self.refused(value)

    def test_foreign_caller_must_be_distinct_and_cannot_read_owned_resources(self):
        value = controls()
        primary = value["calls"][0]["wire"]["expectedCredentialFingerprint"]
        for call in value["calls"]:
            if call.get("credentialRole") == "foreign":
                call["wire"]["expectedCredentialFingerprint"] = primary
                for phase in ("initialize", "invocation"):
                    call["wire"][phase]["request"]["credentialFingerprint"] = primary
        self.refused(value)
        for family in ("deployment", "workspace"):
            value = controls()
            foreign = next(call for call in value["calls"] if call["id"] == "foreign-" + family)
            foreign["result"] = copy.deepcopy(next(call for call in value["calls"] if call["id"] == family)["result"])
            rebind(value)
            self.refused(value)

    def test_real_publication_pointer_saved_bytes_and_writer_lifecycle_required(self):
        for mutation in ("pointer", "saved-bytes", "draft-substitute", "false-health", "transition", "source-hash", "publication"):
            value = controls(); stores_value = value["storeBefore"]["stores"]
            if mutation == "pointer": stores_value["studioItem"]["publishedVersionId"] = "55555555-5555-4555-8555-555555555555"
            elif mutation == "saved-bytes": stores_value["studioVersion"]["contentHashInputBase64"] = base64.b64encode(b"changed source bytes").decode()
            elif mutation == "draft-substitute": stores_value["deployment"]["document"]["status"] = "Draft"
            elif mutation == "false-health": stores_value["deployment"]["document"]["runtime"]["health"] = "Healthy"
            elif mutation == "transition": stores_value["deployment"]["document"]["transitions"].pop()
            elif mutation == "source-hash": stores_value["deployment"]["document"]["source"]["studioContentHash"] = "0" * 64
            else: stores_value["studioPublication"]["governsStudioRoutes"] = False
            self.refused(value)

    def test_workspace_requires_actual_scalar_job_slot_and_owner_scope(self):
        for mutation in ("owner", "scope", "artifact-id", "scalar", "state", "extra", "degraded"):
            value = controls(); stores_value = value["storeBefore"]["stores"]
            if mutation == "owner": stores_value["workspace"]["owner_id"] = "another-owner"
            elif mutation == "scope": stores_value["workspace"]["scope_id"] = "another-tenant"
            elif mutation == "artifact-id": stores_value["artifacts"][0]["artifact_id"] = "unrelated-output"
            elif mutation == "scalar": stores_value["artifacts"][0]["uri"] = "data:application/json;base64," + base64.b64encode(b'{"value":0}').decode()
            elif mutation == "state": stores_value["workspace"]["state"] = 3
            elif mutation == "extra": stores_value["artifacts"].append(copy.deepcopy(stores_value["artifacts"][0]))
            else:
                entry = next(call for call in value["calls"] if call["id"] == "workspace")
                entry["result"]["contents"][0]["text"] = json.dumps({"workspaceId": "workspace-controlled", "notImplementedReason": "No lifecycle provider"}); rebind(value)
            self.refused(value)

    def test_gp_prerequisite_cannot_be_fabricated_from_an_existing_workspace(self):
        for mutation in ("form", "path", "job", "poll", "bodyhash", "result"):
            value = controls(); setup = value["workspacePrerequisite"]
            if mutation == "form": setup["submitted"]["requestBodyBase64"] = base64.b64encode(b'f=json').decode()
            elif mutation == "path": setup["submitted"]["path"] = "/admin/unrelated"
            elif mutation == "job": setup["jobId"] = "another-job"
            elif mutation == "poll": setup["polls"] = []
            elif mutation == "bodyhash": setup["output"]["responseBodySha256"] = "sha256:" + "0" * 64
            else: setup["output"] = gp_wire(setup["output"]["path"], "GET", {"paramName": "outputScalar", "dataType": "GPString", "value": "invented"})
            self.refused(value)

    def test_prior_history_requires_two_real_different_succeeded_provider_operations(self):
        for mutation in ("missing", "failed", "same-revision", "wrong-target", "truncated", "no-provider"):
            value = controls(); history_value = value["storeBefore"]["succeededWorkflowHistory"]
            if mutation == "missing": history_value["operations"].pop()
            elif mutation == "failed": history_value["operations"][0]["record"]["status"] = 5
            elif mutation == "same-revision": history_value["operations"][0]["deploy"]["desiredRevision"] = "revision-2"
            elif mutation == "wrong-target": history_value["operations"][0]["deploy"]["targetId"] = "another-target"
            elif mutation == "truncated": history_value["isTruncated"] = True
            else: history_value["operations"][0]["record"]["providerOperationId"] = None
            self.refused(value)

    def test_rollback_is_new_forward_proposal_not_approval_or_completed_actuation(self):
        for mutation in ("approved", "executed", "revision", "audit", "seal", "idempotency", "action"):
            value = controls(); proposal_value = value["proposalBefore"]
            if mutation == "approved": proposal_value["record"]["status"] = 2
            elif mutation == "executed": proposal_value["record"]["executionOperationId"] = "actuated-operation"
            elif mutation == "revision": proposal_value["payload"]["desiredRevision"] = "revision-2"
            elif mutation == "audit": proposal_value["audit"]["auditId"] = ""
            elif mutation == "seal": proposal_value["record"]["sealedPlanHash"] = "0" * 64
            elif mutation == "idempotency": proposal_value["audit"]["idempotencyKey"] += "-changed"
            else: proposal_value["record"]["actionDiscriminator"] = "deploy.apply"
            self.refused(value)

    def test_replay_and_restart_must_preserve_real_records_and_exact_container(self):
        for mutation in ("proposal", "source", "container", "start", "store", "database", "time"):
            value = controls()
            if mutation == "proposal": value["proposalAfter"]["record"]["version"] += 1
            elif mutation == "source": value["runtimeAfter"]["candidateSourceSha"] = "f" * 40
            elif mutation == "container": value["runtimeRestarted"]["containerId"] = "f" * 64
            elif mutation == "start": value["runtimeRestarted"]["startedAt"] = value["runtimeBefore"]["startedAt"]
            elif mutation == "store": value["storeAfter"]["stores"]["workspace"]["label"] += "-changed"
            elif mutation == "database": value["storeAfter"]["stores"]["databaseIdentity"]["clusterId"] = "99999999"
            else: value["storeBefore"]["completedAt"] = stamp(400)
            self.refused(value)

    def test_truthful_stale_history_posture_is_valid_read_but_not_actionable(self):
        value = controls(); value["storeBefore"]["startedAt"], value["storeBefore"]["completedAt"] = stamp(400), stamp(401)
        value["runtimeRestarted"]["startedAt"] = value["runtimeAfter"]["startedAt"] = stamp(600)
        for entry in value["calls"]:
            if entry["id"] in ("current-deploy", "prior-deploy"):
                body = entry["result"]["structuredContent"]
                body["evidencePosture"]["status"] = "unavailable"
                body["evidencePosture"]["sources"][0].update(completeness="unavailable", reasonCodes=["stale"])
                entry["result"] = tool_output(body)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), rebind(value))["positive"])
        changed = copy.deepcopy(value)
        for entry in changed["calls"]:
            if entry["id"] in ("current-deploy", "prior-deploy"):
                body = entry["result"]["structuredContent"]; body["evidencePosture"]["status"] = "complete"
                body["evidencePosture"]["sources"][0].update(completeness="complete", reasonCodes=[]); entry["result"] = tool_output(body)
        self.refused(rebind(changed))

    def test_readonly_history_observer_uses_actual_global_window_and_refuses_ambiguity(self):
        body = {"operationId": "deploy-1", "kind": 0, "status": 4, "version": 1, "deploy": {"targetId": "owned-target", "desiredRevision": "revision-1", "currentRevision": None}}
        calls = []
        result = state.workflow_history("owned-target", execute=lambda args: calls.append(args) or [b"1", [[b"deploy-1", json.dumps(body).encode()]]])
        self.assertEqual(len(result["operations"]), 1)
        self.assertIn("controlplane:workflow:terminal", calls[0][1]); self.assertNotIn("deploy-succeeded:", calls[0][1])
        self.assertNotIn("redis.call('SET'", calls[0][1]); self.assertNotIn("redis.call('ZREM'", calls[0][1])
        for reply in ([b"1", []], [b"0", [[b"deploy-1", json.dumps(body).encode()]]], [b"1", [[b"wrong-id", json.dumps(body).encode()]]]):
            with self.assertRaises(ValueError): state.workflow_history("owned-target", execute=lambda args: reply)

    def test_actual_nested_plan_bytes_must_match_durable_gateway_seal(self):
        plan = {"summary": "controlled", "executionPayload": json.dumps({"targetId": "owned-target", "desiredRevision": "old", "currentRevision": "new", "priority": 1})}
        plan_bytes = json.dumps(plan, separators=(",", ":")).encode()
        body = {"proposalId": "proposal-controlled", "plan": plan, "sealedPlanHash": hashlib.sha256(plan_bytes).hexdigest()}
        raw = json.dumps(body, separators=(",", ":")).encode()
        result = state.observe_proposal("proposal-controlled", execute=lambda args: raw)
        self.assertEqual(result["planBytesSha256"], hashlib.sha256(plan_bytes).hexdigest())
        changed = copy.deepcopy(body); changed["plan"]["summary"] = "altered"
        with self.assertRaises(ValueError): state.observe_proposal("proposal-controlled", execute=lambda args: json.dumps(changed).encode())

    def test_unknown_observation_fields_and_incomplete_progress_are_not_proof(self):
        value = controls(); value["inventedPassed"] = True; self.refused(value)
        value = controls(); value["calls"] = value["calls"][:2]; self.refused(value)


if __name__ == "__main__":
    unittest.main()
