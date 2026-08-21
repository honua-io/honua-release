"""Focused tests for the dual-target AI delivery-arc entrypoint."""
from __future__ import annotations

import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import ai_delivery_arc as arc  # noqa: E402


def test_checker_forwards_every_required_external_receipt(monkeypatch, tmp_path: Path):
    paths = {}
    for receipt_id, env_name in arc.EXTERNAL_RECEIPT_ENV:
        path = tmp_path / f"{receipt_id}.json"
        path.write_text("{}", encoding="utf-8")
        paths[receipt_id] = path
        monkeypatch.setenv(env_name, str(path))

    captured: list[str] = []

    def run(command, **_kwargs):
        captured.extend(command)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(arc.subprocess, "run", run)
    assert arc.checker(mode="live", include_receipt=True, require_real=True) == 0

    forwarded = {
        captured[index + 1]
        for index, argument in enumerate(captured)
        if argument == "--external-receipt"
    }
    assert forwarded == {
        f"{receipt_id}={path}"
        for receipt_id, path in paths.items()
    }
    assert "--require-real" in captured


def test_full_aws_arc_receipt_is_a_distinct_required_input():
    assert ("aws-ecs-provision", "E2E_AI_AWS_RECEIPT") in arc.EXTERNAL_RECEIPT_ENV
    assert ("aws-ecs-ai-delivery-arc", "E2E_AI_AWS_ARC_RECEIPT") in arc.EXTERNAL_RECEIPT_ENV
    assert len({env_name for _, env_name in arc.EXTERNAL_RECEIPT_ENV}) == len(arc.EXTERNAL_RECEIPT_ENV)
