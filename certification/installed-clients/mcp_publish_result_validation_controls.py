"""Authored offline controls only; no real worker/client/candidate credit."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import unittest

import mcp_publish_result_validation as validation
import mcp_publish_result_state as state
from mcp_render_validation_controls import FIXTURE, bootstrap as render_bootstrap, runtime, SOURCE, IMAGE
from mcp_map_package_validation_controls import control_wire, CONTROL_FINGERPRINT
from mcp_ingest_publish_validation_controls import connection_control
from mcp_execute_plan_validation_controls import envelope, resource

GUID = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-19T00:01:00Z"


def bootstrap():
    value = render_bootstrap(); value["connectionId"] = GUID; value["ownedArea"]["connectionId"] = GUID
    return value


def rebind(value):
    for index,entry in enumerate(value["calls"]):
        entry["wire"],entry["client"],entry["result"] = control_wire(entry["method"],entry["params"],entry["result"],index,entry["authenticated"])
    return value


def make_setup(authored):
    graph = authored["packageArguments"]["graph"]
    node = graph["nodes"][0]
    parts = [graph["schemaVersion"],"",node["nodeId"],node["nodeTypeId"],""]
    framed = "".join(str(len(v))+":"+v+";" for v in parts) + str(len(node["parameters"]))+";"
    for key,value in sorted(node["parameters"].items()):
        framed += str(len(key))+":"+key+";"+str(len(value))+":"+value+";"
    graph_hash = hashlib.sha256(framed.encode()).hexdigest()
    checked = {"isValid":True,"failures":[],"packageHash":graph_hash}
    publication = {"publicationId":"wfp-"+"d"*32,"packageId":authored["packageId"],"packageVersion":1,"packageHash":graph_hash,
        "target":"Schedule","status":"Active","schedule":authored["publicationArguments"]["schedule"],"workflowDefinitionId":"workflow-controlled-result",
        "endpointPath":"/api/v1/console/workflow-publications/wfp-"+"d"*32+"/runs","eligibility":checked}
    package = {"packageId":authored["packageId"],"name":authored["packageId"],"graph":graph}
    version = {"packageId":authored["packageId"],"version":1,"schemaVersion":"workflow-package.v1","packageHash":graph_hash,"graph":graph,"validation":checked}
    runid, jobid = "wf-"+"e"*32, "gp-"+"f"*24
    run_response = {"publicationId":publication["publicationId"],"packageId":authored["packageId"],"packageVersion":1,"target":"Schedule","jobId":None,"workflowRunId":runid}
    root = "/api/v1/console"; version_path = root+"/workflow-packages/"+authored["packageId"]+"/versions/1"
    steps = []
    for identifier,method,path,args,status,data in [
        ("package","POST",root+"/workflow-packages",authored["packageArguments"],201,package),
        ("version","POST",root+"/workflow-packages/"+authored["packageId"]+"/versions",None,201,version),
        ("validate","POST",version_path+"/validate",None,200,checked),
        ("publication","POST",version_path+"/publish",authored["publicationArguments"],200,publication),
        ("publication-read","GET",root+"/workflow-publications",None,200,{"items":[publication]}),
        ("run","POST",publication["endpointPath"],{},201,run_response)]:
        text = json.dumps({"success":True,"data":data,"timestamp":STAMP})
        steps.append({"id":identifier,"method":method,"path":path,"arguments":args,"authenticated":True,"credentialFingerprint":CONTROL_FINGERPRINT,
            "status":status,"responseMediaType":"application/json","responseText":text,"responseSha256":"sha256:"+hashlib.sha256(text.encode()).hexdigest()})
    plan = {"planId":publication["workflowDefinitionId"]+":sink","intentId":"workflow-package","outputs":[1],
        "steps":[{"stepId":"sink","kind":1,"processId":"sink.external-postgis","inputs":node["parameters"]}]}
    definition = {"workflowId":publication["workflowDefinitionId"],"trigger":{"kind":1,**authored["publicationArguments"]["schedule"]},
        "steps":[{"stepId":"sink","plan":plan}]}
    run = {"runId":runid,"workflowId":definition["workflowId"],"status":2,"triggerKind":0,
        "metadata":{"workflow.packageId":authored["packageId"],"workflow.packageVersion":"1","workflow.publicationId":publication["publicationId"],
            "workflow.packageHash":graph_hash,"workflow.publicationTarget":"Schedule"},
        "stepStates":[{"stepId":"sink","planId":plan["planId"],"status":3,"attemptCount":1,"jobId":jobid}]}
    receipt = {"processId":"sink.external-postgis","schema":"public","table":authored["table"],"batchId":authored["batchId"],"committed":True,"featuresWritten":2,"featuresRejected":0}
    uri = "data:application/json;base64,"+base64.b64encode(json.dumps(receipt).encode()).decode()
    parameters = {"honua.geoprocessing.plan_id":plan["planId"],"honua.geoprocessing.process_definitions":"sink.external-postgis","honua.geoprocessing.output_artifact_kinds":"FeatureLayer"}
    parameters.update({"honua.geoprocessing.step.0."+k:v for k,v in node["parameters"].items()})
    job = {"operationId":jobid,"status":3,"createdAt":STAMP,"spec":{"kind":0,"parameters":parameters},"artifactReferences":[uri],"committedEffectReferences":[uri]}
    artifact = {"artifactId":jobid+":artifact:1","kind":1,"label":"Committed PostGIS sink","uri":uri,"contentType":"application/json",
        "metadata":{"connectionId":GUID,"schema":"public","table":authored["table"]}}
    result = {"resultPackageId":"controlled-real-result-package","status":5,"errors":[],"artifacts":[artifact]}
    empty = {k:[] for k in ("jobs","results","workflows","definitions","proposals","operations")}
    baseline = copy.deepcopy(empty); baseline["definitions"] = [{"id":definition["workflowId"],"sha256":"sha256:"+"1"*64}]
    census = copy.deepcopy(baseline)
    for family,identifier,letter in (("jobs",jobid,"2"),("results",jobid,"3"),("workflows",runid,"4")):
        census[family] = [{"id":identifier,"sha256":"sha256:"+letter*64}]
    return {"schema":"honua.mcp-publish-result-setup/v1","phase":"complete","steps":steps,"connection":connection_control(),
        "beforeRun":{"census":baseline,"definition":copy.deepcopy(definition)},"runtimeBefore":runtime(),"runtimeAfter":runtime(),
        "workflow":{"schema":"honua.mcp-publish-result-workflow-state/v1","census":census,"run":run,"definition":definition,"job":job,"result":result}}


def database(authored,published):
    status = {"lifecycle":"active","state":"ready","observedAt":STAMP}
    metadata = lambda name: {"id":name,"name":name}
    old = {"schemaVersion":"metadata-v2","apiVersion":"v2","environment":"production","revision":41,"generatedAt":"2026-10-19T00:00:00Z",
        "metadata":metadata("root"),"policies":[{"unchanged":"policy"}],"namespaces":["owned"],
        "services":[{"metadata":metadata("prior-service"),"route":"/prior"}],
        "resources":[{"metadata":metadata("prior-resource"),"payload":{"must":"survive"}}],
        "storageBindings":[{"metadata":metadata("prior-binding")}],"publications":[{"metadata":metadata("prior-publication")}],
        "connections":[{"metadata":metadata(GUID),"type":"database","provider":"postgis","status":copy.deepcopy(status)}]}
    rows = [{"id":index+11,**copy.deepcopy(row),"srid":4326} for index,row in enumerate(authored["expectedRows"])]
    initial = {"schema":"honua.mcp-publish-result-persisted/v1","rows":rows,"mappings":[],"graphs":[{"environment":"production","revision":41,"document":old}],
        "services":[{"service_name":"prior-service","custom":"preserve"}],"layers":[{"layer_id":17,"custom":"preserve"}],
        "serviceLayers":[{"service_name":"prior-service","layer_id":17,"layer_order":0}],"fields":[{"layer_id":17,"field_name":"old","field_type":"String","field_order":1}],
        "tableNames":[{"schema":"public","name":authored["table"]}],"features":[{"id":900,"layerId":17,"sha256":"sha256:"+"5"*64}],
        "materialized":[],"connectionHashes":[{"connectionId":GUID,"sha256":"sha256:"+"6"*64}],"connection":connection_control()}
    current = copy.deepcopy(initial); physical = int(published["layerId"])
    current["services"].append({"service_name":authored["serviceName"]})
    current["layers"].append({"layer_id":physical,"layer_name":"authored-result","table_schema":"public","table_name":authored["table"],
        "primary_key_column":"id","geometry_column":"geom","storage_srid":4326,"srid":4326,"enabled":True,"default_visibility":True})
    current["serviceLayers"].append({"service_name":authored["serviceName"],"layer_id":physical,"layer_order":0})
    for ordinal,(name,kind) in enumerate((("id","BigInteger"),("geom","Geometry"),("attributes","Json")),1):
        current["fields"].append({"layer_id":physical,"field_name":name,"field_type":kind,"field_order":ordinal})
    for index,row in enumerate(rows):
        canonical_id = 881+index
        current["features"].append({"id":canonical_id,"layerId":physical,"sha256":"sha256:"+str(index+7)*64})
        current["materialized"].append({"id":canonical_id,"layerId":physical,"attributes":{"id":row["id"],"geom":copy.deepcopy(row["geometry"]),"attributes":copy.deepcopy(row["properties"])},"geometry":copy.deepcopy(row["geometry"]),"srid":4326})
    mapping = {"serviceId":"canonical-service-7","serviceName":authored["serviceName"],"layerId":0,"storageLayerId":physical,"locator":"public."+authored["table"],"connectionId":GUID}
    current["mappings"] = [{"environment":"production","revision":42,"publication":mapping}]
    graph = current["graphs"][0]; graph["revision"] = 42; document = graph["document"]; document.update(revision=42,generatedAt=STAMP)
    document["services"].append({"metadata":{"id":"canonical-service-7","name":authored["serviceName"]},"route":"/rest/services/"+authored["serviceName"]+"/FeatureServer","publicationIds":["own-feature","own-stac"]})
    document["resources"].append({"metadata":metadata("own-resource"),"type":"feature-dataset","primaryStorageBindingId":"own-binding","storageBindingIds":["own-binding"]})
    document["storageBindings"].append({"metadata":metadata("own-binding"),"resourceId":"own-resource","connectionId":GUID,"storageLayerId":physical,"locator":"public."+authored["table"]})
    for identifier,kind,primary in (("own-feature","esri-feature-layer",True),("own-stac","stac-collection",False)):
        document["publications"].append({"metadata":metadata(identifier),"serviceId":"canonical-service-7","resourceId":"own-resource","storageBindingId":"own-binding",
            "publicationType":kind,"isPrimary":primary,"identifier":{"value":"0","isNumeric":True}})
    return initial,current


def controls():
    authored = validation.scenario(FIXTURE,bootstrap(),"abcdef123456","Official MCP TypeScript SDK")
    setup = make_setup(authored); jobid = setup["workflow"]["job"]["operationId"]; artifact = setup["workflow"]["result"]["artifacts"][0]
    identity = {"jobId":jobid,"artifactId":artifact["artifactId"]}
    published = {"status":"Completed","requiresApproval":False,"operationId":"service.publish","sourceJobId":jobid,"artifactId":artifact["artifactId"],
        "serviceId":authored["serviceName"],"serviceUri":"honua://published-services/"+authored["serviceName"],"layerId":"741","metadataRevision":42,
        "operationInstanceId":"controlled-publication-operation","handleId":"controlled-publication-operation"}
    initial,completed = database(authored,published)
    uri = "honua://jobs/"+jobid
    worker = {"jobId":jobid,"status":"Succeeded","createdAt":STAMP,"updatedAt":STAMP,"completedAt":STAMP,"percentComplete":100,"warnings":[],"resultsUri":uri+"/results"}
    result = {"jobId":jobid,"resultPackageId":setup["workflow"]["result"]["resultPackageId"],"status":"Completed","totalArtifactCount":1,
        "artifactsTruncated":False,"errors":[],"artifacts":[{**artifact,"kind":"FeatureLayer"}]}
    mapping = completed["mappings"][0]["publication"]
    fresh = {"serviceId":published["serviceId"],"resourceUri":published["serviceUri"],"metadataServiceId":mapping["serviceId"],"sourceId":mapping["serviceId"],
        "sourceKind":"MetadataV2Graph","targetKind":"FeatureService","layerIds":[0],"servingLayerIds":[0],"featureServerEnabled":True,"serving":True,"status":"Active"}
    query = {"serviceId":mapping["serviceId"],"layerId":0,"returnedCount":2,"exceededTransferLimit":False,
        "geojson":{"type":"FeatureCollection","features":[{"type":"Feature","id":row["id"],"geometry":row["geometry"],"properties":{"id":row["id"],"attributes":row["properties"]}} for row in completed["rows"]]},
        "features":[{"id":row["id"],"geometry":row["geometry"],"attributes":{"id":row["id"],"attributes":row["properties"]}} for row in completed["rows"]]}
    query_result = {"isError":False,"structuredContent":query,"content":[{"type":"text","text":"Returned 2 authored features"}]}
    query_args = {"serviceId":published["serviceId"],"layerId":0,"where":"1=1","outFields":["id","attributes"],"outSrid":4326,"returnGeometry":True,"limit":20}
    denial = {"status":"error","code":"unauthenticated","message":"Controlled anonymous publication denied","requiresReauthentication":True,
        "error":{"kind":"AuthorizationDenied","message":"Controlled anonymous publication denied"}}
    denied_resource = {"error":{"code":-32000,"message":"Controlled anonymous resource denial","data":{"code":"unauthenticated","requiresReauthentication":True}}}
    def read(identifier,selected,output,auth=True):
        return {"id":identifier,"method":"resources/read","params":{"uri":selected},"authenticated":auth,"result":output}
    def tool(identifier,args,output,auth=True,name="honua_publish_result"):
        return {"id":identifier,"method":"tools/call","params":{"name":name,"arguments":args},"authenticated":auth,"result":output}
    calls = [read("worker-job",uri,resource(uri,worker)),read("worker-results",uri+"/results",resource(uri+"/results",result)),
        tool("unknown-artifact",validation.negative_arguments(authored,identity,"unknown-artifact"),envelope({"status":"error","code":"not_found","message":"Controlled missing artifact"},True)),
        tool("unknown-job",validation.negative_arguments(authored,identity,"unknown-job"),envelope({"status":"error","code":"not_found","message":"Controlled missing job"},True)),
        tool("anonymous",validation.negative_arguments(authored,identity,"anonymous"),envelope(denial,True),False),
        tool("publish",validation.publication_arguments(authored,jobid,artifact["artifactId"]),envelope(published)),
        read("fresh-resource",published["serviceUri"],resource(published["serviceUri"],fresh)),tool("fresh-query",query_args,copy.deepcopy(query_result),name="honua_query_features"),
        read("anonymous-resource",published["serviceUri"],denied_resource,False),read("restarted-resource",published["serviceUri"],resource(published["serviceUri"],fresh)),
        tool("restarted-query",query_args,copy.deepcopy(query_result),name="honua_query_features"),read("restarted-worker-results",uri+"/results",resource(uri+"/results",result))]
    names = ["initial","worker-read","unknown-artifact","unknown-job","anonymous","published","readback","anonymous-resource","restarted","restarted-readback"]
    snapshots = {name:{"schema":"honua.mcp-publish-result-state/v1","database":copy.deepcopy(initial if index < 5 else completed),"census":copy.deepcopy(setup["workflow"]["census"])} for index,name in enumerate(names)}
    restarted = runtime(); restarted["startedAt"] = "2026-10-19T00:05:00Z"
    return rebind({"schema":validation.SCHEMA,"nonce":authored["nonce"],"canonicalClient":authored["canonicalClient"],"clientVersion":"1.32.1",
        "candidateSourceSha":SOURCE,"candidateImage":IMAGE,"setup":setup,"calls":calls,"snapshots":snapshots,"runtimeBefore":runtime(),
        "runtimePrePublish":runtime(),"runtimeRestarted":restarted,"runtimeAfter":copy.deepcopy(restarted)})


class PublicationControls(unittest.TestCase):
    def assert_refused(self,value):
        with self.assertRaises((ValueError,TypeError,KeyError,IndexError,AttributeError)):
            validation.judge(FIXTURE,bootstrap(),value)

    def test_complete_authored_worker_and_publication(self):
        proof = validation.judge(FIXTURE,bootstrap(),controls())
        self.assertTrue(proof["genuineWorkerResult"] and proof["geometricResult"] and proof["negativeNonmutation"])

    def test_actual_worker_media_destination_and_committed_receipt(self):
        for target,field,change in (("job","status",4),("result","status",6),("run","triggerKind",1),("run","status",3)):
            value = controls(); value["setup"]["workflow"][target][field] = change; self.assert_refused(value)
        for field,change in (("contentType","application/geo+json"),("artifactId","fabricated-artifact"),("kind",3),("metadata",{})):
            value = controls(); value["setup"]["workflow"]["result"]["artifacts"][0][field] = change; self.assert_refused(value)
        value = controls(); value["setup"]["workflow"]["job"]["committedEffectReferences"] = []; self.assert_refused(value)
        value = controls(); value["setup"]["workflow"]["definition"]["steps"][0]["plan"]["steps"][0]["inputs"]["connectionId"] = "unregistered"; self.assert_refused(value)

    def test_exact_new_records_and_no_prior_record_mutation(self):
        for family in ("jobs","results","workflows","definitions","proposals","operations"):
            value = controls(); value["setup"]["workflow"]["census"][family].append({"id":"unrelated","sha256":"sha256:"+"a"*64}); self.assert_refused(value)
        value = controls(); value["setup"]["workflow"]["census"]["definitions"][0]["sha256"] = "sha256:"+"a"*64; self.assert_refused(value)

    def test_every_refusal_and_read_only_checkpoint_preserves_full_state(self):
        for name in ("worker-read","unknown-artifact","unknown-job","anonymous","readback","anonymous-resource","restarted","restarted-readback"):
            for field in ("database","census"):
                value = controls()
                if field == "database": value["snapshots"][name][field]["graphs"][0]["revision"] += 1; value["snapshots"][name][field]["graphs"][0]["document"]["revision"] += 1
                else: value["snapshots"][name][field]["proposals"].append({"id":"hidden-created-proposal","sha256":"sha256:"+"a"*64})
                self.assert_refused(value)

    def test_positive_preserves_existing_rows_and_coherent_graph(self):
        for family in ("services","layers","serviceLayers","fields","features"):
            value = controls()
            for name in ("published","readback","anonymous-resource","restarted","restarted-readback"):
                value["snapshots"][name]["database"][family].pop(0)
            self.assert_refused(value)
        value = controls()
        for name in ("published","readback","anonymous-resource","restarted","restarted-readback"):
            value["snapshots"][name]["database"]["graphs"][0]["document"]["resources"].pop(0)
        self.assert_refused(value)

    def test_connection_nonstatus_fields_cannot_disappear_during_publication(self):
        value = controls()
        for name in ("initial","worker-read","unknown-artifact","unknown-job","anonymous"):
            value["snapshots"][name]["database"]["graphs"][0]["document"]["connections"][0]["options"] = {"operatorOwned":"preserve"}
        self.assert_refused(value)
        value = controls()
        for name in ("published","readback","anonymous-resource","restarted","restarted-readback"):
            value["snapshots"][name]["database"]["graphs"][0]["document"]["resources"][0]["payload"]["must"] = "changed coherently"
        self.assert_refused(value)

    def test_independent_full_geometry_and_batch_attributes(self):
        for name in ("rows","materialized"):
            for field in ("geometry","properties" if name == "rows" else "attributes"):
                value = controls(); rows = value["snapshots"]["published"]["database"][name]
                if field == "geometry": rows[0][field]["coordinates"][0] += 0.001
                else: rows[0][field]["__pipeline_batch_id"] = "wrong"
                self.assert_refused(value)
        value = controls(); value["snapshots"]["published"]["database"]["materialized"][1]["attributes"]["id"] = 11; self.assert_refused(value)

    def test_physical_public_and_source_canonical_ids_stay_distinct(self):
        value = controls(); self.assertEqual("741",value["calls"][5]["result"]["structuredContent"]["layerId"])
        self.assertEqual(0,value["calls"][7]["params"]["arguments"]["layerId"])
        self.assertEqual(881,value["snapshots"]["published"]["database"]["materialized"][0]["id"])
        self.assertEqual(11,value["calls"][7]["result"]["structuredContent"]["features"][0]["id"])
        validation.judge(FIXTURE,bootstrap(),value)
        value["calls"][7]["params"]["arguments"]["layerId"] = 741; self.assert_refused(rebind(value))
        value = controls(); output = value["calls"][7]["result"]["structuredContent"]; output["features"][0]["id"] = 881; self.assert_refused(rebind(value))

    def test_runtime_allows_prior_bootstrap_restart_but_not_current_setup_drift(self):
        value = controls()
        for key in ("runtimeBefore","runtimePrePublish"): value[key]["startedAt"] = "2026-10-19T00:03:00Z"
        for key in ("runtimeBefore","runtimeAfter"): value["setup"][key]["startedAt"] = "2026-10-19T00:03:00Z"
        self.assertTrue(validation.judge(FIXTURE,bootstrap(),value)["restartReadback"])
        for target in ("runtimeBefore","runtimeAfter"):
            changed = controls(); changed["setup"][target]["startedAt"] = "2026-10-19T00:03:00Z"; self.assert_refused(changed)
        value = controls(); value["runtimePrePublish"]["startedAt"] = "2026-10-19T00:03:00Z"; self.assert_refused(value)
        value = controls(); value["runtimeRestarted"] = value["runtimeAfter"] = runtime(); self.assert_refused(value)

    def test_anonymous_actual_invocation_credentials_sessions_and_errors(self):
        for request in ("initialize","invocation"):
            value = controls(); value["calls"][4]["wire"][request]["request"]["credentialPresent"] = True; self.assert_refused(value)
        value = controls(); value["calls"][4]["wire"]["invocation"]["request"]["sessionSha256"] = value["calls"][0]["wire"]["invocation"]["request"]["sessionSha256"]; self.assert_refused(value)
        for field,change in (("code","not_found"),("requiresReauthentication",False),("serviceId","hidden-created")):
            value = controls(); output = value["calls"][4]["result"]["structuredContent"]; output[field] = change; value["calls"][4]["result"] = envelope(output,True); self.assert_refused(rebind(value))

    def test_pending_denied_failed_and_unrelated_publication_never_completed(self):
        for field,change in (("status","Queued"),("status","RequiresApproval"),("status","Failed"),("status","Denied"),("sourceJobId","other"),("artifactId","other"),("metadataRevision",True),("requiresApproval",True),("serviceUri","honua://published-services/other")):
            value = controls(); output = value["calls"][5]["result"]["structuredContent"]; output[field] = change; value["calls"][5]["result"] = envelope(output); self.assert_refused(rebind(value))

    def test_source_query_jsonb_roundtrip_and_changed_second_representation(self):
        value = controls()
        for index in (7,10):
            output = value["calls"][index]["result"]["structuredContent"]
            for row in output["features"]: row["attributes"]["attributes"] = json.dumps(row["attributes"]["attributes"])
            for row in output["geojson"]["features"]: row["properties"]["attributes"] = json.dumps(row["properties"]["attributes"])
        self.assertTrue(validation.judge(FIXTURE,bootstrap(),rebind(value))["persistedReadback"])
        value["calls"][7]["result"]["structuredContent"]["features"][0]["attributes"]["attributes"] = "{}"; self.assert_refused(rebind(value))

    def test_setup_credentials_never_retained_direct_escaped_or_url_encoded(self):
        for payload in ('{"body":"controlled-private-key"}','{"body":"controlled\\u002dprivate-key"}','{"links":[{"href":"http://owned/%63ontrolled%2Dprivate%2Dkey"}]}','{"controlled%2Dprivate%2Dkey":"value"}'):
            with self.assertRaises(ValueError): validation.safe_response(payload,["controlled-private-key"])
        self.assertEqual({"public":"safe"},validation.safe_response('{"public":"safe"}',["controlled-private-key"]))

    def test_ambiguous_mapping_revision_and_source_destination(self):
        for name in ("duplicate","revision","registry","target"):
            value = controls(); database = value["snapshots"]["published"]["database"]
            if name == "duplicate": database["mappings"].append(copy.deepcopy(database["mappings"][0]))
            elif name == "revision": database["graphs"][0]["revision"] = 41; database["graphs"][0]["document"]["revision"] = 41; database["mappings"][0]["revision"] = 41
            elif name == "registry": database["connection"]["registry"].append(copy.deepcopy(database["connection"]["registry"][0]))
            else: database["connection"]["target"]["clusterId"] = "foreign"
            self.assert_refused(value)

    def test_state_malformed_steps_fail_closed_and_raw_record_multiplicity(self):
        value = controls()["setup"]["workflow"]
        def rows(run):
            return [[b"workflows",run["runId"].encode(),json.dumps(run).encode()],[b"definitions",value["definition"]["workflowId"].encode(),json.dumps(value["definition"]).encode()]]
        for malformed in (None,[],[None],[1],["sink"],[{}]):
            run = copy.deepcopy(value["run"]); run["stepStates"] = malformed
            with self.assertRaises(ValueError): state.workflow_state(run["runId"],value["definition"]["workflowId"],execute=lambda _:rows(run))
        raw = rows(value["run"])
        with self.assertRaises(ValueError): state.read_state(execute=lambda _:raw+raw)
        with self.assertRaises(ValueError): state.read_state(execute=lambda _:[[b"workflows",b"wf-bad",b'{}']])


if __name__ == "__main__":
    unittest.main()
