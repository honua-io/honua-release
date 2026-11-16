"""Evidence-map representation and fail-closed proof-state regressions."""
import copy
import json
import importlib.util
import subprocess
import sys
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tools/fixtures/evidence-map.synthetic.md"
SCHEMA = json.loads((ROOT / "schemas/2026.1-evidence-map.schema.json").read_text())
ROW = SCHEMA["$defs"]["row"]["properties"]
TEST = {"file": "tests/auth.cs", "name": "DeniedRead", "fixture": "two tenants",
        "executionLayer": "api-integration", "candidateBoundReceipt": False}


spec = importlib.util.spec_from_file_location("cem", ROOT / "tools/check_evidence_map.py")
cem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cem)

def valid(definition, instance):
    return Draft202012Validator({**definition, "$defs": SCHEMA["$defs"]}).is_valid(instance)


def test_schema_is_well_formed():
    Draft202012Validator.check_schema(SCHEMA)


def test_all_documented_row_ids_are_accepted():
    document = EXAMPLE.read_text()
    ids = re.findall(r"^\| \*\*([A-Z]+-\d+)\*\* / P[012]", document, re.M)
    assert len(ids) == len(set(ids)) == 6
    assert all(valid(ROW["id"], row_id) for row_id in ids)


def test_emitted_instances_carry_the_packet_their_rows_belong_to():
    """The document holds both packets; neither may be published under the other's identity."""
    by_packet = cem.instances(EXAMPLE)
    assert set(by_packet) == {"part1", "part2"}
    for packet, payload in by_packet.items():
        assert payload["packet"] == packet
        assert valid(SCHEMA["properties"]["packet"], payload["packet"])
        assert payload["rows"], f"{packet} must not be emitted empty"
    part1 = [r["id"] for r in by_packet["part1"]["rows"]]
    part2 = [r["id"] for r in by_packet["part2"]["rows"]]
    assert not set(part1) & set(part2)
    assert len(part1) + len(part2) == 6
    # The families part 2 introduced cannot appear in the part-1 instance.
    assert not [r for r in part1 if r.startswith(("I-", "R-"))]
    assert set(part2) == {"I-01", "R-01"}


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


SCHEMA_PATH = ROOT / "schemas/2026.1-evidence-map.schema.json"


def test_synthetic_example_passes_parser_and_schema_checks():
    assert cem.check(EXAMPLE, SCHEMA_PATH) == []


@pytest.mark.parametrize("mutation,expected", [
    (lambda md: md + next(line for line in md.splitlines() if line.startswith("| **A-01**")),
     "duplicate row id"),
    (lambda md: md.replace("| Synthetic operation |", "| |", 1), "empty Contract cell"),
    (lambda md: md.replace("| **A-01** / P0 |", "| **A-01** / P0 | extra |", 1),
     "expected 6"),
    (lambda md: md.replace("**keep**", "unknown", 1), "names no disposition"),
])
def test_parser_rejects_malformed_examples(mutation, expected):
    _, errors = cem.parse(mutation(EXAMPLE.read_text()))
    assert any(expected in error for error in errors)


@pytest.mark.parametrize("revisions,expected", [
    (["trunk"], "proof links must be pinned"),
    (["a" * 40, "b" * 40], "different revisions"),
    (["a" * 40], None),
])
def test_source_links_require_one_named_immutable_revision(tmp_path, revisions, expected):
    document = EXAMPLE.read_text() + "\nAudited revision: " + "a" * 10 + "\n"
    document += "\n".join("https://github.com/honua-io/honua-server/blob/" + revision + "/example.cs"
                            for revision in revisions)
    example = tmp_path / "example.md"
    example.write_text(document)
    errors = cem.check(example, SCHEMA_PATH)
    if expected:
        assert any(expected in error for error in errors)
    else:
        assert errors == []


def test_cli_requires_an_explicit_map():
    result = subprocess.run([sys.executable, str(ROOT / "tools/check_evidence_map.py")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "--map" in result.stderr
