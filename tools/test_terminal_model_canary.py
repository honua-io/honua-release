"""Harness-only contracts for the genuine terminal-model canary (honua-release#161)."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import terminal_model_canary as canary  # noqa: E402

MANIFEST = REPO_ROOT / "platform-manifest.yaml"
JOURNEY = REPO_ROOT / "certification" / "terminal-journey" / "journey.v1.json"
PROTOCOL = REPO_ROOT / "certification" / "terminal-model-canary" / "driver-protocol.v1.json"
SCHEMA_PATH = REPO_ROOT / "certification" / "terminal-model-canary" / "receipt.schema.json"


def _endpoint(*, key: str | None = None) -> canary.EndpointConfig:
    return canary.EndpointConfig(
        base_url="http://127.0.0.1:8000/v1",
        model="qwen-local",
        api_key=key,
        api_key_env="TERMINAL_MODEL_API_KEY",
        runtime="vllm",
        quantization="awq-4bit",
    )


def _builder(endpoint: canary.EndpointConfig | None = None) -> canary.ReceiptBuilder:
    return canary.build_receipt_builder(
        manifest_path=MANIFEST,
        journey_path=JOURNEY,
        protocol_path=PROTOCOL,
        endpoint=endpoint
        or canary.EndpointConfig(None, None, None, "TERMINAL_MODEL_API_KEY", None, None),
        driver_command=None,
    )


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _green_deterministic_receipt(*, generated_at: datetime | None = None) -> dict:
    manifest = canary.load_manifest(MANIFEST)
    journey = canary.load_journey(JOURNEY)
    server = manifest["components"]["honua-server"]
    return {
        "schemaVersion": 1,
        "generatedAt": (generated_at or datetime.now(timezone.utc)).isoformat().replace(
            "+00:00", "Z"
        ),
        "evidenceKey": journey["evidenceKey"],
        "release": manifest["platformRelease"],
        "clientArtifacts": {
            name: {
                key: pin.get(key)
                for key in ("package", "version", "integrity", "digest", "sourceSha")
            }
            for name, pin in manifest["clientArtifacts"].items()
            if name in {"honua-sdk-js", "honua-mcp-server"}
        },
        "server": {
            "sourceSha": server["sha"],
            "image": f"{server['image']}@{server['digest']}",
        },
        "roster": {"status": "pass"},
        "status": "pass",
        "stages": [
            {
                "number": stage["number"],
                "stage": stage["id"],
                "command": stage["command"],
                "status": "pass",
                "evidence": {
                    "uri": f"artifact://terminal-journey/{stage['id']}",
                    "freshness": "verified-current",
                    "completeness": "complete",
                },
            }
            for stage in journey["stages"]
        ],
    }


def _workflow() -> tuple[dict, dict]:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "terminal-model-canary.yml").read_text(
            encoding="utf-8"
        )
    )
    triggers = workflow.get("on", workflow.get(True))
    return workflow, triggers


def test_skipped_receipt_validates_against_the_committed_schema():
    endpoint = canary.EndpointConfig(None, None, None, "TERMINAL_MODEL_API_KEY", None, None)
    builder = _builder(endpoint)
    canary.unavailable_receipt(builder, endpoint, None)

    receipt = builder.validated_receipt(_schema())

    assert receipt["status"] == "skipped"
    assert list(Draft202012Validator(_schema()).iter_errors(receipt)) == []
    assert receipt["journeyContract"]["sha256"] == canary._sha256(JOURNEY)
    assert receipt["journeyContract"]["path"] == "certification/terminal-journey/journey.v1.json"


def test_green_deterministic_receipt_is_parsed_and_bound_to_the_candidate(tmp_path: Path):
    receipt_path = tmp_path / "artifacts" / "terminal-journey-receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(_green_deterministic_receipt()), encoding="utf-8")

    proof = canary.validate_deterministic_receipt(
        Path("artifacts/terminal-journey-receipt.json"),
        manifest=canary.load_manifest(MANIFEST),
        journey=canary.load_journey(JOURNEY),
        repo_root=tmp_path,
    )

    assert proof["status"] == "pass"
    assert proof["candidateVerified"] is True
    assert proof["freshnessVerified"] is True
    assert proof["path"] == "artifacts/terminal-journey-receipt.json"
    assert proof["sha256"] == canary._sha256(receipt_path)


@pytest.mark.parametrize("mutation", ["arbitrary", "candidate", "stale"])
def test_invalid_deterministic_receipt_cannot_satisfy_the_green_prerequisite(
    tmp_path: Path,
    mutation: str,
):
    receipt_path = tmp_path / "receipt.json"
    receipt = _green_deterministic_receipt()
    if mutation == "arbitrary":
        receipt_path.write_text("not a receipt", encoding="utf-8")
    else:
        if mutation == "candidate":
            receipt["server"]["sourceSha"] = "0" * 40
        else:
            receipt["generatedAt"] = (
                datetime.now(timezone.utc) - timedelta(hours=25)
            ).isoformat().replace("+00:00", "Z")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(canary.CanaryError):
        canary.validate_deterministic_receipt(
            receipt_path,
            manifest=canary.load_manifest(MANIFEST),
            journey=canary.load_journey(JOURNEY),
            repo_root=tmp_path,
        )


def test_model_and_harness_actions_have_distinct_provable_attribution():
    builder = _builder(_endpoint())
    stage_id = builder.journey["stages"][0]["id"]
    assistant = builder.capture_transcript(
        "assistant",
        {"kind": "terminal_command", "command": "honua status"},
        stage_id=stage_id,
    )
    model_sequence = builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="terminal_command",
        status="pass",
        request={"command": "honua status"},
        result={"status": "ready"},
        transcript_sequence=assistant,
    )
    harness_sequence = builder.record_action(
        stage_id=stage_id,
        attribution=canary.HARNESS_DRIVEN,
        kind="setup",
        status="pass",
        request={"workspace": "clean"},
        result={"status": "ready"},
    )

    receipt = builder.validated_receipt(_schema())

    actions = {action["sequence"]: action for action in receipt["actions"]}
    assert actions[model_sequence]["attribution"] == "MODEL_SELECTED"
    assert actions[model_sequence]["selectionEvidence"] == {"transcriptSequence": assistant}
    assert actions[harness_sequence]["attribution"] == "HARNESS_DRIVEN"
    assert actions[harness_sequence]["selectionEvidence"] is None
    with pytest.raises(canary.CanaryError, match="assistant transcript"):
        builder.record_action(
            stage_id=stage_id,
            attribution=canary.MODEL_SELECTED,
            kind="tool_call",
            status="pass",
            request={"tool": "honua_get_style", "arguments": {}},
            result={},
        )


def test_endpoint_absent_is_a_visible_failed_gate_and_never_a_pass(tmp_path: Path, monkeypatch, capsys):
    for name in (
        "TERMINAL_MODEL_BASE_URL",
        "TERMINAL_MODEL_NAME",
        "TERMINAL_MODEL_API_KEY",
        "TERMINAL_MODEL_RUNTIME",
        "TERMINAL_MODEL_QUANTIZATION",
    ):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / "receipt.json"

    rc = canary.main(["--output", str(output)])

    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert rc == 1
    assert receipt["status"] == "skipped"
    assert receipt["scope"]["executionToGreen"] == "blocked"
    assert all(stage["status"] == "skipped" for stage in receipt["stages"])
    assert "never passed" in receipt["notices"][0]
    output_text = capsys.readouterr().out
    assert "terminal model canary: fail (skipped)" in output_text
    assert "TERMINAL_MODEL" not in output_text


def test_recoverable_error_bookkeeping_binds_harness_injection_to_model_recovery():
    builder = _builder(_endpoint())
    stage_id = builder.receipt["errorInjection"]["stageId"]
    injection_sequence = builder.arm_injection(
        stage_id=stage_id,
        result={"status": "armed", "errorId": "recoverable-error-1", "recoverable": True},
    )
    first_transcript = builder.capture_transcript(
        "assistant",
        {"kind": "tool_call", "tool": "honua_render_map", "arguments": {}},
        stage_id=stage_id,
    )
    trigger_sequence = builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="tool_call",
        status="fail",
        request={"tool": "honua_render_map", "arguments": {}},
        result={"injectedError": {"id": "recoverable-error-1", "recoverable": True}},
        transcript_sequence=first_transcript,
    )
    builder.observe_injected_error(trigger_sequence)
    recovery_transcript = builder.capture_transcript(
        "assistant",
        {"kind": "tool_call", "tool": "honua_get_style", "arguments": {}},
        stage_id=stage_id,
    )
    recovery_sequence = builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="tool_call",
        status="pass",
        request={"tool": "honua_get_style", "arguments": {}},
        result={"status": "ok"},
        transcript_sequence=recovery_transcript,
    )
    builder.record_recovery(
        recovery_sequence,
        {"id": "recoverable-error-1", "recovered": True},
    )

    receipt = builder.validated_receipt(_schema())
    injection = receipt["errorInjection"]
    assert injection == {
        "id": "recoverable-error-1",
        "stageId": stage_id,
        "injectedBy": "HARNESS_DRIVEN",
        "recoverable": True,
        "status": "recovered",
        "injectionActionSequence": injection_sequence,
        "triggeringModelActionSequence": trigger_sequence,
        "recoveryModelActionSequence": recovery_sequence,
    }
    assert receipt["actions"][injection_sequence - 1]["attribution"] == "HARNESS_DRIVEN"


def test_recoverable_error_bookkeeping_rejects_an_unrelated_success():
    builder = _builder(_endpoint())
    stage_id = builder.receipt["errorInjection"]["stageId"]
    builder.arm_injection(
        stage_id=stage_id,
        result={"status": "armed", "errorId": "recoverable-error-1", "recoverable": True},
    )
    failed_transcript = builder.capture_transcript(
        "assistant",
        {"kind": "tool_call", "tool": "honua_render_map", "arguments": {}},
        stage_id=stage_id,
    )
    failed_action = builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="tool_call",
        status="fail",
        request={"tool": "honua_render_map", "arguments": {}},
        result={"injectedError": {"id": "recoverable-error-1", "recoverable": True}},
        transcript_sequence=failed_transcript,
    )
    builder.observe_injected_error(failed_action)
    successful_transcript = builder.capture_transcript(
        "assistant",
        {"kind": "terminal_command", "command": "honua status"},
        stage_id=stage_id,
    )
    unrelated_success = builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="terminal_command",
        status="pass",
        request={"command": "honua status"},
        result={"status": "ok"},
        transcript_sequence=successful_transcript,
    )

    with pytest.raises(canary.CanaryError, match="did not prove recovery"):
        builder.record_recovery(
            unrelated_success,
            {"id": "different-error", "recovered": True},
        )


def test_candidate_proxy_configuration_rejects_direct_provider_urls():
    local = canary.EndpointConfig(
        base_url="http://127.0.0.1:8080/api",
        model="claude-sonnet",
        api_key=None,
        api_key_env="TERMINAL_MODEL_API_KEY",
        runtime="candidate",
        quantization="provider-managed",
    )
    hosted = canary.EndpointConfig(
        base_url="https://models.example.test/v1/chat/completions",
        model="hosted-model",
        api_key="top-secret-key",
        api_key_env="TERMINAL_MODEL_API_KEY",
        runtime="hosted",
        quantization="provider-managed",
        require_api_key=True,
    )

    assert local.proxy_chat_url() == "http://127.0.0.1:8080/api/v1/studio/ai/chat"
    assert local.evidence()["authentication"] == {
        "mode": "none",
        "credentialReference": None,
        "required": False,
    }
    with pytest.raises(canary.CanaryError, match="direct-provider"):
        hosted.proxy_chat_url()
    assert hosted.evidence()["authentication"] == {
        "mode": "bearer-env",
        "credentialReference": "env:TERMINAL_MODEL_API_KEY",
        "required": True,
    }
    assert "top-secret-key" not in json.dumps(hosted.evidence())


def test_candidate_proxy_binds_trust_anchor_event_names_and_requested_model(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest = {
        "requiredForCertification": True,
        "keys": [{
            "keyId": "candidate-1",
            "algorithm": "Ed25519",
            "publicKey": base64.b64encode(public).decode(),
            "fingerprint": f"sha256:{hashlib.sha256(public).hexdigest()}",
        }],
    }
    manifest_digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    endpoint = replace(
        _endpoint(),
        base_url="http://127.0.0.1:8000/api",
        signing_manifest_sha256=manifest_digest,
    )
    certification = {
        "candidateId": "sha256:candidate",
        "releaseId": "2026.1",
        "endpointIdentity": endpoint.validated_base_url(),
        "actionId": "publish",
        "runNonce": "random-run-nonce",
    }
    request_body = {
        "model": endpoint.model,
        "messages": [{"role": "user", "content": "advance"}],
        "temperature": 0,
        "certification": certification,
    }
    provider_events = [
        {"event": "text_delta", "data": {"text": "{}"}},
        {"event": "message_stop", "data": {"promptTokens": 1, "completionTokens": 1}},
    ]
    canonical_events = json.dumps(provider_events, sort_keys=True, separators=(",", ":")).encode()
    transcript = {
        **certification,
        "model": endpoint.model,
        "provider": "candidate-proxy",
        "issuedAt": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "request": base64.b64encode(
            json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()
        ).decode(),
        "providerEvents": base64.b64encode(canonical_events).decode(),
        "terminalResultDigest": base64.b64encode(hashlib.sha256(canonical_events).digest()).decode(),
    }
    transcript_bytes = json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode()
    signed = {
        "keyId": "candidate-1",
        "canonicalTranscript": base64.b64encode(transcript_bytes).decode(),
        "transcriptDigest": hashlib.sha256(transcript_bytes).hexdigest(),
        "signature": base64.b64encode(key.sign(transcript_bytes)).decode(),
    }

    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self):
            return self.payload

    sse = "\n\n".join(
        [
            "event: text_delta\ndata: {\"text\":\"{}\"}",
            "event: message_stop\ndata: {\"promptTokens\":1,\"completionTokens\":1}",
            f"event: transcript_provenance\ndata: {json.dumps({'provenance': signed})}",
        ]
    ).encode()
    responses = iter([Response(json.dumps({"transcriptSigning": manifest}).encode()), Response(sse)])
    monkeypatch.setattr(canary.urllib.request, "urlopen", lambda *args, **kwargs: next(responses))

    _, _, _, evidence = canary.CandidateProxyClient(endpoint).complete(
        request_body["messages"], certification
    )

    assert evidence["manifestDigest"] == manifest_digest
    assert evidence["reportedModel"] == endpoint.model


def test_run_nonces_are_random_and_run_scoped():
    first = canary._new_run_nonce()
    second = canary._new_run_nonce()
    assert first != second
    assert len(first) >= 40 and len(second) >= 40


def test_local_authentication_mode_does_not_read_a_present_hosted_key(monkeypatch):
    monkeypatch.setenv("TERMINAL_MODEL_API_KEY", "hosted-secret")

    endpoint = canary.EndpointConfig.from_environment(
        base_url="http://127.0.0.1:8000/v1",
        model="qwen-local",
        use_api_key=False,
    )

    assert endpoint.api_key is None
    assert endpoint.evidence()["authentication"]["mode"] == "none"


def test_missing_required_hosted_key_is_a_visible_skip():
    endpoint = canary.EndpointConfig(
        base_url="https://models.example.test/v1",
        model="hosted-model",
        api_key=None,
        api_key_env="TERMINAL_MODEL_API_KEY",
        runtime="hosted",
        quantization="provider-managed",
        require_api_key=True,
    )
    builder = _builder(endpoint)

    receipt = canary.unavailable_receipt(builder, endpoint, canary.DEFAULT_DRIVER)

    assert receipt["status"] == "skipped"
    assert "env:TERMINAL_MODEL_API_KEY" in receipt["notices"][0]
    assert "never passed" in receipt["notices"][0]


def test_transcript_and_action_capture_redacts_credentials_before_receipt_storage():
    builder = _builder(_endpoint(key="top-secret-key"))
    stage_id = builder.journey["stages"][0]["id"]
    assistant = builder.capture_transcript(
        "assistant",
        "Authorization: Bearer top-secret-key api_key=top-secret-key",
        stage_id=stage_id,
    )
    builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="terminal_command",
        status="pass",
        request={"command": "honua status", "token": "top-secret-key"},
        result={"status": "ok"},
        transcript_sequence=assistant,
    )

    serialized = json.dumps(builder.validated_receipt(_schema()))

    assert "top-secret-key" not in serialized
    assert "[REDACTED]" in serialized


def test_schema_rejects_false_model_attribution_without_selection_evidence():
    builder = _builder(_endpoint())
    stage_id = builder.journey["stages"][0]["id"]
    assistant = builder.capture_transcript("assistant", "{}", stage_id=stage_id)
    builder.record_action(
        stage_id=stage_id,
        attribution=canary.MODEL_SELECTED,
        kind="terminal_command",
        status="pass",
        request={"command": "honua status"},
        result={},
        transcript_sequence=assistant,
    )
    forged = copy.deepcopy(builder.receipt)
    forged["actions"][0]["selectionEvidence"] = None

    errors = list(Draft202012Validator(_schema()).iter_errors(forged))

    assert errors


def test_workflow_is_manual_only_and_references_the_single_123_journey_contract():
    workflow, triggers = _workflow()
    assert set(triggers) == {"workflow_dispatch"}
    assert "driver_command" not in triggers["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["harness"]
    commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "certification/terminal-journey/journey.v1.json" in commands
    assert "tools/terminal_model_canary.py" in commands
    assert "schedule" not in triggers and "pull_request" not in triggers
    assert job["runs-on"] == "${{ inputs.runner }}"


def test_protocol_declares_123_as_the_live_adapter_owner():
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    assert protocol["owner"] == "honua-release#123"
    assert protocol["deterministicReceiptRequirements"] == {
        "status": "pass",
        "maxAgeHours": 24,
        "candidateBinding": "exact release, server source/image pins, and #123 client artifact pins",
        "stageBinding": (
            "exact imported stage order, IDs, commands, pass status, verified-current freshness, "
            "and complete evidence"
        ),
    }
    assert set(protocol["operations"]) == {
        "setup",
        "observe",
        "execute",
        "inject_error",
        "approve",
        "verify",
        "teardown",
    }


def test_driver_adapter_rejects_a_response_missing_protocol_fields(tmp_path: Path):
    driver = tmp_path / "incomplete_driver.py"
    driver.write_text(
        "import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'status': 'ready'}))\n",
        encoding="utf-8",
    )
    adapter = canary.DriverAdapter(driver, json.loads(PROTOCOL.read_text(encoding="utf-8")))

    with pytest.raises(canary.CanaryError, match="omitted required fields"):
        adapter.invoke("setup", {})


def test_harness_source_imports_stage_ids_instead_of_duplicating_them():
    journey = json.loads(JOURNEY.read_text(encoding="utf-8"))
    source = (REPO_ROOT / "tools" / "terminal_model_canary.py").read_text(encoding="utf-8")

    assert all(f'"{stage["id"]}"' not in source for stage in journey["stages"])
