"""The shared plan oracle rejects false positives without a server or client."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "certification/installed-clients"
SPEC = importlib.util.spec_from_file_location("shared_plan_validation", HERE / "mcp_plan_validation.py")
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)
FIXTURE = (HERE / "fixture.v1.json").read_bytes()


def observations():
    value = PLAN.scenario(FIXTURE)
    outputs = [
        {"isExecutable": True, "requiresApproval": False, "violations": [], "warnings": []},
        {"isExecutable": False, "requiresApproval": False, "warnings": [], "violations": [
            {"code": "INVALID_PARAMETER_VALUE", "fieldPath": "steps[buffer].inputs.wkb", "message": "Invalid base64 WKB"}]}]
    return {"schema": value["schema"], "fixtureSha256": value["fixtureSha256"],
            "cases": [{**case, "result": {"isError": False, "structuredContent": output}}
                      for case, output in zip(value["cases"], outputs, strict=True)]}


def test_authored_request_pair_changes_only_wkb():
    value = PLAN.scenario(FIXTURE)
    valid, invalid = [item["arguments"]["plan"] for item in value["cases"]]
    assert valid["steps"][0]["kind"] == "Geoprocess"
    assert valid["steps"][0]["processId"] == "geometry.buffer"
    assert valid["steps"][0]["inputs"]["srid"] == "3857"
    assert valid["steps"][0]["inputs"]["distance"] == "500.0"
    repaired = copy.deepcopy(invalid)
    repaired["steps"][0]["inputs"]["wkb"] = valid["steps"][0]["inputs"]["wkb"]
    assert repaired == valid
    assert invalid["steps"][0]["inputs"]["wkb"] == "not-base64"


def test_structured_and_public_text_tool_results_share_the_existing_oracle():
    value = observations()
    assert PLAN.judge(FIXTURE, value)["positive"] is True
    for case in value["cases"]:
        case["result"]["content"] = [{"type": "text", "text": json.dumps(case["result"]["structuredContent"])}]
    assert PLAN.judge(FIXTURE, value)["mediaSchema"] is True
    for case in value["cases"]:
        case["result"] = {"content": [{"type": "text", "text": json.dumps(case["result"]["structuredContent"])}]}
    assert PLAN.judge(FIXTURE, value)["negative"] is True


@pytest.mark.parametrize("mutation", [
    "wrong-fixture", "missing-case", "duplicate-case", "wrong-request", "wrong-order", "error-envelope",
    "nonobject-output", "missing-output", "nonbool-executable", "unexpected-approval", "positive-violations",
    "invalid-accepted", "wrong-violation-code", "wrong-violation-path", "no-encoding-diagnosis", "missing-violations"])
def test_retained_observations_fail_closed(mutation):
    value = observations()
    positive = value["cases"][0]["result"]["structuredContent"]
    negative = value["cases"][1]["result"]["structuredContent"]
    if mutation == "wrong-fixture": value["fixtureSha256"] = "sha256:" + "0" * 64
    elif mutation == "missing-case": value["cases"].pop()
    elif mutation == "duplicate-case": value["cases"][1] = copy.deepcopy(value["cases"][0])
    elif mutation == "wrong-request": value["cases"][0]["arguments"]["plan"]["steps"][0]["inputs"]["distance"] = "1"
    elif mutation == "wrong-order": value["cases"].reverse()
    elif mutation == "error-envelope": value["cases"][0]["result"]["isError"] = True
    elif mutation == "nonobject-output": value["cases"][0]["result"]["structuredContent"] = []
    elif mutation == "missing-output": value["cases"][0]["result"] = {}
    elif mutation == "nonbool-executable": positive["isExecutable"] = 1
    elif mutation == "unexpected-approval": positive["requiresApproval"] = True
    elif mutation == "positive-violations": positive["violations"] = [{"code": "BAD"}]
    elif mutation == "invalid-accepted": negative["isExecutable"] = True
    elif mutation == "wrong-violation-code": negative["violations"][0]["code"] = "SOMETHING_ELSE"
    elif mutation == "wrong-violation-path": negative["violations"][0]["fieldPath"] = "steps[other].inputs.wkb"
    elif mutation == "no-encoding-diagnosis": negative["violations"][0]["message"] = "Unknown error"
    elif mutation == "missing-violations": negative.pop("violations")
    with pytest.raises(ValueError):
        PLAN.judge(FIXTURE, value)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("mutation", [
    "missing-approval", "nonbool-approval", "missing-warnings", "null-warnings", "warning-item",
    "null-violations", "violation-item", "missing-code", "nonstr-code", "missing-message",
    "nonstr-message", "nonstr-fieldpath"])
def test_every_response_obeys_the_published_output_schema(index, mutation):
    value = observations()
    output = value["cases"][index]["result"]["structuredContent"]
    # Include an otherwise schema-valid entry on the positive response, so an
    # invalid item cannot be hidden by relying only on the negative predicate.
    if mutation in {"violation-item", "missing-code", "nonstr-code", "missing-message", "nonstr-message", "nonstr-fieldpath"}:
        output["violations"] = [{"code": "INVALID_PARAMETER_VALUE", "message": "Invalid base64", "fieldPath": "steps[buffer].inputs.wkb"}]
    if mutation == "missing-approval": output.pop("requiresApproval")
    elif mutation == "nonbool-approval": output["requiresApproval"] = 0
    elif mutation == "missing-warnings": output.pop("warnings")
    elif mutation == "null-warnings": output["warnings"] = None
    elif mutation == "warning-item": output["warnings"] = [1]
    elif mutation == "null-violations": output["violations"] = None
    elif mutation == "violation-item": output["violations"] = ["not-an-object"]
    elif mutation == "missing-code": output["violations"][0].pop("code")
    elif mutation == "nonstr-code": output["violations"][0]["code"] = 1
    elif mutation == "missing-message": output["violations"][0].pop("message")
    elif mutation == "nonstr-message": output["violations"][0]["message"] = False
    elif mutation == "nonstr-fieldpath": output["violations"][0]["fieldPath"] = []
    with pytest.raises(ValueError):
        PLAN.tool_output(value["cases"][index]["result"])


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("mutation", ["conflicting-json", "malformed-json", "nonobject-json", "extra-media", "nonarray-content", "extra-text"])
def test_all_available_public_representations_must_be_consistent(index, mutation):
    value = observations()
    result = value["cases"][index]["result"]
    output = copy.deepcopy(result["structuredContent"])
    if mutation == "conflicting-json": output["isExecutable"] = not output["isExecutable"]
    text = "broken JSON" if mutation == "malformed-json" else "[]" if mutation == "nonobject-json" else json.dumps(output)
    result["content"] = [{"type": "text", "text": text}]
    if mutation == "extra-media": result["content"].append({"type": "image", "data": "AA==", "mimeType": "image/png"})
    elif mutation == "nonarray-content": result["content"] = {}
    elif mutation == "extra-text": result["content"].append({"type": "text", "text": "an unrelated message"})
    with pytest.raises(ValueError):
        PLAN.judge(FIXTURE, value)


@pytest.mark.parametrize("violation", [
    {"code": "UNKNOWN_PROCESS", "fieldPath": "steps[buffer].processId", "message": "Unknown process"},
    {"code": "INVALID_PARAMETER_VALUE", "fieldPath": "steps[buffer].inputs.distance", "message": "Invalid distance"},
    {"code": "INVALID_PARAMETER_VALUE", "fieldPath": "steps[buffer].inputs.wkb", "message": "Unrelated diagnosis"}])
def test_expected_wkb_violation_cannot_hide_an_unrelated_diagnosis(violation):
    value = observations()
    value["cases"][1]["result"]["structuredContent"]["violations"].append(violation)
    with pytest.raises(ValueError, match="unrelated"):
        PLAN.judge(FIXTURE, value)
