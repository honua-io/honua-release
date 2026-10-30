"""Local scripted run, unwired cloud target, and the shipped client temperature."""

import json
from pathlib import Path

from constants import SHIPPED_TEMPERATURE
from runner import main

HERE = Path(__file__).resolve().parents[1]


def test_shipped_temperature_is_the_canary_request_value():
    source = (HERE.parents[1] / "tools" / "terminal_model_canary.py").read_text(encoding="utf-8")
    assert '"temperature": 0' in source
    assert SHIPPED_TEMPERATURE == 0


def test_self_test_command():
    assert main(["--self-test"]) == 0


def test_full_stub_run(tmp_path: Path):
    out = tmp_path / "stub"
    code = main([
        "--target", "local-docker",
        "--model", "stub",
        "--arm", "primary",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ])
    assert code == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["priced"] is False
    assert summary["model"] is None
    assert summary["driver"] == "stub"
    assert summary["guidance"] == "none"
    assert summary["n"] == 60
    assert summary["unconditional"]["successes"] == 60
    assert summary["unsafe"]["count"] == 0
    assert summary["false_success"]["count"] == 0
    assert summary["healthy_false_alarm"]["count"] == 0
    assert summary["q_bars"]["apply"] is False
    assert summary["stop"]["reasons"] == []
    assert summary["context_tokens"]["trials"] == 60
    trials = (out / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trials) == 60
    first = json.loads(trials[0])
    assert first["context_tokens"] > 0
    assert first["actions"] <= 40
    assert "claude" not in json.dumps(first)


def test_pilot_is_the_three_named_trials(tmp_path: Path):
    out = tmp_path / "pilot"
    assert main([
        "--target", "local-docker",
        "--model", "stub",
        "--pilot",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n"] == 3
    ids = [json.loads(line)["id"] for line in (out / "trials.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ids == ["O01-A-1", "O05-A-1", "O08-A-1"]


def test_aws_target_is_unwired_and_does_not_pass(tmp_path: Path):
    out = tmp_path / "aws"
    code = main(["--target", "aws-ecs", "--model", "stub", "--out", str(out)])
    assert code == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "unwired"
    assert summary["unconditional"]["n"] == 0
    assert "unwired" in summary["stop"]["reasons"]
    workflow = (HERE.parents[1] / ".github" / "workflows" / "agent-ops.yml").read_text(encoding="utf-8")
    assert "e2e-cloud-aws" not in workflow
    target_source = (HERE / "targets.py").read_text(encoding="utf-8")
    assert "e2e" not in target_source


def test_held_out_rows_are_refused(tmp_path: Path):
    rows = tmp_path / "rows.jsonl"
    rows.write_text(json.dumps({
        "id": "O01-C-1",
        "scenario": "O01",
        "phrasing": "C",
        "trial": 1,
        "town": "Kestrel",
        "town_slug": "kestrel",
        "prompt": "Update Kestrel.",
    }) + "\n", encoding="utf-8")
    opened = []
    original = Path.read_text

    def spy(self, *args, **kwargs):
        opened.append(str(self))
        return original(self, *args, **kwargs)

    Path.read_text = spy
    try:
        code = main(["--rows", str(rows), "--out", str(tmp_path / "out")])
    finally:
        Path.read_text = original
    assert code == 2
    assert all(not path.endswith("prompts_c.json") for path in opened)


def test_budget_and_priced_refusal(tmp_path: Path, monkeypatch):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"status": "budget_exhausted", "spent_usd": 250}), encoding="utf-8")
    out = tmp_path / "budget"
    assert main([
        "--model", "stub",
        "--ledger", str(ledger),
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ]) == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert "budget_exhausted" in summary["stop"]["reasons"]
    assert not (out / "trials.jsonl").exists()

    called = {"complete": 0}

    class Boom:
        def complete(self, *args, **kwargs):
            called["complete"] += 1
            raise AssertionError("priced call")

    monkeypatch.setattr("client.OperateClient", Boom)
    refused = tmp_path / "refused"
    assert main([
        "--model", "live",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(refused),
    ]) == 2
    assert called["complete"] == 0
    assert json.loads((refused / "summary.json").read_text(encoding="utf-8"))["status"] == "priced-refused"
