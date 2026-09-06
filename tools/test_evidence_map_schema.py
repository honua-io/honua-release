"""Evidence-map representation and fail-closed proof-state regressions."""
import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/2026.1-evidence-map.schema.json").read_text())
ROW = SCHEMA["$defs"]["row"]["properties"]
TEST = {"file": "tests/auth.cs", "name": "DeniedRead", "fixture": "two tenants",
        "executionLayer": "api-integration", "candidateBoundReceipt": False}


def valid(definition, instance):
    return Draft202012Validator({**definition, "$defs": SCHEMA["$defs"]}).is_valid(instance)


def test_schema_is_well_formed():
    Draft202012Validator.check_schema(SCHEMA)


def test_all_documented_row_ids_are_accepted():
    document = (ROOT / "docs/2026.1-evidence-map.md").read_text()
    ids = re.findall(r"^\| \*\*([A-Z]+-\d+)\*\* / P[012]", document, re.M)
    assert len(ids) == len(set(ids)) == 62
    assert all(valid(ROW["id"], row_id) for row_id in ids)


@pytest.mark.parametrize("row_id", ["P1-01", "P2-01", "X-01", "A-1", "GP-001"])
def test_undocumented_id_families_and_bad_numbering_are_rejected(row_id):
    assert not valid(ROW["id"], row_id)


@pytest.mark.parametrize("dispositions", [
    ["keep"], ["keep", "strengthen"], ["move", "strengthen"],
    ["strengthen", "consolidate"], ["remove"], ["not yet reviewed"],
])
def test_single_and_combined_dispositions(dispositions):
    assert valid(ROW["disposition"], dispositions)


@pytest.mark.parametrize("dispositions", [[], "keep", ["keep", "keep"], ["unknown"]])
def test_empty_duplicate_or_unknown_dispositions_fail(dispositions):
    assert not valid(ROW["disposition"], dispositions)


@pytest.mark.parametrize("has_tests,no_proof,expected", [
    (False, None, False), (False, False, False), (False, True, True),
    (True, None, True), (True, False, True), (True, True, False),
])
def test_proof_and_gap_states_are_exclusive(has_tests, no_proof, expected):
    proof = {"summary": "source audit", "tests": [TEST] if has_tests else []}
    if no_proof is not None:
        proof["noProof"] = no_proof
    assert valid(ROW["proof"], proof) is expected


RECEIPT = {"imageDigest": "sha256:" + "a" * 64, "fixtureSha256": "sha256:" + "b" * 64,
           "resultSha256": "sha256:" + "c" * 64, "result": "pass"}


def valid_test(test):
    return valid(ROW["proof"], {"summary": "source audit", "tests": [test]})


def test_bound_flag_requires_receipt_and_unbound_state_forbids_it():
    assert valid_test(TEST)
    assert not valid_test({**TEST, "candidateBoundReceipt": True})
    assert not valid_test({**TEST, "receipt": RECEIPT})


@pytest.mark.parametrize("candidate_key", ["imageDigest", "lockSha256"])
@pytest.mark.parametrize("result", ["pass", "fail", "skipped"])
def test_receipt_binds_image_or_lock_fixture_and_behavior_result(candidate_key, result):
    receipt = copy.deepcopy(RECEIPT)
    receipt[candidate_key] = receipt.pop("imageDigest")
    receipt["result"] = result
    assert valid_test({**TEST, "candidateBoundReceipt": True, "receipt": receipt})


@pytest.mark.parametrize("field", list(RECEIPT))
@pytest.mark.parametrize("mutation", ["missing", "invalid"])
def test_incomplete_or_mutable_receipt_references_fail(field, mutation):
    receipt = copy.deepcopy(RECEIPT)
    if mutation == "missing":
        del receipt[field]
    else:
        receipt[field] = "latest"
    assert not valid_test({**TEST, "candidateBoundReceipt": True, "receipt": receipt})
