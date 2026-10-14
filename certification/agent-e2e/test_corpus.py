"""Corpus/oracle regressions; these never provision or invoke a model."""
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parent


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


oracle=load("agent_corpus_oracle",ROOT/"corpus/oracle.py")
generator=load("agent_corpus_generator",ROOT/"corpus/generate.py")
pack_source=load("agent_pack_source",ROOT/"scenarios/build_pack.py")


def test_pack_has_exact_252_cases_and_all_machine_predicates():
    pack=json.loads((ROOT/"pack.v1.json").read_text(encoding="utf-8"))
    assert pack==pack_source.build()
    cases={(s["id"],v,t) for s in pack["scenarios"] for v in pack["variants"] for t in s["targets"]}
    assert len(cases)==252
    assert len(pack["scenarios"])==36
    for s in pack["scenarios"]:
        assert set(s["prompts"])=={"A","B","C"}
        assert len(set(s["prompts"].values()))==3
        assert set(s["assertions"])<=oracle.PREDICATES.keys()
        assert s["conceptIds"] and all(x.startswith("concepts/domain/") for x in s["conceptIds"])
        assert s["workflowIds"] and all(x.startswith("guides/workflows/") for x in s["workflowIds"])
    assert [sum(t==target for _,_,t in cases) for target in pack["targets"]]==[72,72,72,18,18]


def test_committed_corpus_hashes_and_external_pin():
    manifest=json.loads((ROOT/"corpus/manifest.v1.json").read_text())
    assert hashlib.sha256((ROOT/"corpus/generate.py").read_bytes()).hexdigest()==manifest["generatorSha256"]
    for row in manifest["files"]:
        data=(ROOT/"corpus"/row["path"]).read_bytes()
        assert len(data)==row["bytes"]
        assert hashlib.sha256(data).hexdigest()==row["sha256"]
    natural=next(x for x in manifest["fixtures"] if x["id"]=="natural-earth-countries")
    assert len(natural["sourceCommit"])==40 and natural["sourceCommit"] in natural["sourceUrl"]
    assert len(json.loads((ROOT/"corpus"/natural["path"]).read_text(encoding="utf-8"))["features"])==177
    large=next(x for x in manifest["fixtures"] if x["id"]=="large-import-s10")
    assert large["bytes"]>64*1024*1024 and large["featureCount"]==330000
    materializer=(ROOT.parents[1]/large["materializer"]).read_bytes().replace(b"\r\n",b"\n")
    assert hashlib.sha256(materializer).hexdigest()==large["materializerSha256"]


def test_regeneration_preserves_all_synthetic_bytes(tmp_path):
    for row in generator.generate(tmp_path):
        assert (tmp_path/row["path"]).read_bytes()==(ROOT/"corpus"/row["path"]).read_bytes()


def test_adversarial_files_are_limited_to_their_own_scenarios():
    manifest=json.loads((ROOT/"corpus/manifest.v1.json").read_text())
    for row in manifest["files"]:
        name=Path(row["path"]).name
        if name=="metadata.json": assert row["scenarioIds"]==["P19"]
        if name=="invalid-rows.csv": assert row["scenarioIds"]==["P05"]
        if name=="diagnostic-empty-geometry.geojson": assert row["scenarioIds"]==["P04"]


@pytest.mark.parametrize("variant",["A","B","C"])
def test_town_analytic_oracle_and_thresholds(variant):
    layers,expected=generator.build(variant)
    shelters=layers["shelters"]
    assert len(expected["openCapacityOver50Ids"])==2
    assert any(f["properties"]["capacity"]==50 for f in shelters)
    assert any(f["properties"]["capacity"] is None for f in shelters)
    assert any(f["properties"]["capacity"]>50 and not f["properties"]["is_open"] for f in shelters if f["properties"]["capacity"] is not None)
    assert len(expected["floodIntersectingParcelIds"])==2
    flood=layers["flood_zones"][0]["geometry"]["coordinates"][0]
    flood_max_x=max(p[0] for p in flood)
    assert expected["floodIntersectingParcelIds"]==[p["id"] for p in layers["parcels"] if min(x[0] for x in p["geometry"]["coordinates"][0])<flood_max_x]


@pytest.mark.parametrize("corruption",["missing-row","duplicate-id","wrong-attribute","wrong-type","wrong-crs","wrong-geometry"])
def test_feature_oracle_rejects_corruption(corruption):
    layers,_=generator.build("A")
    expected={"srid":3857,"features":layers["shelters"],"tolerance":0.01}
    actual=copy.deepcopy(expected)
    oracle.verify("features.exact",actual,expected)
    if corruption=="missing-row": actual["features"].pop()
    if corruption=="duplicate-id": actual["features"][1]=copy.deepcopy(actual["features"][0])
    if corruption=="wrong-attribute": actual["features"][0]["properties"]["capacity"]=50
    if corruption=="wrong-type": actual["features"][0]["properties"]["is_open"]=1
    if corruption=="wrong-crs": actual["srid"]=4326
    if corruption=="wrong-geometry": actual["features"][0]["geometry"]["coordinates"][0]+=1
    with pytest.raises(oracle.VerificationError): oracle.verify("features.exact",actual,expected)


@pytest.mark.parametrize("radius",[0.005,498,502,1000])
def test_buffer_rejects_wrong_metric_radius(radius):
    _,expected=generator.build("A")
    b=expected["buffer"]
    cx,cy=b["center3857"]
    ring=[[cx+radius*math.cos(i*2*math.pi/128),cy+radius*math.sin(i*2*math.pi/128)] for i in range(128)]
    ring.append(ring[0])
    with pytest.raises(oracle.VerificationError): oracle.verify("buffer.metric",{"srid":3857,"sourceId":b["sourceId"],"geometry":{"type":"Polygon","coordinates":[ring]}},b)


@pytest.mark.parametrize("segments",[32,128])
def test_buffer_accepts_independent_analytic_circle(segments):
    _,expected=generator.build("A")
    b=expected["buffer"]
    cx,cy=b["center3857"]
    ring=[[cx+500*math.cos(i*2*math.pi/segments),cy+500*math.sin(i*2*math.pi/segments)] for i in range(segments)]
    ring.append(ring[0])
    oracle.verify("buffer.metric",{"srid":3857,"sourceId":b["sourceId"],"geometry":{"type":"Polygon","coordinates":[ring]}},b)


def test_unknown_missing_and_empty_assertions_fail_closed():
    for name,seen,wanted in [("unknown",{},{}),("candidate.identity",{},{}),("candidate.identity",{"pass":True},{"sourceRevision":"a"*40}),("features.exact",{},{}),("render.actual",{},{}),("batch.native-worker-bound",{}, {})]:
        with pytest.raises(oracle.VerificationError): oracle.verify(name,seen,wanted)


def test_permission_oracle_rejects_missing_and_leaking_probe():
    expected={"requests":[{"principalId":"outsider","method":"GET","resourceId":"private-map","status":403},{"principalId":"owner","method":"GET","resourceId":"private-map","status":200}]}
    actual={"requests":[{**row,"responseSha256":"a"*64,"disclosedResourceIds":[]} for row in expected["requests"]]}
    oracle.verify("tenant.denied",actual,expected)
    actual["requests"][0]["disclosedResourceIds"]=["private-map"]
    with pytest.raises(oracle.VerificationError): oracle.verify("tenant.denied",actual,expected)
    with pytest.raises(oracle.VerificationError): oracle.verify("tenant.denied",{"requests":[]},expected)
    with pytest.raises(oracle.VerificationError): oracle.verify("tenant.denied",{"requests":[]},{"requests":[]})
    with pytest.raises(oracle.VerificationError): oracle.verify("tenant.denied",{"requests":actual["requests"][:1]},{"requests":expected["requests"][:1]})


def test_cleanup_requires_exact_inventory_not_success_text():
    oracle.verify("cleanup.complete",{"resourceIds":[]},{"resourceIds":[]})
    with pytest.raises(oracle.VerificationError): oracle.verify("cleanup.complete",{"resourceIds":["leaked-s3-object"]},{"resourceIds":[]})
    with pytest.raises(oracle.VerificationError): oracle.verify("cleanup.complete",{"status":"success"},{"resourceIds":[]})


def test_render_requires_actual_geometry_regions_labels_and_layers():
    expected={"layerIds":["parcels","shelters"],"minimumNonBackgroundPixels":100,"regions":[{"id":"shelter-point","minimumMatchingPixels":10}],"labelSourceIds":["s1"]}
    actual={"httpStatus":200,"decodedWidth":512,"decodedHeight":512,"loadedLayerIds":["parcels","shelters"],"nonBackgroundPixels":200,"regions":{"shelter-point":{"matchingPixels":20}},"visibleLabelSourceIds":["s1"],"browserErrors":[]}
    oracle.verify("render.actual",actual,expected)
    for field,value in [("nonBackgroundPixels",0),("loadedLayerIds",["parcels"]),("visibleLabelSourceIds",[]),("browserErrors",["layer failed"]),("regions",{"shelter-point":{"matchingPixels":0}})]:
        wrong={**actual,field:value}
        with pytest.raises(oracle.VerificationError): oracle.verify("render.actual",wrong,expected)


def test_native_worker_rejects_server_image_or_missing_real_batch_job():
    actual={"batchJobId":"batch-123","workerOciDigest":"sha256:"+"a"*64,"workerArchitecture":"linux/amd64","workerSourceRevision":"b"*40,"workerAttestationSha256":"c"*64,"serverJobId":"server-123","s3OutputUri":"s3://run-owned/out.geojson","s3OutputSha256":"d"*64,"workerArtifact":"honua-worker-etl","batchStatus":"SUCCEEDED"}
    oracle.verify("batch.native-worker-bound",actual,actual.copy())
    with pytest.raises(oracle.VerificationError): oracle.verify("batch.native-worker-bound",{**actual,"workerArtifact":"honua-server"},actual)
    with pytest.raises(oracle.VerificationError): oracle.verify("batch.native-worker-bound",{**actual,"batchJobId":""},actual)


def test_variants_vary_values_and_relationships_not_only_names():
    towns=[generator.build(v) for v in "ABC"]
    # Compare feature-order facts after stripping variant source ID prefixes.
    capacities=[tuple(f["properties"]["capacity"] for f in layers["shelters"]) for layers,_ in towns]
    assert len(set(capacities))==3
    flood_slots=[tuple(i for i,f in enumerate(layers["parcels"]) if f["id"] in expected["floodIntersectingParcelIds"]) for layers,expected in towns]
    assert len(set(flood_slots))==3
    for layers,_ in towns:
        assert any(len(f["geometry"]["coordinates"])==2 for f in layers["parcels"])


def test_buffer_rejects_crossed_ring_with_nearly_identical_area():
    _,expected=generator.build("A")
    b=expected["buffer"]
    cx,cy=b["center3857"]
    ring=[[cx+500*math.cos(i*2*math.pi/128),cy+500*math.sin(i*2*math.pi/128)] for i in range(128)]
    ring[10],ring[11]=ring[11],ring[10]
    ring.append(ring[0])
    with pytest.raises(oracle.VerificationError): oracle.verify("buffer.metric",{"srid":3857,"sourceId":b["sourceId"],"geometry":{"type":"Polygon","coordinates":[ring]}},b)


def test_matched_expected_cannot_legitimize_leaks_or_fake_cold_start():
    secrets={"scannedArtifactDigests":["a"*64],"leakedCredentialFingerprints":["secret"],"matchedPrivateCanaries":[]}
    with pytest.raises(oracle.VerificationError): oracle.verify("secrets.absent",secrets,secrets.copy())
    cold={"functionVersion":"42","requestId":"request-1","initializationDurationMs":0}
    with pytest.raises(oracle.VerificationError): oracle.verify("lambda.cold-start",cold,cold.copy())


def test_static_expectations_match_reviewed_fixture_and_multi_part_data():
    for variant in "ABC":
        expected=oracle.fixture_expectation(variant,"features.exact",["multipolygons","multilines"])
        assert [f["geometry"]["type"] for f in expected["features"]]==["MultiPolygon","MultiLineString"]
        oracle.verify("features.exact",copy.deepcopy(expected),expected)
        assert len(oracle.fixture_expectation(variant,"filter.exact")["resourceIds"])==2
    with pytest.raises(oracle.VerificationError): oracle.fixture_expectation("C","features.exact","../../oracles")


def test_pagination_rejects_first_page_only_and_duplicate_page():
    expected=oracle.fixture_expectation("A","pagination.complete")
    ids=expected["sourceIds"]
    assert len(ids)==2105
    pages=[{"status":200,"sourceIds":ids[i:i+1000],"responseSha256":"a"*64} for i in range(0,len(ids),1000)]
    oracle.verify("pagination.complete",{"pages":pages},expected)
    with pytest.raises(oracle.VerificationError): oracle.verify("pagination.complete",{"pages":pages[:1]},expected)
    with pytest.raises(oracle.VerificationError): oracle.verify("pagination.complete",{"pages":pages+[pages[0]]},expected)


def test_exactly_once_requires_failure_after_commit_and_actual_redelivery():
    row={"serverJobId":"job-1","idempotencyKey":"key-1","committedOutputDigest":"a"*64,"deliveryIds":["d1","d2"],"workerAttemptIds":["w1","w2"],"firstCommitTimestamp":"2026-10-14T00:00:00Z","injectedFailureTimestamp":"2026-10-14T00:00:01Z","redeliveryTimestamp":"2026-10-14T00:00:02Z","committedOutputCount":1,"duplicateSideEffects":[]}
    oracle.verify("job.exactly-once",row,row)
    for key,value in [("deliveryIds",["d1"]),("injectedFailureTimestamp","2026-10-13T23:59:59Z"),("committedOutputCount",2),("duplicateSideEffects",["duplicate-output"])]:
        wrong={**row,key:value}
        with pytest.raises(oracle.VerificationError): oracle.verify("job.exactly-once",wrong,wrong)
