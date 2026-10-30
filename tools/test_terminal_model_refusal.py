"""Redis-off producer contracts, never a live model qualification receipt."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import terminal_model_canary as c
import terminal_model_refusal as r
from test_terminal_model_canary import _green_deterministic_receipt, _endpoint, MANIFEST, JOURNEY, PROTOCOL, LOCK

sys.path.insert(0, str(c.DEFAULT_DRIVER.parent))
import refusal_profile as profile
import executor
import stages
from transport import ExecutionError


def _builder():
    return r.RefusalReceiptBuilder(manifest=c.load_manifest(MANIFEST), protocol=c._load_json(PROTOCOL),
        protocol_path=PROTOCOL, endpoint=_endpoint(), driver_command=c.DEFAULT_DRIVER,
        deterministic_receipt={"path": "receipt.json", "sha256": "a"*64, "generatedAt": c._now(),
            "status": "pass", "prerequisiteProfile": r.PROFILE, "sourceStatus": "blocked",
            "candidateVerified": True, "freshnessVerified": True},
        redactor=c.Redactor([]), lock_digest=LOCK, cell="aws-ecs/redis-off", attempt=1)


def _prerequisite(tmp_path, cell="aws-ecs/redis-off"):
    receipt = _green_deterministic_receipt()
    receipt["status"] = "blocked"
    for s in receipt["stages"]:
        s["evidence"]["source"] = "live-" + cell.split("/")[0]
        if s["number"] not in (1,2,5):
            s["status"] = "blocked"
    receipt["stages"][4]["topologyRefusal"] = {"topology":"redis-off", "code":"unavailable",
        "missingDependency":"redis", "manifestCapability":"jobs.runner",
        "manifestReasonCode":"dependency-unavailable", "rulingStatus":"ruled", "tool":"honua_validate_plan"}
    if cell.startswith("aws-serverless"):
        server = c.load_manifest(MANIFEST)["components"]["honua-server"]
        receipt["server"]["image"] = f"{server['awsLambdaImage']}@{server['awsLambdaDigest']}"
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    return path, receipt


def validate(path, root, cell="aws-ecs/redis-off", **kw):
    return c.validate_deterministic_receipt(path, manifest=c.load_manifest(MANIFEST), journey=c.load_journey(JOURNEY),
                                          repo_root=root, cell=cell, prerequisite_profile=r.PROFILE, **kw)


@pytest.mark.parametrize("cell", sorted(r.CELLS))
def test_explicit_bounded_prerequisite_retains_raw_blocked_status_without_green_full_journey(tmp_path,cell):
    path, _ = _prerequisite(tmp_path,cell)
    proof = validate(path,tmp_path,cell)
    assert (proof["prerequisiteProfile"], proof["sourceStatus"]) == (r.PROFILE,"blocked")
    with pytest.raises(c.CanaryError,match="not green"):
        c.validate_deterministic_receipt(path, manifest=c.load_manifest(MANIFEST),journey=c.load_journey(JOURNEY),
                                        repo_root=tmp_path,cell=cell)


@pytest.mark.parametrize("mutation", ["roster","fail","serving","source","freshness","missing","dependency","capability","manifest","tool","job","candidate","image","client","old","future","on"])
def test_bounded_prerequisite_negative_controls(tmp_path,mutation):
    path,x = _prerequisite(tmp_path)
    stage=x["stages"][4]; refusal=stage["topologyRefusal"]
    if mutation=="roster":x["roster"]["status"]="blocked"
    if mutation=="fail":x["stages"][7]["status"]="fail"
    if mutation=="serving":x["stages"][1]["status"]="blocked"
    if mutation=="source":stage["evidence"]["source"]="build"
    if mutation=="freshness":stage["evidence"]["freshness"]="stale"
    if mutation=="missing":stage.pop("topologyRefusal")
    if mutation=="dependency":refusal["missingDependency"]="postgres"
    if mutation=="capability":refusal["manifestCapability"]="operations.proposals"
    if mutation=="manifest":refusal["manifestReasonCode"]="available"
    if mutation=="tool":refusal["tool"]="honua_query_features"
    if mutation=="job":stage["jobId"]="job-accepted"
    if mutation=="candidate":x["server"]["sourceSha"]="f"*40
    if mutation=="image":x["server"]["image"]="wrong@sha256:"+"f"*64
    if mutation=="client":x["clientArtifacts"]["honua-sdk-js"]["version"]="0.0.0"
    if mutation=="old":x["generatedAt"]="2020-01-01T00:00:00Z"
    if mutation=="future":x["generatedAt"]="2099-01-01T00:00:00Z"
    path.write_text(json.dumps(x))
    with pytest.raises(c.CanaryError):validate(path,tmp_path,"aws-ecs/redis-on" if mutation=="on" else "aws-ecs/redis-off")


@pytest.mark.parametrize("cell", sorted(r.CELLS))
def test_cell_specific_lambda_and_ecs_image_pins_are_not_interchangeable(tmp_path,cell):
    path,x = _prerequisite(tmp_path,cell)
    server=c.load_manifest(MANIFEST)["components"]["honua-server"]
    other=("image","digest") if cell.startswith("aws-serverless") else ("awsLambdaImage","awsLambdaDigest")
    x["server"]["image"]=f"{server[other[0]]}@{server[other[1]]}"
    path.write_text(json.dumps(x))
    with pytest.raises(c.CanaryError,match="candidate pins"):validate(path,tmp_path,cell)


def _seed_manifest():
    return {"slice1":{"e2e_src_fs":{"service":"e2e","layerId":9}},"layers":{"e2e_src_fs":9}}


def _fixture():
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/"seed-manifest.json";path.write_text(json.dumps(_seed_manifest()))
        return profile.fixture_binding(path)


def _features():
    return {"type":"FeatureCollection","features":[{"type":"Feature","id":int(f["id"]),
            "properties":{"name":f["name"]},"geometry":f["geometry"]} for f in _fixture()["expectedFeatures"]]}


@pytest.mark.parametrize("mutation", [None,"empty","geometry","properties","identity","duplicate","count"])
def test_serving_read_requires_independent_geometry_properties_and_identity(mutation):
    actual,reference=_features(),_features()
    if mutation=="empty":actual["features"]=[]
    if mutation=="geometry":actual["features"][0]["geometry"]["coordinates"]=[9,9]
    if mutation=="properties":actual["features"][0]["properties"]["name"]="invented"
    if mutation=="identity":actual["features"][0].pop("id")
    if mutation=="duplicate":actual["features"]*=2;reference["features"]*=2
    if mutation=="count":reference["features"]*=2
    if mutation:
        with pytest.raises(ExecutionError):profile.serving_proof({"geoJson":actual},reference,_fixture())
    else:
        proof=profile.serving_proof({"geoJson":actual},reference,_fixture())
        assert proof["featureCount"]==2 and proof["independentReadMatches"] is True


def _driver(*,cell="aws-ecs/redis-off",problem=None,available=False):
    state={"workspaceId":"w","stackUp":True,"servingFixtureBinding":_fixture()}
    target={"id":cell,"kind":"aws-ecs", "execution":{"buffer":{"point":[1,2],"srid":4326,"distance":10}}}
    observation=stages.Observation(ready=True,setup_view_present=True,proxy_available=True,
        setup_discovery={"metadata":{"view":"bounded"},"tools":[{"name":name,"inputSchema":{"type":"object"}}
            for name in {"honua_validate_plan","honua_execute_plan","honua_list_capabilities"}]})
    transport=mock.Mock(credentials={"operator":"operator","proposer":"proposer"})
    transport.get_json.return_value={"capabilities":[{"id":"jobs.runner","available":available,
                                                    "reasonCode":"dependency-unavailable"}]}
    transport.tool.return_value=({"isError":True,"structuredContent":problem} if problem else
                                 {"structuredContent":{"accepted":True}})
    engine=executor.JourneyExecutor(state,target,observation,transport)
    driver=SimpleNamespace(_rehydrate=lambda _: (state,target,{},observation,None),
        _executor=lambda *_:engine,_write_state=mock.Mock())
    return driver,engine,state


def _plan_arguments():
    return {"plan":{"planId":"model-buffer","intentId":"model-intent","outputs":["FeatureLayer"],
            "steps":[{"stepId":"buffer", "kind":"Geoprocess","processId":"geometry.buffer",
                "inputs":{"wkb":executor.oracles.point_wkb(1,2),"srid":"4326","distance":"10","geodesic":"false"}}]}}


GP={"code":"unavailable","missingDependency":"redis","capability":"jobs.runner"}


@pytest.mark.parametrize("mutation", [None,"on","other-dependency","available","other-capability","job","resource-uri","created-at","encoded-uri"])
def test_actual_tool_refusal_must_match_independent_live_dependency_manifest(mutation):
    problem=dict(GP)
    if mutation=="other-dependency":problem["missingDependency"]="postgres"
    if mutation=="other-capability":problem["capability"]="operations.proposals"
    if mutation=="job":problem["jobId"]="accepted"
    if mutation=="resource-uri":problem["resourceUri"]="honua://jobs/accepted"
    if mutation=="created-at":problem["jobCreatedAt"]="2026-10-17T10:00:00Z"
    if mutation=="encoded-uri":problem["encoded"]=json.dumps({"nested":{"resourceUri":"honua://jobs/accepted"}})
    driver,engine,state=_driver(cell="aws-ecs/redis-on" if mutation=="on" else "aws-ecs/redis-off",
                               problem=problem,available=mutation=="available")
    request={"profile":r.PROFILE,"stage":"durable-refusal","action":{"kind":"tool_call","tool":"honua_validate_plan","arguments":_plan_arguments()}}
    if mutation:
        with pytest.raises(ExecutionError):profile.execute(driver,request)
    else:
        result=profile.execute(driver,request)["result"]
        assert result["executionOutcome"]=="refused" and result["functionalGpExecuted"] is False
        engine.transport.get_json.assert_called_once()
        assert state["modelRefusal"]["acknowledged"] is False
        engine.transport.tool.return_value={"structuredContent":{"available":False}}
        request["action"]={"kind":"tool_call","tool":"honua_list_capabilities","arguments":{}}
        with pytest.raises(ExecutionError,match="acknowledge"):profile.execute(driver,request)
        request["action"]["assessment"]={"executionOutcome":"refused","missingDependency":"redis","functionalGpExecuted":False}
        assert profile.execute(driver,request)["result"]["modelAcknowledgedRefusal"] is True


def test_unexpected_durable_acceptance_is_explicit_not_a_false_refusal():
    driver,_,state=_driver()
    result=profile.execute(driver,{"profile":r.PROFILE,"stage":"durable-refusal", "action":{
        "kind":"tool_call","tool":"honua_execute_plan","arguments":_plan_arguments()}})["result"]
    assert result=={"accepted":True,"executionOutcome":"unexpectedly-accepted","functionalGpExecuted":None}
    assert state["modelRefusal"]["refusal"] is None


@pytest.mark.parametrize("cell",["local-docker","aws-ecs/redis-on","aws-eks/redis-off"])
def test_profile_rejects_other_topologies_and_venues(cell):
    b=_builder()
    with pytest.raises(c.CanaryError):
        r.RefusalReceiptBuilder(manifest=c.load_manifest(MANIFEST),protocol=b.protocol,protocol_path=PROTOCOL,
            endpoint=_endpoint(),driver_command=None,deterministic_receipt=None,redactor=c.Redactor([]),cell=cell)


def test_unexecuted_refusal_receipt_is_schema_valid_and_never_pass():
    b=_builder();b.mark_blocked("no live candidate execution")
    receipt=b.validated_receipt()
    assert receipt["profile"]==r.PROFILE and receipt["executionOutcome"]=="not-attempted"
    assert receipt["functionalGpExecuted"] is False and receipt["status"]=="blocked"
    assert "errorInjection" not in receipt


@pytest.mark.parametrize("mutation", ["empty","wrong-point","wrong-process","wrong-distance","extra-step","empty-id","wrong-output"])
def test_preparse_refusal_cannot_certify_an_invalid_or_unexecuted_plan(mutation):
    args=_plan_arguments();plan=args["plan"]
    if mutation=="empty":args={}
    if mutation=="wrong-point":plan["steps"][0]["inputs"]["wkb"]=executor.oracles.point_wkb(8,9)
    if mutation=="wrong-process":plan["steps"][0]["processId"]="geometry.erase"
    if mutation=="wrong-distance":plan["steps"][0]["inputs"]["distance"]="-1"
    if mutation=="extra-step":plan["steps"]*=2
    if mutation=="empty-id":plan["planId"]=""
    if mutation=="wrong-output":plan["outputs"]=["fake-completed"]
    driver,engine,_=_driver(problem=GP)
    with pytest.raises(ExecutionError,match="valid|authored"):
        profile.execute(driver,{"profile":r.PROFILE,"stage":"durable-refusal","action":{
            "kind":"tool_call","tool":"honua_validate_plan","arguments":args}})
    engine.transport.tool.assert_not_called()


def _runner(tmp_path,monkeypatch,*,mutation=None):
    """State-machine test only. Fake responses here cannot qualify a candidate."""
    b=_builder();endpoint=_endpoint()
    target=tmp_path/"target.json";target.write_text(json.dumps({"id":"aws-ecs/redis-off","replicaBaseUrl":endpoint.base_url}))
    receipt=tmp_path/"receipt.json";receipt.write_text(json.dumps({"target":{"id":"aws-ecs/redis-off","configSha256":c._sha256(target)}}))
    b.receipt["linkedEvidence"]["deterministicReceipt"]["path"]=str(receipt)
    (tmp_path/"extended").mkdir()
    (tmp_path/"extended/seed-manifest.json").write_text(json.dumps(_seed_manifest()))
    state={"serving":False,"refused":False,"ack":False,"calls":0,"teardown":False,"bindings":[],"admission":False,
           "messages":[]}
    serving={"featureCount":2,"featureIds":["1","2"],"featuresSha256":"a"*64,"independentReadMatches":True,
             "fixtureBinding":{k:v for k,v in _fixture().items() if k!="expectedFeatures"}}
    refused={"topology":"redis-off","ruling":"redis-off","rulingStatus":"ruled","tool":"honua_validate_plan",
             **GP,"manifestCapability":"jobs.runner","manifestReasonCode":"dependency-unavailable"}
    class Driver:
        def __init__(self,*_):pass
        def invoke(self,op,payload):
            if op=="setup":
                return {"status":"ready","workspaceId":"unit-only","baseUrl":endpoint.base_url,
                        "toolView":{"bounded":True},"credentialReferences":[{"envVar":"UNIT_ONLY"}]}
            if op=="observe":
                stage=next(s for s in b.journey["stages"] if s["id"]==payload["stage"])
                done=state["serving"] if stage["number"]==1 else state["ack"]
                tools=sorted(profile.STAGE_TOOLS[stage["number"]])
                if mutation=="no-stage-tools":tools=[]
                return {"status":"pass" if done else "ready","stageStatus":{**stage,"status":"pass" if done else "ready",
                        "checks":[{"status":"pass"}] if done else [],"blockedBy":[]},"observation":{},
                        "toolView":{"tools":[{"name":n} for n in tools+["honua_list_jobs"]]},"stageTools":tools}
            if op=="execute":
                if payload["stage"]=="serving-read":state["serving"]=True;result={"servingProof":serving}
                elif not state["refused"]:
                    state["refused"]=True
                    result=({"executionOutcome":"unexpectedly-accepted","functionalGpExecuted":None} if mutation=="accepted"
                            else {"typedRefusal":refused,"executionOutcome":"refused","functionalGpExecuted":False})
                else:state["ack"]=True;result={"modelAcknowledgedRefusal":True}
                return {"status":"pass","result":result}
            if op=="verify":
                assertions=dict.fromkeys(r.ASSERTIONS[:-1],"pass")
                if mutation in assertions:assertions[mutation]="fail"
                return {"status":"pass","executionOutcome":"refused","functionalGpExecuted":False,
                        "assertions":assertions,"servingProof":serving,"typedRefusal":refused}
            assert op=="teardown";state["teardown"]=True
            return {"status":"fail" if mutation=="teardown" else "complete"}
    class Model:
        def __init__(self,*_):pass
        def ensure_admission(self):
            assert state["calls"]==0;state["admission"]=True
            if mutation=="admission":raise c.CanaryError("candidate AI admission admin.ai.admission.initialize failed")
            return [{"operation":"admin.ai.admission.initialize","status":"Completed"},
                    {"operation":"admin.ai.admission.resume","status":"Completed"}]
        def complete(self,messages,bindings):
            assert state["admission"],"a chat was sent before the AI admission ledger was opened"
            state["calls"]+=1;state["bindings"].append(bindings);state["messages"].append(copy.deepcopy(messages))
            if mutation=="unsigned" and state["calls"]==2:raise c.CanaryError("signed provenance verification failed")
            proof={"transcriptDigest":("a"*64 if mutation=="replay" else f"{state['calls']:064x}"),
                "keyId":"unit","manifestDigest":"a"*64,"provider":"bedrock","reportedModel":"unit-only",
                "verifierVerdict":"pass","replayVerdict":"consumed","bindings":bindings}
            return json.dumps({"kind":"tool_call","tool":"unit-only","arguments":{}}),{},1,proof
    monkeypatch.setattr(c,"DriverAdapter",Driver);monkeypatch.setattr(c,"CandidateProxyClient",Model)
    result=r.execute_live(b,endpoint=endpoint,driver_command=c.DEFAULT_DRIVER,max_actions_per_stage=2,target_path=target)
    return b,result,state


def test_separate_runner_counts_three_distinct_signed_actions_and_never_claims_gp(tmp_path,monkeypatch):
    b,result,state=_runner(tmp_path,monkeypatch)
    assert result["status"]=="pass" and result["executionOutcome"]=="refused" and result["functionalGpExecuted"] is False
    assert state["calls"]==3 and state["teardown"]
    assert len({x["runNonce"] for x in state["bindings"]})==3
    assert result["servingProof"]["modelActionSequence"]<result["refusalProof"]["modelActionSequence"]<result["refusalProof"]["acknowledgementActionSequence"]
    assert b.validated_receipt()["profile"]==r.PROFILE


def test_runner_opens_the_ai_admission_ledger_before_the_first_chat(tmp_path,monkeypatch):
    # Run 40968671034 (aws-ecs/redis-off): without this the first chat was refused 503
    # "AI invocation admission is unavailable or denied" before any model call.
    b,result,state=_runner(tmp_path,monkeypatch)
    assert state["admission"] and result["status"]=="pass"
    setup=[a for a in result["actions"] if a["kind"]=="setup"]
    assert [a["stageId"] for a in setup]==["serving-read","serving-read"] and all(a["status"]=="pass" for a in setup)
    assert setup[1]["sequence"]<min(a["sequence"] for a in result["actions"] if a["attribution"]==c.MODEL_SELECTED)


def test_runner_failing_to_open_ai_admission_is_infrastructure_with_no_model_call(tmp_path,monkeypatch):
    _,result,state=_runner(tmp_path,monkeypatch,mutation="admission")
    assert result["status"]=="fail" and result["failureAttribution"]==c.INFRASTRUCTURE
    assert state["calls"]==0 and state["teardown"] and result["totals"]["modelCalls"]==0
    assert any("AI admission" in n for n in result["notices"])


@pytest.mark.parametrize("mutation",["accepted","unsigned","replay","teardown",*r.ASSERTIONS[:-1]])
def test_runner_fails_on_acceptance_unsigned_replay_cleanup_or_any_independent_control(tmp_path,monkeypatch,mutation):
    _,result,state=_runner(tmp_path,monkeypatch,mutation=mutation)
    assert result["status"]=="fail" and state["teardown"]
    if mutation=="accepted":assert result["executionOutcome"]=="unexpectedly-accepted" and result["functionalGpExecuted"] is None


@pytest.mark.parametrize("mutation",["absent","full-profile","completed-gp","true-gp","string-false"])
def test_report_and_promotion_reject_false_redis_off_outcomes(tmp_path,mutation):
    import model_journey_report
    from check_promotion_readiness import _journey
    receipt={"mode":"genuine-model","cell":"aws-ecs/redis-off","attempt":1,"lockDigest":LOCK,
        "status":"pass","failureAttribution":None,"completedAt":"2026-10-17T10:00:00Z",
        "profile":r.PROFILE,"executionOutcome":"refused","functionalGpExecuted":False}
    if mutation=="absent":receipt.pop("profile")
    if mutation=="full-profile":receipt["profile"]="full-workflow"
    if mutation=="completed-gp":receipt["executionOutcome"]="completed"
    if mutation=="true-gp":receipt["functionalGpExecuted"]=True
    if mutation=="string-false":receipt["functionalGpExecuted"]="false"
    path=tmp_path/"attempt.json";path.write_text(json.dumps(receipt))
    report=model_journey_report.build([path],cell=receipt["cell"],lock_digest=LOCK,candidate_digest="a"*64,run_id="1",run_attempt="1")
    assert report["status"]=="fail"
    ledger={"cells":[{"cell":receipt["cell"],"mode":"genuine-model","attemptCount":1,
             "attempts":[{**receipt,"attempt":1}]}]}
    assert not _journey(ledger,frozenset({receipt["cell"]}),"genuine-model",LOCK,
                        datetime(2026,10,17,tzinfo=timezone.utc),datetime(2026,10,18,tzinfo=timezone.utc))


@pytest.mark.parametrize("mutation",[None,"authentication","revision","catalog","anonymous","viewer","unknown-profile"])
def test_refusal_verify_checks_real_permission_boundaries_and_exact_catalog(mutation):
    driver,engine,state=_driver(problem=GP)
    state["modelRefusal"]={"serving":{"independentReadMatches":True},"refusal":GP,
        "acknowledged":True,"executionOutcome":"refused"}
    observation=engine.observation
    observation.credential_probe=SimpleNamespace(status="fail" if mutation=="authentication" else "pass")
    observation.capability_manifest={"server":{"deploymentRevision":"wrong" if mutation=="revision" else "a"*40}}
    observation.setup_discovery.update(http={"status":"pass","fullCatalogComparisonSha256":"b"*64},
        proxy={"status":"pass","fullCatalogComparisonSha256":"c"*64 if mutation=="catalog" else "b"*64})
    driver._rehydrate=lambda _: (state,engine.target,{"components":{"honua-server":{"sha":"a"*40}}},observation,None)
    def http(method,path,*,principal,expected):
        actual=200 if principal=="operator" or (mutation=="anonymous" and principal is None) or (mutation=="viewer" and principal=="viewer") else 401 if principal is None else 403
        if actual not in expected:raise ExecutionError("permission oracle","candidate did not enforce required denial")
    engine.transport.http.side_effect=http
    request={"profile":"unknown" if mutation=="unknown-profile" else r.PROFILE}
    if mutation in {"anonymous","viewer","unknown-profile"}:
        with pytest.raises(ExecutionError):profile.verify(driver,request)
    else:
        result=profile.verify(driver,request)
        assert result["status"]==("pass" if mutation is None else "fail")
    if mutation!="unknown-profile":
        assert engine.transport.http.call_args_list[0].kwargs=={"principal":"operator","expected":(200,)}


@pytest.mark.parametrize("mutation",["filter","url","offset","no-geometry","fields","srid","limit"])
def test_independent_serving_oracle_refuses_unreviewed_query_variants(mutation):
    args={"serviceId":"published","layerId":1}
    if mutation=="filter":args["where"]="1=0"
    if mutation=="url":args["serviceId"]=""  # Service identifiers cannot become a caller-selected host.
    if mutation=="offset":args["resultOffset"]=1
    if mutation=="no-geometry":args["returnGeometry"]=False
    if mutation=="fields":args["outFields"]=["secret"]
    if mutation=="srid":args["outSrid"]=3857
    if mutation=="limit":args["limit"]=1000
    transport=mock.Mock()
    with pytest.raises(ExecutionError):profile._reference_features(transport,args)
    transport.opener.open.assert_not_called()


def test_independent_serving_oracle_uses_same_fixed_candidate_and_encoded_service_path():
    transport=mock.Mock(base_url="https://candidate.example",opener=mock.MagicMock())
    transport.credential.return_value="private-application-key"
    response=mock.MagicMock(status=200);response.read.return_value=json.dumps(_features()).encode()
    transport.opener.open.return_value.__enter__.return_value=response
    assert profile._reference_features(transport,{"serviceId":"https://other.example/x","layerId":1})==_features()
    request=transport.opener.open.call_args.args[0]
    assert request.full_url.startswith("https://candidate.example/rest/services/https%3A%2F%2Fother.example%2Fx/FeatureServer/1/query?")
    assert request.get_header("X-api-key")=="private-application-key"


def test_fixture_binding_uses_retained_allocated_ids_and_maintained_seed_bytes(tmp_path):
    path=tmp_path/"seed-manifest.json";path.write_text(json.dumps(_seed_manifest()))
    fixture=profile.fixture_binding(path)
    assert fixture["serviceId"]=="e2e" and fixture["layerId"]==9
    assert [(f["id"],f["name"],f["geometry"]["coordinates"]) for f in fixture["expectedFeatures"]]==[
        ("1","alpha",[-156.33,20.75]),("2","bravo",[-156.45,20.88])]
    for key in ("seedManifestSha256","seedScriptSha256","seedSqlSha256","expectedFeaturesSha256"):
        assert len(fixture[key])==64
    for patch in ({"slice1":{"e2e_src_fs":{"service":"other","layerId":9}}},{"layers":{"e2e_src_fs":8}}):
        path.write_text(json.dumps({**_seed_manifest(),**patch}))
        with pytest.raises(ExecutionError):profile.fixture_binding(path)


@pytest.mark.parametrize("mutation",["name","geometry","id","extra","empty"])
def test_two_equal_adapters_cannot_qualify_the_wrong_authored_fixture(mutation):
    actual=_features()
    if mutation=="name":actual["features"][0]["properties"]["name"]="irrelevant"
    if mutation=="geometry":actual["features"][0]["geometry"]["coordinates"]=[0,0]
    if mutation=="id":actual["features"][0]["id"]=100
    if mutation=="extra":actual["features"].append(copy.deepcopy(actual["features"][0]))
    if mutation=="empty":actual["features"]=[]
    with pytest.raises(ExecutionError):profile.serving_proof({"geoJson":actual},copy.deepcopy(actual),_fixture())


@pytest.mark.parametrize("mutation",["service","layer"])
def test_model_selected_query_cannot_qualify_an_unrelated_publication(mutation):
    driver,engine,_=_driver()
    engine.observation.setup_discovery["tools"].append({"name":"honua_query_features","inputSchema":{"type":"object"}})
    engine.transport.tool.return_value={"structuredContent":{"geoJson":_features()}}
    args={"serviceId":"e2e","layerId":9}
    args["serviceId" if mutation=="service" else "layerId"]="other" if mutation=="service" else 123
    with pytest.raises(ExecutionError,match="another service or layer"):
        profile.execute(driver,{"profile":r.PROFILE,"stage":"serving-read","action":{
            "kind":"tool_call","tool":"honua_query_features","arguments":args}})
    engine.transport.opener.open.assert_not_called()


def test_driver_rejects_a_retained_seed_manifest_changed_after_setup(tmp_path,monkeypatch):
    import live_driver
    target=tmp_path/"target.json";target.write_text("{}")
    fixture=tmp_path/"seed-manifest.json";fixture.write_text(json.dumps(_seed_manifest()))
    binding=profile.fixture_binding(fixture)
    monkeypatch.setattr(live_driver,"STATE_ROOT",tmp_path/"sessions")
    live_driver._write_state("unit",{"workspaceId":"unit","targetPath":str(target),
        "servingFixturePath":str(fixture),"servingFixtureBinding":binding})
    fixture.write_text(json.dumps({**_seed_manifest(),"changed":True}))
    with pytest.raises(live_driver.DriverError,match="serving fixture"):
        live_driver._rehydrate({"workspaceId":"unit"})


@pytest.mark.parametrize("cell", sorted(r.CELLS))
def test_setup_prerequisite_does_not_require_raw_gp_execution_before_actual_profile(tmp_path, cell):
    path, receipt = _prerequisite(tmp_path, cell)
    receipt['stages'][4].update(status='blocked')
    receipt['stages'][4].pop('topologyRefusal')
    path.write_text(json.dumps(receipt))
    before = path.read_bytes()
    proof = validate(path, tmp_path, cell)
    assert proof['status'] == 'pass' and proof['sourceStatus'] == 'blocked'
    assert path.read_bytes() == before
    # This is a deployment prerequisite only; it never qualifies the full journey or GP.
    with pytest.raises(c.CanaryError, match='not green'):
        c.validate_deterministic_receipt(path, manifest=c.load_manifest(MANIFEST),
            journey=c.load_journey(JOURNEY), repo_root=tmp_path, cell=cell)


@pytest.mark.parametrize("field", ["jobId", "resourceUri", "jobCreatedAt", "operationId"])
def test_blocked_raw_gp_stage_cannot_hide_execution_behind_bounded_prerequisite(tmp_path, field):
    path, receipt = _prerequisite(tmp_path)
    receipt['stages'][4].update(status='blocked', **{field: 'actually-executed'})
    path.write_text(json.dumps(receipt))
    with pytest.raises(c.CanaryError, match='contradictory GP execution'):
        validate(path, tmp_path)


def test_optional_nonobject_raw_stage_is_a_typed_prerequisite_rejection(tmp_path):
    path, receipt = _prerequisite(tmp_path)
    receipt['stages'][2] = None; path.write_text(json.dumps(receipt))
    with pytest.raises(c.CanaryError, match='not complete and current'):
        validate(path, tmp_path)


@pytest.mark.parametrize("wire", ["jsonrpc", "jsonrpc-sibling", "mcp"])
@pytest.mark.parametrize("extra", [None, {"resourceUri": "honua://jobs/private-job"},
    {"jobCreatedAt": "2026-10-17T10:00:00Z"},
    {"encoded": json.dumps({"nested": {"resourceUri": "honua://jobs/private-job"}})}])
def test_actual_proxy_error_wire_cannot_mask_execution_into_refusal(tmp_path, monkeypatch, wire, extra):
    import probes
    from transport import Transport
    driver, engine, state = _driver(problem=GP)
    view = engine.view(); secret = 'private-application-key'
    problem = {**GP, **(extra or {})}
    response = ({"error": {"code": -32603, "message": 'refused for ' + secret, "data": problem}}
                if wire.startswith('jsonrpc') else {"result": {"isError": True, "structuredContent": problem}})
    if wire == 'jsonrpc-sibling' and extra:
        response['error']['data'] = dict(GP)
        response['result'] = extra
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def initialize(self, **kwargs): return {}
        def request(self, method, params):
            return {"result": {"_meta": view['metadata'], "tools": view['tools']}} if method == 'tools/list' else response
    monkeypatch.setattr(probes, 'McpProxySession', Session)
    proxy = tmp_path / 'installed-proxy'; proxy.write_text('')
    actual = Transport('http://127.0.0.1:8123', proxy, None, tmp_path, {'operator': secret, 'proposer': secret})
    actual.get_json = mock.Mock(return_value={'capabilities': [{'id': 'jobs.runner', 'available': False,
                                                             'reasonCode': 'dependency-unavailable'}]})
    engine.transport = actual
    request = {'profile': r.PROFILE, 'stage': 'durable-refusal', 'action': {'kind': 'tool_call',
        'tool': 'honua_execute_plan', 'arguments': _plan_arguments()}}
    if extra:
        with pytest.raises(ExecutionError, match='contradictory execution') as raised:
            profile.execute(driver, request)
        assert not raised.value.problem
        actual.get_json.assert_not_called()
        assert state['modelRefusal']['refusal'] is None
        assert 'private-job' not in str(raised.value) and secret not in str(raised.value)
    else:
        result = profile.execute(driver, request)['result']
        assert result['executionOutcome'] == 'refused' and result['functionalGpExecuted'] is False
        actual.get_json.assert_called_once()
        assert secret not in json.dumps(state['modelRefusal']['refusal'])


@pytest.mark.parametrize('marker', ['true', 'false', 1, None])
@pytest.mark.parametrize('actual_transport', [True, False])
def test_nonboolean_error_marker_cannot_become_typed_refusal(tmp_path, monkeypatch, marker, actual_transport):
    import probes
    from transport import Transport
    driver, engine, state = _driver(problem=GP)
    raw = {'isError': marker, 'structuredContent': GP}
    if actual_transport:
        view = engine.view()
        class Session:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def initialize(self, **kwargs): return {}
            def request(self, method, params):
                return {'result': {'_meta': view['metadata'], 'tools': view['tools']}} if method == 'tools/list' else {'result': raw}
        monkeypatch.setattr(probes, 'McpProxySession', Session)
        proxy = tmp_path / 'installed-proxy'; proxy.write_text('')
        engine.transport = Transport('http://127.0.0.1:8123', proxy, None, tmp_path,
                                     {'operator': 'app-key', 'proposer': 'app-key'})
    else: engine.transport.tool.return_value = raw
    with pytest.raises(ExecutionError, match='nonboolean error marker'):
        profile.execute(driver, {'profile': r.PROFILE, 'stage': 'durable-refusal',
            'action': {'kind': 'tool_call', 'tool': 'honua_execute_plan', 'arguments': _plan_arguments()}})
    assert state['modelRefusal']['refusal'] is None


def test_unlocked_run_bound_to_the_pinned_image_executes_instead_of_refusing(tmp_path, monkeypatch):
    # A manual dispatch has no lock digest, but the receipt is bound to the manifest-pinned image the
    # green deterministic receipt verified (operator ruling 2026-10-22). The dispatcher must agree with
    # unavailable_receipt, which treats that as runnable; requiring --lock-digest made it raise.
    import types
    import terminal_model_refusal as refusal
    driver = tmp_path / "driver.py"
    driver.write_text("")
    calls = []

    class Builder:
        def __init__(self, **_kwargs):
            self.receipt = {"status": "pending", "linkedEvidence": {"deterministicReceipt": None}}

        def bound(self):
            return True

        def validated_receipt(self):
            return self.receipt

    monkeypatch.setattr(refusal, "RefusalReceiptBuilder", Builder)
    monkeypatch.setattr(refusal.c, "build_receipt_builder", lambda **_k: types.SimpleNamespace(
        protocol={}, redactor=c.Redactor([]), journey={}))
    monkeypatch.setattr(refusal.c, "load_manifest", lambda _p: {})
    monkeypatch.setattr(refusal.c, "validate_deterministic_receipt", lambda *a, **k: {"candidateVerified": True})
    monkeypatch.setattr(refusal.c, "write_receipt", lambda *_a: None)
    monkeypatch.setattr(refusal.c, "unavailable_receipt", lambda *a: calls.append("unavailable"))
    monkeypatch.setattr(refusal, "execute_live", lambda builder, **_k: calls.append("live"))
    args = types.SimpleNamespace(manifest=tmp_path / "m", journey=tmp_path / "j", driver_protocol=tmp_path / "p",
                                 driver_command=driver, lock_digest=None, lock=None, cell="aws-ecs/redis-off",
                                 attempt=1, deterministic_receipt=tmp_path / "d.json", output=tmp_path / "o.json",
                                 max_actions_per_stage=4, target=None)
    endpoint = types.SimpleNamespace(configured=True, require_api_key=False, api_key=None, runtime="r", quantization="q")
    refusal.main_from_args(args, endpoint)
    assert calls == ["live"]


def test_successful_unlocked_run_bound_to_the_pinned_image_validates(tmp_path, monkeypatch):
    # The dispatcher admits a run bound to the manifest-pinned image without --lock-digest; the pass
    # invariant must accept the same binding, or the receipt never validates and is never written.
    b, result, _ = _runner(tmp_path, monkeypatch)
    assert result["status"] == "pass"
    b.receipt["lockDigest"] = None
    assert b.bound()
    assert b.validated_receipt()["status"] == "pass"
    b.receipt["linkedEvidence"]["deterministicReceipt"]["candidateVerified"] = False
    assert not b.bound()
    with pytest.raises(c.CanaryError):
        b.validated_receipt()


def _run_view():
    """The bounded setup view run 41013398407 showed the model (41 tools; names only)."""
    names=["list_capabilities","resolve_entity","ingest_dataset","ingest_job_artifact","publish_result",
        "publish_service","describe_layer","list_layers","query_features","apply_style_preset","get_style",
        "render_map","dry_run_plan","execute_plan","get_job_results","list_jobs","plan_analysis","validate_plan",
        "create_app_package","create_map_package","studio_create_draft","studio_get_draft","studio_save_version",
        "studio_propose_publication","supported_operation_kinds"]
    plan={"type":"object","required":["plan"],"properties":{"plan":{"type":"object"}}}
    return [{"name":"honua_"+n,"inputSchema":plan if n.endswith("_plan") else {"type":"object"}} for n in names]


def _view_driver(problem=GP):
    driver,engine,state=_driver(problem=problem)
    engine.observation.setup_discovery={"metadata":{"view":"setup"},"tools":_run_view()}
    driver._observation_payload=lambda _o:{}
    driver._tool_view=lambda o:{"bounded":True,"tools":o.setup_discovery["tools"]}
    return driver,engine,state


def test_refusal_stage_advertises_exactly_the_tools_execute_admits_and_a_correct_plan_is_refused():
    # Run 41013398407: stage 2 was shown 41 tools and no admitted names; the model's action was refused.
    driver,engine,state=_view_driver()
    observed=profile.observe(driver,{"profile":r.PROFILE,"stage":"durable-refusal"})
    assert observed["stageTools"]==sorted(profile.STAGE_TOOLS[2])
    assert r.admitted_tools(observed)==observed["stageTools"]
    assert profile.observe(driver,{"profile":r.PROFILE,"stage":"serving-read"})["stageTools"]==sorted(profile.STAGE_TOOLS[1])
    contract=observed["observation"]["planContract"]
    assert contract["tools"]==list(profile.PLAN_TOOLS)
    arguments=copy.deepcopy(contract["arguments"]);plan=arguments["plan"]
    plan.update(planId="model-buffer",intentId="model-intent");plan["steps"][0]["stepId"]="buffer"
    assert arguments==_plan_arguments()
    result=profile.execute(driver,{"profile":r.PROFILE,"stage":"durable-refusal",
        "action":{"kind":"tool_call","tool":"honua_validate_plan","arguments":arguments,"intent":"attempt"}})["result"]
    assert result["executionOutcome"]=="refused" and state["modelRefusal"]["refusal"]
    system=r.stage_system(observed["stageStatus"],observed["stageTools"])
    assert "Current stage: durable-refusal" in system and ", ".join(sorted(profile.STAGE_TOOLS[2])) in system


def test_refusal_stage_omits_a_stage_tool_the_view_does_not_list():
    driver,engine,_=_view_driver()
    engine.observation.setup_discovery["tools"]=[t for t in _run_view() if t["name"]!="honua_dry_run_plan"]
    observed=profile.observe(driver,{"profile":r.PROFILE,"stage":"durable-refusal"})
    assert "honua_dry_run_plan" not in observed["stageTools"]
    assert "honua_dry_run_plan" not in observed["observation"]["planContract"]["tools"]
    with pytest.raises(ExecutionError,match="tool=honua_dry_run_plan"):
        profile.execute(driver,{"profile":r.PROFILE,"stage":"durable-refusal",
            "action":{"kind":"tool_call","tool":"honua_dry_run_plan","arguments":_plan_arguments()}})


@pytest.mark.parametrize("action,summary",[
    ({"kind":"tool_call","tool":"honua_list_jobs","arguments":{"secret":"s3cr3t"}},"kind=tool_call; tool=honua_list_jobs"),
    ({"kind":"terminal_command","command":"curl s3cr3t"},"kind=terminal_command)"),
    ({"kind":"tool_call","tool":"x y; s3cr3t","arguments":{}},"kind=tool_call; tool=<non-identifier>"),
    ({"kind":"tool_call","tool":"honua_query_features","arguments":{}},"tool=honua_query_features")])
def test_rejected_refusal_action_names_kind_and_identifier_tool_only(action,summary):
    driver,engine,_=_view_driver()
    with pytest.raises(ExecutionError) as caught:
        profile.execute(driver,{"profile":r.PROFILE,"stage":"durable-refusal","action":action})
    assert summary in str(caught.value) and "s3cr3t" not in str(caught.value)
    engine.transport.tool.assert_not_called()


def test_runner_tells_each_stage_its_admitted_tools_and_task(tmp_path,monkeypatch):
    b,result,state=_runner(tmp_path,monkeypatch)
    assert result["status"]=="pass"
    systems=[m[0]["content"] for m in state["messages"]]
    assert systems[0]!=systems[1] and "Current stage: serving-read" in systems[0]
    assert "Current stage: durable-refusal" in systems[1]
    for stage,system,messages in zip((1,2,2),systems,state["messages"]):
        allowed=sorted(profile.STAGE_TOOLS[stage])
        assert "Allowed tool_call names (the server-authored view for this stage): "+", ".join(allowed)+"." in system
        assert "honua_list_jobs" not in system
        assert json.loads(messages[1]["content"])["allowedTools"]==allowed


def test_runner_without_advertised_stage_tools_is_infrastructure_without_a_model_call(tmp_path,monkeypatch):
    b,result,state=_runner(tmp_path,monkeypatch,mutation="no-stage-tools")
    assert result["status"]=="fail" and result["failureAttribution"]==c.INFRASTRUCTURE and state["calls"]==0


ASSESSMENT={"executionOutcome":"refused","missingDependency":"redis","functionalGpExecuted":False}


def _real_driver_path(monkeypatch):
    """parse_model_action -> DriverAdapter.invoke (real JSON request) -> live_driver.main/handle/op_execute
    -> refusal_profile.execute, with only the candidate transport and workspace state faked."""
    import io
    import live_driver
    driver,engine,state=_driver(problem=GP)
    profile.execute(driver,{"profile":r.PROFILE,"stage":"durable-refusal",
        "action":{"kind":"tool_call","tool":"honua_validate_plan","arguments":_plan_arguments()}})
    assert state["modelRefusal"]["refusal"]["missingDependency"]=="redis"
    engine.transport.tool.return_value={"structuredContent":{"available":False}}
    for name in ("_rehydrate","_executor","_write_state"):
        monkeypatch.setattr(live_driver,name,getattr(driver,name))
    seen=[];real=profile.execute
    monkeypatch.setattr(profile,"execute",lambda d,req:(seen.append(copy.deepcopy(req)),real(d,req))[1])
    def run(argv,*,input,**_):
        assert argv[-1]==str(c.DEFAULT_DRIVER)
        stdout=io.StringIO()
        with mock.patch.object(sys,"stdin",io.StringIO(input)),mock.patch.object(sys,"stdout",stdout):
            code=live_driver.main()
        return SimpleNamespace(returncode=code,stdout=stdout.getvalue(),stderr="")
    monkeypatch.setattr(c.subprocess,"run",run)
    adapter=c.DriverAdapter(c.DEFAULT_DRIVER,c._load_json(PROTOCOL))
    def invoke(action):
        kind,parsed=c.parse_model_action(json.dumps(action))
        return adapter.invoke("execute",{"workspaceId":"w","stage":"durable-refusal","profile":r.PROFILE,"action":parsed})
    return invoke,seen,state


def test_model_assessment_reaches_refusal_profile_unchanged_through_the_real_driver_path(monkeypatch):
    # Run 41018933357: the third model call was refused as "model must acknowledge the observed refusal".
    # Nothing between the model reply and refusal_profile drops, nests or stringifies the assessment.
    invoke,seen,state=_real_driver_path(monkeypatch)
    response=invoke({"kind":"tool_call","tool":"honua_list_capabilities","arguments":{},
                     "assessment":ASSESSMENT,"intent":"acknowledge the typed Redis refusal"})
    assessment=seen[0]["action"]["assessment"]
    assert assessment==ASSESSMENT and assessment["functionalGpExecuted"] is False
    assert seen[0]["action"]["arguments"]=={}
    assert response["status"]=="pass" and response["result"]["modelAcknowledgedRefusal"] is True
    assert state["modelRefusal"]["acknowledged"] is True


@pytest.mark.parametrize("change,expected",[
    (lambda a:a.pop("assessment"),"assessment absent"),
    (lambda a:a["arguments"].update(assessment=a.pop("assessment")),"assessment absent (placed inside the tool arguments)"),
    (lambda a:a.update(assessment=["refused"]),"assessment is not an object (<array>)"),
    (lambda a:a["assessment"].update(missingDependency="postgres"),"missingDependency='postgres'"),
    (lambda a:a["assessment"].update(functionalGpExecuted="false"),"functionalGpExecuted='false'"),
    (lambda a:a["assessment"].update(functionalGpExecuted=None),"functionalGpExecuted=null"),
    (lambda a:a["assessment"].pop("executionOutcome"),"executionOutcome absent"),
    (lambda a:a["assessment"].update(executionOutcome="completed"),"executionOutcome='completed'"),
    (lambda a:a["assessment"].update(executionOutcome="the job ran and s3cr3t completed fine"),
     "executionOutcome=<string of 37 chars>"),
    (lambda a:a["assessment"].update(note="x"),"1 unexpected key"),
])
def test_assessment_mismatch_names_the_field_and_only_a_safe_value(monkeypatch,change,expected):
    invoke,seen,state=_real_driver_path(monkeypatch)
    action={"kind":"tool_call","tool":"honua_list_capabilities","arguments":{},"assessment":dict(ASSESSMENT)}
    change(action)
    with pytest.raises(c.CanaryError) as caught:invoke(action)
    message=str(caught.value)
    assert "driverError=ExecutionError: model assessment: acknowledgement of the observed refusal differs: " in message
    assert expected in message and "s3cr3t" not in message
    assert state["modelRefusal"]["acknowledged"] is False


def test_assessment_dependency_matches_the_observed_refusal_case_insensitively_only():
    refusal={"missingDependency":"redis"}
    ok={**ASSESSMENT,"missingDependency":"Redis"}
    assert profile.assessment_problems({"assessment":ok},refusal)==[]
    # Derived from the observed refusal, never hard-coded: another dependency is a different refusal.
    assert profile.assessment_problems({"assessment":ok},{"missingDependency":"postgres"})==["missingDependency='Redis'"]
    assert profile.assessment_problems({"assessment":ASSESSMENT},{})==["observed typed refusal names no missingDependency"]
    assert profile.assessment_problems({"assessment":{**ASSESSMENT,"functionalGpExecuted":0}},refusal)==["functionalGpExecuted=0"]
