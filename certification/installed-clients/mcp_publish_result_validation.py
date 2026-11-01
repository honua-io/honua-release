"""Authored canonical publication of a real worker-created PostGIS result.

This module is pure: setup, actual client launch and state observation remain
separate trusted release producers. A successful parser is never candidate proof.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, unquote_plus
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_map_package_validation import (strict_json, same_json, client_observation,
    anonymous_tool_refusal, resource_refusal)
from mcp_execute_plan_validation import timestamp
from mcp_render_validation import scenario as fixture_binding
from mcp_ingest_publish_validation import (OWNED_DATABASE, json_tool_output,
    resource_output as published_resource, query_arguments as published_query_arguments,
    snapshot_output as persisted_table, connection_provenance, invocation_wire, registry_output)

SCHEMA = "honua.mcp-publish-result-scenario/v1"
OPERATION = "tools/call:publish_result"


def scenario(fixture_bytes, bootstrap, nonce, canonical_client):
    binding = fixture_binding(fixture_bytes, bootstrap)
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12}", nonce):
        raise ValueError("publication requires owned run identity")
    if canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("publication requires an actual canonical client")
    connection = bootstrap.get("connectionId")
    if not isinstance(connection, str) or str(uuid.UUID(connection)) != connection or uuid.UUID(connection).int == 0:
        raise ValueError("publication requires actual registered bootstrap identity")
    fixture = strict_json(fixture_bytes)
    rows = fixture["lifecycle"]["features"]
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("authored lifecycle rows are unavailable")
    features = []
    for row in rows:
        if (set(row) != {"gid", "name", "x", "y"} or type(row["gid"]) is not int
                or not isinstance(row["name"], str)
                or any(type(row[k]) not in (int, float) for k in ("x", "y"))):
            raise ValueError("authored sink input differs from fixture contract")
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [row["x"], row["y"]]},
                         "properties": {"gid": row["gid"], "name": row["name"]}})
    payload = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"), allow_nan=False).encode()
    slug = "sdk" if canonical_client == "Official MCP TypeScript SDK" else "inspector"
    table, service, package = f"mcp_result_{slug}_{nonce}", f"mcp-result-{slug}-{nonce}", f"mcp-result-package-{slug}-{nonce}"
    batch_id = f"mcp-result-{slug}-{nonce}"
    inputs = {"input": "data:application/geo+json;base64," + base64.b64encode(payload).decode(),
              "connectionId": connection, "schema": "public", "table": table, "targetSrid": "4326", "batchId": batch_id}
    return {**{k: binding[k] for k in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "runtimeBinding")},
            "schema": SCHEMA, "nonce": nonce, "canonicalClient": canonical_client, "ownedDatabase": OWNED_DATABASE,
            "connectionId": connection, "table": table, "serviceName": service, "packageId": package, "batchId": batch_id,
            "expectedRows": [{"properties": {**f["properties"], "__pipeline_batch_id": batch_id}, "geometry": f["geometry"]} for f in features],
            "packageArguments": {"packageId": package, "name": package,
                "graph": {"schemaVersion": "workflow-package.v1", "nodes": [{"nodeId": "sink", "nodeTypeId": "process:sink.external-postgis", "parameters": inputs}], "edges": []}},
            # Active manual-run publication with a dormant scheduler: no automatic
            # second write may race this single owned execution.
            "publicationArguments": {"target": "Schedule", "enabled": True,
                "schedule": {"cronExpression": "0 0 * * *", "timeZone": "UTC", "enabled": False}}}


def publication_arguments(authored, job_id, artifact_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"gp-[a-f0-9]{24,32}", job_id) or artifact_id != job_id + ":artifact:1":
        raise ValueError("publication must use actual single committed artifact identity")
    return {"sourceId": job_id, "artifactId": artifact_id, "serviceName": authored["serviceName"], "layerName": "authored-result"}


def publication_output(result, authored, job_id, artifact_id):
    value = json_tool_output(result)
    if (value.get("status") != "Completed" or value.get("requiresApproval") is not False
            or value.get("operationId") != "service.publish" or value.get("sourceJobId") != job_id or value.get("artifactId") != artifact_id
            or value.get("serviceId") != authored["serviceName"]
            or value.get("serviceUri") != "honua://published-services/" + quote(authored["serviceName"], safe="")
            or not isinstance(value.get("layerId"), str) or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value["layerId"])
            or int(value["layerId"]) >= 2147483647
            or type(value.get("metadataRevision")) is not int or value["metadataRevision"] <= 0
            or any(not isinstance(value.get(k), str) or not value[k] for k in ("operationInstanceId", "handleId"))
            or value["handleId"] != value["operationInstanceId"]
            or any(value.get(k) is not None for k in ("proposalId", "jobId", "approvalLane"))):
        raise ValueError("result publication is incomplete or lost exact job/artifact/publication identity")
    return value


def shared_publication(published):
    # publish_service spells this field serviceName; publish_result spells it
    # serviceId. They are the same exact returned route selector, never a graph ID.
    return {**published, "serviceName": published["serviceId"]}


def table_binding(authored):
    return {"connectionId": authored["connectionId"], "schema": "public", "table": authored["table"],
            "geometryColumn": "geom", "primaryKey": "id"}


def published_snapshot(snapshot, published, authored):
    # Reuse the independent canonical source/public-index/provenance oracle.
    # The observer explicitly selects sink attributes AS properties; it never
    # pretends the sink table uses the ingest storage layout.
    persisted_table(snapshot, table_binding(authored), shared_publication(published), authored)


def resource_output(result, published, snapshot):
    return published_resource(result, shared_publication(published), snapshot)


def query_arguments(published, resource):
    # The tool's public contract documents explicit names (or omission for all),
    # not the provider wildcard. Wildcard silent-empty behavior is a separate
    # server regression; it must not replace this full authored-attribute proof.
    return {**published_query_arguments(shared_publication(published), resource), "outFields": ["id", "attributes"]}


def graph_output(graph, authored):
    expected = authored["packageArguments"]["graph"]
    if (not isinstance(graph, dict) or set(graph) - {"schemaVersion", "nodes", "edges", "schedule", "workerProfile", "editorMetadata"}
            or graph.get("schemaVersion") != expected["schemaVersion"] or graph.get("edges") != []
            or graph.get("schedule") is not None or graph.get("workerProfile") is not None or graph.get("editorMetadata", {}) != {}
            or not isinstance(graph.get("nodes"), list) or len(graph["nodes"]) != 1):
        raise ValueError("workflow graph changed authored topology or runtime configuration")
    node = graph["nodes"][0]
    if (not isinstance(node, dict) or set(node) - {"nodeId", "nodeTypeId", "parameters", "workerProfile", "metadata"}
            or node.get("workerProfile") is not None or node.get("metadata", {}) != {}
            or any(not same_json(node.get(k), expected["nodes"][0][k]) for k in ("nodeId", "nodeTypeId", "parameters"))):
        raise ValueError("workflow node lost the exact owned sink inputs")
    # Public package hash contract: length-prefixed ASCII fields, ordinal-sorted
    # parameters. All authored values here are ASCII, so UTF-16/Unicode lengths
    # agree; no server graph or hash supplies the expected input.
    fields = [expected["schemaVersion"], "", node["nodeId"], node["nodeTypeId"], ""]
    prefix = "".join(f"{len(v)}:{v};" for v in fields) + str(len(node["parameters"])) + ";"
    content = prefix + "".join(f"{len(k)}:{k};{len(v)}:{v};" for k, v in sorted(node["parameters"].items()))
    if not content.isascii():
        raise ValueError("owned workflow hash input is not bounded ASCII")
    return hashlib.sha256(content.encode()).hexdigest()


def package_output(package, authored):
    if not isinstance(package, dict) or package.get("packageId") != authored["packageId"] or package.get("name") != authored["packageId"]:
        raise ValueError("workflow draft identity changed")
    return graph_output(package.get("graph"), authored)


def version_output(version, authored, expected_hash):
    if (not isinstance(version, dict) or version.get("packageId") != authored["packageId"]
            or type(version.get("version")) is not int or version["version"] != 1
            or version.get("schemaVersion") != "workflow-package.v1" or version.get("packageHash") != expected_hash
            or graph_output(version.get("graph"), authored) != expected_hash
            or not isinstance(version.get("validation"), dict) or version["validation"].get("isValid") is not True
            or version["validation"].get("failures") != [] or version["validation"].get("packageHash") != expected_hash):
        raise ValueError("immutable workflow version/hash/validation differs")


def validation_output(checked, expected_hash):
    if not isinstance(checked, dict) or checked.get("isValid") is not True or checked.get("failures") != [] or checked.get("packageHash") != expected_hash:
        raise ValueError("workflow target did not pass actual validation")


def publication_record(publication, authored, expected_hash):
    if not isinstance(publication, dict):
        raise ValueError("workflow publication record missing")
    pubid = publication.get("publicationId")
    if (not isinstance(pubid, str) or not re.fullmatch(r"wfp-[a-f0-9]{32}", pubid)
            or publication.get("packageId") != authored["packageId"] or type(publication.get("packageVersion")) is not int or publication["packageVersion"] != 1
            or publication.get("packageHash") != expected_hash or publication.get("target") != "Schedule" or publication.get("status") != "Active"
            or not same_json(publication.get("schedule"), authored["publicationArguments"]["schedule"])
            or not isinstance(publication.get("workflowDefinitionId"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", publication["workflowDefinitionId"])
            or publication.get("endpointPath") != "/api/v1/console/workflow-publications/"+pubid+"/runs"
            or not isinstance(publication.get("eligibility"), dict) or publication["eligibility"].get("isValid") is not True
            or publication["eligibility"].get("failures") != [] or publication["eligibility"].get("packageHash") != expected_hash):
        raise ValueError("manual workflow publication is not active with a dormant owned scheduler")
    return pubid, publication["workflowDefinitionId"]


def publication_inventory(listing, publication):
    items = listing.get("items") if isinstance(listing, dict) else None
    if not isinstance(items, list) or len(items) > 1000 or any(not isinstance(item, dict) for item in items):
        raise ValueError("workflow publication inventory missing or unbounded")
    matches = [item for item in items if item.get("publicationId") == publication["publicationId"]]
    if len(matches) != 1 or not same_json(matches[0], publication):
        raise ValueError("fresh workflow publication readback changed")


def definition_output(definition, authored, workflow_id):
    if (not isinstance(definition, dict) or definition.get("workflowId") != workflow_id
            or not isinstance(definition.get("trigger"), dict)):
        raise ValueError("actual persisted workflow definition is missing")
    trigger, expected = definition["trigger"], authored["publicationArguments"]["schedule"]
    if (type(trigger.get("kind")) is not int or trigger["kind"] != 1 or trigger.get("enabled") is not False
            or trigger.get("cronExpression") != expected["cronExpression"] or trigger.get("timeZone") != expected["timeZone"]):
        raise ValueError("persisted scheduler is not the dormant authored schedule")
    steps = definition.get("steps")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict) or steps[0].get("stepId") != "sink":
        raise ValueError("persisted workflow is not the one authored sink")
    plan = steps[0].get("plan")
    if (not isinstance(plan, dict) or plan.get("planId") != workflow_id+":sink" or plan.get("intentId") != "workflow-package"
            or not same_json(plan.get("outputs"), [1]) or not isinstance(plan.get("steps"), list) or len(plan["steps"]) != 1):
        raise ValueError("persisted workflow plan has foreign identity/output/topology")
    node = plan["steps"][0]
    if (not isinstance(node, dict) or node.get("stepId") != "sink" or type(node.get("kind")) is not int or node["kind"] != 1
            or node.get("processId") != "sink.external-postgis"
            or not same_json(node.get("inputs"), authored["packageArguments"]["graph"]["nodes"][0]["parameters"])):
        raise ValueError("persisted workflow changed the positively verified owned destination or input")
    return plan


def setup_output(setup, authored):
    """Judge genuine public setup observations; never construct a result record."""
    if not isinstance(setup, dict) or set(setup) != {"schema", "phase", "steps", "workflow", "connection", "beforeRun", "runtimeBefore", "runtimeAfter"} or setup["schema"] != "honua.mcp-publish-result-setup/v1" or setup["phase"] != "complete":
        raise ValueError("actual workflow setup evidence missing")
    connection_provenance(setup["connection"], authored["connectionId"], authored["ownedDatabase"])
    # The shared bootstrap predates earlier owned-client restarts. Only that
    # historical comparison allows StartedAt drift. Setup itself must keep the
    # exact current runtime unchanged; the canonical producer separately binds
    # its CURRENT runtimeBefore to both observations before invoking clients.
    before, after = setup["runtimeBefore"], setup["runtimeAfter"]
    if (not isinstance(before, dict) or not same_json(before, after)
            or not same_json({**before, "startedAt": authored["runtimeBinding"]["startedAt"]}, authored["runtimeBinding"])):
        raise ValueError("workflow setup belongs to a changed candidate")
    steps = setup["steps"]
    if not isinstance(steps, list) or len(steps) != 6:
        raise ValueError("workflow setup request sequence incomplete")
    root = "/api/v1/console"
    fingerprints = set()
    def step(index, identifier, method, path, arguments, status):
        entry = steps[index]
        if (not isinstance(entry, dict) or set(entry) != {"id", "method", "path", "arguments", "authenticated", "credentialFingerprint", "status", "responseMediaType", "responseText", "responseSha256"}
                or entry["id"] != identifier or entry["method"] != method or entry["path"] != path
                or not same_json(entry["arguments"], arguments) or entry["authenticated"] is not True or type(entry["status"]) is not int or entry["status"] != status
                or not isinstance(entry["credentialFingerprint"], str) or not re.fullmatch(r"scrypt:[a-f0-9]{32}:[a-f0-9]{64}", entry["credentialFingerprint"])
                or not isinstance(entry["responseText"], str) or len(entry["responseText"].encode()) > 262144
                or entry["responseMediaType"] != "application/json"
                or entry["responseSha256"] != "sha256:" + hashlib.sha256(entry["responseText"].encode()).hexdigest()):
            raise ValueError("workflow setup request/response identity changed")
        fingerprints.add(entry["credentialFingerprint"])
        body = strict_json(entry["responseText"])
        if (not isinstance(body, dict) or set(body) - {"success", "data", "message", "timestamp"}
                or body.get("success") is not True or not isinstance(body.get("data"), dict) or "timestamp" not in body):
            raise ValueError("workflow setup did not return a successful typed API response")
        timestamp(body["timestamp"])
        return body["data"]
    package = step(0, "package", "POST", root+"/workflow-packages", authored["packageArguments"], 201)
    expected_hash = package_output(package, authored)
    version = step(1, "version", "POST", root+"/workflow-packages/"+authored["packageId"]+"/versions", None, 201)
    version_output(version, authored, expected_hash)
    base = root+"/workflow-packages/"+authored["packageId"]+"/versions/1"
    checked = step(2, "validate", "POST", base+"/validate", None, 200)
    validation_output(checked, expected_hash)
    publication = step(3, "publication", "POST", base+"/publish", authored["publicationArguments"], 200)
    pubid, workflow_id = publication_record(publication, authored, expected_hash)
    listing = step(4, "publication-read", "GET", root+"/workflow-publications", None, 200)
    publication_inventory(listing, publication)
    run = step(5, "run", "POST", root+"/workflow-publications/"+pubid+"/runs", {}, 201)
    runid = run.get("workflowRunId")
    if (run.get("publicationId") != pubid or run.get("packageId") != authored["packageId"] or run.get("packageVersion") != 1
            or type(run.get("packageVersion")) is not int or run.get("target") != "Schedule" or run.get("jobId") is not None
            or not isinstance(runid, str) or not re.fullmatch(r"wf-[a-f0-9]{32}", runid) or len(fingerprints) != 1):
        raise ValueError("manual workflow run lost actual publication identity")
    return {"runId": runid, "workflowId": workflow_id, "publicationId": pubid,
            "packageHash": expected_hash, "credentialFingerprint": next(iter(fingerprints))}


def census_output(census):
    if not isinstance(census, dict) or set(census) != {"jobs", "results", "workflows", "definitions", "proposals", "operations"}:
        raise ValueError("complete durable workflow census missing")
    for family, rows in census.items():
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("durable workflow census unbounded")
        ids = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"id", "sha256"} or not isinstance(row["id"], str)
                    or not 0 < len(row["id"]) <= 1024 or not isinstance(row["sha256"], str)
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["sha256"])):
                raise ValueError("durable workflow census malformed")
            ids.append(row["id"])
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("durable workflow census duplicated or incomplete")
    return census


def committed_endpoint_fingerprint(connection, authored):
    """SHA-256 hex of the verified registry endpoint, ``host|port|database``.

    Same canonical form as the server's ``ConnectionEndpointFingerprint``:
    trimmed lowercase host, decimal port, trimmed database name. The user,
    password, and connection string never enter the hash.
    """
    row = registry_output(connection["registry"], authored["connectionId"], authored["ownedDatabase"])
    canonical = f"{row['host'].strip().lower()}|{row['port']}|{row['databaseName'].strip()}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def workflow_output(setup, authored):
    identity = setup_output(setup, authored)
    value = setup["workflow"]
    if not isinstance(value, dict) or set(value) != {"schema", "census", "run", "definition", "job", "result"} or value["schema"] != "honua.mcp-publish-result-workflow-state/v1":
        raise ValueError("independent terminal workflow observation missing")
    census = census_output(value["census"])
    run, definition, job, result = (value[k] for k in ("run", "definition", "job", "result"))
    if any(not isinstance(v, dict) for v in (run, definition, job, result)):
        raise ValueError("workflow did not produce actual durable terminal job/results")
    if (run.get("runId") != identity["runId"] or run.get("workflowId") != identity["workflowId"]
            or type(run.get("status")) is not int or run["status"] != 2
            or type(run.get("triggerKind")) is not int or run["triggerKind"] != 0
            or definition.get("workflowId") != identity["workflowId"]
            or definition.get("trigger", {}).get("enabled") is not False):
        raise ValueError("workflow identity/status/manual-trigger/dormant scheduler differs")
    before = setup["beforeRun"]
    if not isinstance(before, dict) or set(before) != {"census", "definition"}:
        raise ValueError("independent before-run definition/census missing")
    baseline = census_output(before["census"])
    definition_output(before["definition"], authored, identity["workflowId"])
    if not same_json(before["definition"], definition):
        raise ValueError("workflow definition changed across the manual run")
    definition_output(definition, authored, identity["workflowId"])
    expected_metadata = {"workflow.packageId": authored["packageId"], "workflow.packageVersion": "1",
                         "workflow.publicationId": identity["publicationId"], "workflow.packageHash": identity["packageHash"],
                         "workflow.publicationTarget": "Schedule"}
    if any(run.get("metadata", {}).get(k) != v for k, v in expected_metadata.items()):
        raise ValueError("real workflow run lost package/publication provenance")
    steps, states = definition.get("steps"), run.get("stepStates")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(states, list) or len(states) != 1:
        raise ValueError("workflow did not retain the one authored sink step")
    step, state = steps[0], states[0]
    if not isinstance(step, dict) or not isinstance(state, dict):
        raise ValueError("workflow step/state is not an object")
    plan = step.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or len(plan["steps"]) != 1:
        raise ValueError("workflow plan is not the authored single sink")
    node = plan["steps"][0]
    if not isinstance(node, dict):
        raise ValueError("workflow plan step is not an object")
    job_id = state.get("jobId")
    if (step.get("stepId") != "sink" or state.get("stepId") != "sink"
            or plan.get("planId") != identity["workflowId"]+":sink" or state.get("planId") != plan["planId"]
            or plan.get("intentId") != "workflow-package" or not same_json(plan.get("outputs"), [1])
            or node.get("stepId") != "sink" or type(node.get("kind")) is not int or node["kind"] != 1
            or node.get("processId") != "sink.external-postgis"
            or not same_json(node.get("inputs"), authored["packageArguments"]["graph"]["nodes"][0]["parameters"])
            or type(state.get("status")) is not int or state["status"] != 3
            or type(state.get("attemptCount")) is not int or not 1 <= state["attemptCount"] <= 100
            or not isinstance(job_id, str) or not re.fullmatch(r"gp-[a-f0-9]{24,32}", job_id)
            or job.get("operationId") != job_id or type(job.get("status")) is not int or job["status"] != 3):
        raise ValueError("workflow step/job differs from actual authored completed execution")
    spec = job.get("spec")
    parameters = spec.get("parameters") if isinstance(spec, dict) else None
    if (not isinstance(spec, dict) or type(spec.get("kind")) is not int or spec["kind"] != 0 or not isinstance(parameters, dict)
            or parameters.get("honua.geoprocessing.plan_id") != plan["planId"]
            or parameters.get("honua.geoprocessing.process_definitions") != "sink.external-postgis"
            or parameters.get("honua.geoprocessing.output_artifact_kinds") != "FeatureLayer"
            or any(parameters.get("honua.geoprocessing.step.0."+k) != v for k,v in node["inputs"].items())):
        raise ValueError("actual worker immutable spec lost the exact owned sink binding")
    references, committed = job.get("artifactReferences"), job.get("committedEffectReferences")
    if (not isinstance(references, list) or len(references) != 1 or not same_json(references, committed)
            or not isinstance(references[0], str) or not references[0].startswith("data:application/json;base64,") or len(references[0]) > 4096):
        raise ValueError("actual worker lacks exactly one matching committed sink receipt")
    receipt = strict_json(base64.b64decode(references[0].split(",",1)[1], validate=True))
    fingerprint = committed_endpoint_fingerprint(setup["connection"], authored)
    expected = {"processId": "sink.external-postgis", "schema": "public", "table": authored["table"],
                "batchId": authored["batchId"], "committed": True, "featuresWritten": len(authored["expectedRows"]),
                "featuresRejected": 0, "endpointFingerprint": fingerprint}
    if not same_json(receipt, expected):
        raise ValueError("worker committed receipt is partial or differs from authored destination/counts")
    artifacts = result.get("artifacts")
    if (type(result.get("status")) is not int or result["status"] != 5 or result.get("errors") != []
            or not isinstance(result.get("resultPackageId"), str) or not result["resultPackageId"]
            or not isinstance(artifacts, list) or len(artifacts) != 1):
        raise ValueError("worker did not create a complete successful result package")
    artifact = artifacts[0]
    if (not isinstance(artifact, dict) or artifact.get("artifactId") != job_id+":artifact:1"
            or type(artifact.get("kind")) is not int or artifact["kind"] != 1
            or artifact.get("uri") != references[0] or artifact.get("contentType") != "application/json"
            or not same_json(artifact.get("metadata"), {"connectionId": authored["connectionId"], "schema": "public", "table": authored["table"],
                "connectionEndpointFingerprint": fingerprint})):
        raise ValueError("actual result artifact has conflicting media or missing committed destination metadata")
    for family, identifier in (("workflows", identity["runId"]), ("definitions", identity["workflowId"]), ("jobs", job_id), ("results", job_id)):
        if sum(row["id"] == identifier for row in census[family]) != 1:
            raise ValueError("actual workflow/job/result identity missing from durable census")
    additions = {"workflows": identity["runId"], "jobs": job_id, "results": job_id}
    for family, rows in census.items():
        prior = {row["id"]: row["sha256"] for row in baseline[family]}
        current = {row["id"]: row["sha256"] for row in rows}
        if family in additions:
            added = additions[family]
            if added in prior or set(current) != set(prior) | {added} or any(current[k] != v for k,v in prior.items()):
                raise ValueError("manual workflow created unrelated/duplicate work or changed prior durable records")
        elif not same_json(rows, baseline[family]):
            raise ValueError("manual workflow changed unrelated definitions/proposals/operations")
    return {**identity, "jobId": job_id, "artifactId": artifact["artifactId"], "artifact": artifact,
            "resultPackageId": result["resultPackageId"], "census": census}


def safe_response(text, secrets):
    value = strict_json(text)
    def safe_string(value):
        decoded = value
        for _ in range(16):
            if any(secret and secret in decoded for secret in secrets):
                raise ValueError("workflow setup response contains credential material")
            expanded = unquote_plus(decoded, encoding="utf-8", errors="strict")
            if expanded == decoded:
                return
            decoded = expanded
        raise ValueError("workflow setup response encoding exceeds safe bound")
    def walk(item, depth=0):
        if depth > 64:
            raise ValueError("workflow setup response nesting unbounded")
        if isinstance(item, str):
            safe_string(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                safe_string(key); walk(child, depth+1)
        elif isinstance(item, list):
            for child in item: walk(child, depth+1)
    # Check original JSON escapes as well as every decoded key/value.
    safe_string(text); walk(value)
    return value




def indexed(rows, keys):
    if not isinstance(rows, list):
        raise ValueError("persisted catalog list missing")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or any(key not in row for key in keys):
            raise ValueError("persisted catalog identity missing")
        identity = tuple(row[key] for key in keys)
        if any(type(item) not in (int, str) for item in identity) or identity in result:
            raise ValueError("persisted catalog identity malformed/duplicated")
        result[identity] = row
    return result


def database_output(value, authored, published=None):
    keys = {"schema", "rows", "mappings", "graphs", "services", "layers", "serviceLayers", "fields", "tableNames", "features", "materialized", "connectionHashes", "connection"}
    if not isinstance(value, dict) or set(value) != keys or value["schema"] != "honua.mcp-publish-result-persisted/v1":
        raise ValueError("complete independent publication state missing")
    connection_provenance(value["connection"], authored["connectionId"], authored["ownedDatabase"])
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) != len(authored["expectedRows"]):
        raise ValueError("actual worker source rows partial/extra")
    ids, observed = set(), {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "properties", "geometry", "srid"}
                or type(row["id"]) is not int or row["id"] <= 0 or row["id"] in ids
                or type(row["srid"]) is not int or row["srid"] != 4326
                or not isinstance(row["properties"], dict) or type(row["properties"].get("gid")) is not int):
            raise ValueError("worker source feature identity/schema/SRID malformed")
        gid = row["properties"]["gid"]
        if gid in observed:
            raise ValueError("worker source attributes duplicated")
        ids.add(row["id"]); observed[gid] = {k:row[k] for k in ("properties", "geometry")}
    if not same_json(observed, {row["properties"]["gid"]: row for row in authored["expectedRows"]}):
        raise ValueError("worker persisted attributes/batch/geometry differ from authored input")
    for name, keys in (("services",("service_name",)),("layers",("layer_id",)),("serviceLayers",("service_name","layer_id")),
                       ("fields",("layer_id","field_name")),("tableNames",("schema","name")),("connectionHashes",("connectionId",))):
        indexed(value[name], keys)
    for row in value["connectionHashes"]:
        if (set(row) != {"connectionId", "sha256"} or not isinstance(row["connectionId"], str)
                or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", row["connectionId"])
                or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["sha256"])):
            raise ValueError("connection secret-bearing row census malformed")
    features = indexed(value["features"], ("id",))
    for row in features.values():
        if (set(row) != {"id", "layerId", "sha256"} or type(row["id"]) is not int or row["id"] <= 0
                or type(row["layerId"]) is not int or row["layerId"] < 0 or not isinstance(row["sha256"], str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["sha256"])):
            raise ValueError("canonical feature census malformed")
    if not isinstance(value["graphs"], list) or not value["graphs"]:
        raise ValueError("actual active canonical graphs missing")
    environments = set()
    for graph in value["graphs"]:
        if (not isinstance(graph, dict) or set(graph) != {"environment", "revision", "document"}
                or not isinstance(graph["environment"], str) or not graph["environment"] or graph["environment"] in environments
                or type(graph["revision"]) is not int or graph["revision"] <= 0 or not isinstance(graph["document"], dict)
                or graph["document"].get("environment") != graph["environment"]
                or type(graph["document"].get("revision")) is not int or graph["document"]["revision"] != graph["revision"]):
            raise ValueError("canonical graph environment/revision is missing or ambiguous")
        environments.add(graph["environment"])
        for field in ("services", "resources", "storageBindings", "publications", "connections"):
            graph_members(graph["document"].get(field))
        timestamp(graph["document"].get("generatedAt"))
    mappings = value["mappings"]
    if not isinstance(mappings, list) or any(not isinstance(m, dict) or set(m) != {"environment", "revision", "publication"} for m in mappings):
        raise ValueError("publication mappings malformed")
    if published is None:
        if (mappings or value["materialized"] or any(row["service_name"] == authored["serviceName"] for row in value["services"])
                or any(row.get("table_schema") == "public" and row.get("table_name") == authored["table"] for row in value["layers"])):
            raise ValueError("worker setup unexpectedly published/promoted the result")
        return
    if len(mappings) != 1 or not any(graph["environment"] == mappings[0]["environment"] and graph["revision"] == mappings[0]["revision"] for graph in value["graphs"]):
        raise ValueError("actual publication lacks one committed environment/mapping/revision")
    mapping = mappings[0]
    shared = {"schema": "honua.mcp-ingest-persisted/v2", "tuple": table_binding(authored), "rows": rows,
              "publication": mapping["publication"], "revision": mapping["revision"], "connection": value["connection"]}
    published_snapshot(shared, published, authored)
    materialized = value["materialized"]
    if not isinstance(materialized, list) or len(materialized) != len(rows):
        raise ValueError("publication canonical snapshot copy is partial/extra")
    by_source = {row["id"]: row for row in rows}; seen = set()
    for row in materialized:
        if (not isinstance(row, dict) or set(row) != {"id", "layerId", "attributes", "geometry", "srid"}
                or type(row["id"]) is not int or (row["id"],) not in features or row["id"] in seen
                or type(row["layerId"]) is not int or row["layerId"] != int(published["layerId"])
                or features[(row["id"],)]["layerId"] != row["layerId"] or type(row["srid"]) is not int or row["srid"] != 4326
                or not isinstance(row["attributes"], dict) or set(row["attributes"]) != {"id", "attributes"}
                or type(row["attributes"]["id"]) is not int or row["attributes"]["id"] not in by_source):
            raise ValueError("materialized copy conflates source PK/canonical object ID/layer or loses attributes")
        source = by_source[row["attributes"]["id"]]
        if (not same_json(row["attributes"]["attributes"], source["properties"])
                or not same_json(row["geometry"], source["geometry"])):
            raise ValueError("materialized full attributes/geometry differ from independently authored source")
        seen.add(row["id"])
    if len({row["attributes"]["id"] for row in materialized}) != len(rows):
        raise ValueError("materialized copy duplicated an authored source identity")


def graph_members(rows):
    if not isinstance(rows, list):
        raise ValueError("canonical graph member collection missing")
    result = {}
    for row in rows:
        identifier = row.get("metadata", {}).get("id") if isinstance(row, dict) and isinstance(row.get("metadata"), dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in result:
            raise ValueError("canonical graph member identity missing/duplicated")
        result[identifier] = row
    return result


def preserve_rows(before, after, keys, added):
    prior, current = indexed(before, keys), indexed(after, keys)
    if set(current) != set(prior) | set(added) or set(prior) & set(added) or any(not same_json(v, current[k]) for k,v in prior.items()):
        raise ValueError("publication deleted/changed prior catalog rows or added unrelated rows")
    return [current[k] for k in added]


def committed_database(initial, current, authored, published):
    """Preserve the entire old catalog, not merely a self-consistent new graph."""
    database_output(initial, authored)
    database_output(current, authored, published)
    for field in ("rows", "tableNames", "connectionHashes", "connection"):
        if not same_json(initial[field], current[field]):
            raise ValueError("publication changed source rows, tables or connection provenance")
    physical = int(published["layerId"])
    layer, = preserve_rows(initial["layers"], current["layers"], ("layer_id",), [(physical,)])
    if any(not same_json(layer.get(k), v) for k,v in {
        "layer_name":"authored-result", "table_schema":"public", "table_name":authored["table"],
        "primary_key_column":"id", "geometry_column":"geom", "storage_srid":4326, "srid":4326,
        "enabled":True, "default_visibility":True}.items()):
        raise ValueError("physical catalog layer is not the exact source-backed authored table")
    service, = preserve_rows(initial["services"], current["services"], ("service_name",), [(authored["serviceName"],)])
    if service.get("service_name") != authored["serviceName"]:
        raise ValueError("new service catalog selector changed")
    relation, = preserve_rows(initial["serviceLayers"], current["serviceLayers"], ("service_name","layer_id"), [(authored["serviceName"],physical)])
    if type(relation.get("layer_order")) is not int or relation["layer_order"] != 0:
        raise ValueError("new service has an unexpected physical catalog association")
    # Discovery selects nongeometry id/attributes columns; BuildLayerFields
    # preserves those and appends the separate geometry field last.
    added_fields = [(physical,k) for k in ("id","attributes","geom")]
    fields = preserve_rows(initial["fields"], current["fields"], ("layer_id","field_name"), added_fields)
    for row, name, kind, ordinal in zip(fields, ("id","attributes","geom"), ("BigInteger","Json","Geometry"), (1,2,3)):
        if row.get("field_name") != name or row.get("field_type") != kind or type(row.get("field_order")) is not int or row["field_order"] != ordinal:
            raise ValueError("new published source fields lost their actual type/order")
    added_features = [(row["id"],) for row in current["materialized"]]
    preserve_rows(initial["features"], current["features"], ("id",), added_features)
    before = indexed(initial["graphs"], ("environment",)); after = indexed(current["graphs"], ("environment",))
    if set(before) != set(after):
        raise ValueError("publication changed the set of active canonical environments")
    mapping = current["mappings"][0]
    for key, old in before.items():
        new = after[key]
        if old["environment"] != mapping["environment"]:
            if not same_json(old, new):
                raise ValueError("publication changed an unrelated canonical environment")
            continue
        if (new["revision"] <= old["revision"] or published["metadataRevision"] <= old["revision"]
                or new["revision"] < published["metadataRevision"]):
            raise ValueError("first actual publication did not advance the committed metadata revision")
        previous, document = old["document"], new["document"]
        allowed = {"revision","generatedAt","services","resources","storageBindings","publications","connections"}
        if set(previous) != set(document) or any(not same_json(v, document[k]) for k,v in previous.items() if k not in allowed):
            raise ValueError("publication changed unrelated graph root policy/schema/catalog/runtime data")
        if datetime.fromisoformat(document["generatedAt"].replace("Z","+00:00")) < datetime.fromisoformat(previous["generatedAt"].replace("Z","+00:00")):
            raise ValueError("publication graph generation time regressed")
        collections = {}
        for family, count in (("services",1),("resources",1),("storageBindings",1),("publications",2)):
            prior, now = graph_members(previous[family]), graph_members(document[family])
            additions = set(now)-set(prior)
            if len(additions) != count or not set(prior) <= set(now) or any(not same_json(v,now[k]) for k,v in prior.items()):
                raise ValueError("publication removed/changed prior graph members or added unrelated members")
            collections[family] = {k:now[k] for k in additions}
        svc, = collections["services"].values(); res, = collections["resources"].values(); binding, = collections["storageBindings"].values()
        service_id, resource_id, binding_id = (item["metadata"]["id"] for item in (svc,res,binding))
        publications = collections["publications"]
        types = {row.get("publicationType"):row for row in publications.values()}
        if set(types) != {"geoservices-rest-feature-layer","stac-collection"}:
            raise ValueError("new graph has unrelated/duplicate publication protocols")
        if (service_id != mapping["publication"]["serviceId"] or svc["metadata"].get("name") != authored["serviceName"]
                or svc.get("route") != "/rest/services/"+authored["serviceName"]+"/FeatureServer"
                or set(svc.get("publicationIds",[])) != set(publications) or len(svc.get("publicationIds",[])) != 2
                or res.get("type") != "feature-dataset" or res.get("primaryStorageBindingId") != binding_id
                or res.get("storageBindingIds") != [binding_id]
                or binding.get("resourceId") != resource_id or binding.get("connectionId") != authored["connectionId"]
                or binding.get("storageLayerId") != physical or binding.get("locator") != "public."+authored["table"]):
            raise ValueError("new canonical service/resource/binding relationship differs from actual returned source")
        for kind, row in types.items():
            if (row.get("serviceId") != service_id or row.get("resourceId") != resource_id or row.get("storageBindingId") != binding_id
                    or row.get("isPrimary") is not (kind == "geoservices-rest-feature-layer")
                    or not isinstance(row.get("identifier"),dict) or row["identifier"].get("value") != str(mapping["publication"]["layerId"])
                    or row["identifier"].get("isNumeric") is not True):
                raise ValueError("new publication conflates physical storage and public route index")
        prior, now = graph_members(previous["connections"]), graph_members(document["connections"])
        own = authored["connectionId"]
        if set(now) != set(prior)|{own} or any(not same_json(v,now[k]) for k,v in prior.items() if k != own):
            raise ValueError("publication changed an unrelated canonical connection")
        if own in prior:
            old_connection, new_connection = prior[own], now[own]
            # Require preservation of the complete prior dependency outside its
            # observed status. The current publisher explicitly preserves only
            # Metadata while replacing the connection object; losing nondefault
            # Options/SecretRef would be a real preservation failure here.
            if ({k:v for k,v in old_connection.items() if k != "status"} != {k:v for k,v in new_connection.items() if k != "status"}):
                raise ValueError("publication changed the registered connection's graph identity/options")
        status = now[own].get("status")
        if (now[own].get("type") != "database" or now[own].get("provider") != "postgis"
                or not isinstance(status,dict) or status.get("lifecycle") != "active" or status.get("state") != "ready"):
            raise ValueError("published connection lacks actual readiness observation")
        timestamp(status.get("observedAt"))


def shared_snapshot(database, authored):
    mapping = database["mappings"][0]
    return {"schema":"honua.mcp-ingest-persisted/v2", "tuple":table_binding(authored), "rows":database["rows"],
            "publication":mapping["publication"], "revision":mapping["revision"], "connection":database["connection"]}


def query_output(result, published, database):
    if (not isinstance(result,dict) or result.get("isError",False) is not False or not isinstance(result.get("structuredContent"),dict)
            or not isinstance(result.get("content"),list) or len(result["content"]) != 1):
        raise ValueError("fresh query lacks typed content/summary")
    summary = result["content"][0]; value = result["structuredContent"]
    if (not isinstance(summary,dict) or set(summary) != {"type","text"} or summary["type"] != "text"
            or not isinstance(summary["text"],str) or not 0 < len(summary["text"]) <= 16384):
        raise ValueError("fresh query summary media malformed")
    if summary["text"].lstrip().startswith(("{","[")) and not same_json(strict_json(summary["text"]),value):
        raise ValueError("query JSON representations conflict")
    if "geojson" in value and "geoJson" in value and not same_json(value["geojson"],value["geoJson"]):
        raise ValueError("query GeoJSON aliases conflict")
    collection = value.get("geojson",value.get("geoJson")); mapping = database["mappings"][0]["publication"]
    if (value.get("serviceId") != mapping["serviceId"] or type(value.get("layerId")) is not int or value["layerId"] != mapping["layerId"]
            or type(value.get("returnedCount")) is not int or value["returnedCount"] != len(database["rows"])
            or value.get("exceededTransferLimit") is not False or not isinstance(collection,dict)
            or collection.get("type") != "FeatureCollection" or not isinstance(collection.get("features"),list)):
        raise ValueError("query addresses a different canonical/public layer or is partial")
    expected = {row["id"]:row for row in database["rows"]}
    def features(rows, geojson):
        if not isinstance(rows,list) or len(rows) != len(expected):
            raise ValueError("fresh query feature census incomplete")
        seen = set()
        for feature in rows:
            if not isinstance(feature,dict) or type(feature.get("id")) is not int or feature["id"] not in expected or feature["id"] in seen:
                raise ValueError("query conflates actual source PK/canonical snapshot copy IDs")
            seen.add(feature["id"]); original = expected[feature["id"]]
            if geojson and feature.get("type") != "Feature":
                raise ValueError("query GeoJSON type missing")
            attrs = feature.get("properties" if geojson else "attributes")
            if not isinstance(attrs,dict) or set(attrs) != {"id","attributes"} or type(attrs["id"]) is not int or attrs["id"] != feature["id"]:
                raise ValueError("query lost published source schema/primary-key attributes")
            nested = strict_json(attrs["attributes"]) if isinstance(attrs["attributes"],str) else attrs["attributes"]
            if not same_json(nested,original["properties"]) or not same_json(feature.get("geometry"),original["geometry"]):
                raise ValueError("query changed authored attributes/batch or exact geometry")
    features(collection["features"],True); features(value.get("features"),False)
    return value


def worker_resource(result, identity, setup, *, results=False):
    from mcp_execute_plan_validation import resource, job_output
    uri = "honua://jobs/"+identity["jobId"]
    job = setup["workflow"]["job"]
    if not results:
        return job_output(result,{"jobId":identity["jobId"],"createdAt":job["createdAt"],"resourceUri":uri},terminal=True)
    value = resource(result,uri+"/results")
    artifact = {**identity["artifact"],"kind":"FeatureLayer"}
    if (value.get("jobId") != identity["jobId"] or value.get("resultPackageId") != identity["resultPackageId"]
            or value.get("status") != "Completed" or value.get("totalArtifactCount") != 1 or type(value.get("totalArtifactCount")) is not int
            or value.get("artifactsTruncated") is not False or not same_json(value.get("artifacts"),[artifact])
            or value.get("errors") != []):
        raise ValueError("fresh worker result resource differs from genuine committed result package")
    return value


def negative_arguments(authored, identity, case):
    args = publication_arguments(authored,identity["jobId"],identity["artifactId"])
    if case == "unknown-artifact":
        return {**args,"artifactId":identity["jobId"]+":missing-artifact-"+authored["nonce"]}
    if case == "unknown-job":
        return {**args,"sourceId":"gp-"+authored["nonce"]*2}
    if case == "anonymous":
        return args
    raise ValueError("unknown authored publication refusal case")


def judge(fixture_bytes, bootstrap, observed):
    from mcp_render_validation import negative_output
    if not isinstance(observed,dict):
        raise ValueError("publication observations missing")
    authored = scenario(fixture_bytes,bootstrap,observed.get("nonce"),observed.get("canonicalClient"))
    fields = {"schema","nonce","canonicalClient","clientVersion","candidateSourceSha","candidateImage","setup","calls","snapshots",
              "runtimeBefore","runtimePrePublish","runtimeRestarted","runtimeAfter"}
    if set(observed) != fields or any(not same_json(observed[k],authored[k]) for k in ("schema","candidateSourceSha","candidateImage")):
        raise ValueError("publication evidence belongs to another authored candidate/scenario")
    identity = workflow_output(observed["setup"],authored)
    before, prepublish, restarted, after = (observed[k] for k in ("runtimeBefore","runtimePrePublish","runtimeRestarted","runtimeAfter"))
    for runtime in (before,prepublish,restarted,after):
        if not isinstance(runtime,dict) or not same_json({**runtime,"startedAt":authored["runtimeBinding"]["startedAt"]},authored["runtimeBinding"]):
            raise ValueError("publication candidate identity changed")
    if (not same_json(before,observed["setup"]["runtimeBefore"]) or not same_json(before,observed["setup"]["runtimeAfter"])
            or not same_json(before,prepublish) or not same_json(restarted,after)
            or datetime.fromisoformat(restarted["startedAt"].replace("Z","+00:00")) <= datetime.fromisoformat(before["startedAt"].replace("Z","+00:00"))):
        raise ValueError("setup/client runtime continuity or explicit same-container restart missing")
    snapshots = observed["snapshots"]
    names = ["initial","worker-read","unknown-artifact","unknown-job","anonymous","published","readback","anonymous-resource","restarted","restarted-readback"]
    if not isinstance(snapshots,dict) or set(snapshots) != set(names):
        raise ValueError("complete publication/refusal/restart persisted checkpoints missing")
    for snapshot in snapshots.values():
        if not isinstance(snapshot,dict) or set(snapshot) != {"schema","database","census"} or snapshot["schema"] != "honua.mcp-publish-result-state/v1":
            raise ValueError("independent publication state missing")
        census_output(snapshot["census"])
    if not same_json(snapshots["initial"]["census"],identity["census"]):
        raise ValueError("worker records changed before canonical-client publication journey")
    missing_job = negative_arguments(authored,identity,"unknown-job")["sourceId"]
    if any(row["id"] == missing_job for family in ("jobs","results") for row in snapshots["initial"]["census"][family]):
        raise ValueError("authored unknown-job probe already exists in the actual initial census")
    calls = observed["calls"]
    if not isinstance(calls,list) or len(calls) != 12:
        raise ValueError("publication actual-client sequence incomplete/unrelated")
    sessions, fingerprints = set(), set()
    def call(index, identifier, method, params, authenticated=True):
        entry = calls[index]
        if (not isinstance(entry,dict) or set(entry) != {"id","method","params","authenticated","result","wire","client"}
                or entry["id"] != identifier or entry["method"] != method or entry["authenticated"] is not authenticated or not same_json(entry["params"],params)):
            raise ValueError("publication request differs from independent authored request")
        frame, session = invocation_wire(entry["wire"],method,params,authenticated,observed["canonicalClient"])
        if session in sessions or not same_json(entry["result"],frame if "error" in frame else frame["result"]):
            raise ValueError("publication reused a session or changed wire response")
        sessions.add(session); fingerprints.add(entry["wire"]["expectedCredentialFingerprint"])
        client_observation(entry["client"],frame,entry["wire"],observed["canonicalClient"],observed["clientVersion"])
        return entry["result"]
    def tool(index,identifier,args,auth=True,name="honua_publish_result"):
        return call(index,identifier,"tools/call",{"name":name,"arguments":args},auth)
    uri = "honua://jobs/"+identity["jobId"]
    original_job = worker_resource(call(0,"worker-job","resources/read",{"uri":uri}),identity,observed["setup"])
    original_result = worker_resource(call(1,"worker-results","resources/read",{"uri":uri+"/results"}),identity,observed["setup"],results=True)
    for index,identifier in ((2,"unknown-artifact"),(3,"unknown-job")):
        negative_output(tool(index,identifier,negative_arguments(authored,identity,identifier)),"not_found")
    anonymous_tool_refusal(tool(4,"anonymous",negative_arguments(authored,identity,"anonymous"),False))
    published = publication_output(tool(5,"publish",publication_arguments(authored,identity["jobId"],identity["artifactId"])),authored,identity["jobId"],identity["artifactId"])
    database = snapshots["published"]["database"]
    committed_database(snapshots["initial"]["database"],database,authored,published)
    snapshot = shared_snapshot(database,authored)
    fresh = resource_output(call(6,"fresh-resource","resources/read",{"uri":published["serviceUri"]}),published,snapshot)
    query = query_arguments(published,fresh)
    first_query = query_output(tool(7,"fresh-query",query,name="honua_query_features"),published,database)
    resource_refusal(call(8,"anonymous-resource","resources/read",{"uri":published["serviceUri"]},False),-32000,"unauthenticated")
    repeated = resource_output(call(9,"restarted-resource","resources/read",{"uri":published["serviceUri"]}),published,snapshot)
    second_query = query_output(tool(10,"restarted-query",query,name="honua_query_features"),published,database)
    worker_after = worker_resource(call(11,"restarted-worker-results","resources/read",{"uri":uri+"/results"}),identity,observed["setup"],results=True)
    if (fingerprints != {setup_output(observed["setup"],authored)["credentialFingerprint"]}
            or not same_json(fresh,repeated) or not same_json(first_query,second_query) or not same_json(original_result,worker_after)):
        raise ValueError("publication authority, fresh readback or durable result changed after restart")
    for name in names[:5]:
        database_output(snapshots[name]["database"],authored)
        if not same_json(snapshots[name],snapshots["initial"]):
            raise ValueError("read-only/refused publication changed persisted graph/data/jobs/proposals/operations")
    # service.publish may create its real operation-instance receipt. No jobs,
    # results, definitions, workflow run or proposal may appear/disappear/change.
    initial_census = snapshots["initial"]["census"]; completed = snapshots["published"]["census"]
    for family in initial_census:
        if family != "operations" and not same_json(initial_census[family],completed[family]):
            raise ValueError("publication created unrelated workflow/job/result/proposal effects")
    prior = {row["id"]:row for row in initial_census["operations"]}; current = {row["id"]:row for row in completed["operations"]}
    added = set(current)-set(prior)
    if (not set(prior) <= set(current) or any(not same_json(v,current[k]) for k,v in prior.items())
            or added not in (set(),{published["operationInstanceId"]})):
        raise ValueError("publication changed prior operations or created unrelated operation instances")
    for name in names[5:]:
        database_output(snapshots[name]["database"],authored,published)
        if not same_json(snapshots[name],snapshots["published"]):
            raise ValueError("fresh/anonymous/restarted readback changed committed metadata revision/data/durable records")
    return {k:authored[k] for k in ("candidateSourceSha","candidateImage","fixtureSha256","bootstrapSha256")} | {
        "schema":SCHEMA,"operation":OPERATION,"canonicalClient":observed["canonicalClient"],"clientVersion":observed["clientVersion"],
        "positive":True,"negative":True,"mediaSchema":True,"persistedReadback":True,"restartReadback":True,
        "anonymousInvocationRefusal":True,"negativeNonmutation":True,"genuineWorkerResult":True,"geometricResult":True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture",type=Path,required=True); parser.add_argument("--bootstrap",type=Path,required=True)
    parser.add_argument("--nonce"); parser.add_argument("--canonical-client"); parser.add_argument("--observations",type=Path)
    parser.add_argument("--publication-arguments",action="store_true"); parser.add_argument("--query-arguments",action="store_true")
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes())
        if args.observations:
            observed = strict_json(args.observations.read_bytes())
            if args.publication_arguments or args.query_arguments:
                authored = scenario(fixture,bootstrap,observed["nonce"],observed["canonicalClient"])
                identity = workflow_output(observed["setup"],authored)
                value = publication_arguments(authored,identity["jobId"],identity["artifactId"])
                if args.query_arguments:
                    published = publication_output(observed["calls"][5]["result"],authored,identity["jobId"],identity["artifactId"])
                    database = observed["snapshots"]["published"]["database"]
                    committed_database(observed["snapshots"]["initial"]["database"],database,authored,published)
                    fresh = resource_output(observed["calls"][6]["result"],published,shared_snapshot(database,authored))
                    value = query_arguments(published,fresh)
            else:
                value = judge(fixture,bootstrap,observed)
        else:
            value = scenario(fixture,bootstrap,args.nonce,args.canonical_client)
        print(json.dumps(value,allow_nan=False)); return 0
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,OSError):
        print("authored genuine result publication evidence refused",file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
