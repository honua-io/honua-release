"""Bind the seed's returned layer ids into the pinned documentation queries.

The excerpts in fixtures/pinned-seed-queries.json are the fenced blocks the
inventory records. Substitution goes through inputs.render and run.substitute,
which is the runner's request-construction path. A missing buildings id stays
visible; it is not rewritten to the public demo's layer 13.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from inputs import load_vars, render  # noqa: E402
from run import refresh_seed_bindings, substitute  # noqa: E402

FIXTURE = HERE / "fixtures" / "pinned-seed-queries.json"
INVENTORY = HERE / "inventory.json"
VARS = HERE / "vars"
WHERE = re.compile(r"""(?:where|filter)=("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")
SIBLINGS = {
    "honua-io/honua-sdk-python": Path("/home/mike/honua-io/honua-sdk-python"),
    "honua-io/honua-sdk-js": Path("/home/mike/honua-io/honua-sdk-js"),
}


def load_plan():
    path = ROOT / "e2e/harness/seed/plan.py"
    spec = importlib.util.spec_from_file_location("honua_release_seed_plan_contract", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plan = load_plan()


def inventory_docs() -> dict[str, dict]:
    payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
    return {document["id"]: document for document in payload["documents"]}


def pinned_blocks() -> dict[str, list[dict]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def sequential_ids() -> dict[str, int]:
    return {row.service_name: index + 1 for index, row in enumerate(plan.publications())}


def context_for(ids: dict[str, int]) -> dict[str, str]:
    manifest = plan.render_manifest("dry-run-connection", ids)
    context = {
        "candidate.baseUrl": "http://localhost:8080",
        "candidate.apiKey": "honua-console-dev-key",
        "candidate.grpcAddress": "localhost:8081",
        "fixture.featureService": "maui-zoning",
        "fixture.featureLayerId": "2",
    }
    context.update(plan.bind_fixture_context(manifest))
    return context


def apply_document(document_id: str, code: str, context: dict[str, str]) -> str:
    variables = load_vars(VARS, document_id)
    table = {key: render(str(entry["value"]), context) for key, entry in variables["substitute"].items()}
    return substitute(code, table)


def matches(row: dict, where: str) -> bool:
    if where == "1=1":
        return True
    if where == "status = 'active'":
        return row["status"] == "active"
    if where == "height > 10":
        return row["height"] is not None and row["height"] > 10
    raise AssertionError(f"pinned where clause has no row evaluator: {where}")


def test_pinned_excerpts_match_the_inventory_and_the_pinned_tree_when_present():
    docs = inventory_docs()
    assert docs["honua-sdk-js-readme"]["revision"].startswith("984425f9")
    assert docs["honua-sdk-python-readme"]["revision"].startswith("a9cd320a")
    assert docs["honua-sdk-python-docs-quickstart"]["revision"].startswith("a9cd320a")
    for document_id, blocks in pinned_blocks().items():
        document = docs[document_id]
        recorded = {block["sha256"]: block for block in document["blocks"]}
        for block in blocks:
            digest = hashlib.sha256(block["code"].encode("utf-8")).hexdigest()
            assert digest == block["sha256"]
            assert recorded[digest]["index"] == block["index"]
            assert recorded[digest]["line"] == block["line"]
        sibling = SIBLINGS.get(document["repo"])
        if sibling is None or not (sibling / ".git").exists():
            continue
        text = subprocess.check_output(
            ["git", "-C", str(sibling), "show", f"{document['revision']}:{document['path']}"],
            text=True,
        )
        sys.path.insert(0, str(HERE))
        from blocks import extract
        extracted = {block.sha256: block.code for block in extract(text, "markdown")}
        for block in blocks:
            assert extracted[block["sha256"]] == block["code"]


def test_only_the_buildings_documents_rewrite_layer_13():
    rewriting = []
    for path in sorted(VARS.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = set((data.get("substitute") or {}))
        assert "layer_id=0" not in keys
        assert "svc" not in keys
        if "layer_id=13" in keys:
            rewriting.append(data["document"])
    assert rewriting == ["honua-sdk-python-docs-quickstart", "honua-sdk-python-readme"]


def test_returned_buildings_id_replaces_layer_13_and_queries_hit_real_rows():
    ids = sequential_ids()
    assert ids["maui-buildings"] == 10
    assert ids["maui-buildings"] != 13
    context = context_for(ids)
    assert context["fixture.mauiBuildingsLayerId"] == "10"
    assert context["fixture.featureLayerId"] == "2"
    rows = plan.buildings_rows()
    for document_id, blocks in pinned_blocks().items():
        for block in blocks:
            rewritten = apply_document(document_id, block["code"], context)
            if "layer_id=13" in block["code"]:
                assert "layer_id=13" not in rewritten
                assert "layer_id=10" in rewritten
                assert "maui-buildings" in rewritten
            if "layer_id=0" in block["code"]:
                assert "layer_id=0" in rewritten
            if 'apply_edits("svc", 0' in block["code"]:
                assert 'apply_edits("svc", 0' in rewritten
            if 'ogc.collection("parcels")' in block["code"]:
                assert 'ogc.collection("parcels")' in rewritten
                assert 'parcels.item("123")' in rewritten
            if "zone = '1'" in block["code"]:
                assert "zone = '1'" in rewritten
                assert "tmk_txt" not in rewritten
                assert "tmk_txt" not in block["code"]
            if "maui-buildings" not in block["code"]:
                continue
            for quoted in WHERE.findall(block["code"]):
                where = quoted[1:-1]
                matched = [row for row in rows if matches(row, where)]
                assert matched, where
                if where != "1=1":
                    assert len(matched) < len(rows)


def test_a_different_returned_id_is_what_the_query_uses():
    ids = sequential_ids()
    ids["maui-buildings"] = 42
    context = context_for(ids)
    readme = pinned_blocks()["honua-sdk-python-readme"][0]
    assert "layer_id=13" in readme["code"]
    rewritten = apply_document("honua-sdk-python-readme", readme["code"], context)
    assert "layer_id=42" in rewritten
    assert "layer_id=13" not in rewritten
    assert "layer_id=10" not in rewritten


def test_missing_buildings_binding_stays_visible():
    ids = sequential_ids()
    manifest = plan.render_manifest("dry-run-connection", ids)
    del manifest["demo"]["maui-buildings"]
    bound = plan.bind_fixture_context(manifest)
    assert "fixture.mauiBuildingsLayerId" not in bound
    context = {"candidate.baseUrl": "http://localhost:8080"}
    readme = pinned_blocks()["honua-sdk-python-readme"][0]
    rewritten = apply_document("honua-sdk-python-readme", readme["code"], context)
    assert "layer_id={fixture.mauiBuildingsLayerId}" in rewritten
    assert "layer_id=13" not in rewritten
    assert "layer_id=10" not in rewritten


def test_runner_refreshes_bindings_from_the_seed_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("E2E_OUT", str(tmp_path))
    context: dict[str, str] = {"fixture.featureLayerId": "2"}
    refresh_seed_bindings(context)
    assert "fixture.mauiBuildingsLayerId" not in context

    ids = sequential_ids()
    manifest = plan.render_manifest("dry-run-connection", ids)
    (tmp_path / "seed-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    refresh_seed_bindings(context)
    assert context["fixture.mauiBuildingsLayerId"] == "10"
    assert context["fixture.featureService"] == "maui-zoning"
    assert context["fixture.featureLayerId"] == "2"
    assert context["fixture.editService"] == "maui-inspections"
    assert context["fixture.mauiInspectionsLayerId"] == "7"

    stale = dict(manifest)
    stale_demo = dict(manifest["demo"])
    del stale_demo["maui-buildings"]
    stale["demo"] = stale_demo
    (tmp_path / "seed-manifest.json").write_text(json.dumps(stale), encoding="utf-8")
    untouched = {"fixture.featureLayerId": "2"}
    refresh_seed_bindings(untouched)
    assert "fixture.mauiBuildingsLayerId" not in untouched
