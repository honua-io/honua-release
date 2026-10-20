"""Explicit offline normal role API/store controls, not candidate evidence."""
import base64
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import mcp_deployment_identity_role as subject
from mcp_deployment_identity_configuration import declaration,digest,ENV

RUNTIME={"running":True,"containerId":"a"*64,"imageId":"sha256:"+"b"*64,"candidateSourceSha":"c"*40}
IDENTIFIER="11111111-1111-4111-8111-111111111111"


def original_journeys(fixture):
    # Explicit offline pre-cleanup original observation envelopes, no qualification.
    return [json.dumps({"schema":"honua.mcp-deployment-client-journey/v1","canonicalClient":client,
        "cohortId":"d"*12,"candidate":copy.deepcopy(fixture.receipt["candidate"]),
        "configurationSha256":fixture.receipt["configurationSha256"],"startedAt":"2026-10-20T00:00:01Z",
        "completedAt":"2026-10-20T00:00:02Z","observations":{"canonicalClient":client,"nonce":"d"*12,
            "calls":[{"explicitOfflineControl":True}]}}).encode() for client in subject.CANONICAL_CLIENTS]


def rehash_store(value):
    raw=json.dumps(value["value"]).encode();value["storeBytesBase64"]=base64.b64encode(raw).decode()
    value["storeSha256"]="sha256:"+hashlib.sha256(raw).hexdigest()


def mutate_journey(binding,mutate):
    value=json.loads(base64.b64decode(binding["journeyBytesBase64"]))
    mutate(value);raw=json.dumps(value).encode();binding["journeyBytesBase64"]=base64.b64encode(raw).decode()
    binding["journeySha256"]="sha256:"+hashlib.sha256(raw).hexdigest()
    for field in ("canonicalClient","startedAt","completedAt"):binding[field]=value[field]

class OfflineRoleApi:
    def __init__(self): self.role=None; self.grants=[]; self.mutations=[]
    def open(self,request,timeout):
        assert request.get_header("X-api-key")=="controlled-private-admin"
        path=urlsplit(request.full_url).path; method=request.method
        status=200
        if method=="GET" and path=="/api/v1/admin/roles/": data=[] if self.role is None or self.role.get("deletedAt") else [self.api_role()]
        elif method=="POST" and path=="/api/v1/admin/roles/":
            self.mutations.append((method,path))
            if self.role is not None: raise ValueError("explicit offline globally unique role-name tombstone conflict")
            body=json.loads(request.data)
            self.role={"roleId":IDENTIFIER,"name":body["name"],"description":body["description"],"isBuiltIn":False,
                "createdAt":"2026-10-20T00:00:00Z","updatedAt":"2026-10-20T00:00:00Z","deletedAt":None}; status=201; data=self.api_role()
        elif method=="PUT" and path.endswith("/permissions"):
            self.mutations.append((method,path)); self.grants=json.loads(request.data)["permissions"]; data=self.grants
        elif method=="GET" and path.endswith("/permissions"): data=self.grants
        elif method=="GET" and path.endswith(IDENTIFIER): data=self.api_role()
        elif method=="DELETE" and path.endswith(IDENTIFIER):
            self.mutations.append((method,path)); self.grants=[]; self.role["deletedAt"]="2026-10-20T00:01:00Z"; data=None
        else: raise AssertionError("unexpected offline API route")
        raw=json.dumps({"success":True,"data":data}).encode()
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,limit): return raw[:limit]
        response=Response(); response.status=status; return response
    def api_role(self): return {key:value for key,value in self.role.items() if key!="deletedAt"}|{"permissions":copy.deepcopy(self.grants)}
    def observe(self,identifier):
        assert identifier==IDENTIFIER
        value={"databaseIdentity":{"clusterId":"1234567","database":"honua_test","user":"honua"},
            "role":copy.deepcopy(self.role),"permissions":copy.deepcopy(self.grants)}
        raw=json.dumps(value).encode()
        return {"startedAt":"2026-10-20T00:00:00Z","storeBytesBase64":base64.b64encode(raw).decode(),
            "storeSha256":"sha256:"+hashlib.sha256(raw).hexdigest(),"value":value}

class RoleControls(unittest.TestCase):
    def fixture(self,api):
        configuration=declaration("mcp-deployment-"+"d"*12)
        observation={"schema":"honua.mcp-deployment-configuration-observation/v1","declarationSha256":digest(configuration),
            "containerId":RUNTIME["containerId"],"imageId":RUNTIME["imageId"],"environment":dict(ENV),"observedAt":"2026-10-20T00:00:00Z"}
        fixture=subject.ManagedRoleFixture(admin_credential="controlled-private-admin",configuration=configuration,
            configuration_observation=observation,inspect_owned=lambda:copy.deepcopy(RUNTIME),observe=api.observe)
        return fixture,configuration,observation
    def complete(self):
        api=OfflineRoleApi(); fixture,config,observation=self.fixture(api)
        with patch.object(subject,"build_opener",return_value=api): fixture.create(); fixture.bind_journeys(original_journeys(fixture)); fixture.revoke()
        return fixture,config,observation,api
    def test_normal_create_grant_readback_revoke_original_bytes_join_independent_database(self):
        fixture,config,observation,api=self.complete()
        result=subject.verify_role_receipt(fixture.receipt,config,observation,RUNTIME)
        self.assertEqual(result["permissions"],subject.GRANTS)
        self.assertEqual([method for method,path in api.mutations],["POST","PUT","DELETE"])
        self.assertNotIn("controlled-private-admin",json.dumps(fixture.receipt))
    def test_preexisting_role_never_mutated_or_adopted(self):
        api=OfflineRoleApi(); api.role={"roleId":IDENTIFIER,"name":"scoped-api-key","description":"foreign-role","isBuiltIn":False}
        fixture,_,_=self.fixture(api)
        with patch.object(subject,"build_opener",return_value=api),self.assertRaises(ValueError): fixture.create()
        self.assertEqual(api.mutations,[])
    def test_foreign_name_marker_builtin_or_grants_prevent_delete(self):
        for field,value in [("name","different-role"),("description","foreign-owner"),("isBuiltIn",True),("grants",[{"service":"*","layer":"*","operation":"*"}])]:
            with self.subTest(field=field):
                api=OfflineRoleApi(); fixture,_,_=self.fixture(api)
                with patch.object(subject,"build_opener",return_value=api):
                    fixture.create()
                    if field=="grants":api.grants=value
                    else:api.role[field]=value
                    with self.assertRaises(ValueError):fixture.revoke()
                self.assertFalse(any(method=="DELETE" for method,path in api.mutations))
    def test_original_role_bytes_config_identity_and_cleanup_mutations_refuse(self):
        fixture,config,observation,api=self.complete()
        for field,value in [("cleanupComplete",False),("configurationSha256","sha256:"+"0"*64),("marker","foreign-owner"),("events",[])]:
            altered=copy.deepcopy(fixture.receipt);altered[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):subject.verify_role_receipt(altered,config,observation,RUNTIME)
        for field in ("createdStore","configuredStore","beforeCleanupStore","afterCleanupStore"):
            altered=copy.deepcopy(fixture.receipt); altered[field]["value"]["role"]["description"]="foreign-owner"
            with self.subTest(field=field),self.assertRaises(ValueError):subject.verify_role_receipt(altered,config,observation,RUNTIME)

    def test_foreign_create_record_never_authorizes_permission_update_or_delete(self):
        for field,value in [("description","foreign-owner"),("name","foreign-role"),("isBuiltIn",True)]:
            with self.subTest(field=field):
                api=OfflineRoleApi(); fixture,_,_=self.fixture(api)
                original=api.observe
                def foreign(identifier):
                    api.role[field]=value
                    return original(identifier)
                fixture.observe=foreign
                with patch.object(subject,"build_opener",return_value=api),self.assertRaises(ValueError):fixture.create()
                self.assertEqual([method for method,path in api.mutations],["POST"])
                self.assertEqual(fixture.receipt["failures"][0]["phase"],"create")
    def test_failed_cleanup_retained_after_actual_owned_retry_and_final_rejudge_refuses(self):
        api=OfflineRoleApi(); fixture,config,observation=self.fixture(api)
        with patch.object(subject,"build_opener",return_value=api):
            fixture.create(); fixture.bind_journeys(original_journeys(fixture));marker=api.role["description"];api.role["description"]="foreign-role"
            with self.assertRaises(ValueError):fixture.revoke()
            self.assertFalse(any(method=="DELETE" for method,path in api.mutations))
            api.role["description"]=marker; fixture.revoke()
        self.assertTrue(fixture.receipt["cleanupComplete"])
        self.assertEqual(fixture.receipt["failures"][0]["phase"],"revoke")
        with self.assertRaises(ValueError):subject.verify_role_receipt(fixture.receipt,config,observation,RUNTIME)

    def test_one_role_lifecycle_binds_both_original_client_journeys_and_cannot_recreate_tombstone(self):
        fixture,config,observation,api=self.complete()
        value=subject.verify_role_receipt(fixture.receipt,config,observation,RUNTIME)
        self.assertEqual([member["canonicalClient"] for member in value["cohort"]["journeys"]],subject.CANONICAL_CLIENTS)
        self.assertEqual(sum(method=="POST" for method,path in api.mutations),1)
        second,_,_=self.fixture(api)
        with patch.object(subject,"build_opener",return_value=api),self.assertRaises(ValueError):second.create()
        self.assertEqual(second.receipt["failures"][0]["phase"],"create")
        self.assertFalse(second.receipt["cleanupComplete"])
    def test_rehashed_cross_cluster_and_creation_identity_substitutions_refuse(self):
        fixture,config,observation,api=self.complete()
        for phase in ("createdStore","configuredStore","beforeCleanupStore","afterCleanupStore"):
            for field,value in (("clusterId","7654321"),("createdAt","2026-10-19T23:59:59Z")):
                altered=copy.deepcopy(fixture.receipt)
                target=altered[phase]["value"]["databaseIdentity"] if field=="clusterId" else altered[phase]["value"]["role"]
                target[field]=value;rehash_store(altered[phase])
                with self.subTest(phase=phase,field=field),self.assertRaises(ValueError):subject.verify_role_receipt(altered,config,observation,RUNTIME)
    def test_rehashed_member_candidate_nonce_and_late_journey_substitutions_refuse(self):
        fixture,config,observation,api=self.complete()
        for mutate in (lambda value:value["candidate"].update(containerId="e"*64),
                       lambda value:value.update(cohortId="e"*12),
                       lambda value:value.update(canonicalClient=subject.CANONICAL_CLIENTS[1],observations={"canonicalClient":subject.CANONICAL_CLIENTS[1],"nonce":"d"*12,"calls":[{}]}),
                       lambda value:value.update(completedAt="2099-10-20T00:00:02Z")):
            altered=copy.deepcopy(fixture.receipt);mutate_journey(altered["cohort"]["journeys"][0],mutate)
            with self.assertRaises(ValueError):subject.verify_role_receipt(altered,config,observation,RUNTIME)
        for mutation in ([],fixture.receipt["cohort"]["journeys"][:1]):
            altered=copy.deepcopy(fixture.receipt);altered["cohort"]["journeys"]=mutation
            with self.assertRaises(ValueError):subject.verify_role_receipt(altered,config,observation,RUNTIME)
    def test_rehashed_malformed_role_telemetry_refuses_supported_null_only(self):
        fixture,config,observation,api=self.complete()
        for field,value in (("createdAt",1),("updatedAt","2026-10-20T00:00:00"),("deletedAt",False)):
            altered=copy.deepcopy(fixture.receipt);altered["afterCleanupStore"]["value"]["role"][field]=value
            rehash_store(altered["afterCleanupStore"])
            with self.subTest(field=field),self.assertRaises(ValueError):subject.verify_role_receipt(altered,config,observation,RUNTIME)

if __name__=="__main__":unittest.main()
