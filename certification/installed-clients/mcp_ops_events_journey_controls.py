"""Complete synthetic SDK/Inspector joins, never live client/candidate evidence."""
from __future__ import annotations

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

import yaml

import mcp_ops_events_data_read as limited_read
import mcp_ops_events_validation as oracle
from mcp_ops_events_config_controls import manifest as configuration_manifest
from mcp_ops_events_validation_controls import scenario_inputs, context_vector, findings_vector, response
from mcp_map_package_validation_controls import control_wire, bind_launch
from mcp_render_validation_controls import FIXTURE, bootstrap


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def encoded(raw):
    return base64.b64encode(raw).decode()


def stamp(seconds=0):
    return (datetime(2026, 10, 19, 22, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def managed_receipt(role, runtime):
    """Independent literal managed-store/API vector, not helper-produced proof."""
    limited = role == "limited"
    identifier = "22222222-2222-4222-8222-222222222222" if limited else "11111111-1111-4111-8111-111111111111"
    name = "mcp-owned-read-" + ("2" if limited else "1") * 24
    permissions = ["read:*"] if limited else ["admin"]
    key = "controlled-limited-read-key" if limited else "controlled-full-key"
    salt = "a" * 32
    fingerprint = "scrypt:" + salt + ":" + hashlib.scrypt(key.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()
    created, active_at, revoked, revoked_at, expires = stamp(-50), stamp(-48), stamp(3500), stamp(3501), stamp(7200)
    original = {"Id": identifier, "Name": name, "KeyPrefix": "controlled", "Permissions": permissions,
        "KeyHash": encoded(hashlib.sha256(key.encode()).digest()), "CreatedAt": created,
        "UpdatedAt": created, "ExpiresAt": expires, "LastUsedAt": None, "RevokedAt": None,
        "RotatedAt": None, "CreatedBy": "controlled-owner"}

    def record(active):
        value = copy.deepcopy(original)
        if not active:
            value.update(RevokedAt=revoked, UpdatedAt=revoked)
        raw = json.dumps(value).encode()
        return {"keyId": identifier, "recordBytesBase64": encoded(raw), "recordSha256": sha(raw),
            "permissions": permissions, "active": active, "hashIndexValue": identifier if active else None,
            "activeIndexMembership": 1 if active else 0, "canonicalActorId": "apikey:api-key:" + identifier,
            "studioActorId": identifier, "workspaceOwnerId": identifier, "credentialFingerprint": fingerprint,
            "createdAt": created, "expiresAt": expires, "lastUsedAt": None,
            "revokedAt": None if active else revoked, "observedAt": active_at if active else revoked_at}

    provisioned = {"id": identifier, "name": name, "permissions": permissions, "status": "active"}
    active_effective = {**provisioned, "canAuthenticate": True}
    revoked_metadata = {**provisioned, "status": "revoked"}
    revoked_effective = {**revoked_metadata, "canAuthenticate": False}
    paths = ["/api/v1/admin/api-keys/", "/api/v1/admin/api-keys/" + identifier + "/effective-permissions",
        "/api/v1/admin/api-keys/" + identifier + "/revoke", "/api/v1/admin/api-keys/" + identifier + "/effective-permissions"]
    events = []
    for index, (method, path, data) in enumerate(zip(["POST", "GET", "POST", "GET"], paths,
            [None, active_effective, revoked_metadata, revoked_effective], strict=True)):
        request = json.dumps({"name": name, "permissions": permissions, "expiresAt": expires}).encode() if index == 0 else b""
        # The create secret itself is deliberately absent from public controls.
        reply = b"controlled original create bytes not retained" if index == 0 else json.dumps({"success": True, "data": data}).encode()
        events.append({"method": method, "path": path, "status": 201 if index == 0 else 200,
            "requestBytesBase64": encoded(request), "requestSha256": sha(request), "responseSha256": sha(reply),
            "observedAt": active_at if index < 2 else revoked_at,
            **({} if index == 0 else {"responseBytesBase64": encoded(reply)})})
    return {"schema": "honua.mcp-managed-identity-fixture/v1", "name": name,
        "profile": "limited-read" if limited else "admin", "credentialSalt": salt, "permissions": permissions,
        "createdAt": created, "events": events, "limitedReadProven": False, "tenantIsolationProven": False,
        "cleanupComplete": True, "candidate": {"sourceSha": runtime["candidateSourceSha"],
            **{field: runtime[field] for field in ("containerId", "imageId", "imageReference")}},
        "keyId": identifier, "provisioned": provisioned, "effectivePermissions": active_effective,
        "identity": record(True), "revocation": record(False), "revokedKeyIds": [identifier],
        "revokedEffectivePermissions": revoked_effective}


def context_at(seconds, runtime, full_fingerprint):
    value = context_vector()[0]
    value.update(startedAt=stamp(seconds), completedAt=stamp(seconds + 1.5), runtimeBinding=copy.deepcopy(runtime))
    value["storeObservation"].update(startedAt=stamp(seconds), completedAt=stamp(seconds + 1), runtimeBinding=copy.deepcopy(runtime))
    value["storeObservation"]["store"]["observedAt"] = stamp(seconds + .5)
    value["configuration"].update(observedAt=stamp(seconds + .1), runtimeBinding=copy.deepcopy(runtime))
    value["preflight"].update(startedAt=stamp(seconds + .2), completedAt=stamp(seconds + .3), credentialFingerprint=full_fingerprint)
    return value


def findings_at(seconds, *, empty=False):
    value = findings_vector()[0]
    # Keep the independent literal finding and component contracts, changing
    # only their explicit causal clock for each fresh synthetic evaluation.
    def replace(item):
        if isinstance(item, dict):
            return {key: replace(content) for key, content in item.items()}
        if isinstance(item, list):
            return [replace(content) for content in item]
        if item == "2026-10-19T22:00:02Z":
            return stamp(seconds)
        if item == "2026-10-19T22:05:02Z":
            return stamp(seconds + 300)
        return item
    value = replace(value)
    if empty:
        value["findings"] = []
    return value


def events_for(entry, setup, seconds):
    """Independent wire contract vector over the two explicit synthetic rows."""
    by_id = {row["eventId"]: row for row in setup["originalStore"]["store"]["alerts"]}
    rows = [by_id[identifier] for identifier in entry["selectedEventIds"]]
    operate = entry["expectation"] == "operate"
    items = []
    for row in rows:
        if operate:
            items.append({"eventId": "alert:" + str(row["eventId"]), "kind": "alert", "severity": row["severity"],
                "occurredAt": row["occurredAt"], "title": "Enter alert on layer 1", "summary": "Canonical MCP Harbor Entry",
                "serviceId": "canonical-mcp-ops", "layerId": 1, "objectId": row["objectId"], "resourceRef": "alert/" + str(row["eventId"])})
        else:
            item = {field: row[field] for field in ("eventId", "ruleId", "ruleName", "zoneId", "serviceId", "layerId",
                "objectId", "severity", "occurredAt", "incidentDurationMs")}
            item.update(triggerType="enter", incidentStatus="ended" if row["eventId"] == 32 else "started",
                lifecycleStatus="resolved" if row["eventId"] == 32 else "open", resourceRef="alert/" + str(row["eventId"]))
            if row["eventId"] == 32:
                item.update(resolvedAt=row["resolvedAt"], resolvedBy="canonical-mcp-fixture")
            items.append(item)
    has_more = entry["hasMore"]
    coverage = {"hasMore": has_more, "truncated": has_more,
        "requestedFrom": entry["params"]["arguments"]["from"], "requestedTo": entry["params"]["arguments"]["to"]}
    if rows:
        coverage.update(returnedFrom=rows[-1]["occurredAt"], returnedTo=rows[0]["occurredAt"])
    observed = rows[0]["occurredAt"] if rows else stamp(seconds)
    observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    reasons = (["stale"] if rows else []) + (["truncated"] if has_more else [])
    status = "unavailable" if rows else "complete"
    source = {"sourceId": "honua_operate_events" if operate else "honua_alert_events", "backendKind": "durableStore",
        "backendId": "operate-event-feed" if operate else "alert-event-store", "observedAt": observed,
        "lastSuccessfulAt": observed, "maximumAgeSeconds": 300, "validUntil": (observed_at + timedelta(seconds=300)).isoformat(),
        "reasonCodes": reasons, "completeness": status, "coverage": coverage}
    value = {"items": items, "evidencePosture": {"schemaVersion": "1.0", "status": status, "sources": [source]}}
    if operate:
        value["partialResult"] = False
    elif has_more:
        value["nextCursor"] = "original-synthetic-first-page-cursor"
    summary = "Operate-event page returned in structuredContent." if operate else "Alert-event page returned in structuredContent."
    return response(value, summary)


def rebind(value, *, inspector=False):
    for index, entry in enumerate(value["calls"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], index, entry["authenticated"])
        role = "limited" if entry["credentialRole"] == "limited" else "full"
        fingerprint = value["identityFixtures"][role]["identity"]["credentialFingerprint"]
        entry["wire"]["expectedCredentialFingerprint"] = fingerprint
        for field in ("initialize", "invocation"):
            entry["wire"][field]["request"]["credentialFingerprint"] = fingerprint if entry["authenticated"] else None
        if inspector:
            frame = entry["result"]
            error = frame.get("error")
            anonymous = bool(error and error.get("data", {}).get("code") == "unauthenticated")
            entry["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 456,
                "requestUrl": entry["wire"]["listener"], "exitCode": 3 if anonymous else 1 if error else 5 if frame.get("isError") else 0,
                "exitSignal": None, "timedOut": False, "stdout": "" if error else json.dumps({"result": frame}),
                "stderr": json.dumps({"error": {"code": "auth_required" if anonymous else "error", "message": error["message"]}}) if error else ""}
            bind_launch(entry["client"], entry["wire"], "inspector")
        else:
            bind_launch(entry["client"], entry["wire"])
    return value


def journey(*, inspector=False):
    setup, declaration = scenario_inputs()
    selected = setup["runtimeBinding"]
    restarted = {**selected, "startedAt": stamp(180)}
    canonical = "MCP Inspector" if inspector else "Official MCP TypeScript SDK"
    value = oracle.scenario(FIXTURE, bootstrap(), setup, declaration, "abcdef123456", canonical,
        actual_cursor="original-synthetic-first-page-cursor")
    value.update(clientVersion="2.10.1" if inspector else "1.32.1", runtimeBefore=copy.deepcopy(selected),
        runtimeRestarted=restarted, runtimeAfter=copy.deepcopy(restarted),
        identityFixtures={role: managed_receipt(role, selected) for role in ("full", "limited")})
    text = json.dumps({"exceededTransferLimit": False, "count": len(json.loads(FIXTURE)["area"]["features"])})
    value["limitedRead"] = {"schema": limited_read.SCHEMA, "method": "GET", "url": limited_read.data_url(FIXTURE, bootstrap()),
        "status": 200, "responseText": text, "responseSha256": sha(text.encode()),
        "credentialFingerprint": value["identityFixtures"]["limited"]["identity"]["credentialFingerprint"],
        "runtimeBefore": copy.deepcopy(selected), "runtimeAfter": copy.deepcopy(selected), "startedAt": stamp(-40), "completedAt": stamp(-39)}
    for index, entry in enumerate(value["calls"]):
        seconds = 12 + index * 10
        positive = entry["expectation"] in ("finding", "empty-finding", "alerts", "operate")
        if positive:
            runtime = restarted if entry["id"].startswith("restarted-") else selected
            fingerprint = value["identityFixtures"]["full"]["identity"]["credentialFingerprint"]
            entry.update(before=context_at(seconds - 2, runtime, fingerprint), after=context_at(seconds + 2, runtime, fingerprint))
            if entry["expectation"] in ("finding", "empty-finding"):
                body = findings_at(seconds, empty=entry["expectation"] == "empty-finding")
                entry["result"] = {"contents": [{"uri": oracle.FINDINGS_URI, "mimeType": "application/json", "text": json.dumps(body)}]} \
                    if entry["method"] == "resources/read" else response(body, "Ops findings returned in structuredContent.")
            else:
                entry["result"] = events_for(entry, setup, seconds)
        elif entry["expectation"] == "invalid-argument":
            body = {"status": "error", "code": "invalid_argument", "message": "Controlled invalid filter"}
            entry["result"] = {**response(body), "isError": True}
        elif entry["method"] == "resources/read":
            entry["result"] = {"error": {"code": -32000, "message": "Controlled authorization refusal",
                "data": {"code": "unauthenticated", "requiresReauthentication": True} if entry["credentialRole"] == "anonymous" else {"code": "permission_denied"}}}
        else:
            anonymous = entry["credentialRole"] == "anonymous"
            message = "Authentication required" if anonymous else "Caller is not authorized to read operational observability."
            body = {"status": "error", "code": "unauthenticated" if anonymous else "permission_denied", "message": message,
                "error": {"kind": "AuthorizationDenied", "message": message}, **({"requiresReauthentication": True} if anonymous else {})}
            entry["result"] = {**response(body), "isError": True}
    return rebind(value, inspector=inspector)


def judge(value):
    setup, declaration = scenario_inputs()
    manifest = configuration_manifest()
    manifest["candidate"]["ref"] = setup["runtimeBinding"]["candidateSourceSha"]
    manifest["components"]["honua-server"].update(sha=setup["runtimeBinding"]["candidateSourceSha"], digest="sha256:" + "c" * 64)
    return oracle.judge(FIXTURE, bootstrap(), setup, yaml.safe_dump(manifest).encode(), declaration, value)


class CompleteOperateJourneyControls(unittest.TestCase):
    def test_both_complete_canonical_client_vectors_require_all_original_joins(self):
        for inspector in (False, True):
            with self.subTest(inspector=inspector):
                result = judge(journey(inspector=inspector))
                self.assertTrue(all(result[field] for field in ("positive", "negative", "independentStores",
                    "persistedRestart", "liveFindingsRecomputed", "managedIdentityJoined", "limitedDataReadPositive", "actualCursorPaging")))
                self.assertFalse(result["alertEvaluationExercised"] or result["notificationDeliveryExercised"] or result["lifecycleApiExercised"])

    def test_missing_or_coherently_false_authority_read_and_lifecycle_refuse(self):
        mutations = (
            lambda v: v.pop("identityFixtures"),
            lambda v: v["identityFixtures"].pop("limited"),
            lambda v: v["identityFixtures"]["limited"].update(cleanupComplete=False),
            lambda v: v["identityFixtures"].update(limited=copy.deepcopy(v["identityFixtures"]["full"])),
            lambda v: v.pop("limitedRead"),
            lambda v: v["limitedRead"].update(credentialFingerprint=v["identityFixtures"]["full"]["identity"]["credentialFingerprint"]),
            lambda v: v["limitedRead"].update(startedAt=stamp(-55), completedAt=stamp(-54)),
            lambda v: v["limitedRead"].update(startedAt=stamp(20), completedAt=stamp(21)),
            lambda v: v["limitedRead"]["runtimeBefore"].update(containerId="f" * 64),
        )
        original = journey()
        for mutation in mutations:
            value = copy.deepcopy(original)
            mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError, KeyError)):
                judge(value)

    def test_semantic_changes_still_refuse_after_rebinding_wire_and_client(self):
        original = journey()
        for name in ("missing-finding", "wrong-event", "hide-staleness", "false-limited-reauth", "ignore-cursor", "cached-restart"):
            value = copy.deepcopy(original)
            calls = {entry["id"]: entry for entry in value["calls"]}
            if name == "missing-finding":
                calls["findings"]["result"]["structuredContent"]["findings"] = []
            elif name == "wrong-event":
                calls["alerts"]["result"]["structuredContent"]["items"][0]["objectId"] = 9999
            elif name == "hide-staleness":
                posture = calls["alerts"]["result"]["structuredContent"]["evidencePosture"]
                posture.update(status="complete")
                posture["sources"][0].update(completeness="complete", reasonCodes=[])
            elif name == "false-limited-reauth":
                calls["limited-ops_findings"]["result"]["structuredContent"].update(code="unauthenticated", requiresReauthentication=True)
            elif name == "ignore-cursor":
                calls["alerts-second"]["params"]["arguments"].pop("cursor")
            else:
                calls["restarted-findings"]["result"] = copy.deepcopy(calls["findings"]["result"])
            for entry in value["calls"]:
                if "structuredContent" in entry["result"]:
                    body = entry["result"]["structuredContent"]
                    summary = "Ops findings returned in structuredContent." if "findings" in body else "Alert-event page returned in structuredContent."
                    entry["result"]["content"] = response(body, summary)["content"]
            rebind(value)
            with self.subTest(name=name), self.assertRaises((ValueError, TypeError, KeyError)):
                judge(value)

    def test_actual_wire_session_raw_bytes_and_process_record_are_mandatory(self):
        original = journey(inspector=True)
        for name in ("missing-wire", "raw-hash", "session-reuse", "false-output", "missing-restart", "foreign-preflight-key"):
            value = copy.deepcopy(original)
            first = value["calls"][0]
            if name == "missing-wire":
                first.pop("wire")
            elif name == "raw-hash":
                first["wire"]["invocation"]["response"]["bodySha256"] = "sha256:" + "0" * 64
            elif name == "session-reuse":
                value["calls"][1]["wire"]["initialize"]["response"]["sessionSha256"] = first["wire"]["initialize"]["response"]["sessionSha256"]
            elif name == "false-output":
                first["client"]["stdout"] = '{"result":{"fabricated":true}}'
                bind_launch(first["client"], first["wire"], "inspector")
            elif name == "missing-restart":
                value["runtimeRestarted"] = copy.deepcopy(value["runtimeBefore"])
            else:
                first["before"]["preflight"]["credentialFingerprint"] = value["identityFixtures"]["limited"]["identity"]["credentialFingerprint"]
            with self.subTest(name=name), self.assertRaises((ValueError, TypeError, KeyError)):
                judge(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
