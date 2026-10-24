"""Independent persisted deployment/workspace semantics; no I/O or actuation."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_map_package_validation import strict_json, same_json


def instant(value):
    if not isinstance(value, str):
        raise ValueError("lifecycle timestamp missing")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("lifecycle timestamp lacks timezone")
    return stamp


def ticks(value):
    stamp = instant(value)
    match = re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.([0-9]{1,7}))?(?:Z|[+-][0-9]{2}:[0-9]{2})", value)
    if match is None:
        raise ValueError("lifecycle timestamp precision differs")
    base = stamp.replace(microsecond=0).astimezone(timezone.utc) - datetime(1, 1, 1, tzinfo=timezone.utc)
    return (base.days * 86400 + base.seconds) * 10_000_000 + int((match.group(1) or "").ljust(7, "0"))


def identity(value):
    if (not isinstance(value, dict) or set(value) != {"clusterId", "database", "user"}
            or not isinstance(value["clusterId"], str) or not re.fullmatch(r"[0-9]+", value["clusterId"])
            or value["database"] != "honua_test" or value["user"] != "honua"):
        raise ValueError("independent owned database identity differs")


def observation(value, target, deployment_id, workspace_id):
    if (not isinstance(value, dict) or set(value) != {"schema", "startedAt", "completedAt", "targetId", "deploymentId", "workspaceId", "stores", "succeededWorkflowHistory"}
            or value["schema"] != "honua.mcp-deployment-workspace-state/v1"
            or value["targetId"] != target or value["deploymentId"] != deployment_id or value["workspaceId"] != workspace_id
            or instant(value["completedAt"]) < instant(value["startedAt"])):
        raise ValueError("independent lifecycle observation differs")
    stores = value["stores"]
    if not isinstance(stores, dict) or set(stores) != {"databaseIdentity", "deployment", "studioItem", "studioVersion", "studioPublication", "workspace", "artifacts"}:
        raise ValueError("independent lifecycle stores incomplete")
    identity(stores["databaseIdentity"])
    return stores


def rollback_selection(history, target):
    if (not isinstance(history, dict) or set(history) != {"terminalCount", "visibleCount", "isTruncated", "operations"}
            or type(history["terminalCount"]) is not int or not 0 <= history["terminalCount"] <= 1000
            or history["visibleCount"] != history["terminalCount"] or history["isTruncated"] is not False
            or not isinstance(history["operations"], list) or len(history["operations"]) < 2):
        raise ValueError("real complete prior succeeded history missing")
    operations, seen = [], set()
    for item in history["operations"]:
        if (not isinstance(item, dict) or set(item) != {"operationId", "recordSha256", "record", "deploy"}
                or not isinstance(item["recordSha256"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", item["recordSha256"])):
            raise ValueError("durable workflow identity missing")
        record, deploy = item["record"], item["deploy"]
        if (not isinstance(record, dict) or record.get("operationId") != item["operationId"]
                or item["operationId"] in seen or type(record.get("kind")) is not int or record["kind"] != 0
                or type(record.get("status")) is not int or record["status"] != 4
                or type(record.get("version")) is not int or record["version"] < 1
                or not isinstance(record.get("providerOperationId"), str) or not record["providerOperationId"].strip()
                or not isinstance(deploy, dict) or set(deploy) != {"targetId", "desiredRevision", "currentRevision"}
                or deploy["targetId"] != target or not isinstance(deploy["desiredRevision"], str) or not deploy["desiredRevision"].strip()):
            raise ValueError("actual succeeded Deploy record missing")
        if instant(record["createdAt"]) > min(instant(record["updatedAt"]), instant(record["completedAt"])):
            raise ValueError("actual workflow lifecycle timestamps differ")
        seen.add(item["operationId"]); operations.append(item)
    # This is QueryAsync's order, not the target converge index's completion order.
    operations.sort(key=lambda item: (instant(item["record"]["createdAt"]), item["operationId"]), reverse=True)
    current, prior = operations[:2]
    if current["deploy"]["desiredRevision"] == prior["deploy"]["desiredRevision"]:
        raise ValueError("real prior deploy revision does not differ")
    return {"targetId": target, "currentRevision": current["deploy"]["desiredRevision"],
            "desiredRevision": prior["deploy"]["desiredRevision"],
            "currentOperationId": current["operationId"], "priorOperationId": prior["operationId"]}


def deployment(stores, expected):
    row, item, version, publication = (stores[key] for key in ("deployment", "studioItem", "studioVersion", "studioPublication"))
    if not all(isinstance(value, dict) for value in (row, item, version, publication)):
        raise ValueError("real publication-created deployment missing")
    document = row.get("document")
    if (not isinstance(document, dict) or row.get("deploymentId") != expected["deploymentId"]
            or row.get("sourceKind") != "StudioPublishedVersion" or row.get("status") != "Active"
            or document.get("deploymentId") != expected["deploymentId"] or document.get("status") != "Active"
            or document.get("publicationState") != "Published" or document.get("rolloutState") != "Promoted"
            or document.get("runtime", {}).get("health") != "Unknown"):
        raise ValueError("real Studio deployment lifecycle differs")
    source = document.get("source", {})
    target = document.get("target", {})
    if (not isinstance(target, dict) or row.get("targetId") != "studio-route:" + expected["route"]
            or target.get("targetId") != row["targetId"] or target.get("kind") != "StudioPublishedRoute"
            or target.get("hostingMode") != "ManagedService" or target.get("routePrefix") != "/api/v1/studio/published" + expected["route"]):
        raise ValueError("actual Studio serving route target differs")
    for name in ("studioItemId", "studioVersionId", "studioContentHash"):
        if source.get(name) != expected[name]:
            raise ValueError("deployment source differs from actual saved publication")
    item_id, version_id, content_hash = (expected[key] for key in ("studioItemId", "studioVersionId", "studioContentHash"))
    if (row.get("sourceId") != version_id or source.get("sourceId") != version_id
            or source.get("kind") != "StudioPublishedVersion" or source.get("studioFamily") != "map"
            or item.get("itemId") != item_id or item.get("currentVersionId") != version_id or item.get("publishedVersionId") != version_id
            or version.get("itemId") != item_id or version.get("versionId") != version_id or version.get("contentHash") != content_hash
            or item.get("ownerId") != source.get("studioOwnerId") or version.get("ownerId") != item.get("ownerId")
            or version.get("tenantId") != item.get("tenantId")
            or publication.get("itemId") != item_id or publication.get("versionId") != version_id
            or publication.get("requestId") != source.get("studioPublicationRequestId")
            or publication.get("deploymentId") != expected["deploymentId"] or publication.get("status") != "accepted"
            or publication.get("governsStudioRoutes") is not True
            or publication.get("intent") != {"route": expected["route"], "visibility": "public"}):
        raise ValueError("real Studio version/publication/pointer join differs")
    encoded = version.get("contentHashInputBase64")
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4 * 1024 * 1024:
        raise ValueError("immutable version hash input missing")
    raw = base64.b64decode("".join(encoded.split()), validate=True)
    if (not isinstance(content_hash, str) or hashlib.sha256(raw).hexdigest() != content_hash.removeprefix("sha256:")
            or not same_json(strict_json(raw), version.get("envelope"))
            or not isinstance(version.get("envelope"), dict)
            or not same_json(version["envelope"].get("body"), expected["mapBody"])):
        raise ValueError("immutable version bytes differ from published hash")
    transitions = document.get("transitions")
    if (not isinstance(transitions, list) or [(step.get("from"), step.get("to")) for step in transitions] != [
            ("Draft", "Provisioning"), ("Provisioning", "RollingOut"), ("RollingOut", "Active")]):
        raise ValueError("publication-created transitions missing")
    times = [ticks(document["createdAt"])] + [ticks(step["at"]) for step in transitions]
    if any(right <= left for left, right in zip(times, times[1:])) or ticks(document["updatedAt"]) != times[-1] or not times[-2] <= ticks(document["activatedAt"]) <= times[-1]:
        raise ValueError("publication lifecycle times differ")
    return document


def workspace(stores, expected):
    row, artifacts = stores["workspace"], stores["artifacts"]
    if (not isinstance(row, dict) or row.get("workspace_id") != expected["workspaceId"] or row.get("label") != expected["label"]
            or row.get("owner_id") != expected["ownerId"] or row.get("scope_id") != expected["scopeId"]
            or type(row.get("state")) is not int or row["state"] != 0
            or not isinstance(artifacts, list) or len(artifacts) != 1):
        raise ValueError("actual owned active GP workspace missing")
    artifact = artifacts[0]
    if (not isinstance(artifact, dict) or artifact.get("workspace_id") != expected["workspaceId"]
            or type(artifact.get("state")) is not int or artifact["state"] != 1 or not isinstance(artifact.get("artifact_id"), str)
            or not isinstance(artifact.get("metadata"), dict) or not isinstance(artifact.get("uri"), str)
            or type(artifact.get("size_bytes")) is not int or artifact["size_bytes"] <= 0):
        raise ValueError("actual workspace result artifact missing")
    reference = artifact["uri"]
    prefix = "data:application/json;base64,"
    if not reference.startswith(prefix) or len(reference) > 1024 * 1024:
        raise ValueError("workspace scalar is not the actual bounded inline result")
    payload = strict_json(base64.b64decode(reference[len(prefix):], validate=True))
    wanted = {"type": "MeasureResult", "processId": "geometry.area", "measure": "area", "value": 12,
              "unit": "input-crs-units-squared", "inputSrid": 3857, "inputGeometryType": "Polygon"}
    publication_identity = f"{len(expected['workspaceId'])}:{expected['workspaceId']}{len(expected['jobId'])}:{expected['jobId']}:0"
    artifact_id = "output-" + hashlib.sha256(publication_identity.encode()).hexdigest()
    if (not same_json(payload, wanted) or artifact["artifact_id"] != artifact_id
            or artifact.get("label") != "outputScalar" or artifact.get("label_key") != "OUTPUTSCALAR"
            or type(artifact.get("kind")) is not int or artifact["kind"] != 0
            or artifact["size_bytes"] != len(reference.encode()) or artifact["metadata"] != {}):
        raise ValueError("actual GP area output/slot publication differs")
    instant(row["created_at"]); instant(artifact["created_at"])
    return row, artifact


def proposal(value, selection, idempotency_key):
    if (not isinstance(value, dict) or set(value) != {"schema", "proposalId", "recordSha256", "planBytesSha256", "record", "audit", "payload"}
            or value["schema"] != "honua.mcp-rollback-proposal-state/v1"
            or not isinstance(value["recordSha256"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["recordSha256"])
            or not isinstance(value["planBytesSha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["planBytesSha256"])):
        raise ValueError("actual durable forward proposal missing")
    record, audit, payload = value["record"], value["audit"], value["payload"]
    if (not isinstance(record, dict) or not isinstance(audit, dict) or not isinstance(payload, dict)
            or type(record.get("kind")) is not int or record["kind"] != 1
            or type(record.get("status")) is not int or record["status"] != 1
            or type(record.get("version")) is not int or record["version"] < 1
            or record.get("operationId") != "control-plane.deploy" or record.get("actionDiscriminator") != "deploy.rollback"
            or record.get("sealedPlanHash") != value["planBytesSha256"]
            or record.get("proposalId") != value["proposalId"] or record.get("executionOperationId") is not None
            or record.get("resolvedBy") is not None or record.get("resolvedAt") is not None
            or audit.get("idempotencyKey") != idempotency_key
            or not isinstance(record.get("requestedBy"), str) or not record["requestedBy"]
            or audit.get("requestedBy") != record["requestedBy"]
            or any(not isinstance(audit.get(key), str) or not audit[key] for key in ("operationInstanceId", "auditId", "correlationId"))):
        raise ValueError("gateway did not durably await separate approval")
    expected = {"targetId": selection["targetId"], "desiredRevision": selection["desiredRevision"],
                "currentRevision": selection["currentRevision"], "priority": 1}
    if "parameterOverrides" in payload:
        expected["parameterOverrides"] = None
    if not same_json(payload, expected):
        raise ValueError("actual forward proposal payload differs from real prior history")
    material = "Deploy:control-plane.deploy:" + idempotency_key
    tenant = record.get("tenantId")
    if tenant is not None:
        if not isinstance(tenant, str) or not tenant or not tenant.isascii():
            raise ValueError("owned fixture tenant identity differs")
        material = f"{len(tenant)}:{tenant}:" + material
    identifier = "proposal-" + hashlib.sha256(material.encode()).hexdigest()[:32]
    if value["proposalId"] != identifier:
        raise ValueError("durable proposal idempotency identity differs")
    if instant(record["updatedAt"]) < instant(record["createdAt"]):
        raise ValueError("proposal lifecycle timestamps differ")
    return value["proposalId"]
