"""Controls for the live scratch-edit proof, including HTTP-200 row failures."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verify_seed_edit", ROOT / "e2e/harness/seed/verify_edit.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def exercise(*, storage="managed", capabilities="Query,Create,Update,Delete", success=True, persists=True):
    calls = []
    attributes = {"objectid": 42, "id": 1, "name": "Kahului Harbor pier 2", "note": "fixture"}
    stored = dict(attributes)

    def request(path, values, method="GET"):
        calls.append((path, values, method))
        if path.endswith("/applyEdits"):
            update = json.loads(values["updates"])[0]["attributes"]
            assert update["objectid"] == 42 and set(update) == {"objectid", "note"}
            assert method == "POST"
            if success and persists:
                stored.update(update)
            return {"updateResults": [{"objectId": 42, "success": success}]}
        if path.endswith("/query"):
            return {"features": [{"attributes": dict(stored)}]}
        return {"objectIdField": "objectid", "capabilities": capabilities}

    result = proof.verify({"data": {"serviceName": "maui-inspections", "storageMode": storage, "layerId": 7}}, request)
    return result, calls


def test_actual_managed_identity_is_used_and_mutation_is_read_back_then_restored():
    result, calls = exercise()
    assert result["objectIdField"] == "objectid" and result["objectId"] == 42
    assert result["mutationReadBack"] is True and result["originalNoteRestored"] is True
    assert len(calls) == 6 and calls[-1][1]["objectIds"] == 42


@pytest.mark.parametrize("kwargs", [{"storage": "source"}, {"capabilities": "Query"},
                                   {"success": False}, {"persists": False}])
def test_bad_publication_metadata_row_verdict_or_readback_cannot_pass(kwargs):
    with pytest.raises(ValueError):
        exercise(**kwargs)


def test_current_sample_placeholder_binds_to_seeded_zoning():
    path = ROOT / "certification/executable-docs/vars/honua-samples-samples-hello-featureserver-rest-readme.json"
    substitutions = json.loads(path.read_text(encoding="utf-8"))["substitute"]
    assert substitutions["your-feature-service"]["value"] == "{fixture.featureService}"
    command = "export HONUA_SAMPLE_SERVICE=your-feature-service"
    for literal, replacement in substitutions.items():
        command = command.replace(literal, replacement["value"].replace("{fixture.featureService}", "maui-zoning"))
    assert command == "export HONUA_SAMPLE_SERVICE=maui-zoning"
