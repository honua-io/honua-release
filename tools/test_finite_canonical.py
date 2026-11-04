"""Finite receipt documents keep their historical canonical bytes. NaN and infinity are refused."""
import hashlib
import json
import math
from pathlib import Path

import pytest

import check_protocol_certification as certification
import cloud_cell_inventory_policy as inventory
import convergence_rebind
import ecs_honua_recovery as recovery
import gp_process_release as gate

ROOT = Path(__file__).resolve().parents[1]
STORED_ROOTS = (
    ROOT / "tools/fixtures/gp-process-release",
    ROOT / "certification/ecs-recovery",
    ROOT / "release/gp-environments",
)
FINITE_DOCUMENTS = (
    {"b": 1, "a": "café", "n": [1, 2.5, 0, -3, True, None, ""]},
    {"nested": {"x": False, "y": None}},
    {},
    {"k": "\u0000"},
)
NON_FINITE = (math.nan, math.inf, -math.inf)


def historical_utf8(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=True
    ).encode("utf-8")


def historical_ascii(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()


def contains_non_finite(value):
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_non_finite(item) for item in value)
    return False


def assert_finite_bytes(value):
    utf8 = historical_utf8(value)
    ascii_form = historical_ascii(value)
    assert gate.canonical_bytes(value) == utf8
    assert gate.digest(value) == "sha256:" + hashlib.sha256(utf8).hexdigest()
    assert inventory.digest(value) == hashlib.sha256(ascii_form).hexdigest()
    assert recovery.digest(value) == "sha256:" + hashlib.sha256(ascii_form).hexdigest()
    digest = "sha256:" + hashlib.sha256(utf8).hexdigest()
    assert convergence_rebind.receipt_digest(value) == digest
    assert certification._receipt_digest(value) == digest


@pytest.mark.parametrize("value", FINITE_DOCUMENTS)
def test_finite_values_keep_the_historical_canonical_bytes(value):
    assert_finite_bytes(value)


@pytest.mark.parametrize("number", NON_FINITE)
def test_non_finite_numbers_are_refused(number):
    value = {"measure": [number]}
    with pytest.raises(gate.GateError):
        gate.canonical_bytes(value)
    with pytest.raises(gate.GateError):
        gate.digest(value)
    with pytest.raises(ValueError):
        inventory.digest(value)
    with pytest.raises(recovery.QualificationError):
        recovery.digest(value)
    assert convergence_rebind.receipt_digest(value) is None
    assert certification._receipt_digest(value) is None


def test_convergence_and_protocol_certification_share_one_receipt_digest():
    from digests import receipt_digest
    assert convergence_rebind.receipt_digest is receipt_digest


def test_receipt_digest_ignores_non_objects():
    for value in (None, [], "nan", 1, 1.5):
        assert convergence_rebind.receipt_digest(value) is None
        assert certification._receipt_digest(value) is None


def test_stored_finite_documents_match_historical_bytes():
    checked = 0
    for folder in STORED_ROOTS:
        for path in sorted(folder.rglob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, (dict, list)):
                continue
            if contains_non_finite(document):
                with pytest.raises(gate.GateError):
                    gate.canonical_bytes(document)
                continue
            assert_finite_bytes(document)
            checked += 1
    assert checked > 0
