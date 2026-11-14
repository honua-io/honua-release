"""Inventory drift admission and live verdict regression tests."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inventory import drift, content_drift, document_record


def test_inventory_check_reports_revision_only_drift_without_failing(tmp_path, monkeypatch, capsys):
    import inventory
    committed = {"documents": [{"id": "doc", "revision": "a" * 40, "blocks": []}]}
    current = {"documents": [{"id": "doc", "revision": "b" * 40, "blocks": []}]}
    (tmp_path / "inventory.json").write_text(json.dumps(committed))
    (tmp_path / "sources.json").write_text('{"documents":[]}')
    (tmp_path / "manifest.yaml").write_text("platformRelease: 2026.1")
    monkeypatch.setattr(inventory, "build", lambda *args: current)
    monkeypatch.setattr(sys, "argv", ["inventory.py", "--check", "--sources", str(tmp_path / "sources.json"),
                                     "--manifest", str(tmp_path / "manifest.yaml"),
                                     "--inventory", str(tmp_path / "inventory.json")])
    assert inventory.main() == 0
    assert "revision aaaaaaaaaaaa -> bbbbbbbbbbbb" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["block", "intent", "add", "remove"])
def test_content_drift_still_blocks_when_revision_changes(change):
    import copy
    committed = {"documents": [{"id": "doc", "revision": "a" * 40,
                                "blocks": [{"sha256": "original", "intent": "run"}]}]}
    current = copy.deepcopy(committed)
    current["documents"][0]["revision"] = "b" * 40
    if change == "block":
        current["documents"][0]["blocks"][0]["sha256"] = "changed"
    elif change == "intent":
        current["documents"][0]["blocks"][0]["intent"] = "excluded"
    elif change == "add":
        current["documents"].append({"id": "new", "revision": "b" * 40, "blocks": []})
    else:
        current["documents"] = []
    assert content_drift(committed, current)


def test_revision_only_drift_remains_in_evidence_but_does_not_block():
    committed = {"documents": [{"id": "doc", "revision": "a" * 40, "blocks": []}]}
    current = {"documents": [{"id": "doc", "revision": "b" * 40, "blocks": []}]}
    assert drift(committed, current)
    assert content_drift(committed, current) == []


@pytest.mark.parametrize("case", [(False, 0), (True, 1)])
def test_live_verdict_retains_revision_drift_and_rejects_changed_blocks(tmp_path, monkeypatch, case):
    changed_block, expected = case
    from types import SimpleNamespace
    import run
    document = {"repo": "honua-io/example", "path": "README.md", "runtime": "python",
                "revision": {"defaultBranch": True}}
    text = "```sh\ntrue\n```"
    committed, _ = run.document_record(document, "a" * 40, text, None)
    (tmp_path / "inventory.json").write_text(json.dumps({"documents": [committed]}))
    (tmp_path / "sources.json").write_text(json.dumps({"documents": [document], "runtimes": {"python": "pinned"}}))
    (tmp_path / "manifest.yaml").write_text("components:\n  honua-server:\n    image: candidate\n    digest: sha256:" + "a" * 64)
    monkeypatch.setattr(run, "Resolver", lambda *args, **kwargs: SimpleNamespace(
        revision=lambda doc: "b" * 40, read=lambda *args: text.replace("true", "false") if changed_block else text))
    monkeypatch.setattr(run, "Guards", lambda *args: SimpleNamespace(
        base=lambda network: "guard", closure=lambda: {}, refusals=lambda: [], stop=lambda: None))
    monkeypatch.setattr(run, "Session", lambda *args, **kwargs: SimpleNamespace(
        close=lambda: None, workdir=tmp_path))
    monkeypatch.setattr(run, "prepare_tools", lambda *args: tmp_path)
    monkeypatch.setattr(run, "checkout_context", lambda *args: None)
    monkeypatch.setattr(run.source_build, "derive", lambda *args: None)
    monkeypatch.setattr(run, "run_teardowns", lambda *args: None)
    monkeypatch.setattr(run, "snapshot_containers", lambda: set())
    monkeypatch.setattr(run, "cleanup_containers", lambda *args: None)
    monkeypatch.setattr(run, "run_document", lambda *args: ({"status": "pass", "counts": {"pass": 1},
                                                           "blocks": [], "checks": []}, set()))
    monkeypatch.delenv("HONUA_SERVER_IMAGE", raising=False)
    monkeypatch.setattr(sys, "argv", ["run.py", "--evidence-uri", "local", "--sources", str(tmp_path / "sources.json"),
                                     "--manifest", str(tmp_path / "manifest.yaml"),
                                     "--inventory", str(tmp_path / "inventory.json"),
                                     "--workdir", str(tmp_path / "work"), "--output", str(tmp_path / "report.json")])
    assert run.main() == expected
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == ("fail" if changed_block else "pass")
    assert any("revision aaaaaaaaaaaa -> bbbbbbbbbbbb" in line for line in report["inventoryDrift"])
    assert any("blocks changed" in line for line in report["inventoryDrift"]) == changed_block


@pytest.mark.parametrize("case", [
    ("```sh\nfalse\n```", "<!-- doc-run: expect-fail -->\n```sh\nfalse\n```"),
    ("```sh\ntrue\n```", "```python\ntrue\n```"),
    ("Save this as `first.py`:\n```python\nprint(1)\n```",
     "Save this as `second.py`:\n```python\nprint(1)\n```"),
])
def test_execution_metadata_changes_block_admission(case):
    document = {"repo": "honua-io/example", "path": "README.md", "runtime": "python",
                "revision": {"defaultBranch": True}}
    before, _ = document_record(document, "a" * 40, case[0])
    after, _ = document_record(document, "b" * 40, case[1])
    assert before["blocks"][0]["sha256"] == after["blocks"][0]["sha256"]
    assert content_drift({"documents": [before]}, {"documents": [after]})


def test_prose_line_shift_does_not_block_admission():
    document = {"repo": "honua-io/example", "path": "README.md", "runtime": "python",
                "revision": {"defaultBranch": True}}
    before, _ = document_record(document, "a" * 40, "```sh\ntrue\n```")
    after, _ = document_record(document, "b" * 40, "New prose.\n\n```sh\ntrue\n```")
    assert before["blocks"][0]["line"] != after["blocks"][0]["line"]
    assert not content_drift({"documents": [before]}, {"documents": [after]})


@pytest.mark.parametrize("case", [
    ("```console\n$ echo actual\nexpected\n```", "```console\n$ echo actual\n```"),
    ('<!-- doc-run: run ready-log="ready" -->\n```sh\nnpm start\n```',
     '<!-- doc-run: run ready-log="changed" -->\n```sh\nnpm start\n```'),
    ('<!-- doc-run: run ready-url="http://localhost:3000/" -->\n```sh\nnpm start\n```',
     '<!-- doc-run: run -->\n```sh\nnpm start\n```'),
])
def test_console_assertions_and_readiness_changes_block_revision_drift(case):
    document = {"repo": "honua-io/example", "path": "README.md", "runtime": "python",
                "revision": {"defaultBranch": True}}
    before, _ = document_record(document, "a" * 40, case[0])
    after, _ = document_record(document, "b" * 40, case[1])
    assert before["blocks"][0]["sha256"] == after["blocks"][0]["sha256"]
    assert content_drift({"documents": [before]}, {"documents": [after]})
