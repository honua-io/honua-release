"""Authored live Ops health reads with independent store/queue adjudication."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_map_package_validation as wire
from mcp_render_validation import negative_output

SCHEMA = "honua.mcp-ops-health-scenario/v1"
OPERATIONS = ["tools/call:ops_health", "resources/read:Ops health"]
URI = "honua://ops/health"
STATES = ("Queued", "Provisioning", "Running", "Succeeded", "Failed", "Cancelled")
SECTIONS = ("health_checks", "serving_latency", "gp_queue", "alert_dispatch", "deploy_release", "database_cache")


def instant(value):
    if not isinstance(value, str): raise ValueError("health timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None: raise ValueError("health timestamp is not aware")
    return parsed


def number(value, lower=0, upper=None):
    if type(value) not in (int, float) or not math.isfinite(value) or value < lower or (upper is not None and value > upper):
        raise ValueError("health numeric field malformed")
    return value


def scenario(fixture, bootstrap, nonce, canonical):
    binding = wire.scenario(fixture, bootstrap)
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12,32}", nonce) or canonical not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("health requires authored nonce/canonical client")
    calls = [
        {"id": "tool", "method": "tools/call", "params": {"name": "honua_ops_health", "arguments": {}}, "authenticated": True},
        {"id": "resource", "method": "resources/read", "params": {"uri": URI}, "authenticated": True},
        {"id": "invalid-tool", "method": "tools/call", "params": {"name": "honua_ops_health", "arguments": {"unexpected": nonce}}, "authenticated": True},
        {"id": "unknown-resource", "method": "resources/read", "params": {"uri": URI + "/unknown-" + nonce}, "authenticated": True},
        {"id": "anonymous-tool", "method": "tools/call", "params": {"name": "honua_ops_health", "arguments": {}}, "authenticated": False},
        {"id": "anonymous-resource", "method": "resources/read", "params": {"uri": URI}, "authenticated": False},
        {"id": "restarted-tool", "method": "tools/call", "params": {"name": "honua_ops_health", "arguments": {}}, "authenticated": True},
        {"id": "restarted-resource", "method": "resources/read", "params": {"uri": URI}, "authenticated": True}]
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "schema": SCHEMA, "operations": OPERATIONS, "nonce": nonce, "canonicalClient": canonical, "calls": calls}


def census(value, bootstrap):
    if (not isinstance(value, dict) or set(value) != {"schema", "startedAt", "completedAt", "databaseIdentity", "database", "databaseProbeDurationMs", "redis"}
            or value["schema"] != "honua.mcp-ops-health-observation/v1"
            or instant(value["completedAt"]) < instant(value["startedAt"])):
        raise ValueError("independent live health observation missing")
    identity, database = value["databaseIdentity"], value["database"]
    if (not isinstance(identity, dict) or set(identity) != {"clusterId", "database", "user"}
            or not isinstance(identity["clusterId"], str) or not re.fullmatch(r"[0-9]+", identity["clusterId"])
            or identity["database"] != "honua_test" or identity["user"] != "honua"
            or not isinstance(database, dict) or set(database) != {"database", "user", "probe", "postgis", "area"}
            or database["database"] != "honua_test" or database["user"] != "honua" or type(database["probe"]) is not int or database["probe"] != 1
            or not isinstance(database["postgis"], str) or not database["postgis"]
            or not wire.same_json(database["area"], bootstrap["areaReadback"])):
        raise ValueError("actual owned database/PostGIS/fixture identity differs")
    number(value["databaseProbeDurationMs"])
    redis = value["redis"]
    if (not isinstance(redis, dict) or set(redis) != {"ping", "cacheRoundtrip", "probeRemoved", "jobs", "activeIds"}
            or redis["ping"] != "PONG" or redis["cacheRoundtrip"] is not True or redis["probeRemoved"] is not True
            or not isinstance(redis["jobs"], list) or len(redis["jobs"]) > 10000
            or not isinstance(redis["activeIds"], list) or len(redis["activeIds"]) > 10000):
        raise ValueError("actual bounded Redis ping/cache/census missing")
    identities, active, counts = set(), set(redis["activeIds"]), Counter()
    if len(active) != len(redis["activeIds"]) or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", item) for item in active):
        raise ValueError("actual Redis active-index identifiers malformed")
    for row in redis["jobs"]:
        if (not isinstance(row, dict) or set(row) != {"jobId", "status", "backend", "sha256"}
                or not isinstance(row["jobId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", row["jobId"])
                or row["jobId"] in identities or type(row["status"]) is not int or not 0 <= row["status"] < len(STATES)
                or not isinstance(row["sha256"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["sha256"])
                or row["backend"] is not None and not isinstance(row["backend"], str)):
            raise ValueError("actual Redis job census malformed")
        identities.add(row["jobId"])
        if row["status"] < 3:
            if row["jobId"] not in active: raise ValueError("nonterminal durable job absent from live index")
            backend = row["backend"] if row["backend"] and row["backend"].strip() else "unknown"
            counts[(STATES[row["status"]], backend)] += 1
    if active - identities: raise ValueError("active index references missing durable job")
    return [{"status": status, "backend": backend, "count": count} for (status, backend), count in sorted(counts.items())]


def posture(value, generated):
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "status", "sources"} or value["schemaVersion"] != "1.0" or not isinstance(value["sources"], list):
        raise ValueError("health evidence posture missing")
    sources = value["sources"]
    expected = {"honua_ops_health"} | {"honua_ops_health." + suffix for suffix in SECTIONS}
    if len(sources) != len(expected) or {source.get("sourceId") for source in sources} != expected:
        raise ValueError("health evidence sections dropped/duplicated")
    by_id = {source["sourceId"]: source for source in sources}
    actionable = []
    for source in sources:
        if not isinstance(source, dict) or set(source) - {"sourceId", "backendKind", "backendId", "observedAt", "lastSuccessfulAt", "completeness", "reasonCodes", "coverage", "maximumAgeSeconds", "validUntil"}:
            raise ValueError("health source contains unsupported schema fields")
        for key in ("observedAt", "lastSuccessfulAt", "validUntil"):
            if source.get(key) is not None: instant(source[key])
        complete = source.get("completeness")
        if complete not in ("complete", "partial", "unavailable", "notConfigured") or not isinstance(source.get("reasonCodes"), list):
            raise ValueError("health evidence completeness malformed")
        reasons = source["reasonCodes"]
        allowed_reasons = {"sourceUnavailable", "neverSucceeded", "stale", "missingObservationTime", "malformedObservationTime", "futureObservationTime", "partialResult", "incompleteCoverage", "truncated", "backendUnverified", "notConfigured"}
        if any(not isinstance(reason, str) or reason not in allowed_reasons for reason in reasons) or reasons != sorted(set(reasons)):
            raise ValueError("health source reason vocabulary/order malformed")
        if complete == "complete":
            if (source["reasonCodes"] or source.get("observedAt") is None or source.get("maximumAgeSeconds") is None
                    or source.get("lastSuccessfulAt") is None or source.get("backendKind") not in ("inProcess", "durableStore", "configProjection", "composite")
                    or not isinstance(source.get("backendId"), str) or not source["backendId"]):
                raise ValueError("health claims complete without actionable evidence")
            observed, expiry = instant(source["observedAt"]), instant(source.get("validUntil"))
            if instant(source["lastSuccessfulAt"]) != observed or observed > generated or expiry < generated or (expiry - observed).total_seconds() != number(source["maximumAgeSeconds"], lower=1):
                raise ValueError("health claims stale/future evidence complete")
            if source["sourceId"] != "honua_ops_health": actionable.append(source["sourceId"])
        elif complete in ("unavailable", "notConfigured") and not source["reasonCodes"]:
            raise ValueError("missing-source posture lacks reason")
    for suffix, backend, identifier in (("health_checks", "inProcess", "aspnet-health-checks"), ("gp_queue", "durableStore", "execution-job-store")):
        source = by_id["honua_ops_health." + suffix]
        if source.get("backendKind") != backend or source.get("backendId") != identifier or source.get("completeness") != "complete":
            raise ValueError("independently healthy configured source hidden/unverified")
    aggregate = by_id["honua_ops_health"]
    coverage = aggregate.get("coverage")
    if (aggregate.get("backendKind") != "composite" or aggregate.get("backendId") != "ops-health-composer"
            or not isinstance(coverage, dict) or coverage.get("expectedComponentIds") != sorted(expected - {"honua_ops_health"})
            or coverage.get("includedComponentIds") != sorted(actionable)
            or aggregate["completeness"] != ("complete" if len(actionable) == 6 else "partial" if actionable else "unavailable")):
        raise ValueError("aggregate hides missing health sections")
    summary = "complete" if all(source["completeness"] == "complete" for source in sources) else (
        "unavailable" if any(source["completeness"] in ("unavailable", "notConfigured") for source in sources) else "partial")
    if value["status"] != summary: raise ValueError("top-level health posture hides missing evidence")
    if actionable:
        oldest = min(instant(by_id[name]["observedAt"]) for name in actionable)
        if instant(aggregate.get("observedAt")) != oldest or instant(aggregate.get("lastSuccessfulAt")) != oldest:
            raise ValueError("aggregate invents a newer component observation")
    return by_id


def tool_output(result):
    if (not isinstance(result, dict) or result.get("isError", False) is not False
            or set(result) - {"isError", "structuredContent", "content"}
            or not isinstance(result.get("structuredContent"), dict)
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1):
        raise ValueError("health tool lacks actual structured payload and text projection")
    item, value = result["content"][0], result["structuredContent"]
    if not isinstance(item, dict) or set(item) != {"type", "text"} or item["type"] != "text" or not isinstance(item["text"], str):
        raise ValueError("health tool contains unrelated media")
    # Server SuccessJsonElement uses a1024-character threshold. A full health
    # payload is large; OpsHealthTool supplies this exact informational summary.
    # The complete structured payload remains mandatory and is judged below.
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=True)) >= 1024:
        if item["text"] != "Ops health snapshot returned in structuredContent.":
            raise ValueError("health large-payload text differs from actual server summary")
    elif not wire.same_json(wire.strict_json(item["text"]), value):
        raise ValueError("health inline JSON differs from structured payload")
    return value


def health_output(result, method, before, after, bootstrap):
    expected = census(before, bootstrap)
    if census(after, bootstrap) != expected or before["databaseIdentity"] != after["databaseIdentity"] or before["redis"]["jobs"] != after["redis"]["jobs"]:
        raise ValueError("underlying health/queue changed inside observation bracket")
    if method == "tools/call": value = tool_output(result)
    else:
        contents = result.get("contents") if isinstance(result, dict) else None
        if not isinstance(contents, list) or len(contents) != 1 or contents[0].get("uri") != URI or contents[0].get("mimeType") != "application/json":
            raise ValueError("health resource lacks exact JSON media/URI")
        value = wire.strict_json(contents[0]["text"])
    if not isinstance(value, dict) or set(value) != {"evidencePosture", "generatedAt", "overallStatus", "health", "servingLatency", "geoprocessing", "alertDispatch", "deploy", "database"}:
        raise ValueError("health response lacks server-supported sections")
    generated = instant(value["generatedAt"])
    if not instant(before["completedAt"]) <= generated <= instant(after["startedAt"]):
        raise ValueError("health timestamp outside independent live observations")
    checks = value["health"]
    if not isinstance(checks, dict) or set(checks) != {"status", "totalDurationMs", "entries"} or not isinstance(checks["entries"], list) or not checks["entries"]:
        raise ValueError("actual health checks missing")
    number(checks["totalDurationMs"])
    names, statuses = [], []
    for entry in checks["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or entry.get("status") not in ("Healthy", "Degraded", "Unhealthy"):
            raise ValueError("health check name/status malformed")
        if set(entry) - {"name", "status", "description", "durationMs", "outputStoreAttestation"} or entry.get("description") is not None and not isinstance(entry["description"], str):
            raise ValueError("health check description/schema malformed")
        attestation = entry.get("outputStoreAttestation")
        if attestation is not None and (entry["name"] != "gp-output-store" or not isinstance(attestation, dict)
                or any(not isinstance(key, str) or not isinstance(item, str) for key, item in attestation.items())):
            raise ValueError("output-store attestation schema malformed")
        names.append(entry["name"]); statuses.append(entry["status"]); number(entry.get("durationMs"))
    if names != sorted(set(names)) or any(name not in names for name in ("database", "redis", "self")):
        raise ValueError("real database/Redis/self checks missing or duplicated")
    for entry in checks["entries"]:
        if entry["name"] in ("database", "redis", "self") and entry["status"] != "Healthy":
            raise ValueError("owned candidate required health check is not healthy")
    worst = max(statuses, key=("Healthy", "Degraded", "Unhealthy").index)
    if checks["status"] != worst or value["overallStatus"] != worst:
        raise ValueError("health rollup hides actual degraded/unhealthy check")
    queue = value["geoprocessing"]
    if (not isinstance(queue, dict) or set(queue) != {"totalActive", "available", "buckets"} or queue["available"] is not True
            or not wire.same_json(queue["buckets"], expected) or type(queue["totalActive"]) is not int
            or queue["totalActive"] != sum(row["count"] for row in expected)):
        raise ValueError("health GP queue differs from actual independent Redis census")
    latency = value["servingLatency"]
    if not isinstance(latency, dict) or not isinstance(latency.get("protocols"), list):
        raise ValueError("serving latency projection missing")
    if set(latency) - {"windowSeconds", "protocols", "clusterReplicaCount"}: raise ValueError("serving latency schema fields differ")
    number(latency.get("windowSeconds"), lower=1)
    protocols = set()
    for row in latency["protocols"]:
        if not isinstance(row, dict) or not isinstance(row.get("protocol"), str) or not row["protocol"] or row["protocol"] in protocols:
            raise ValueError("serving protocol identity malformed")
        if set(row) != {"protocol", "requestCount", "errorCount", "errorRate", "p50Ms", "p95Ms", "p99Ms", "maxMs"}:
            raise ValueError("serving protocol schema fields differ")
        protocols.add(row["protocol"])
        for key in ("requestCount", "errorCount"):
            if type(row.get(key)) is not int: raise ValueError("serving sample count malformed")
            number(row[key])
        if row["errorCount"] > row["requestCount"] or number(row.get("errorRate"), upper=1) != (row["errorCount"] / row["requestCount"] if row["requestCount"] else 0):
            raise ValueError("serving error ratio differs from observed counts")
        quantiles = [number(row.get(key)) for key in ("p50Ms", "p95Ms", "p99Ms", "maxMs")]
        if quantiles != sorted(quantiles): raise ValueError("serving latency quantiles inverted")
    database = value["database"]
    if not isinstance(database, dict) or type(database.get("hasConnectionPoolData")) is not bool:
        raise ValueError("database telemetry projection missing")
    if set(database) - {"connectionPoolUtilization", "hasConnectionPoolData", "activeConnections", "connectionAcquisitionTimeouts", "connectionAcquisitionFailures", "cacheHitRatio", "errorRate"}:
        raise ValueError("database telemetry schema fields differ")
    for key in ("activeConnections", "connectionAcquisitionTimeouts", "connectionAcquisitionFailures"):
        if type(database.get(key)) is not int: raise ValueError("database counter malformed")
        number(database[key])
    for key in ("cacheHitRatio", "errorRate"): number(database.get(key), upper=1)
    if database["hasConnectionPoolData"]: number(database.get("connectionPoolUtilization"), upper=1)
    elif database.get("connectionPoolUtilization") is not None: raise ValueError("database invents absent pool data")
    alert = value["alertDispatch"]
    if not isinstance(alert, dict) or any(type(alert.get(key)) is not bool for key in ("dispatcherRunning", "dispatcherEnabled", "storagePollFailing")):
        raise ValueError("alert dispatch availability malformed")
    if set(alert) - {"dispatcherRunning", "dispatcherEnabled", "storagePollFailing", "lastPollAt", "backlogObservedAt", "pendingCount", "deadLetteredCount", "retryingCount", "oldestItemAgeSeconds", "channels"}:
        raise ValueError("alert telemetry schema fields differ")
    for key in ("pendingCount", "deadLetteredCount", "retryingCount", "oldestItemAgeSeconds"):
        if alert.get(key) is not None:
            if type(alert[key]) is not int: raise ValueError("alert backlog counter malformed")
            number(alert[key])
    for key in ("lastPollAt", "backlogObservedAt"):
        if alert.get(key) is not None and instant(alert[key]) > generated: raise ValueError("alert observation is in future")
    if not isinstance(alert.get("channels"), list): raise ValueError("alert channel projection missing")
    channels = set()
    for channel in alert["channels"]:
        if (not isinstance(channel, dict) or set(channel) != {"channel", "paused", "pendingCount", "retryingCount", "deadLetteredCount", "oldestItemAgeSeconds"}
                or not isinstance(channel["channel"], str) or not channel["channel"] or channel["channel"] in channels or type(channel["paused"]) is not bool):
            raise ValueError("alert channel schema malformed")
        channels.add(channel["channel"])
        for key in ("pendingCount", "retryingCount", "deadLetteredCount", "oldestItemAgeSeconds"):
            if type(channel[key]) is not int: raise ValueError("alert channel count malformed")
            number(channel[key])
    deploy = value["deploy"]
    if (not isinstance(deploy, dict) or deploy.get("status") not in ("ready", "blocked")
            or type(deploy.get("readyForCoordinatedDeploy")) is not bool
            or deploy["readyForCoordinatedDeploy"] != (deploy["status"] == "ready")):
        raise ValueError("deploy readiness projection contradicts status")
    if set(deploy) != {"status", "readyForCoordinatedDeploy", "pendingMigrationsCount", "pendingContractScriptsCount", "platformRelease"}:
        raise ValueError("deploy readiness schema fields differ")
    for key in ("pendingMigrationsCount", "pendingContractScriptsCount"):
        if type(deploy.get(key)) is not int: raise ValueError("deploy migration count malformed")
        number(deploy[key])
    release = deploy.get("platformRelease")
    if (not isinstance(release, dict) or any(type(release.get(key)) is not bool for key in ("releaseDeclared", "isCoVersioned"))
            or not isinstance(release.get("skewedIds"), list) or any(not isinstance(item, str) or not item for item in release["skewedIds"])
            or release["releaseDeclared"] and (not isinstance(release.get("releaseVersion"), str) or not release["releaseVersion"])
            or release["isCoVersioned"] and release["skewedIds"]):
        raise ValueError("declared release posture malformed")
    if set(release) - {"releaseVersion", "releaseDeclared", "isCoVersioned", "skewedIds"}:
        raise ValueError("release posture schema fields differ")
    sources = posture(value["evidencePosture"], generated)
    for suffix, backend, identifier in (("health_checks", "inProcess", "aspnet-health-checks"),
            ("serving_latency", "inProcess", "honua-telemetry"), ("gp_queue", "durableStore", "execution-job-store"),
            ("alert_dispatch", "durableStore", "alert-dispatch-store"), ("deploy_release", "configProjection", "control-plane-options"),
            ("database_cache", "inProcess", "production-metrics")):
        source = sources["honua_ops_health." + suffix]
        if source.get("backendKind") != backend or source.get("backendId") != identifier:
            raise ValueError("health section source/backend identity differs from actual server contract")
        if suffix in ("health_checks", "gp_queue", "deploy_release"):
            if (source["completeness"] != "complete" or instant(source["observedAt"]) != generated
                    or source.get("maximumAgeSeconds") != (300 if suffix == "deploy_release" else 120)):
                raise ValueError("fresh health/queue/config observation source differs")
    source = sources["honua_ops_health.database_cache"]
    if database["hasConnectionPoolData"]:
        if source["completeness"] != "complete" or instant(source["observedAt"]) != generated:
            raise ValueError("available pool telemetry not represented by actual complete source")
    elif source["completeness"] != "unavailable" or "sourceUnavailable" not in source["reasonCodes"]:
        raise ValueError("missing pool telemetry falsely declared available")
    source = sources["honua_ops_health.alert_dispatch"]
    if not alert["dispatcherEnabled"]:
        if source["completeness"] != "notConfigured" or "notConfigured" not in source["reasonCodes"]:
            raise ValueError("disabled alert dispatcher falsely declared actionable")
    else:
        backlog = alert.get("backlogObservedAt")
        if source.get("observedAt") != backlog or source.get("lastSuccessfulAt") != backlog or source.get("maximumAgeSeconds") != 120:
            raise ValueError("alert posture invents a heartbeat instead of actual backlog observation")
        reason = "sourceUnavailable" if alert["storagePollFailing"] else "neverSucceeded" if backlog is None else "stale" if (generated-instant(backlog)).total_seconds() > 120 else None
        if reason is None:
            if source["completeness"] != "complete": raise ValueError("valid alert observation hidden")
        elif source["completeness"] != "unavailable" or reason not in source["reasonCodes"]:
            raise ValueError("failed/missing/stale alert observation falsely declared actionable")
    source = sources["honua_ops_health.serving_latency"]
    coverage = source.get("coverage")
    replicas = latency.get("clusterReplicaCount") or 1
    if type(replicas) is not int or replicas < 1 or not isinstance(coverage, dict) or coverage.get("observedReplicaCount") != replicas:
        raise ValueError("serving latency replica coverage differs from actual projection")
    if source["completeness"] == "complete":
        if coverage.get("expectedReplicaCount") != replicas: raise ValueError("serving latency complete coverage is unknown")
    elif (source["completeness"] != "partial" or coverage.get("expectedReplicaCount") is not None
            or "incompleteCoverage" not in source["reasonCodes"]):
        raise ValueError("serving latency degraded rollup posture hidden")
    return generated


def judge(fixture, bootstrap, observed):
    if not isinstance(observed, dict): raise ValueError("health observations missing")
    authored = scenario(fixture, bootstrap, observed.get("nonce"), observed.get("canonicalClient"))
    if set(observed) != set(authored) | {"clientVersion", "runtimeBefore", "runtimeRestarted", "runtimeAfter"} or any(not wire.same_json(observed[k], v) for k, v in authored.items() if k != "calls"):
        raise ValueError("health observations differ from authored scenario")
    version = observed["clientVersion"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version): raise ValueError("health actual client version missing")
    before, restarted, after = (observed[k] for k in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
    for runtime in (before, restarted, after):
        binding = dict(bootstrap); binding["runtimeBinding"] = runtime; wire.scenario(fixture, binding)
    if before != {**restarted, "startedAt": before["startedAt"]} or restarted != after or instant(restarted["startedAt"]) <= instant(before["startedAt"]):
        raise ValueError("health did not prove exact same-container restart and live recomputation")
    if not isinstance(observed["calls"], list) or len(observed["calls"]) != len(authored["calls"]): raise ValueError("health calls incomplete")
    sessions, fingerprints, generated = set(), set(), []
    for expected, actual in zip(authored["calls"], observed["calls"], strict=True):
        positive = expected["id"] in ("tool", "resource", "restarted-tool", "restarted-resource")
        fields = set(expected) | {"wire", "client", "result"} | ({"before", "after"} if positive else set())
        if not isinstance(actual, dict) or set(actual) != fields or any(not wire.same_json(actual[k], v) for k, v in expected.items()):
            raise ValueError("health invocation differs from authored method/parameters")
        frame, session = wire.observed_wire(actual["wire"], expected["method"], expected["params"], expected["authenticated"])
        if session in sessions or not wire.same_json(actual["result"], frame if "error" in frame else frame["result"]):
            raise ValueError("health reused session or altered actual response")
        sessions.add(session)
        if expected["authenticated"]: fingerprints.add(actual["wire"]["expectedCredentialFingerprint"])
        wire.client_observation(actual["client"], frame, actual["wire"], authored["canonicalClient"], version)
        if positive:
            stamp = health_output(actual["result"], expected["method"], actual["before"], actual["after"], bootstrap)
            runtime = restarted if expected["id"].startswith("restarted-") else before
            if stamp <= instant(runtime["startedAt"]): raise ValueError("health was cached from before actual server start")
            generated.append(stamp)
        elif expected["id"] == "invalid-tool": negative_output(actual["result"], "invalid_argument")
        elif expected["id"] == "anonymous-tool": wire.anonymous_tool_refusal(actual["result"])
        else: wire.resource_refusal(actual["result"], -32000 if not expected["authenticated"] else -32002,
                                   "unauthenticated" if not expected["authenticated"] else "not_found")
    if len(fingerprints) != 1 or generated != sorted(generated) or generated[-1] <= generated[0]:
        raise ValueError("health caller changed or live health was not recomputed")
    return {k: authored[k] for k in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient")} | {
        "clientVersion": version, "operations": OPERATIONS, "positive": True, "negative": True, "mediaSchema": True,
        "independentStores": True, "restartRecomputed": True, "anonymousInvocationRefusal": True, "liveNotPersisted": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path); parser.add_argument("--nonce"); parser.add_argument("--canonical-client")
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), wire.strict_json(args.bootstrap.read_bytes())
        value = judge(fixture, bootstrap, wire.strict_json(args.observations.read_bytes())) if args.observations else scenario(fixture, bootstrap, args.nonce, args.canonical_client)
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        print("authored live health oracle refused evidence", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
