"""Owned normal role fixture for a non-bypass deployment/workspace caller."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import hashlib
import json
import re
from functools import wraps
from urllib.request import Request, build_opener, ProxyHandler
from mcp_managed_identity_fixture import NoRedirect, guid
from mcp_map_package_validation import strict_json, same_json
from mcp_deployment_identity_configuration import digest, verify_configuration

GRANTS = [{"service": "Deployment", "layer": "*", "operation": "Read"},
          {"service": "Workspace", "layer": "*", "operation": "Read"}]

def stamp():
    return datetime.now(timezone.utc).isoformat()


CANONICAL_CLIENTS=["Official MCP TypeScript SDK","MCP Inspector"]


def instant(value):
    if not isinstance(value,str): raise ValueError("normal role timestamp malformed")
    try: parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError: raise ValueError("normal role timestamp malformed") from None
    if parsed.tzinfo is None: raise ValueError("normal role timestamp lacks timezone")
    return parsed


def journey_bindings(originals,candidate,configuration_sha256,cohort_id):
    if not isinstance(originals,list) or len(originals)!=2:
        raise ValueError("one shared role requires both original canonical client journeys")
    bindings=[];seen=set()
    fields={"schema","canonicalClient","cohortId","candidate","configurationSha256","startedAt","completedAt","observations"}
    for raw in originals:
        if not isinstance(raw,bytes) or not raw or len(raw)>2*1024*1024:
            raise ValueError("original client journey bytes missing or oversized")
        value=strict_json(raw)
        if (not isinstance(value,dict) or set(value)!=fields or value["schema"]!="honua.mcp-deployment-client-journey/v1"
                or value["canonicalClient"] not in CANONICAL_CLIENTS or value["canonicalClient"] in seen
                or value["cohortId"]!=cohort_id or value["candidate"]!=candidate
                or value["configurationSha256"]!=configuration_sha256
                or instant(value["completedAt"])<instant(value["startedAt"])
                or not isinstance(value["observations"],dict)
                or value["observations"].get("canonicalClient")!=value["canonicalClient"]
                or value["observations"].get("nonce")!=cohort_id
                or not isinstance(value["observations"].get("calls"),list) or not value["observations"]["calls"]):
            raise ValueError("original shared role client journey binding differs")
        seen.add(value["canonicalClient"])
        bindings.append({"canonicalClient":value["canonicalClient"],"journeyBytesBase64":base64.b64encode(raw).decode(),
            "journeySha256":"sha256:"+hashlib.sha256(raw).hexdigest(),"startedAt":value["startedAt"],"completedAt":value["completedAt"]})
    return sorted(bindings,key=lambda value:CANONICAL_CLIENTS.index(value["canonicalClient"]))


def observe_role(role_id, password, *, query=None):
    from mcp_ingest_publish_state import database_query
    query = query or database_query
    role_id = guid(role_id)
    started = stamp()
    raw = query("""SET TRANSACTION READ ONLY;
SELECT jsonb_build_object(
 'databaseIdentity',(SELECT jsonb_build_object('clusterId',system_identifier::text,'database',current_database(),'user',current_user) FROM pg_control_system()),
 'role',(SELECT jsonb_build_object('roleId',role_id,'name',name,'description',description,'isBuiltIn',is_built_in,'createdAt',created_at,'updatedAt',updated_at,'deletedAt',deleted_at) FROM honua.rbac_roles WHERE role_id='""" + role_id + """'::uuid),
 'permissions',COALESCE((SELECT jsonb_agg(jsonb_build_object('service',service,'layer',layer,'operation',operation) ORDER BY service,layer,operation) FROM honua.rbac_role_permissions WHERE role_id='""" + role_id + """'::uuid),'[]'::jsonb))::text;""", password)
    if isinstance(raw, str): raw = raw.encode()
    value = strict_json(raw)
    return {"startedAt": started, "storeBytesBase64": base64.b64encode(raw).decode(),
            "storeSha256": "sha256:"+hashlib.sha256(raw).hexdigest(), "value": value}


def owned_record(observation, role_id, marker, grants, *, deleted=False):
    if not isinstance(observation, dict) or set(observation) != {"startedAt", "storeBytesBase64", "storeSha256", "value"}:
        raise ValueError("independent role observation missing")
    instant(observation["startedAt"])
    raw=base64.b64decode(observation["storeBytesBase64"], validate=True)
    if (len(raw)>65536 or observation["storeSha256"]!="sha256:"+hashlib.sha256(raw).hexdigest()
            or not same_json(strict_json(raw), observation["value"])):
        raise ValueError("original role store bytes differ")
    value=observation["value"]
    def identity(value):
        if (not isinstance(value,dict) or set(value)!={"clusterId","database","user"}
                or not isinstance(value["clusterId"],str) or not re.fullmatch(r"[0-9]+",value["clusterId"])
                or value["database"]!="honua_test" or value["user"]!="honua"):
            raise ValueError("independent owned role database identity differs")
    if not isinstance(value, dict) or set(value)!={"databaseIdentity", "role", "permissions"}:
        raise ValueError("independent role store schema differs")
    identity(value["databaseIdentity"])
    role=value["role"]
    if (not isinstance(role, dict) or set(role)!={"roleId","name","description","isBuiltIn","createdAt","updatedAt","deletedAt"}
            or role["roleId"]!=guid(role_id) or role["name"]!="scoped-api-key"
            or role["description"]!=marker or role["isBuiltIn"] is not False
            or (role["deletedAt"] is not None)!=deleted or not same_json(value["permissions"], grants)):
        raise ValueError("normal role is not the exact owned non-bypass fixture")
    created,updated=instant(role["createdAt"]),instant(role["updatedAt"])
    if updated<created or (role["deletedAt"] is not None and instant(role["deletedAt"])<created):
        raise ValueError("normal role persisted timestamp order differs")
    return role


def retained_failure(phase):
    def decorate(method):
        @wraps(method)
        def invoke(self,*args,**kwargs):
            try: return method(self,*args,**kwargs)
            except Exception:
                # Never retain exception text: a transport diagnostic may echo a key.
                self.receipt["failures"].append({"phase":phase,"observedAt":stamp()})
                raise
        return invoke
    return decorate


class ManagedRoleFixture:
    """No adoption or overwrite of an existing role; observe ownership before cleanup."""
    def __init__(self, *, admin_credential, configuration, configuration_observation, inspect_owned, observe, http=None):
        if not isinstance(admin_credential,str) or not admin_credential or re.search(r"[\r\n]",admin_credential):
            raise ValueError("private role fixture credential malformed")
        self.inspect=inspect_owned; self.runtime=self.inspect()
        verify_configuration(configuration, configuration_observation, self.runtime)
        self.configuration=configuration; self.admin=admin_credential; self.observe=observe; self.http=http or self.request
        self.marker="mcp-owned-deployment:"+self.runtime["containerId"]+":"+digest(configuration)
        self.role_id=None; self.grants=[]
        self.receipt={"schema":"honua.mcp-deployment-role-fixture/v1","marker":self.marker,
            "candidate":{key:self.runtime.get(key) for key in ("containerId","imageId","candidateSourceSha")},
            "configurationSha256":digest(configuration),"events":[],"failures":[],"cleanupComplete":False,
            "cohort":{"id":configuration["primaryTenant"].removeprefix("mcp-deployment-"),"members":CANONICAL_CLIENTS.copy(),"journeys":[]}}

    def request(self, method, path, body=None):
        if not re.fullmatch(r"/api/v1/admin/roles/(?:[a-f0-9-]{36}(?:/permissions)?)?",path):
            raise ValueError("role fixture request outside fixed API")
        current=self.inspect()
        if any(current.get(key)!=self.runtime.get(key) for key in ("containerId","imageId","candidateSourceSha")):
            raise ValueError("role fixture runtime changed")
        raw=b"" if body is None else json.dumps(body).encode()
        request=Request("http://127.0.0.1:8080"+path, data=raw if method in ("POST","PUT") else None,
            method=method,headers={"X-API-Key":self.admin,"Content-Type":"application/json"})
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=20) as response:
            payload=response.read(1048577)
            if len(payload)>1048576 or response.status not in (200,201,204):
                raise ValueError("normal role fixture response refused")
            event={"method":method,"path":path,"status":response.status,"observedAt":stamp(),
                "requestBytesBase64":base64.b64encode(raw).decode(),"responseBytesBase64":base64.b64encode(payload).decode(),
                "requestSha256":"sha256:"+hashlib.sha256(raw).hexdigest(),"responseSha256":"sha256:"+hashlib.sha256(payload).hexdigest()}
            self.receipt["events"].append(event)
            if response.status==204: return None
            value=strict_json(payload)
            if (not isinstance(value,dict) or value.get("success") is not True
                    or (method != "DELETE" and "data" not in value)):
                raise ValueError("normal role fixture envelope malformed")
            current=self.inspect()
            if any(current.get(key)!=self.runtime.get(key) for key in ("containerId","imageId","candidateSourceSha")):
                raise ValueError("role fixture runtime changed after request")
            return value.get("data")

    @retained_failure("create")
    def create(self):
        census=self.http("GET","/api/v1/admin/roles/")
        if not isinstance(census,list) or any(not isinstance(role,dict) for role in census):
            raise ValueError("normal role census malformed")
        if any(str(role.get("name","")).lower()=="scoped-api-key" for role in census):
            raise ValueError("preexisting role cannot be adopted or overwritten")
        result=self.http("POST","/api/v1/admin/roles/",{"name":"scoped-api-key","description":self.marker})
        if not isinstance(result,dict): raise ValueError("normal role creation malformed")
        identifier=guid(result.get("roleId"))
        # A foreign ID in a create response must never authorize PUT/DELETE.
        before=self.observe(identifier)
        owned_record(before,identifier,self.marker,[])
        self.role_id=identifier; self.receipt["createdStore"]=before; self.receipt["roleId"]=identifier
        permissions=self.http("PUT","/api/v1/admin/roles/"+identifier+"/permissions",{"permissions":GRANTS})
        # Independently read even if the normal response body is malformed.
        configured=self.observe(identifier)
        owned_record(configured,identifier,self.marker,GRANTS)
        self.grants=GRANTS; self.receipt["configuredStore"]=configured
        self.receipt["normalReadback"]=self.http("GET","/api/v1/admin/roles/"+identifier)
        self.receipt["normalPermissions"]=self.http("GET","/api/v1/admin/roles/"+identifier+"/permissions")
        return self.receipt

    @retained_failure("bind-journeys")
    def bind_journeys(self,originals):
        if self.role_id is None or self.receipt["cohort"]["journeys"] or self.receipt["cleanupComplete"]:
            raise ValueError("shared role cohort cannot be rebound or reused")
        self.receipt["cohort"]["journeys"]=journey_bindings(originals,self.receipt["candidate"],self.receipt["configurationSha256"],self.receipt["cohort"]["id"])
        return self.receipt

    @retained_failure("revoke")
    def revoke(self):
        self.receipt["cleanupStartedAt"]=stamp()
        if self.role_id is None: raise ValueError("owned role identity missing; no deletion permitted")
        # Re-read ownership and complete grant set BEFORE the normal mutation.
        before=self.observe(self.role_id)
        current = before.get("value",{}).get("permissions") if isinstance(before,dict) else None
        if current not in ([],GRANTS):
            raise ValueError("unexpected role grants; deletion refused")
        owned_record(before,self.role_id,self.marker,current)
        self.receipt["beforeCleanupStore"]=before
        self.http("DELETE","/api/v1/admin/roles/"+self.role_id)
        after=self.observe(self.role_id)
        owned_record(after,self.role_id,self.marker,[],deleted=True)
        self.receipt["afterCleanupStore"]=after; self.receipt["cleanupComplete"]=True
        return self.receipt


def verify_role_receipt(receipt, configuration, configuration_observation, runtime):
    """Rejudge original normal API and DB bytes after owned role cleanup."""
    verify_configuration(configuration,configuration_observation,runtime)
    expected={"schema","marker","candidate","configurationSha256","events","failures","cleanupComplete","cohort","cleanupStartedAt","roleId",
        "createdStore","configuredStore","normalReadback","normalPermissions","beforeCleanupStore","afterCleanupStore"}
    if (not isinstance(receipt,dict) or set(receipt)!=expected
            or receipt["schema"]!="honua.mcp-deployment-role-fixture/v1" or receipt["cleanupComplete"] is not True or receipt["failures"]!=[]
            or receipt["configurationSha256"]!=digest(configuration)
            or receipt["candidate"]!={key:runtime.get(key) for key in ("containerId","imageId","candidateSourceSha")}):
        raise ValueError("original owned role fixture incomplete")
    marker="mcp-owned-deployment:"+runtime["containerId"]+":"+digest(configuration)
    identifier=guid(receipt["roleId"])
    if receipt["marker"]!=marker: raise ValueError("owned role marker differs")
    identities=[];created_times=[]
    for field,grants,deleted in [("createdStore",[],False),("configuredStore",GRANTS,False),
                                ("beforeCleanupStore",GRANTS,False),("afterCleanupStore",[],True)]:
        owned_record(receipt[field],identifier,marker,grants,deleted=deleted)
        identities.append(receipt[field]["value"]["databaseIdentity"])
        created_times.append(instant(receipt[field]["value"]["role"]["createdAt"]))
    if any(value!=identities[0] for value in identities) or any(value!=created_times[0] for value in created_times):
        raise ValueError("shared role database cluster or original creation identity changed")
    cohort=receipt["cohort"]
    if (not isinstance(cohort,dict) or set(cohort)!={"id","members","journeys"}
            or cohort["id"]!=configuration["primaryTenant"].removeprefix("mcp-deployment-") or cohort["members"]!=CANONICAL_CLIENTS
            or not isinstance(cohort["journeys"],list)):
        raise ValueError("original shared canonical-client role cohort missing")
    originals=[]
    for binding in cohort["journeys"]:
        if not isinstance(binding,dict) or set(binding)!={"canonicalClient","journeyBytesBase64","journeySha256","startedAt","completedAt"}:
            raise ValueError("original role cohort journey schema differs")
        originals.append(base64.b64decode(binding["journeyBytesBase64"],validate=True))
    if not same_json(journey_bindings(originals,receipt["candidate"],receipt["configurationSha256"],cohort["id"]),cohort["journeys"]):
        raise ValueError("original role cohort bytes/hash differ")
    cleanup_started=instant(receipt["cleanupStartedAt"])
    configured=instant(receipt["configuredStore"]["startedAt"])
    if any(not configured<=instant(binding["startedAt"])<=instant(binding["completedAt"])<=cleanup_started for binding in cohort["journeys"]):
        raise ValueError("one role lifecycle must bracket both canonical-client journeys before cleanup")
    expected_events=[("GET","/api/v1/admin/roles/"),("POST","/api/v1/admin/roles/"),
        ("PUT","/api/v1/admin/roles/"+identifier+"/permissions"),("GET","/api/v1/admin/roles/"+identifier),
        ("GET","/api/v1/admin/roles/"+identifier+"/permissions"),("DELETE","/api/v1/admin/roles/"+identifier)]
    events=receipt["events"]
    if not isinstance(events,list) or len(events)!=len(expected_events): raise ValueError("normal role API lifecycle missing")
    bodies=[]
    for event,(method,path) in zip(events,expected_events,strict=True):
        fields={"method","path","status","observedAt","requestBytesBase64","responseBytesBase64","requestSha256","responseSha256"}
        if (not isinstance(event,dict) or set(event)!=fields or event["method"]!=method or event["path"]!=path
                or type(event["status"]) is not int or event["status"]!=(201 if method=="POST" else 200)):
            raise ValueError("normal role API event differs")
        raw=base64.b64decode(event["requestBytesBase64"],validate=True)
        response=base64.b64decode(event["responseBytesBase64"],validate=True)
        if (len(raw)>65536 or len(response)>1048576 or event["requestSha256"]!="sha256:"+hashlib.sha256(raw).hexdigest()
                or event["responseSha256"]!="sha256:"+hashlib.sha256(response).hexdigest()):
            raise ValueError("original normal role bytes differ")
        wanted=({"name":"scoped-api-key","description":marker} if method=="POST" else
                {"permissions":GRANTS} if method=="PUT" else None)
        if (wanted is None and raw) or (wanted is not None and not same_json(strict_json(raw),wanted)):
            raise ValueError("normal role request differs")
        value=strict_json(response)
        if not isinstance(value,dict) or value.get("success") is not True: raise ValueError("normal role API refused")
        bodies.append(value.get("data"))
    census,created,configured,readback,permissions,_=bodies
    if (not isinstance(census,list) or any(not isinstance(role,dict) or str(role.get("name","")).lower()=="scoped-api-key" for role in census)
            or not same_json(configured,GRANTS) or not same_json(permissions,GRANTS)
            or not same_json(readback,receipt["normalReadback"]) or not same_json(permissions,receipt["normalPermissions"])):
        raise ValueError("normal role API readback differs")
    for value,grants in ((created,[]),(readback,GRANTS)):
        if (not isinstance(value,dict) or value.get("roleId")!=identifier or value.get("name")!="scoped-api-key"
                or value.get("description")!=marker or value.get("isBuiltIn") is not False
                or not same_json(value.get("permissions"),grants)):
            raise ValueError("normal role response ownership differs")
        created,updated=instant(value.get("createdAt")),instant(value.get("updatedAt"))
        if updated<created: raise ValueError("normal role API timestamp order differs")
    if cleanup_started>instant(events[-1]["observedAt"]): raise ValueError("normal role deletion preceded declared cleanup")
    return {"roleId":identifier,"name":"scoped-api-key","permissions":GRANTS,"configurationSha256":digest(configuration),"cohort":cohort,
        "databaseIdentity":identities[0],"roleCreatedAt":created_times[0].isoformat()}
