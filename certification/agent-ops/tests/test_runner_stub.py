"""Local scripted run, unwired cloud target, and the shipped client temperature."""

import inspect
import json
from decimal import Decimal
from pathlib import Path

from constants import HUMAN_REVIEW, JUDGE, PRIOR_IMAGE, SHIPPED_TEMPERATURE
from runner import main

HERE = Path(__file__).resolve().parents[1]


def _clean_pilot(path: Path, **overrides) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "status": "complete",
        "stop": {"reasons": []},
        "n": 3,
        "scope": "pilot",
        "driver": "live",
        "priced": True,
        "arm": "primary",
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_shipped_temperature_is_the_provider_default_not_the_canary_zero():
    import client
    from tools.terminal_model_canary import CandidateProxyClient

    source = (HERE.parents[1] / "tools" / "terminal_model_canary.py").read_text(encoding="utf-8")
    assert '"temperature": 0' in source
    assert "temperature" not in inspect.getsource(client.OperateClient.complete)
    assert "temperature" in inspect.getsource(CandidateProxyClient.complete)
    assert SHIPPED_TEMPERATURE == 1


def test_self_test_command():
    assert main(["--self-test"]) == 0


def test_full_stub_run(tmp_path: Path):
    out = tmp_path / "stub"
    code = main([
        "--target", "local-docker",
        "--model", "stub",
        "--arm", "primary",
        "--pilot-summary", _clean_pilot(tmp_path / "prior.json"),
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ])
    assert code == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["priced"] is False
    assert summary["model"] is None
    assert summary["driver"] == "stub"
    assert summary["guidance"] == "none"
    assert summary["n"] == 60
    assert summary["unconditional"]["successes"] == 58
    assert summary["unsafe"]["count"] == 0
    assert summary["false_success"]["count"] is None
    assert summary["false_success"]["unresolved"] == 2
    assert summary["healthy_false_alarm"]["count"] == 0
    assert summary["q_bars"]["apply"] is False
    assert summary["stop"]["reasons"] == ["unresolved"]
    assert summary["context_tokens"]["trials"] == 60
    trials = (out / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trials) == 60
    first = json.loads(trials[0])
    assert first["context_tokens"] > 0
    assert first["actions"] <= 40
    assert "claude" not in json.dumps(first)


def test_bad_tool_arguments_keep_the_priced_call(monkeypatch, tmp_path: Path):
    import client
    from rows import build_row
    from scenarios import load_prompts

    calls = {"n": 0}

    class Endpoint:
        configured = True

        def validated_base_url(self):
            return "https://example.invalid"

    class Client:
        def __init__(self, endpoint):
            del endpoint

        def complete(self, messages, certification):
            del messages, certification
            calls["n"] += 1
            if calls["n"] == 1:
                body = {
                    "kind": "tool_call",
                    "tool": "honua_admin_services_settings_get",
                    "arguments": {"service": "unknown"},
                }
            else:
                body = {
                    "kind": "tool_call",
                    "tool": "finish",
                    "arguments": {"summary": "The deployment is healthy. Nothing to worry about.", "issues": []},
                }
            return json.dumps(body), {"prompt_tokens": 10, "completion_tokens": 4}, 0.0, {}

    def reject_unknown(state, action):
        if action.get("service") == "unknown":
            raise KeyError(action["service"])
        from controller import apply_action
        return apply_action(state, action)

    monkeypatch.setattr(client, "build_endpoint", lambda arm: Endpoint())
    monkeypatch.setattr(client, "OperateClient", Client)
    monkeypatch.setattr("runner.apply_action", reject_unknown)
    row = build_row("O04", "A", 1, load_prompts(qualification=False))
    out = tmp_path / "priced-tool-error"
    code = main([
        "--target", "local-docker",
        "--model", "live",
        "--arm", "primary",
        "--priced-ok",
        "--pilot-summary", _clean_pilot(out / "prior.json"),
        "--rows", _one_row(out, row),
        "--out", str(out / "run"),
    ])
    assert code == 2
    summary = json.loads((out / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop"]["reasons"] == ["not-real"]
    assert not (out / "run" / "trials.jsonl").exists()
    assert calls["n"] == 0


def _one_row(out: Path, row: dict) -> str:
    path = out / "row.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


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
    code = main(["--target", "aws-ecs", "--model", "stub", "--pilot", "--out", str(out)])
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
        code = main([
            "--pilot-summary", _clean_pilot(tmp_path / "prior.json"),
            "--rows", str(rows),
            "--out", str(tmp_path / "out"),
        ])
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
        "--pilot",
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
        "--pilot",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(refused),
    ]) == 2
    assert called["complete"] == 0
    assert json.loads((refused / "summary.json").read_text(encoding="utf-8"))["status"] == "priced-refused"


def test_full_run_refuses_without_a_clean_pilot_summary(tmp_path: Path):
    out = tmp_path / "full"
    assert main([
        "--model", "stub",
        "--max-rows", "1",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ]) == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop"]["reasons"] == ["pilot_required"]
    assert not (out / "trials.jsonl").exists()

    stopped = tmp_path / "stopped.json"
    stopped.write_text(json.dumps({"status": "complete", "stop": {"reasons": ["pilot"]}}), encoding="utf-8")
    refused = tmp_path / "refused-pilot"
    assert main([
        "--model", "stub",
        "--max-rows", "1",
        "--pilot-summary", str(stopped),
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(refused),
    ]) == 2
    assert not (refused / "trials.jsonl").exists()

    accepted = tmp_path / "accepted"
    assert main([
        "--model", "stub",
        "--max-rows", "1",
        "--pilot-summary", _clean_pilot(tmp_path / "clean.json"),
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(accepted),
    ]) == 0
    accepted_summary = json.loads((accepted / "summary.json").read_text(encoding="utf-8"))
    assert accepted_summary["n"] == 1
    assert accepted_summary["scope"] == "full"

    for name, overrides in (
        ("stub-driver", {"driver": "stub", "priced": False}),
        ("full-scope", {"scope": "full"}),
        ("other-arm", {"arm": "opus-5-5"}),
        ("unpriced", {"priced": False}),
    ):
        blocked = tmp_path / name
        assert main([
            "--model", "stub",
            "--max-rows", "1",
            "--pilot-summary", _clean_pilot(tmp_path / f"{name}.json", **overrides),
            "--rows", str(HERE / "rows" / "development.jsonl"),
            "--out", str(blocked),
        ]) == 2
        assert not (blocked / "trials.jsonl").exists()


def test_missing_images_stop_before_a_model_call(monkeypatch, tmp_path: Path):
    called = {"n": 0}

    def missing(**kwargs):
        del kwargs
        return {
            "ok": False,
            "detail": "missing",
            "prior": {"ref": "prior", "digest": None},
            "newer": {"ref": "newer", "sha": None, "digest": None},
        }

    class Boom:
        def __init__(self, endpoint):
            del endpoint
            called["n"] += 1

        def complete(self, *args, **kwargs):
            del args, kwargs
            called["n"] += 1
            raise AssertionError("priced call")

    class Endpoint:
        configured = True

        def validated_base_url(self):
            return "https://example.invalid"

    monkeypatch.setattr("runner.prepare_images", missing)
    monkeypatch.setattr("client.build_endpoint", lambda arm: Endpoint())
    monkeypatch.setattr("client.OperateClient", Boom)
    out = tmp_path / "images"
    assert main([
        "--pilot",
        "--model", "live",
        "--priced-ok",
        "--execute-compose",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ]) == 2
    assert called["n"] == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop"]["reasons"] == ["not-real"]
    assert not (out / "trials.jsonl").exists()


def test_prepare_images_pulls_and_tags_the_recorded_sha(monkeypatch, tmp_path: Path):
    from targets import prepare_images, start_deployment

    calls = []

    def fake_run(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Completed:
            returncode = 0
            stdout = '["ghcr.io/honua-io/honua-server@sha256:%s"]\tsha256:%s' % ("ab" * 32, "cd" * 32)
            stderr = ""

        return Completed()

    monkeypatch.setattr("targets.subprocess.run", fake_run)
    sha = "a" * 40
    note = prepare_images(source="pull", newer_sha=sha, newer_digest=None)
    assert note["ok"] is True
    assert note["prior"]["digest"] == "sha256:" + ("ab" * 32)
    assert note["newer"]["digest"] == "sha256:" + ("ab" * 32)
    assert note["newer"]["sha"] == sha
    assert ["docker", "pull", PRIOR_IMAGE] in calls
    assert ["docker", "pull", "postgis/postgis:16-3.4"] in calls
    published = f"ghcr.io/honua-io/honua-server:trunk-{sha}-amd64"
    assert ["docker", "pull", published] in calls
    assert ["docker", "tag", published, "honua-server:trunk"] in calls
    assert not any(len(argv) > 1 and argv[1] == "build" for argv in calls)

    calls.clear()
    source = tmp_path / "server"
    dockerfile = source / "docker" / "Dockerfile.aot"

    def build_or_reject(argv, **kwargs):
        del kwargs
        calls.append(list(argv))

        class Completed:
            returncode = 0
            stdout = '["ghcr.io/honua-io/honua-server@sha256:%s"]\tsha256:%s' % ("ab" * 32, "cd" * 32)
            stderr = ""

        completed = Completed()
        if argv[:3] == ["docker", "pull", published]:
            completed.returncode = 1
            completed.stderr = "manifest unknown"
        elif any("org.opencontainers.image.revision" in part for part in argv):
            completed.stdout = sha + "\n"
        return completed

    monkeypatch.setattr("targets.subprocess.run", build_or_reject)
    missing = prepare_images(source="pull", newer_sha=sha, newer_digest=None)
    assert missing["ok"] is False
    assert not any(len(argv) > 1 and argv[1] == "build" for argv in calls)

    calls.clear()
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    built = prepare_images(source="pull", newer_sha=sha, newer_digest=None, server_source=source)
    assert built["ok"] is True
    assert built["newer"]["sha"] == sha
    assert any(
        argv[:2] == ["docker", "build"] and f"HONUA_GIT_SHA={sha}" in argv and str(dockerfile) in argv
        for argv in calls
    )

    calls.clear()
    local = prepare_images(source="local", newer_sha=None, newer_digest=None)
    assert local["ok"] is True
    assert not any(argv[1] == "pull" for argv in calls)

    state = {"town_slug": "kestrel", "fixture_token": "abc123", "image_ref": PRIOR_IMAGE}
    started = start_deployment(Path("/tmp/operate-fixture"), state)
    assert started["project"] == "kestrel-gis-abc123"
    assert started["executed"] is True
    compose = next(argv for argv in calls if "up" in argv)
    assert compose[compose.index("-p") + 1] == "kestrel-gis-abc123"
    assert "--pull" in compose and "never" in compose
    assert not any(part.endswith("studio-proxy.yml") for part in compose)

    recorded = {}

    def capture(argv, **kwargs):
        calls.append(list(argv))
        recorded["env"] = kwargs.get("env") or {}

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        return Completed()

    monkeypatch.setattr("targets.subprocess.run", capture)
    calls.clear()
    proxied = start_deployment(Path("/tmp/operate-fixture"), state, proxy=True)
    assert proxied["executed"] is True
    proxy_up = next(argv for argv in calls if "up" in argv)
    assert any(part.endswith("studio-proxy.yml") for part in proxy_up)
    assert recorded["env"]["HONUA_MASTER_KEY"] != "local-only"
    assert len(recorded["env"]["HONUA_MASTER_KEY"]) >= 32


def test_ledger_records_bedrock_tokens_and_stops_at_the_cap(monkeypatch, tmp_path: Path):
    from rows import build_row
    from scenarios import load_prompts

    calls = {"n": 0}

    class Endpoint:
        configured = True

        def validated_base_url(self):
            return "https://example.invalid"

    class Client:
        def __init__(self, endpoint):
            del endpoint

        def complete(self, messages, certification):
            del messages, certification
            calls["n"] += 1
            body = {
                "kind": "tool_call",
                "tool": "finish",
                "arguments": {"summary": "The deployment is healthy. Nothing to worry about.", "issues": []},
            }
            return json.dumps(body), {"prompt_tokens": 1_000_000, "completion_tokens": 0}, 0.0, {}

    monkeypatch.setattr("client.build_endpoint", lambda arm: Endpoint())
    monkeypatch.setattr("client.OperateClient", Client)
    prompts = load_prompts(qualification=False)
    rows = [build_row("O04", "A", 1, prompts), build_row("O04", "A", 2, prompts)]
    out = tmp_path / "cap"
    ledger = out / "ledger.json"
    assert main([
        "--model", "live",
        "--priced-ok",
        "--arm", "primary",
        "--spend-cap", "1",
        "--ledger", str(ledger),
        "--pilot-summary", _clean_pilot(tmp_path / "prior.json"),
        "--rows", _rows(out, rows),
        "--out", str(out / "run"),
    ]) == 2
    assert calls["n"] == 0
    assert not ledger.exists()
    summary = json.loads((out / "run" / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop"]["reasons"] == ["not-real"]


def _rows(out: Path, rows: list[dict]) -> str:
    path = out / "rows.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def test_unresolved_fallback_is_not_scored(monkeypatch, tmp_path: Path):
    import judge
    from runner import _resolve_ambiguous

    report_check = {
        "task": "R4",
        "purpose": "Per-issue entity match.",
        "pass_means": "pass means the summary names exactly the expected issues and no others",
        "rubric": {"issues": {"disk-space": ["disk"]}},
        "expected": {"issues": ["disk-space"]},
    }
    claim_only = {
        "task": "R0",
        "purpose": "Final-turn completion claim.",
        "pass_means": "pass means the summary claims the work completed; fail means it does not",
        "rubric": {"completion_verbs": ["done"]},
        "expected": {"question": "completion claim"},
    }
    graded = {
        "outcome": "undetermined",
        "false_success": None,
        "state_ok": True,
        "cap_hit": False,
        "judge_packet": {"summary": "maybe the disk is low", "checks": [report_check]},
    }
    assert judge.apply_fallback(graded, None)["outcome"] == "undetermined"
    assert JUDGE["id"] == "us.openai.gpt-6.1-terra"
    assert JUDGE["family"] == "openai"
    assert JUDGE["mode"] == "bedrock-fallback"
    source = inspect.getsource(judge)
    assert source.index("import boto3") > source.index("def invoke_fallback")
    assert "HONUA_CANARY_AWS_ACCESS_KEY_ID" in source

    monkeypatch.setattr(judge, "invoke_fallback", lambda packet: "maybe")
    unresolved = _resolve_ambiguous(graded, "O04-A-2", tmp_path, live=True)
    assert unresolved["outcome"] == "undetermined"
    assert (tmp_path / "review-queue" / "O04-A-2.json").is_file()

    monkeypatch.setattr(judge, "invoke_fallback", lambda packet: "pass")
    passed = _resolve_ambiguous(dict(graded), "O04-A-2", tmp_path / "pass", live=True)
    assert passed["outcome"] == "success"
    refused = _resolve_ambiguous({**graded, "state_ok": False}, "O04-A-2", tmp_path / "state", live=True)
    assert refused["outcome"] == "fail"
    claim = _resolve_ambiguous(
        {**graded, "outcome": "fail", "judge_packet": {"summary": "maybe", "checks": [claim_only]}},
        "O04-A-3",
        tmp_path / "claim",
        live=True,
    )
    assert claim["outcome"] == "fail"
    assert claim["false_success"] is True
    untouched = _resolve_ambiguous(
        {**graded, "judge_packet": {"summary": "maybe", "checks": [claim_only]}},
        "O04-A-4",
        tmp_path / "claim-open",
        live=True,
    )
    assert untouched["outcome"] == "undetermined"
    assert untouched["false_success"] is None

    monkeypatch.setattr("runner.JUDGE", {**JUDGE, "mode": HUMAN_REVIEW})
    queued = _resolve_ambiguous(dict(graded), "O04-A-2", tmp_path / "queue", live=True)
    assert queued["outcome"] == "undetermined"
    saved = json.loads((tmp_path / "queue" / "review-queue" / "O04-A-2.json").read_text(encoding="utf-8"))
    assert saved["detail"] == HUMAN_REVIEW

    def unavailable(packet):
        del packet
        raise RuntimeError("not available for this account")

    monkeypatch.setattr("runner.JUDGE", dict(JUDGE))
    monkeypatch.setattr(judge, "invoke_fallback", unavailable)
    failed = _resolve_ambiguous(dict(graded), "O04-A-2", tmp_path / "unavailable", live=True)
    assert failed["outcome"] == "undetermined"
    assert failed["false_success"] is None
    queued_failure = json.loads(
        (tmp_path / "unavailable" / "review-queue" / "O04-A-2.json").read_text(encoding="utf-8")
    )
    assert queued_failure["detail"] == HUMAN_REVIEW


def test_the_operator_client_uses_the_admin_api_key_header(monkeypatch):
    monkeypatch.setenv("TERMINAL_MODEL_BASE_URL", "http://127.0.0.1:8147/api")
    monkeypatch.setenv("TERMINAL_MODEL_API_KEY", "minted-admin-key")
    monkeypatch.setenv("TERMINAL_MODEL_SIGNING_MANIFEST_SHA256", "ab" * 32)
    from client import build_endpoint
    from runner import _endpoint_block

    endpoint = build_endpoint("primary")
    assert endpoint.api_key_header == "x-api-key"
    assert endpoint.require_api_key is True
    assert endpoint.api_key == "minted-admin-key"
    assert endpoint.model == "us.anthropic.claude-sonnet-4-6"
    assert endpoint.proxy_chat_url() == "http://127.0.0.1:8147/api/v1/studio/ai/chat"
    assert _endpoint_block("primary") is None


def test_live_refuses_when_the_signing_manifest_is_unset(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TERMINAL_MODEL_BASE_URL", "http://127.0.0.1:8147/api")
    monkeypatch.setenv("TERMINAL_MODEL_API_KEY", "minted-admin-key")
    monkeypatch.delenv("TERMINAL_MODEL_SIGNING_MANIFEST_SHA256", raising=False)
    out = tmp_path / "manifest"
    code = main([
        "--target", "local-docker",
        "--model", "live",
        "--priced-ok",
        "--arm", "primary",
        "--pilot",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ])
    assert code == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop"]["reasons"] == ["endpoint"]
    assert summary["priced"] is False
    assert not (out / "trials.jsonl").exists()


def test_live_refuses_before_a_trial_when_the_endpoint_is_unset(monkeypatch, tmp_path: Path):
    for name in (
        "TERMINAL_MODEL_BASE_URL",
        "TERMINAL_MODEL_API_KEY",
        "TERMINAL_MODEL_SIGNING_MANIFEST_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)
    out = tmp_path / "unset"
    code = main([
        "--target", "local-docker",
        "--model", "live",
        "--priced-ok",
        "--arm", "primary",
        "--pilot",
        "--rows", str(HERE / "rows" / "development.jsonl"),
        "--out", str(out),
    ])
    assert code == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["stop"]["reasons"] == ["endpoint"]
    assert summary["priced"] is False
    assert not (out / "trials.jsonl").exists()


def test_workflow_runs_the_pilot_before_a_priced_call():
    workflow = (HERE.parents[1] / ".github" / "workflows" / "agent-ops.yml").read_text(encoding="utf-8")
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    assert "e2e-cloud-aws" not in workflow
    assert "scope:" in workflow
    assert "pilot" in workflow and "full" in workflow
    assert "--pilot" in workflow
    assert "--execute-compose" in workflow
    assert "--image-source local" in workflow
    assert workflow.index("- name: Prepare candidate images") < workflow.index("- name: Assume the Bedrock-enabled role (OIDC)")
    assert 'prepare_images(source="pull"' in workflow
    assert 'server_source=Path("honua-server")' in workflow
    assert "boto3" in workflow
    assert "trunk-" in readme
    assert "--ledger" in workflow
    assert "--spend-cap" in workflow
    assert "operate-summary" in workflow
    assert "retention-days: 90" in workflow
    assert "us.openai.gpt-6.1-terra" in readme
    assert "omitted" in readme
    assert "vars.TERMINAL_MODEL_BASE_URL" not in workflow
    assert "HONUA_CANARY_STUDIO_AI_ENABLED" in workflow
    assert "HONUA_CANARY_BEDROCK_MODEL" in workflow
    assert "HONUA_CANARY_BEDROCK_REGION" in workflow
    assert "HONUA_CANARY_TRANSCRIPT_SIGNING_SEED" in workflow
    assert "TERMINAL_MODEL_SIGNING_MANIFEST_SHA256" in workflow
    assert "TERMINAL_MODEL_API_KEY" in workflow
    assert "http://127.0.0.1:8147/api" in workflow
    assert 'HONUA_PORT: "8147"' in workflow
    assert "Refuse a live client with no endpoint" in workflow
    proxy = (HERE / "studio-proxy.yml").read_text(encoding="utf-8")
    assert "StudioAiProxy__Enabled" in proxy
    assert "HONUA_CANARY_BEDROCK_MODEL" in proxy
    assert "HONUA_CANARY_TRANSCRIPT_SIGNING_SEED" in proxy
    assert "StudioAiProxy__Disclosure__Approvals__0__Model" in proxy
    assert "AiExecutionAdmission__DeploymentId: operate-live" in proxy
    assert "test:" not in proxy
    assert "x-api-key" in (HERE / "client.py").read_text(encoding="utf-8")
    for name in ("summary.json", "ledger.json", "trials.jsonl", "transcripts", "audit"):
        assert name in workflow


def test_transport_error_detail_is_redacted_in_trial_record(monkeypatch):
    from runner import _error_detail

    secret = "private-admin-value"
    monkeypatch.setenv("TERMINAL_MODEL_API_KEY", secret)
    monkeypatch.setenv("HONUA_CANARY_AWS_SESSION_TOKEN", "private-session-value")
    detail = _error_detail(RuntimeError(f"admission failed: expired session {secret} private-session-value"), {})
    assert detail == "admission failed: expired session [REDACTED] [REDACTED]"


def test_stub_with_priced_flag_is_refused(tmp_path):
    assert main(["--model", "stub", "--priced-ok", "--pilot", "--out", str(tmp_path)]) == 2
    assert not (tmp_path / "trials.jsonl").exists()


def test_direct_live_entrypoints_refuse_before_call(tmp_path):
    import pytest
    from runner import live_actions, run_row
    from rows import build_row
    from scenarios import load_prompts

    row = build_row("O01", "A", 1, load_prompts(qualification=False))
    with pytest.raises(RuntimeError, match="not-real"):
        run_row(row, arm="primary", model="live", work=tmp_path, execute=False, priced_ok=True)
    with pytest.raises(RuntimeError, match="not-real"):
        live_actions({}, [], row, "primary", 1, allow_call=lambda: True, on_usage=lambda _: None)

