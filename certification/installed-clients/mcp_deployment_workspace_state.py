"""Read-only observations of actual owned deployment/workspace stores.

No succeeded workflow, deployment, publication or workspace is manufactured here.
Missing real history or a missing protected lifecycle writer remains a refusal in
the separate semantic oracle. Private credentials never enter retained output.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_execute_plan_state import redis_command
from mcp_ingest_publish_state import database_query
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate
from mcp_style_state import restart


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("owned lifecycle identifier malformed")
    return value


def workflow_history(target, *, execute=redis_command):
    identifier(target)
    # Match QueryAsync's actual terminal materialization window, not the
    # separate per-target converge index: rollback selection pages QueryAsync,
    # ordered by CreatedAt then OperationId, not completion time.
    script = """local key='controlplane:workflow:terminal'
local kind=redis.call('TYPE',key).ok
if kind=='none' then return {'0',{}} end
if kind~='zset' then return redis.error_reply('workflow history type differs') end
local count=redis.call('ZCARD',key)
local out={}
local total=0
for _,id in ipairs(redis.call('ZREVRANGE',key,0,999)) do
 local raw=redis.call('GET','controlplane:workflow:'..id)
 if not raw then return redis.error_reply('history references absent durable operation') end
 total=total+string.len(raw)
 if total>4194304 then return redis.error_reply('workflow history bytes exceed bound') end
 table.insert(out,{id,raw})
end
return {tostring(count),out}"""
    reply = execute(["EVAL", script, "0"])
    if not isinstance(reply, list) or len(reply) != 2 or not isinstance(reply[0], bytes):
        raise ValueError("actual workflow history framing malformed")
    count, rows = int(reply[0]), reply[1]
    if not 0 <= count <= 5000 or not isinstance(rows, list) or len(rows) != min(count, 1000):
        raise ValueError("actual workflow materialization differs")
    retained, seen = [], set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 2 or not all(isinstance(item, bytes) for item in row):
            raise ValueError("actual workflow history framing differs")
        operation_id = identifier(row[0].decode())
        body = strict_json(row[1])
        if operation_id in seen or not isinstance(body, dict) or body.get("operationId") != operation_id:
            raise ValueError("actual workflow history identity ambiguous")
        seen.add(operation_id)
        # Enums are persisted numerically by the production ControlPlaneJsonContext.
        # Keep only the exact target's real succeeded Deploy records, without
        # retaining arbitrary backend parameters or unrelated workflow payloads.
        if body.get("kind") != 0 or body.get("status") != 4 or not isinstance(body.get("deploy"), dict) or body["deploy"].get("targetId") != target:
            continue
        retained.append({"operationId": operation_id, "recordSha256": "sha256:" + hashlib.sha256(row[1]).hexdigest(),
                         "record": {key: body.get(key) for key in ("operationId", "kind", "status", "version", "createdAt", "updatedAt", "completedAt", "providerOperationId", "observedState")},
                         "deploy": {key: body["deploy"].get(key) for key in ("targetId", "desiredRevision", "currentRevision")} if isinstance(body.get("deploy"), dict) else None})
    return {"terminalCount": count, "visibleCount": len(rows), "isTruncated": count > 1000, "operations": retained}


def member_bytes(raw, member):
    # Preserve the nested serialized plan bytes. The production seal hashes the
    # same source-generated plan serialization; do not recreate JSON ordering,
    # escaping or nullable defaults in this independent observer.
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    def space(index):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index
    index = space(0)
    if text[index:index + 1] != "{":
        raise ValueError("durable proposal is not an object")
    index = space(index + 1)
    while text[index:index + 1] != "}":
        key, index = decoder.raw_decode(text, index)
        index = space(index)
        if not isinstance(key, str) or text[index:index + 1] != ":":
            raise ValueError("durable proposal member framing differs")
        start = space(index + 1)
        _, end = decoder.raw_decode(text, start)
        if key == member:
            return text[start:end].encode()
        index = space(end)
        if text[index:index + 1] != ",":
            break
        index = space(index + 1)
    raise ValueError("durable proposal member missing")


def observe_proposal(proposal_id, *, execute=redis_command):
    identifier(proposal_id)
    raw = execute(["GET", "controlplane:proposal:" + proposal_id])
    if not isinstance(raw, bytes):
        raise ValueError("actual durable gateway proposal missing")
    body = strict_json(raw)
    plan = body.get("plan") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("proposalId") != proposal_id or not isinstance(plan, dict) or not isinstance(plan.get("executionPayload"), str):
        raise ValueError("actual durable proposal has no bound forward payload")
    payload = strict_json(plan["executionPayload"])
    # This producer requests no opaque overrides. Do not export arbitrary
    # backend parameters or an unrelated proposal's private execution payload.
    if (not isinstance(payload, dict) or set(payload) - {"targetId", "desiredRevision", "currentRevision", "priority", "parameterOverrides"}
            or type(payload.get("priority")) is not int or payload["priority"] != 1
            or payload.get("parameterOverrides") not in (None, {})):
        raise ValueError("proposal differs from bounded authored forward deploy")
    plan_digest = hashlib.sha256(member_bytes(raw, "plan")).hexdigest()
    if body.get("sealedPlanHash") != plan_digest:
        raise ValueError("actual gateway plan seal differs from persisted bytes")
    return {"schema": "honua.mcp-rollback-proposal-state/v1", "planBytesSha256": plan_digest, "proposalId": proposal_id,
            "recordSha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "record": {key: body.get(key) for key in ("proposalId", "kind", "status", "version", "tenantId", "operationId", "actionDiscriminator", "requestedBy", "requestedByAgent", "sealedPlanHash", "executionOperationId", "resolvedBy", "resolvedAt", "createdAt", "updatedAt")},
            "audit": {key: body.get("audit", {}).get(key) for key in ("operationInstanceId", "auditId", "correlationId", "idempotencyKey", "requestedBy", "tenantId")},
            "payload": payload}


def observe(target, deployment_id, workspace_id, *, password, sql=database_query, redis=redis_command):
    target, deployment_id, workspace_id = map(identifier, (target, deployment_id, workspace_id))
    started = datetime.now(timezone.utc).isoformat()
    # Inputs are bounded identifiers above; still quote all SQL literals.
    literal = lambda value: "'" + value.replace("'", "''") + "'"
    query = f"""SET TRANSACTION READ ONLY;
WITH selected AS (
 SELECT * FROM honua.promotion_deployments WHERE deployment_id={literal(deployment_id)}
)
SELECT jsonb_build_object(
 'databaseIdentity',(SELECT jsonb_build_object('clusterId',system_identifier::text,'database',current_database(),'user',current_user) FROM pg_control_system()),
 'deployment',(SELECT jsonb_build_object('deploymentId',deployment_id,'sourceKind',source_kind,'sourceId',source_id,'targetId',target_id,'status',status,'document',document,'createdAt',created_at,'updatedAt',updated_at) FROM selected),
 'studioItem',(SELECT jsonb_build_object('itemId',item_id,'family',family,'packageKey',package_key,'ownerId',owner_id,'tenantId',tenant_id,'currentVersionId',current_version_id,'publishedVersionId',published_version_id) FROM honua.studio_content_items WHERE item_id=(SELECT document#>>'{{source,studioItemId}}' FROM selected)::uuid),
 'studioVersion',(SELECT jsonb_build_object('itemId',item_id,'versionId',version_id,'contentHash',content_hash,'contentHashInputBase64',encode(content_hash_input,'base64'),'envelope',envelope,'ownerId',owner_id,'tenantId',tenant_id) FROM honua.studio_content_versions WHERE version_id=(SELECT document#>>'{{source,studioVersionId}}' FROM selected)::uuid),
 'studioPublication',(SELECT jsonb_build_object('requestId',request_id,'itemId',item_id,'versionId',version_id,'intent',intent,'status',status,'deploymentId',deployment_id,'governsStudioRoutes',governs_studio_routes) FROM honua.studio_publication_requests WHERE request_id=(SELECT document#>>'{{source,studioPublicationRequestId}}' FROM selected)::uuid),
 'workspace',(SELECT to_jsonb(w) FROM honua.gp_workspaces w WHERE workspace_id={literal(workspace_id)}),
 'artifacts',COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY artifact_id) FROM (SELECT * FROM honua.gp_workspace_artifacts WHERE workspace_id={literal(workspace_id)} ORDER BY artifact_id LIMIT 2) a),'[]'::jsonb))::text;"""
    stores = strict_json(sql(query, password))
    history = workflow_history(target, execute=redis)
    return {"schema": "honua.mcp-deployment-workspace-state/v1", "startedAt": started,
            "completedAt": datetime.now(timezone.utc).isoformat(), "targetId": target,
            "deploymentId": deployment_id, "workspaceId": workspace_id,
            "stores": stores, "succeededWorkflowHistory": history}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--target-id"); parser.add_argument("--deployment-id"); parser.add_argument("--workspace-id")
    parser.add_argument("--restart", action="store_true"); parser.add_argument("--proposal-id")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        before = inspect_candidate(args.candidate_source_sha, args.candidate_image)
        if args.restart:
            value = restart({"candidateSourceSha": args.candidate_source_sha, "candidateImage": args.candidate_image}, before)
        elif args.proposal_id:
            value = observe_proposal(args.proposal_id)
            if inspect_candidate(args.candidate_source_sha, args.candidate_image) != before:
                raise ValueError("candidate changed during independent proposal read")
        else:
            value = observe(args.target_id, args.deployment_id, args.workspace_id, password=os.environ.get("HONUA_CERT_DB_PASSWORD"))
            if inspect_candidate(args.candidate_source_sha, args.candidate_image) != before:
                raise ValueError("candidate changed during independent lifecycle observation")
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.SubprocessError):
        print("owned independent lifecycle observation refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
