"""Independent offline live-health vectors; no actual client/candidate credit."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

import mcp_ops_health_validation as validation
import mcp_ops_health_state as state
from mcp_map_package_validation_controls import control_wire, bind_launch
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime


# The entries honua-server 8cb074f7 registers in a certified (non-Aspire) deployment.
REAL_CHECK_NAMES = ("alert-dispatch", "alert-evaluation", "audit-chain-integrity", "database",
                    "feature-change-outbox", "feature-streams", "file-upload", "plugins",
                    "production-metrics", "redis")

def timestamp(seconds):
    return (datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def independent(seconds, active=True):
    jobs = [{"jobId": "owned-active", "status": 1 if active else 3, "backend": "local", "sha256": "sha256:" + "1" * 64}]
    return {"schema": "honua.mcp-ops-health-observation/v1", "startedAt": timestamp(seconds), "completedAt": timestamp(seconds + 1),
        "databaseIdentity": {"clusterId": "12345678", "database": "honua_test", "user": "honua"},
        "database": {"database": "honua_test", "user": "honua", "probe": 1, "postgis": "3.4 controlled", "area": bootstrap()["areaReadback"]},
        "databaseProbeDurationMs": 2, "redis": {"ping": "PONG", "cacheRoundtrip": True, "probeRemoved": True,
        "jobs": jobs, "activeIds": ["owned-active"] if active else []}}


def payload(seconds, active=True):
    source = lambda suffix, backend, identifier: {"sourceId": "honua_ops_health." + suffix, "backendKind": backend,
        "backendId": identifier, "observedAt": timestamp(seconds), "lastSuccessfulAt": timestamp(seconds),
        "completeness": "complete", "reasonCodes": [], "maximumAgeSeconds": 120, "validUntil": timestamp(seconds + 120)}
    components = [source(suffix, backend, identifier) for suffix, backend, identifier in (
        ("health_checks", "inProcess", "aspnet-health-checks"), ("serving_latency", "inProcess", "honua-telemetry"),
        ("gp_queue", "durableStore", "execution-job-store"), ("alert_dispatch", "durableStore", "alert-dispatch-store"),
        ("deploy_release", "configProjection", "control-plane-options"), ("database_cache", "inProcess", "production-metrics"))]
    components[1]["coverage"] = {"observedReplicaCount": 1, "expectedReplicaCount": 1}
    components[3].update(observedAt=timestamp(seconds-1), lastSuccessfulAt=timestamp(seconds-1), validUntil=timestamp(seconds+119))
    components[4].update(maximumAgeSeconds=300, validUntil=timestamp(seconds+300))
    aggregate = source("unused", "composite", "ops-health-composer"); aggregate["sourceId"] = "honua_ops_health"
    aggregate.update(observedAt=timestamp(seconds-1), lastSuccessfulAt=timestamp(seconds-1), validUntil=timestamp(seconds+119))
    aggregate["coverage"] = {"expectedComponentIds": sorted(item["sourceId"] for item in components),
        "includedComponentIds": sorted(item["sourceId"] for item in components)}
    return {"generatedAt": timestamp(seconds), "overallStatus": "Healthy", "health": {"status": "Healthy", "totalDurationMs": 7,
        "entries": [{"name": name, "status": "Healthy", "description": "Controlled actual-dependency shape", "durationMs": 1}
                    for name in REAL_CHECK_NAMES]},
        "geoprocessing": {"available": True, "totalActive": 1 if active else 0,
            "buckets": [{"status": "Provisioning", "backend": "local", "count": 1}] if active else []},
        "servingLatency": {"windowSeconds": 60, "protocols": [{"protocol": "mcp", "requestCount": 8, "errorCount": 0,
            "errorRate": 0, "p50Ms": 1, "p95Ms": 3, "p99Ms": 4, "maxMs": 5}], "clusterReplicaCount": 1},
        "database": {"hasConnectionPoolData": True, "connectionPoolUtilization": 0.2, "activeConnections": 2,
            "connectionAcquisitionTimeouts": 0, "connectionAcquisitionFailures": 0, "cacheHitRatio": 0.5, "errorRate": 0},
        "alertDispatch": {"dispatcherRunning": True, "dispatcherEnabled": True, "storagePollFailing": False,
            "lastPollAt": timestamp(seconds - 1), "backlogObservedAt": timestamp(seconds - 1), "pendingCount": 0,
            "deadLetteredCount": 0, "retryingCount": 0, "oldestItemAgeSeconds": 0, "channels": []},
        "deploy": {"status": "ready", "readyForCoordinatedDeploy": True, "pendingMigrationsCount": 0,
            "pendingContractScriptsCount": 0, "platformRelease": {"releaseDeclared": False, "isCoVersioned": True, "skewedIds": []}},
        "evidencePosture": {"schemaVersion": "1.0", "status": "complete", "sources": [aggregate, *components]}}


def envelope(value, error=False):
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": "Ops health snapshot returned in structuredContent." if "overallStatus" in value else json.dumps(value)}]}


def rebind(value):
    for index, entry in enumerate(value["calls"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], index, entry["authenticated"])
    return value


def controls():
    value = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
    restarted = runtime(); restarted["startedAt"] = timestamp(300)
    value.update(clientVersion="1.32.1", runtimeBefore=runtime(), runtimeRestarted=restarted, runtimeAfter=copy.deepcopy(restarted))
    for entry in value["calls"]:
        if entry["id"] in ("tool", "resource", "restarted-tool", "restarted-resource"):
            seconds = {"tool": 62, "resource": 72, "restarted-tool": 362, "restarted-resource": 372}[entry["id"]]
            entry.update(before=independent(seconds - 2), after=independent(seconds + 2))
            body = payload(seconds)
            entry["result"] = envelope(body) if entry["method"] == "tools/call" else {
                "contents": [{"uri": validation.URI, "mimeType": "application/json", "text": json.dumps(body)}]}
        elif entry["id"] == "invalid-tool": entry["result"] = envelope({"status": "error", "code": "invalid_argument", "message": "No arguments expected"}, True)
        elif entry["id"] == "anonymous-tool": entry["result"] = envelope({"status": "error", "code": "unauthenticated", "message": "Authentication required",
            "requiresReauthentication": True, "error": {"kind": "AuthorizationDenied", "message": "Authentication required"}}, True)
        else:
            anonymous = entry["id"] == "anonymous-resource"
            entry["result"] = {"error": {"code": -32000 if anonymous else -32002, "message": "Controlled refusal",
                "data": {"code": "unauthenticated", "requiresReauthentication": True} if anonymous else {"code": "not_found"}}}
    return rebind(value)


def inspector_controls():
    """Explicit offline Inspector process vectors, rejudged by the real oracle."""
    value = controls()
    value["canonicalClient"], value["clientVersion"] = "MCP Inspector", "2.10.1"
    for item in value["calls"]:
        frame = item["result"]; error = frame.get("error")
        auth = bool(error and error.get("data", {}).get("code") == "unauthenticated")
        item["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 456,
            "requestUrl": item["wire"]["listener"], "exitCode": 3 if auth else 1 if error else 5 if frame.get("isError") else 0,
            "exitSignal": None, "timedOut": False, "stdout": "" if error else json.dumps({"result": frame}),
            "stderr": json.dumps({"error": {"code": "auth_required" if auth else "error", "message": error["message"]}}) if error else ""}
        bind_launch(item["client"], item["wire"], "inspector")
    return value


def mutate_payload(value, transform):
    body = value["calls"][0]["result"]["structuredContent"]
    transform(body)
    value["calls"][0]["result"]["content"][0]["text"] = "Ops health snapshot returned in structuredContent."
    return rebind(value)


class OpsHealthControls(unittest.TestCase):
    def refused(self, value):
        with self.assertRaises((ValueError, TypeError, KeyError)):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_complete_positive_actual_field_shape_and_restart_recomputation(self):
        judged = validation.judge(FIXTURE, bootstrap(), controls())
        self.assertTrue(judged["independentStores"] and judged["restartRecomputed"] and judged["liveNotPersisted"])
        self.assertEqual(judged["operations"], ["tools/call:ops_health", "resources/read:Ops health"])

    def test_offline_inspector_tool_resource_and_refusal_outputs_are_rejudged(self):
        value = inspector_controls()
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["positive"])
        for mutation in ("positive-output", "refusal-diagnostic"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(value)
                item = changed["calls"][0] if mutation == "positive-output" else next(call for call in changed["calls"] if call["id"] == "anonymous-resource")
                if mutation == "positive-output": item["client"]["stdout"] = json.dumps({"result": {"fabricated": True}})
                else: item["client"]["stderr"] = json.dumps({"error": {"code": "error", "message": "different refusal"}})
                bind_launch(item["client"], item["wire"], "inspector")
                self.refused(changed)

    def test_independent_store_or_queue_loss_cannot_be_success(self):
        for name in ("db-identity", "db-probe", "fixture", "redis-ping", "cache", "cleanup", "active-index", "record-hash", "changed-record", "nonfinite-database-timing"):
            with self.subTest(name=name):
                value = controls(); before = value["calls"][0]["before"]
                if name == "db-identity": before["databaseIdentity"]["database"] = "foreign"
                elif name == "db-probe": before["database"]["probe"] = False
                elif name == "fixture": before["database"]["area"] = []
                elif name == "redis-ping": before["redis"]["ping"] = "fake"
                elif name == "cache": before["redis"]["cacheRoundtrip"] = False
                elif name == "cleanup": before["redis"]["probeRemoved"] = False
                elif name == "active-index": before["redis"]["activeIds"] = []
                elif name == "record-hash": before["redis"]["jobs"][0]["sha256"] = "missing"
                elif name == "changed-record": value["calls"][0]["after"]["redis"]["jobs"][0]["sha256"] = "sha256:" + "2" * 64
                else: before["databaseProbeDurationMs"] = float("nan")
                self.refused(value)

    def test_real_dependency_checks_are_required_without_an_aspire_only_self_check(self):
        # honua-server registers "self" only under Aspire service defaults (absent when certified).
        value = controls()
        names = [entry["name"] for entry in value["calls"][0]["result"]["structuredContent"]["health"]["entries"]]
        self.assertNotIn("self", names)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["positive"])
        for name in ("database", "redis"):
            def drop(body, name=name):
                body["health"]["entries"] = [entry for entry in body["health"]["entries"] if entry["name"] != name]
            def degrade(body, name=name):
                next(entry for entry in body["health"]["entries"] if entry["name"] == name)["status"] = "Degraded"
                body["health"]["status"] = body["overallStatus"] = "Degraded"
            for transform, message in ((drop, "real database/Redis checks missing"), (degrade, "required health check is not healthy")):
                with self.subTest(name=name, change=transform.__name__), self.assertRaisesRegex(ValueError, message):
                    validation.judge(FIXTURE, bootstrap(), mutate_payload(controls(), transform))

    def test_fabricated_zero_wrong_status_backend_or_missing_real_checks_refuse(self):
        mutations = [lambda b: b["geoprocessing"].update(totalActive=0, buckets=[]),
            lambda b: b["geoprocessing"]["buckets"][0].update(status="Running"),
            lambda b: b["geoprocessing"]["buckets"][0].update(backend="invented"),
            lambda b: b["geoprocessing"].update(available=False),
            lambda b: b["health"].update(entries=[]),
            lambda b: b["health"]["entries"][0].update(status="Unhealthy"),
            lambda b: b.update(overallStatus="Unhealthy"),
            lambda b: b["servingLatency"]["protocols"][0].update(errorRate=1),
            lambda b: b["database"].update(hasConnectionPoolData=False),
            lambda b: b["deploy"].update(status="blocked")]
        for transform in mutations:
            with self.subTest(transform=transform): self.refused(mutate_payload(controls(), transform))

    def test_posture_dropped_stale_future_unverified_hidden_source_refuses(self):
        for name in ("missing", "duplicate", "stale", "future", "unverified", "last-success", "coverage", "hidden"):
            with self.subTest(name=name):
                def change(body):
                    posture = body["evidencePosture"]; source = posture["sources"][1]
                    if name == "missing": posture["sources"].pop()
                    elif name == "duplicate": posture["sources"][-1] = copy.deepcopy(source)
                    elif name == "stale": source["validUntil"] = timestamp(0)
                    elif name == "future": source["observedAt"] = timestamp(1000)
                    elif name == "unverified": source["backendKind"] = "unverified"
                    elif name == "last-success": source.pop("lastSuccessfulAt")
                    elif name == "coverage": posture["sources"][0]["coverage"]["includedComponentIds"] = []
                    else: posture["status"] = "unavailable"
                self.refused(mutate_payload(controls(), change))

    def test_raw_wire_launch_session_anonymous_and_candidate_refusals(self):
        for name in ("missing-wire", "raw-hash", "session", "anonymous-auth", "fake-client", "refusal-code", "restart", "cached"):
            with self.subTest(name=name):
                value = controls()
                if name == "missing-wire": value["calls"][0].pop("wire")
                elif name == "raw-hash": value["calls"][0]["wire"]["invocation"]["response"]["bodySha256"] = "sha256:" + "0" * 64
                elif name == "session": value["calls"][1]["wire"]["initialize"]["response"]["sessionSha256"] = value["calls"][0]["wire"]["initialize"]["response"]["sessionSha256"]
                elif name == "anonymous-auth": value["calls"][4]["wire"]["invocation"]["request"]["credentialPresent"] = True
                elif name == "fake-client": value["calls"][0]["client"]["processId"] = 0
                elif name == "refusal-code": value["calls"][3]["result"]["error"]["data"]["code"] = "invalid_argument"; rebind(value)
                elif name == "restart": value["runtimeAfter"] = value["runtimeBefore"]
                else: value["calls"][6]["result"] = copy.deepcopy(value["calls"][0]["result"]); rebind(value)
                self.refused(value)

    def test_live_empty_queue_requires_real_empty_independent_census(self):
        value = controls()
        for entry in value["calls"]:
            if "before" not in entry: continue
            for key in ("before", "after"):
                entry[key]["redis"]["jobs"][0]["status"] = 3; entry[key]["redis"]["activeIds"] = []
            if entry["method"] == "tools/call":
                entry["result"]["structuredContent"]["geoprocessing"] = {"available": True, "totalActive": 0, "buckets": []}
                entry["result"]["content"][0]["text"] = "Ops health snapshot returned in structuredContent."
            else:
                body = json.loads(entry["result"]["contents"][0]["text"]); body["geoprocessing"] = {"available": True, "totalActive": 0, "buckets": []}
                entry["result"]["contents"][0]["text"] = json.dumps(body)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), rebind(value))["positive"])

    def test_concurrent_alert_poll_uses_server_skew_and_actual_read_bracket(self):
        def move_poll(body, delta):
            stamp = (validation.instant(body["generatedAt"]) + validation.timedelta(seconds=delta)).isoformat()
            source = next(row for row in body["evidencePosture"]["sources"] if row["sourceId"] == "honua_ops_health.alert_dispatch")
            source.update(observedAt=stamp, lastSuccessfulAt=stamp,
                          validUntil=(validation.instant(stamp) + validation.timedelta(seconds=120)).isoformat())
            body["alertDispatch"].update(lastPollAt=stamp, backlogObservedAt=stamp)
            aggregate = body["evidencePosture"]["sources"][0]
            aggregate.update(observedAt=body["generatedAt"], lastSuccessfulAt=body["generatedAt"],
                             validUntil=(validation.instant(body["generatedAt"]) + validation.timedelta(seconds=120)).isoformat())
        positive = mutate_payload(controls(), lambda body: move_poll(body, 0.25))
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), positive)["positive"])
        outside_bracket = mutate_payload(controls(), lambda body: move_poll(body, 3))
        self.refused(outside_bracket)
        outside_skew = controls()
        outside_skew["calls"][0]["after"] = independent(130)
        mutate_payload(outside_skew, lambda body: move_poll(body, 61))
        self.refused(outside_skew)

    def test_independent_database_timing_is_telemetry_not_a_new_latency_gate(self):
        value = controls()
        for entry in value["calls"]:
            if "before" in entry:
                entry["before"]["databaseProbeDurationMs"] = 3600
                entry["after"]["databaseProbeDurationMs"] = 4200
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["independentStores"])

    def test_known_server_media_shapes_reject_malformed_nested_fields(self):
        channel = {"channel": "owned-channel", "paused": False, "pendingCount": 0, "retryingCount": 0, "deadLetteredCount": 0, "oldestItemAgeSeconds": 0}
        for name in ("description", "attestation", "protocol-extra", "channel-kind", "channel-paused", "channel-count", "unknown-reason"):
            with self.subTest(name=name):
                def change(body):
                    if name == "description": body["health"]["entries"][0]["description"] = {}
                    elif name == "attestation": body["health"]["entries"][0]["outputStoreAttestation"] = {"secret": 42}
                    elif name == "protocol-extra": body["servingLatency"]["protocols"][0]["unknown"] = True
                    elif name == "unknown-reason": body["evidencePosture"]["sources"][0]["reasonCodes"] = ["invented"]
                    else:
                        item = copy.deepcopy(channel)
                        if name == "channel-kind": body["alertDispatch"]["channels"] = [42]; return
                        if name == "channel-paused": item["paused"] = 0
                        else: item["pendingCount"] = False
                        body["alertDispatch"]["channels"] = [item]
                self.refused(mutate_payload(controls(), change))

    def test_nullable_release_version_and_replica_count_preserve_supported_types(self):
        for absent in (False, True):
            def nullable(body):
                body["deploy"]["platformRelease"]["releaseVersion"] = None
                if absent: body["servingLatency"].pop("clusterReplicaCount")
                else: body["servingLatency"]["clusterReplicaCount"] = None
            self.assertTrue(validation.judge(FIXTURE, bootstrap(), mutate_payload(controls(), nullable))["mediaSchema"])
        for field, malformed in (("releaseVersion", 42), ("clusterReplicaCount", False), ("clusterReplicaCount", 0)):
            with self.subTest(field=field, malformed=malformed):
                def change(body):
                    target = body["deploy"]["platformRelease"] if field == "releaseVersion" else body["servingLatency"]
                    target[field] = malformed
                self.refused(mutate_payload(controls(), change))

    def test_exact_server_large_payload_summary_never_substitutes_for_structured_semantics(self):
        value = controls()
        self.assertEqual(value["calls"][0]["result"]["content"][0]["text"], "Ops health snapshot returned in structuredContent.")
        for name in ("arbitrary", "conflicting-json", "missing-structured", "extra-media"):
            with self.subTest(name=name):
                value = controls(); result = value["calls"][0]["result"]
                if name == "arbitrary": result["content"][0]["text"] = "healthy"
                elif name == "conflicting-json": result["content"][0]["text"] = "{}"
                elif name == "missing-structured": result.pop("structuredContent")
                else: result["content"].append({"type": "image", "mimeType": "image/png", "data": "AA=="})
                self.refused(rebind(value))

    def test_source_specific_postures_cannot_invent_alert_heartbeat_or_metrics(self):
        for name in ("alert-backend", "alert-heartbeat", "disabled-alert", "failed-alert", "absent-pool", "aggregate-newer", "unknown-replicas"):
            with self.subTest(name=name):
                def change(body):
                    sources = {source["sourceId"]: source for source in body["evidencePosture"]["sources"]}
                    if name == "alert-backend": sources["honua_ops_health.alert_dispatch"]["backendKind"] = "inProcess"
                    elif name == "alert-heartbeat": body["alertDispatch"]["backlogObservedAt"] = timestamp(1)
                    elif name == "disabled-alert": body["alertDispatch"]["dispatcherEnabled"] = False
                    elif name == "failed-alert": body["alertDispatch"]["storagePollFailing"] = True
                    elif name == "absent-pool": body["database"]["hasConnectionPoolData"] = False; body["database"].pop("connectionPoolUtilization")
                    elif name == "aggregate-newer": sources["honua_ops_health"].update(observedAt=body["generatedAt"], lastSuccessfulAt=body["generatedAt"], validUntil=timestamp(182))
                    else: sources["honua_ops_health.serving_latency"]["coverage"]["expectedReplicaCount"] = None
                self.refused(mutate_payload(controls(), change))

    def test_truthful_absent_metrics_disabled_alert_and_partial_replica_coverage_remain_visible(self):
        value = controls()
        for entry in value["calls"]:
            if "before" not in entry: continue
            body = entry["result"]["structuredContent"] if entry["method"] == "tools/call" else json.loads(entry["result"]["contents"][0]["text"])
            body["database"].update(hasConnectionPoolData=False); body["database"].pop("connectionPoolUtilization")
            body["alertDispatch"].update(dispatcherEnabled=False)
            sources = {source["sourceId"]: source for source in body["evidencePosture"]["sources"]}
            database = sources["honua_ops_health.database_cache"]
            database.update(completeness="unavailable", reasonCodes=["missingObservationTime", "neverSucceeded", "sourceUnavailable"])
            alert = sources["honua_ops_health.alert_dispatch"]
            alert.update(completeness="notConfigured", reasonCodes=["missingObservationTime", "neverSucceeded", "notConfigured"])
            for source in (database, alert):
                for key in ("observedAt", "lastSuccessfulAt", "validUntil"): source.pop(key)
            alert.pop("maximumAgeSeconds")
            latency = sources["honua_ops_health.serving_latency"]
            latency.update(completeness="partial", reasonCodes=["incompleteCoverage"]); latency["coverage"]["expectedReplicaCount"] = None
            aggregate = sources["honua_ops_health"]
            aggregate["coverage"]["includedComponentIds"] = sorted(source["sourceId"] for source in body["evidencePosture"]["sources"] if source is not aggregate and source["completeness"] == "complete")
            aggregate.update(completeness="partial", reasonCodes=["incompleteCoverage"], observedAt=body["generatedAt"], lastSuccessfulAt=body["generatedAt"], validUntil=timestamp(int((validation.instant(body["generatedAt"])-validation.instant(timestamp(0))).total_seconds())+120))
            body["evidencePosture"]["status"] = "unavailable"
            if entry["method"] == "tools/call": entry["result"]["content"][0]["text"] = "Ops health snapshot returned in structuredContent."
            else: entry["result"]["contents"][0]["text"] = json.dumps(body)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), rebind(value))["positive"])

    def test_owned_redis_probe_boundaries_and_census_parsing(self):
        job = {"operationId": "controlled", "status": 1, "spec": {"backend": "local"}}
        reply = [b"PONG", b"owned-health-probe", b"1", [[b"jobs", b"controlled", json.dumps(job).encode()]], [b"controlled"]]
        seen = []
        result = state.queue_probe("abcdef123456", execute=lambda args: seen.append(args) or reply)
        self.assertEqual(result["jobs"][0]["status"], 1)
        self.assertEqual(seen[0][0], "EVAL"); self.assertIn("'NX','PX','5000'", seen[0][1]); self.assertIn("redis.call('DEL',key)", seen[0][1])
        for broken in ([b"wrong", *reply[1:]], [*reply[:2], b"0", *reply[3:]], [*reply[:3], [[b"jobs", b"wrong", json.dumps(job).encode()]], reply[4]]):
            with self.assertRaises(ValueError): state.queue_probe("abcdef123456", execute=lambda _: broken)
        with self.assertRaises(ValueError): state.queue_probe("unowned:';", execute=lambda _: self.fail("must refuse before store use"))


if __name__ == "__main__":
    unittest.main()
