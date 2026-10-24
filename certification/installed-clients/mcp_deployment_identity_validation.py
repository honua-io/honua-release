"""Full original managed principal/tenant/owner join; no weaker fallback."""
from __future__ import annotations
import re
import base64
import hashlib
from mcp_managed_identity_fixture import verify_receipt
from mcp_deployment_identity_configuration import verify_configuration
from mcp_deployment_identity_role import verify_role_receipt
from mcp_deployment_workspace_store_oracle import instant
from mcp_map_package_validation import strict_json

CLIENTS={"sdk":"Official MCP TypeScript SDK","inspector":"MCP Inspector"}


def verified_cohort(state, *, runtime, nonce, service_id):
    """Return original per-client inputs only after rejudging the entire cohort.

    The caller must independently judge BOTH returned raw journeys semantically
    and bind every actual call and resource to these identities. This helper
    alone grants no operation or authority facet.
    """
    fields={"schema","nonce","serviceId","candidateImage","candidate","credentialSalt","configuration",
            "configurationObservation","roleReceipt","keys","failures","cleanupComplete","cleanupStartedAt"}
    expected_candidate={key:runtime.get(key) for key in ("containerId","imageId","candidateSourceSha")}
    if (not isinstance(state,dict) or set(state)!=fields or state["schema"]!="honua.mcp-deployment-managed-cohort/v1"
            or state["nonce"]!=nonce or state["serviceId"]!=service_id or state["candidate"]!=expected_candidate
            or state["candidateImage"]!=runtime.get("imageReference") or state["cleanupComplete"] is not True
            or state["failures"]!=[] or not isinstance(state["credentialSalt"],str)
            or not re.fullmatch(r"[a-f0-9]{32}",state["credentialSalt"])
            or not isinstance(state["keys"],dict) or set(state["keys"])!=set(CLIENTS)):
        raise ValueError("complete original two-client managed cohort missing")
    configuration=verify_configuration(state["configuration"],state["configurationObservation"],runtime)
    if configuration["primaryTenant"]!="mcp-deployment-"+nonce:
        raise ValueError("original managed cohort nonce/configuration differs")
    role=verify_role_receipt(state["roleReceipt"],state["configuration"],state["configurationObservation"],runtime)
    if role["cohort"]["id"]!=nonce:
        raise ValueError("normal role cohort belongs to another original journey")
    cleanup=instant(state["cleanupStartedAt"])
    receipt=state["roleReceipt"];events=receipt["events"]
    phases=[instant(events[0]["observedAt"]),instant(events[1]["observedAt"]),
        instant(receipt["createdStore"]["startedAt"]),instant(events[2]["observedAt"]),
        instant(receipt["configuredStore"]["startedAt"]),instant(events[3]["observedAt"]),
        instant(events[4]["observedAt"])]
    if phases!=sorted(phases):
        raise ValueError("normal role API and independent grant observations are out of order")
    cleanup_phases=[cleanup,instant(receipt["cleanupStartedAt"]),instant(receipt["beforeCleanupStore"]["startedAt"]),
        instant(events[5]["observedAt"]),instant(receipt["afterCleanupStore"]["startedAt"])]
    if cleanup_phases!=sorted(cleanup_phases):
        raise ValueError("original role cleanup observations do not follow both journeys and deletion")
    blocks={};identities=[]
    for selector,canonical in CLIENTS.items():
        pair=state["keys"][selector]
        if not isinstance(pair,dict) or set(pair)!={"primaryReceipt","foreignReceipt"}:
            raise ValueError("both original normal managed caller receipts required")
        pair_identities=[]
        for receipt_name,profile in (("primaryReceipt","admin"),("foreignReceipt","workspace-reader")):
            receipt=pair[receipt_name]
            identity=verify_receipt(receipt,runtime,profile,**({"service_id":service_id} if profile=="workspace-reader" else {}))
            if receipt["credentialSalt"]!=state["credentialSalt"]:
                raise ValueError("managed caller salt differs from original shared cohort")
            if instant(identity["observedAt"])<phases[-1]:
                raise ValueError("managed caller provisioning preceded normal role readback")
            if cleanup>instant(receipt["events"][2]["observedAt"]):
                raise ValueError("managed revocation preceded declared shared cleanup")
            identities.append(identity)
            pair_identities.append(identity)
        matches=[item for item in role["cohort"]["journeys"] if item["canonicalClient"]==canonical]
        if len(matches)!=1:raise ValueError("one original journey for each canonical client required")
        item=matches[0]
        raw=base64.b64decode(item["journeyBytesBase64"],validate=True)
        if "sha256:"+hashlib.sha256(raw).hexdigest()!=item["journeySha256"]:
            raise ValueError("original managed cohort journey bytes differ")
        original=strict_json(raw)
        if any(instant(identity["observedAt"])>instant(original["startedAt"]) for identity in pair_identities):
            raise ValueError("both managed callers must be provisioned before their original journey")
        if instant(original["completedAt"])>cleanup:
            raise ValueError("managed cleanup must follow both complete original journeys")
        block={key:state[key] for key in ("configuration","configurationObservation","roleReceipt","cleanupStartedAt")}
        block.update(pair)
        blocks[canonical]={"original":original,"identity":block}
    for field in ("keyId","canonicalActorId","studioActorId","workspaceOwnerId","credentialFingerprint"):
        if len({identity[field] for identity in identities})!=4:
            raise ValueError("both clients must use independently distinct original managed callers")
    return blocks


def workspace_identity(prerequisite, identity, tenant, *, requested_tenant):
    security=prerequisite.get("submitterSecurityContext") if isinstance(prerequisite,dict) else None
    raw=base64.b64decode(prerequisite.get("durableJobBytesBase64",""),validate=True)
    if (not raw or len(raw)>2*1024*1024
            or prerequisite.get("durableJobSha256")!="sha256:"+hashlib.sha256(raw).hexdigest()):
        raise ValueError("original durable workspace job bytes missing or altered")
    job=strict_json(raw)
    original=job.get("audit",{}).get("submitterSecurityContext",{}) if isinstance(job,dict) else {}
    projection={key:original.get(key) for key in ("principalId","tenantId","ownerActorId","workspaceOwnerId","roleClaimType")}
    projection["claims"]=[claim for claim in original.get("claims",[]) if isinstance(claim,dict)
        and claim.get("type") in ("api_key_id","api_key_name","permission","http://schemas.microsoft.com/ws/2008/06/identity/claims/role")]
    if (job.get("operationId")!=prerequisite.get("jobId") or type(job.get("status")) is not int or job["status"]!=3
            or job.get("audit",{}).get("requestedBy")!=identity["canonicalActorId"]
            or security!=projection):
        raise ValueError("workspace identity differs from original actual durable job")
    if (not isinstance(security,dict) or set(security)!={"principalId","tenantId","ownerActorId","workspaceOwnerId","roleClaimType","claims"}
            or security["tenantId"]!=tenant or security["workspaceOwnerId"]!=identity["workspaceOwnerId"]
            or security["principalId"]!=identity["canonicalActorId"]
            or security["ownerActorId"]!=identity["canonicalActorId"]
            or prerequisite.get("ownerId")!=identity["workspaceOwnerId"] or prerequisite.get("scopeId")!=tenant):
        raise ValueError("actual workspace submitter principal/tenant/owner binding missing")
    claims=security["claims"]
    if not isinstance(claims,list) or any(not isinstance(claim,dict) or set(claim)!={"type","value"} for claim in claims):
        raise ValueError("actual workspace identity claims malformed")
    if [claim["value"] for claim in claims if claim["type"]=="api_key_id"]!=[identity["keyId"]]:
        raise ValueError("actual GP submitter key identity differs")
    permissions=[claim["value"] for claim in claims if claim["type"]=="permission"]
    if len(permissions)!=len(set(permissions)) or sorted(permissions)!=sorted(identity["permissions"]):
        raise ValueError("actual GP submitter permission claims differ")
    expected_role="admin" if identity["permissions"]==["admin"] else "scoped-api-key"
    if [claim["value"] for claim in claims if claim["type"]==security["roleClaimType"]]!=[expected_role]:
        raise ValueError("actual GP submitter non-bypass role differs")
    records=[prerequisite.get("submitted"),*(prerequisite.get("polls") or []),prerequisite.get("output")]
    for record in records:
        if (not isinstance(record,dict) or record.get("credentialFingerprint")!=identity["credentialFingerprint"]
                or record.get("tenantId")!=requested_tenant or "startedAt" not in record or "completedAt" not in record
                or instant(record["completedAt"])<instant(record["startedAt"])):
            raise ValueError("actual workspace HTTP key/tenant observation missing")
    return records


def verify_full_identity(block, *, runtime, nonce, service_id, calls, primary_stores, foreign_stores,
                         primary_prerequisite, foreign_prerequisite, personal_prerequisite, personal_stores,
                         primary_deployment_id, foreign_deployment_id):
    expected={"configuration","configurationObservation","roleReceipt","primaryReceipt","foreignReceipt","cleanupStartedAt"}
    if not isinstance(block,dict) or set(block)!=expected:
        raise ValueError("full original tenant/owner/role receipt missing")
    cleanup_started=instant(block["cleanupStartedAt"])
    config=block["configuration"]
    checked=verify_configuration(config,block["configurationObservation"],runtime)
    if checked["primaryTenant"]!="mcp-deployment-"+nonce:
        raise ValueError("owned tenant must bind the authored journey nonce")
    role=verify_role_receipt(block["roleReceipt"],config,block["configurationObservation"],runtime)
    primary=verify_receipt(block["primaryReceipt"],runtime,"admin")
    foreign=verify_receipt(block["foreignReceipt"],runtime,"workspace-reader",service_id=service_id)
    if (primary["keyId"]==foreign["keyId"] or primary["canonicalActorId"]==foreign["canonicalActorId"]
            or primary["studioActorId"]==foreign["studioActorId"] or primary["workspaceOwnerId"]==foreign["workspaceOwnerId"]
            or primary["credentialFingerprint"]==foreign["credentialFingerprint"]
            or block["primaryReceipt"]["credentialSalt"]!=block["foreignReceipt"]["credentialSalt"]):
        raise ValueError("normal callers must have independently distinct principals and owners")
    if primary_deployment_id==foreign_deployment_id:
        raise ValueError("foreign positive cannot alias the denied primary resource")
    for stores,identity,tenant in ((primary_stores,primary,checked["primaryTenant"]),(foreign_stores,foreign,checked["foreignTenant"])):
        if stores.get("databaseIdentity")!=role["databaseIdentity"]:
            raise ValueError("normal role and actual resource stores belong to different databases")
        item,version,workspace=(stores.get(key) for key in ("studioItem","studioVersion","workspace"))
        if (not isinstance(item,dict) or not isinstance(version,dict) or not isinstance(workspace,dict)
                or item.get("ownerId")!=identity["studioActorId"] or version.get("ownerId")!=identity["studioActorId"]
                or item.get("tenantId")!=tenant or version.get("tenantId")!=tenant
                or workspace.get("owner_id")!=identity["workspaceOwnerId"] or workspace.get("scope_id")!=tenant):
            raise ValueError("actual normal caller does not own its independent tenant resources")
    if checked["primaryTenant"]==checked["foreignTenant"]:
        raise ValueError("tenant isolation cannot be inferred from same-tenant callers")
    personal_workspace=personal_stores.get("workspace")
    if (personal_stores.get("databaseIdentity")!=role["databaseIdentity"] or not isinstance(personal_workspace,dict)
            or personal_workspace.get("owner_id")!=primary["workspaceOwnerId"]
            or personal_workspace.get("scope_id")!=checked["foreignTenant"]
            or personal_prerequisite.get("workspaceId") in (primary_prerequisite.get("workspaceId"),foreign_prerequisite.get("workspaceId"))
            or personal_workspace.get("workspace_id")!=personal_prerequisite.get("workspaceId")):
        raise ValueError("same-tenant personal-owner denial requires an independently owned real workspace")
    gp_records={"primary":workspace_identity(primary_prerequisite,primary,checked["primaryTenant"],requested_tenant=checked["primaryTenant"]),
                "foreign":workspace_identity(foreign_prerequisite,foreign,checked["foreignTenant"],requested_tenant=None)}
    gp_records["primary"].extend(workspace_identity(personal_prerequisite,primary,checked["foreignTenant"],requested_tenant=checked["foreignTenant"]))
    identities={"primary":primary,"foreign":foreign}
    for role,identity in identities.items():
        receipt=block[role+"Receipt"]
        provisioned=instant(receipt["identity"]["observedAt"])
        revoked=instant(receipt["events"][2]["observedAt"])
        if cleanup_started>revoked:
            raise ValueError("owned cleanup start does not precede normal revocation")
        for record in gp_records[role]:
            if not provisioned<=instant(record["startedAt"])<=instant(record["completedAt"])<=cleanup_started:
                raise ValueError("managed lifecycle does not bracket actual workspace execution")
    positions={}
    for index,call in enumerate(calls):
        positions[call["id"]]=index
        if call.get("authenticated") is not True: continue
        role=call.get("credentialRole","primary")
        if role not in identities: raise ValueError("unknown managed caller role")
        identity=identities[role]; receipt=block[role+"Receipt"]
        if (call.get("wire",{}).get("expectedCredentialFingerprint")!=identity["credentialFingerprint"]
                or "startedAt" not in call or "completedAt" not in call
                or not instant(receipt["identity"]["observedAt"])<=instant(call["startedAt"])<=instant(call["completedAt"])<=cleanup_started):
            raise ValueError("normal managed lifecycle/key does not bind every actual MCP call")
    for own,denied,uri in (("foreign-own-deployment","foreign-deployment","honua://deployments/"+foreign_deployment_id),
                           ("foreign-own-workspace","foreign-workspace","honua://workspaces/"+foreign_prerequisite["workspaceId"]),
                           ("foreign-own-workspace","foreign-personal-workspace","honua://workspaces/"+foreign_prerequisite["workspaceId"])):
        if own not in positions or denied not in positions or positions[own]>=positions[denied]:
            raise ValueError("actual eligible caller own-resource positive must precede foreign denial")
        call=calls[positions[own]]
        if call.get("credentialRole")!="foreign" or call.get("params")!={"uri":uri}:
            raise ValueError("foreign own-resource positive bound to wrong resource or principal")
    personal_denial=calls[positions["foreign-personal-workspace"]]
    if (personal_denial.get("credentialRole")!="foreign" or personal_denial.get("params")!={"uri":"honua://workspaces/"+personal_prerequisite["workspaceId"]}):
        raise ValueError("same-tenant personal-owner denial targets the wrong caller/resource")
    return {"primaryKeyId":primary["keyId"],"foreignKeyId":foreign["keyId"],
        "primaryTenant":checked["primaryTenant"],"foreignTenant":checked["foreignTenant"],
        "configurationSha256":checked["configurationSha256"],"normalManagedIdentity":True,"tenantOwnerIsolation":True}
